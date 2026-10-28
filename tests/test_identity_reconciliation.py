from dataclasses import replace

import pytest

import test_webxdc_flow as flow_fixtures
from contactbot.service import _now


@pytest.fixture
def clients(tmp_path):
    yield from flow_fixtures.clients.__wrapped__(tmp_path)


def reconcile(client, *, actor=None, fingerprint="a" * 40, verified=True, account=1):
    incoming = replace(client.incoming, actor_key=actor or client.incoming.actor_key,
                       contact_address=(actor or client.incoming.actor_key) + "@example.org",
                       identity_fingerprint=fingerprint, identity_verified=verified,
                       account_id=account)
    with client.bridge.service.db.transaction() as conn:
        return dict(client.bridge.service._ensure_user(conn, incoming, _now()))


def test_address_change_reuses_member_and_admin_identity(clients):
    alice, _ = clients
    alice.join()
    original = alice.snapshot()["profile"]["member_code"]
    uid = reconcile(alice)["id"]
    alice.db.execute("UPDATE users SET is_system_admin=1 WHERE id=?", (uid,)); alice.db.commit()
    rebound = reconcile(alice, actor="new-alice")
    assert rebound["id"] == uid and rebound["is_system_admin"] == 1
    member = alice.db.execute("SELECT * FROM memberships WHERE user_id=?", (uid,)).fetchone()
    assert member["member_code"] == original and member["share_contact"] is None
    assert alice.db.execute("SELECT actor_key FROM xdc_instances WHERE token=?", (alice.token,)).fetchone()[0] == "new-alice"
    reconcile(alice, actor="new-alice")
    assert alice.db.execute("SELECT count(*) FROM users").fetchone()[0] == 1


@pytest.mark.parametrize("reason", ["unverified", "different-key", "different-account"])
def test_no_merge_without_matching_identity(clients, reason):
    alice, _ = clients
    alice.join(); original = reconcile(alice)["id"]
    result = reconcile(alice, actor="other", verified=reason != "unverified",
                       fingerprint="b" * 40 if reason == "different-key" else "a" * 40,
                       account=2 if reason == "different-account" else 1)
    assert result["id"] != original
    # A second update must not erase the contradictory key and merge later.
    again = reconcile(alice, actor="other", verified=reason != "unverified",
                      fingerprint="b" * 40 if reason == "different-key" else "a" * 40,
                      account=2 if reason == "different-account" else 1)
    assert again["id"] == result["id"]


def test_existing_duplicates_preserve_history_restrictions_and_stable_code(clients):
    alice, bob = clients
    alice.join(); bob.join()
    old_code = alice.snapshot()["profile"]["member_code"]
    mid = alice.db.execute("SELECT id FROM memberships WHERE member_code=?", (old_code,)).fetchone()[0]
    uid = alice.db.execute("SELECT user_id FROM memberships WHERE id=?", (mid,)).fetchone()[0]
    bobid = alice.db.execute("SELECT id FROM users WHERE actor_key='bob'").fetchone()[0]
    # Model the historical bug: two application identities for one verified chat.
    alice.db.execute("UPDATE users SET contact_id=?,chat_id=? WHERE id=?", (alice.incoming.contact_id, alice.incoming.chat_id, bobid))
    alice.db.execute("UPDATE memberships SET discoverable=0,status='removed' WHERE id=?", (mid,))
    net = alice.db.execute("SELECT network_id FROM memberships WHERE id=?", (mid,)).fetchone()[0]
    alice.db.execute("INSERT INTO bans VALUES(?,?,?,?,?)", (net, "alice", "test", uid, _now()))
    alice.db.execute("INSERT INTO xdc_admin_grants VALUES(?,?,?)", (uid, "test-version", _now()))
    alice.db.commit()
    keeper = reconcile(alice, actor="bob")
    member = alice.db.execute("SELECT * FROM memberships WHERE user_id=?", (keeper["id"],)).fetchone()
    assert member["member_code"] == old_code and member["id"] == mid
    assert member["status"] == "removed" and member["discoverable"] == 0
    assert alice.db.execute("SELECT 1 FROM bans WHERE actor_key='bob' AND network_id=?", (net,)).fetchone()
    assert alice.db.execute("SELECT 1 FROM xdc_admin_grants WHERE user_id=?", (keeper["id"],)).fetchone()
    archived = alice.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert archived["merged_into"] == keeper["id"] and archived["contact_id"] < 0
    count = alice.db.execute("SELECT count(*) FROM audit_log WHERE action='merge_duplicate_identity'").fetchone()[0]
    reconcile(alice, actor="bob")
    assert alice.db.execute("SELECT count(*) FROM audit_log WHERE action='merge_duplicate_identity'").fetchone()[0] == count
    assert not list(alice.db.execute("PRAGMA foreign_key_check"))


