import { Command, UserError, now, type Command as Request } from "./protocol";
import { Service } from "./service";
import { Admin } from "./xdc_admin";
import { type Row } from "./db";
import { decrypt, encrypt, hmac, hash, equal } from "./crypto";
export interface Publisher {
  isLive?(token: string): boolean;
  status(
    account: number,
    message: number,
    payload: unknown,
    notify?: boolean,
  ): Promise<void>;
  realtime(account: number, message: number, payload: unknown): Promise<void>;
}
export class Bridge {
  readonly admin: Admin;
  private fullSnapshots = new Set<string>();
  constructor(
    readonly service: Service,
    readonly publisher: Publisher,
  ) {
    this.admin = new Admin(service);
  }
  get s() {
    return this.service.s;
  }
  instance(account: number, message: number) {
    return this.s.one(
      "SELECT x.*,v.user_id,v.page,v.admin_network,v.admin_page,v.revision,v.snapshot FROM xdc_instances x JOIN v2_instances v ON v.token=x.token WHERE x.account_id=? AND x.message_id=?",
      account,
      message,
    );
  }
  handle(instance: Row, payload: unknown, serial?: number): boolean {
    let c: Request;
    try {
      c = Command.parse(decrypt(payload, instance.token));
      if (c.token !== instance.token) return false;
    } catch {
      return false;
    }
    if (c.action === "refresh") this.fullSnapshots.add(instance.token);
    this.s.tx(() => {
      const digest = hmac(this.service.config.secret, JSON.stringify(c));
      const prior = this.s.one(
        "SELECT * FROM v2_results WHERE token=? AND request_id=?",
        instance.token,
        c.id,
      );
      if (prior) {
        if (prior.digest !== digest) throw new UserError("请求编号冲突");
        if (serial !== undefined) this.cursor(instance, serial);
        return;
      }
      const user = this.service.user(instance.user_id);
      let error: string | null = null,
        message = "",
        ok = 1;
      try {
        this.service.rate(user.actor_key, "command", 120, 1);
        if (c.action === "join_network")
          this.service.rate(user.actor_key, "join", 10, 60);
        if (c.action === "recover_by_name")
          this.service.rate(user.actor_key, "recovery", 10, 60);
        if (c.action === "admin_unlock")
          this.service.rate(user.actor_key, "admin", 5, 15);
      } catch (e) {
        if (e instanceof UserError) error = e.message;
        else throw e;
      }
      const challenge =
        c.action === "admin_unlock"
          ? this.s.one(
              "SELECT * FROM xdc_admin_challenges WHERE token=?",
              instance.token,
            )
          : undefined;
      if (c.action === "admin_unlock")
        this.admin.challenge(user, instance.token);
      this.s.db.exec("SAVEPOINT action");
      try {
        if (error) throw new UserError(error);
        this.service.expire();
        if (
          !["refresh", "join_network", "cancel_join", "start"].includes(
            c.action,
          ) &&
          c.network !== user.current_network_id
        )
          throw new UserError("当前网络已变化，请查看最新状态后操作");
        const value = c.value || "";
        switch (c.action) {
          case "start":
          case "refresh":
            break;
          case "join_network":
            message = this.service.join(user, c);
            break;
          case "cancel_join": {
            const p = this.s.one(
              "SELECT * FROM pending_actions WHERE user_id=? AND token=?",
              user.id,
              c.step,
            );
            if (
              p &&
              ["join", "guide_join_code", "guide_nickname"].includes(p.action)
            )
              this.s.run("DELETE FROM pending_actions WHERE token=?", p.token);
            break;
          }
          case "use_current_account":
            message = this.service.command(user, "设置联系方式", user.address);
            break;
          case "command":
            message = this.service.command(user, c.command || "", value);
            break;
          case "reply":
            message = this.service.reply(user, c);
            break;
          case "exchange_now": {
            const me = this.service.member(user),
              target = this.service.target(me, value, true);
            message = this.service.queueExchange(user, target, "");
            break;
          }
          case "auto_share": {
            if (typeof c.enabled !== "boolean") throw new UserError("设置无效");
            const m = this.service.member(user);
            if (c.enabled) {
              if (!m.share_contact) throw new UserError("请先设置联系方式");
              this.s.run(
                "INSERT OR IGNORE INTO xdc_auto_share VALUES(?)",
                m.id,
              );
            } else
              this.s.run(
                "DELETE FROM xdc_auto_share WHERE membership_id=?",
                m.id,
              );
            message = "已保存。";
            break;
          }
          case "directory": {
            this.service.member(user);
            this.s.run(
              "UPDATE v2_instances SET page=? WHERE token=?",
              c.page ?? 0,
              instance.token,
            );
            break;
          }
          case "admin_open":
            this.admin.open(user, instance.token);
            break;
          case "admin_unlock": {
            if (!challenge || challenge.expires_at <= now())
              throw new UserError("验证已过期，请重新输入");
            const expected = hmac(
              Buffer.from(this.admin.verifier.key, "hex"),
              "contact-admin-v1:" + challenge.nonce + ":" + instance.token,
            );
            if (!equal(c.proof || "", expected))
              throw new UserError("密钥验证失败");
            this.s.run(
              "INSERT OR REPLACE INTO xdc_admin_grants VALUES(?,?,?)",
              user.id,
              this.admin.version,
              now(),
            );
            this.s.run(
              "DELETE FROM xdc_admin_challenges WHERE token=?",
              instance.token,
            );
            this.admin.open(user, instance.token);
            this.service.audit(user, null, "xdc_admin_unlock");
            message = "管理员验证成功。";
            break;
          }
          case "admin_lock":
            this.s.run("DELETE FROM xdc_admin_grants WHERE user_id=?", user.id);
            this.s.run(
              "DELETE FROM xdc_admin_challenges WHERE token=?",
              instance.token,
            );
            this.admin.challenge(user, instance.token);
            break;
          case "admin_select":
            this.admin.select(
              user,
              instance.token,
              c.admin_network || 0,
              c.page ?? 0,
            );
            break;
          case "admin_action": {
            const ctx = this.s.one(
              "SELECT admin_network FROM v2_instances WHERE token=?",
              instance.token,
            )!;
            message = this.admin.action(
              user,
              ctx.admin_network,
              c.operation || "",
              c.data || {},
              instance.token,
            );
            break;
          }
          case "recover_by_name":
            message = this.service.recover(user, c);
            break;
        }
        this.s.db.exec("RELEASE action");
      } catch (e) {
        this.s.db.exec("ROLLBACK TO action; RELEASE action");
        if (!(e instanceof UserError)) throw e;
        ok = 0;
        error = e.message;
        message = e.message;
      }
      this.s.run(
        "INSERT INTO v2_results VALUES(?,?,?,?,?,?,?)",
        instance.token,
        c.id,
        digest,
        ok,
        error,
        message,
        now(),
      );
      if (serial !== undefined) this.cursor(instance, serial);
    });
    return true;
  }
  cursor(instance: Row, serial: number) {
    this.s.run(
      "INSERT INTO xdc_cursors VALUES(?,?,?) ON CONFLICT(account_id,message_id) DO UPDATE SET serial=MAX(serial,excluded.serial)",
      instance.account_id,
      instance.message_id,
      serial,
    );
  }
  snapshot(instance: Row): Row {
    const u = this.service.user(instance.user_id),
      ctx = this.s.one(
        "SELECT * FROM v2_instances WHERE token=?",
        instance.token,
      )!,
      profile = this.s.one(
        "SELECT member_code,nickname,bio,discoverable,accepts_requests,share_contact,id FROM memberships WHERE user_id=? AND network_id=? AND status='active'",
        u.id,
        u.current_network_id,
      );
    const networks = this.s
      .all(
        "SELECT n.public_id,n.name,m.nickname,m.member_code,n.id FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.status='active' ORDER BY n.id",
        u.id,
      )
      .map((n) => ({
        public_id: n.public_id,
        name: n.name,
        nickname: n.nickname,
        member_code: n.member_code,
        current: n.id === u.current_network_id,
      }));
    const active =
      profile &&
      this.s.one(
        "SELECT 1 FROM networks WHERE id=? AND status='active'",
        u.current_network_id,
      );
    let directory = null;
    if (active) {
      const total = Number(
          this.s.one(
            "SELECT count(*) n FROM memberships WHERE network_id=? AND status='active' AND discoverable=1 AND user_id<>?",
            u.current_network_id,
            u.id,
          )?.n,
        ),
        page = Math.min(ctx.page, Math.max(0, Math.ceil(total / 12) - 1)),
        members = this.s.all(
          "SELECT member_code,nickname,bio FROM memberships WHERE network_id=? AND status='active' AND discoverable=1 AND user_id<>? ORDER BY nickname,member_code LIMIT 13 OFFSET ?",
          u.current_network_id,
          u.id,
          page * 12,
        );
      directory = {
        page,
        members: members.slice(0, 12),
        has_more: members.length > 12,
      };
    }
    const requests = profile
      ? this.s.all(
          `SELECT r.public_id,r.status,r.reason,r.request_kind,r.expires_at,a.nickname sender,b.nickname recipient,a.member_code sender_code,b.member_code recipient_code,CASE WHEN a.user_id=? THEN 'outgoing' ELSE 'incoming' END direction,EXISTS(SELECT 1 FROM v2_jobs j WHERE j.request_id=r.id AND j.status='uncertain') delivery_uncertain FROM contact_requests r JOIN memberships a ON a.id=r.requester_membership_id JOIN memberships b ON b.id=r.target_membership_id WHERE r.network_id=? AND (a.user_id=? OR b.user_id=?) ORDER BY r.id DESC LIMIT 20`,
          u.id,
          u.current_network_id,
          u.id,
          u.id,
        )
      : [];
    if (profile) {
      profile.auto_share = !!this.s.one(
        "SELECT 1 FROM xdc_auto_share WHERE membership_id=?",
        profile.id,
      );
      profile.communication_address = u.address;
      delete profile.id;
    }
    const p = this.s.one(
        "SELECT * FROM pending_actions WHERE user_id=? AND expires_at>?",
        u.id,
        now(),
      ),
      pending = p
        ? { action: p.action, token: p.token, ...JSON.parse(p.payload) }
        : null;
    const results = this.s.all(
        "SELECT request_id,ok,error,message FROM v2_results WHERE token=? ORDER BY rowid DESC LIMIT 100",
        instance.token,
      ),
      latest = results[0];
    return {
      type: "contact-state",
      notifications: this.s
        .all(
          "SELECT id,body,created_at at FROM v2_notifications WHERE user_id=? ORDER BY rowid DESC LIMIT 30",
          u.id,
        )
        .reverse(),
      profile,
      communication_address: profile ? u.address : "",
      admin_role: this.admin.system(u) ? "system" : null,
      requests,
      blocked_members: profile
        ? this.s.all(
            "SELECT m.member_code,m.nickname,m.status FROM blocks b JOIN memberships me ON me.id=b.blocker_membership_id JOIN memberships m ON m.id=b.blocked_membership_id WHERE b.network_id=? AND me.user_id=?",
            u.current_network_id,
            u.id,
          )
        : [],
      current_network: u.current_network_id,
      choices: [],
      networks,
      pending,
      messages: latest?.message ? [latest.message] : [],
      directory,
      notifications_enabled: true,
      admin: this.admin.snapshot(u, instance.token),
      receipt: latest ? { request_id: latest.request_id, ok: latest.ok } : null,
      recent_receipts: results.map((r) => ({
        request_id: r.request_id,
        ok: r.ok,
        ...(r.error ? { error: r.error } : {}),
      })),
    };
  }
  async publish(instance: Row, force = false, realtime = false) {
    const fresh = this.instance(instance.account_id, instance.message_id);
    if (!fresh) return;
    force = force || this.fullSnapshots.has(fresh.token);
    const state = this.s.tx(() => this.snapshot(fresh)),
      encoded = JSON.stringify(state),
      version = hash(encoded),
      previous = fresh.snapshot ? JSON.parse(fresh.snapshot) : null;
    if (!force && previous && hash(JSON.stringify(previous)) === version)
      return;
    const patch: Row = {};
    for (const [k, v] of Object.entries(state))
      if (!previous || JSON.stringify(previous[k]) !== JSON.stringify(v))
        patch[k] = v;
    const full = force || !previous || fresh.revision % 20 === 0;
    const sequence = Number(fresh.revision) + 1;
    this.s.tx(() =>
      this.s.run(
        "UPDATE v2_instances SET revision=? WHERE token=?",
        sequence,
        fresh.token,
      ),
    );
    const cold = {
      role: "bot",
      sequence,
      version,
      base: full ? null : hash(JSON.stringify(previous)),
      ...(full ? { state } : { patch }),
      ack_req_id: state.receipt?.request_id,
    };
    if (realtime) {
      try {
        await this.publisher.realtime(
          fresh.account_id,
          fresh.message_id,
          encrypt(
            { role: "bot", sequence, version, base: null, state },
            fresh.token,
          ),
        );
      } catch {
        /* Durable update remains authoritative. */
      }
    }
    await this.publisher.status(
      fresh.account_id,
      fresh.message_id,
      encrypt(cold, fresh.token),
      !!previous &&
        previous.notifications?.at(-1)?.id !==
          state.notifications?.at(-1)?.id &&
        state.notifications?.length > 0,
    );
    this.fullSnapshots.delete(fresh.token);
    this.s.tx(() =>
      this.s.run(
        "UPDATE v2_instances SET snapshot=? WHERE token=?",
        encoded,
        fresh.token,
      ),
    );
  }
  async publishRealtime(instance: Row) {
    const fresh = this.instance(instance.account_id, instance.message_id);
    if (!fresh) return;
    const state = this.s.tx(() => this.snapshot(fresh)),
      sequence = Number(fresh.revision) + 1;
    this.s.tx(() =>
      this.s.run(
        "UPDATE v2_instances SET revision=? WHERE token=?",
        sequence,
        fresh.token,
      ),
    );
    await this.publisher.realtime(
      fresh.account_id,
      fresh.message_id,
      encrypt(
        {
          role: "bot",
          sequence,
          version: hash(JSON.stringify(state)),
          base: null,
          state,
        },
        fresh.token,
      ),
    );
  }
  async publishAll(forceToken?: string, realtimeToken?: string) {
    for (const row of this.s.all(
      "SELECT x.*,v.user_id FROM xdc_instances x JOIN v2_instances v ON v.token=x.token",
    )) {
      try {
        await this.publish(
          row,
          row.token === forceToken,
          row.token === realtimeToken || !!this.publisher.isLive?.(row.token),
        );
      } catch (e) {
        console.warn(
          "State publication deferred:",
          e instanceof Error ? e.name : "Error",
        );
      }
    }
  }
}
