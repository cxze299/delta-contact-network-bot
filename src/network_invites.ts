import type { Service } from "./service";
import { hmac, random } from "./crypto";
import { UserError, now } from "./protocol";
export class NetworkInvites {
  constructor(readonly service: Service) {}
  token(input: string): string | null {
    let value = input.trim();
    if (/^https?:\/\//.test(value)) { try { value = new URLSearchParams(new URL(value).hash.slice(1)).get("invite") || ""; } catch { return null; } }
    return /^INV-[A-Za-z0-9_-]{32}$/.test(value) ? value : null;
  }
  find(input: string) {
    const token = this.token(input);
    if (!token) throw new UserError("邀请链接或邀请码不正确");
    const row = this.service.s.one("SELECT i.*,n.name,n.status network_status,n.join_enabled FROM v2_network_invites i JOIN networks n ON n.id=i.network_id WHERE i.token_hash=?", hmac(this.service.config.secret, token));
    if (!row || row.revoked || row.expires_at <= now() || row.network_status !== "active" || !row.join_enabled)
      throw new UserError("邀请已失效或网络已停止加入，请联系管理员");
    return row;
  }
  create(network: number, creator: number, duration = "7") {
    const base = this.service.config.publicUrl;
    if (!base) throw new UserError("请先配置邀请页面的公网地址");
    if (!["1","7","forever"].includes(duration)) throw new UserError("请选择 1 天、7 天或长期有效");
    const token = "INV-" + random(24), expires = duration === "forever" ? "9999-12-31T23:59:59.999Z" : new Date(Date.now()+Number(duration)*86400000).toISOString();
    this.service.s.run("INSERT INTO v2_network_invites(network_id,creator_id,token_hash,token_cipher,expires_at,created_at) VALUES(?,?,?,?,?,?)", network, creator, hmac(this.service.config.secret,token),this.service.storage(token),expires,now());
  }
  list(network: number, creator?: number) {
    return this.service.s.all("SELECT * FROM v2_network_invites WHERE network_id=? AND (? IS NULL OR creator_id=?) AND revoked=0 AND expires_at>? ORDER BY id DESC",network,creator ?? null,creator ?? null,now()).map(row=>({id:row.id,expires_at:row.expires_at,uses:row.uses,active:!row.revoked && row.expires_at>now(),link:this.service.config.publicUrl+"/join#invite="+this.service.readCode(row.token_cipher)}));
  }
}
