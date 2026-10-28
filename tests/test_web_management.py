import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from contactbot.admin_web import create_admin_app
from contactbot.config import Settings
from contactbot.database import Database
from contactbot.service import BotService


@pytest.fixture
def web(tmp_path):
    token = 'testing-web-management-token-at-least-32'
    settings = Settings(secret=b's'*32, database=tmp_path/'web.db', accounts_dir=tmp_path/'accounts', system_admins=frozenset(), system_admin_contact_ids=frozenset(), account_id=None)
    db = Database(settings.database)
    db.initialize()
    service = BotService(db, settings.secret)
    service.seed_network('Network One', 'join-code-one')
    service.seed_network('Network Two', 'join-code-two')
    with db.transaction() as conn:
        for uid in range(1,6):
            conn.execute('INSERT INTO users(id,actor_key,contact_id,chat_id,is_system_admin,current_network_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (uid,f'actor{uid}',uid,uid,int(uid==1),2,'2026-10-22','2026-10-22'))
        for mid, nid, uid, role in ((1,1,1,'member'),(2,1,2,'network_admin'),(3,1,3,'member'),(4,1,4,'member'),(5,2,4,'member')):
            conn.execute('INSERT INTO memberships(id,network_id,user_id,member_code,nickname,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (mid,nid,uid,f'M{mid}',f'Member {mid}',role,'2026-10-22','2026-10-22'))
    client = TestClient(create_admin_app(settings, token))
    client.auth = ('admin',token)
    csrf = hmac.new(token.encode(),b'contactbot-admin-csrf',hashlib.sha256).hexdigest()
    def action(operation, network=1, **fields):
        return client.post('/action', data={'csrf':csrf,'network_id':network,'action':operation,**fields})
    yield client, db, action, csrf
    client.close()
    db.close()


def login_network_admin(client, action):
    response = action('web_access', member_code='M2')
    import re
    password = re.search(r'密码（仅显示本次）：([^\n<]+)',response.text)[1]
    client.auth = ('user-2',password)


def test_web_credentials_follow_live_role_and_network_scope(web):
    client, db, action, csrf = web
    login_network_admin(client,action)
    response = client.get('/?network=2')
    assert 'Network One' in response.text and 'Network Two' not in response.text
    assert action('view_code').status_code == 200
    assert 'join-code-one' in action('view_code').text
    assert action('view_code',network=2).status_code == 400
    assert action('create',network='',name='Forbidden').status_code == 400
    assert action('appoint',member_code='M3').status_code == 400
    assert action('rename',name='Forbidden').status_code == 400
    assert action('disable',confirmed='yes').status_code == 400
    assert client.post('/member/3',data={'csrf':csrf,'network_id':1,'role':'network_admin','status':'active'}).status_code == 400
    db.connection.execute("UPDATE memberships SET role='member' WHERE id=2")
    db.connection.commit()
    assert client.get('/').status_code == 401


def test_ban_unban_joins_and_actor_chat_context(web):
    client, db, action, _ = web
    login_network_admin(client,action)
    assert action('ban',member_code='M4',reason='test',confirmed='no').status_code == 400
    assert db.connection.execute('SELECT count(*) FROM bans').fetchone()[0] == 0
    assert action('ban',member_code='M4',reason='test reason',confirmed='yes').status_code == 200
    assert db.connection.execute('SELECT network_id FROM bans').fetchone()[0] == 1
    assert db.connection.execute('SELECT status FROM memberships WHERE id=4').fetchone()[0] == 'removed'
    assert db.connection.execute('SELECT status FROM memberships WHERE id=5').fetchone()[0] == 'active'
    assert db.connection.execute("SELECT count(*) FROM outbox WHERE recipient_user_id=4 AND status='pending'").fetchone()[0] == 1
    assert db.connection.execute('SELECT current_network_id FROM users WHERE id=2').fetchone()[0] == 2
    assert action('unban',member_code='M4').status_code == 200
    assert db.connection.execute('SELECT count(*) FROM bans').fetchone()[0] == 0
    assert db.connection.execute('SELECT status FROM memberships WHERE id=4').fetchone()[0] == 'removed'
    assert action('join_off').status_code == 200
    assert db.connection.execute('SELECT join_enabled FROM networks WHERE id=1').fetchone()[0] == 0
    assert action('join_on').status_code == 200


def test_recovery_scope_and_duplicate_approval(web):
    client, db, action, _ = web
    with db.transaction() as conn:
        conn.execute("INSERT INTO account_recoveries(public_id,membership_id,requester_user_id,status,expires_at,created_at) VALUES('AR1',3,5,'pending','2099-01-01','2026-10-22')")
    login_network_admin(client,action)
    assert action('approve_recovery',network=2,recovery_id='AR1',confirmed='yes').status_code == 400
    assert action('approve_recovery',recovery_id='AR1',confirmed='yes').status_code == 200
    assert db.connection.execute('SELECT user_id FROM memberships WHERE id=3').fetchone()[0] == 5
    assert db.connection.execute('SELECT status FROM account_recoveries').fetchone()[0] == 'approved'
    assert action('approve_recovery',recovery_id='AR1',confirmed='yes').status_code == 400
    assert db.connection.execute("SELECT count(*) FROM outbox WHERE recipient_user_id=5 AND status='pending'").fetchone()[0] == 1


def test_system_create_rotate_role_and_disable_share_database(web):
    client, db, action, csrf = web
    db.connection.execute("INSERT INTO pending_actions(token,user_id,action,payload,expires_at,created_at) VALUES('chat-pending',1,'privacy','{}','2099-01-01','2026-10-22')")
    db.connection.commit()
    assert action('create',network='',name='Third Network').status_code == 200
    assert db.connection.execute("SELECT count(*) FROM pending_actions WHERE token='chat-pending'").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM networks WHERE name='Third Network'").fetchone()[0] == 1
    response = action('rotate', code='new-shared-code')
    assert response.status_code == 200
    service = BotService(db,b's'*32)
    with db.transaction() as conn:
        assert service._find_network_by_code(conn,'new-shared-code')['id'] == 1
        assert service._find_network_by_code(conn,'join-code-one') is None
    assert action('appoint',member_code='M3').status_code == 200
    assert db.connection.execute('SELECT role FROM memberships WHERE id=3').fetchone()[0] == 'network_admin'
    assert action('revoke',member_code='M3').status_code == 200
    assert action('rename',name='Renamed Network').status_code == 200
    assert action('disable',confirmed='yes').status_code == 200
    assert db.connection.execute('SELECT status FROM networks WHERE id=1').fetchone()[0] == 'disabled'
    assert action('enable').status_code == 200
    assert db.connection.execute('SELECT status FROM networks WHERE id=1').fetchone()[0] == 'active'
    assert db.connection.execute('SELECT current_network_id FROM users WHERE id=1').fetchone()[0] == 2
