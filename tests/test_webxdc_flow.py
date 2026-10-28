import base64
import hashlib
import json
import secrets
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from deltachat2.types import ChatType

from contactbot.database import Database
from contactbot.delta_adapter import DeltaAdapter
from contactbot.service import BotService, IncomingMessage
from contactbot.webxdc_bridge import PREFIX, WebxdcBridge


class Client:
    def __init__(self, bridge, actor, contact_id):
        self.bridge = bridge
        self.incoming = IncomingMessage(1, contact_id, actor, contact_id, contact_id + 100, "", True, True, contact_address=actor + "@example.org")
        bridge.offer(self.incoming)
        self.token = self.db.execute("SELECT token FROM xdc_instances WHERE actor_key=?", (actor,)).fetchone()[0]

    @property
    def db(self):
        return self.bridge.service.db.connection

    def call(self, action="command", **extra):
        row = self.db.execute("SELECT current_network_id FROM users WHERE actor_key=?", (self.incoming.actor_key,)).fetchone()
        values = {"id": secrets.token_hex(12), "token": self.token, "network": row[0] if row else None, "action": action, **extra}
        message = replace(self.incoming, text=PREFIX + json.dumps(values))
        assert self.bridge.handle(message)
        return message

    def reply(self, value, **extra):
        step = self.db.execute("SELECT p.token FROM pending_actions p JOIN users u ON u.id=p.user_id WHERE u.actor_key=?", (self.incoming.actor_key,)).fetchone()[0]
        return self.call("reply", step=step, value=value, **extra)

    def join(self, code="test-network-code", visible=True):
        self.call("join")
        self.reply(code)
        self.reply(self.incoming.actor_key)
        self.reply("1", consent=True, discoverable=visible, accepts_requests=True)
        self.call(command="设置联系方式", value=self.incoming.contact_address)

    def snapshot(self):
        self.bridge._last_sync.clear()
        self.bridge.sync(self.incoming)
        return json.loads(self.bridge.rpc.send_webxdc_status_update.call_args.args[2])["payload"]


@pytest.fixture
def clients(tmp_path):
    db = Database(tmp_path / "flow.db")
    db.initialize()
    service = BotService(db, b"test-secret-with-at-least-32-bytes!")
    service.seed_network("共同网络", "test-network-code")
    rpc = Mock()
    rpc.get_webxdc_status_updates.return_value = "[]"
    rpc.get_chat_media.return_value = []
    rpc.send_msg.side_effect = range(100, 200)
    bridge = WebxdcBridge(rpc, service)
    yield Client(bridge, "alice", 10), Client(bridge, "bob", 20)
    db.close()


def start_request(alice, bob):
    code = bob.snapshot()["profile"]["member_code"]
    alice.call(command="申请", value=code + " 想交流读书心得")
    assert alice.snapshot()["pending"]["contact"] == "alice@example.org"
    assert alice.snapshot()["pending"]["reason"] == "想交流读书心得"
    assert alice.db.execute("SELECT COUNT(*) FROM contact_requests").fetchone()[0] == 0
    message = alice.reply("1")
    alice.bridge.handle(message)
    assert alice.db.execute("SELECT COUNT(*) FROM contact_requests").fetchone()[0] == 1
    return alice.db.execute("SELECT public_id FROM contact_requests").fetchone()[0]


def test_privacy_confirmation_is_required(clients):
    alice, _ = clients
    alice.call("join"); alice.reply("test-network-code"); alice.reply("小林")
    alice.reply("1")
    assert alice.db.execute("SELECT COUNT(*) FROM memberships").fetchone()[0] == 0
    alice.reply("1", consent=True, discoverable=False, accepts_requests=False)
    profile = alice.snapshot()["profile"]
    assert profile["discoverable"] == profile["accepts_requests"] == 0


def test_onboarding_completes_with_current_account_without_checkbox(clients):
    alice, _ = clients
    alice.call("join", value="test-network-code")
    assert alice.snapshot()["pending"]["action"] == "guide_nickname"
    alice.reply("小林")
    state = alice.snapshot()
    assert state["pending"]["nickname"] == "小林"
    assert state["communication_address"] == "alice@example.org"
    assert state["profile"] is None
    alice.reply("1", consent=True, use_current_account=True, discoverable=False, accepts_requests=True, communication_address="untrusted@example.org")
    state = alice.snapshot()
    assert state["pending"] is None
    assert state["profile"]["share_contact"] == "alice@example.org"
    assert state["profile"]["discoverable"] == 0
    assert state["profile"]["accepts_requests"] == 1


def test_invalid_inline_join_keeps_rate_limit_and_reports_error(clients):
    alice, _ = clients
    alice.call("join", value="wrong-code")
    assert alice.snapshot()["receipt"]["ok"] == 0
    assert alice.db.execute("SELECT count(*) FROM rate_events WHERE category='join_attempt'").fetchone()[0] == 1


def test_use_current_account_uses_server_identity(clients):
    alice, _ = clients
    alice.join()
    alice.call("use_current_account", value="untrusted@example.org")
    assert alice.snapshot()["profile"]["share_contact"] == "alice@example.org"


def test_full_mutual_consent_and_replay(clients):
    alice, bob = clients
    alice.join(); bob.join()
    alice.call("search", value="bob")
    snapshot = alice.snapshot()
    assert snapshot["choices"][0]["nickname"] == "bob"
    assert "bob@example.org" not in json.dumps(snapshot)
    assert alice.db.execute("SELECT COUNT(*) FROM rate_events WHERE category='search'").fetchone()[0] == 1
    public_id = start_request(alice, bob)
    bob.call(command="同意申请", value=public_id)
    snapshot = bob.snapshot()
    assert snapshot["pending"]["contact"] == "bob@example.org"
    assert snapshot["pending"]["recipient"]["nickname"] == "alice"
    assert bob.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE 'exchange:%'").fetchone()[0] == 0
    message = bob.reply("1")
    # Restart-style bridge replays cannot repeat the exchange.
    restarted = WebxdcBridge(bob.bridge.rpc, bob.bridge.service)
    restarted.handle(message)
    assert bob.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE 'exchange:%'").fetchone()[0] == 2