def test_same_fingerprint_can_rebind_new_core_contact_and_keeps_networks(clients):
    alice, bob = clients
    alice.join(); reconcile(alice)
    alice.bridge.service.seed_network("second", "second-code")
    bob.join("second-code")
    incoming = replace(bob.incoming, identity_fingerprint="a" * 40, identity_verified=True)
    with alice.bridge.service.db.transaction() as conn:
        user = alice.bridge.service._ensure_user(conn, incoming, _now())
    assert alice.db.execute("SELECT count(*) FROM memberships WHERE user_id=?", (user["id"],)).fetchone()[0] == 2
    assert alice.db.execute("SELECT count(DISTINCT network_id) FROM memberships WHERE user_id=?", (user["id"],)).fetchone()[0] == 2
    assert not list(alice.db.execute("PRAGMA foreign_key_check"))


def test_pending_exchange_is_invalidated_during_merge(clients):
    alice, bob = clients
    alice.join(); bob.join()
    alice.call("exchange_now", value=bob.snapshot()["profile"]["member_code"])
    alice.db.execute("UPDATE users SET contact_id=?,chat_id=? WHERE actor_key='bob'", (alice.incoming.contact_id, alice.incoming.chat_id)); alice.db.commit()
    reconcile(alice, actor="bob")
    assert alice.db.execute("SELECT status FROM contact_requests").fetchone()[0] == "invalid"
    assert not alice.db.execute("SELECT 1 FROM outbox WHERE status='pending' AND dedupe_key LIKE 'exchange:%'").fetchone()
    assert not list(alice.db.execute("PRAGMA foreign_key_check"))


def test_repeated_merges_keep_archived_aliases_resolvable(clients):
    alice, bob = clients
    alice.join(); bob.join(); reconcile(alice)
    bob_message = replace(bob.incoming, identity_fingerprint="a" * 40, identity_verified=True)
    with alice.bridge.service.db.transaction() as conn:
        keeper = alice.bridge.service._ensure_user(conn, bob_message, _now())
    rebound = reconcile(alice)
    assert rebound["id"] == keeper["id"]
    assert alice.db.execute("SELECT count(*) FROM memberships WHERE status='active'").fetchone()[0] == 1
    assert not list(alice.db.execute("PRAGMA foreign_key_check"))


def test_blocks_and_reports_follow_stable_membership(clients):
    alice, bob = clients
    alice.join(); bob.join()
    third = flow_fixtures.Client(alice.bridge, "third", 30); third.join()
    ids = {r['actor_key']:r['id'] for r in alice.db.execute('SELECT u.actor_key,m.id FROM memberships m JOIN users u ON u.id=m.user_id')}
    network = alice.db.execute('SELECT network_id FROM memberships LIMIT 1').fetchone()[0]
    for name in ('alice','bob'):
        alice.db.execute('INSERT INTO blocks VALUES(?,?,?,?)',(network,ids[name],ids['third'],_now()))
    alice.db.execute("INSERT INTO reports(public_id,network_id,reporter_membership_id,target_membership_id,reason,created_at) VALUES('REPORT',?,?,?,'test',?)",(network,ids['third'],ids['bob'],_now()))
    alice.db.execute("UPDATE users SET contact_id=?,chat_id=? WHERE actor_key='bob'",(alice.incoming.contact_id,alice.incoming.chat_id));alice.db.commit()
    reconcile(alice,actor='bob')
    assert alice.db.execute('SELECT count(*) FROM blocks').fetchone()[0]==1
    assert alice.db.execute('SELECT target_membership_id FROM reports').fetchone()[0]==ids['alice']
    assert not list(alice.db.execute('PRAGMA foreign_key_check'))
