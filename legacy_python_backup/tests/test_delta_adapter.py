from pathlib import Path
from types import SimpleNamespace

from deltachat2.types import ChatType

from contactbot.delta_adapter import DeltaAdapter, approved_vcard


def test_approved_vcard_contains_only_consented_fields():
    card = approved_vcard("approved@example.org", "昵称,甲;乙\n附加")
    assert card == "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:昵称\\,甲\\;乙\\n附加\r\nEMAIL:approved@example.org\r\nEND:VCARD\r\n"


def test_web_outbox_is_polled_without_incoming_messages():
    import threading
    adapter = object.__new__(DeltaAdapter)
    adapter._xdc_wakeup = threading.Event()
    stop = threading.Event()
    calls = []
    def drain(account_id):
        calls.append(account_id)
        stop.set()
    adapter._drain_outbox = drain
    adapter._poll_outbox(7, stop)
    assert calls == [7]


def test_vcard_outbox_generates_and_sends_attachment():
    class Rpc:
        sent = None

        def make_vcard(self, account_id, contacts):
            assert account_id == 7
            assert contacts == [42]
            return "BEGIN:VCARD\r\nVERSION:4.0\r\nEMAIL:alice@example.org\r\nEND:VCARD"

        def send_msg(self, account_id, chat_id, data):
            assert account_id == 7
            assert chat_id == 99
            self.sent = (data, Path(data.file).read_text(encoding="utf-8"))

    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = Rpc()
    adapter._send_outbox_item(
        7,
        {
            "body": "点击名片添加联系人",
            "chat_id": 99,
            "vcard_contact_id": 42,
        },
    )

    data, contents = adapter.rpc.sent
    assert data.filename == "contact.vcf"
    assert data.text == "点击名片添加联系人"
    assert "EMAIL:alice@example.org" in contents
    assert not Path(data.file).exists()


def test_securejoin_completion_sends_welcome_automatically():
    captured = []

    class Rpc:
        def get_contact(self, account_id, contact_id):
            return SimpleNamespace(
                address="alice@example.org", is_verified=True
            )

        def get_basic_chat_info(self, account_id, chat_id):
            return SimpleNamespace(
                is_encrypted=True,
                chat_type=ChatType.SINGLE,
                is_device_chat=False,
                is_self_talk=False,
            )

    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = Rpc()
    adapter.settings = SimpleNamespace(
        actor_key=lambda address: "alice-key",
        is_bootstrap_admin=lambda address: False,
        is_bootstrap_admin_contact=lambda contact_id: False,
    )
    from contextlib import nullcontext
    import threading
    adapter.service = SimpleNamespace(db=SimpleNamespace(transaction=lambda: nullcontext(None)), _ensure_user=lambda conn, incoming, now: captured.append(incoming))
    offered = []
    adapter.webxdc = SimpleNamespace(offer=offered.append)
    adapter._outbox_lock = threading.Lock()
    adapter.log = SimpleNamespace(warning=lambda *args: None)

    adapter._on_securejoin(
        None,
        1,
        SimpleNamespace(progress=1000, contact_id=5, chat_id=6),
    )

    assert len(captured) == 1
    assert captured[0].text == "__GUIDE_WELCOME__"
    assert captured[0].is_private is True
    assert offered == captured

def test_approved_card_contains_only_authorized_contact_and_key(tmp_path):
    from unittest.mock import Mock
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = Mock()
    adapter.settings = SimpleNamespace(require_verified=False)
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(is_encrypted=True, chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False)
    adapter.rpc.get_contact.return_value = SimpleNamespace(address="alice@example.org")
    adapter.rpc.make_vcard.return_value = "native card with key and unrelated profile"
    adapter.rpc.parse_vcard.return_value = [SimpleNamespace(addr="alice@example.org", key="YWJj")]
    captured = []
    adapter.rpc.send_msg.side_effect = lambda a, c, data: captured.append(Path(data.file).read_text(encoding="utf8"))
    item = {"chat_id": 2, "source_contact_id": 3, "contact_id": 4, "body": ""}
    adapter._send_approved_vcard(1, item, "alice@example.org", "Alice")
    assert "KEY;TYPE=PGP;ENCODING=b:YWJj" in captured[0]
    assert "unrelated profile" not in captured[0]
    adapter.rpc.get_contact.return_value.address = "wrong@example.org"
    import pytest
    with pytest.raises(RuntimeError):
        adapter._send_approved_vcard(1, item, "alice@example.org", "Alice")
    adapter.rpc.get_contact.return_value.address = "alice@example.org"
    adapter.rpc.parse_vcard.return_value[0].key = None
    with pytest.raises(RuntimeError):
        adapter._send_approved_vcard(1, item, "alice@example.org", "Alice")
    assert len(captured) == 1


def test_webxdc_update_wakes_serialized_worker_without_trusting_event():
    import threading
    adapter = object.__new__(DeltaAdapter)
    adapter._xdc_wakeup = threading.Event()
    adapter._on_webxdc_update(None, 7, object())
    assert adapter._xdc_wakeup.is_set()


def test_webxdc_event_wakes_waiting_worker_before_poll_timeout():
    import threading
    adapter = object.__new__(DeltaAdapter)
    adapter._xdc_wakeup = threading.Event()
    stop = threading.Event()
    first = threading.Event()
    second = threading.Event()
    calls = []

    def drain(account_id):
        calls.append(account_id)
        if len(calls) == 1:
            first.set()
        else:
            second.set()
            stop.set()

    adapter._drain_outbox = drain
    worker = threading.Thread(target=adapter._poll_outbox, args=(7, stop))
    worker.start()
    try:
        assert first.wait(1)
        adapter._on_webxdc_update(None, 7, object())
        assert second.wait(1)
        assert calls == [7, 7]
    finally:
        stop.set()
        adapter._xdc_wakeup.set()
        worker.join(timeout=2)
