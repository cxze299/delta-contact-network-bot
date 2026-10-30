import hashlib
import hmac
import json
from dataclasses import replace

import pytest
import test_webxdc_flow as flow
from contactbot.webxdc_bridge import PREFIX


@pytest.fixture
def clients(tmp_path):
    yield from flow.clients.__wrapped__(tmp_path)


def bind_real_actor(client):
    actor = hmac.new(client.bridge.service.secret, client.incoming.contact_address.casefold().encode(), hashlib.sha256).hexdigest()
    old = client.incoming.actor_key
    client.db.execute("UPDATE users SET actor_key=? WHERE actor_key=?", (actor, old))
    client.db.execute("UPDATE xdc_instances SET actor_key=? WHERE actor_key=?", (actor, old))
    client.db.commit()
    client.incoming = replace(client.incoming, actor_key=actor)


def test_recover_by_network_code_and_nickname_still_requires_admin_approval(clients):
    alice, _ = clients
    alice.join(); bind_real_actor(alice)
    restored = flow.Client(alice.bridge, "replacement", 30)
    restored.call(command="恢复账号")
    restored.call("recover_by_name", join_code="test-network-code", nickname="alice", old_address="alice@example.org")
    assert restored.snapshot()["receipt"]["ok"]
    row = alice.db.execute("SELECT ar.status,m.nickname,u.actor_key FROM account_recoveries ar JOIN memberships m ON m.id=ar.membership_id JOIN users u ON u.id=ar.requester_user_id").fetchone()
    assert tuple(row) == ("pending", "alice", "replacement")
    assert alice.db.execute("SELECT count(*) FROM memberships WHERE user_id=(SELECT id FROM users WHERE actor_key='replacement')").fetchone()[0] == 0


def test_recovery_needs_valid_code_and_old_address_and_disambiguates_names(clients):
    alice, bob = clients
    alice.join(); bob.join(); bind_real_actor(alice); bind_real_actor(bob)
    restored = flow.Client(alice.bridge, "replacement", 30)
    restored.call("recover_by_name", join_code="wrong", nickname="alice", old_address="alice@example.org")
    assert not restored.snapshot()["receipt"]["ok"]
    restored.call("recover_by_name", join_code="test-network-code", nickname="alice", old_address="wrong@example.org")
    assert not restored.snapshot()["receipt"]["ok"]
    alice.db.execute("UPDATE memberships SET nickname='alice' WHERE user_id=(SELECT id FROM users WHERE actor_key=?)", (bob.incoming.actor_key,)); alice.db.commit()
    restored.call("recover_by_name", join_code="test-network-code", nickname="alice", old_address="alice@example.org")
    assert restored.snapshot()["receipt"]["ok"]
    assert alice.db.execute("SELECT m.user_id FROM account_recoveries r JOIN memberships m ON m.id=r.membership_id").fetchone()[0] == alice.db.execute("SELECT id FROM users WHERE actor_key=?", (alice.incoming.actor_key,)).fetchone()[0]


def test_recovery_accepts_retained_historical_address_alias(clients):
    alice, _ = clients
    alice.join(); bind_real_actor(alice)
    old_actor = alice.incoming.actor_key
    current = hmac.new(alice.bridge.service.secret, b"current@example.org", hashlib.sha256).hexdigest()
    with alice.bridge.service.db.transaction() as conn:
        uid = conn.execute("SELECT id FROM users WHERE actor_key=?", (old_actor,)).fetchone()[0]
        conn.execute("UPDATE users SET actor_key=? WHERE id=?", (current, uid))
        alias_id = conn.execute("INSERT INTO users(actor_key,contact_id,chat_id,merged_into,created_at,updated_at) VALUES('temporary-alias',-1,-1,?,'2026','2026')", (uid,)).lastrowid
        conn.execute("UPDATE users SET actor_key=? WHERE id=?", (f"merged:{alias_id}:{old_actor}", alias_id))
    restored = flow.Client(alice.bridge, "replacement", 30)
    restored.call("recover_by_name", join_code="test-network-code", nickname="alice", old_address="alice@example.org")
    assert restored.snapshot()["receipt"]["ok"]
    assert alice.db.execute("SELECT m.user_id FROM account_recoveries r JOIN memberships m ON m.id=r.membership_id").fetchone()[0] == uid


