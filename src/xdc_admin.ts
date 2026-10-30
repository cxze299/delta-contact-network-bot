import { Service, pid } from "./service";
import { type Row } from "./db";
import { hmac, hash, random, equal } from "./crypto";
import { UserError, now, later } from "./protocol";
export class Admin {
  readonly version: string;
  readonly verifier: { salt: string; key: string; iterations: number };
  constructor(readonly service: Service) {
    const [iterations, salt, key] = service.config.verifier.split(":");
    this.verifier = { iterations: Number(iterations), salt, key };
    this.version = hash(service.config.verifier);
  }
  get s() {
    return this.service.s;
  }
  system(u: Row) {
    return !!this.s.one(
      "SELECT 1 FROM xdc_admin_grants WHERE user_id=? AND version=?",
      u.id,
      this.version,
    );
  }
  networks(u: Row, system = this.system(u)) {
    return this.s.all(
      `SELECT n.id,n.public_id,n.name,n.status,n.join_enabled,(SELECT count(*) FROM memberships m WHERE m.network_id=n.id AND m.status='active') member_count FROM networks n WHERE ?=1 OR EXISTS(SELECT 1 FROM memberships a WHERE a.network_id=n.id AND a.user_id=? AND a.status='active' AND a.role='network_admin') ORDER BY n.id`,
      Number(system),
      u.id,
    );
  }
  require(u: Row, network: number | null, system = false) {
    if (system) {
      if (!this.system(u)) throw new UserError("请先验证系统管理员密钥");
      return;
    }
    if (this.system(u)) return;
    if (
      !network ||
      !this.s.one(
        "SELECT 1 FROM memberships WHERE user_id=? AND network_id=? AND status='active' AND role='network_admin'",
        u.id,
        network,
      )
    )
      throw new UserError("没有管理此网络的权限");
  }
  open(u: Row, token: string) {
    if (this.system(u) || this.networks(u).length) {
      const ctx = this.s.one(
        "SELECT * FROM v2_instances WHERE token=?",
        token,
      )!;
      if (!ctx.admin_network)
        this.s.run(
          "UPDATE v2_instances SET admin_network=? WHERE token=?",
          this.networks(u)[0]?.id ?? null,
          token,
        );
    } else this.challenge(u, token);
  }
  challenge(u: Row, token: string) {
    this.s.run(
      "INSERT OR REPLACE INTO xdc_admin_challenges VALUES(?,?,?,?)",
      token,
      u.id,
      hash(random(32)),
      later(5 / 1440),
    );
  }
  unlock(u: Row, token: string, proof: string) {
    const c = this.s.one(
      "SELECT * FROM xdc_admin_challenges WHERE token=? AND user_id=?",
      token,
      u.id,
    );
    this.challenge(u, token);
    if (!c || c.expires_at <= now())
      throw new UserError("验证已过期，请重新输入");
    const expected = hmac(
      Buffer.from(this.verifier.key, "hex"),
      "contact-admin-v1:" + c.nonce + ":" + token,
    );
    if (!equal(proof, expected)) throw new UserError("密钥验证失败");
    this.s.run(
      "INSERT OR REPLACE INTO xdc_admin_grants VALUES(?,?,?)",
      u.id,
      this.version,
      now(),
    );
    this.s.run("DELETE FROM xdc_admin_challenges WHERE token=?", token);
    this.open(u, token);
    this.service.audit(u, null, "xdc_admin_unlock");
    return "管理员验证成功。";
  }
  select(u: Row, token: string, network: number, page = 0) {
    this.require(u, network);
    if (!this.s.one("SELECT 1 FROM networks WHERE id=?", network))
      throw new UserError("网络不存在");
    this.s.run(
      "UPDATE v2_instances SET admin_network=?,admin_page=? WHERE token=?",
      network,
      page,
      token,
    );
  }
  action(
    u: Row,
    network: number | null,
    op: string,
    data: Record<string, string>,
    token?: string,
  ): string {
    const systems = [
      "create",
      "rename",
      "appoint",
      "revoke",
      "disable",
      "enable",
      "web_access",
    ];
    this.require(u, network, systems.includes(op));
    const n = network
      ? this.s.one("SELECT * FROM networks WHERE id=?", network)
      : undefined;
    if (op !== "create" && !n) throw new UserError("请选择网络");
    if (
      [
        "remove",
        "disable",
        "approve_recovery",
        "reject_recovery",
        "delivery_close",
      ].includes(op) &&
      data.confirmed !== "yes"
    )
      throw new UserError("请确认操作");
    let result = "操作已完成。",
      target = "";
    if (op === "create") {
      const name = (data.name || "").trim();
      if (!name || name.length > 60)
        throw new UserError("网络名称需要 1–60 字");
      const code = data.code || random(24);
      if (code.length < 16 || code.length > 200)
        throw new UserError("加入码至少 16 字符");
      const id = Number(
        this.s.run(
          "INSERT INTO networks(public_id,name,join_code_hash,join_code_ciphertext,join_code_hint,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
          pid("N"),
          name,
          hmac(this.service.config.secret, code),
          this.service.storage(code),
          code.slice(-4),
          u.id,
          now(),
          now(),
        ).lastInsertRowid,
      );
      network = id;
      if (token)
        this.s.run(
          "UPDATE v2_instances SET admin_network=?,admin_page=0 WHERE token=?",
          id,
          token,
        );
      result =
        "网络已创建。加入码：" + code + "\n加入码是加入凭证，请勿公开传播。";
    } else if (op === "view_code") {
      result =
        "加入码：" +
        this.service.readCode(n!.join_code_ciphertext) +
        "\n请勿公开传播。";
    } else if (op === "rotate") {
      const code = data.code === undefined ? random(24) : data.code.trim();
      if (code.length < 6 || code.length > 200 || /\s/.test(code))
        throw new UserError("加入码需为 6–200 个字符，不能含空格");
      if (this.s.one("SELECT id FROM networks WHERE join_code_hash=? AND id<>?", hmac(this.service.config.secret, code), network))
        throw new UserError("该加入码不可用，请换一个");
      this.s.run(
        "UPDATE networks SET join_code_hash=?,join_code_ciphertext=?,join_code_hint=?,updated_at=? WHERE id=?",
        hmac(this.service.config.secret, code),
        this.service.storage(code),
        code.slice(-4),
        now(),
        network,
      );
      result = "新加入码：" + code + "\n旧加入码已失效。";
    } else if (op === "join_on" || op === "join_off")
      this.s.run(
        "UPDATE networks SET join_enabled=?,updated_at=? WHERE id=?",
        op === "join_on" ? 1 : 0,
        now(),
        network,
      );
    else if (op === "rename") {
      const name = (data.name || "").trim();
      if (!name || name.length > 60) throw new UserError("名称需要 1–60 字");
      this.s.run(
        "UPDATE networks SET name=?,updated_at=? WHERE id=?",
        name,
        now(),
        network,
      );
    } else if (op === "enable" || op === "disable") {
      this.s.run(
        "UPDATE networks SET status=?,updated_at=? WHERE id=?",
        op === "enable" ? "active" : "disabled",
        now(),
        network,
      );
      if (op === "disable") {
        this.s.run(
          "UPDATE contact_requests SET status='invalid',updated_at=? WHERE network_id=? AND status IN ('pending_target','processing')",
          now(),
          network,
        );
        this.service.expire();
      }
    } else if (
      ["remove", "appoint", "revoke", "unban", "web_access"].includes(op)
    ) {
      const m = this.s.one(
        "SELECT * FROM memberships WHERE network_id=? AND member_code=?",
        network,
        data.member_code,
      );
      if (!m) throw new UserError("成员不存在");
      target = m.member_code;
      if (op === "unban") {
        const other = this.service.user(m.user_id);
        this.s.run(
          "DELETE FROM bans WHERE network_id=? AND actor_key=?",
          network,
          other.actor_key,
        );
      } else if (m.status !== "active") throw new UserError("成员已退出");
      else if (op === "remove") {
        if (m.role === "network_admin")
          throw new UserError("请先撤销管理员职责");
        if (this.system(this.service.user(m.user_id)))
          throw new UserError("请先撤销系统管理授权");
        this.service.invalidate(m.id, "removed");
        this.service.notify(m.user_id, "你已被该网络移除。");
        const next = this.s.one(
          "SELECT m.network_id FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.status='active' AND n.status='active' ORDER BY m.id LIMIT 1",
          m.user_id,
        );
        this.s.run(
          "UPDATE users SET current_network_id=? WHERE id=? AND current_network_id=?",
          next?.network_id ?? null,
          m.user_id,
          network,
        );
      } else if (op === "appoint" || op === "revoke")
        this.s.run(
          "UPDATE memberships SET role=?,updated_at=? WHERE id=?",
          op === "appoint" ? "network_admin" : "member",
          now(),
          m.id,
        );
      else if (op === "web_access") {
        if (m.role !== "network_admin")
          throw new UserError("请先任命网络管理员");
        const password = random(32),
          salt = random(16);
        const { pbkdf2Sync } = require("node:crypto");
        const stored =
          salt +
          ":" +
          pbkdf2Sync(password, salt, 200000, 32, "sha256").toString("hex");
        this.s.run(
          "INSERT OR REPLACE INTO web_admin_logins VALUES(?,?)",
          m.user_id,
          stored,
        );
        result = `网页账号：user-${m.user_id}\n密码：${password}`;
      }
    } else if (op === "resolve_report") {
      const r = this.s.one(
        "SELECT id FROM reports WHERE network_id=? AND public_id=?",
        network,
        data.report_id,
      );
      if (!r || !data.resolution?.trim() || data.resolution.length > 300)
        throw new UserError("举报或处理说明无效");
      this.s.run(
        "UPDATE reports SET status='resolved',resolution=?,resolved_at=? WHERE id=?",
        data.resolution.trim(),
        now(),
        r.id,
      );
      target = data.report_id;
    } else if (op === "approve_recovery" || op === "reject_recovery") {
      const r = this.s.one(
        "SELECT ar.*,m.user_id,m.network_id FROM account_recoveries ar JOIN memberships m ON m.id=ar.membership_id WHERE ar.public_id=? AND m.network_id=? AND ar.status='pending' AND ar.expires_at>?",
        data.recovery_id,
        network,
        now(),
      );
      if (!r) throw new UserError("恢复申请已结束");
      if (op === "approve_recovery") {
        const conflict = this.s.one(
          "SELECT * FROM memberships WHERE network_id=? AND user_id=?",
          network,
          r.requester_user_id,
        );
        if (conflict && conflict.id !== r.membership_id)
          throw new UserError(
            "新账号已有本网络资料，请先由新账号退出，再联系管理员核实",
          );
        this.service.invalidate(r.membership_id, "left");
        const newUser = this.service.user(r.requester_user_id);
        this.s.run(
          "UPDATE memberships SET user_id=?,status='active',share_contact=?,share_version=share_version+1,updated_at=? WHERE id=?",
          r.requester_user_id,
          newUser.address,
          now(),
          r.membership_id,
        );
        this.s.run(
          "UPDATE users SET current_network_id=? WHERE id=?",
          network,
          r.requester_user_id,
        );
        const replacement = this.s.one(
          "SELECT m.network_id FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.status='active' AND n.status='active' ORDER BY m.id LIMIT 1",
          r.user_id,
        );
        this.s.run(
          "UPDATE users SET current_network_id=? WHERE id=? AND current_network_id=?",
          replacement?.network_id ?? null,
          r.user_id,
          network,
        );
      }
      this.s.run(
        "UPDATE account_recoveries SET status=?,resolved_by=?,resolved_at=? WHERE id=?",
        op === "approve_recovery" ? "approved" : "rejected",
        u.id,
        now(),
        r.id,
      );
      this.service.notify(
        r.requester_user_id,
        op === "approve_recovery"
          ? "账号恢复已批准，请查看你的资料。"
          : "恢复申请未获批准。",
      );
      target = data.recovery_id;
    } else if (op === "delivery_close") {
      const job = this.s.one(
        "SELECT j.* FROM v2_jobs j JOIN contact_requests r ON r.id=j.request_id WHERE j.id=? AND r.network_id=? AND j.status='uncertain'",
        Number(data.outbox_id),
        network,
      );
      if (!job) throw new UserError("交付记录不存在");
      this.s.run("UPDATE v2_jobs SET status='closed' WHERE id=?", job.id);
      this.s.run(
        "UPDATE contact_requests SET status='invalid',updated_at=? WHERE id=?",
        now(),
        job.request_id,
      );
    } else throw new UserError("不支持此管理操作");
    this.service.audit(u, network, op, target);
    return result;
  }
  snapshot(u: Row, token: string) {
    const system = this.system(u),
      networks = this.networks(u, system),
      ctx = this.s.one("SELECT * FROM v2_instances WHERE token=?", token)!;
    if (!system && !networks.length) {
      const c = this.s.one(
        "SELECT * FROM xdc_admin_challenges WHERE token=? AND expires_at>?",
        token,
        now(),
      );
      return {
        unlocked: false,
        challenge: c
          ? {
              nonce: c.nonce,
              salt: this.verifier.salt,
              iterations: this.verifier.iterations,
            }
          : null,
      };
    }
    const current =
      networks.find((n) => n.id === ctx.admin_network)?.id ??
      networks[0]?.id ??
      null;
    if (current !== ctx.admin_network)
      this.s.run(
        "UPDATE v2_instances SET admin_network=?,admin_page=0 WHERE token=?",
        current,
        token,
      );
    const total = Number(
        networks.find((n) => n.id === current)?.member_count || 0,
      ),
      page = Math.min(ctx.admin_page, Math.max(0, Math.ceil(total / 20) - 1));
    const members = current
      ? this.s.all(
          "SELECT member_code,nickname,role,status,discoverable,user_id FROM memberships WHERE network_id=? AND status='active' ORDER BY nickname,member_code LIMIT 21 OFFSET ?",
          current,
          page * 20,
        )
      : [];
    const data: Row = {
      unlocked: true,
      system,
      networks,
      current,
      page,
      members: members.slice(0, 20).map((m) => ({
        member_code: m.member_code,
        nickname: m.nickname,
        role: m.role,
        status: m.status,
        discoverable: m.discoverable,
        is_self: m.user_id === u.id,
        is_system_admin: 0,
        key_admin: this.system(this.service.user(m.user_id)),
        banned: false,
      })),
      has_more: members.length > 20,
      bans: [],
      reports: [],
      recoveries: [],
      audit: [],
      deliveries: [],
    };
    if (current) {
      const network = this.s.one("SELECT join_code_ciphertext FROM networks WHERE id=?", current);
      data.join_code = network?.join_code_ciphertext ? this.service.readCode(network.join_code_ciphertext) : "";
      data.reports = this.s.all(
        "SELECT r.public_id,r.reason,r.status,r.resolution,a.nickname reporter,b.nickname target FROM reports r JOIN memberships a ON a.id=r.reporter_membership_id JOIN memberships b ON b.id=r.target_membership_id WHERE r.network_id=? ORDER BY r.id DESC LIMIT 30",
        current,
      );
      data.recoveries = this.s.all(
        "SELECT ar.public_id,m.nickname,m.member_code FROM account_recoveries ar JOIN memberships m ON m.id=ar.membership_id WHERE m.network_id=? AND ar.status='pending' AND ar.expires_at>?",
        current,
        now(),
      );
      data.audit = this.s.all(
        "SELECT action,target_ref,details,created_at FROM audit_log WHERE network_id=? ORDER BY id DESC LIMIT 30",
        current,
      );
      data.deliveries = this.s.all(
        "SELECT j.id,r.public_id,m.nickname recipient,j.started_at delivery_started_at FROM v2_jobs j JOIN contact_requests r ON r.id=j.request_id LEFT JOIN memberships m ON m.user_id=j.recipient AND m.network_id=r.network_id WHERE r.network_id=? AND j.status='uncertain'",
        current,
      );
    }
    return data;
  }
}
