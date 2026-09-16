from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from deltachat2 import Bot, MessageData, NewMsgEvent, Rpc, events
from deltachat2.types import ChatType, EventTypeSecurejoinInviterProgress

from .config import Settings
from .service import BotService, IncomingMessage


class DeltaAdapter:
    def __init__(self, rpc: Rpc, service: BotService, settings: Settings):
        self.rpc = rpc
        self.service = service
        self.settings = settings
        self.log = logging.getLogger("contactbot.delta")
        hooks = events.HookCollection()
        hooks.on(events.NewMessage)(self._on_message)
        hooks.on(
            events.RawEvent(
                func=lambda event: isinstance(event, EventTypeSecurejoinInviterProgress)
            )
        )(self._on_securejoin)
        self.bot = Bot(rpc, hooks)

    def run_forever(self, account_id: int) -> None:
        self._drain_outbox(account_id)
        self.bot.run_forever(account_id)

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
        self.service.handle(incoming)
        self._drain_outbox(account_id)

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
        self.service.handle(incoming)
        self._drain_outbox(account_id)

    def _drain_outbox(self, account_id: int) -> None:
        items = self.service.pending_outbox()
        for item in items:
            try:
                chat = self.rpc.get_basic_chat_info(account_id, item["chat_id"])
                if item["requires_encryption"] and not chat.is_encrypted:
                    raise RuntimeError("目标私聊当前未加密")
                self._send_outbox_item(account_id, item)
            except Exception as exc:
                self.log.exception("发送待处理消息失败，outbox_id=%s", item["id"])
                self.service.mark_outbox_failed(item["id"], type(exc).__name__)
            else:
                self.service.mark_outbox_sent(item["id"])

    def _send_outbox_item(self, account_id: int, item) -> None:
        attachment_path = None
        try:
            message_data = MessageData(text=item["body"])
            if item["vcard_contact_id"] is not None:
                vcard = self.rpc.make_vcard(account_id, [item["vcard_contact_id"]])
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".vcf", encoding="utf-8", delete=False
                ) as attachment:
                    attachment.write(vcard)
                    attachment_path = Path(attachment.name)
                message_data = MessageData(
                    text=item["body"], file=str(attachment_path), filename="contact.vcf"
                )
            self.rpc.send_msg(account_id, item["chat_id"], message_data)
        finally:
            if attachment_path is not None:
                attachment_path.unlink(missing_ok=True)