def test_directory_first_page_arrives_in_initial_snapshot(clients):
    alice, bob = clients
    alice.join(); bob.join()
    directory = alice.snapshot()["directory"]
    assert directory["page"] == 0
    assert [m["nickname"] for m in directory["members"]] == ["bob"]
    assert [m["nickname"] for m in bob.snapshot()["directory"]["members"]] == ["alice"]
    alice.call(command="隐藏资料")
    assert bob.snapshot()["directory"]["members"] == []
    assert alice.db.execute("SELECT count(*) FROM xdc_directory WHERE token=?", (alice.token,)).fetchone()[0] == 0


def test_exchange_sends_only_authorized_addresses(clients):
    alice, bob = clients
    alice.join(); bob.join()
    alice.call(command="设置联系方式", value="different@example.org")
    assert not alice.snapshot()["receipt"]["ok"]
    public_id = start_request_with_current_contact(alice, bob)
    bob.call(command="同意申请", value=public_id); bob.reply("1")
    service = alice.bridge.service
    cards = []
    items = [r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:")]
    assert len(items) == 2
    for item in items:
        assert service.send_approved_vcard(item["id"], lambda queued, address, name: cards.append((queued["recipient_user_id"], address, name)))
        assert not service.send_approved_vcard(item["id"], lambda *_: pytest.fail("duplicate"))
    assert {(address, name) for _, address, name in cards} == {("alice@example.org", "alice"), ("bob@example.org", "bob")}
    assert alice.db.execute("SELECT status FROM contact_requests").fetchone()[0] == "completed"


def start_request_with_current_contact(alice, bob):
    code = bob.snapshot()["profile"]["member_code"]
    alice.call(command="申请", value=code + " 你好")
    alice.reply("1")
    return alice.db.execute("SELECT public_id FROM contact_requests").fetchone()[0]


def test_failed_vcard_delivery_stays_uncertain_and_is_not_retried(clients):
    alice, bob = clients
    alice.join(); bob.join()
    public_id = start_request_with_current_contact(alice, bob)
    bob.call(command="同意申请", value=public_id); bob.reply("1")
    service = alice.bridge.service
    item = next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:"))
    def fail(*_):
        raise RuntimeError("send failed")
    with pytest.raises(RuntimeError):
        service.send_approved_vcard(item["id"], fail)
    assert alice.db.execute("SELECT status FROM outbox WHERE id=?", (item["id"],)).fetchone()[0] == "sending"
    assert not service.send_approved_vcard(item["id"], lambda *_: pytest.fail("uncertain delivery retried"))


def test_commit_failure_after_send_never_retries(clients):
    alice, bob = clients
    alice.join(); bob.join()
    public_id = start_request_with_current_contact(alice, bob)
    bob.call(command="同意申请", value=public_id); bob.reply("1")
    service = alice.bridge.service
    item = next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:"))
    alice.db.execute("CREATE TRIGGER fail_sent BEFORE UPDATE OF status ON outbox WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT,'simulated commit failure'); END")
    alice.db.commit()
    sent = []
    with pytest.raises(Exception):
        service.send_approved_vcard(item["id"], lambda *_: sent.append(True))
    alice.db.execute("DROP TRIGGER fail_sent"); alice.db.commit()
    service.mark_outbox_failed(item["id"], "simulated")
    assert not service.send_approved_vcard(item["id"], lambda *_: sent.append(True))
    assert sent == [True]
    assert alice.db.execute("SELECT status FROM outbox WHERE id=?", (item["id"],)).fetchone()[0] == "sending"


@pytest.mark.parametrize("change", ["block", "leave", "expire", "contact", "network"])
def test_queued_exchange_rechecked_before_delivery(clients, change):
    alice, bob = clients
    alice.join(); bob.join()
    public_id = start_request_with_current_contact(alice, bob)
    bob.call(command="同意申请", value=public_id); bob.reply("1")
    service = alice.bridge.service
    item = next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:"))
    if change == "block":
        alice.call(command="屏蔽", value=bob.snapshot()["profile"]["member_code"])
    elif change == "leave":
        alice.call(command="退出网络"); alice.reply("1")
    elif change == "contact":
        with service.db.transaction() as conn:
            conn.execute("UPDATE memberships SET share_contact='changed@example.org',share_version=share_version+1 WHERE user_id=(SELECT id FROM users WHERE actor_key='alice')")
    else:
        with service.db.transaction() as conn:
            if change == "expire":
                conn.execute("UPDATE contact_requests SET expires_at='2000-01-01T00:00:00+00:00'")
            else:
                conn.execute("UPDATE networks SET status='disabled'")
    assert not service.send_approved_vcard(item["id"], lambda *_: pytest.fail("invalid exchange"))


@pytest.mark.parametrize("ending", ["withdraw", "block", "expire", "leave"])
def test_invalidated_request_cannot_be_accepted(clients, ending):
    alice, bob = clients
    alice.join(); bob.join()
    public_id = start_request(alice, bob)
    bob.call(command="同意申请", value=public_id)
    step = bob.snapshot()["pending"]["token"]
    if ending == "withdraw":
        alice.call(command="撤销申请", value=public_id)
    elif ending == "block":
        alice.call(command="屏蔽", value=bob.snapshot()["profile"]["member_code"])
    elif ending == "leave":
        alice.call(command="退出网络"); alice.reply("1")
    else:
        with alice.bridge.service.db.transaction() as conn:
            conn.execute("UPDATE contact_requests SET expires_at='2000-01-01T00:00:00+00:00'")
    bob.call("reply", step=step, value="1")
    assert alice.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE 'exchange:%'").fetchone()[0] == 0


def test_isolation_hidden_profile_and_deleted_snapshot(clients):
    alice, bob = clients
    alice.join(); bob.join(visible=False)
    alice.call("search", value="bob")
    assert alice.snapshot()["choices"] == []
    alice.bridge.service.seed_network("另一个网络", "another-private-code")
    bob.join("another-private-code")
    bob.call(command="公开资料")
    alice.call("search", value="bob")
    assert alice.snapshot()["choices"] == []
    alice.call(command="删除我的数据"); alice.reply("1")
    snapshot = alice.snapshot()
    assert snapshot["profile"] is None
    assert snapshot["networks"] == snapshot["requests"] == snapshot["choices"] == []
    assert "alice@example.org" not in json.dumps(snapshot)


def test_replacement_preserves_old_instance(clients):
    alice, _ = clients
    alice.join()
    stale = alice.call("refresh")
    alice.bridge.offer(alice.incoming, replace=True)
    count = alice.db.execute("SELECT COUNT(*) FROM xdc_requests").fetchone()[0]
    data = json.loads(stale.text[len(PREFIX):]); data["id"] = "new-id"
    alice.bridge.handle(replace(stale, text=PREFIX + json.dumps(data)))
    assert alice.db.execute("SELECT COUNT(*) FROM xdc_requests").fetchone()[0] == count + 1


def test_existing_vcard_request_uses_vcard_acceptance(clients):
    alice, bob = clients
    alice.join(); bob.join()
    with alice.bridge.service.db.transaction() as conn:
        user = conn.execute("SELECT * FROM users WHERE actor_key='alice'").fetchone()
        target = conn.execute("SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='bob'").fetchone()[0]
        from contactbot.service import _now
        alice.bridge.service._confirm_vcard_request(conn, user, {"target_id": target}, _now(), "test-vcard")
    public_id = alice.db.execute("SELECT public_id FROM contact_requests").fetchone()[0]
    bob.call(command="同意申请", value=public_id)
    row = alice.db.execute("SELECT vcard_contact_id FROM outbox WHERE dedupe_key LIKE 'vcard:%'").fetchone()
    assert row[0] == alice.incoming.contact_id
    assert alice.db.execute("SELECT status FROM contact_requests").fetchone()[0] == "processing"


def test_sync_checks_verified_private_chat_and_skips_failed_instance(clients):
    alice, bob = clients
    alice.join(); bob.join()
    bridge = alice.bridge
    bridge.require_verified = True
    bridge.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    bridge.rpc.get_contact.return_value = SimpleNamespace(is_verified=False)
    bridge.rpc.send_webxdc_status_update.reset_mock()
    bridge.sync_all(1)
    bridge.rpc.send_webxdc_status_update.assert_not_called()
    bridge.rpc.get_contact.return_value.is_verified = True
    bridge.rpc.get_basic_chat_info.side_effect = [RuntimeError("unavailable"), SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)]
    bridge._last_sync.clear()
    bridge.sync_all(1)
    bridge.rpc.send_webxdc_status_update.assert_called_once()


