import hashlib
import hmac
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_webxdc_flow import Client

from contactbot.database import Database
from contactbot.service import BotService
from contactbot.webxdc_bridge import WebxdcBridge
from contactbot.xdc_admin import make_verifier


@pytest.fixture
def admin_fixture(tmp_path, monkeypatch):
    secret = "test-administrator-key"
    monkeypatch.setenv("CONTACTBOT_XDC_ADMIN_VERIFIER", make_verifier(secret))
    db = Database(tmp_path / "admin.db"); db.initialize()
    service = BotService(db, b"a-test-key-that-is-long-enough-1234")
    first, _ = service.seed_network("第一网络", "network-one-secret")
    second, _ = service.seed_network("第二网络", "network-two-secret")
    rpc = Mock(); rpc.send_msg.side_effect = range(100, 200)
    rpc.get_webxdc_status_updates.return_value = "[]"; rpc.get_chat_media.return_value = []
    bridge = WebxdcBridge(rpc, service)
    admin = Client(bridge, "system", 10); admin.join("network-one-secret")
    member = Client(bridge, "member", 20); member.join("network-one-secret")
    with db.transaction() as conn:
        conn.execute("UPDATE users SET is_system_admin=1 WHERE actor_key='system'")
    yield admin, member, secret, first, second
    db.close()


def proof(admin, secret):
    challenge = admin.snapshot()["admin"]["challenge"]
    key = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(challenge["salt"]), challenge["iterations"])
    return hmac.new(key, ("contact-admin-v1:" + challenge["nonce"] + ":" + admin.token).encode(), hashlib.sha256).hexdigest()


