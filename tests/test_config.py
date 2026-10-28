from contactbot.config import Settings


def test_encrypted_new_accounts_are_allowed_by_default(monkeypatch):
    monkeypatch.setenv("CONTACTBOT_SECRET", "x" * 32)
    monkeypatch.delenv("CONTACTBOT_REQUIRE_VERIFIED", raising=False)
    assert Settings.from_env().require_verified is False
    monkeypatch.setenv("CONTACTBOT_REQUIRE_VERIFIED", "true")
    assert Settings.from_env().require_verified is True
