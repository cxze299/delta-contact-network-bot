from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from contactbot.database import Database
from contactbot.service import BotService, IncomingMessage


@pytest.fixture
def app(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    service = BotService(db, b"test-secret-that-is-at-least-32-bytes")

    class Harness:
        def __init__(self):
            self.next_message_id = 1
            self.last_outbox = []

        def send(self, actor: str, text: str, *, admin=False, encrypted=True):
            actor_number = sum(actor.encode("utf-8"))
            service.handle(
                IncomingMessage(
                    account_id=1,
                    message_id=self.next_message_id,
                    actor_key=actor,
                    contact_id=actor_number,
                    chat_id=actor_number + 1000,
                    text=text,
                    is_private=True,
                is_encrypted=encrypted,
                bootstrap_admin=admin,
                contact_address=f"{actor}@example.org",
            )
            )
            self.next_message_id += 1
            with db.transaction() as conn:
                rows = conn.execute(
                    """SELECT o.id,o.body,o.vcard_contact_id,o.requires_encryption,u.actor_key
                       FROM outbox o JOIN users u ON u.id=o.recipient_user_id
                       WHERE o.status='pending' ORDER BY o.id"""
                ).fetchall()
            self.last_outbox = [dict(row) for row in rows]
            for row in rows:
                service.mark_outbox_sent(row["id"])
            return [(r["actor_key"], r["body"]) for r in rows]

        def confirm_from(self, actor: str, outputs):
            own = [body for who, body in outputs if who == actor]
            token = own[-1].rsplit("确认 ", 1)[1].splitlines()[0].strip()
            return self.send(actor, f"确认 {token}")

        @property
        def conn(self):
            return db.connection

    yield Harness()
    db.close()
