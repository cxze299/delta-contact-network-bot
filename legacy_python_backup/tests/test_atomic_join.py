import json
from test_webxdc_flow import clients  # noqa: F401
from contactbot.webxdc_bridge import PREFIX


def submit(client, code='test-network-code', **changes):
    values = dict(value=code, nickname='成员昵称', consent=True, use_current_account=True,
                  discoverable=True, accepts_requests=True)
    values.update(changes)
    return client.call('join_network', **values)


def test_regular_member_can_join_five_networks_atomically(clients):
    alice, bob = clients
    submit(alice, discoverable=False)
    original = alice.snapshot()['current_network']
    assert len(alice.snapshot()['networks']) == 1
    assert alice.snapshot()['pending'] is None
    assert alice.snapshot()['profile']['share_contact'] == alice.incoming.contact_address
    for index in range(2, 6):
        alice.bridge.service.seed_network(f'网络{index}', f'network-code-{index}')
        message = submit(alice, f'network-code-{index}', nickname=f'昵称{index}')
        alice.bridge.handle(message)  # Replayed delivery cannot duplicate membership.
        state = alice.snapshot()
        assert state['receipt']['ok'] == 1
        assert len(state['networks']) == index
        assert state['profile']['nickname'] == f'昵称{index}'
        assert state['pending'] is None
    first = next(n for n in alice.snapshot()['networks'] if n['nickname'] == '成员昵称')
    alice.call(command='切换网络', value=first['public_id'])
    assert alice.snapshot()['current_network'] == original
    assert alice.snapshot()['profile']['discoverable'] == 0
    assert not alice.snapshot()['admin']['unlocked']
    bob.call('refresh')
    assert bob.snapshot()['networks'] == []


def test_rejoining_preserves_profile_and_preferences(clients):
    alice, _ = clients
    submit(alice, nickname='原昵称', discoverable=False, accepts_requests=False)
    original = dict(alice.snapshot()['profile'])
    submit(alice, nickname='不应覆盖', discoverable=True, accepts_requests=True)
    assert alice.snapshot()['receipt']['ok'] == 1
    assert alice.snapshot()['profile'] == original
    assert len(alice.snapshot()['networks']) == 1


def test_bad_code_keeps_original_network_and_error_survives_outbox_cleanup(clients):
    alice, _ = clients
    submit(alice)
    original = alice.snapshot()['current_network']
    request = submit(alice, '共同网络')
    request_id = json.loads(request.text[len(PREFIX):])['id']
    assert alice.snapshot()['receipt']['ok'] == 0
    with alice.bridge.service.db.transaction() as conn:
        conn.execute('DELETE FROM outbox')
    alice.call('refresh')
    state = alice.snapshot()
    error = next(r for r in state['recent_receipts'] if r['request_id'] == request_id)
    assert '网络名称不能用于加入' in error['error']
    assert state['current_network'] == original
    assert len(state['networks']) == 1
    assert state['pending'] is None


def test_invalid_attempt_rate_limit_survives_rollback(clients):
    alice, _ = clients
    for _ in range(11): submit(alice, 'invalid')
    result = alice.snapshot()
    assert result['receipt']['ok'] == 0
    assert '一小时' in result['recent_receipts'][0]['error']
    assert result['networks'] == []


def test_regular_user_cannot_join_by_admin_network_id(clients):
    alice, _ = clients
    target = alice.db.execute('SELECT id FROM networks').fetchone()[0]
    submit(alice, '', admin_network=target)
    assert alice.snapshot()['receipt']['ok'] == 0
    assert alice.snapshot()['networks'] == []


def test_invalid_profile_is_atomic_and_stale_source_network_is_safe(clients):
    alice, _ = clients
    submit(alice)
    original = alice.snapshot()['current_network']
    alice.bridge.service.seed_network('第二网络', 'second-code')
    submit(alice, 'second-code', nickname='')
    assert alice.snapshot()['receipt']['ok'] == 0
    assert alice.snapshot()['current_network'] == original
    assert len(alice.snapshot()['networks']) == 1
    submit(alice, 'second-code', network=None)
    assert alice.snapshot()['receipt']['ok'] == 1
    assert len(alice.snapshot()['networks']) == 2


def test_disabled_network_and_missing_consent_are_rejected(clients):
    alice, _ = clients
    submit(alice, consent=False)
    assert alice.snapshot()['networks'] == []
    with alice.bridge.service.db.transaction() as conn:
        conn.execute('UPDATE networks SET join_enabled=0')
    submit(alice)
    assert alice.snapshot()['networks'] == []
    assert '停止加入' in alice.snapshot()['recent_receipts'][0]['error']
