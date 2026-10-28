from __future__ import annotations

import logging
from dataclasses import replace
import tempfile
import threading
import time
from pathlib import Path

from deltachat2 import Bot, MessageData, NewMsgEvent, Rpc, events
from deltachat2.types import ChatType, EventTypeSecurejoinInviterProgress

from .delivery import DeliveryNotStarted, filename as delivery_filename
from .identity import public_key_fingerprint
from .config import Settings
from .service import APPROVED_CONTACT_VCARD, BotService, IncomingMessage
from .webxdc_bridge import WebxdcBridge


class DeltaAdapter:
    def __init__(self, rpc: Rpc, service: BotService, settings: Settings):
        self.rpc = rpc
        self.service = service
        self.settings = settings
        self.log = logging.getLogger("contactbot.delta")
        self._outbox_lock = threading.Lock()
        self.webxdc = WebxdcBridge(rpc, service, require_verified=settings.require_verified, identity_key=settings.actor_key, identity_resolver=self._identity_message)
        hooks = events.HookCollection()
        hooks.on(events.NewMessage)(self._on_message)
        hooks.on(
            events.RawEvent(
                func=lambda event: isinstance(event, EventTypeSecurejoinInviterProgress)
            )
        )(self._on_securejoin)
        self.bot = Bot(rpc, hooks)

    def run_forever(self, account_id: int) -> None:
        self._refresh_identity_bindings(account_id)
        self._recover_recent_contacts(account_id)
        stop = threading.Event()
        worker = threading.Thread(target=self._poll_outbox, args=(account_id, stop), daemon=True)
        worker.start()
        try:
            self.bot.run_forever(account_id)
        finally:
            stop.set()
            worker.join(timeout=5)

    def _identity_message(self, incoming):
        if not incoming.is_private or not incoming.is_encrypted:
            return incoming
        try:
            contact = self.rpc.get_contact(incoming.account_id, incoming.contact_id)
            if contact.is_verified is not True or contact.address.strip().casefold() != incoming.contact_address.strip().casefold():
                return incoming
            card = self.rpc.make_vcard(incoming.account_id, [incoming.contact_id])
            with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", encoding="utf-8", delete=False) as raw:
                path = Path(raw.name); raw.write(card)
            try:
                parsed = self.rpc.parse_vcard(str(path))
            finally:
                path.unlink(missing_ok=True)
            if len(parsed) != 1 or parsed[0].addr.strip().casefold() != incoming.contact_address.strip().casefold() or not parsed[0].key:
                return incoming
            return replace(incoming, identity_fingerprint=public_key_fingerprint(parsed[0].key), identity_verified=True)
        except Exception as exc:
            self.log.warning("读取加密身份暂未完成（%s）", type(exc).__name__)
            return incoming

    def _refresh_identity_bindings(self, account_id):
        from .service import _now
        with self.service.db.transaction() as conn:
            users = list(conn.execute("SELECT DISTINCT contact_id,chat_id FROM users WHERE contact_id>0 AND merged_into IS NULL AND core_account_id IN (0,?)", (account_id,)))
        for user in users:
            try:
                chat = self.rpc.get_basic_chat_info(account_id, user["chat_id"])
                if not chat.is_encrypted or chat.chat_type != ChatType.SINGLE or chat.is_device_chat or chat.is_self_talk:
                    continue
                contact = self.rpc.get_contact(account_id, user["contact_id"])
                incoming = IncomingMessage(account_id, 0, self.settings.actor_key(contact.address), user["contact_id"], user["chat_id"], "", True, True, contact_address=contact.address)
                incoming = self._identity_message(incoming)
                if incoming.identity_verified:
                    with self.service.db.transaction() as conn:
                        self.service._ensure_user(conn, incoming, _now())
            except Exception as exc:
                self.log.warning("账号归并检查将于下次交互重试（%s）", type(exc).__name__)

    def _recover_recent_contacts(self, account_id: int) -> None:
        """Offer one app to recent unread chats missed while offline or gated."""
        try:
            message_ids = self.rpc.get_fresh_msgs(account_id)[:200]
        except Exception as exc:  # noqa: BLE001 - recovery must not prevent startup
            self.log.warning("读取待回复联系人失败（%s）", type(exc).__name__)
            return
        seen = set()
        cutoff = time.time() - 86400
        for message_id in message_ids:
            try:
                msg = self.rpc.get_message(account_id, message_id)
                if msg.is_info or msg.is_bot or msg.from_id <= 9 or msg.received_timestamp < cutoff or msg.chat_id in seen:
                    continue
                seen.add(msg.chat_id)
                with self.service.db.transaction() as conn:
                    exists = conn.execute("SELECT 1 FROM xdc_instances WHERE account_id=? AND chat_id=?", (account_id, msg.chat_id)).fetchone()
                if not exists:
                    self._on_message(self.bot, account_id, NewMsgEvent(command="", payload="", msg=msg))
            except Exception as exc:  # noqa: BLE001 - isolate failed contacts without private RPC logs
                self.log.warning("补发小程序暂未完成，message_id=%s（%s）", message_id, type(exc).__name__)

    def _poll_outbox(self, account_id, stop):
        while not stop.is_set():
            try:
                self._drain_outbox(account_id)
            except Exception:
                self.log.exception('待发送队列暂不可用，将自动重试')
            stop.wait(3)

    def _on_message(self, _bot: Bot, account_id: int, event: NewMsgEvent) -> None:
        message = event.msg
        chat = self.rpc.get_basic_chat_info(account_id, message.chat_id)
        address = message.sender.address
        incoming = IncomingMessage(
            account_id=account_id,
            message_id=message.id,
            actor_key=self.settings.actor_key(address),
            contact_id=message.from_id,
            chat_id=message.chat_id,
            text=message.text,
            is_private=(
                chat.chat_type == ChatType.SINGLE
                and not chat.is_device_chat
                and not chat.is_self_talk
            ),
            is_encrypted=bool(
                chat.is_encrypted
                and message.show_padlock
                and (not self.settings.require_verified or message.sender.is_verified)
            ),
            bootstrap_admin=(
                self.settings.is_bootstrap_admin(address)
                or self.settings.is_bootstrap_admin_contact(message.from_id)
            ),
            contact_address=address,
        )
        incoming = self._identity_message(incoming)
        with self._outbox_lock:
            if not incoming.is_private or not incoming.is_encrypted:
                return
            with self.service.db.transaction() as conn:
                from .service import _now
                self.service._ensure_user(conn, incoming, _now())
            if not self.webxdc.handle(incoming):
                with self.service.db.transaction() as conn:
                    from .service import _now
                    if conn.execute("SELECT 1 FROM incoming_messages WHERE account_id=? AND message_id=?", (incoming.account_id, incoming.message_id)).fetchone():
                        return
                    self.service._ensure_user(conn, incoming, _now())
                self.webxdc.offer(incoming, replace=True)
                with self.service.db.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO incoming_messages(account_id,message_id,processed_at) VALUES(?,?,?)", (incoming.account_id, incoming.message_id, _now()))
            self._sync_webxdc(account_id)
            self._drain_outbox_locked(account_id)

    def _on_securejoin(
        self, _bot: Bot, account_id: int, event: EventTypeSecurejoinInviterProgress
    ) -> None:
        if event.progress != 1000:
            return
        contact = self.rpc.get_contact(account_id, event.contact_id)
        chat = self.rpc.get_basic_chat_info(account_id, event.chat_id)
        if (
            not contact.is_verified or not chat.is_encrypted
            or chat.chat_type != ChatType.SINGLE or chat.is_device_chat or chat.is_self_talk
        ):
            return
        incoming = IncomingMessage(
                account_id=account_id,
                message_id=-(event.contact_id + 1_000_000),
                actor_key=self.settings.actor_key(contact.address),
                contact_id=event.contact_id,
                chat_id=event.chat_id,
                text="__GUIDE_WELCOME__",
                is_private=True,
                is_encrypted=True,
                bootstrap_admin=(
                    self.settings.is_bootstrap_admin(contact.address)
                    or self.settings.is_bootstrap_admin_contact(event.contact_id)
                ),
                contact_address=contact.address,
        )
        incoming = self._identity_message(incoming)
        with self._outbox_lock:
            with self.service.db.transaction() as conn:
                from .service import _now
                self.service._ensure_user(conn, incoming, _now())
            self.webxdc.offer(incoming)

    def _drain_outbox(self, account_id: int) -> None:
        with self._outbox_lock:
            self._sync_webxdc(account_id)
            self._drain_outbox_locked(account_id)

    def _sync_webxdc(self, account_id):
        try:
            self.webxdc.sync_all(account_id)
        except Exception as exc:  # noqa: BLE001 - preserve chat delivery without leaking RPC payloads
            # RPC errors can contain private update payloads. Do not log them.
            self.log.warning("小应用同步暂未完成，将重试（%s）", type(exc).__name__)

    def _drain_outbox_locked(self, account_id: int) -> None:
        self._reconcile_deliveries(account_id)
        items = self.service.pending_outbox(app_ready_only=True)
        for item in items:
            try:
                if item["vcard_contact_id"] == APPROVED_CONTACT_VCARD:
                    self.service.send_approved_vcard(
                        item["id"], lambda queued, address, nickname: self._send_approved_vcard(
                            account_id, queued, address, nickname
                        ), account_id=account_id
                    )
                    continue
                if item["vcard_contact_id"] is not None:
                    self.service.send_native_vcard(item["id"], lambda queued: self._send_outbox_item(account_id, queued), account_id)
                    continue
                chat = self.rpc.get_basic_chat_info(account_id, item["chat_id"])
                if item["requires_encryption"] and not chat.is_encrypted:
                    raise RuntimeError("目标私聊当前未加密")
                if not self.webxdc.deliver_outbox(account_id, item):
                    self._send_outbox_item(account_id, item)
            except Exception as exc:  # noqa: BLE001 - keep failed items in the queue
                self.log.warning("发送待处理消息失败，outbox_id=%s，错误类型=%s", item["id"], type(exc).__name__)
                self.service.mark_outbox_failed(item["id"], type(exc).__name__)
            else:
                self.service.mark_outbox_sent(item["id"])

    def _reconcile_deliveries(self, account_id):
        last = getattr(self, "_delivery_checks", {})
        self._delivery_checks = last
        for item in self.service.uncertain_deliveries(account_id):
            if time.monotonic() - last.get(item["id"], -1000) < 15:
                continue
            last[item["id"]] = time.monotonic()
            try:
                chat_id = item["delivery_chat_id"]
                expected = delivery_filename(item["delivery_token"])
                ids = [item["core_message_id"]] if item["core_message_id"] else self.rpc.get_message_ids(account_id, chat_id, False, False)
                for message_id in reversed(ids):
                    message = self.rpc.get_message(account_id, message_id)
                    if message.from_id == 1 and message.chat_id == chat_id and message.file_name == expected:
                        self.service.reconcile_delivery(item["id"], item["delivery_token"], message_id)
                        break
            except Exception as exc:
                self.log.warning("名片交付状态核对将重试（%s）", type(exc).__name__)

    def _send_approved_vcard(self, account_id, item, address, nickname):
        path = None
        try:
            try:
                chat = self.rpc.get_basic_chat_info(account_id, item["chat_id"])
                if (not chat.is_encrypted or chat.chat_type != ChatType.SINGLE
                        or chat.is_device_chat or chat.is_self_talk):
                    raise RuntimeError("目标私聊不可用于加密交付")
                if self.settings.require_verified and not self.rpc.get_contact(account_id, item["contact_id"]).is_verified:
                    raise RuntimeError("目标联系人尚未验证")
                source_id = item["source_contact_id"]
                source = self.rpc.get_contact(account_id, source_id)
                if source.address.strip().casefold() != address.strip().casefold():
                    raise RuntimeError("授权账号与可用联系人不一致")
                native_card = self.rpc.make_vcard(account_id, [source_id])
                with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", encoding="utf-8", delete=False) as raw:
                    raw_path = Path(raw.name); raw.write(native_card)
                try:
                    parsed = self.rpc.parse_vcard(str(raw_path))
                finally:
                    raw_path.unlink(missing_ok=True)
                if len(parsed) != 1 or parsed[0].addr.strip().casefold() != address.strip().casefold() or not parsed[0].key:
                    raise RuntimeError("授权联系人缺少可用公钥")
                vcard = approved_vcard(address, nickname, parsed[0].key)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", encoding="utf-8", newline="", delete=False) as attachment:
                    path = Path(attachment.name); attachment.write(vcard)
                name = delivery_filename(item["delivery_token"]) if item.get("delivery_token") else "contact.vcf"
                data = MessageData(text=item["body"], file=str(path), filename=name)
            except Exception as exc:
                raise DeliveryNotStarted(type(exc).__name__) from None
            # From this point any exception is ambiguous. Never retry send_msg
            # until the existing Core message has been reconciled.
            return self.rpc.send_msg(account_id, item["chat_id"], data)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def _send_outbox_item(self, account_id: int, item) -> int:
        attachment_path = None
        try:
            try:
                message_data = MessageData(text=item["body"])
                if item["vcard_contact_id"] is not None:
                    # Direct test callers have no durable delivery token.
                    if item.get("delivery_token"):
                        chat = self.rpc.get_basic_chat_info(account_id, item["chat_id"])
                        if not chat.is_encrypted or chat.chat_type != ChatType.SINGLE or chat.is_device_chat or chat.is_self_talk:
                            raise RuntimeError("目标私聊不可用于加密交付")
                    vcard = self.rpc.make_vcard(account_id, [item["vcard_contact_id"]])
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", encoding="utf-8", delete=False) as attachment:
                        attachment.write(vcard); attachment_path = Path(attachment.name)
                    name = delivery_filename(item["delivery_token"]) if item.get("delivery_token") else "contact.vcf"
                    message_data = MessageData(text=item["body"], file=str(attachment_path), filename=name)
            except Exception as exc:
                if item["vcard_contact_id"] is not None:
                    raise DeliveryNotStarted(type(exc).__name__) from None
                raise
            return self.rpc.send_msg(account_id, item["chat_id"], message_data)
        finally:
            if attachment_path is not None:
                attachment_path.unlink(missing_ok=True)


def approved_vcard(address: str, nickname: str, public_key: str | None = None) -> str:
    def escape(value):
        return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")
    key_line = f"KEY;TYPE=PGP;ENCODING=b:{public_key}\r\n" if public_key else ""
    return f"BEGIN:VCARD\r\nVERSION:4.0\r\nFN:{escape(nickname)}\r\nEMAIL:{escape(address)}\r\n{key_line}END:VCARD\r\n"
