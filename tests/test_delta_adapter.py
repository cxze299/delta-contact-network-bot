from pathlib import Path
from types import SimpleNamespace

from deltachat2.types import ChatType

from contactbot.delta_adapter import DeltaAdapter


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
    adapter.service = SimpleNamespace(handle=captured.append)
    adapter._drain_outbox = lambda account_id: None
    adapter.log = SimpleNamespace(warning=lambda *args: None)

    adapter._on_securejoin(
        None,
        1,
        SimpleNamespace(progress=1000, contact_id=5, chat_id=6),
    )

    assert len(captured) == 1
    assert captured[0].text == "__GUIDE_WELCOME__"
    assert captured[0].is_private is True