def test_adapter_routes_private_app_messages(clients):
    alice, _ = clients
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = alice.bridge.rpc
    adapter.service = alice.bridge.service
    adapter.webxdc = alice.bridge
    adapter._outbox_lock = threading.Lock()
    adapter._sync_webxdc = Mock()
    adapter._drain_outbox_locked = Mock()
    adapter.settings = SimpleNamespace(actor_key=lambda _: "alice", require_verified=True,
                                      is_bootstrap_admin=lambda _: False,
                                      is_bootstrap_admin_contact=lambda _: False)
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    event = SimpleNamespace(msg=SimpleNamespace(id=10, chat_id=110, from_id=10, text="小程序", show_padlock=True, sender=SimpleNamespace(address="alice@example.org", is_verified=True)))
    adapter._on_message(None, 1, event)
    assert adapter.rpc.send_msg.call_args.args[2].file.endswith("好友助手.xdc")
    assert alice.db.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0


@pytest.mark.parametrize("address,verified,strict", [("new@example.org", True, True), ("new@relay-one.invalid", False, False), ("new@relay-two.invalid", False, False)])
def test_each_encrypted_message_offers_app_without_text_guidance(clients, address, verified, strict):
    alice, _ = clients
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = alice.bridge.rpc; adapter.service = alice.bridge.service; adapter.webxdc = alice.bridge
    adapter._outbox_lock = threading.Lock(); adapter._sync_webxdc = Mock(); adapter._drain_outbox_locked = Mock()
    adapter.settings = SimpleNamespace(actor_key=lambda _: "new-user", require_verified=strict,
        is_bootstrap_admin=lambda _: False, is_bootstrap_admin_contact=lambda _: False)
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    adapter.rpc.send_msg.reset_mock()
    event = SimpleNamespace(msg=SimpleNamespace(id=321, chat_id=333, from_id=222, text="你好", show_padlock=True, sender=SimpleNamespace(address=address, is_verified=verified)))
    adapter._on_message(None, 1, event)
    adapter._on_message(None, 1, event)
    assert adapter.rpc.send_msg.call_count == 1
    assert adapter.rpc.send_msg.call_args.args[2].file.endswith("好友助手.xdc")
    event.msg.id += 1
    event.msg.text = "任意文字"
    adapter._on_message(None, 1, event)
    assert adapter.rpc.send_msg.call_count == 2
    assert alice.db.execute("SELECT count(*) FROM xdc_instances WHERE actor_key='new-user'").fetchone()[0] == 2
    assert alice.db.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert alice.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_startup_recovery_offers_one_app_to_recent_missing_chat(clients):
    import time
    alice, _ = clients
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = alice.bridge.rpc; adapter.service = alice.bridge.service; adapter.bot = Mock(); adapter.log = Mock()
    recent = SimpleNamespace(id=1, chat_id=999, from_id=222, is_info=False, is_bot=False, received_timestamp=time.time())
    older_same = SimpleNamespace(**{**vars(recent), "id": 2})
    old = SimpleNamespace(**{**vars(recent), "id": 3, "chat_id": 998, "received_timestamp": time.time()-90000})
    existing = SimpleNamespace(**{**vars(recent), "id": 4, "chat_id": alice.incoming.chat_id})
    adapter.rpc.get_fresh_msgs.return_value = [1, 2, 3, 4]
    adapter.rpc.get_message.side_effect = [recent, older_same, old, existing]
    adapter._on_message = Mock()
    adapter._recover_recent_contacts(1)
    assert adapter._on_message.call_count == 1
    assert adapter._on_message.call_args.args[2].msg.id == 1


