"""Webxdc UI over authenticated, encrypted chat messages, never selfAddr."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from deltachat2 import MessageData

from .service import UserError, _now
from .xdc_admin import ADMIN_COMMANDS, ADMIN_PENDING, XdcAdmin

PREFIX = "联系人小程序 "
COMMANDS = frozenset({
    "查找", "申请", "同意申请", "拒绝申请", "撤销申请", "我的申请", "我的资料",
    "编辑昵称", "编辑介绍", "设置联系方式", "公开资料", "隐藏资料", "接收申请",
    "暂停申请", "退出网络", "删除我的数据", "屏蔽", "解除屏蔽", "屏蔽列表",
    "举报", "切换网络", "我的网络", "取消", "管理", "创建网络", "网络概况",
    "成员列表", "查看加入码", "更换加入码", "停用加入", "恢复加入", "移除成员",
    "封禁成员", "解除封禁", "举报列表", "处理举报", "管理记录", "任命管理员",
    "撤销管理员", "停用网络", "恢复网络", "网络列表", "修改网络名称",
    "恢复申请列表", "批准恢复", "拒绝恢复", "恢复账号",
})


def package(destination: Path, token: str = "") -> None:
    assets = Path(__file__).with_name("webxdc")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("index.html", "app.js", "style.css", "manifest.toml", "icon.png"):
            archive.write(assets / name, name)
        archive.writestr("config.js", "window.CONTACT_CONFIG=" + json.dumps({"token": token}) + ";")


def package_p2p(destination: Path) -> None:
    """Build the standalone, peer-to-peer Webxdc prototype.

    Unlike :func:`package`, this archive has no service token or backend
    integration.  Its complete application state travels as Webxdc updates.
    """
    assets = Path(__file__).with_name("webxdc_p2p")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("index.html", "app.js", "style.css", "manifest.toml"):
            archive.write(assets / name, name)


class WebxdcBridge:
    def __init__(self, rpc, service, *, require_verified=False, identity_key=None, identity_resolver=None):
        self.rpc, self.service = rpc, service
        self.require_verified = require_verified
        self.identity_key = identity_key
        self.identity_resolver = identity_resolver
        self._last_sync = {}
        self._summary_cleanup_done = set()
        self.admin = XdcAdmin(service, os.environ.get("CONTACTBOT_XDC_ADMIN_VERIFIER", ""))
        with service.db.transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xdc_instances (
                    token TEXT PRIMARY KEY, actor_key TEXT NOT NULL,
                    account_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xdc_requests (
                    token TEXT NOT NULL, request_id TEXT NOT NULL,
                    PRIMARY KEY(token,request_id)
                );
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_request_results (token TEXT NOT NULL, request_id TEXT NOT NULL, ok INTEGER NOT NULL, PRIMARY KEY(token,request_id), FOREIGN KEY(token,request_id) REFERENCES xdc_requests(token,request_id) ON DELETE CASCADE)")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_cursors (account_id INTEGER NOT NULL, message_id INTEGER NOT NULL, serial INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account_id,message_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_receipts (token TEXT PRIMARY KEY, request_id TEXT NOT NULL, ok INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_directory (token TEXT PRIMARY KEY, network_id INTEGER NOT NULL, page INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_auto_share (membership_id INTEGER PRIMARY KEY REFERENCES memberships(id) ON DELETE CASCADE)")
            conn.execute("CREATE TABLE IF NOT EXISTS xdc_auto_exchanges (request_id INTEGER PRIMARY KEY REFERENCES contact_requests(id) ON DELETE CASCADE)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(xdc_instances)")}
            if "notifications_enabled" not in columns:
                conn.execute("ALTER TABLE xdc_instances ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 0")

    def offer(self, incoming, *, replace=False):
        if not incoming.is_private or not incoming.is_encrypted:
            return
        with self.service.db.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM xdc_instances WHERE actor_key=? AND account_id=? AND chat_id=?",
                (incoming.actor_key, incoming.account_id, incoming.chat_id),
            ).fetchone()
        if exists and not replace:
            return
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix="contact-xdc-") as folder:
            path = Path(folder) / "好友助手.xdc"
            package(path, token)
            message_id = self.rpc.send_msg(
                incoming.account_id, incoming.chat_id,
                MessageData(file=str(path)),
            )
        with self.service.db.transaction() as conn:
            # Older instances remain usable: users may reopen an earlier chat item.
            conn.execute("INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)", (
                token, incoming.actor_key, incoming.account_id, incoming.chat_id, message_id, _now(),
            ))
        self.sync(incoming)

    def handle(self, incoming):
        if not incoming.text.startswith(PREFIX):
            return False
        if not incoming.is_private or not incoming.is_encrypted:
            return True
        if len(incoming.text) > 8000:
            return True
        try:
            request = json.loads(incoming.text[len(PREFIX):])
            if not isinstance(request, dict):
                return True
            token, request_id = request.get("token"), request.get("id")
            if not isinstance(token, str) or not isinstance(request_id, str) or not request_id or len(request_id) > 100:
                return True
        except (ValueError, TypeError):
            return True
        with self.service.db.transaction() as conn:
            instance = conn.execute(
                "SELECT * FROM xdc_instances WHERE token=? AND actor_key=? AND account_id=? AND chat_id=?",
                (token, incoming.actor_key, incoming.account_id, incoming.chat_id),
            ).fetchone()
            if not instance:
                return True
            duplicate = conn.execute("SELECT r.ok FROM xdc_requests q LEFT JOIN xdc_request_results r ON r.token=q.token AND r.request_id=q.request_id WHERE q.token=? AND q.request_id=?", (token, request_id)).fetchone()
            if duplicate:
                prior = conn.execute("SELECT request_id,ok FROM xdc_receipts WHERE token=?", (token,)).fetchone()
                # Older requests did not store an outcome. Never execute them again
                # or claim success if their original outcome cannot be recovered.
                outcome = duplicate["ok"] if duplicate["ok"] is not None else (prior["ok"] if prior and prior["request_id"] == request_id else 0)
                conn.execute("INSERT INTO xdc_receipts VALUES(?,?,?) ON CONFLICT(token) DO UPDATE SET request_id=excluded.request_id,ok=excluded.ok", (token, request_id, outcome))
                self._last_sync.pop((incoming.account_id, instance["message_id"]), None)
                return True
            now = _now()
            user = self.service._ensure_user(conn, incoming, now)
            self.service._expire(conn, now)
            conn.execute("INSERT INTO xdc_requests(token,request_id) VALUES(?,?)", (token, request_id))
            key = "xdc:" + request_id
            success = True
            onboarding_action = False
            action = request.get("action")
            previous, allowed = self.admin.prepare_attempt(conn, user, token, now) if action == "admin_unlock" else (None, False)
            # Record failed attempts outside the action savepoint so rollback cannot
            # bypass the recovery join-code rate limit.
            recovery_allowed = self.service._rate_allowed(conn, user["actor_key"], "recovery_lookup", 10, 60, now) if action == "recover_by_name" else True
            conn.execute("SAVEPOINT xdc_action")
            try:
                value = request.get("value", "")
                if not isinstance(value, str) or len(value) > 2000:
                    raise UserError("输入内容必须为文本")
                if action not in {"refresh", "cancel_join"} and request.get("network") != user["current_network_id"]:
                    raise UserError("当前网络已变化，请刷新后操作")
                if action == "start":
                    existing = conn.execute("SELECT 1 FROM pending_actions WHERE user_id=? AND expires_at>?", (user["id"], now)).fetchone()
                    if not existing:
                        self.service._begin_guidance(conn, user, now, key)
                elif action == "cancel_join":
                    step = request.get("step")
                    if not isinstance(step, str):
                        raise UserError("加入步骤无效，请刷新")
                    pending = conn.execute("SELECT action FROM pending_actions WHERE user_id=? AND token=?", (user["id"], step)).fetchone()
                    if not pending or pending["action"] not in {"guide_join_code", "guide_nickname", "join"}:
                        raise UserError("该加入步骤已结束，请刷新当前状态")
                    conn.execute("DELETE FROM pending_actions WHERE user_id=? AND token=?", (user["id"], step))
                elif action == "join":
                    onboarding_action = True
                    self.service._pending(conn, user["id"], "guide_join_code", {}, now)
                    if value.strip():
                        pending = conn.execute("SELECT * FROM pending_actions WHERE user_id=?", (user["id"],)).fetchone()
                        self.service._guided_reply(conn, user, pending, value, True, incoming.contact_address, now, key)
                        if conn.execute("SELECT 1 FROM pending_actions WHERE user_id=? AND action='guide_join_code'", (user["id"],)).fetchone():
                            success = False
                            conn.execute("UPDATE outbox SET body='加入码无效或已停用，请检查后重试。' WHERE dedupe_key=?", (f"{key}:{user['id']}",))
                    else:
                        self.service._reply(conn, user["id"], "请输入管理员提供的加入码。", key)
                elif action == "recover_by_name":
                    if not recovery_allowed:
                        raise UserError("恢复尝试过多，请一小时后再试")
                    join_code, nickname, old_address = request.get("join_code"), request.get("nickname"), request.get("old_address")
                    if not isinstance(join_code, str) or not isinstance(nickname, str) or not isinstance(old_address, str) or not 1 <= len(join_code.strip()) <= 200 or not 1 <= len(nickname.strip()) <= 40 or not self.service._valid_contact(old_address.strip()):
                        raise UserError("请输入网络加入码、原昵称和原 Delta Chat 账号")
                    network = self.service._find_network_by_code(conn, join_code.strip())
                    old_actor = hmac.new(self.service.secret, old_address.strip().casefold().encode("utf-8"), hashlib.sha256).hexdigest()
                    rows = conn.execute("""SELECT m.member_code FROM memberships m JOIN users u ON u.id=m.user_id
                        WHERE m.network_id=? AND m.nickname=? AND m.status='active' AND (u.actor_key=? OR EXISTS
                        (SELECT 1 FROM users alias WHERE alias.merged_into=u.id AND (alias.actor_key=? OR alias.actor_key=('merged:' || alias.id || ':' || ?)))) LIMIT 2""",
                        (network["id"], nickname.strip(), old_actor, old_actor, old_actor)).fetchall() if network and network["status"] == "active" else []
                    if len(rows) != 1:
                        raise UserError("未找到唯一匹配的成员，请检查信息或联系管理员")
                    self.service._submit_account_recovery(conn, user, network["public_id"] + " " + rows[0]["member_code"], now, key)
                elif action == "use_current_account":
                    onboarding_action = True
                    if not self.service._valid_contact(incoming.contact_address):
                        raise UserError("暂时无法读取当前账号，请刷新后重试")
                    self.service._edit_profile(conn, user, "设置联系方式", incoming.contact_address, now, key, incoming.contact_address)
                    conn.execute("DELETE FROM pending_actions WHERE user_id=? AND action IN ('guide_contact_choice','guide_contact_manual','guide_privacy_choice')", (user["id"],))
                elif action == "admin_open":
                    self.admin.open(conn, user, token, now)
                elif action == "admin_unlock":
                    self.admin.unlock(conn, user, token, request, now, previous, allowed)
                elif action == "admin_lock":
                    conn.execute("DELETE FROM xdc_admin_grants WHERE user_id=?", (user["id"],))
                    self.service._audit(conn, None, user["id"], "xdc_admin_revoke_self", "", "", now)
                    conn.execute("DELETE FROM xdc_admin_sessions WHERE token=?", (token,))
                    conn.execute("DELETE FROM xdc_admin_challenges WHERE token=?", (token,))
                elif action == "admin_select":
                    self.admin.select(conn, user, token, request, now)
                elif action == "admin_action":
                    self.admin.action(conn, user, token, request, now, key)
                elif action == "search":
                    if not value.strip():
                        raise UserError("请输入昵称或成员编号")
                    self.service._pending(conn, user["id"], "guide_search", {}, now)
                    pending = conn.execute("SELECT * FROM pending_actions WHERE user_id=?", (user["id"],)).fetchone()
                    self.service._guided_reply(conn, user, pending, value, True, incoming.contact_address, now, key + ":choices")
                elif action == "auto_share":
                    enabled = request.get("enabled")
                    if not isinstance(enabled, bool):
                        raise UserError("设置无效")
                    member, network = self.service._current_membership(conn, user)
                    if enabled:
                        if not member["share_contact"]:
                            raise UserError("请先设置联系方式")
                        conn.execute("INSERT OR IGNORE INTO xdc_auto_share VALUES(?)", (member["id"],))
                    else:
                        conn.execute("DELETE FROM xdc_auto_share WHERE membership_id=?", (member["id"],))
                    self.service._reply(conn, user["id"], "自动交换名片已开启。" if enabled else "自动交换名片已关闭。", key)
                elif action == "exchange_now":
                    self.service._exchange_now(conn, user, value, now, key)
                elif action == "directory":
                    page = request.get("page")
                    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 999:
                        raise UserError("页面无效")
                    _, network = self.service._current_membership(conn, user)
                    if network["status"] != "active":
                        raise UserError("当前网络已停用")
                    if not self.service._rate_allowed(conn, user["actor_key"], "search", 30, 60, now):
                        raise UserError("查看次数过多，请稍后再试")
                    conn.execute("INSERT INTO xdc_directory VALUES(?,?,?) ON CONFLICT(token) DO UPDATE SET network_id=excluded.network_id,page=excluded.page", (token, network["id"], page))
                elif action == "reply":
                    if not isinstance(request.get("step"), str):
                        raise UserError("页面已过期，请刷新")
                    pending = conn.execute("SELECT * FROM pending_actions WHERE user_id=? AND token=? AND expires_at>?", (user["id"], request.get("step"), now)).fetchone()
                    if not pending:
                        raise UserError("页面已过期，请刷新后重新操作")
                    onboarding_action = pending["action"] in {"guide_join_code", "guide_nickname", "join"}
                    if pending["action"] in ADMIN_PENDING:
                        self.admin.require(conn, user, token, now)
                    if pending["action"] == "guide_choose_target":
                        raise UserError("请在成员资料卡填写联系理由并提交申请")
                    elif pending["action"] == "guide_nickname":
                        self.service._validate_nickname(value)
                        payload = json.loads(pending["payload"])
                        payload["nickname"] = value
                        self.service._pending(conn, user["id"], "join", payload, now)
                        self.service._reply(conn, user["id"], "请阅读隐私说明并确认加入。机器人服务端可处理发给它的数据；交付后的联系方式无法收回。", key)
                    elif pending["action"] == "join" and value == "1":
                        if request.get("consent") is not True:
                            raise UserError("请先确认隐私说明")
                        if not all(isinstance(request.get(k), bool) for k in ("discoverable", "accepts_requests")):
                            raise UserError("请选择资料公开和接收申请设置")
                        payload = json.loads(pending["payload"])
                        use_current = request.get("use_current_account") is True
                        if use_current and not self.service._valid_contact(incoming.contact_address):
                            raise UserError("暂时无法读取当前账号，请刷新后重试")
                        self.service._confirm(conn, user, pending["token"], True, now, key)
                        conn.execute("UPDATE memberships SET discoverable=?,accepts_requests=? WHERE user_id=? AND network_id=?", (int(request["discoverable"]), int(request["accepts_requests"]), user["id"], payload["network_id"]))
                        if use_current:
                            conn.execute("UPDATE memberships SET share_contact=?,share_version=share_version+1,updated_at=? WHERE user_id=? AND network_id=?", (incoming.contact_address.strip(), now, user["id"], payload["network_id"]))
                        conn.execute("UPDATE outbox SET body=? WHERE dedupe_key=?", ("设置完成，可以开始认识好友了。" if use_current else "已加入网络，请完成账号设置。", f"{key}:{user['id']}"))
                    elif pending["action"] == "send_vcard_request":
                        self.service._guided_reply(conn, user, pending, "确认" if value == "1" else value, True, incoming.contact_address, now, key)
                    elif pending["action"].startswith("guide_"):
                        self.service._guided_reply(conn, user, pending, value, True, incoming.contact_address, now, key)
                        if pending["action"] == "guide_join_code" and conn.execute("SELECT 1 FROM pending_actions WHERE user_id=? AND action='guide_join_code'", (user["id"],)).fetchone():
                            success = False
                            conn.execute("UPDATE outbox SET body='加入码无效或已停用，请检查后重试。' WHERE dedupe_key=?", (f"{key}:{user['id']}",))
                    elif value == "1":
                        self.service._confirm(conn, user, pending["token"], True, now, key)
                    elif value == "2":
                        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
                    else:
                        raise UserError("请选择确认或取消")
                elif action == "command":
                    command = request.get("command")
                    if not isinstance(command, str) or command not in COMMANDS:
                        raise UserError("不支持此操作")
                    if command in ADMIN_COMMANDS:
                        self.admin.require(conn, user, token, now)
                    if user["is_system_admin"] and command == "切换网络" and value:
                        membership = conn.execute("SELECT 1 FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.status='active' AND n.public_id=?", (user["id"], value.upper())).fetchone()
                        if not membership:
                            self.admin.require(conn, user, token, now)
                    if command == "取消":
                        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
                    if command == "申请":
                        code = value.split(maxsplit=1)[0].upper() if value.strip() else ""
                        visible = conn.execute("SELECT 1 FROM memberships WHERE network_id=? AND member_code=? AND status='active' AND discoverable=1", (user["current_network_id"], code)).fetchone()
                        if not visible:
                            raise UserError("该成员当前不可通过目录申请，请刷新列表")
                    if command == "设置联系方式" and ("://" in value or "i.delta.chat/#" in value.casefold()):
                        link = self.service._validate_share_link(value, incoming.contact_address)
                        value = parse_qs(urlsplit(link).fragment.split("&", 1)[1])["a"][0].strip()
                    if (command == "设置联系方式" and request.get("verify_current_account") is True
                            and (not incoming.contact_address or value.strip().casefold() != incoming.contact_address.strip().casefold())):
                        raise UserError("这个好友链接不属于当前与你机器人私聊的 Delta Chat 账号")
                    vcard = None
                    if command == "同意申请":
                        vcard = conn.execute("SELECT 1 FROM contact_requests r JOIN memberships m ON m.id=r.target_membership_id WHERE r.public_id=? AND r.request_kind='vcard' AND m.user_id=? AND m.network_id=?", (value.upper(), user["id"], user["current_network_id"])).fetchone()
                    if vcard:
                        self.service._accept_vcard_request(conn, user, value, now, key)
                    else:
                        self.service._dispatch(conn, user, command + (" " + value if value else ""), True, incoming.contact_address, now)
                elif action != "refresh":
                    raise UserError("不支持此操作")
                if onboarding_action and success:
                    conn.execute("DELETE FROM outbox WHERE recipient_user_id=? AND dedupe_key=?", (user["id"], f"{key}:{user['id']}"))
                conn.execute("RELEASE xdc_action")
            except UserError as exc:
                success = False
                conn.execute("ROLLBACK TO xdc_action")
                conn.execute("RELEASE xdc_action")
                self.service._reply(conn, user["id"], str(exc), key)
            conn.execute("INSERT OR REPLACE INTO xdc_request_results(token,request_id,ok) VALUES(?,?,?)", (token, request_id, int(success)))
            conn.execute("INSERT INTO xdc_receipts VALUES(?,?,?) ON CONFLICT(token) DO UPDATE SET request_id=excluded.request_id,ok=excluded.ok", (token, request_id, int(success)))
            if request.get("notification_version") == 1:
                conn.execute("UPDATE xdc_instances SET notifications_enabled=1 WHERE token=?", (token,))
        return True

    def sync(self, incoming):
        if not incoming.is_private or not incoming.is_encrypted:
            return
        with self.service.db.transaction() as conn:
            user = conn.execute("SELECT * FROM users WHERE actor_key=?", (incoming.actor_key,)).fetchone()
            if not user:
                return
            instances = conn.execute("SELECT * FROM xdc_instances WHERE actor_key=? AND account_id=? AND chat_id=?", (incoming.actor_key, incoming.account_id, incoming.chat_id)).fetchall()
            networks = [dict(r) for r in conn.execute("SELECT n.public_id,n.name,m.nickname,m.member_code FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.user_id=? AND m.status='active'", (user["id"],))]
            pending = conn.execute("SELECT action,token,payload FROM pending_actions WHERE user_id=? AND expires_at>?", (user["id"], _now())).fetchone()
            messages = [r[0] for r in conn.execute("SELECT body FROM outbox WHERE recipient_user_id=? AND status='pending' ORDER BY id DESC LIMIT 6", (user["id"],))][::-1]
            profile_row = conn.execute("SELECT member_code,nickname,bio,discoverable,accepts_requests,share_contact FROM memberships WHERE user_id=? AND network_id=? AND status='active'", (user["id"], user["current_network_id"])).fetchone()
            profile = dict(profile_row) if profile_row else None
            if profile:
                profile["auto_share"] = bool(conn.execute("SELECT 1 FROM xdc_auto_share a JOIN memberships m ON m.id=a.membership_id WHERE m.user_id=? AND m.network_id=? AND m.status='active'", (user["id"], user["current_network_id"])).fetchone())
            if profile and incoming.contact_address:
                profile["communication_address"] = incoming.contact_address
            membership = conn.execute("SELECT role FROM memberships WHERE user_id=? AND network_id=? AND status='active'", (user["id"], user["current_network_id"])).fetchone()
            admin_role = "system" if user["is_system_admin"] else "network" if membership and membership["role"] == "network_admin" else None
            requests = [dict(row) for row in conn.execute("""SELECT r.public_id,r.status,r.reason,r.request_kind,r.expires_at,a.nickname AS sender,b.nickname AS recipient,a.member_code AS sender_code,b.member_code AS recipient_code,
                CASE WHEN a.user_id=? THEN 'outgoing' ELSE 'incoming' END AS direction
                FROM contact_requests r JOIN memberships a ON a.id=r.requester_membership_id
                JOIN memberships b ON b.id=r.target_membership_id
                WHERE r.network_id=? AND (a.user_id=? OR b.user_id=?) ORDER BY r.id DESC LIMIT 20""",
                (user["id"], user["current_network_id"], user["id"], user["id"]))] if profile else []
            for request in requests:
                request["delivery_uncertain"] = bool(conn.execute("SELECT 1 FROM outbox WHERE status='sending' AND delivery_closed_at IS NULL AND (dedupe_key LIKE ? OR dedupe_key LIKE ?)", (f"exchange:{request['public_id']}:%", f"reissue:{request['public_id']}:%")).fetchone())
            blocked_members = [dict(row) for row in conn.execute("""SELECT m.member_code,m.nickname,m.status FROM blocks b
                JOIN memberships me ON me.id=b.blocker_membership_id
                JOIN memberships m ON m.id=b.blocked_membership_id AND m.network_id=b.network_id
                WHERE b.network_id=? AND me.user_id=? AND me.status='active' ORDER BY m.nickname,m.id LIMIT 150""", (user["current_network_id"], user["id"]))] if profile else []
            choices = []
            if pending and pending["action"] == "guide_choose_target":
                for index, member_id in enumerate(json.loads(pending["payload"])["ids"], 1):
                    member = conn.execute("SELECT nickname,member_code,bio FROM memberships WHERE id=? AND network_id=? AND status='active' AND (discoverable=1 OR user_id=?)", (member_id, user["current_network_id"], user["id"])).fetchone()
                    if member:
                        choices.append({**dict(member), "label": member["nickname"], "value": str(index)})
            for network in networks:
                network["current"] = bool(conn.execute("SELECT 1 FROM networks WHERE public_id=? AND id=?", (network["public_id"], user["current_network_id"])).fetchone())
            payload = {"profile": profile, "communication_address": incoming.contact_address if profile or (pending and pending["action"] == "join") else "", "admin_role": admin_role, "requests": requests, "blocked_members": blocked_members, "current_network": user["current_network_id"], "choices": choices if profile else [], "type": "contact-state", "networks": networks, "pending": {"action": pending["action"], "token": pending["token"]} if pending else None, "messages": messages}
            if pending:
                details = json.loads(pending["payload"])
                if pending["action"] in {"guide_nickname", "join"}:
                    network = conn.execute("SELECT name FROM networks WHERE id=?", (details["network_id"],)).fetchone()
                    payload["pending"]["network_name"] = network["name"] if network else ""
                    payload["pending"]["nickname"] = details.get("nickname", "")
                if pending["action"] in {"send_request", "accept_request"}:
                    payload["pending"]["contact"] = details["contact"]
                    if pending["action"] == "send_request":
                        payload["pending"]["reason"] = details["reason"]
                        other = conn.execute("SELECT nickname,member_code FROM memberships WHERE id=? AND network_id=?", (details["target_id"], user["current_network_id"])).fetchone()
                    else:
                        other = conn.execute("SELECT m.nickname,m.member_code FROM contact_requests r JOIN memberships m ON m.id=r.requester_membership_id WHERE r.id=? AND r.network_id=?", (details["request_id"], user["current_network_id"])).fetchone()
                    payload["pending"]["recipient"] = dict(other) if other else None
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        payload["at"] = _now()
        for instance in instances:
            with self.service.db.transaction() as conn:
                receipt = conn.execute("SELECT request_id,ok FROM xdc_receipts WHERE token=?", (instance["token"],)).fetchone()
                directory = conn.execute("SELECT page FROM xdc_directory WHERE token=? AND network_id=?", (instance["token"], user["current_network_id"])).fetchone()
                payload["directory"] = None
                if profile and conn.execute("SELECT 1 FROM memberships m JOIN networks n ON n.id=m.network_id JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND m.network_id=? AND m.status='active' AND n.status='active' AND u.current_network_id=m.network_id", (user["id"], user["current_network_id"])).fetchone():
                    total = conn.execute("SELECT count(*) FROM memberships WHERE network_id=? AND status='active' AND discoverable=1 AND user_id<>?", (user["current_network_id"], user["id"])).fetchone()[0]
                    page = min(directory["page"] if directory else 0, max(0, (total - 1) // 12))
                    if directory and page != directory["page"]:
                        conn.execute("UPDATE xdc_directory SET page=? WHERE token=?", (page, instance["token"],))
                    members = [dict(row) for row in conn.execute("SELECT member_code,nickname,bio FROM memberships WHERE network_id=? AND status='active' AND discoverable=1 AND user_id<>? ORDER BY nickname,member_code LIMIT 13 OFFSET ?", (user["current_network_id"], user["id"], page * 12))]
                    payload["directory"] = {"page": page, "members": members[:12], "has_more": len(members) > 12}
                payload["notifications_enabled"] = bool(instance["notifications_enabled"])
                payload["admin"] = self.admin.snapshot(conn, user, instance["token"], _now())
            payload["receipt"] = dict(receipt) if receipt else None
            # Include acknowledgements even when the business snapshot is unchanged.
            digest = hashlib.sha256(json.dumps({k: v for k, v in payload.items() if k != "at"}, sort_keys=True).encode()).hexdigest()
            identity = (incoming.account_id, instance["message_id"])
            if self._last_sync.get(identity) == digest:
                continue
            # iOS replaces an empty summary with its own app-category label.
            # A non-breaking space stays invisible while preventing that fallback.
            self.rpc.send_webxdc_status_update(incoming.account_id, instance["message_id"], json.dumps({"payload": payload, "summary": "\u00a0"}, ensure_ascii=False), "")
            self._last_sync[identity] = digest

    def deliver_outbox(self, account_id, item):
        """Send text to a registered app; native contact attachments use chat."""
        from deltachat2.types import ChatType

        if item["vcard_contact_id"] is not None:
            return False
        with self.service.db.transaction() as conn:
            instance = conn.execute("""SELECT x.* FROM xdc_instances x JOIN users u ON u.actor_key=x.actor_key
                WHERE x.account_id=? AND x.chat_id=u.chat_id AND u.id=? AND x.notifications_enabled=1""", (account_id, item["recipient_user_id"])).fetchone()
        if not instance:
            raise RuntimeError("小程序尚未注册接收消息")
        chat = self.rpc.get_basic_chat_info(account_id, instance["chat_id"])
        if chat.chat_type != ChatType.SINGLE or chat.is_device_chat or chat.is_self_talk or not chat.is_encrypted:
            raise RuntimeError("小应用私聊未加密")
        if self.require_verified and not self.rpc.get_contact(account_id, item["contact_id"]).is_verified:
            raise RuntimeError("小应用联系人未验证")
        payload = {"type": "contact-notification", "id": str(item["id"]), "body": item["body"], "at": item["created_at"]}
        update = {"payload": payload}
        if item["dedupe_key"].startswith(("request-notify:", "vcard-request-notify:", "exchange:", "vcard-accepted:", "request-rejected:", "recovery-", "admin-action:")):
            update["notify"] = {"*": "好友助手有新消息，点击查看"}
        self.rpc.send_webxdc_status_update(account_id, instance["message_id"], json.dumps(update, ensure_ascii=False), "")
        return True

    def clear_previous_summaries(self, incoming):
        """Clear the iOS fallback on this verified chat's earlier bot apps too."""
        from deltachat2.types import Viewtype

        identity = (incoming.account_id, incoming.chat_id)
        if not incoming.is_private or not incoming.is_encrypted or identity in self._summary_cleanup_done:
            return
        message_ids = self.rpc.get_chat_media(incoming.account_id, incoming.chat_id, Viewtype.WEBXDC, None, None)
        for message_id in message_ids:
            msg = self.rpc.get_message(incoming.account_id, message_id)
            if msg.chat_id != incoming.chat_id or msg.from_id != 1 or msg.file_name != "好友助手.xdc":
                continue
            info = self.rpc.get_webxdc_info(incoming.account_id, message_id)
            if info.name != "好友助手" or not info.is_app_sender or info.summary == "\u00a0":
                continue
            update = {"payload": {"type": "contact-appearance-update"}, "summary": "\u00a0"}
            self.rpc.send_webxdc_status_update(incoming.account_id, message_id, json.dumps(update, ensure_ascii=False), "")
        self._summary_cleanup_done.add(identity)

    def sync_all(self, account_id):
        """Verify each private chat once, consume all instances, publish once."""
        from deltachat2.types import ChatType
        from .service import IncomingMessage
        with self.service.db.transaction() as conn:
            self.service._expire(conn, _now())
            rows = list(conn.execute("SELECT x.*,u.contact_id FROM xdc_instances x JOIN users u ON u.actor_key=x.actor_key WHERE x.account_id=? AND u.chat_id=x.chat_id", (account_id,)))
        verified, changed = {}, {}
        for row in rows:
            try:
                identity = (row["contact_id"], row["chat_id"])
                if identity not in verified:
                    verified[identity] = None
                    chat = self.rpc.get_basic_chat_info(account_id, row["chat_id"])
                    if chat.chat_type != ChatType.SINGLE or chat.is_device_chat or chat.is_self_talk or not chat.is_encrypted:
                        continue
                    contact = self.rpc.get_contact(account_id, row["contact_id"])
                    if self.require_verified and not contact.is_verified:
                        continue
                    address = getattr(contact, "address", "")
                    if self.identity_key and not address:
                        continue
                    actor_key = self.identity_key(address) if self.identity_key else row["actor_key"]
                    incoming = IncomingMessage(account_id, 0, actor_key, row["contact_id"], row["chat_id"], "", True, True, contact_address=address)
                    if self.identity_resolver:
                        incoming = self.identity_resolver(incoming)
                        with self.service.db.transaction() as conn:
                            self.service._ensure_user(conn, incoming, _now())
                    elif actor_key != row["actor_key"]:
                        continue
                    verified[identity] = incoming
                incoming = verified[identity]
                if incoming is None:
                    continue
                with self.service.db.transaction() as conn:
                    refreshed = conn.execute("SELECT * FROM xdc_instances WHERE token=? AND actor_key=? AND account_id=? AND chat_id=?", (row["token"], incoming.actor_key, account_id, incoming.chat_id)).fetchone()
                if not refreshed:
                    continue
                self.consume_updates(refreshed, incoming)
                changed[identity] = incoming
            except Exception as exc:
                logging.getLogger(__name__).warning("小应用同步将重试（%s）", type(exc).__name__)
        for incoming in changed.values():
            try:
                self.clear_previous_summaries(incoming)
            except Exception as exc:
                logging.getLogger(__name__).warning("旧应用外观更新将重试（%s）", type(exc).__name__)
            try:
                self.sync(incoming)
            except Exception as exc:
                logging.getLogger(__name__).warning("小应用状态发布将重试（%s）", type(exc).__name__)

    def consume_updates(self, instance, incoming):
        """Only consume the server-bound instance; never trust selfAddr/payload identity.

        The per-instance capability was delivered in a verified encrypted private
        chat. AES-GCM authenticates commands without assuming that the status API
        exposes each update's sender or encryption metadata (it does not).
        """
        from dataclasses import replace

        from cryptography.exceptions import InvalidTag

        if not incoming.is_private or not incoming.is_encrypted:
            return
        if any(instance[field] != getattr(incoming, field) for field in ("actor_key", "account_id", "chat_id")):
            return
        with self.service.db.transaction() as conn:
            row = conn.execute("SELECT serial FROM xdc_cursors WHERE account_id=? AND message_id=?", (incoming.account_id, instance["message_id"])).fetchone()
            serial = row[0] if row else 0
        updates = json.loads(self.rpc.get_webxdc_status_updates(incoming.account_id, instance["message_id"], serial))
        key = hashlib.sha256(("friend-assistant-v2:" + instance["token"]).encode()).digest()
        for update in updates:
            current = update.get("serial")
            if not isinstance(current, int) or current <= serial:
                continue
            payload = update.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "contact-action-v2":
                try:
                    if not isinstance(payload.get("body"), str) or len(payload["body"]) > 16000:
                        raise ValueError("Invalid envelope")
                    if not isinstance(payload.get("nonce"), str) or len(payload["nonce"]) != 16:
                        raise ValueError("Invalid nonce")
                    nonce = base64.b64decode(payload["nonce"], validate=True)
                    if len(nonce) != 12:
                        raise ValueError("Invalid nonce")
                    raw = AESGCM(key).decrypt(nonce, base64.b64decode(payload["body"], validate=True), b"contact-action-v2")
                    command = json.loads(raw)
                    if not isinstance(command, dict) or command.get("token") != instance["token"]:
                        raise ValueError("Wrong instance")
                except (InvalidTag, ValueError, TypeError, KeyError):
                    command = None
                if command is not None:
                    self.handle(replace(incoming, text=PREFIX + json.dumps(command)))
            # Advancing only after a transaction completes makes restarts replay safe.
            with self.service.db.transaction() as conn:
                conn.execute("INSERT INTO xdc_cursors VALUES(?,?,?) ON CONFLICT(account_id,message_id) DO UPDATE SET serial=excluded.serial", (incoming.account_id, instance["message_id"], current))
            serial = current
