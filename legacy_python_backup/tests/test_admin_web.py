from __future__ import annotations

import base64
import hashlib
import hmac

from fastapi.testclient import TestClient

from contactbot.admin_web import create_admin_app
from contactbot.config import Settings
from contactbot.database import Database
from contactbot.service import BotService


def test_local_admin_requires_auth_and_edits_network(tmp_path):
    token = "local-admin-token-with-more-than-32-characters"
    settings = Settings(
        secret=b"s" * 32,
        database=tmp_path / "admin.sqlite3",
        accounts_dir=tmp_path / "accounts",
        system_admins=frozenset(),
        system_admin_contact_ids=frozenset(),
        account_id=None,
    )
    database = Database(settings.database)
    database.initialize()
    database.connection.execute("INSERT INTO users(actor_key,contact_id,chat_id,is_system_admin,created_at,updated_at) VALUES('admin',1,1,1,'2026-10-22','2026-10-22')")
    database.connection.commit()
    public_id, _ = BotService(database, settings.secret).seed_network("原网络", "旧加入码")
    network_id = database.connection.execute(
        "SELECT id FROM networks WHERE public_id=?", (public_id,)
    ).fetchone()[0]
    database.close()

    client = TestClient(create_admin_app(settings, token))
    assert client.get("/").status_code == 401
    auth = "Basic " + base64.b64encode(f"admin:{token}".encode()).decode()
    headers = {"Authorization": auth}
    page = client.get("/", headers=headers)
    assert page.status_code == 200
    assert "原网络" in page.text

    csrf = hmac.new(token.encode(), b"contactbot-admin-csrf", hashlib.sha256).hexdigest()
    response = client.post(
        f"/network/{network_id}/code",
        headers=headers,
        data={"csrf": csrf, "code": "中文 空格✨后台加入码"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    check = Database(settings.database)
    check.initialize()
    service = BotService(check, settings.secret)
    with check.transaction() as conn:
        assert service._find_network_by_code(conn, "中文 空格✨后台加入码") is not None
        assert service._find_network_by_code(conn, "旧加入码") is None
    check.close()


def test_dashboard_privacy_scope_reports_and_csrf(tmp_path):
    token = 'test-dashboard-token-at-least-32-characters'
    settings = Settings(secret=b's'*32, database=tmp_path/'web.db', accounts_dir=tmp_path/'accounts', system_admins=frozenset(), system_admin_contact_ids=frozenset(), account_id=None)
    db = Database(settings.database)
    db.initialize()
    service = BotService(db, settings.secret)
    service.seed_network('Xhsybyx', 'secret-network-one')
    service.seed_network('Other', 'secret-network-two')
    with db.transaction() as conn:
        for uid in (1, 2):
            conn.execute('INSERT INTO users(id,actor_key,contact_id,chat_id,is_system_admin,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (uid, f'actor{uid}', uid, uid, int(uid==1), '2026-10-22', '2026-10-22'))
        for mid, nid, nick in ((1,1,'Admin'), (2,1,'<script>alert(1)</script>'), (3,2,'PRIVATE-OTHER-NETWORK')):
            conn.execute('INSERT INTO memberships(id,network_id,user_id,member_code,nickname,share_contact,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (mid,nid,1 if mid==1 else 2,f'M{mid}',nick,'PRIVATE-CONTACT','2026-10-22','2026-10-22'))
        conn.execute("INSERT INTO reports(id,public_id,network_id,reporter_membership_id,target_membership_id,reason,created_at) VALUES(1,'R1',1,1,2,'test report','2026-10-22')")
    client = TestClient(create_admin_app(settings, token))
    client.auth = ('admin', token)
    csrf = hmac.new(token.encode(), b'contactbot-admin-csrf', hashlib.sha256).hexdigest()
    response = client.get('/?network=1')
    assert 'PRIVATE-CONTACT' not in response.text
    assert 'PRIVATE-OTHER-NETWORK' not in response.text
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/admin.css').status_code == 200
    assert client.get('/?network=1&q=no-match').status_code == 200
    assert client.post('/member/2', data={'network_id':1,'role':'network_admin','status':'active'}).status_code == 400
    assert client.post('/member/3', data={'csrf':csrf,'network_id':1,'role':'network_admin','status':'active'}).status_code == 400
    result = client.post('/member/2', data={'csrf':csrf,'network_id':1,'role':'network_admin','status':'active','discoverable':'on','accepts_requests':'on'})
    assert result.status_code == 200
    member = db.connection.execute('SELECT role,discoverable,accepts_requests FROM memberships WHERE id=2').fetchone()
    assert tuple(member) == ('network_admin',0,0)
    assert client.post('/report/1', data={'csrf':csrf,'network_id':2,'resolution':'wrong network'}).status_code == 400
    assert client.post('/report/1', data={'csrf':csrf,'network_id':1,'resolution':'resolved'}).status_code == 200
    assert db.connection.execute('SELECT status FROM reports WHERE id=1').fetchone()[0] == 'resolved'
    assert client.post('/report/1', data={'csrf':csrf,'network_id':1,'resolution':'duplicate'}).status_code == 400
    db.close()