@pytest.mark.parametrize("chat_encrypted,signed,strict", [(False, False, False), (True, False, False), (False, True, False), (True, True, True)])
def test_insecure_or_explicitly_strict_message_does_not_offer_app(clients, chat_encrypted, signed, strict):
    alice, _ = clients
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = alice.bridge.rpc; adapter.service = alice.bridge.service; adapter.webxdc = alice.bridge
    adapter._outbox_lock = threading.Lock()
    adapter.settings = SimpleNamespace(actor_key=lambda _: "new-user", require_verified=strict,
        is_bootstrap_admin=lambda _: False, is_bootstrap_admin_contact=lambda _: False)
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=chat_encrypted)
    adapter.rpc.send_msg.reset_mock()
    event = SimpleNamespace(msg=SimpleNamespace(id=322, chat_id=333, from_id=222, text="你好", show_padlock=signed, sender=SimpleNamespace(address="new@example.org", is_verified=False)))
    adapter._on_message(None, 1, event)
    adapter.rpc.send_msg.assert_not_called()
    assert alice.db.execute("SELECT count(*) FROM users WHERE actor_key='new-user'").fetchone()[0] == 0


def native_envelope(client, request, *, token=None):
    token = token or client.token
    nonce = secrets.token_bytes(12)
    key = hashlib.sha256(("friend-assistant-v2:" + token).encode()).digest()
    encrypted = AESGCM(key).encrypt(nonce, json.dumps(request).encode(), b"contact-action-v2")
    return {"type": "contact-action-v2", "nonce": base64.b64encode(nonce).decode(), "body": base64.b64encode(encrypted).decode()}


def consume(client, updates):
    instance = client.db.execute("SELECT * FROM xdc_instances WHERE token=?", (client.token,)).fetchone()
    client.bridge.rpc.get_webxdc_status_updates.return_value = json.dumps(updates)
    client.bridge.consume_updates(instance, client.incoming)


def test_native_update_ack_and_restart_replay(clients):
    alice, _ = clients
    alice.join()
    request = {"token": alice.token, "id": "native-one", "action": "command", "command": "编辑昵称", "value": "直接操作", "network": alice.snapshot()["current_network"]}
    envelope = native_envelope(alice, request)
    consume(alice, [{"serial": 1, "payload": envelope}])
    assert alice.snapshot()["profile"]["nickname"] == "直接操作"
    assert alice.snapshot()["receipt"] == {"request_id": "native-one", "ok": 1}
    # A duplicate request in a new status serial is still exactly-once.
    alice.bridge = WebxdcBridge(alice.bridge.rpc, alice.bridge.service)
    consume(alice, [{"serial": 2, "payload": envelope}])
    assert alice.db.execute("SELECT COUNT(*) FROM xdc_requests WHERE request_id='native-one'").fetchone()[0] == 1
    assert alice.db.execute("SELECT serial FROM xdc_cursors").fetchone()[0] == 2


def test_native_wrong_key_tamper_and_other_instance_cannot_authorize(clients):
    alice, bob = clients
    alice.join(); bob.join()
    request = {"token": alice.token, "id": "attack", "action": "command", "command": "编辑昵称", "value": "不应修改", "network": alice.snapshot()["current_network"]}
    wrong_key = native_envelope(alice, request, token=bob.token)
    correct = native_envelope(alice, request)
    damaged = {**correct, "body": "AAAA"}
    consume(alice, [{"serial": 1, "payload": wrong_key}, {"serial": 2, "payload": damaged}, {"serial": 3, "payload": {"type": "contact-state", "command": "编辑昵称"}}])
    consume(bob, [{"serial": 1, "payload": correct}])
    assert alice.snapshot()["profile"]["nickname"] == "alice"
    assert bob.snapshot()["profile"]["nickname"] == "bob"
    assert alice.db.execute("SELECT COUNT(*) FROM xdc_requests WHERE request_id='attack'").fetchone()[0] == 0


def test_native_error_receipt(clients):
    alice, _ = clients
    alice.join()
    request = {"token": alice.token, "id": "no-permission", "action": "command", "command": "创建网络", "value": "越权", "network": alice.snapshot()["current_network"]}
    consume(alice, [{"serial": 1, "payload": native_envelope(alice, request)}])
    assert alice.snapshot()["receipt"] == {"request_id": "no-permission", "ok": 0}
    assert alice.db.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1