def test_recovery_failed_attempts_are_rate_limited_after_rollback(clients):
    alice, _ = clients
    alice.join(); bind_real_actor(alice)
    restored = flow.Client(alice.bridge, "replacement", 30)
    for _ in range(11):
        restored.call("recover_by_name", join_code="incorrect", nickname="alice", old_address="alice@example.org")
    state = restored.snapshot()
    assert not state["receipt"]["ok"]
    assert "过多" in state["messages"][-1]
    assert alice.db.execute("SELECT count(*) FROM rate_events WHERE actor_key='replacement' AND category='recovery_lookup'").fetchone()[0] == 10


def test_recovery_targets_only_network_matching_join_code(clients):
    alice, _ = clients
    alice.join()
    alice.bridge.service.seed_network("另一个网络", "another-network-code")
    alice.join("another-network-code")
    bind_real_actor(alice)
    restored = flow.Client(alice.bridge, "replacement", 30)
    restored.call("recover_by_name", join_code="another-network-code", nickname="alice", old_address="ALICE@example.org")
    assert restored.snapshot()["receipt"]["ok"]
    network = alice.db.execute("SELECT n.name FROM account_recoveries r JOIN memberships m ON m.id=r.membership_id JOIN networks n ON n.id=m.network_id").fetchone()[0]
    assert network == "另一个网络"


def test_recovery_rejects_unencrypted_or_nonprivate_messages(clients):
    alice, _ = clients
    alice.join(); bind_real_actor(alice)
    restored = flow.Client(alice.bridge, "replacement", 30)
    data = {"id": "untrusted", "token": restored.token, "network": None,
            "action": "recover_by_name", "join_code": "test-network-code",
            "nickname": "alice", "old_address": "alice@example.org"}
    for changes in ({"is_private": False}, {"is_encrypted": False}):
        message = replace(restored.incoming, text=PREFIX + json.dumps(data), **changes)
        assert restored.bridge.handle(message)
    assert alice.db.execute("SELECT count(*) FROM account_recoveries").fetchone()[0] == 0
    assert alice.db.execute("SELECT count(*) FROM rate_events WHERE category='recovery_lookup'").fetchone()[0] == 0


def test_block_list_is_private_to_member_and_can_unblock_removed_person(clients):
    alice, bob = clients
    alice.join(); bob.join()
    code = bob.snapshot()["profile"]["member_code"]
    alice.call(command="屏蔽", value=code)
    assert [(r["nickname"], r["member_code"]) for r in alice.snapshot()["blocked_members"]] == [("bob", code)]
    assert bob.snapshot()["blocked_members"] == []
    alice.db.execute("UPDATE memberships SET status='removed' WHERE member_code=?", (code,)); alice.db.commit()
    alice.call(command="解除屏蔽", value=code)
    assert alice.snapshot()["receipt"]["ok"]
    assert alice.snapshot()["blocked_members"] == []


def test_report_uses_internal_code_without_showing_it_in_ui(clients):
    alice, bob = clients
    alice.join(); bob.join()
    code = bob.snapshot()["profile"]["member_code"]
    alice.call(command="举报", value=code + " 不当行为")
    assert alice.snapshot()["receipt"]["ok"]
    assert alice.db.execute("SELECT reason FROM reports").fetchone()[0] == "不当行为"


def test_blocked_members_follow_current_network(clients):
    alice, bob = clients
    alice.join(); bob.join()
    first = alice.db.execute("SELECT n.public_id FROM networks n JOIN memberships m ON m.network_id=n.id JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'").fetchone()[0]
    alice.call(command="屏蔽", value=bob.snapshot()["profile"]["member_code"])
    alice.bridge.service.seed_network("其他网络", "other-network-code")
    alice.join("other-network-code")
    assert alice.snapshot()["blocked_members"] == []
    alice.call(command="切换网络", value=first)
    assert [row["nickname"] for row in alice.snapshot()["blocked_members"]] == ["bob"]
