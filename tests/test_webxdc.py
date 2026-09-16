import json
from dataclasses import replace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from contactbot.database import Database
from contactbot.service import BotService, IncomingMessage
from contactbot.webxdc_bridge import PREFIX, WebxdcBridge, package, package_p2p


@pytest.fixture
def bridge(tmp_path):
    db = Database(tmp_path / "xdc.db")
    db.initialize()
    rpc = Mock()
    rpc.send_msg.return_value = 44
    service = BotService(db, b"test-secret-that-is-at-least-32-bytes")
    bridge = WebxdcBridge(rpc, service)
    incoming = IncomingMessage(1, 1, "alice", 2, 3, "", True, True, contact_address="alice@example.org")
    bridge.offer(incoming)
    token = db.connection.execute("SELECT token FROM xdc_instances").fetchone()[0]
    yield bridge, incoming, token
    db.close()


def request(incoming, token, **values):
    data = {"token": token, "id": "one", "action": "start", "network": None, **values}
    return replace(incoming, text=PREFIX + json.dumps(data))


def test_package(tmp_path):
    path = tmp_path / "contacts.xdc"
    package(path)
    with ZipFile(path) as z:
        assert set(z.namelist()) == {"index.html", "app.js", "style.css", "manifest.toml", "icon.png", "config.js"}
        assert b'"token": ""' in z.read("config.js")
        assert b"fetch(" not in z.read("app.js")


def test_p2p_package_has_no_service_configuration(tmp_path):
    path = tmp_path / "contacts-p2p.xdc"
    package_p2p(path)
    with ZipFile(path) as archive:
        assert set(archive.namelist()) == {"index.html", "app.js", "style.css", "manifest.toml"}
        source = archive.read("app.js")
        assert b"sendToChat" not in source
        assert b"sendUpdate" in source
        assert b"fetch(" not in source
        assert b'event.kind==="join"' in source
        assert b"event.body.signPublic" in source


@pytest.mark.parametrize("changes", [{"actor_key": "bob"}, {"chat_id": 99}, {"account_id": 9}, {"is_encrypted": False}, {"is_private": False}])
def test_bound_to_real_sender(bridge, changes):
    b, incoming, token = bridge
    b.handle(request(replace(incoming, **changes), token))
    assert b.service.db.connection.execute("SELECT COUNT(*) FROM xdc_requests").fetchone()[0] == 0


def test_replay_and_stale_confirmation(bridge):
    b, incoming, token = bridge
    message = request(incoming, token)
    b.handle(message)
    db = b.service.db.connection
    pending = db.execute("SELECT token FROM pending_actions").fetchone()[0]
    b.handle(message)
    assert db.execute("SELECT token FROM pending_actions").fetchone()[0] == pending
    b.handle(request(incoming, token, id="two", action="reply", step="stale", value="test"))
    assert db.execute("SELECT token FROM pending_actions").fetchone()[0] == pending
    b.sync(incoming)
    payload = json.loads(b.rpc.send_webxdc_status_update.call_args.args[2])["payload"]
    assert payload["pending"]["token"] == pending
    assert "payload" not in payload["pending"]


def test_malformed_command_and_network(bridge):
    b, incoming, token = bridge
    b.handle(request(incoming, token, action="command", command=[]))
    b.handle(request(incoming, token, id="two", network=99))
    assert b.service.db.connection.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0


def test_join_wizard(bridge):
    b, incoming, token = bridge
    admin = replace(incoming, actor_key="admin", contact_id=10, chat_id=11, bootstrap_admin=True,
                    text="创建网络 测试网络", message_id=100)
    b.service.handle(admin)
    b.service.handle(replace(admin, message_id=101, text="中文 空格 🌿"))
    b.handle(request(incoming, token))
    for i, value in enumerate(["中文 空格 🌿", "小林", "1", "4"], 2):
        pending = b.service.db.connection.execute("SELECT token FROM pending_actions WHERE user_id=(SELECT id FROM users WHERE actor_key='alice')").fetchone()[0]
        b.handle(request(incoming, token, id=str(i), action="reply", step=pending, value=value,
                         network=b.service.db.connection.execute("SELECT current_network_id FROM users WHERE actor_key='alice'").fetchone()[0]))
    row = b.service.db.connection.execute("SELECT nickname,discoverable,accepts_requests FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'").fetchone()
    assert tuple(row) == ("小林", 1, 1)


def test_member_cannot_admin(bridge):
    b, incoming, token = bridge
    b.handle(request(incoming, token, action="command", command="创建网络", value="越权网络"))
    assert b.service.db.connection.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 0


def test_sync_excludes_other_user(bridge):
    b, incoming, token = bridge
    b.handle(request(incoming, token))
    bob = replace(incoming, actor_key="bob", contact_id=9, chat_id=10, message_id=9, text="你好")
    b.service.handle(bob)
    with b.service.db.transaction() as conn:
        uid = conn.execute("SELECT id FROM users WHERE actor_key='bob'").fetchone()[0]
        b.service._reply(conn, uid, "PRIVATE_BOB_CONTACT", "private-bob")
    b.sync(incoming)
    encoded = b.rpc.send_webxdc_status_update.call_args.args[2]
    assert "PRIVATE_BOB_CONTACT" not in encoded