def test_full_flow_over_native_updates(clients):
    alice, bob = clients

    def call(client, action="command", **extra):
        row = client.db.execute("SELECT current_network_id FROM users WHERE actor_key=?", (client.incoming.actor_key,)).fetchone()
        request = {"id": secrets.token_hex(12), "token": client.token, "network": row[0] if row else None, "action": action, **extra}
        instance = client.db.execute("SELECT message_id FROM xdc_instances WHERE token=?", (client.token,)).fetchone()[0]
        cursor = client.db.execute("SELECT serial FROM xdc_cursors WHERE account_id=1 AND message_id=?", (instance,)).fetchone()
        consume(client, [{"serial": cursor[0] + 1 if cursor else 1, "payload": native_envelope(client, request)}])

    alice.call = lambda action="command", **extra: call(alice, action, **extra)
    bob.call = lambda action="command", **extra: call(bob, action, **extra)
    alice.join(); bob.join()
    alice.call("search", value="bob")
    found = alice.snapshot()["choices"][0]
    alice.call(command="申请", value=found["member_code"] + " 认识一下")
    alice.reply("1")
    request_id = bob.snapshot()["requests"][0]["public_id"]
    bob.call(command="同意申请", value=request_id)
    assert bob.snapshot()["pending"]["contact"] == "bob@example.org"
    bob.reply("1")
    assert alice.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE 'exchange:%'").fetchone()[0] == 2


def test_directory_pagination_isolation_and_current_visibility(clients):
    from contactbot.service import _now

    alice, bob = clients
    alice.join(); bob.join()
    with alice.bridge.service.db.transaction() as conn:
        network_id = conn.execute("SELECT current_network_id FROM users WHERE actor_key='alice'").fetchone()[0]
        for i in range(16):
            user = conn.execute("INSERT INTO users(actor_key,contact_id,chat_id,created_at,updated_at) VALUES(?,?,?,?,?)", (f"person-{i}", 500+i, 600+i, _now(), _now())).lastrowid
            conn.execute("INSERT INTO memberships(network_id,user_id,member_code,nickname,discoverable,accepts_requests,share_contact,created_at,updated_at) VALUES(?,?,?,?,1,1,?,?,?)", (network_id, user, f"MFAKE{i}", f"person-{i:02}", f"secret-{i}@example.org", _now(), _now()))
        other = alice.bridge.service._public_id("N", 6)
        other_network = conn.execute("INSERT INTO networks(public_id,name,join_code_hash,join_code_hint,created_at,updated_at) VALUES(?,?,?,?,?,?)", (other, "其他网络", "separate-hash", "hint", _now(), _now())).lastrowid
        outsider = conn.execute("INSERT INTO users(actor_key,contact_id,chat_id,created_at,updated_at) VALUES(?,?,?,?,?)", ("outsider", 999, 999, _now(), _now())).lastrowid
        conn.execute("INSERT INTO memberships(network_id,user_id,member_code,nickname,discoverable,accepts_requests,created_at,updated_at) VALUES(?,?,?,?,1,1,?,?)", (other_network, outsider, "MOTHER", "隔离成员", _now(), _now()))
    alice.call("directory", page=0)
    first = alice.snapshot()["directory"]
    assert len(first["members"]) == 12 and first["has_more"]
    assert "secret-" not in json.dumps(first)
    alice.call("directory", page=1)
    second = alice.snapshot()["directory"]
    assert len(second["members"]) == 5 and not second["has_more"]
    assert not {m["member_code"] for m in first["members"]} & {m["member_code"] for m in second["members"]}
    assert "MOTHER" not in {m["member_code"] for m in first["members"] + second["members"]}
    code = bob.snapshot()["profile"]["member_code"]
    bob.call(command="隐藏资料")
    alice.call(command="申请", value=code + " 你好")
    assert alice.db.execute("SELECT COUNT(*) FROM contact_requests").fetchone()[0] == 0
    alice.call("directory", page=0)
    assert code not in {m["member_code"] for m in alice.snapshot()["directory"]["members"]}
    alice.call(command="退出网络"); alice.reply("1")
    assert alice.snapshot()["directory"] is None


def test_notification_routing_requires_new_app_registration(clients):
    alice, _ = clients
    alice.join()
    with alice.bridge.service.db.transaction() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE actor_key='alice'").fetchone()[0]
        alice.bridge.service._reply(conn, user_id, "需要保密的联系人信息", "request-notify:test", sensitive=True)
    item = next(i for i in alice.bridge.service.pending_outbox() if i["dedupe_key"].startswith("request-notify:test"))
    with pytest.raises(RuntimeError):
        alice.bridge.deliver_outbox(1, item)
    alice.call("refresh", notification_version=1)
    alice.bridge.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    assert alice.bridge.deliver_outbox(1, item) is True
    update = json.loads(alice.bridge.rpc.send_webxdc_status_update.call_args.args[2])
    assert update["payload"]["body"] == "需要保密的联系人信息"
    assert update["notify"] == {"*": "好友助手有新消息，点击查看"}
    assert "保密" not in json.dumps(update["notify"], ensure_ascii=False)
    item_with_vcard = dict(item); item_with_vcard["vcard_contact_id"] = 5
    assert alice.bridge.deliver_outbox(1, item_with_vcard) is False
    alice.bridge.require_verified = True
    alice.bridge.rpc.get_contact.return_value = SimpleNamespace(is_verified=False)
    with pytest.raises(RuntimeError):
        alice.bridge.deliver_outbox(1, item)