def test_system_admin_must_unlock_before_any_admin_operation(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    admin.call(command="网络列表")
    assert admin.snapshot()["receipt"]["ok"] == 0
    admin.call("admin_action", operation="create", data={"name": "不允许"})
    assert admin.db.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 2
    admin.call("admin_open")
    assert admin.snapshot()["admin"]["unlocked"] is False
    wrong = proof(admin, "incorrect")
    admin.call("admin_unlock", proof=wrong)
    assert admin.snapshot()["admin"]["unlocked"] is False
    correct = proof(admin, secret)
    admin.call("admin_unlock", proof=correct)
    state = admin.snapshot()["admin"]
    assert state["unlocked"] and state["system"] and len(state["networks"]) == 2
    assert "@example.org" not in str(state)
    admin.call("admin_action", operation="rename", data={"name": "新网络名"})
    assert admin.db.execute("SELECT name FROM networks WHERE id=?", (state["current"],)).fetchone()[0] == "新网络名"
    admin.call("admin_lock")
    admin.call("admin_action", operation="rename", data={"name": "不允许"})
    assert admin.db.execute("SELECT name FROM networks WHERE id=?", (state["current"],)).fetchone()[0] == "新网络名"
    member.call("admin_open")
    assert member.snapshot()["receipt"]["ok"] == 1
    assert not member.snapshot()["admin"]["unlocked"]


def test_key_attempts_are_rate_limited_and_challenges_not_reusable(admin_fixture):
    admin, _, secret, _, _ = admin_fixture
    admin.call("admin_open")
    stale_proof = proof(admin, secret)
    admin.call("admin_unlock", proof="0" * 64)
    admin.call("admin_unlock", proof=stale_proof)
    assert not admin.snapshot()["admin"]["unlocked"]
    for _ in range(3):
        admin.call("admin_unlock", proof="0" * 64)
    admin.call("admin_unlock", proof=proof(admin, secret))
    assert not admin.snapshot()["admin"]["unlocked"]
    assert admin.db.execute("SELECT COUNT(*) FROM rate_events WHERE category='xdc_admin_key'").fetchone()[0] == 5


def test_restart_replay_and_key_change_revoke_access(admin_fixture, monkeypatch):
    admin, _, secret, _, _ = admin_fixture
    admin.call("admin_open")
    unlock_message = admin.call("admin_unlock", proof=proof(admin, secret))
    assert admin.snapshot()["admin"]["unlocked"]
    restarted = WebxdcBridge(admin.bridge.rpc, admin.bridge.service)
    restarted.handle(unlock_message)
    admin.bridge = restarted
    assert admin.snapshot()["admin"]["unlocked"]
    monkeypatch.setenv("CONTACTBOT_XDC_ADMIN_VERIFIER", make_verifier("another-test-key"))
    admin.bridge = WebxdcBridge(admin.bridge.rpc, admin.bridge.service)
    assert not admin.snapshot()["admin"]["unlocked"]


@pytest.mark.parametrize("network_role", ["member", "network_admin"])
def test_any_member_can_grant_persistent_account_management(admin_fixture, network_role):
    admin, member, secret, _, _ = admin_fixture
    with admin.bridge.service.db.transaction() as conn:
        conn.execute("UPDATE memberships SET role=? WHERE user_id=(SELECT id FROM users WHERE actor_key='member')", (network_role,))
    member.call("admin_open")
    state = member.snapshot()["admin"]
    assert not state["unlocked"]
    member.call("admin_action", operation="join_off", data={})
    assert member.snapshot()["receipt"]["ok"] == 0
    member.call("admin_unlock", proof=proof(member, secret))
    state = member.snapshot()["admin"]
    assert state["unlocked"] and state["system"] and len(state["networks"]) == 2
    other = member.db.execute("SELECT id FROM networks WHERE id<>?", (state["current"],)).fetchone()[0]
    member.call("admin_select", admin_network=other, page=0)
    assert member.snapshot()["receipt"]["ok"] == 1
    member.call("admin_action", operation="join_off", data={})
    assert member.db.execute("SELECT join_enabled FROM networks WHERE id=?", (other,)).fetchone()[0] == 0
    assert member.db.execute("SELECT is_system_admin FROM users WHERE actor_key='member'").fetchone()[0] == 0
    member.call("admin_lock")
    member.call("admin_action", operation="join_on", data={})
    assert member.snapshot()["receipt"]["ok"] == 0


def test_session_cannot_be_transferred_to_another_instance(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    assert admin.snapshot()["admin"]["unlocked"]
    attacker = replace(admin.incoming, actor_key=member.incoming.actor_key, contact_id=member.incoming.contact_id, chat_id=member.incoming.chat_id)
    before = admin.db.execute("SELECT COUNT(*) FROM xdc_admin_sessions").fetchone()[0]
    admin.bridge.handle(replace(attacker, text=admin.call("refresh").text))
    assert admin.db.execute("SELECT COUNT(*) FROM xdc_admin_sessions").fetchone()[0] == before


def test_revoked_account_and_pending_admin_confirmation_are_blocked(admin_fixture):
    from contactbot.service import _now

    admin, _, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    with admin.bridge.service.db.transaction() as conn:
        conn.execute("DELETE FROM xdc_admin_grants")
        user_id, network_id = conn.execute("SELECT id,current_network_id FROM users WHERE actor_key='system'").fetchone()
        pending = admin.bridge.service._pending(conn, user_id, "rotate_code", {"network_id": network_id, "code": "unapproved-code"}, _now())
    admin.call("reply", step=pending, value="1")
    assert admin.snapshot()["receipt"]["ok"] == 0
    with admin.bridge.service.db.transaction() as conn:
        assert admin.bridge.service._find_network_by_code(conn, "unapproved-code") is None


def test_key_unlock_does_not_require_persistent_system_role(admin_fixture):
    admin, _, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    assert admin.snapshot()["admin"]["unlocked"]
    with admin.bridge.service.db.transaction() as conn:
        conn.execute("UPDATE users SET is_system_admin=0 WHERE actor_key='system'")
    admin.call("admin_action", operation="rename", data={"name": "新的名称"})
    assert admin.snapshot()["receipt"]["ok"] == 1
    assert admin.snapshot()["admin"]["unlocked"]
    assert admin.db.execute("SELECT is_system_admin FROM users WHERE actor_key='system'").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["remove"])
def test_removed_members_hidden_and_audit_retained(admin_fixture, operation):
    admin, member, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    code = member.snapshot()["profile"]["member_code"]
    admin.call("admin_action", operation=operation, data={"member_code": code, "confirmed": "yes", "reason": "test"})
    state = admin.snapshot()["admin"]
    assert code not in {m["member_code"] for m in state["members"]}
    assert any(a["action"] == operation + "_member" and a["target_ref"] == code for a in state["audit"])
    assert (code in {m["member_code"] for m in state["bans"]}) == (operation == "ban")
    if operation == "ban":
        admin.call("admin_action", operation="unban", data={"member_code": code})
        assert admin.snapshot()["admin"]["bans"] == []
        assert code not in {m["member_code"] for m in admin.snapshot()["admin"]["members"]}


def test_member_page_clamped_after_list_shrinks(admin_fixture):
    admin, _, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    current = admin.snapshot()["admin"]["current"]
    admin.call("admin_select", admin_network=current, page=999)
    assert admin.snapshot()["admin"]["page"] == 0
    assert admin.snapshot()["admin"]["members"]


def test_grant_survives_new_instance_and_is_account_scoped(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    member.call("admin_open")
    member.call("admin_unlock", proof=proof(member, secret))
    with member.bridge.service.db.transaction() as conn:
        conn.execute("UPDATE xdc_admin_grants SET granted_at='2000-01-01T00:00:00+00:00'")
    fresh = Client(member.bridge, "member", 20)
    fresh.call("admin_open")
    assert fresh.snapshot()["admin"]["unlocked"]
    assert not admin.snapshot()["admin"]["unlocked"]
    fresh.call("admin_lock")
    fresh.call("admin_open")
    assert not fresh.snapshot()["admin"]["unlocked"]


def test_admin_visibility_matches_directory_without_disclosing_hidden_profiles(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    with admin.bridge.service.db.transaction() as conn:
        conn.execute("UPDATE memberships SET discoverable=0 WHERE user_id=(SELECT id FROM users WHERE actor_key='member')")
    state = admin.snapshot()
    hidden = next(m for m in state["admin"]["members"] if m["nickname"] == "member")
    assert not hidden["discoverable"] and not hidden["is_self"]
    assert hidden["member_code"] not in {m["member_code"] for m in state["directory"]["members"]}
    own = next(m for m in state["admin"]["members"] if m["nickname"] == "system")
    assert own["is_self"]


def test_delete_data_revokes_account_grant(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    member.call("admin_open"); member.call("admin_unlock", proof=proof(member, secret))
    member.call(command="删除我的数据")
    member.reply("1")
    assert not member.snapshot()["admin"]["unlocked"]


def test_delivery_uncertainty_management_is_scoped_and_never_resends(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    admin.call("exchange_now", value=member.snapshot()["profile"]["member_code"])
    service = admin.bridge.service
    item = next(r for r in service.pending_outbox(200) if r["dedupe_key"].startswith("exchange:"))
    with pytest.raises(TimeoutError):
        service.send_approved_vcard(item["id"], lambda *_: (_ for _ in ()).throw(TimeoutError()), account_id=1)
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    snapshot = admin.snapshot()["admin"]
    assert snapshot["deliveries"][0]["id"] == item["id"]
    network = snapshot["current"]
    other = next(n["id"] for n in snapshot["networks"] if n["id"] != network)
    admin.call("admin_select", admin_network=other)
    admin.call("admin_action", operation="delivery_close", data={"outbox_id": str(item["id"]), "confirmed": "yes"})
    assert not admin.snapshot()["receipt"]["ok"]
    admin.call("admin_select", admin_network=network)
    admin.call("admin_action", operation="delivery_close", data={"outbox_id": str(item["id"]), "confirmed": "yes"})
    assert admin.snapshot()["receipt"]["ok"] and not admin.snapshot()["admin"]["deliveries"]
    assert not service.send_approved_vcard(item["id"], lambda *_: pytest.fail("resend"), account_id=1)


def test_new_ban_is_rejected_without_removing_member(admin_fixture):
    admin, member, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    code = member.snapshot()["profile"]["member_code"]
    admin.call("admin_action", operation="ban", data={"member_code": code, "confirmed": "yes", "reason": "test"})
    assert not admin.snapshot()["receipt"]["ok"]
    assert code in {m["member_code"] for m in admin.snapshot()["admin"]["members"]}
    assert admin.db.execute("SELECT COUNT(*) FROM bans").fetchone()[0] == 0


def test_create_network_selects_it_and_creator_can_join_and_switch(admin_fixture):
    admin, _, secret, _, _ = admin_fixture
    admin.call("admin_open"); admin.call("admin_unlock", proof=proof(admin, secret))
    previous = admin.snapshot()["current_network"]
    admin.call("admin_action", operation="create", data={"name": "新网络"})
    state = admin.snapshot()
    assert state["receipt"]["ok"]
    new_id = state["admin"]["current"]
    assert new_id != previous
    assert any(n["id"] == new_id and n["name"] == "新网络" for n in state["admin"]["networks"])
    assert state["current_network"] == previous
    cipher = admin.db.execute("SELECT join_code_ciphertext FROM networks WHERE id=?", (new_id,)).fetchone()[0]
    code = admin.bridge.service._decrypt_join_code(cipher)
    assert code in state["messages"][-1]
    admin.call("join", value=code)
    assert admin.snapshot()["pending"]["action"] == "guide_nickname"
    admin.reply("新昵称")
    assert admin.snapshot()["pending"]["action"] == "join"
    admin.reply("1", consent=True, use_current_account=True, discoverable=True, accepts_requests=True)
    state = admin.snapshot()
    assert state["current_network"] == new_id
    assert state["profile"]["nickname"] == "新昵称"
    assert state["pending"] is None
    admin.call("join", value="network-one-secret")
    state = admin.snapshot()
    assert state["current_network"] == previous
    assert state["pending"] is None or state["pending"]["action"] == "guide_next"


def test_managed_join_requires_key_and_preserves_original_membership(admin_fixture):
    admin, member, secret, first, second = admin_fixture
    second_id = admin.db.execute('SELECT id FROM networks WHERE public_id=?', (second,)).fetchone()[0]
    original = admin.snapshot()['current_network']
    for client in (admin, member):
        client.call('join_managed', admin_network=second_id)
        assert client.snapshot()['receipt']['ok'] == 0
        assert len(client.snapshot()['networks']) == 1
    admin.call('admin_open'); admin.call('admin_unlock', proof=proof(admin, secret))
    admin.call('join_managed', admin_network=second_id)
    state = admin.snapshot()
    assert state['receipt']['ok'] == 1
    assert state['pending']['action'] == 'join'
    assert state['pending']['network_name'] == '第二网络'
    assert state['current_network'] == original
    admin.call('cancel_join', step=state['pending']['token'])
    assert admin.snapshot()['pending'] is None
    assert admin.snapshot()['current_network'] == original
    admin.call('join_managed', admin_network=second_id)
    request = admin.reply('1', consent=True, use_current_account=True, discoverable=False, accepts_requests=True)
    admin.bridge.handle(request)
    state = admin.snapshot()
    assert state['receipt']['ok'] == 1 and state['pending'] is None
    assert state['current_network'] == second_id
    assert len(state['networks']) == 2
    assert state['profile']['share_contact'] == admin.incoming.contact_address
    assert state['profile']['discoverable'] == 0
    admin.call(command='切换网络', value=first)
    assert admin.snapshot()['current_network'] == original
    assert admin.snapshot()['profile']['discoverable'] == 1
    admin.call('join_managed', admin_network=second_id)
    assert admin.snapshot()['current_network'] == second_id
    assert admin.snapshot()['profile']['discoverable'] == 0
    assert len(admin.snapshot()['networks']) == 2


@pytest.mark.parametrize('blocked', ['disabled', 'join_off', 'revoked', 'removed', 'invalid_id'])
def test_managed_join_cannot_bypass_constraints(admin_fixture, blocked):
    admin, _, secret, _, second = admin_fixture
    second_id = admin.db.execute('SELECT id FROM networks WHERE public_id=?', (second,)).fetchone()[0]
    admin.call('admin_open'); admin.call('admin_unlock', proof=proof(admin, secret))
    if blocked == 'invalid_id':
        admin.call('join_managed', admin_network=True)
    else:
        admin.call('join_managed', admin_network=second_id)
        with admin.bridge.service.db.transaction() as conn:
            if blocked == 'disabled': conn.execute("UPDATE networks SET status='disabled' WHERE id=?", (second_id,))
            elif blocked == 'join_off': conn.execute('UPDATE networks SET join_enabled=0 WHERE id=?', (second_id,))
            elif blocked == 'removed': conn.execute('DELETE FROM networks WHERE id=?', (second_id,))
        if blocked == 'revoked': admin.call('admin_lock')
        admin.reply('1', consent=True, use_current_account=True, discoverable=True, accepts_requests=True)
    assert admin.snapshot()['receipt']['ok'] == 0
    assert len(admin.snapshot()['networks']) == 1


def test_new_admin_without_membership_can_join_managed_network(admin_fixture):
    admin, member, secret, _, second = admin_fixture
    fresh = Client(admin.bridge, 'newadmin', 30)
    fresh.call('admin_open'); fresh.call('admin_unlock', proof=proof(fresh, secret))
    second_id = admin.db.execute('SELECT id FROM networks WHERE public_id=?', (second,)).fetchone()[0]
    fresh.call('join_managed', admin_network=second_id)
    assert fresh.snapshot()['pending']['action'] == 'guide_nickname'
    fresh.reply('新成员')
    fresh.reply('1', consent=True, use_current_account=True, discoverable=True, accepts_requests=True)
    assert fresh.snapshot()['current_network'] == second_id
    assert len(fresh.snapshot()['networks']) == 1


def test_fresh_admin_instance_has_networks_and_can_join_in_one_submission(admin_fixture):
    admin, _, secret, first, second = admin_fixture
    admin.call('admin_open'); admin.call('admin_unlock', proof=proof(admin, secret))
    fresh = Client(admin.bridge, 'system', 10)
    snapshot = fresh.snapshot()
    assert snapshot['admin']['system'] is True
    assert len(snapshot['admin']['networks']) == 2
    target = next(n['id'] for n in snapshot['admin']['networks'] if n['public_id'] == second)
    fresh.call('join_network', admin_network=target, nickname='管理者', consent=True,
               use_current_account=True, discoverable=True, accepts_requests=True)
    assert fresh.snapshot()['receipt']['ok'] == 1
    assert fresh.snapshot()['pending'] is None
    assert len(fresh.snapshot()['networks']) == 2
