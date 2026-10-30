import {
  existsSync,
  readFileSync,
  openSync,
  writeSync,
  fsyncSync,
  closeSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { Store, type Row } from "./db";
import type { Config } from "./config";
import { random, hmac, hash, encrypt, decrypt } from "./crypto";
import { UserError, now, later, type Command } from "./protocol";
export type Identity = {
  accountId: number;
  chatId: number;
  contactId: number;
  address: string;
  fingerprint: string;
};
export const pid = (prefix: string) =>
  prefix + random(9).replace(/[-_]/g, "").slice(0, 12).toUpperCase();
export class Service {
  constructor(
    readonly s: Store,
    readonly config: Config,
  ) {
    this.restoreDeletions();
  }
  restoreDeletions() {
    if (this.s.path === ":memory:") return;
    const path = join(dirname(this.s.path), "deletions.jsonl");
    if (!existsSync(path)) return;
    const latest = new Map<string, string>();
    for (const line of readFileSync(path, "utf8").split("\n").filter(Boolean)) {
      try {
        const row = JSON.parse(line);
        if (
          /^[a-f0-9]{64}$/.test(row.actor) &&
          typeof row.at === "string" &&
          row.at > (latest.get(row.actor) || "")
        )
          latest.set(row.actor, row.at);
      } catch {
        console.warn("Incomplete deletion ledger entry requires review");
      }
    }
    this.s.tx(() => {
      for (const [actor, at] of latest) {
        const u = this.s.one(
          "SELECT u.*,v.deleted_at FROM users u JOIN v2_users v ON v.user_id=u.id WHERE u.actor_key=?",
          actor,
        );
        if (u && !u.deleted_at && u.updated_at <= at) this.erase(u, false);
      }
    });
  }

  user(id: number) {
    const u = this.s.one(
      "SELECT u.*,v.address,v.fingerprint,v.deleted_at FROM users u JOIN v2_users v ON v.user_id=u.id WHERE u.id=? AND u.merged_into IS NULL",
      id,
    );
    if (!u) throw new UserError("账号不存在");
    return u;
  }
  identify(identity: Identity): Row {
    const address = identity.address.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+$/.test(address)) throw new UserError("无法验证账号");
    const actor = hmac(this.config.secret, address),
      t = now();
    return this.s.tx(() => {
      let u = this.s.one(
        "SELECT u.* FROM users u LEFT JOIN v2_aliases a ON a.user_id=u.id WHERE u.merged_into IS NULL AND (u.actor_key=? OR a.actor_key=?)",
        actor,
        actor,
      );
      const byFingerprint = identity.fingerprint
        ? this.s.one(
            "SELECT u.* FROM users u JOIN v2_users v ON v.user_id=u.id WHERE u.merged_into IS NULL AND v.fingerprint=? AND u.core_account_id=?",
            identity.fingerprint,
            identity.accountId,
          )
        : undefined;
      if (u) {
        const bound = this.s.one(
          "SELECT fingerprint FROM v2_users WHERE user_id=?",
          u.id,
        );
        if (bound?.fingerprint && bound.fingerprint !== identity.fingerprint)
          throw new UserError("账号加密身份已变化，请先联系管理员恢复资料");
      }
      if (!u && byFingerprint) u = byFingerprint;
      if (!u) {
        const r = this.s.run(
          "INSERT INTO users(actor_key,contact_id,chat_id,core_account_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
          actor,
          identity.contactId,
          identity.chatId,
          identity.accountId,
          t,
          t,
        );
        u = { id: Number(r.lastInsertRowid) };
        this.s.run(
          "INSERT INTO v2_users(user_id,address,fingerprint) VALUES(?,?,?)",
          u.id,
          address,
          identity.fingerprint,
        );
      }
      if (byFingerprint && byFingerprint.id !== u.id)
        this.merge(u.id, byFingerprint.id);
      const oldAddress = this.s.one(
        "SELECT address FROM v2_users WHERE user_id=?",
        u.id,
      )?.address;
      if (oldAddress && oldAddress !== address) {
        for (const m of this.s.all(
          "SELECT id FROM memberships WHERE user_id=?",
          u.id,
        )) {
          this.s.run(
            "UPDATE contact_requests SET status='invalid',updated_at=? WHERE status IN ('pending_target','processing') AND (requester_membership_id=? OR target_membership_id=?)",
            t,
            m.id,
            m.id,
          );
        }
        this.s.run(
          "UPDATE memberships SET share_contact=NULL,share_version=share_version+1 WHERE user_id=?",
          u.id,
        );
      }
      this.s.run("INSERT OR REPLACE INTO v2_aliases VALUES(?,?)", actor, u.id);
      this.s.run(
        "UPDATE users SET contact_id=?,chat_id=?,core_account_id=?,updated_at=? WHERE id=?",
        identity.contactId,
        identity.chatId,
        identity.accountId,
        t,
        u.id,
      );
      this.s.run(
        "UPDATE v2_users SET address=?,fingerprint=CASE WHEN ?<>'' THEN ? ELSE fingerprint END WHERE user_id=?",
        address,
        identity.fingerprint,
        identity.fingerprint,
        u.id,
      );
      return this.user(u.id);
    });
  }
  merge(keeper: number, old: number) {
    for (const m of this.s.all(
      "SELECT * FROM memberships WHERE user_id=?",
      old,
    )) {
      const existing = this.s.one(
        "SELECT * FROM memberships WHERE user_id=? AND network_id=?",
        keeper,
        m.network_id,
      );
      if (existing) {
        this.invalidate(m.id, "removed");
        this.s.run(
          "UPDATE memberships SET role=CASE WHEN ?='network_admin' THEN ? ELSE role END WHERE id=?",
          m.role,
          m.role,
          existing.id,
        );
      } else
        this.s.run("UPDATE memberships SET user_id=? WHERE id=?", keeper, m.id);
    }
    this.s.run("UPDATE v2_aliases SET user_id=? WHERE user_id=?", keeper, old);
    this.s.run(
      "UPDATE v2_instances SET user_id=? WHERE user_id=?",
      keeper,
      old,
    );
    this.s.run(
      "UPDATE users SET merged_into=?,current_network_id=NULL,contact_id=-id,chat_id=-id WHERE id=?",
      keeper,
      old,
    );
    this.s.run(
      "DELETE FROM pending_actions WHERE user_id IN (?,?)",
      keeper,
      old,
    );
    this.s.run("DELETE FROM xdc_admin_grants WHERE user_id=?", old);
  }
  rate(actor: string, category: string, limit: number, minutes: number) {
    const cutoff = new Date(Date.now() - minutes * 60000).toISOString();
    if (
      Number(
        this.s.one(
          "SELECT count(*) n FROM rate_events WHERE actor_key=? AND category=? AND created_at>?",
          actor,
          category,
          cutoff,
        )?.n,
      ) >= limit
    )
      throw new UserError("操作次数过多，请稍后再试");
    this.s.run("INSERT INTO rate_events VALUES(?,?,?)", actor, category, now());
  }
  member(user: Row, network = user.current_network_id, allowDisabled = false) {
    const m = this.s.one(
      "SELECT m.*,n.status network_status,n.name network_name,n.public_id network_public_id FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.network_id=? AND m.status='active'",
      user.id,
      network,
    );
    if (!m || (!allowDisabled && m.network_status !== "active"))
      throw new UserError("请先加入有效网络");
    return m;
  }
  target(me: Row, code: string, visible = false) {
    const m = this.s.one(
      "SELECT * FROM memberships WHERE network_id=? AND member_code=? AND status='active'",
      me.network_id,
      code.toUpperCase(),
    );
    if (!m || m.id === me.id || (visible && !m.discoverable))
      throw new UserError("该成员当前不可用");
    return m;
  }
  blocked(a: Row, b: Row) {
    return !!this.s.one(
      "SELECT 1 FROM blocks WHERE network_id=? AND ((blocker_membership_id=? AND blocked_membership_id=?) OR (blocker_membership_id=? AND blocked_membership_id=?))",
      a.network_id,
      a.id,
      b.id,
      b.id,
      a.id,
    );
  }
  pending(user: Row, action: string, payload: Row) {
    this.s.run("DELETE FROM pending_actions WHERE user_id=?", user.id);
    this.s.run(
      "INSERT INTO pending_actions VALUES(?,?,?,?,?,?)",
      random(16),
      user.id,
      action,
      JSON.stringify(payload),
      later(1 / 48),
      now(),
    );
  }
  notify(userId: number, body: string) {
    this.s.run(
      "INSERT INTO v2_notifications VALUES(?,?,?,?)",
      random(16),
      userId,
      body,
      now(),
    );
  }
  audit(
    user: Row,
    network: number | null,
    action: string,
    target = "",
    details = "",
  ) {
    this.s.run(
      "INSERT INTO audit_log(network_id,actor_user_id,action,target_ref,details,created_at) VALUES(?,?,?,?,?,?)",
      network,
      user.id,
      action,
      target,
      details.slice(0, 300),
      now(),
    );
  }
  invalidate(memberId: number, status: string) {
    this.s.run(
      "UPDATE memberships SET status=?,updated_at=? WHERE id=?",
      status,
      now(),
      memberId,
    );
    this.s.run(
      "UPDATE contact_requests SET status='invalid',updated_at=? WHERE status IN ('pending_target','processing') AND (requester_membership_id=? OR target_membership_id=?)",
      now(),
      memberId,
      memberId,
    );
    this.s.run(
      "UPDATE v2_jobs SET status='cancelled' WHERE status='pending' AND request_id IN (SELECT id FROM contact_requests WHERE status='invalid')",
    );
  }
  expire() {
    const t = now();
    this.s.run(
      "UPDATE contact_requests SET status='expired',updated_at=? WHERE expires_at<=? AND status IN ('pending_target','processing')",
      t,
      t,
    );
    this.s.run(
      "UPDATE v2_jobs SET status='cancelled' WHERE status='pending' AND request_id IN (SELECT id FROM contact_requests WHERE status IN ('invalid','expired','withdrawn','rejected'))",
    );
    this.s.run("DELETE FROM pending_actions WHERE expires_at<?", t);
    this.s.run(
      "UPDATE account_recoveries SET status='expired' WHERE expires_at<? AND status='pending'",
      t,
    );
    this.s.run(
      "DELETE FROM rate_events WHERE created_at<?",
      new Date(Date.now() - 86400000).toISOString(),
    );
    this.s.run(
      "UPDATE contact_requests SET reason='',requester_contact='',target_contact=NULL WHERE status NOT IN ('pending_target','processing') AND updated_at<?",
      new Date(Date.now() - 30 * 86400000).toISOString(),
    );
    this.s.run(
      "DELETE FROM v2_notifications WHERE created_at<?",
      new Date(Date.now() - 30 * 86400000).toISOString(),
    );
    this.s.run(
      "DELETE FROM reports WHERE created_at<?",
      new Date(Date.now() - 90 * 86400000).toISOString(),
    );
  }
  join(user: Row, c: Command) {
    if (
      c.consent !== true ||
      c.use_current_account !== true ||
      typeof c.discoverable !== "boolean" ||
      typeof c.accepts_requests !== "boolean"
    )
      throw new UserError("请确认隐私说明和资料设置");
    const nickname = (c.nickname || "").trim();
    if (!nickname || nickname.length > 40)
      throw new UserError("昵称需要 1–40 字");
    const n = this.s.one(
      "SELECT * FROM networks WHERE join_code_hash=?",
      hmac(this.config.secret, (c.value || "").trim()),
    );
    if (!n)
      throw new UserError(
        "加入码不正确。请填写管理员提供的完整加入码，网络名称不能用于加入。",
      );
    if (n.status !== "active" || !n.join_enabled)
      throw new UserError("该网络目前已停止加入");
    if (
      this.s.one(
        "SELECT 1 FROM bans WHERE network_id=? AND actor_key=?",
        n.id,
        user.actor_key,
      )
    )
      throw new UserError("该账号不能加入此网络");
    const previous = this.s.one(
      "SELECT * FROM memberships WHERE network_id=? AND user_id=?",
      n.id,
      user.id,
    );
    if (!previous || previous.status !== "active") {
      if (previous)
        this.s.run(
          "UPDATE memberships SET nickname=?,bio='',discoverable=?,accepts_requests=?,share_contact=?,share_version=share_version+1,status='active',role='member',updated_at=? WHERE id=?",
          nickname,
          Number(c.discoverable),
          Number(c.accepts_requests),
          user.address,
          now(),
          previous.id,
        );
      else
        this.s.run(
          "INSERT INTO memberships(network_id,user_id,member_code,nickname,discoverable,accepts_requests,share_contact,share_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
          n.id,
          user.id,
          pid("M"),
          nickname,
          Number(c.discoverable),
          Number(c.accepts_requests),
          user.address,
          now(),
          now(),
        );
      const m = this.s.one(
        "SELECT id FROM memberships WHERE network_id=? AND user_id=?",
        n.id,
        user.id,
      )!;
      this.s.run("INSERT OR IGNORE INTO xdc_auto_share VALUES(?)", m.id);
    }
    this.s.run(
      "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
      n.id,
      now(),
      user.id,
    );
    this.s.run("UPDATE v2_instances SET page=0 WHERE user_id=?", user.id);
    this.s.run("UPDATE v2_users SET deleted_at=NULL WHERE user_id=?", user.id);
    this.s.run("DELETE FROM pending_actions WHERE user_id=?", user.id);
    return "已加入网络。";
  }
  queueExchange(user: Row, target: Row, reason: string, forcePending = false) {
    const me = this.member(user);
    if (this.blocked(me, target) || !target.accepts_requests)
      throw new UserError("暂时无法联系该成员");
    if (
      !me.share_contact ||
      me.share_contact.toLowerCase() !== user.address.toLowerCase()
    )
      throw new UserError("请先使用当前账号设置联系方式");
    const other = this.user(target.user_id);
    if (
      !target.share_contact ||
      target.share_contact.toLowerCase() !== other.address.toLowerCase()
    )
      throw new UserError("对方尚未设置可用名片");
    if (
      this.s.one(
        "SELECT 1 FROM contact_requests WHERE network_id=? AND status IN ('pending_target','processing') AND min(requester_membership_id,target_membership_id)=? AND max(requester_membership_id,target_membership_id)=?",
        me.network_id,
        Math.min(me.id, target.id),
        Math.max(me.id, target.id),
      )
    )
      throw new UserError("该好友已有未完成的交换");
    if (
      this.s.one(
        "SELECT 1 FROM contact_requests WHERE network_id=? AND status='rejected' AND updated_at>? AND min(requester_membership_id,target_membership_id)=? AND max(requester_membership_id,target_membership_id)=?",
        me.network_id,
        new Date(Date.now() - 86400000).toISOString(),
        Math.min(me.id, target.id),
        Math.max(me.id, target.id),
      )
    )
      throw new UserError("请稍后再联系该成员");
    const count = Number(
      this.s.one(
        "SELECT count(*) n FROM contact_requests WHERE requester_membership_id=? AND created_at>?",
        me.id,
        new Date(Date.now() - 86400000).toISOString(),
      )?.n,
    );
    if (count >= 30) throw new UserError("今日联系次数已达上限");
    if (
      Number(
        this.s.one(
          "SELECT count(*) n FROM contact_requests WHERE requester_membership_id=? AND status IN ('pending_target','processing')",
          me.id,
        )?.n,
      ) >= 10
    )
      throw new UserError("待处理交换过多");
    const auto =
      !forcePending &&
      this.s.one("SELECT 1 FROM xdc_auto_share WHERE membership_id=?", me.id) &&
      this.s.one(
        "SELECT 1 FROM xdc_auto_share WHERE membership_id=?",
        target.id,
      );
    const t = now(),
      id = Number(
        this.s.run(
          `INSERT INTO contact_requests(public_id,network_id,requester_membership_id,target_membership_id,reason,requester_contact,requester_share_version,target_contact,target_share_version,request_kind,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'vcard',?,?,?,?)`,
          pid("R"),
          me.network_id,
          me.id,
          target.id,
          reason.slice(0, 300),
          me.share_contact,
          me.share_version,
          auto ? target.share_contact : null,
          auto ? target.share_version : null,
          auto ? "processing" : "pending_target",
          later(7),
          t,
          t,
        ).lastInsertRowid,
      );
    if (auto) this.jobs(id, user.id, target.user_id);
    this.notify(
      target.user_id,
      auto
        ? me.nickname + " 正在与你交换名片。"
        : me.nickname + " 希望与你交换名片，请在申请页查看。",
    );
    return auto ? "正在交换名片。" : "已发送联系申请。";
  }
  jobs(id: number, a: number, b: number) {
    this.s.run(
      "INSERT OR IGNORE INTO v2_jobs(request_id,recipient,source) VALUES(?,?,?)",
      id,
      a,
      b,
    );
    this.s.run(
      "INSERT OR IGNORE INTO v2_jobs(request_id,recipient,source) VALUES(?,?,?)",
      id,
      b,
      a,
    );
  }
  command(user: Row, name: string, value: string) {
    if (name === "取消") {
      this.s.run("DELETE FROM pending_actions WHERE user_id=?", user.id);
      return "已取消。";
    }
    if (name === "切换网络") {
      const n = this.s.one(
        "SELECT n.id FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND n.public_id=? AND m.status='active' AND n.status='active'",
        user.id,
        value,
      );
      if (!n) throw new UserError("你尚未加入该网络");
      this.s.run(
        "UPDATE users SET current_network_id=? WHERE id=?",
        n.id,
        user.id,
      );
      this.s.run("DELETE FROM pending_actions WHERE user_id=?", user.id);
      return "已切换网络。";
    }
    if (name === "恢复账号") {
      this.pending(user, "guide_recover_identity", {});
      return "";
    }
    if (name === "删除我的数据") {
      if (
        this.s.one(
          "SELECT 1 FROM memberships WHERE user_id=? AND status='active' AND role='network_admin'",
          user.id,
        )
      )
        throw new UserError("请先交接网络管理员职责");
      this.pending(user, "delete", {});
      return "";
    }
    const me = this.member(user, user.current_network_id, name === "退出网络");
    if (name === "退出网络") {
      if (
        me.role === "network_admin" &&
        Number(
          this.s.one(
            "SELECT count(*) n FROM memberships WHERE network_id=? AND status='active' AND role='network_admin'",
            me.network_id,
          )?.n,
        ) <= 1
      )
        throw new UserError("请先交接网络管理员职责");
      this.pending(user, "leave", {
        membership_id: me.id,
        network_id: me.network_id,
      });
      return "";
    }
    if (name === "编辑昵称" || name === "编辑介绍") {
      const max = name === "编辑昵称" ? 40 : 300;
      if ((name === "编辑昵称" && !value.trim()) || value.length > max)
        throw new UserError("资料长度不正确");
      this.s.run(
        `UPDATE memberships SET ${name === "编辑昵称" ? "nickname" : "bio"}=?,updated_at=? WHERE id=?`,
        value.trim(),
        now(),
        me.id,
      );
      return "已保存。";
    }
    if (name === "设置联系方式") {
      let address = value.trim();
      if (address.includes("://")) {
        try {
          address =
            new URLSearchParams(
              new URL(address).hash.slice(1).split("&").slice(1).join("&"),
            ).get("a") || "";
        } catch {
          address = "";
        }
      }
      if (address.toLowerCase() !== user.address.toLowerCase())
        throw new UserError("请使用当前私聊账号的名片链接或账号");
      this.s.run(
        "UPDATE memberships SET share_contact=?,share_version=share_version+1 WHERE id=?",
        user.address,
        me.id,
      );
      return "联系方式已保存。";
    }
    const flags: Record<string, [string, number]> = {
      公开资料: ["discoverable", 1],
      隐藏资料: ["discoverable", 0],
      接收申请: ["accepts_requests", 1],
      暂停申请: ["accepts_requests", 0],
    };
    if (flags[name]) {
      const [field, v] = flags[name];
      this.s.run(`UPDATE memberships SET ${field}=? WHERE id=?`, v, me.id);
      return "隐私设置已保存。";
    }
    if (name === "屏蔽" || name === "解除屏蔽") {
      const target = this.target(me, value);
      if (name === "屏蔽") {
        this.s.run(
          "INSERT OR IGNORE INTO blocks VALUES(?,?,?,?)",
          me.network_id,
          me.id,
          target.id,
          now(),
        );
        this.s.run(
          "UPDATE contact_requests SET status='invalid',updated_at=? WHERE network_id=? AND min(requester_membership_id,target_membership_id)=? AND max(requester_membership_id,target_membership_id)=? AND status IN ('pending_target','processing')",
          now(),
          me.network_id,
          Math.min(me.id, target.id),
          Math.max(me.id, target.id),
        );
        this.expire();
      } else
        this.s.run(
          "DELETE FROM blocks WHERE network_id=? AND blocker_membership_id=? AND blocked_membership_id=?",
          me.network_id,
          me.id,
          target.id,
        );
      return "已更新。";
    }
    if (name === "举报") {
      const [code, ...rest] = value.split(/\s+/),
        reason = rest.join(" ").trim(),
        target = this.target(me, code);
      if (!reason || reason.length > 500)
        throw new UserError("请填写 1–500 字举报原因");
      this.rate(user.actor_key, "report", 10, 60);
      this.s.run(
        "INSERT INTO reports(public_id,network_id,reporter_membership_id,target_membership_id,reason,created_at) VALUES(?,?,?,?,?,?)",
        pid("P"),
        me.network_id,
        me.id,
        target.id,
        reason,
        now(),
      );
      return "举报已提交。";
    }
    if (name === "申请") {
      const [code, ...rest] = value.split(/\s+/),
        target = this.target(me, code, true),
        reason = rest.join(" ").trim();
      if (!reason || reason.length > 300)
        throw new UserError("请填写 1–300 字联系理由");
      this.pending(user, "send_request", {
        contact: me.share_contact,
        target_id: target.id,
        reason,
        recipient: {
          nickname: target.nickname,
          member_code: target.member_code,
        },
      });
      return "";
    }
    if (["同意申请", "拒绝申请", "撤销申请"].includes(name)) {
      const r = this.s.one(
        "SELECT * FROM contact_requests WHERE public_id=? AND network_id=?",
        value,
        me.network_id,
      );
      if (!r || r.status !== "pending_target" || r.expires_at <= now())
        throw new UserError("申请已结束");
      const outgoing = r.requester_membership_id === me.id;
      if (
        (name === "撤销申请" && !outgoing) ||
        (name !== "撤销申请" && r.target_membership_id !== me.id)
      )
        throw new UserError("没有操作权限");
      if (name === "同意申请") {
        const other = this.s.one(
          "SELECT * FROM memberships WHERE id=? AND status='active'",
          r.requester_membership_id,
        );
        if (!other || this.blocked(me, other))
          throw new UserError("申请已结束");
        this.pending(user, "accept_request", {
          request_id: r.id,
          contact: me.share_contact,
          recipient: {
            nickname: other.nickname,
            member_code: other.member_code,
          },
        });
        return "";
      }
      this.s.run(
        "UPDATE contact_requests SET status=?,updated_at=? WHERE id=?",
        name === "撤销申请" ? "withdrawn" : "rejected",
        now(),
        r.id,
      );
      return "已处理。";
    }
    if (["我的资料", "我的网络", "我的申请", "屏蔽列表", "查找"].includes(name))
      return "";
    throw new UserError("不支持此操作");
  }
  reply(user: Row, c: Command) {
    if (!c.step) throw new UserError("此操作已结束");
    const p = this.s.one(
      "SELECT * FROM pending_actions WHERE user_id=? AND token=? AND expires_at>?",
      user.id,
      c.step,
      now(),
    );
    if (!p) throw new UserError("此操作已结束");
    const data = JSON.parse(p.payload);
    if (c.value !== "1") {
      this.s.run("DELETE FROM pending_actions WHERE token=?", p.token);
      return "已取消。";
    }
    if (p.action === "leave") {
      const m = this.member(user, data.network_id, true);
      if (m.id !== data.membership_id) throw new UserError("成员状态已变化");
      if (
        m.role === "network_admin" &&
        Number(
          this.s.one(
            "SELECT count(*) n FROM memberships WHERE network_id=? AND role='network_admin' AND status='active'",
            m.network_id,
          )?.n,
        ) <= 1
      )
        throw new UserError("请先交接管理员职责");
      this.invalidate(m.id, "left");
      const other = this.s.one(
        "SELECT m.network_id FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.status='active' AND n.status='active' ORDER BY m.id LIMIT 1",
        user.id,
      );
      this.s.run(
        "UPDATE users SET current_network_id=? WHERE id=?",
        other?.network_id ?? null,
        user.id,
      );
    } else if (p.action === "delete") {
      if (
        this.s.one(
          "SELECT 1 FROM memberships WHERE user_id=? AND role='network_admin' AND status='active'",
          user.id,
        )
      )
        throw new UserError("请先交接管理员职责");
      this.erase(user);
    } else if (p.action === "send_request") {
      const m = this.member(user),
        target = this.s.one(
          "SELECT * FROM memberships WHERE id=? AND network_id=? AND status='active' AND discoverable=1",
          data.target_id,
          m.network_id,
        );
      if (!target || m.share_contact !== data.contact)
        throw new UserError("资料已变化");
      this.queueExchange(user, target, data.reason, true);
    } else if (p.action === "accept_request") {
      const m = this.member(user),
        r = this.s.one(
          "SELECT * FROM contact_requests WHERE id=? AND target_membership_id=? AND network_id=? AND status='pending_target' AND expires_at>?",
          data.request_id,
          m.id,
          m.network_id,
          now(),
        ),
        a = r
          ? this.s.one(
              "SELECT * FROM memberships WHERE id=? AND status='active'",
              r.requester_membership_id,
            )
          : undefined;
      if (
        !r ||
        !a ||
        this.blocked(m, a) ||
        !m.accepts_requests ||
        m.share_contact !== data.contact ||
        a.share_version !== r.requester_share_version
      )
        throw new UserError("申请或资料已变化");
      this.s.run(
        "UPDATE contact_requests SET status='processing',target_contact=?,target_share_version=?,updated_at=? WHERE id=?",
        m.share_contact,
        m.share_version,
        now(),
        r.id,
      );
      this.jobs(r.id, a.user_id, user.id);
    } else throw new UserError("此操作不可用");
    this.s.run("DELETE FROM pending_actions WHERE token=?", p.token);
    return "操作已完成。";
  }
  erase(user: Row, record = true) {
    if (record && this.s.path !== ":memory:") {
      const fd = openSync(
        join(dirname(this.s.path), "deletions.jsonl"),
        "a",
        0o600,
      );
      try {
        writeSync(
          fd,
          JSON.stringify({ actor: user.actor_key, at: now() }) + "\n",
        );
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
    }
    for (const m of this.s.all(
      "SELECT * FROM memberships WHERE user_id=?",
      user.id,
    )) {
      this.invalidate(m.id, "left");
      this.s.run(
        "UPDATE memberships SET nickname='已删除成员',bio='',share_contact=NULL,discoverable=0,accepts_requests=0,role='member' WHERE id=?",
        m.id,
      );
      this.s.run("DELETE FROM xdc_auto_share WHERE membership_id=?", m.id);
      this.s.run(
        "DELETE FROM blocks WHERE blocker_membership_id=? OR blocked_membership_id=?",
        m.id,
        m.id,
      );
      this.s.run(
        "UPDATE contact_requests SET reason='',requester_contact='',target_contact=NULL WHERE requester_membership_id=? OR target_membership_id=?",
        m.id,
        m.id,
      );
    }
    this.s.run("DELETE FROM v2_notifications WHERE user_id=?", user.id);
    this.s.run("DELETE FROM xdc_admin_grants WHERE user_id=?", user.id);
    this.s.run("DELETE FROM web_admin_logins WHERE user_id=?", user.id);
    this.s.run("DELETE FROM outbox WHERE recipient_user_id=?", user.id);
    this.s.run(
      "DELETE FROM v2_results WHERE token IN (SELECT token FROM v2_instances WHERE user_id=?)",
      user.id,
    );
    this.s.run(
      "UPDATE v2_instances SET snapshot=NULL WHERE user_id=?",
      user.id,
    );
    this.s.run("UPDATE users SET current_network_id=NULL WHERE id=?", user.id);
    this.s.run(
      "UPDATE v2_users SET deleted_at=? WHERE user_id=?",
      now(),
      user.id,
    );
    this.s.run(
      "INSERT OR REPLACE INTO v2_tombstones VALUES(?,?)",
      user.actor_key,
      now(),
    );
  }
  recover(user: Row, c: Command) {
    const n = this.s.one(
      "SELECT * FROM networks WHERE join_code_hash=? AND status='active'",
      hmac(this.config.secret, (c.join_code || "").trim()),
    );
    const old = hmac(
      this.config.secret,
      (c.old_address || "").trim().toLowerCase(),
    );
    const m = n
      ? this.s.one(
          "SELECT m.* FROM memberships m JOIN users u ON u.id=m.user_id LEFT JOIN v2_aliases a ON a.user_id=u.id WHERE m.network_id=? AND m.nickname=? AND m.status='active' AND (u.actor_key=? OR a.actor_key=?)",
          n.id,
          c.nickname?.trim(),
          old,
          old,
        )
      : undefined;
    if (!m) throw new UserError("未找到匹配成员，请检查信息或联系管理员");
    if (m.user_id === user.id) throw new UserError("当前账号已绑定该资料");
    if (
      this.s.one(
        "SELECT 1 FROM account_recoveries WHERE membership_id=? AND status='pending'",
        m.id,
      )
    )
      throw new UserError("恢复申请已提交");
    this.s.run(
      "INSERT INTO account_recoveries(public_id,membership_id,requester_user_id,expires_at,created_at) VALUES(?,?,?,?,?)",
      pid("A"),
      m.id,
      user.id,
      later(7),
      now(),
    );
    this.s.run(
      "DELETE FROM pending_actions WHERE user_id=? AND action='guide_recover_identity'",
      user.id,
    );
    return "恢复申请已提交，请联系管理员核实。";
  }
  storage(code: string) {
    return JSON.stringify(encrypt(code, this.config.secret, "storage-v1"));
  }
  readCode(ciphertext: string) {
    return decrypt(
      JSON.parse(ciphertext),
      this.config.secret,
      "storage-v1",
    ) as string;
  }
}