def test_notification_failure_stays_queued_without_chat_fallback(clients):
    alice, _ = clients
    alice.join(); alice.call("refresh", notification_version=1)
    for row in alice.bridge.service.pending_outbox():
        alice.bridge.service.mark_outbox_sent(row["id"])
    with alice.bridge.service.db.transaction() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE actor_key='alice'").fetchone()[0]
        alice.bridge.service._reply(conn, user_id, "新消息", "request-notify:fail", sensitive=True)
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = alice.bridge.rpc; adapter.service = alice.bridge.service; adapter.webxdc = alice.bridge
    adapter.log = Mock(); adapter._send_outbox_item = Mock()
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    adapter.rpc.send_webxdc_status_update.side_effect = RuntimeError("offline")
    adapter._drain_outbox_locked(1)
    adapter._send_outbox_item.assert_not_called()
    pending = alice.bridge.service.pending_outbox()
    assert len(pending) == 1 and pending[0]["attempts"] == 1


def test_unopened_app_waits_without_chat_delivery(clients):
    alice, _ = clients
    alice.join()
    for row in alice.bridge.service.pending_outbox():
        alice.bridge.service.mark_outbox_sent(row["id"])
    with alice.bridge.service.db.transaction() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE actor_key='alice'").fetchone()[0]
        alice.bridge.service._reply(conn, user_id, "新消息", "request-notify:chat", sensitive=True)
    adapter = object.__new__(DeltaAdapter)
    adapter.rpc = alice.bridge.rpc; adapter.service = alice.bridge.service; adapter.webxdc = alice.bridge
    adapter.log = Mock(); adapter._send_outbox_item = Mock()
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(is_encrypted=True)
    adapter._drain_outbox_locked(1)
    adapter._send_outbox_item.assert_not_called()
    assert len(alice.bridge.service.pending_outbox()) == 1
    alice.call("refresh", notification_version=1)
    adapter.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    adapter._drain_outbox_locked(1)
    adapter._send_outbox_item.assert_not_called()
    assert not alice.bridge.service.pending_outbox()


def friend_link(address):
    from urllib.parse import quote
    return "https://i.delta.chat/#" + "A" * 40 + "&v=3&i=invite&s=auth&a=" + quote(address, safe="")


def test_extracts_own_friend_link_and_keeps_contact_version_on_repeat(clients):
    alice, _ = clients
    alice.join()
    before = alice.db.execute("SELECT share_version FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'").fetchone()[0]
    alice.call(command="设置联系方式", value="这是我的链接：" + friend_link("alice@example.org"))
    row = alice.db.execute("SELECT share_contact,share_version FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'").fetchone()
    assert tuple(row) == ("alice@example.org", before)
    assert "i.delta.chat" not in json.dumps(alice.snapshot())


@pytest.mark.parametrize("value", [friend_link("bob@example.org"), "https://i.delta.chat/#invalid&a=alice%40example.org", "https://i.delta.chat.evil/#" + "A" * 40 + "&v=3&i=x&s=y&a=alice%40example.org"])
def test_rejects_other_accounts_and_invalid_friend_links(clients, value):
    alice, _ = clients
    alice.join()
    before = alice.snapshot()["profile"]["share_contact"]
    alice.call(command="设置联系方式", value=value)
    snapshot = alice.snapshot()
    assert snapshot["profile"]["share_contact"] == before
    assert snapshot["receipt"]["ok"] == 0


def test_native_sync_uses_bound_core_contact_as_identity(clients):
    alice, _ = clients
    alice.join()
    bridge = alice.bridge
    bridge.require_verified = True
    bridge.identity_key = lambda address: address.split("@")[0]
    bridge.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    bridge.rpc.get_contact.return_value = SimpleNamespace(is_verified=True, address="alice@example.org")
    bridge.rpc.get_webxdc_status_updates.return_value = "[]"
    bridge._last_sync.clear()
    bridge.rpc.send_webxdc_status_update.reset_mock()
    bridge.sync_all(1)
    payload = json.loads(bridge.rpc.send_webxdc_status_update.call_args.args[2])["payload"]
    assert payload["profile"]["communication_address"] == "alice@example.org"
    bridge.rpc.get_contact.return_value.address = "bob@example.org"
    bridge._last_sync.clear()
    bridge.rpc.send_webxdc_status_update.reset_mock()
    bridge.sync_all(1)
    bridge.rpc.send_webxdc_status_update.assert_not_called()


def test_personal_app_message_has_no_description(clients):
    alice, _ = clients
    data = alice.bridge.rpc.send_msg.call_args.args[2]
    assert not data.text


def test_locally_extracted_link_account_is_verified_server_side(clients):
    alice, _ = clients
    alice.join()
    alice.call(command="设置联系方式", value="bob@example.org", verify_current_account=True)
    assert alice.snapshot()["receipt"]["ok"] == 0
    assert alice.snapshot()["profile"]["share_contact"] == "alice@example.org"


def test_native_sync_accepts_extracted_account_from_verified_core_contact(clients):
    alice, _ = clients
    alice.join()
    bridge = alice.bridge
    bridge.require_verified = True
    bridge.identity_key = lambda address: address.split("@")[0]
    bridge.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=ChatType.SINGLE, is_device_chat=False, is_self_talk=False, is_encrypted=True)
    bridge.rpc.get_contact.return_value = SimpleNamespace(is_verified=True, address="alice@example.org")
    with bridge.service.db.transaction() as conn:
        conn.execute("UPDATE memberships SET share_contact=NULL WHERE user_id=(SELECT id FROM users WHERE actor_key='alice')")
    request = {"token": alice.token, "id": "native-contact", "action": "command", "command": "设置联系方式", "value": "alice@example.org", "verify_current_account": True, "network": alice.snapshot()["current_network"]}
    bridge.rpc.get_webxdc_status_updates.return_value = json.dumps([{"serial": 1, "payload": native_envelope(alice, request)}])
    bridge.sync_all(1)
    row = alice.db.execute("SELECT share_contact FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'").fetchone()
    assert row[0] == "alice@example.org"
    assert alice.snapshot()["receipt"] == {"request_id": "native-contact", "ok": 1}
    alice.call(command="设置联系方式", value="alice@example.org", verify_current_account=True)
    assert alice.snapshot()["receipt"]["ok"] == 1
    assert alice.snapshot()["profile"]["share_contact"] == "alice@example.org"


