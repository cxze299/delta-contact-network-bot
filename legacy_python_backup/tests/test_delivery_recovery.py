import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_webxdc_flow as flow_fixtures

from contactbot.database import Database
from contactbot.delta_adapter import DeltaAdapter
from contactbot.delivery import DeliveryNotStarted, filename
from contactbot.service import BotService, UserError, _now


@pytest.fixture
def clients(tmp_path):
    yield from flow_fixtures.clients.__wrapped__(tmp_path)


def queued(clients):
    alice, bob = clients
    alice.join(); bob.join()
    alice.call("exchange_now", value=bob.snapshot()["profile"]["member_code"])
    service = alice.bridge.service
    return alice, service, next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:"))


def test_committed_claim_survives_restart_and_reconciles_existing_core_message(clients):
    alice, service, item = queued(clients)
    alice.db.execute("CREATE TRIGGER fault BEFORE UPDATE OF status ON outbox WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    alice.db.commit()
    sent = []
    with pytest.raises(sqlite3.IntegrityError):
        service.send_approved_vcard(item["id"], lambda row, *_: sent.append(row["delivery_token"]) or 42, account_id=1)
    alice.db.execute("DROP TRIGGER fault"); alice.db.commit()
    reopened = Database(service.db.path); reopened.initialize()
    try:
        fresh = BotService(reopened, service.secret)
        assert not fresh.send_approved_vcard(item["id"], lambda *_: pytest.fail("duplicate send"), account_id=1)
        adapter = object.__new__(DeltaAdapter); adapter.service = fresh
        adapter.rpc = Mock(); adapter.log = Mock()
        adapter.rpc.get_message_ids.return_value = [42]
        adapter.rpc.get_message.return_value = SimpleNamespace(from_id=1, chat_id=item["chat_id"], file_name=filename(sent[0]))
        adapter._reconcile_deliveries(1)
        row = reopened.connection.execute("SELECT status,core_message_id FROM outbox WHERE id=?", (item["id"],)).fetchone()
        assert tuple(row) == ("sent", 42)
        adapter.rpc.send_msg.assert_not_called()
        assert len(sent) == 1
    finally:
        reopened.close()


def test_preflight_failure_is_retryable_and_uses_same_delivery_token(clients):
    alice, service, item = queued(clients)
    def fail(*_):
        raise DeliveryNotStarted("No send RPC made")
    assert not service.send_approved_vcard(item["id"], fail, account_id=1)
    before = dict(alice.db.execute("SELECT * FROM outbox WHERE id=?", (item["id"],)).fetchone())
    assert before["status"] == "pending"
    tokens = []
    assert service.send_approved_vcard(item["id"], lambda row, *_: tokens.append(row["delivery_token"]) or 91, account_id=1)
    assert tokens == [before["delivery_token"]]


@pytest.mark.parametrize("wrong", ["sender", "chat", "filename", "missing"])
def test_ambiguous_delivery_not_retried_or_falsely_acknowledged(clients, wrong):
    alice, service, item = queued(clients)
    def unknown(*_):
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        service.send_approved_vcard(item["id"], unknown, account_id=1)
    service.mark_outbox_failed(item["id"], "TimeoutError")
    delivery = service.uncertain_deliveries(1)[0]
    adapter = object.__new__(DeltaAdapter); adapter.service = service; adapter.rpc = Mock(); adapter.log = Mock()
    adapter.rpc.get_message_ids.return_value = [] if wrong == "missing" else [42]
    adapter.rpc.get_message.return_value = SimpleNamespace(from_id=2 if wrong == "sender" else 1, chat_id=-1 if wrong == "chat" else item["chat_id"], file_name="other.vcf" if wrong == "filename" else filename(delivery["delivery_token"]))
    adapter._reconcile_deliveries(1)
    assert alice.db.execute("SELECT status FROM outbox WHERE id=?", (item["id"],)).fetchone()[0] == "sending"
    assert item["id"] not in {r["id"] for r in service.pending_outbox(200)}
    adapter.rpc.send_msg.assert_not_called()


def test_claim_failure_does_not_call_core(clients):
    alice, service, item = queued(clients)
    alice.db.execute("CREATE TRIGGER fault BEFORE UPDATE OF status ON outbox WHEN NEW.status='sending' BEGIN SELECT RAISE(ABORT,'claim failure'); END"); alice.db.commit()
    sender = Mock()
    with pytest.raises(sqlite3.IntegrityError):
        service.send_approved_vcard(item["id"], sender, account_id=1)
    sender.assert_not_called()
    assert alice.db.execute("SELECT status FROM outbox WHERE id=?", (item["id"],)).fetchone()[0] == "pending"


def test_admin_can_close_uncertainty_but_not_cross_network(clients):
    alice, service, item = queued(clients)
    with pytest.raises(TimeoutError):
        service.send_approved_vcard(item["id"], lambda *_: (_ for _ in ()).throw(TimeoutError()), account_id=1)
    with service.db.transaction() as conn:
        admin = dict(conn.execute("SELECT * FROM users WHERE actor_key='alice'").fetchone()); admin["is_system_admin"] = 1
        with pytest.raises(UserError):
            service.close_uncertain_delivery(conn, admin, 999, item["id"], _now())
        service.close_uncertain_delivery(conn, admin, admin["current_network_id"], item["id"], _now())
    assert not service.uncertain_deliveries(1)
    assert not service.send_approved_vcard(item["id"], lambda *_: pytest.fail("resend"), account_id=1)
    assert alice.db.execute("SELECT status FROM contact_requests").fetchone()[0] == "invalid"


def test_native_card_path_also_has_durable_claim(clients):
    alice, service, _ = queued(clients)
    with service.db.transaction() as conn:
        user = conn.execute("SELECT id FROM users WHERE actor_key='alice'").fetchone()[0]
        service._reply(conn, user, "", "recovery:test", sensitive=True, vcard_contact_id=20)
    item = next(r for r in service.pending_outbox(200) if r["vcard_contact_id"] == 20)
    with pytest.raises(TimeoutError):
        service.send_native_vcard(item["id"], lambda *_: (_ for _ in ()).throw(TimeoutError()), 1)
    assert not service.send_native_vcard(item["id"], lambda *_: pytest.fail("duplicate"), 1)
    assert service.uncertain_deliveries(1)[0]["id"] == item["id"]
