from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .database import Database


class UserError(Exception):
    pass


@dataclass(frozen=True)
class IncomingMessage:
    account_id: int
    message_id: int
    actor_key: str
    contact_id: int
    chat_id: int
    text: str
    is_private: bool
    is_encrypted: bool
    bootstrap_admin: bool = False
    contact_address: str = ""


HELP = """好友助手｜帮助

直接发送任意消息，可以打开主菜单。

【开始与退出】
菜单：打开主菜单
帮助：查看本指令表
q：退出当前操作

【联系网络】
加入新网络
我的网络
切换网络 <网络编号>
退出网络

【个人资料】
我的资料
编辑昵称 <新昵称>
编辑介绍 <个人介绍>
公开资料｜隐藏资料
接收申请｜暂停申请

【查找与添加好友】
查找 <昵称>
申请 <成员编号> <联系理由>
我的申请
同意申请 <申请编号>
拒绝申请 <申请编号>
撤销申请 <申请编号>

【屏蔽、举报与数据】
屏蔽 <成员编号>
屏蔽列表
解除屏蔽 <成员编号>
举报 <成员编号> <原因>
删除我的数据

【更换账号】
恢复账号：申请恢复原成员编号和历史好友

管理员发送“管理”查看管理员指令。"""

class BotService:
    def __init__(self, database: Database, secret: bytes):
        self.db = database
        self.secret = secret

    def handle(self, message: IncomingMessage) -> None:
        if not message.is_private:
            return
        text = message.text.strip()
        if not text:
            text = "__GUIDE_WELCOME__"
        with self.db.transaction() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM incoming_messages WHERE account_id=? AND message_id=?",
                (message.account_id, message.message_id),
            ).fetchone()
            if duplicate:
                return
            now = _now()
            user = self._ensure_user(conn, message, now)
            conn.execute(
                "INSERT INTO incoming_messages(account_id,message_id,processed_at) VALUES(?,?,?)",
                (message.account_id, message.message_id, now),
            )
            self._expire(conn, now)
            try:
                self._dispatch(conn, user, text, message.is_encrypted, message.contact_address, now)
            except UserError as exc:
                self._reply(conn, user["id"], f"无法完成：{exc}", message.message_id)

    def pending_outbox(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.db.transaction() as conn:
            return list(
                conn.execute(
                    """SELECT o.*,u.chat_id,u.contact_id FROM outbox o
                       JOIN users u ON u.id=o.recipient_user_id
                       WHERE o.status='pending' ORDER BY o.id LIMIT ?""",
                    (limit,),
                )
            )

    def mark_outbox_sent(self, outbox_id: int) -> None:
        with self.db.transaction() as conn:
            item = conn.execute("SELECT dedupe_key FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if not item:
                return
            conn.execute(
                """UPDATE outbox SET status='sent',body='',vcard_contact_id=NULL,
                   sent_at=?,attempts=attempts+1,
                   last_error=NULL WHERE id=?""",
                (_now(), outbox_id),
            )
            if item["dedupe_key"].startswith(("exchange:", "vcard:")):
                request_public_id = item["dedupe_key"].split(":", 2)[1]
                unfinished = conn.execute(
                    """SELECT 1 FROM outbox WHERE
                       (dedupe_key LIKE ? OR dedupe_key LIKE ?) AND status<>'sent'""",
                    (f"exchange:{request_public_id}:%", f"vcard:{request_public_id}:%"),
                ).fetchone()
                if not unfinished:
                    now = _now()
                    conn.execute(
                        """UPDATE contact_requests SET status='completed',completed_at=?,updated_at=?
                           WHERE public_id=? AND status='processing'""",
                        (now, now, request_public_id),
                    )

    def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET status='pending',attempts=attempts+1,last_error=? WHERE id=?",
                (error[:300], outbox_id),
            )

    def cleanup_retention(self) -> dict[str, int]:
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            request_cutoff = _iso(now - timedelta(days=30))
            report_cutoff = _iso(now - timedelta(days=90))
            cleared = conn.execute(
                """UPDATE contact_requests SET reason='',requester_contact='',target_contact=NULL
                   WHERE status IN ('completed','rejected','withdrawn','expired','invalid')
                     AND updated_at<? AND (reason<>'' OR requester_contact<>'')""",
                (request_cutoff,),
            ).rowcount
            reports = conn.execute(
                "DELETE FROM reports WHERE status='resolved' AND resolved_at<?", (report_cutoff,)
            ).rowcount
            audits = conn.execute(
                "DELETE FROM audit_log WHERE created_at<?", (report_cutoff,)
            ).rowcount
            inbox = conn.execute(
                "DELETE FROM incoming_messages WHERE processed_at<?", (report_cutoff,)
            ).rowcount
            outbox = conn.execute(
                "DELETE FROM outbox WHERE status='sent' AND sent_at<?", (request_cutoff,)
            ).rowcount
        return {
            "requests_cleared": cleared,
            "reports_deleted": reports,
            "audits_deleted": audits,
            "inbox_deleted": inbox,
            "outbox_deleted": outbox,
        }

    def seed_network(self, name: str, join_code: str) -> tuple[str, bool]:
        if not 1 <= len(name.strip()) <= 60:
            raise ValueError("网络名称长度应为 1 至 60 个字符")
        if not join_code:
            raise ValueError("初始网络码不能为空")
        with self.db.transaction() as conn:
            existing = self._find_network_by_code(conn, join_code)
            if existing:
                return existing["public_id"], False
            public_id = self._public_id("N", 6)
            now = _now()
            conn.execute(
                """INSERT INTO networks(
                       public_id,name,join_code_hash,join_code_ciphertext,join_code_hint,
                       created_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,NULL,?,?)""",
                (
                    public_id,
                    name.strip(),
                    self._join_hash(join_code),
                    self._encrypt_join_code(join_code),
                    join_code[-4:],
                    now,
                    now,
                ),
            )
            return public_id, True

    def _ensure_user(
        self, conn: sqlite3.Connection, message: IncomingMessage, now: str
    ) -> sqlite3.Row:
        conn.execute(
            """INSERT INTO users(actor_key,contact_id,chat_id,is_system_admin,created_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(actor_key) DO UPDATE SET
                 contact_id=excluded.contact_id,chat_id=excluded.chat_id,updated_at=excluded.updated_at,
                 is_system_admin=max(users.is_system_admin,excluded.is_system_admin)""",
            (
                message.actor_key,
                message.contact_id,
                message.chat_id,
                int(message.bootstrap_admin),
                now,
                now,
            ),
        )
        return conn.execute(
            "SELECT * FROM users WHERE actor_key=?", (message.actor_key,)
        ).fetchone()

    def _dispatch(
        self,
        conn: sqlite3.Connection,
        user: sqlite3.Row,
        text: str,
        encrypted: bool,
        contact_address: str,
        now: str,
    ) -> None:
        command, _, args = text.partition(" ")
        msg_key = secrets.token_hex(8)
        if command == "__GUIDE_WELCOME__":
            self._begin_guidance(conn, user, now, msg_key)
            return
        pending = conn.execute(
            "SELECT * FROM pending_actions WHERE user_id=? AND expires_at>?", (user["id"], now)
        ).fetchone()
        if pending and text.strip().casefold() == "q":
            conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
            self._reply(conn, user["id"], "已退出当前步骤。需要时发送任意消息重新开始。", msg_key)
            return
        if pending and pending["action"] == "guide_next" and text.strip().casefold() in {
            "你好", "您好", "hi", "hello"
        }:
            self._begin_guidance(conn, user, now, msg_key)
            return
        natural_confirm = text.strip().lower() in {
            "同意",
            "确认",
            "是",
            "否",
            "好",
            "可以",
            "取消",
            "yes",
            "no",
            "y",
            "n",
            "确认更换",
        }
        if pending and natural_confirm:
            self._guided_reply(
                conn, user, pending, text, encrypted, contact_address, now, msg_key
            )
            return
        explicit_commands = {
            "帮助", "菜单", "/help", "/start", "取消", "加入", "确认", "我的网络",
            "切换网络", "我的资料", "编辑昵称", "编辑介绍", "设置联系方式", "公开资料",
            "隐藏资料", "接收申请", "暂停申请", "查找", "申请", "我的申请", "同意申请",
            "拒绝申请", "撤销申请", "屏蔽", "解除屏蔽", "屏蔽列表", "举报", "退出网络",
            "删除我的数据", "管理", "创建网络", "网络概况", "成员列表", "查看加入码",
            "更换加入码", "停用加入", "恢复加入", "移除成员", "封禁成员", "解除封禁",
            "举报列表", "处理举报", "管理记录", "任命管理员", "撤销管理员", "停用网络",
            "恢复网络", "网络列表", "修改网络名称", "恢复账号", "恢复申请列表",
            "批准恢复", "拒绝恢复",
        }
        has_membership = conn.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchone()
        if (
            not pending
            and not has_membership
            and (command in {"菜单", "/start"} or command not in explicit_commands)
        ):
            self._begin_guidance(conn, user, now, msg_key)
            return
        if pending and command not in explicit_commands and (
            pending["action"].startswith("guide_")
            or pending["action"] in {"send_request", "accept_request", "rotate_code"}
        ):
            self._guided_reply(
                conn, user, pending, text, encrypted, contact_address, now, msg_key
            )
            return
        if command in {"我的资料", "设置联系方式", "申请", "同意申请"} and not encrypted:
            raise UserError("此操作涉及联系方式，请在加密私聊中操作")
        if command in {"帮助", "/help"}:
            self._reply(conn, user["id"], HELP, msg_key)
        elif command in {"菜单", "/start"}:
            self._begin_guidance(conn, user, now, msg_key)
        elif command == "取消":
            conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
            self._reply(conn, user["id"], "已取消尚未确认的操作。", msg_key)
        elif command == "加入":
            self._start_join(conn, user, args, now, msg_key)
        elif command == "恢复账号":
            self._start_account_recovery(conn, user, now, msg_key)
        elif command == "确认":
            self._confirm(conn, user, args, encrypted, now, msg_key)
        elif command == "我的网络":
            self._my_networks(conn, user, msg_key)
        elif command == "切换网络":
            self._switch_network(conn, user, args, now, msg_key)
        elif command == "我的资料":
            self._my_profile(conn, user, msg_key)
        elif command in {"编辑昵称", "编辑介绍", "设置联系方式"}:
            self._edit_profile(conn, user, command, args, now, msg_key)
        elif command in {"公开资料", "隐藏资料", "接收申请", "暂停申请"}:
            self._set_privacy(conn, user, command, now, msg_key)
        elif command == "查找":
            self._search(conn, user, args, now, msg_key)
        elif command == "申请":
            self._start_request(conn, user, args, now, msg_key)
        elif command == "我的申请":
            self._list_requests(conn, user, msg_key)
        elif command == "同意申请":
            self._start_accept(conn, user, args, now, msg_key)
        elif command == "拒绝申请":
            self._reject(conn, user, args, now, msg_key)
        elif command == "撤销申请":
            self._withdraw(conn, user, args, now, msg_key)
        elif command in {"屏蔽", "解除屏蔽"}:
            self._block(conn, user, args, command == "屏蔽", now, msg_key)
        elif command == "屏蔽列表":
            self._block_list(conn, user, msg_key)
        elif command == "举报":
            self._report(conn, user, args, now, msg_key)
        elif command == "退出网络":
            self._start_leave(conn, user, now, msg_key)
        elif command == "删除我的数据":
            self._start_delete(conn, user, now, msg_key)
        elif command == "管理":
            self._admin_help(conn, user, msg_key)
        elif command in {
            "创建网络",
            "网络概况",
            "成员列表",
            "查看加入码",
            "更换加入码",
            "停用加入",
            "恢复加入",
            "移除成员",
            "封禁成员",
            "解除封禁",
            "举报列表",
            "处理举报",
            "管理记录",
            "任命管理员",
            "撤销管理员",
            "停用网络",
            "恢复网络",
            "网络列表",
            "修改网络名称",
            "恢复申请列表",
            "批准恢复",
            "拒绝恢复",
        }:
            self._admin_command(conn, user, command, args, encrypted, now, msg_key)
        else:
            self._begin_guidance(conn, user, now, msg_key)

    def _start_account_recovery(self, conn, user, now, key) -> None:
        self._pending(conn, user["id"], "guide_recover_identity", {}, now)
        self._reply(
            conn,
            user["id"],
            "请输入原来的成员编号。\n"
            "如果机器人提示编号重复，请输入“网络编号 成员编号”。\n"
            "申请需由管理员核实并批准；输入 q 退出。",
            key,
        )

    def _submit_account_recovery(self, conn, user, answer, now, key) -> None:
        parts = answer.split()
        if len(parts) == 1:
            rows = conn.execute(
                """SELECT m.*,n.public_id network_public_id,n.name network_name
                   FROM memberships m JOIN networks n ON n.id=m.network_id
                   WHERE m.member_code=? AND m.status='active' AND n.status='active'""",
                (parts[0].upper(),),
            ).fetchall()
        elif len(parts) == 2:
            rows = conn.execute(
                """SELECT m.*,n.public_id network_public_id,n.name network_name
                   FROM memberships m JOIN networks n ON n.id=m.network_id
                   WHERE n.public_id=? AND m.member_code=?
                     AND m.status='active' AND n.status='active'""",
                (parts[0].upper(), parts[1].upper()),
            ).fetchall()
        else:
            raise UserError("请输入成员编号，或输入“网络编号 成员编号”")
        if not rows:
            raise UserError("没有找到可恢复的成员编号，请检查后重试")
        if len(rows) > 1:
            raise UserError("该编号存在于多个网络，请输入“网络编号 成员编号”")
        membership = rows[0]
        if membership["user_id"] == user["id"]:
            raise UserError("该成员编号已经属于当前账号")
        if conn.execute(
            """SELECT 1 FROM memberships WHERE network_id=? AND user_id=? AND status='active'""",
            (membership["network_id"], user["id"]),
        ).fetchone():
            raise UserError("当前账号已经加入该网络，不能覆盖现有成员编号")
        existing = conn.execute(
            """SELECT public_id,requester_user_id FROM account_recoveries
               WHERE membership_id=? AND status='pending' AND expires_at>?""",
            (membership["id"], now),
        ).fetchone()
        if existing:
            if existing["requester_user_id"] != user["id"]:
                raise UserError("该成员编号已有待审核恢复申请")
            public_id = existing["public_id"]
        else:
            public_id = self._public_id("V", 8)
            conn.execute(
                """INSERT INTO account_recoveries(
                       public_id,membership_id,requester_user_id,status,expires_at,created_at
                   ) VALUES(?,?,?,'pending',?,?)""",
                (
                    public_id,
                    membership["id"],
                    user["id"],
                    _iso(_parse(now) + timedelta(days=2)),
                    now,
                ),
            )
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
        self._reply(
            conn,
            user["id"],
            f"恢复申请 {public_id} 已提交，请联系网络管理员核实身份。申请 48 小时后过期。",
            key,
        )
        admins = conn.execute(
            """SELECT DISTINCT u.id FROM users u
               LEFT JOIN memberships m ON m.user_id=u.id AND m.network_id=?
               WHERE u.is_system_admin=1
                  OR (m.role='network_admin' AND m.status='active')""",
            (membership["network_id"],),
        ).fetchall()
        for admin in admins:
            if admin["id"] == user["id"]:
                continue
            self._reply(
                conn,
                admin["id"],
                f"收到账号恢复申请 {public_id}\n网络：{membership['network_name']}"
                f"（{membership['network_public_id']}）\n成员：{membership['nickname']}"
                f"（{membership['member_code']}）\n请在线下核实身份后发送：批准恢复 {public_id}\n"
                f"拒绝请发送：拒绝恢复 {public_id}",
                f"recovery-notify:{public_id}:{admin['id']}",
            )

    def _start_join(
        self, conn: sqlite3.Connection, user: sqlite3.Row, args: str, now: str, key: Any
    ) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            raise UserError("格式：加入 <加入码> <昵称>")
        raw_code, nickname = parts
        self._validate_nickname(nickname)
        if not self._rate_allowed(conn, user["actor_key"], "join_attempt", 10, 60, now):
            raise UserError("加入尝试过多，请一小时后再试")
        network = self._find_network_by_code(conn, raw_code)
        if not network or not network["join_enabled"] or network["status"] != "active":
            raise UserError("加入码无效或该网络暂停加入")
        if conn.execute(
            "SELECT 1 FROM bans WHERE network_id=? AND actor_key=?",
            (network["id"], user["actor_key"]),
        ).fetchone():
            raise UserError("此账号不能加入该网络")
        membership = conn.execute(
            "SELECT status FROM memberships WHERE network_id=? AND user_id=?",
            (network["id"], user["id"]),
        ).fetchone()
        if membership and membership["status"] == "active":
            raise UserError("你已经是该网络成员")
        token = self._pending(
            conn,
            user["id"],
            "join",
            {
                "network_id": network["id"],
                "nickname": nickname,
                "join_code_hash": network["join_code_hash"],
            },
            now,
        )
        self._reply(
            conn,
            user["id"],
            f"准备加入“{network['name']}”，昵称为“{nickname}”。\n"
            f"请在 10 分钟内发送：确认 {token}\n输入 q 退出。",
            key,
        )

    def _begin_guidance(self, conn, user, now, key, first_answer="") -> None:
        membership = conn.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchone()
        if membership:
            self._pending(conn, user["id"], "guide_next", {}, now)
            self._reply(
                conn,
                user["id"],
                "你好，我来带你认识网络中的成员。\n\n"
                "🔎 发送“查找好友”按昵称查找\n"
                "👤 发送“查看资料”查看我的资料\n"
                "🌐 发送“加入新网络”加入新网络\n"
                "📖 发送“帮助”查看完整指令\n"
                "🚪 输入 q 退出。",
                key,
            )
            return
        self._pending(conn, user["id"], "guide_join_code", {}, now)
        self._reply(
            conn,
            user["id"],
            "你好，我是好友助手。我会一步步带你加入联系人网络并添加好友。\n"
            "请直接回复管理员给你的网络加入码。\n"
            "如果更换了 Delta Chat 账号，请发送“恢复账号”。\n输入 q 退出。",
            key,
        )
        if first_answer:
            pending = conn.execute(
                "SELECT * FROM pending_actions WHERE user_id=?", (user["id"],)
            ).fetchone()
            self._guided_reply(conn, user, pending, first_answer, True, "", now, f"{key}:first")

    def _guided_reply(self, conn, user, pending, text, encrypted, contact_address, now, key):
        action = pending["action"]
        payload = json.loads(pending["payload"])
        answer = text.strip()
        normalized = answer.casefold()
        yes = normalized in {"同意", "确认", "确认更换", "是", "好", "可以", "yes", "y"}
        no = normalized in {"不同意", "拒绝", "否", "取消", "no", "n"}

        if action == "guide_recover_identity":
            self._submit_account_recovery(conn, user, answer, now, key)
            return

        if action == "guide_join_code":
            if not self._rate_allowed(conn, user["actor_key"], "join_attempt", 10, 60, now):
                raise UserError("加入尝试过多，请一小时后再试")
            network = self._find_network_by_code(conn, answer)
            if not network or not network["join_enabled"] or network["status"] != "active":
                self._reply(
                    conn, user["id"],
                    "这个加入码无效或已停用，请检查后重新回复。\n输入 q 退出。", key
                )
                return
            if conn.execute(
                "SELECT 1 FROM bans WHERE network_id=? AND actor_key=?",
                (network["id"], user["actor_key"]),
            ).fetchone():
                raise UserError("此账号不能加入该网络")
            existing = conn.execute(
                """SELECT 1 FROM memberships WHERE network_id=? AND user_id=?
                   AND status='active'""",
                (network["id"], user["id"]),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
                    (network["id"], now, user["id"]),
                )
                self._reply(
                    conn, user["id"],
                    f"你已经加入“{network['name']}”，现已切换到该网络。", key
                )
                self._guide_next(conn, user, now, f"{key}:next")
                return
            self._pending(
                conn,
                user["id"],
                "guide_nickname",
                {"network_id": network["id"], "join_code_hash": network["join_code_hash"]},
                now,
            )
            self._reply(
                conn, user["id"],
                f"已找到“{network['name']}”。\n你希望在网络中显示什么昵称？\n输入 q 退出。",
                key,
            )
            return

        if action == "guide_nickname":
            self._validate_nickname(answer)
            payload["nickname"] = answer
            self._confirm_join(conn, user, payload, now, key)
            self._guide_next(conn, user, now, f"{key}:next")
            return

        if action == "guide_privacy":
            # Compatibility for users left in the removed privacy-consent step.
            self._confirm_join(conn, user, payload, now, key)
            self._guide_next(conn, user, now, f"{key}:next")
            return

        if action == "guide_share_link":
            link = self._validate_share_link(answer, contact_address)
            membership, _ = self._current_membership(conn, user)
            conn.execute(
                """UPDATE memberships SET share_link_ciphertext=?,share_version=share_version+1,
                   updated_at=? WHERE id=?""",
                (self._encrypt_share_link(link), now, membership["id"]),
            )
            self._reply(conn, user["id"], "好友添加链接已绑定。你的昵称现在可以被网络成员查找。", key)
            self._guide_next(conn, user, now, f"{key}:next")
            return

        if action == "guide_contact_choice":
            if answer == "使用当前地址":
                if not encrypted or not self._valid_contact(contact_address):
                    self._pending(conn, user["id"], "guide_contact_manual", {}, now)
                    self._reply(
                        conn, user["id"],
                        "无法自动读取可分享地址，请直接回复你的 Delta Chat 地址。\n输入 q 退出。",
                        key,
                    )
                    return
                self._set_guided_contact(conn, user, contact_address, now)
                self._guide_privacy_choice(conn, user, now, key)
            elif answer == "手动填写地址":
                self._pending(conn, user["id"], "guide_contact_manual", {}, now)
                self._reply(
                    conn, user["id"], "请直接回复要分享的 Delta Chat 地址。\n输入 q 退出。", key
                )
            elif answer == "稍后设置":
                self._guide_privacy_choice(conn, user, now, key)
            else:
                raise UserError("请回复“使用当前地址”“手动填写地址”或“稍后设置”；输入 q 退出")
            return

        if action == "guide_contact_manual":
            if not encrypted:
                raise UserError("请在加密私聊中设置联系方式")
            if not self._valid_contact(answer):
                raise UserError("地址格式不正确，请重新回复，例如 name@example.org")
            self._set_guided_contact(conn, user, answer, now)
            self._guide_privacy_choice(conn, user, now, key)
            return

        if action == "guide_privacy_choice":
            membership, _ = self._current_membership(conn, user)
            choices = {
                "公开并接收申请": (1, 1),
                "隐藏但接收申请": (0, 1),
                "隐藏并暂停申请": (0, 0),
            }
            if answer not in choices:
                raise UserError("请回复页面中显示的文字选项；输入 q 退出")
            discoverable, accepts = choices[answer]
            conn.execute(
                "UPDATE memberships SET discoverable=?,accepts_requests=?,updated_at=? WHERE id=?",
                (discoverable, accepts, now, membership["id"]),
            )
            self._guide_next(conn, user, now, key)
            return

        if action == "guide_next":
            if answer == "查找好友":
                self._pending(conn, user["id"], "guide_search", {}, now)
                self._reply(
                    conn, user["id"], "请回复对方的昵称。\n输入 q 退出。", key
                )
            elif answer == "查看资料":
                self._guide_profile(conn, user, now, key)
            elif answer in {"加入新网络", "加入其他网络"}:
                self._pending(conn, user["id"], "guide_join_code", {}, now)
                self._reply(
                    conn, user["id"],
                    "请回复要加入的新网络加入码。\n输入 q 退出。", key
                )
            else:
                self._guide_next(conn, user, now, key)
            return

        if action == "guide_profile":
            if answer == "修改昵称":
                self._pending(conn, user["id"], "guide_edit_nickname", {}, now)
                self._reply(conn, user["id"], "请回复新的昵称。\n输入 q 退出。", key)
            elif answer == "修改介绍":
                self._pending(conn, user["id"], "guide_edit_bio", {}, now)
                self._reply(conn, user["id"], "请回复新的个人介绍。\n输入 q 退出。", key)
            elif answer == "返回菜单":
                self._guide_next(conn, user, now, key)
            else:
                self._guide_profile(conn, user, now, key)
            return

        if action == "guide_edit_nickname":
            self._edit_profile(conn, user, "编辑昵称", answer, now, key)
            self._guide_profile(conn, user, now, f"{key}:profile")
            return

        if action == "guide_edit_bio":
            self._edit_profile(conn, user, "编辑介绍", answer, now, key)
            self._guide_profile(conn, user, now, f"{key}:profile")
            return

        if action in {"guide_search", "guide_member_code"}:
            requester, network = self._current_membership(conn, user)
            if network["status"] != "active":
                raise UserError("当前网络已停用")
            if not self._rate_allowed(conn, user["actor_key"], "search", 30, 60, now):
                raise UserError("查找次数过多，请稍后再试")
            escaped = answer[:64].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                """SELECT id,member_code,nickname,bio FROM memberships
                   WHERE network_id=? AND status='active' AND (discoverable=1 OR id=?)
                     AND (member_code=? OR nickname LIKE ? ESCAPE '\\')
                   ORDER BY CASE WHEN member_code=? THEN 0 ELSE 1 END,nickname LIMIT 5""",
                (network["id"], requester["id"], answer.upper(), f"%{escaped}%", answer.upper()),
            ).fetchall()
            if not rows:
                self._reply(
                    conn, user["id"],
                    "没有找到可联系的公开成员，请重新回复昵称或成员编号。\n输入 q 退出。",
                    key,
                )
                return
            self._pending(
                conn, user["id"], "guide_choose_target", {"ids": [r["id"] for r in rows]}, now
            )
            result = "\n".join(
                f"【{index}】{r['nickname']}（{r['member_code']}）"
                f"{'｜本人' if r['id'] == requester['id'] else ''}｜{r['bio'] or '无介绍'}"
                for index, r in enumerate(rows, 1)
            )
            self._reply(
                conn, user["id"],
                f"找到以下成员：\n{result}\n\n回复数字选择联系人并直接发送名片。\n输入 q 退出。",
                key,
            )
            return

        if action == "guide_choose_target":
            if not answer.isdigit() or not 1 <= int(answer) <= len(payload["ids"]):
                conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
                self._guide_next(conn, user, now, key)
                return
            requester, _ = self._current_membership(conn, user)
            target = conn.execute(
                """SELECT * FROM memberships WHERE id=? AND network_id=?
                   AND status='active' AND discoverable=1""",
                (payload["ids"][int(answer) - 1], requester["network_id"]),
            ).fetchone()
            if not target:
                conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
                self._guide_next(conn, user, now, key)
                return
            target_id = target["id"]
            if target_id == requester["id"]:
                self._my_profile(conn, user, key)
                self._guide_next(conn, user, now, f"{key}:next")
                return
            self._confirm_vcard_request(
                conn, user, {"target_id": target_id}, now, key, direct=True
            )
            return

        if action == "guide_reason":
            target = conn.execute("SELECT member_code FROM memberships WHERE id=?", (payload["target_id"],)).fetchone()
            if not target:
                raise UserError("目标成员已失效")
            self._start_request(conn, user, f"{target['member_code']} {answer}", now, key, guided=True)
            return

        if action in {"send_request", "send_vcard_request", "accept_request", "rotate_code"}:
            if no:
                conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
                self._reply(conn, user["id"], "已取消。", key)
                return
            if not yes:
                raise UserError("请回复“确认”或“取消”；输入 q 退出")
            handlers = {
                "send_request": self._confirm_request,
                "send_vcard_request": self._confirm_vcard_request,
                "accept_request": self._confirm_accept,
                "rotate_code": self._confirm_rotate_code,
            }
            handlers[action](conn, user, payload, now, key)
            conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
            return

        if action == "guide_incoming_request":
            request_id = payload["request_public_id"]
            if answer == "同意":
                req = conn.execute(
                    "SELECT request_kind FROM contact_requests WHERE public_id=?", (request_id,)
                ).fetchone()
                if req and req["request_kind"] == "vcard":
                    self._accept_vcard_request(conn, user, request_id, now, key)
                else:
                    membership, _ = self._current_membership(conn, user)
                    if membership["share_contact"]:
                        self._start_accept(conn, user, request_id, now, key, guided=True)
                    else:
                        self._pending(
                            conn,
                            user["id"],
                            "guide_accept_contact",
                            {"request_public_id": request_id},
                            now,
                        )
                        self._reply(
                            conn, user["id"], "请先直接回复你愿意分享的 Delta Chat 地址。", key
                        )
            elif answer == "拒绝":
                self._reject(conn, user, request_id, now, key)
                self._guide_next(conn, user, now, f"{key}:next")
            elif answer == "屏蔽":
                req = conn.execute("SELECT requester_membership_id FROM contact_requests WHERE public_id=?", (request_id,)).fetchone()
                target, _ = self._current_membership(conn, user)
                if req:
                    requester = conn.execute("SELECT member_code FROM memberships WHERE id=?", (req[0],)).fetchone()
                    if requester:
                        self._block(conn, user, requester[0], True, now, key)
                        conn.execute("UPDATE contact_requests SET status='rejected',updated_at=? WHERE public_id=?", (now, request_id))
                        self._guide_next(conn, user, now, f"{key}:next")
            else:
                raise UserError("请回复“同意”“拒绝”或“屏蔽”；输入 q 退出")
            return

        if action == "guide_accept_contact":
            if not encrypted or not self._valid_contact(answer):
                raise UserError("请在加密私聊中回复有效的 Delta Chat 地址")
            self._set_guided_contact(conn, user, answer, now)
            self._start_accept(conn, user, payload["request_public_id"], now, key, guided=True)
            return

        if action == "guide_admin_create_code":
            self._create_network_with_code(conn, user, payload["name"], answer, now, key)
            return

        if action == "guide_admin_rotate_code":
            if not answer:
                raise UserError("加入码不能为空")
            self._pending(
                conn, user["id"], "rotate_code", {"network_id": payload["network_id"], "code": answer}, now
            )
            self._reply(
                conn, user["id"],
                "旧加入码会立即失效，现有成员不受影响。\n回复“确认更换”继续，回复“取消”放弃。\n输入 q 退出。",
                key,
            )
            return

        raise UserError("当前引导已失效，请重新发送任意消息开始")

    def _set_guided_contact(self, conn, user, address, now):
        membership, _ = self._current_membership(conn, user)
        conn.execute(
            "UPDATE memberships SET share_contact=?,share_version=share_version+1,updated_at=? WHERE id=?",
            (address.strip(), now, membership["id"]),
        )

    def _guide_privacy_choice(self, conn, user, now, key):
        self._pending(conn, user["id"], "guide_privacy_choice", {}, now)
        self._reply(
            conn,
            user["id"],
            "请选择资料和申请权限：\n\n"
            "发送“公开并接收申请”\n"
            "发送“隐藏但接收申请”\n"
            "发送“隐藏并暂停申请”\n"
            "输入 q 退出。",
            key,
        )

    def _guide_next(self, conn, user, now, key):
        membership, _ = self._current_membership(conn, user)
        incoming = conn.execute(
            """SELECT r.public_id,a.nickname,a.member_code FROM contact_requests r
               JOIN memberships a ON a.id=r.requester_membership_id
               WHERE r.target_membership_id=? AND r.status='pending_target'
                 AND r.expires_at>? ORDER BY r.id LIMIT 1""",
            (membership["id"], now),
        ).fetchone()
        if incoming:
            self._pending(
                conn,
                user["id"],
                "guide_incoming_request",
                {"request_public_id": incoming["public_id"]},
                now,
            )
            self._reply(
                conn,
                user["id"],
                f"收到 {incoming['nickname']}（{incoming['member_code']}）的好友申请。\n\n"
                "发送“同意”接收对方名片\n发送“拒绝”拒绝申请\n"
                "发送“屏蔽”拒绝并屏蔽对方\n输入 q 退出。",
                key,
            )
            return
        self._pending(conn, user["id"], "guide_next", {}, now)
        self._reply(
            conn,
            user["id"],
            "设置完成。接下来选择：\n\n"
            "🔎 发送“查找好友”按昵称查找\n"
            "👤 发送“查看资料”查看我的资料\n"
            "🌐 发送“加入新网络”加入新网络\n"
            "📖 发送“帮助”查看完整指令\n"
            "🚪 输入 q 退出。",
            key,
        )

    def _guide_profile(self, conn, user, now, key):
        self._my_profile(conn, user, f"{key}:details")
        self._pending(conn, user["id"], "guide_profile", {}, now)
        self._reply(
            conn,
            user["id"],
            "资料操作：\n发送“修改昵称”\n发送“修改介绍”\n"
            "发送“返回菜单”\n输入 q 退出。",
            key,
        )

    def _confirm(
        self,
        conn: sqlite3.Connection,
        user: sqlite3.Row,
        token: str,
        encrypted: bool,
        now: str,
        key: Any,
    ) -> None:
        if not token:
            raise UserError("请提供确认码")
        pending = conn.execute(
            "SELECT * FROM pending_actions WHERE token=? AND user_id=? AND expires_at>?",
            (token.upper(), user["id"], now),
        ).fetchone()
        if not pending:
            raise UserError("确认码无效或已过期")
        payload = json.loads(pending["payload"])
        action = pending["action"]
        if action in {"accept_request", "rotate_code"} and not encrypted:
            raise UserError("此操作会披露敏感信息，请在加密私聊中重试")
        handlers = {
            "join": self._confirm_join,
            "send_request": self._confirm_request,
            "accept_request": self._confirm_accept,
            "leave": self._confirm_leave,
            "delete": self._confirm_delete,
            "rotate_code": self._confirm_rotate_code,
            "remove_member": self._confirm_remove_member,
            "ban_member": self._confirm_ban_member,
            "disable_network": self._confirm_disable_network,
        }
        handler = handlers.get(action)
        if not handler:
            raise UserError("不支持的确认操作")
        handler(conn, user, payload, now, key)
        conn.execute("DELETE FROM pending_actions WHERE token=?", (pending["token"],))

    def _confirm_join(self, conn, user, payload, now, key) -> None:
        network = conn.execute(
            "SELECT * FROM networks WHERE id=?", (payload["network_id"],)
        ).fetchone()
        if not network or network["status"] != "active" or not network["join_enabled"]:
            raise UserError("该网络目前不能加入")
        if not hmac.compare_digest(network["join_code_hash"], payload["join_code_hash"]):
            raise UserError("加入码已更换，请使用新加入码重新加入")
        if conn.execute(
            "SELECT 1 FROM bans WHERE network_id=? AND actor_key=?",
            (network["id"], user["actor_key"]),
        ).fetchone():
            raise UserError("此账号不能加入该网络")
        existing = conn.execute(
            "SELECT * FROM memberships WHERE network_id=? AND user_id=?",
            (network["id"], user["id"]),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE memberships SET nickname=?,status='active',discoverable=1,
                   accepts_requests=1,share_link_ciphertext=NULL,updated_at=? WHERE id=?""",
                (payload["nickname"], now, existing["id"]),
            )
            member_code = existing["member_code"]
        else:
            member_code = self._member_code(conn, network["id"])
            conn.execute(
                """INSERT INTO memberships(
                       network_id,user_id,member_code,nickname,discoverable,accepts_requests,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,1,1,?,?)""",
                (network["id"], user["id"], member_code, payload["nickname"], now, now),
            )
        conn.execute(
            "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
            (network["id"], now, user["id"]),
        )
        self._reply(
            conn,
            user["id"],
            f"已加入“{network['name']}”，成员编号：{member_code}。\n"
            "昵称默认可被同一网络成员查找；联系人名片会在对方同意申请后自动生成。",
            key,
        )

    def _my_networks(self, conn, user, key) -> None:
        # Repair a stale current-network pointer before rendering the list. This can happen
        # when a system administrator creates a network without joining it as a member.
        self._current_membership(conn, user)
        rows = conn.execute(
            """SELECT n.public_id,n.name,m.member_code,n.id=u.current_network_id AS current
               FROM memberships m JOIN networks n ON n.id=m.network_id JOIN users u ON u.id=m.user_id
               WHERE m.user_id=? AND m.status='active' ORDER BY n.name""",
            (user["id"],),
        ).fetchall()
        if not rows:
            raise UserError("你还没有加入任何网络")
        lines = ["你的网络："] + [
            f"{'*' if r['current'] else '-'} {r['public_id']}｜{r['name']}｜成员 {r['member_code']}"
            for r in rows
        ]
        self._reply(conn, user["id"], "\n".join(lines), key)

    def _switch_network(self, conn, user, public_id, now, key) -> None:
        if not public_id.strip():
            memberships = conn.execute(
                """SELECT n.public_id,n.name FROM networks n
                   JOIN memberships m ON m.network_id=n.id
                   WHERE m.user_id=? AND m.status='active' ORDER BY n.name""",
                (user["id"],),
            ).fetchall()
            if len(memberships) == 1:
                public_id = memberships[0]["public_id"]
            elif not memberships:
                raise UserError("你还没有加入任何网络")
            else:
                choices = "、".join(
                    f"{row['name']}（{row['public_id']}）" for row in memberships
                )
                raise UserError(f"请发送“切换网络 <网络编号>”。可选：{choices}")
        row = conn.execute(
            """SELECT n.id,n.name FROM networks n JOIN memberships m ON m.network_id=n.id
               WHERE n.public_id=? AND m.user_id=? AND m.status='active'""",
            (public_id.upper(), user["id"]),
        ).fetchone()
        if not row and user["is_system_admin"]:
            row = conn.execute(
                "SELECT id,name FROM networks WHERE public_id=?", (public_id.upper(),)
            ).fetchone()
        if not row:
            raise UserError("未找到你已加入的该网络")
        conn.execute(
            "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
            (row["id"], now, user["id"]),
        )
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
        self._reply(conn, user["id"], f"当前网络已切换为“{row['name']}”。", key)

    def _my_profile(self, conn, user, key) -> None:
        m, network = self._current_membership(conn, user)
        text = (
            f"网络：{network['name']}（{network['public_id']}）\n成员编号：{m['member_code']}\n"
            f"昵称：{m['nickname']}\n介绍：{m['bio'] or '未填写'}\n联系人名片：自动生成\n"
            f"允许被查找：{'是' if m['discoverable'] else '否'}\n"
            f"接收申请：{'是' if m['accepts_requests'] else '否'}"
        )
        self._reply(conn, user["id"], text, key)

    def _edit_profile(self, conn, user, command, value, now, key) -> None:
        m, _ = self._current_membership(conn, user)
        if command == "编辑昵称":
            self._validate_nickname(value)
            field, new_value = "nickname", value
        elif command == "编辑介绍":
            if len(value) > 160:
                raise UserError("介绍最多 160 个字符")
            field, new_value = "bio", value
        else:
            if not self._valid_contact(value):
                raise UserError("请输入有效的 Delta Chat 地址，例如 name@example.org")
            field, new_value = "share_contact", value.strip()
        if field == "share_contact":
            conn.execute(
                "UPDATE memberships SET share_contact=?,share_version=share_version+1,updated_at=? WHERE id=?",
                (new_value, now, m["id"]),
            )
        else:
            conn.execute(
                f"UPDATE memberships SET {field}=?,updated_at=? WHERE id=?",
                (new_value, now, m["id"]),
            )
        self._reply(conn, user["id"], "资料已更新。", key)

    def _set_privacy(self, conn, user, command, now, key) -> None:
        m, _ = self._current_membership(conn, user)
        field = "discoverable" if command in {"公开资料", "隐藏资料"} else "accepts_requests"
        enabled = command in {"公开资料", "接收申请"}
        conn.execute(
            f"UPDATE memberships SET {field}=?,updated_at=? WHERE id=?",
            (int(enabled), now, m["id"]),
        )
        self._reply(conn, user["id"], f"已设置：{command}。", key)

    def _search(self, conn, user, query, now, key) -> None:
        if not query:
            raise UserError("格式：查找 <昵称或成员编号>")
        m, network = self._current_membership(conn, user)
        if network["status"] != "active":
            raise UserError("当前网络已停用")
        if not self._rate_allowed(conn, user["actor_key"], "search", 30, 60, now):
            raise UserError("查找次数过多，请稍后再试")
        escaped = query[:64].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = f"%{escaped}%"
        rows = conn.execute(
            """SELECT id,member_code,nickname,bio FROM memberships
               WHERE network_id=? AND status='active' AND (discoverable=1 OR id=?)
                 AND (member_code=? OR nickname LIKE ? ESCAPE '\\')
               ORDER BY CASE WHEN member_code=? THEN 0 ELSE 1 END,nickname LIMIT 10""",
            (network["id"], m["id"], query.upper(), q, query.upper()),
        ).fetchall()
        text = (
            "没有找到符合条件的公开成员。"
            if not rows
            else "查找结果：\n"
            + "\n".join(
                f"{r['member_code']}｜{r['nickname']}"
                f"{'｜本人' if r['id'] == m['id'] else ''}｜{r['bio'] or '无介绍'}"
                for r in rows
            )
        )
        self._reply(conn, user["id"], text, key)

    def _confirm_vcard_request(self, conn, user, payload, now, key, direct=False) -> None:
        requester, network = self._current_membership(conn, user)
        target = conn.execute(
            """SELECT * FROM memberships WHERE id=? AND network_id=?
               AND status='active' AND discoverable=1""",
            (payload["target_id"], network["id"]),
        ).fetchone()
        if not target or target["id"] == requester["id"]:
            raise UserError("目标成员已失效，请重新查找")
        if network["status"] != "active" or not target["accepts_requests"]:
            raise UserError("该成员当前暂停接收好友申请")
        if self._is_blocked(conn, network["id"], requester["id"], target["id"]):
            raise UserError("当前无法向该成员发送好友申请")
        if not self._rate_allowed(
            conn, user["actor_key"], "vcard_request", 5, 24 * 60, now, record=False
        ):
            raise UserError("今天发送好友申请的次数已达上限")
        cutoff = _iso(_parse(now) - timedelta(days=7))
        refused = conn.execute(
            """SELECT 1 FROM contact_requests WHERE requester_membership_id=?
               AND target_membership_id=? AND status='rejected' AND updated_at>?""",
            (requester["id"], target["id"], cutoff),
        ).fetchone()
        if refused:
            raise UserError("距离上次被拒绝不足 7 天")
        public_id = self._public_id("R", 8)
        try:
            conn.execute(
                """INSERT INTO contact_requests(
                       public_id,network_id,requester_membership_id,target_membership_id,
                       reason,requester_contact,requester_share_version,request_kind,status,
                       expires_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'',0,'vcard','pending_target',?,?,?)""",
                (
                    public_id,
                    network["id"],
                    requester["id"],
                    target["id"],
                    "",
                    _iso(_parse(now) + timedelta(days=7)),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise UserError("你们之间已有一条待处理申请") from exc
        conn.execute(
            "INSERT INTO rate_events(actor_key,category,created_at) VALUES(?,?,?)",
            (user["actor_key"], "vcard_request", now),
        )
        target_user_id = conn.execute(
            "SELECT user_id FROM memberships WHERE id=?", (target["id"],)
        ).fetchone()[0]
        if direct:
            conn.execute(
                "UPDATE contact_requests SET status='processing',updated_at=? WHERE public_id=?",
                (now, public_id),
            )
            self._reply(
                conn,
                user["id"],
                f"你的联系人名片已发送给 {target['nickname']}（{target['member_code']}）。",
                key,
            )
            self._reply(
                conn,
                target_user_id,
                f"{requester['nickname']}（{requester['member_code']}）向你发送了联系人名片，"
                "点击附件即可添加。",
                f"vcard:{public_id}:target",
                sensitive=True,
                vcard_contact_id=user["contact_id"],
            )
            self._guide_next(conn, user, now, f"{key}:next")
            return
        target_pending = conn.execute(
            "SELECT action FROM pending_actions WHERE user_id=? AND expires_at>?",
            (target_user_id, now),
        ).fetchone()
        can_prompt = not target_pending or target_pending["action"] == "guide_next"
        if can_prompt:
            self._pending(
                conn,
                target_user_id,
                "guide_incoming_request",
                {"request_public_id": public_id},
                now,
            )
        prompt = (
            "\n\n发送“同意”接收名片，发送“拒绝”拒绝申请，"
            "发送“屏蔽”拒绝并屏蔽对方。\n输入 q 退出。"
            if can_prompt
            else "\n\n你当前的操作完成后，机器人会继续引导你处理这条申请。"
        )
        self._reply(conn, user["id"], f"好友申请 {public_id} 已发送，有效期 7 天。", key)
        self._reply(
            conn,
            target_user_id,
            f"收到好友申请 {public_id}\n来自：{requester['nickname']}"
            f"（{requester['member_code']}）{prompt}",
            f"vcard-request-notify:{public_id}",
        )
        self._guide_next(conn, user, now, f"{key}:next")

    def _accept_vcard_request(self, conn, user, public_id, now, key) -> None:
        target, network = self._current_membership(conn, user)
        req = conn.execute(
            """SELECT * FROM contact_requests WHERE public_id=?
               AND target_membership_id=? AND request_kind='vcard'
               AND status='pending_target'""",
            (public_id.upper(), target["id"]),
        ).fetchone()
        if not req or req["expires_at"] <= now:
            raise UserError("好友申请已失效")
        requester = conn.execute(
            "SELECT * FROM memberships WHERE id=?", (req["requester_membership_id"],)
        ).fetchone()
        if (
            not requester
            or requester["status"] != "active"
            or network["status"] != "active"
            or self._is_blocked(conn, network["id"], requester["id"], target["id"])
        ):
            raise UserError("申请双方当前无法交换联系人名片")
        requester_user = conn.execute(
            "SELECT id,contact_id FROM users WHERE id=?", (requester["user_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE contact_requests SET status='processing',updated_at=? WHERE id=?",
            (now, req["id"]),
        )
        self._reply(
            conn,
            user["id"],
            f"已同意 {requester['nickname']} 的申请。联系人名片附在本消息中，点击即可添加。",
            f"vcard:{req['public_id']}:target",
            sensitive=True,
            vcard_contact_id=requester_user["contact_id"],
        )
        self._reply(
            conn,
            requester_user["id"],
            f"{target['nickname']} 已同意好友申请 {req['public_id']}，你的联系人名片已发送给对方。",
            f"vcard-accepted:{req['public_id']}",
        )
        self._guide_next(conn, user, now, f"{key}:next")

    def _share_link_with_target(self, conn, user, requester, target, now, key) -> None:
        if not requester["share_link_ciphertext"]:
            self._pending(conn, user["id"], "guide_share_link", {}, now)
            self._reply(
                conn,
                user["id"],
                "你还没有绑定好友添加链接。请先粘贴自己的 https://i.delta.chat/# 链接。\n"
                "输入 q 退出。",
                key,
            )
            return
        if not target["accepts_requests"]:
            raise UserError("该成员当前暂停接收好友链接")
        if self._is_blocked(
            conn, requester["network_id"], requester["id"], target["id"]
        ):
            raise UserError("当前无法向该成员发送好友链接")
        if not self._rate_allowed(
            conn, user["actor_key"], "share_link", 5, 24 * 60, now, record=False
        ):
            raise UserError("今天发送好友链接的次数已达上限")
        cutoff = _iso(_parse(now) - timedelta(hours=24))
        duplicate = conn.execute(
            """SELECT 1 FROM link_shares WHERE sender_membership_id=?
               AND target_membership_id=? AND created_at>?""",
            (requester["id"], target["id"], cutoff),
        ).fetchone()
        if duplicate:
            raise UserError("你在 24 小时内已经向该成员发送过好友链接")
        try:
            link = self._decrypt_share_link(requester["share_link_ciphertext"])
        except Exception as exc:
            raise UserError("绑定的好友链接无法读取，请重新绑定") from exc
        target_user_id = conn.execute(
            "SELECT user_id FROM memberships WHERE id=?", (target["id"],)
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO link_shares(
                   network_id,sender_membership_id,target_membership_id,created_at
               ) VALUES(?,?,?,?)""",
            (requester["network_id"], requester["id"], target["id"], now),
        )
        conn.execute(
            "INSERT INTO rate_events(actor_key,category,created_at) VALUES(?,?,?)",
            (user["actor_key"], "share_link", now),
        )
        self._reply(
            conn,
            target_user_id,
            f"{requester['nickname']} 希望添加你为 Delta Chat 联系人。\n"
            "点击下面的链接添加：\n"
            f"{link}\n\n如果你不认识对方，可以忽略这条消息或在机器人中屏蔽对方。",
            f"share-link:{requester['id']}:{target['id']}:{now}",
            sensitive=True,
        )
        self._reply(
            conn,
            user["id"],
            f"你的好友添加链接已发送给 {target['nickname']}。对方点击链接即可添加你。",
            key,
        )
        self._guide_next(conn, user, now, f"{key}:next")

    def _start_request(self, conn, user, args, now, key, guided=False) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            raise UserError("格式：申请 <成员编号> <联系理由>")
        code, reason = parts[0].upper(), parts[1].strip()
        if len(reason) > 200:
            raise UserError("联系理由最多 200 个字符")
        requester, network = self._current_membership(conn, user)
        if not requester["share_contact"]:
            raise UserError("请先发送“设置联系方式 <Delta Chat 地址>”")
        if network["status"] != "active":
            raise UserError("当前网络已停用")
        target = conn.execute(
            "SELECT * FROM memberships WHERE network_id=? AND member_code=? AND status='active'",
            (network["id"], code),
        ).fetchone()
        if not target or target["id"] == requester["id"]:
            raise UserError("目标成员不存在")
        if not target["accepts_requests"]:
            raise UserError("该成员当前不接收联系申请")
        if self._is_blocked(conn, network["id"], requester["id"], target["id"]):
            raise UserError("当前无法向该成员发送申请")
        if not self._rate_allowed(
            conn, user["actor_key"], "request", 5, 24 * 60, now, record=False
        ):
            raise UserError("今日申请次数已达上限")
        open_count = conn.execute(
            "SELECT count(*) FROM contact_requests WHERE requester_membership_id=? AND status IN ('pending_target','processing')",
            (requester["id"],),
        ).fetchone()[0]
        if open_count >= 5:
            raise UserError("同时待处理的申请已达上限")
        cutoff = _iso(_parse(now) - timedelta(days=7))
        refused = conn.execute(
            """SELECT 1 FROM contact_requests WHERE requester_membership_id=? AND target_membership_id=?
               AND status='rejected' AND updated_at>?""",
            (requester["id"], target["id"], cutoff),
        ).fetchone()
        if refused:
            raise UserError("距离上次被拒绝不足 7 天")
        token = self._pending(
            conn,
            user["id"],
            "send_request",
            {
                "target_id": target["id"],
                "reason": reason,
                "contact": requester["share_contact"],
                "version": requester["share_version"],
            },
            now,
        )
        confirmation = (
            "\n回复“确认”发送，回复“取消”放弃；输入 q 退出。"
            if guided
            else f"\n确认发送：确认 {token}"
        )
        self._reply(
            conn,
            user["id"],
            f"将向 {target['nickname']}（{target['member_code']}）申请联系。\n理由：{reason}\n"
            f"对方同意后将收到你的联系方式：{requester['share_contact']}{confirmation}",
            key,
            sensitive=True,
        )

    def _confirm_request(self, conn, user, payload, now, key) -> None:
        requester, network = self._current_membership(conn, user)
        target = conn.execute(
            "SELECT * FROM memberships WHERE id=?", (payload["target_id"],)
        ).fetchone()
        if not target or target["network_id"] != network["id"] or target["status"] != "active":
            raise UserError("目标成员已失效")
        if network["status"] != "active" or not target["accepts_requests"]:
            raise UserError("当前无法发送申请")
        if (
            requester["share_version"] != payload["version"]
            or requester["share_contact"] != payload["contact"]
        ):
            raise UserError("联系方式已修改，请重新发起申请")
        if self._is_blocked(conn, network["id"], requester["id"], target["id"]):
            raise UserError("当前无法向该成员发送申请")
        public_id = self._public_id("R", 8)
        try:
            conn.execute(
                """INSERT INTO contact_requests(public_id,network_id,requester_membership_id,target_membership_id,
                   reason,requester_contact,requester_share_version,status,expires_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'pending_target',?,?,?)""",
                (
                    public_id,
                    network["id"],
                    requester["id"],
                    target["id"],
                    payload["reason"],
                    payload["contact"],
                    payload["version"],
                    _iso(_parse(now) + timedelta(days=7)),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise UserError("你们之间已有一条待处理申请") from exc
        conn.execute(
            "INSERT INTO rate_events(actor_key,category,created_at) VALUES(?,?,?)",
            (user["actor_key"], "request", now),
        )
        target_user_id = conn.execute(
            "SELECT user_id FROM memberships WHERE id=?", (target["id"],)
        ).fetchone()[0]
        self._pending(
            conn,
            target_user_id,
            "guide_incoming_request",
            {"request_public_id": public_id},
            now,
        )
        self._reply(conn, user["id"], f"申请 {public_id} 已发送，有效期 7 天。", key)
        self._reply(
            conn,
            target_user_id,
            f"收到联系申请 {public_id}\n来自：{requester['nickname']}（{requester['member_code']}）\n"
            f"理由：{payload['reason']}\n\n发送“同意”“拒绝”或“屏蔽”。\n输入 q 退出。",
            f"request-notify:{public_id}",
        )

    def _list_requests(self, conn, user, key) -> None:
        m, _ = self._current_membership(conn, user)
        rows = conn.execute(
            """SELECT r.public_id,r.status,r.requester_membership_id,r.target_membership_id,
                      a.nickname requester,b.nickname target
               FROM contact_requests r JOIN memberships a ON a.id=r.requester_membership_id
               JOIN memberships b ON b.id=r.target_membership_id
               WHERE r.requester_membership_id=? OR r.target_membership_id=?
               ORDER BY r.id DESC LIMIT 20""",
            (m["id"], m["id"]),
        ).fetchall()
        if not rows:
            text = "暂无联系申请。"
        else:
            labels = {
                "pending_target": "等待处理",
                "processing": "交换处理中",
                "completed": "已完成",
                "rejected": "已拒绝",
                "withdrawn": "已撤销",
                "expired": "已过期",
                "invalid": "已失效",
            }
            text = "最近申请：\n" + "\n".join(
                f"{r['public_id']}｜{'发给 ' + r['target'] if r['requester_membership_id'] == m['id'] else '来自 ' + r['requester']}｜{labels[r['status']]}"
                for r in rows
            )
        self._reply(conn, user["id"], text, key)

    def _start_accept(self, conn, user, public_id, now, key, guided=False) -> None:
        m, _ = self._current_membership(conn, user)
        if not m["share_contact"]:
            raise UserError("请先设置联系方式")
        req = conn.execute(
            "SELECT * FROM contact_requests WHERE public_id=? AND target_membership_id=? AND status='pending_target'",
            (public_id.upper(), m["id"]),
        ).fetchone()
        if not req:
            raise UserError("没有找到可同意的申请")
        requester = conn.execute(
            "SELECT * FROM memberships WHERE id=?", (req["requester_membership_id"],)
        ).fetchone()
        token = self._pending(
            conn,
            user["id"],
            "accept_request",
            {"request_id": req["id"], "contact": m["share_contact"], "version": m["share_version"]},
            now,
        )
        confirmation = (
            "回复“确认”继续，回复“取消”放弃；输入 q 退出。"
            if guided
            else f"确认：确认 {token}\n输入 q 退出。"
        )
        self._reply(
            conn,
            user["id"],
            f"同意后，你的联系方式 {m['share_contact']} 将发送给 {requester['nickname']}；"
            f"同时你会收到对方已确认的联系方式。\n{confirmation}",
            key,
            sensitive=True,
        )

    def _confirm_accept(self, conn, user, payload, now, key) -> None:
        target, network = self._current_membership(conn, user)
        req = conn.execute(
            "SELECT * FROM contact_requests WHERE id=? AND status='pending_target'",
            (payload["request_id"],),
        ).fetchone()
        if not req or req["target_membership_id"] != target["id"] or req["expires_at"] <= now:
            raise UserError("申请已失效")
        requester = conn.execute(
            "SELECT * FROM memberships WHERE id=?", (req["requester_membership_id"],)
        ).fetchone()
        if (
            target["share_version"] != payload["version"]
            or target["share_contact"] != payload["contact"]
        ):
            raise UserError("联系方式已修改，请重新同意")
        if (
            requester["status"] != "active"
            or network["status"] != "active"
            or self._is_blocked(conn, network["id"], requester["id"], target["id"])
        ):
            raise UserError("申请双方当前无法交换联系方式")
        conn.execute(
            """UPDATE contact_requests SET target_contact=?,target_share_version=?,status='processing',
               updated_at=? WHERE id=?""",
            (payload["contact"], payload["version"], now, req["id"]),
        )
        requester_user = conn.execute(
            "SELECT user_id FROM memberships WHERE id=?", (requester["id"],)
        ).fetchone()[0]
        self._reply(
            conn,
            requester_user,
            f"申请 {req['public_id']} 已同意。对方联系方式：{payload['contact']}\n"
            "请在 Delta Chat 中新建聊天并粘贴这个地址；首次联系时核对加密与验证状态。",
            f"exchange:{req['public_id']}:requester",
            sensitive=True,
        )
        self._reply(
            conn,
            user["id"],
            f"申请 {req['public_id']} 已同意。对方联系方式：{req['requester_contact']}\n"
            "请在 Delta Chat 中新建聊天并粘贴这个地址；首次联系时核对加密与验证状态。",
            f"exchange:{req['public_id']}:target",
            sensitive=True,
        )

    def _reject(self, conn, user, public_id, now, key) -> None:
        m, _ = self._current_membership(conn, user)
        req = conn.execute(
            "SELECT * FROM contact_requests WHERE public_id=? AND target_membership_id=? AND status='pending_target'",
            (public_id.upper(), m["id"]),
        ).fetchone()
        if not req:
            raise UserError("没有找到可拒绝的申请")
        conn.execute(
            "UPDATE contact_requests SET status='rejected',updated_at=? WHERE id=?",
            (now, req["id"]),
        )
        requester_uid = conn.execute(
            "SELECT user_id FROM memberships WHERE id=?", (req["requester_membership_id"],)
        ).fetchone()[0]
        self._reply(conn, user["id"], f"已拒绝申请 {req['public_id']}。", key)
        self._reply(
            conn,
            requester_uid,
            f"申请 {req['public_id']} 未获同意。",
            f"request-rejected:{req['public_id']}",
        )

    def _withdraw(self, conn, user, public_id, now, key) -> None:
        m, _ = self._current_membership(conn, user)
        req = conn.execute(
            "SELECT * FROM contact_requests WHERE public_id=? AND requester_membership_id=? AND status='pending_target'",
            (public_id.upper(), m["id"]),
        ).fetchone()
        if not req:
            raise UserError("没有找到可撤销的申请")
        conn.execute(
            "UPDATE contact_requests SET status='withdrawn',updated_at=? WHERE id=?",
            (now, req["id"]),
        )
        self._reply(conn, user["id"], f"已撤销申请 {req['public_id']}。", key)

    def _block(self, conn, user, member_code, add, now, key) -> None:
        me, network = self._current_membership(conn, user)
        target = conn.execute(
            "SELECT * FROM memberships WHERE network_id=? AND member_code=? AND status='active'",
            (network["id"], member_code.upper()),
        ).fetchone()
        if not target or target["id"] == me["id"]:
            raise UserError("目标成员不存在")
        if add:
            conn.execute(
                "INSERT OR IGNORE INTO blocks VALUES(?,?,?,?)",
                (network["id"], me["id"], target["id"], now),
            )
            conn.execute(
                """UPDATE contact_requests SET status='invalid',updated_at=? WHERE network_id=?
                            AND status='pending_target' AND ((requester_membership_id=? AND target_membership_id=?) OR (requester_membership_id=? AND target_membership_id=?))""",
                (now, network["id"], me["id"], target["id"], target["id"], me["id"]),
            )
            text = "已屏蔽该成员。"
        else:
            conn.execute(
                "DELETE FROM blocks WHERE network_id=? AND blocker_membership_id=? AND blocked_membership_id=?",
                (network["id"], me["id"], target["id"]),
            )
            text = "已解除屏蔽。"
        self._reply(conn, user["id"], text, key)

    def _block_list(self, conn, user, key) -> None:
        me, network = self._current_membership(conn, user)
        rows = conn.execute(
            """SELECT m.member_code,m.nickname FROM blocks b JOIN memberships m ON m.id=b.blocked_membership_id
                               WHERE b.network_id=? AND b.blocker_membership_id=? ORDER BY m.nickname""",
            (network["id"], me["id"]),
        ).fetchall()
        text = (
            "屏蔽列表为空。"
            if not rows
            else "已屏蔽：\n" + "\n".join(f"{r['member_code']}｜{r['nickname']}" for r in rows)
        )
        self._reply(conn, user["id"], text, key)

    def _report(self, conn, user, args, now, key) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            raise UserError("格式：举报 <成员编号> <原因>")
        me, network = self._current_membership(conn, user)
        target = conn.execute(
            "SELECT * FROM memberships WHERE network_id=? AND member_code=? AND status='active'",
            (network["id"], parts[0].upper()),
        ).fetchone()
        if not target or target["id"] == me["id"]:
            raise UserError("目标成员不存在")
        if len(parts[1]) > 500:
            raise UserError("举报原因最多 500 个字符")
        public_id = self._public_id("P", 8)
        conn.execute(
            "INSERT INTO reports(public_id,network_id,reporter_membership_id,target_membership_id,reason,created_at) VALUES(?,?,?,?,?,?)",
            (public_id, network["id"], me["id"], target["id"], parts[1], now),
        )
        self._reply(conn, user["id"], f"举报 {public_id} 已提交。", key)

    def _start_leave(self, conn, user, now, key) -> None:
        m, network = self._current_membership(conn, user)
        if m["role"] == "network_admin":
            count = conn.execute(
                "SELECT count(*) FROM memberships WHERE network_id=? AND role='network_admin' AND status='active'",
                (network["id"],),
            ).fetchone()[0]
            if count <= 1:
                raise UserError("你是最后一位网络管理员，请先由系统管理员指定接任者")
        token = self._pending(
            conn, user["id"], "leave", {"membership_id": m["id"], "network_id": network["id"]}, now
        )
        self._reply(
            conn,
            user["id"],
            f"退出“{network['name']}”会移除该网络资料并使未完成申请失效。"
            f"\n确认-请输入：确认 {token}\n退出-请输入q。",
            key,
        )

    def _confirm_leave(self, conn, user, payload, now, key) -> None:
        m = conn.execute(
            "SELECT * FROM memberships WHERE id=? AND user_id=? AND status='active'",
            (payload["membership_id"], user["id"]),
        ).fetchone()
        if not m:
            raise UserError("成员资格已失效")
        self._invalidate_membership(conn, m["id"], "left", now)
        current = conn.execute(
            "SELECT network_id FROM memberships WHERE user_id=? AND status='active' ORDER BY id LIMIT 1",
            (user["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
            (current[0] if current else None, now, user["id"]),
        )
        self._reply(conn, user["id"], "已退出当前网络。", key)

    def _start_delete(self, conn, user, now, key) -> None:
        if conn.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND role='network_admin' AND status='active'",
            (user["id"],),
        ).fetchone():
            raise UserError("请先由系统管理员撤销你的网络管理员职责")
        token = self._pending(conn, user["id"], "delete", {}, now)
        self._reply(
            conn,
            user["id"],
            f"这会删除你在所有网络中的资料和未完成申请；他人已收到的信息无法收回。"
            f"确认：确认 {token}\n输入 q 退出。",
            key,
        )

    def _confirm_delete(self, conn, user, payload, now, key) -> None:
        memberships = conn.execute(
            "SELECT id FROM memberships WHERE user_id=?", (user["id"],)
        ).fetchall()
        membership_ids = [membership["id"] for membership in memberships]
        for m in memberships:
            self._invalidate_membership(conn, m["id"], "left", now)
        if membership_ids:
            placeholders = ",".join("?" for _ in membership_ids)
            conn.execute(
                f"""DELETE FROM contact_requests
                    WHERE requester_membership_id IN ({placeholders})
                       OR target_membership_id IN ({placeholders})""",
                (*membership_ids, *membership_ids),
            )
            conn.execute(
                f"""DELETE FROM reports
                    WHERE reporter_membership_id IN ({placeholders})
                       OR target_membership_id IN ({placeholders})""",
                (*membership_ids, *membership_ids),
            )
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
        conn.execute("DELETE FROM memberships WHERE user_id=?", (user["id"],))
        conn.execute(
            "UPDATE users SET current_network_id=NULL,updated_at=? WHERE id=?", (now, user["id"])
        )
        self._reply(
            conn, user["id"], "你的联系人网络资料已删除。安全与举报记录按隐私说明到期清理。", key
        )

    def _admin_help(self, conn, user, key) -> None:
        memberships = conn.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND role='network_admin' AND status='active'",
            (user["id"],),
        ).fetchone()
        if not user["is_system_admin"] and not memberships:
            raise UserError("你没有管理员权限")
        text = """好友助手｜管理员指令

网络管理员管理当前选中的网络；系统管理员可以管理全部网络。

【切换管理网络】
我的网络
切换网络 <网络编号>

【网络概况】
网络概况
成员列表
成员列表 <关键词>

【加入码管理】
查看加入码
更换加入码
停用加入
恢复加入

【成员管理】
移除成员 <成员编号>
封禁成员 <成员编号> <原因>
解除封禁 <成员编号>

【举报与记录】
举报列表
处理举报 <举报编号> <处理结果>
管理记录

【账号恢复审核】
恢复申请列表
批准恢复 <恢复编号>
拒绝恢复 <恢复编号>

【仅系统管理员】
创建网络 <网络名称>
网络列表
修改网络名称 <网络编号> <新名称>
任命管理员 <网络编号> <成员编号>
撤销管理员 <网络编号> <成员编号>
停用网络 <网络编号>
恢复网络 <网络编号>"""
        self._reply(conn, user["id"], text, key)

    def _list_account_recoveries(self, conn, user, now, key) -> None:
        if not user["is_system_admin"] and not conn.execute(
            """SELECT 1 FROM memberships WHERE user_id=? AND role='network_admin'
               AND status='active'""",
            (user["id"],),
        ).fetchone():
            raise UserError("你没有管理员权限")
        if user["is_system_admin"]:
            rows = conn.execute(
                """SELECT ar.public_id,ar.expires_at,n.public_id network_public_id,
                          n.name network_name,m.member_code,m.nickname
                   FROM account_recoveries ar
                   JOIN memberships m ON m.id=ar.membership_id
                   JOIN networks n ON n.id=m.network_id
                   WHERE ar.status='pending' AND ar.expires_at>?
                   ORDER BY ar.id LIMIT 50""",
                (now,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ar.public_id,ar.expires_at,n.public_id network_public_id,
                          n.name network_name,m.member_code,m.nickname
                   FROM account_recoveries ar
                   JOIN memberships m ON m.id=ar.membership_id
                   JOIN networks n ON n.id=m.network_id
                   JOIN memberships admin_m ON admin_m.network_id=m.network_id
                   WHERE ar.status='pending' AND ar.expires_at>?
                     AND admin_m.user_id=? AND admin_m.role='network_admin'
                     AND admin_m.status='active'
                   ORDER BY ar.id LIMIT 50""",
                (now, user["id"]),
            ).fetchall()
        if not rows:
            self._reply(conn, user["id"], "没有待审核的账号恢复申请。", key)
            return
        lines = ["待审核的账号恢复申请："]
        lines.extend(
            f"{row['public_id']}｜{row['network_name']}（{row['network_public_id']}）｜"
            f"{row['nickname']}（{row['member_code']}）｜{row['expires_at']} 到期"
            for row in rows
        )
        lines.append("核实身份后发送：批准恢复 <恢复编号> 或 拒绝恢复 <恢复编号>")
        self._reply(conn, user["id"], "\n".join(lines), key)

    def _resolve_account_recovery(self, conn, user, public_id, approve, now, key) -> None:
        if not public_id:
            command = "批准恢复" if approve else "拒绝恢复"
            raise UserError(f"格式：{command} <恢复编号>")
        recovery = conn.execute(
            """SELECT ar.*,m.network_id,m.user_id old_user_id,m.member_code,m.nickname,
                      m.status membership_status,n.name network_name,n.status network_status
               FROM account_recoveries ar
               JOIN memberships m ON m.id=ar.membership_id
               JOIN networks n ON n.id=m.network_id
               WHERE ar.public_id=?""",
            (public_id.upper(),),
        ).fetchone()
        if not recovery or recovery["status"] != "pending":
            raise UserError("没有找到待审核的恢复申请")
        if recovery["expires_at"] <= now:
            conn.execute(
                """UPDATE account_recoveries SET status='expired',resolved_at=?
                   WHERE id=? AND status='pending'""",
                (now, recovery["id"]),
            )
            raise UserError("恢复申请已过期，请让申请人重新提交")
        self._assert_admin_for(conn, user, recovery["network_id"])

        if not approve:
            conn.execute(
                """UPDATE account_recoveries
                   SET status='rejected',resolved_by=?,resolved_at=? WHERE id=?""",
                (user["id"], now, recovery["id"]),
            )
            self._audit(
                conn, recovery["network_id"], user["id"], "reject_account_recovery",
                recovery["member_code"], recovery["public_id"], now,
            )
            self._reply(conn, user["id"], f"已拒绝恢复申请 {recovery['public_id']}。", key)
            self._reply(
                conn, recovery["requester_user_id"],
                f"账号恢复申请 {recovery['public_id']} 未通过。如有疑问，请联系网络管理员。",
                f"recovery-rejected:{recovery['public_id']}",
            )
            return

        if recovery["membership_status"] != "active" or recovery["network_status"] != "active":
            raise UserError("原成员身份或网络已失效，不能恢复")
        requester = conn.execute(
            "SELECT * FROM users WHERE id=?", (recovery["requester_user_id"],)
        ).fetchone()
        if not requester:
            raise UserError("申请账号已不存在")
        if conn.execute(
            "SELECT 1 FROM memberships WHERE network_id=? AND user_id=?",
            (recovery["network_id"], requester["id"]),
        ).fetchone():
            raise UserError("申请账号已经在该网络中留有成员记录，不能覆盖")
        if conn.execute(
            "SELECT 1 FROM bans WHERE network_id=? AND actor_key=?",
            (recovery["network_id"], requester["actor_key"]),
        ).fetchone():
            raise UserError("申请账号已被该网络封禁，不能恢复")

        historical_contacts = conn.execute(
            """SELECT DISTINCT other.id membership_id,other.nickname,other.member_code,
                              other_user.contact_id
               FROM contact_requests r
               JOIN memberships other ON other.id=CASE
                    WHEN r.requester_membership_id=? THEN r.target_membership_id
                    ELSE r.requester_membership_id END
               JOIN users other_user ON other_user.id=other.user_id
               WHERE r.network_id=? AND r.status='completed'
                 AND (r.requester_membership_id=? OR r.target_membership_id=?)
                 AND other.id<>? AND other.network_id=? AND other.status='active'
                 AND NOT EXISTS (
                     SELECT 1 FROM blocks b WHERE b.network_id=r.network_id AND (
                         (b.blocker_membership_id=? AND b.blocked_membership_id=other.id) OR
                         (b.blocker_membership_id=other.id AND b.blocked_membership_id=?)
                     )
                 )
               ORDER BY other.id""",
            (
                recovery["membership_id"], recovery["network_id"],
                recovery["membership_id"], recovery["membership_id"],
                recovery["membership_id"], recovery["network_id"],
                recovery["membership_id"], recovery["membership_id"],
            ),
        ).fetchall()

        conn.execute(
            "UPDATE memberships SET user_id=?,updated_at=? WHERE id=?",
            (requester["id"], now, recovery["membership_id"]),
        )
        conn.execute(
            "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
            (recovery["network_id"], now, requester["id"]),
        )
        replacement = conn.execute(
            """SELECT network_id FROM memberships
               WHERE user_id=? AND status='active' AND network_id<>? ORDER BY id LIMIT 1""",
            (recovery["old_user_id"], recovery["network_id"]),
        ).fetchone()
        conn.execute(
            """UPDATE users SET current_network_id=?,updated_at=?
               WHERE id=? AND current_network_id=?""",
            (
                replacement["network_id"] if replacement else None,
                now, recovery["old_user_id"], recovery["network_id"],
            ),
        )
        conn.execute(
            "DELETE FROM pending_actions WHERE user_id IN (?,?)",
            (requester["id"], recovery["old_user_id"]),
        )
        conn.execute(
            """UPDATE account_recoveries
               SET status='approved',resolved_by=?,resolved_at=? WHERE id=?""",
            (user["id"], now, recovery["id"]),
        )
        self._audit(
            conn, recovery["network_id"], user["id"], "approve_account_recovery",
            recovery["member_code"], recovery["public_id"], now,
        )
        count = len(historical_contacts)
        self._reply(
            conn, requester["id"],
            f"账号恢复成功。你已恢复 {recovery['network_name']} 中的资料和成员编号 "
            f"{recovery['member_code']}。正在重新发送 {count} 位历史好友的联系人名片。",
            f"recovery-approved:{recovery['public_id']}",
        )
        for contact in historical_contacts:
            self._reply(
                conn, requester["id"],
                f"历史好友：{contact['nickname']}（{contact['member_code']}）",
                f"recovery:{recovery['public_id']}:contact:{contact['membership_id']}",
                sensitive=True,
                vcard_contact_id=contact["contact_id"],
            )
        self._reply(
            conn, recovery["old_user_id"],
            f"{recovery['network_name']} 中的成员身份已由管理员批准迁移到新账号。",
            f"recovery-old-account:{recovery['public_id']}",
        )
        self._reply(
            conn, user["id"],
            f"已批准恢复申请 {recovery['public_id']}，并补发 {count} 位历史好友的名片。",
            key,
        )

    def _admin_command(self, conn, user, command, args, encrypted, now, key) -> None:
        if command == "恢复申请列表":
            self._list_account_recoveries(conn, user, now, key)
            return
        if command in {"批准恢复", "拒绝恢复"}:
            self._resolve_account_recovery(
                conn, user, args.strip(), command == "批准恢复", now, key
            )
            return
        if command == "创建网络":
            self._require_system_admin(user)
            if not encrypted:
                raise UserError("创建网络会返回加入码，请在加密私聊中操作")
            name = args.strip()
            if not 1 <= len(name) <= 60:
                raise UserError("格式：创建网络 <1 至 60 字名称>")
            self._pending(conn, user["id"], "guide_admin_create_code", {"name": name}, now)
            self._reply(
                conn,
                user["id"],
                "请直接回复你希望使用的网络加入码。可使用任意非空字符和任意长度；"
                "请勿使用容易猜到或已经公开的内容。\n输入 q 退出。",
                key,
            )
            return
        if command == "网络列表":
            self._require_system_admin(user)
            rows = conn.execute(
                "SELECT public_id,name,status,join_enabled FROM networks ORDER BY id"
            ).fetchall()
            text = (
                "暂无网络。"
                if not rows
                else "网络列表：\n"
                + "\n".join(
                    f"{r['public_id']}｜{r['name']}｜{r['status']}｜加入{'开' if r['join_enabled'] else '关'}"
                    for r in rows
                )
            )
            self._reply(conn, user["id"], text, key)
            return
        if command in {"任命管理员", "撤销管理员"}:
            self._require_system_admin(user)
            parts = args.split(maxsplit=1)
            if len(parts) != 2:
                raise UserError(f"格式：{command} <网络编号> <成员编号>")
            net = self._network_by_public(conn, parts[0])
            target = conn.execute(
                "SELECT * FROM memberships WHERE network_id=? AND member_code=? AND status='active'",
                (net["id"], parts[1].upper()),
            ).fetchone()
            if not target:
                raise UserError("成员不存在")
            role = "network_admin" if command == "任命管理员" else "member"
            conn.execute(
                "UPDATE memberships SET role=?,updated_at=? WHERE id=?",
                (role, now, target["id"]),
            )
            self._audit(conn, net["id"], user["id"], "set_role", target["member_code"], role, now)
            self._reply(conn, user["id"], f"已{command}。", key)
            return
        if command == "修改网络名称":
            self._require_system_admin(user)
            parts = args.split(maxsplit=1)
            if len(parts) != 2 or not 1 <= len(parts[1]) <= 60:
                raise UserError("格式：修改网络名称 <网络编号> <名称>")
            net = self._network_by_public(conn, parts[0])
            conn.execute(
                "UPDATE networks SET name=?,updated_at=? WHERE id=?", (parts[1], now, net["id"])
            )
            self._audit(
                conn, net["id"], user["id"], "rename_network", net["public_id"], parts[1], now
            )
            self._reply(conn, user["id"], "网络名称已修改。", key)
            return
        network = self._admin_network(
            conn, user, args if command in {"停用网络", "恢复网络"} else None
        )
        if command == "网络概况":
            count = conn.execute(
                "SELECT count(*) FROM memberships WHERE network_id=? AND status='active'",
                (network["id"],),
            ).fetchone()[0]
            self._reply(
                conn,
                user["id"],
                f"{network['name']}（{network['public_id']}）\n状态：{network['status']}\n成员：{count}\n加入：{'启用' if network['join_enabled'] else '停用'}\n加入码尾号：{network['join_code_hint']}",
                key,
            )
        elif command == "成员列表":
            query = args.strip()
            rows = conn.execute(
                """SELECT member_code,nickname,role,status FROM memberships WHERE network_id=?
                                   AND (?='' OR member_code=? OR nickname LIKE ?) ORDER BY id LIMIT 30""",
                (network["id"], query, query.upper(), f"%{query[:64]}%"),
            ).fetchall()
            text = (
                "没有成员。"
                if not rows
                else "成员：\n"
                + "\n".join(
                    f"{r['member_code']}｜{r['nickname']}｜{r['role']}｜{r['status']}" for r in rows
                )
            )
            self._reply(conn, user["id"], text, key)
        elif command == "查看加入码":
            if not encrypted:
                raise UserError("加入码只能在加密私聊中查看")
            if not network["join_code_ciphertext"]:
                raise UserError("旧版数据库没有可恢复的加入码，请执行“更换加入码”")
            code = self._decrypt_join_code(network["join_code_ciphertext"])
            self._reply(
                conn,
                user["id"],
                f"{network['name']} 的当前加入码：{code}\n请勿公开传播。",
                key,
                sensitive=True,
            )
        elif command == "更换加入码":
            self._pending(
                conn, user["id"], "guide_admin_rotate_code", {"network_id": network["id"]}, now
            )
            self._reply(
                conn, user["id"],
                "请直接回复新的网络加入码。可使用任意非空字符和任意长度。\n输入 q 退出。",
                key,
            )
        elif command in {"停用加入", "恢复加入"}:
            enabled = command == "恢复加入"
            conn.execute(
                "UPDATE networks SET join_enabled=?,updated_at=? WHERE id=?",
                (int(enabled), now, network["id"]),
            )
            self._audit(
                conn,
                network["id"],
                user["id"],
                "set_join_enabled",
                network["public_id"],
                str(enabled),
                now,
            )
            self._reply(conn, user["id"], f"已{command}。", key)
        elif command in {"移除成员", "封禁成员"}:
            parts = args.split(maxsplit=1)
            target = self._admin_target(conn, user, network, parts[0] if parts else "")
            action = "ban_member" if command == "封禁成员" else "remove_member"
            payload = {
                "membership_id": target["id"],
                "network_id": network["id"],
                "reason": parts[1] if len(parts) > 1 else "",
            }
            token = self._pending(conn, user["id"], action, payload, now)
            self._reply(
                conn,
                user["id"],
                f"准备{command} {target['nickname']}（{target['member_code']}）。"
                f"确认：确认 {token}\n输入 q 退出。",
                key,
            )
        elif command == "解除封禁":
            code = args.strip().upper()
            target = conn.execute(
                "SELECT m.*,u.actor_key FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.network_id=? AND m.member_code=?",
                (network["id"], code),
            ).fetchone()
            if not target:
                raise UserError("成员不存在")
            conn.execute(
                "DELETE FROM bans WHERE network_id=? AND actor_key=?",
                (network["id"], target["actor_key"]),
            )
            self._audit(conn, network["id"], user["id"], "unban_member", code, "", now)
            self._reply(conn, user["id"], "已解除封禁；不会自动恢复成员资料。", key)
        elif command == "举报列表":
            rows = conn.execute(
                """SELECT r.public_id,r.reason,m.member_code target FROM reports r
                                   JOIN memberships m ON m.id=r.target_membership_id WHERE r.network_id=? AND r.status='open' ORDER BY r.id LIMIT 30""",
                (network["id"],),
            ).fetchall()
            text = (
                "没有待处理举报。"
                if not rows
                else "待处理举报：\n"
                + "\n".join(f"{r['public_id']}｜对象 {r['target']}｜{r['reason']}" for r in rows)
            )
            self._reply(conn, user["id"], text, key)
        elif command == "处理举报":
            parts = args.split(maxsplit=1)
            if len(parts) != 2:
                raise UserError("格式：处理举报 <举报编号> <结果>")
            changed = conn.execute(
                "UPDATE reports SET status='resolved',resolution=?,resolved_at=? WHERE public_id=? AND network_id=? AND status='open'",
                (parts[1][:300], now, parts[0].upper(), network["id"]),
            ).rowcount
            if not changed:
                raise UserError("没有找到待处理举报")
            self._audit(
                conn,
                network["id"],
                user["id"],
                "resolve_report",
                parts[0].upper(),
                parts[1][:100],
                now,
            )
            self._reply(conn, user["id"], "举报已标记处理。", key)
        elif command == "管理记录":
            rows = conn.execute(
                "SELECT action,target_ref,created_at FROM audit_log WHERE network_id=? ORDER BY id DESC LIMIT 30",
                (network["id"],),
            ).fetchall()
            text = (
                "暂无管理记录。"
                if not rows
                else "管理记录：\n"
                + "\n".join(f"{r['created_at']}｜{r['action']}｜{r['target_ref']}" for r in rows)
            )
            self._reply(conn, user["id"], text, key)
        elif command in {"停用网络", "恢复网络"}:
            self._require_system_admin(user)
            if command == "停用网络":
                token = self._pending(
                    conn, user["id"], "disable_network", {"network_id": network["id"]}, now
                )
                self._reply(
                    conn, user["id"],
                    f"停用会使未完成申请失效。确认：确认 {token}\n输入 q 退出。", key
                )
            else:
                conn.execute(
                    "UPDATE networks SET status='active',updated_at=? WHERE id=?",
                    (now, network["id"]),
                )
                self._audit(
                    conn, network["id"], user["id"], "enable_network", network["public_id"], "", now
                )
                self._reply(conn, user["id"], "网络已恢复；已失效申请不会恢复。", key)

    def _confirm_rotate_code(self, conn, user, payload, now, key) -> None:
        network = conn.execute(
            "SELECT * FROM networks WHERE id=?", (payload["network_id"],)
        ).fetchone()
        self._assert_admin_for(conn, user, network["id"])
        code = payload.get("code") or self._new_join_code()
        if not code:
            raise UserError("加入码不能为空")
        conn.execute(
            """UPDATE networks SET join_code_hash=?,join_code_ciphertext=?,
               join_code_hint=?,updated_at=? WHERE id=?""",
            (
                self._join_hash(code),
                self._encrypt_join_code(code),
                code[-4:],
                now,
                network["id"],
            ),
        )
        self._audit(
            conn, network["id"], user["id"], "rotate_join_code", network["public_id"], "", now
        )
        self._reply(
            conn,
            user["id"],
            f"加入码已更换：{code}\n请勿公开传播。",
            key,
            sensitive=True,
        )

    def _create_network_with_code(self, conn, user, name, code, now, key):
        self._require_system_admin(user)
        if not code:
            raise UserError("加入码不能为空")
        if self._find_network_by_code(conn, code):
            raise UserError("该加入码已经被其他网络使用，请换一个")
        public_id = self._public_id("N", 6)
        conn.execute(
            """INSERT INTO networks(
                   public_id,name,join_code_hash,join_code_ciphertext,join_code_hint,
                   created_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                public_id,
                name,
                self._join_hash(code),
                self._encrypt_join_code(code),
                code[-4:],
                user["id"],
                now,
                now,
            ),
        )
        network_id = conn.execute(
            "SELECT id FROM networks WHERE public_id=?", (public_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
            (network_id, now, user["id"]),
        )
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user["id"],))
        self._audit(conn, None, user["id"], "create_network", public_id, name, now)
        self._reply(
            conn,
            user["id"],
            f"网络已创建：{name}（{public_id}）\n加入码：{code}\n获得加入码的人即可加入，请勿公开传播。",
            key,
            sensitive=True,
        )

    def _confirm_remove_member(self, conn, user, payload, now, key) -> None:
        self._admin_membership_action(conn, user, payload, now, key, ban=False)

    def _confirm_ban_member(self, conn, user, payload, now, key) -> None:
        self._admin_membership_action(conn, user, payload, now, key, ban=True)

    def _admin_membership_action(self, conn, user, payload, now, key, ban) -> None:
        self._assert_admin_for(conn, user, payload["network_id"])
        target = conn.execute(
            "SELECT m.*,u.actor_key,u.id target_user_id FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.id=?",
            (payload["membership_id"],),
        ).fetchone()
        if not target or target["network_id"] != payload["network_id"]:
            raise UserError("目标成员不存在")
        if (
            target["role"] != "member"
            or conn.execute(
                "SELECT is_system_admin FROM users WHERE id=?", (target["target_user_id"],)
            ).fetchone()[0]
        ):
            raise UserError("网络管理员不能处理管理员账号")
        self._invalidate_membership(conn, target["id"], "removed", now)
        action = "ban_member" if ban else "remove_member"
        if ban:
            conn.execute(
                "INSERT OR REPLACE INTO bans(network_id,actor_key,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                (
                    payload["network_id"],
                    target["actor_key"],
                    payload.get("reason", "")[:300],
                    user["id"],
                    now,
                ),
            )
        self._audit(
            conn,
            payload["network_id"],
            user["id"],
            action,
            target["member_code"],
            payload.get("reason", "")[:100],
            now,
        )
        self._reply(conn, user["id"], "成员已封禁。" if ban else "成员已移除。", key)
        self._reply(
            conn,
            target["target_user_id"],
            "你已被当前网络移除。",
            f"admin-action:{action}:{target['id']}:{now}",
        )

    def _confirm_disable_network(self, conn, user, payload, now, key) -> None:
        self._require_system_admin(user)
        net = conn.execute("SELECT * FROM networks WHERE id=?", (payload["network_id"],)).fetchone()
        conn.execute(
            "UPDATE networks SET status='disabled',updated_at=? WHERE id=?", (now, net["id"])
        )
        conn.execute(
            "UPDATE contact_requests SET status='invalid',updated_at=? WHERE network_id=? AND status IN ('pending_target','processing')",
            (now, net["id"]),
        )
        self._audit(conn, net["id"], user["id"], "disable_network", net["public_id"], "", now)
        self._reply(conn, user["id"], "网络已停用，未完成申请已失效。", key)

    def _admin_network(self, conn, user, explicit_public_id=None):
        if explicit_public_id:
            self._require_system_admin(user)
            network = self._network_by_public(conn, explicit_public_id.strip())
        else:
            network_id = conn.execute(
                "SELECT current_network_id FROM users WHERE id=?", (user["id"],)
            ).fetchone()[0]
            if not network_id:
                raise UserError("请先切换到要管理的网络")
            network = conn.execute("SELECT * FROM networks WHERE id=?", (network_id,)).fetchone()
        self._assert_admin_for(conn, user, network["id"])
        return network

    def _admin_target(self, conn, user, network, code):
        if not code:
            raise UserError("请提供成员编号")
        target = conn.execute(
            "SELECT * FROM memberships WHERE network_id=? AND member_code=? AND status='active'",
            (network["id"], code.upper()),
        ).fetchone()
        if not target:
            raise UserError("成员不存在")
        if target["role"] != "member":
            raise UserError("网络管理员不能处理管理员账号")
        return target

    def _assert_admin_for(self, conn, user, network_id):
        if user["is_system_admin"]:
            return
        allowed = conn.execute(
            "SELECT 1 FROM memberships WHERE network_id=? AND user_id=? AND role='network_admin' AND status='active'",
            (network_id, user["id"]),
        ).fetchone()
        if not allowed:
            raise UserError("你没有管理该网络的权限")

    @staticmethod
    def _require_system_admin(user):
        if not user["is_system_admin"]:
            raise UserError("仅系统管理员可执行此操作")

    @staticmethod
    def _network_by_public(conn, public_id):
        net = conn.execute(
            "SELECT * FROM networks WHERE public_id=?", (public_id.upper(),)
        ).fetchone()
        if not net:
            raise UserError("网络不存在")
        return net

    @staticmethod
    def _audit(conn, network_id, actor_id, action, target, details, now):
        conn.execute(
            "INSERT INTO audit_log(network_id,actor_user_id,action,target_ref,details,created_at) VALUES(?,?,?,?,?,?)",
            (network_id, actor_id, action, target, details, now),
        )

    @staticmethod
    def _invalidate_membership(conn, membership_id, status, now):
        conn.execute(
            """UPDATE memberships SET status=?,role='member',discoverable=0,
               accepts_requests=0,share_contact=NULL,share_link_ciphertext=NULL,
               updated_at=? WHERE id=?""",
            (status, now, membership_id),
        )
        conn.execute(
            "UPDATE contact_requests SET status='invalid',updated_at=? WHERE status IN ('pending_target','processing') AND (requester_membership_id=? OR target_membership_id=?)",
            (now, membership_id, membership_id),
        )

    @staticmethod
    def _current_membership(conn, user):
        current_id = conn.execute(
            "SELECT current_network_id FROM users WHERE id=?", (user["id"],)
        ).fetchone()[0]
        row = None
        if current_id:
            row = conn.execute(
                """SELECT m.*,n.public_id network_public_id,n.name network_name,
                          n.status network_status,n.id network_real_id,
                          n.join_enabled network_join_enabled
                   FROM memberships m JOIN networks n ON n.id=m.network_id
                   WHERE m.user_id=? AND m.network_id=? AND m.status='active'""",
                (user["id"], current_id),
            ).fetchone()
        if not row:
            row = conn.execute(
                """SELECT m.*,n.public_id network_public_id,n.name network_name,
                          n.status network_status,n.id network_real_id,
                          n.join_enabled network_join_enabled
                   FROM memberships m JOIN networks n ON n.id=m.network_id
                   WHERE m.user_id=? AND m.status='active'
                   ORDER BY CASE WHEN n.status='active' THEN 0 ELSE 1 END,m.id LIMIT 1""",
                (user["id"],),
            ).fetchone()
            if not row:
                raise UserError("你还没有有效的网络成员资格")
            conn.execute(
                "UPDATE users SET current_network_id=?,updated_at=? WHERE id=?",
                (row["network_real_id"], _now(), user["id"]),
            )
        network = {
            "id": row["network_real_id"],
            "public_id": row["network_public_id"],
            "name": row["network_name"],
            "status": row["network_status"],
            "join_enabled": row["network_join_enabled"],
        }
        return row, network

    @staticmethod
    def _is_blocked(conn, network_id, a, b):
        return bool(
            conn.execute(
                "SELECT 1 FROM blocks WHERE network_id=? AND ((blocker_membership_id=? AND blocked_membership_id=?) OR (blocker_membership_id=? AND blocked_membership_id=?))",
                (network_id, a, b, b, a),
            ).fetchone()
        )

    def _pending(self, conn, user_id, action, payload, now):
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user_id,))
        token = secrets.token_hex(3).upper()
        conn.execute(
            "INSERT INTO pending_actions(token,user_id,action,payload,expires_at,created_at) VALUES(?,?,?,?,?,?)",
            (
                token,
                user_id,
                action,
                json.dumps(payload, ensure_ascii=False),
                _iso(_parse(now) + timedelta(minutes=10)),
                now,
            ),
        )
        return token

    def _reply(
        self, conn, user_id, body, dedupe, *, sensitive=False, vcard_contact_id=None
    ):
        conn.execute(
            """INSERT OR IGNORE INTO outbox(
                   recipient_user_id,body,dedupe_key,requires_encryption,vcard_contact_id,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                user_id,
                body,
                f"{dedupe}:{user_id}",
                int(sensitive),
                vcard_contact_id,
                _now(),
            ),
        )

    def _join_hash(self, code):
        return hmac.new(
            self.secret, ("join:v2:" + code).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _legacy_join_hash(self, code):
        normalized = re.sub(r"[^A-Z0-9]", "", code.upper())
        return hmac.new(self.secret, ("join:" + normalized).encode(), hashlib.sha256).hexdigest()

    def _find_network_by_code(self, conn, code):
        if not code:
            return None
        return conn.execute(
            "SELECT * FROM networks WHERE join_code_hash IN (?,?) ORDER BY id DESC LIMIT 1",
            (self._join_hash(code), self._legacy_join_hash(code)),
        ).fetchone()

    def _encrypt_join_code(self, code: str) -> str:
        key = hashlib.sha256(self.secret + b"\0join-code-encryption-v1").digest()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, code.encode("utf-8"), b"contactbot:join-code:v1")
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def _decrypt_join_code(self, encoded: str) -> str:
        key = hashlib.sha256(self.secret + b"\0join-code-encryption-v1").digest()
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return (
            AESGCM(key)
            .decrypt(payload[:12], payload[12:], b"contactbot:join-code:v1")
            .decode("utf-8")
        )

    def _encrypt_share_link(self, link: str) -> str:
        key = hashlib.sha256(self.secret + b"\0share-link-encryption-v1").digest()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(
            nonce, link.encode("utf-8"), b"contactbot:share-link:v1"
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def _decrypt_share_link(self, encoded: str) -> str:
        key = hashlib.sha256(self.secret + b"\0share-link-encryption-v1").digest()
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return (
            AESGCM(key)
            .decrypt(payload[:12], payload[12:], b"contactbot:share-link:v1")
            .decode("utf-8")
        )

    @staticmethod
    def _validate_share_link(text: str, contact_address: str) -> str:
        normalized = text.replace("\\&", "&").strip()
        match = re.search(r"https://i\.delta\.chat/#[^\s<>\]\)]+", normalized, re.IGNORECASE)
        if not match:
            raise UserError("请粘贴以 https://i.delta.chat/# 开头的完整好友添加链接")
        link = match.group(0).rstrip("。；，,;.!！")
        parsed = urlsplit(link)
        if parsed.scheme.lower() != "https" or parsed.hostname != "i.delta.chat":
            raise UserError("好友链接必须来自 i.delta.chat")
        parts = parsed.fragment.split("&", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", parts[0]):
            raise UserError("好友链接中的安全指纹无效")
        params = parse_qs(parts[1], keep_blank_values=True)
        if any(not params.get(name) or not params[name][0] for name in ("v", "i", "s", "a")):
            raise UserError("好友链接缺少必要的安全参数")
        address = unquote(params["a"][0]).strip()
        if not contact_address or address.casefold() != contact_address.strip().casefold():
            raise UserError("这个好友链接不属于当前与你机器人私聊的 Delta Chat 账号")
        return link

    @staticmethod
    def _new_join_code():
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(26))
        return "-".join(raw[i : i + 5] for i in range(0, 25, 5)) + raw[25]

    @staticmethod
    def _public_id(prefix, size):
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return prefix + "".join(secrets.choice(alphabet) for _ in range(size))

    def _member_code(self, conn, network_id):
        for _ in range(20):
            code = self._public_id("M", 6)
            if not conn.execute(
                "SELECT 1 FROM memberships WHERE network_id=? AND member_code=?", (network_id, code)
            ).fetchone():
                return code
        raise RuntimeError("无法生成成员编号")

    @staticmethod
    def _validate_nickname(value):
        if not 1 <= len(value.strip()) <= 40:
            raise UserError("昵称长度应为 1 至 40 个字符")

    @staticmethod
    def _valid_contact(value):
        return bool(re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", value.strip()))

    @staticmethod
    def _rate_allowed(conn, actor_key, category, maximum, minutes, now, record=True):
        cutoff = _iso(_parse(now) - timedelta(minutes=minutes))
        count = conn.execute(
            "SELECT count(*) FROM rate_events WHERE actor_key=? AND category=? AND created_at>?",
            (actor_key, category, cutoff),
        ).fetchone()[0]
        if count >= maximum:
            return False
        if record:
            conn.execute(
                "INSERT INTO rate_events(actor_key,category,created_at) VALUES(?,?,?)",
                (actor_key, category, now),
            )
        return True

    @staticmethod
    def _expire(conn, now):
        conn.execute(
            "UPDATE contact_requests SET status='expired',updated_at=? WHERE status='pending_target' AND expires_at<=?",
            (now, now),
        )
        conn.execute(
            """UPDATE account_recoveries SET status='expired',resolved_at=?
               WHERE status='pending' AND expires_at<=?""",
            (now, now),
        )
        conn.execute("DELETE FROM pending_actions WHERE expires_at<=?", (now,))
        conn.execute(
            "DELETE FROM rate_events WHERE created_at<?", (_iso(_parse(now) - timedelta(days=8)),)
        )


def _now() -> str:
    return _iso(datetime.now(UTC))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
