"""Scoped Webxdc management and proof-based system administrator unlock."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from .admin_actions import execute
from .service import UserError, _iso, _parse

ITERATIONS = 250_000
SYSTEM_ACTIONS = {"create", "rename", "appoint", "revoke", "disable", "enable"}
NETWORK_ACTIONS = {"view_code", "rotate", "join_off", "join_on", "remove", "ban", "unban", "resolve_report", "approve_recovery", "reject_recovery", "delivery_close"}
ADMIN_COMMANDS = {
    "管理", "创建网络", "网络概况", "成员列表", "查看加入码", "更换加入码", "停用加入",
    "恢复加入", "移除成员", "封禁成员", "解除封禁", "举报列表", "处理举报", "管理记录",
    "任命管理员", "撤销管理员", "停用网络", "恢复网络", "网络列表", "修改网络名称",
    "恢复申请列表", "批准恢复", "拒绝恢复",
}
ADMIN_PENDING = {"rotate_code", "remove_member", "ban_member", "disable_network", "guide_admin_create_code", "guide_admin_rotate_code"}


def make_verifier(secret: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"{ITERATIONS}:{salt}:{key.hex()}"


def parse_verifier(value: str):
    try:
        iterations, salt, key = value.split(":")
        if int(iterations) != ITERATIONS or len(salt) != 32 or len(key) != 64:
            return None
        bytes.fromhex(salt); bytes.fromhex(key)
        return {"iterations": int(iterations), "salt": salt, "key": key}
    except (ValueError, TypeError, AttributeError):
        return None


class XdcAdmin:
    def __init__(self, service, verifier: str):
        self.service = service
        self.verifier = parse_verifier(verifier)
        self.version = hashlib.sha256(verifier.encode()).hexdigest() if self.verifier else ""
        with service.db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_admin_sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,version TEXT NOT NULL,expires_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_admin_challenges(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,nonce TEXT NOT NULL,expires_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_admin_context(token TEXT PRIMARY KEY,network_id INTEGER,page INTEGER NOT NULL DEFAULT 0)")

            conn.execute("CREATE TABLE IF NOT EXISTS xdc_admin_grants(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,version TEXT NOT NULL,granted_at TEXT NOT NULL)")

    def has_access(self, conn, user, token, now):
        return bool(self.verifier and conn.execute("SELECT 1 FROM xdc_admin_grants WHERE user_id=? AND version=?", (user["id"], self.version)).fetchone())

    def require(self, conn, user, token, now):
        if not self.has_access(conn, user, token, now):
            raise UserError("请先验证系统管理员密钥")

    def challenge(self, conn, user, token, now):
        if not self.verifier:
            raise UserError("系统管理员密钥尚未配置")
        nonce = secrets.token_hex(32)
        expires = _iso(_parse(now) + timedelta(minutes=5))
        conn.execute("INSERT OR REPLACE INTO xdc_admin_challenges VALUES(?,?,?,?)", (token, user["id"], nonce, expires))

    def open(self, conn, user, token, now):
        if not self.has_access(conn, user, token, now):
            self.challenge(conn, user, token, now)
            return
        user = {**dict(user), "is_system_admin": 1}
        current = user["current_network_id"]
        try:
            self.service._assert_admin_for(conn, user, current)
            if not conn.execute("SELECT 1 FROM networks WHERE id=?", (current,)).fetchone():
                current = None
        except UserError:
            current = None
        if current is None:
            if user["is_system_admin"]:
                row = conn.execute("SELECT id FROM networks ORDER BY id LIMIT 1").fetchone()
            else:
                row = conn.execute("SELECT network_id FROM memberships WHERE user_id=? AND role='network_admin' AND status='active' ORDER BY network_id LIMIT 1", (user["id"],)).fetchone()
            current = row[0] if row else None
        conn.execute("INSERT OR IGNORE INTO xdc_admin_context VALUES(?,?,0)", (token, current))

    def prepare_attempt(self, conn, user, token, now):
        """Persist rate event and consume nonce outside the per-action savepoint."""
        if not self.verifier:
            return None, False
        allowed = self.service._rate_allowed(conn, user["actor_key"], "xdc_admin_key", 5, 15, now)
        previous = conn.execute("SELECT nonce,expires_at FROM xdc_admin_challenges WHERE token=? AND user_id=?", (token, user["id"])).fetchone()
        self.challenge(conn, user, token, now)
        return previous, allowed

    def unlock(self, conn, user, token, request, now, previous, allowed):
        if not self.verifier:
            raise UserError("你没有系统管理员权限或密钥尚未配置")
        if not allowed:
            raise UserError("验证次数过多，请 15 分钟后再试")
        if not previous or previous["expires_at"] <= now:
            raise UserError("验证已过期，请重试")
        proof = request.get("proof")
        if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
            raise UserError("密钥验证失败")
        expected = hmac.new(bytes.fromhex(self.verifier["key"]), ("contact-admin-v1:" + previous["nonce"] + ":" + token).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, proof):
            raise UserError("密钥验证失败")
        conn.execute("INSERT OR REPLACE INTO xdc_admin_grants VALUES(?,?,?)", (user["id"], self.version, now))
        conn.execute("DELETE FROM xdc_admin_challenges WHERE token=?", (token,))
        self.open(conn, user, token, now)
        self.service._audit(conn, None, user["id"], "xdc_admin_unlock", "", "", now)

    def select(self, conn, user, token, request, now):
        self.require(conn, user, token, now)
        user = {**dict(user), "is_system_admin": 1}
        network_id = request.get("admin_network")
        page = request.get("page", 0)
        if isinstance(network_id, bool) or not isinstance(network_id, int) or isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 999:
            raise UserError("网络或页码无效")
        self.service._assert_admin_for(conn, user, network_id)
        if not conn.execute("SELECT 1 FROM networks WHERE id=?", (network_id,)).fetchone():
            raise UserError("网络不存在")
        conn.execute("INSERT OR REPLACE INTO xdc_admin_context VALUES(?,?,?)", (token, network_id, page))

    def action(self, conn, user, token, request, now, key):
        self.require(conn, user, token, now)
        user = {**dict(user), "is_system_admin": 1}
        action, data = request.get("operation"), request.get("data", {})
        if not isinstance(action, str) or action not in NETWORK_ACTIONS | SYSTEM_ACTIONS or not isinstance(data, dict):
            raise UserError("不支持此管理操作")
        if action in SYSTEM_ACTIONS and not user["is_system_admin"]:
            raise UserError("需要系统管理员权限")
        if len(data) > 8 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 500 for k, v in data.items()):
            raise UserError("管理输入无效")
        ctx = conn.execute("SELECT network_id FROM xdc_admin_context WHERE token=?", (token,)).fetchone()
        network_id = ctx[0] if ctx else None
        if action != "create":
            if network_id is None:
                raise UserError("请选择网络")
            self.service._assert_admin_for(conn, user, network_id)
        result = execute(self.service, conn, user, network_id if action != "create" else None, action, data, now, verified_system=True)
        self.service._reply(conn, user["id"], result, key, sensitive=True)

    def snapshot(self, conn, user, token, now):
        is_system = self.has_access(conn, user, token, now)
        if not is_system:
            challenge = conn.execute("SELECT nonce,expires_at FROM xdc_admin_challenges WHERE token=? AND user_id=? AND expires_at>?", (token, user["id"], now)).fetchone()
            return {"unlocked": False, "challenge": {"nonce": challenge["nonce"], "salt": self.verifier["salt"], "iterations": self.verifier["iterations"]} if challenge and self.verifier else None}
        ctx = conn.execute("SELECT network_id,page FROM xdc_admin_context WHERE token=?", (token,)).fetchone()
        if not ctx:
            return {"unlocked": True, "networks": [], "current": None}
        if is_system:
            networks = conn.execute("SELECT n.id,n.public_id,n.name,n.status,n.join_enabled,(SELECT count(*) FROM memberships m WHERE m.network_id=n.id AND m.status='active') member_count FROM networks n ORDER BY n.name").fetchall()
        else:
            networks = conn.execute("SELECT n.id,n.public_id,n.name,n.status,n.join_enabled,(SELECT count(*) FROM memberships m WHERE m.network_id=n.id AND m.status='active') member_count FROM networks n JOIN memberships a ON a.network_id=n.id WHERE a.user_id=? AND a.role='network_admin' AND a.status='active' ORDER BY n.name", (user["id"],)).fetchall()
        allowed_ids = {n["id"] for n in networks}
        network_id = ctx["network_id"] if ctx["network_id"] in allowed_ids else None
        data = {"unlocked": True, "system": is_system, "networks": [dict(n) for n in networks], "current": network_id, "page": ctx["page"], "members": [], "reports": [], "recoveries": [], "audit": []}
        if network_id is None:
            return data
        total = conn.execute("SELECT count(*) FROM memberships WHERE network_id=? AND status='active'", (network_id,)).fetchone()[0]
        page = min(ctx["page"], max(0, (total - 1) // 20))
        if page != ctx["page"]:
            conn.execute("UPDATE xdc_admin_context SET page=? WHERE token=?", (page, token))
        data["page"] = page
        members = conn.execute("""SELECT m.member_code,m.nickname,m.role,m.status,m.discoverable,m.user_id,u.is_system_admin,
            EXISTS(SELECT 1 FROM bans b WHERE b.network_id=m.network_id AND b.actor_key=u.actor_key) banned
            FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.network_id=? AND m.status='active'
            ORDER BY m.nickname,m.member_code LIMIT 21 OFFSET ?""", (network_id, page * 20)).fetchall()
        data["members"] = [{**dict(m), "is_self": m["user_id"] == user["id"]} for m in members[:20]]
        for member in data["members"]:
            member["key_admin"] = bool(conn.execute("SELECT 1 FROM xdc_admin_grants WHERE user_id=? AND version=?", (member["user_id"], self.version)).fetchone())
            del member["user_id"]
        data["has_more"] = len(members) > 20
        data["bans"] = [dict(m) for m in conn.execute("""SELECT m.member_code,m.nickname FROM bans b
            JOIN users u ON u.actor_key=b.actor_key JOIN memberships m ON m.user_id=u.id AND m.network_id=b.network_id
            WHERE b.network_id=? ORDER BY b.created_at DESC LIMIT 150""", (network_id,))]
        data["reports"] = [dict(r) for r in conn.execute("""SELECT r.public_id,r.reason,r.status,r.resolution,a.nickname reporter,b.nickname target
            FROM reports r JOIN memberships a ON a.id=r.reporter_membership_id JOIN memberships b ON b.id=r.target_membership_id
            WHERE r.network_id=? ORDER BY r.id DESC LIMIT 30""", (network_id,))]
        data["recoveries"] = [dict(r) for r in conn.execute("""SELECT ar.public_id,ar.status,ar.expires_at,m.nickname,m.member_code FROM account_recoveries ar
            JOIN memberships m ON m.id=ar.membership_id WHERE m.network_id=? AND ar.status='pending' AND ar.expires_at>? ORDER BY ar.id DESC LIMIT 30""", (network_id, now))]
        data["deliveries"] = [dict(r) for r in conn.execute("""SELECT o.id,coalesce(r.public_id,'D' || o.id) public_id,coalesce(m.nickname,'成员') recipient,o.delivery_started_at
            FROM outbox o LEFT JOIN contact_requests r ON o.dedupe_key LIKE 'exchange:' || r.public_id || ':%' OR o.dedupe_key LIKE 'reissue:' || r.public_id || ':%'
            LEFT JOIN memberships m ON m.user_id=o.recipient_user_id AND m.network_id=o.delivery_network_id
            WHERE o.delivery_network_id=? AND o.status='sending' AND o.delivery_closed_at IS NULL ORDER BY o.id LIMIT 30""", (network_id,))]
        data["audit"] = [dict(r) for r in conn.execute("SELECT action,target_ref,details,created_at FROM audit_log WHERE network_id=? ORDER BY id DESC LIMIT 30", (network_id,))]
        return data