def test_only_own_matching_chat_apps_get_blank_summary(clients):
    alice, _ = clients
    bridge = alice.bridge
    bridge.rpc.get_chat_media.return_value = [1, 2, 3, 4, 5]
    bridge.rpc.get_message.side_effect = [
        SimpleNamespace(chat_id=110, from_id=1, file_name="好友助手.xdc"),
        SimpleNamespace(chat_id=110, from_id=1, file_name="好友助手.xdc"),
        SimpleNamespace(chat_id=110, from_id=1, file_name="其他应用.xdc"),
        SimpleNamespace(chat_id=110, from_id=10, file_name="好友助手.xdc"),
        SimpleNamespace(chat_id=999, from_id=1, file_name="好友助手.xdc"),
    ]
    bridge.rpc.get_webxdc_info.side_effect = [
        SimpleNamespace(name="好友助手", is_app_sender=True, summary=None),
        SimpleNamespace(name="好友助手", is_app_sender=True, summary="\u00a0"),
    ]
    bridge.rpc.send_webxdc_status_update.reset_mock()
    bridge.clear_previous_summaries(alice.incoming)
    assert bridge.rpc.send_webxdc_status_update.call_count == 1
    args = bridge.rpc.send_webxdc_status_update.call_args.args
    assert args[1] == 1 and json.loads(args[2])["summary"] == "\u00a0"
    bridge.clear_previous_summaries(alice.incoming)
    assert bridge.rpc.send_webxdc_status_update.call_count == 1

def test_directory_clamps_stale_page_without_exposing_hidden_members(clients):
    alice, bob = clients
    alice.join(); bob.join()
    with alice.bridge.service.db.transaction() as conn:
        network_id = conn.execute("SELECT current_network_id FROM users WHERE actor_key='alice'").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO xdc_directory VALUES(?,?,?)", (alice.token, network_id, 999))
    state = alice.snapshot()["directory"]
    assert state["page"] == 0
    assert [m["nickname"] for m in state["members"]] == ["bob"]

def test_reissue_only_completed_exchange_and_still_checks_blocks(clients):
    from contactbot.service import APPROVED_CONTACT_VCARD
    alice, bob = clients
    alice.join(); bob.join()
    public_id = start_request_with_current_contact(alice, bob)
    bob.call(command="同意申请", value=public_id); bob.reply("1")
    service = alice.bridge.service
    original = [r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:")]
    for item in original:
        assert service.send_approved_vcard(item["id"], lambda *_: None)
    assert alice.db.execute("SELECT status FROM contact_requests WHERE public_id=?", (public_id,)).fetchone()[0] == "completed"
    user_id = alice.db.execute("SELECT id FROM users WHERE actor_key='alice'").fetchone()[0]
    with service.db.transaction() as conn:
        service._reply(conn, user_id, "new card", f"reissue:{public_id}:requester", sensitive=True, vcard_contact_id=APPROVED_CONTACT_VCARD)
    item = next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("reissue:"))
    delivered = []
    assert service.send_approved_vcard(item["id"], lambda _, address, name: delivered.append(address))
    assert delivered == ["bob@example.org"]
    with service.db.transaction() as conn:
        service._reply(conn, user_id, "new card", f"reissue:{public_id}:target", sensitive=True, vcard_contact_id=APPROVED_CONTACT_VCARD)
    item = next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("reissue:"))
    assert not service.send_approved_vcard(item["id"], lambda *_: pytest.fail("wrong recipient"))
    assert alice.db.execute("SELECT status FROM contact_requests WHERE public_id=?", (public_id,)).fetchone()[0] == "completed"

