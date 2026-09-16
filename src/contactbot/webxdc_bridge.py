"""Webxdc UI over authenticated, encrypted chat messages, never selfAddr."""
from __future__ import annotations

import json
import secrets
import tempfile
import zipfile
from pathlib import Path

from deltachat2 import MessageData

from .service import UserError, _now

PREFIX = "联系人小程序 "
COMMANDS = frozenset({
    "查找", "申请", "同意申请", "拒绝申请", "撤销申请", "我的申请", "我的资料",
    "编辑昵称", "编辑介绍", "设置联系方式", "公开资料", "隐藏资料", "接收申请",
    "暂停申请", "退出网络", "删除我的数据", "屏蔽", "解除屏蔽", "屏蔽列表",
    "举报", "切换网络", "我的网络", "取消", "管理", "创建网络", "网络概况",
    "成员列表", "查看加入码", "更换加入码", "停用加入", "恢复加入", "移除成员",
    "封禁成员", "解除封禁", "举报列表", "处理举报", "管理记录", "任命管理员",
    "撤销管理员", "停用网络", "恢复网络", "网络列表", "修改网络名称",
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
    def __init__(self, rpc, service):
        self.rpc, self.service = rpc, service
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
            path = Path(folder) / "联系人网络.xdc"
            package(path, token)
            message_id = self.rpc.send_msg(
                incoming.account_id, incoming.chat_id,
                MessageData(file=str(path), text="点击打开联系人网络，按按钮加入网络、找人和处理申请。请仅在本私聊使用。"),
            )
        with self.service.db.transaction() as conn:
            conn.execute("DELETE FROM xdc_instances WHERE actor_key=? AND account_id=? AND chat_id=?", (incoming.actor_key, incoming.account_id, incoming.chat_id))
            conn.execute("INSERT INTO xdc_instances VALUES(?,?,?,?,?,?)", (
                token, incoming.actor_key, incoming.account_id, incoming.chat_id, message_id, _now(),
            ))
        self.sync(incoming)

    def handle(self, incoming):
        if not incoming.text.startswith(PREFIX):
            return False
        if not incoming.is_private or not incoming.is_encrypted:
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
            if conn.execute("SELECT 1 FROM xdc_requests WHERE token=? AND request_id=?", (token, request_id)).fetchone():
                return True
            now = _now()
            user = self.service._ensure_user(conn, incoming, now)
            self.service._expire(conn, now)
            conn.execute("INSERT INTO xdc_requests VALUES(?,?)", (token, request_id))
            key = "xdc:" + request_id
            conn.execute("SAVEPOINT xdc_action")
            try:
                action = request.get("action")
                value = request.get("value", "")
                if not isinstance(value, str):
                    raise UserError("输入内容必须为文本")
                if request.get("network") != user["current_network_id"]:
                    raise UserError("当前网络已变化，请刷新后操作")
                if action == "start":
                    existing = conn.execute("SELECT 1 FROM pending_actions WHERE user_id=? AND expires_at>?", (user["id"], now)).fetchone()
                    if not existing:
                        self.service._begin_guidance(conn, user, now, key)
                elif action == "join":
                    self.service._pending(conn, user["id"], "guide_join_code", {}, now)
                    self.service._reply(conn, user["id"], "请输入管理员提供的加入码。", key)
                elif action == "search":
                    self.service._search(conn, user, value, now, key)
                    self.service._pending(conn, user["id"], "guide_search", {}, now)
                    pending = conn.execute("SELECT * FROM pending_actions WHERE user_id=?", (user["id"],)).fetchone()
                    self.service._guided_reply(conn, user, pending, value, True, incoming.contact_address, now, key + ":choices")
                elif action == "reply":
                    if not isinstance(request.get("step"), str):
                        raise UserError("页面已过期，请刷新")
                    pending = conn.execute("SELECT * FROM pending_actions WHERE user_id=? AND token=? AND expires_at>?", (user["id"], request.get("step"), now)).fetchone()
                    if not pending:
                        raise UserError("页面已过期，请刷新后重新操作")
                    if pending["action"].startswith("guide_"):
                        self.service._guided_reply(conn, user, pending, value, True, incoming.contact_address, now, key)
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
                    if command == "取消":
                        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
                    self.service._dispatch(conn, user, command + (" " + value if value else ""), True, incoming.contact_address, now)
                elif action != "refresh":
                    raise UserError("不支持此操作")
                conn.execute("RELEASE xdc_action")
            except UserError as exc:
                conn.execute("ROLLBACK TO xdc_action")
                conn.execute("RELEASE xdc_action")
                self.service._reply(conn, user["id"], str(exc), key)
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
            membership = conn.execute("SELECT role FROM memberships WHERE user_id=? AND network_id=? AND status='active'", (user["id"], user["current_network_id"])).fetchone()
            admin_role = "system" if user["is_system_admin"] else "network" if membership and membership["role"] == "network_admin" else None
            requests = [dict(row) for row in conn.execute("""SELECT r.public_id,r.status,r.reason,a.nickname AS sender,b.nickname AS recipient,
                CASE WHEN a.user_id=? THEN 'outgoing' ELSE 'incoming' END AS direction
                FROM contact_requests r JOIN memberships a ON a.id=r.requester_membership_id
                JOIN memberships b ON b.id=r.target_membership_id
                WHERE r.network_id=? AND (a.user_id=? OR b.user_id=?) ORDER BY r.id DESC LIMIT 20""",
                (user["id"], user["current_network_id"], user["id"], user["id"]))]
            choices = []
            if pending and pending["action"] == "guide_choose_target":
                for index, member_id in enumerate(json.loads(pending["payload"])["ids"], 1):
                    member = conn.execute("SELECT nickname,member_code FROM memberships WHERE id=? AND network_id=? AND status='active' AND (discoverable=1 OR user_id=?)", (member_id, user["current_network_id"], user["id"])).fetchone()
                    if member:
                        choices.append({"label": member["nickname"] + " · " + member["member_code"], "value": str(index)})
            payload = {"admin_role": admin_role, "requests": requests, "current_network": user["current_network_id"], "choices": choices, "type": "contact-state", "at": _now(), "networks": networks, "pending": {"action": pending["action"], "token": pending["token"]} if pending else None, "messages": messages}
        for instance in instances:
            self.rpc.send_webxdc_status_update(incoming.account_id, instance["message_id"], json.dumps({"payload": payload}, ensure_ascii=False), "联系人网络已更新")
