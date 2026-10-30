"""Real-Core offline probe: a sent card survives app commit failure and restart.

Uses only a temporary account and Saved Messages. Never starts network IO and
never opens the production account directory or application database.
"""
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from deltachat2 import MessageData, Rpc
from deltachat2.transport import IOTransport

from contactbot.database import Database
from contactbot.delivery import filename
from contactbot.delta_adapter import DeltaAdapter
from contactbot.identity import public_key_fingerprint
from contactbot.service import BotService, _now


def main():
    executable = os.environ.get("CONTACTBOT_RPC_SERVER", "/app/rpc/deltachat-rpc-server")
    with tempfile.TemporaryDirectory(prefix="contact-delivery-probe-") as directory:
        root = Path(directory)
        account_dir = root / "account"; account_dir.mkdir()
        database = Database(root / "probe.sqlite3"); database.initialize()
        service = BotService(database, b"isolated-probe-secret-at-least-32-bytes")
        with IOTransport(rpc_executable=executable, cwd=str(account_dir)) as transport:
            rpc = Rpc(transport); account = rpc.add_account()
            rpc.set_config(account, "addr", "probe@example.invalid")
            rpc.set_config(account, "configured_addr", "probe@example.invalid")
            qr = rpc.get_chat_securejoin_qr_code(account, None)
            card = root / "source.vcf"
            card.write_text(rpc.make_vcard(account, [1]), encoding="utf-8")
            key = rpc.parse_vcard(str(card))[0].key
            assert key and public_key_fingerprint(key).upper() in qr.upper()
            chat = rpc.create_chat_by_contact_id(account, 1)
            with database.transaction() as conn:
                conn.execute("INSERT INTO users(actor_key,contact_id,chat_id,created_at,updated_at) VALUES('probe',1,?,?,?)", (chat, _now(), _now()))
                user_id = conn.execute("SELECT id FROM users WHERE actor_key='probe'").fetchone()[0]
                service._reply(conn, user_id, "", "probe:once", sensitive=True, vcard_contact_id=1)
                outbox_id = conn.execute("SELECT id FROM outbox").fetchone()[0]
                conn.execute("CREATE TRIGGER fail_sent BEFORE UPDATE OF status ON outbox WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT,'injected app commit failure'); END")
            try:
                service.send_native_vcard(outbox_id, lambda item: rpc.send_msg(account, chat, MessageData(file=str(card), filename=filename(item["delivery_token"]))), account)
                raise AssertionError("Expected database failure")
            except sqlite3.IntegrityError:
                pass
            token = database.connection.execute("SELECT delivery_token FROM outbox").fetchone()[0]
            database.connection.execute("DROP TRIGGER fail_sent"); database.connection.commit()
        database.close()
        # Restart both the application database connection and the Core process.
        reopened = Database(root / "probe.sqlite3"); reopened.initialize()
        try:
            recovered = BotService(reopened, service.secret)
            with IOTransport(rpc_executable=executable, cwd=str(account_dir)) as transport:
                adapter = object.__new__(DeltaAdapter)
                adapter.rpc = Rpc(transport); adapter.service = recovered
                adapter.log = logging.getLogger("offline-probe")
                adapter._reconcile_deliveries(account)
                row = reopened.connection.execute("SELECT status,core_message_id FROM outbox").fetchone()
                assert row["status"] == "sent" and row["core_message_id"]
                ids = adapter.rpc.get_message_ids(account, chat, False, False)
                matches = [mid for mid in ids if adapter.rpc.get_message(account, mid).file_name == filename(token)]
                assert matches == [row["core_message_id"]]
                assert not recovered.send_native_vcard(outbox_id, lambda _: (_ for _ in ()).throw(AssertionError("duplicate send")), account)
        finally:
            reopened.close()
        print(json.dumps({"core_fingerprint_verified": True, "commit_failure_recovered": True, "core_and_app_restarted": True, "matching_cards": 1, "network_started": False}))


if __name__ == "__main__":
    main()