def test_direct_exchange_obeys_disabled_setting_and_revocation(clients):
    alice, bob = clients
    alice.join(); bob.join()
    assert alice.snapshot()["profile"]["auto_share"] and bob.snapshot()["profile"]["auto_share"]
    alice.call("auto_share", enabled=False); bob.call("auto_share", enabled=False)
    code = bob.snapshot()["profile"]["member_code"]
    alice.call("exchange_now", value=code)
    assert not alice.snapshot()["receipt"]["ok"]
    alice.call("auto_share", enabled=True)
    alice.call("exchange_now", value=code)
    assert not alice.snapshot()["receipt"]["ok"]
    bob.call("auto_share", enabled=True)
    alice.call("exchange_now", value=code)
    assert alice.snapshot()["receipt"]["ok"]
    cards = [r for r in alice.bridge.service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:")]
    assert len(cards) == 2
    bob.call("auto_share", enabled=False)
    assert not alice.bridge.service.send_approved_vcard(cards[0]["id"], lambda *_: pytest.fail("revoked consent"))
    assert alice.db.execute("SELECT status FROM contact_requests ORDER BY id DESC LIMIT 1").fetchone()[0] == "invalid"


def test_direct_exchange_replaces_pending_only_when_both_enabled(clients):
    alice, bob = clients
    alice.join(); bob.join()
    bob.call("auto_share", enabled=False)
    old_id = start_request_with_current_contact(alice, bob)
    alice.call("auto_share", enabled=True)
    code = bob.snapshot()["profile"]["member_code"]
    alice.call("exchange_now", value=code)
    assert not alice.snapshot()["receipt"]["ok"]
    assert alice.db.execute("SELECT status FROM contact_requests WHERE public_id=?",(old_id,)).fetchone()[0] == "pending_target"
    bob.call("auto_share", enabled=True)
    alice.call("exchange_now", value=code)
    assert alice.snapshot()["receipt"]["ok"]
    assert alice.db.execute("SELECT status FROM contact_requests WHERE public_id=?",(old_id,)).fetchone()[0] == "invalid"
    assert alice.db.execute("SELECT count(*) FROM contact_requests WHERE status='processing'").fetchone()[0] == 1


def test_existing_vcard_confirmation_can_be_submitted_from_modal(clients):
    from contactbot.service import _now
    alice, bob = clients
    alice.join(); bob.join()
    with alice.bridge.service.db.transaction() as conn:
        user = conn.execute("SELECT * FROM users WHERE actor_key='alice'").fetchone()
        target = conn.execute("SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='bob'").fetchone()[0]
        alice.bridge.service._pending(conn, user["id"], "send_vcard_request", {"target_id": target}, _now())
    alice.reply("1")
    state = alice.snapshot()
    assert state["receipt"]["ok"], state["messages"]
    assert alice.db.execute("SELECT request_kind FROM contact_requests").fetchone()[0] == "vcard"


@pytest.mark.parametrize("stage", ["code", "nickname", "confirm"])
def test_existing_member_can_cancel_join_without_leaving_original_network(clients, stage):
    alice, _ = clients
    alice.join()
    old_network = alice.snapshot()["current_network"]
    alice.bridge.service.seed_network("新网络", "new-network-code")
    alice.call("join")
    if stage != "code":
        alice.reply("new-network-code")
    if stage == "confirm":
        alice.reply("新昵称")
    alice.call(command="取消")
    state = alice.snapshot()
    assert state["receipt"]["ok"]
    assert state["current_network"] == old_network
    assert not state["pending"] or state["pending"]["action"] not in {"guide_join_code", "guide_nickname", "join"}
    assert len(state["networks"]) == 1


@pytest.mark.parametrize("success", [True, False])
def test_duplicate_request_republishes_original_result_without_reexecution(clients, success):
    alice, _ = clients
    alice.join()
    alice.bridge.service.seed_network("第二网络", "second-test-code")
    request = alice.call("join", value="second-test-code" if success else "invalid-code")
    request_id = json.loads(request.text[len(PREFIX):])["id"]
    first = alice.snapshot()
    assert bool(first["receipt"]["ok"]) == success
    pending_before = first["pending"]
    attempts = alice.db.execute("SELECT count(*) FROM rate_events WHERE category='join_attempt'").fetchone()[0]
    alice.call("refresh")
    assert alice.snapshot()["receipt"]["request_id"] != request_id
    # A cached prior snapshot must not suppress the resent acknowledgement.
    alice.bridge.rpc.send_webxdc_status_update.reset_mock()
    alice.bridge.handle(request)
    alice.bridge.sync(alice.incoming)
    resent = json.loads(alice.bridge.rpc.send_webxdc_status_update.call_args.args[2])["payload"]
    assert resent["receipt"] == {"request_id": request_id, "ok": int(success)}
    assert resent["pending"] == pending_before
    assert alice.db.execute("SELECT count(*) FROM rate_events WHERE category='join_attempt'").fetchone()[0] == attempts


def test_legacy_duplicate_with_unknown_result_is_not_reexecuted(clients):
    alice, _ = clients
    alice.join()
    request = alice.call("join", value="wrong-code")
    request_id = json.loads(request.text[len(PREFIX):])["id"]
    alice.call("refresh")
    with alice.bridge.service.db.transaction() as conn:
        conn.execute("DELETE FROM xdc_request_results WHERE token=? AND request_id=?", (alice.token, request_id))
    before = alice.db.execute("SELECT count(*) FROM rate_events WHERE category='join_attempt'").fetchone()[0]
    alice.bridge.handle(request)
    state = alice.snapshot()
    assert state["receipt"] == {"request_id": request_id, "ok": 0}
    assert alice.db.execute("SELECT count(*) FROM rate_events WHERE category='join_attempt'").fetchone()[0] == before


def test_cancel_join_is_scoped_to_exact_step_and_preserves_networks(clients):
    alice, _ = clients
    alice.join()
    old = alice.snapshot()["current_network"]
    alice.bridge.service.seed_network("第二网络", "second-network-code")
    alice.call("join")
    stale = alice.snapshot()["pending"]["token"]
    alice.reply("second-network-code")
    current = alice.snapshot()["pending"]["token"]
    alice.call("cancel_join", step=stale)
    assert not alice.snapshot()["receipt"]["ok"]
    assert alice.snapshot()["pending"]["token"] == current
    alice.call("cancel_join", step=current)
    assert alice.snapshot()["receipt"]["ok"]
    assert alice.snapshot()["pending"] is None
    assert alice.snapshot()["current_network"] == old
    assert len(alice.snapshot()["networks"]) == 1


def test_stale_network_can_refresh_but_cannot_mutate_other_network(clients):
    alice, _ = clients
    alice.join()
    old = alice.snapshot()["current_network"]
    alice.bridge.service.seed_network("第二网络", "second-network-code")
    alice.join("second-network-code")
    new = alice.snapshot()["current_network"]
    alice.call("refresh", network=old)
    assert alice.snapshot()["receipt"]["ok"]
    assert alice.snapshot()["current_network"] == new
    alice.call(command="隐藏资料", network=old)
    assert not alice.snapshot()["receipt"]["ok"]
    assert alice.snapshot()["profile"]["discoverable"]
    assert len(alice.snapshot()["networks"]) == 2


def test_snapshot_keeps_recent_request_results_for_reconciliation(clients):
    alice, _ = clients
    alice.join()
    old = alice.call('refresh')
    old_id = json.loads(old.text[len(PREFIX):])['id']
    alice.call('refresh')
    state = alice.snapshot()
    assert state['receipt']['request_id'] != old_id
    assert {'request_id': old_id, 'ok': 1} in state['recent_receipts']
