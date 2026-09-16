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
