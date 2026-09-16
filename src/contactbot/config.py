from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    secret: bytes
    database: Path
    accounts_dir: Path
    system_admins: frozenset[str]
    system_admin_contact_ids: frozenset[int]
    account_id: int | None
    display_name: str = "好友助手"
    rpc_server: str = "deltachat-rpc-server"
    require_verified: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        raw_secret = os.environ.get("CONTACTBOT_SECRET", "")
        if len(raw_secret) < 32:
            raise RuntimeError("CONTACTBOT_SECRET 至少需要 32 个字符")
        admins = frozenset(
            normalize_address(item)
            for item in os.environ.get("CONTACTBOT_SYSTEM_ADMINS", "").split(",")
            if item.strip()
        )
        raw_account_id = os.environ.get("CONTACTBOT_ACCOUNT_ID", "").strip()
        admin_contact_ids = frozenset(
            int(item.strip())
            for item in os.environ.get("CONTACTBOT_SYSTEM_ADMIN_CONTACT_IDS", "").split(",")
            if item.strip()
        )
        return cls(
            secret=raw_secret.encode("utf-8"),
            database=Path(os.environ.get("CONTACTBOT_DB", "./data/contactbot.sqlite3")),
            accounts_dir=Path(os.environ.get("CONTACTBOT_ACCOUNTS_DIR", "./accounts")),
            system_admins=admins,
            system_admin_contact_ids=admin_contact_ids,
            account_id=int(raw_account_id) if raw_account_id else None,
            display_name=os.environ.get("CONTACTBOT_DISPLAY_NAME", "好友助手").strip()
            or "好友助手",
            rpc_server=os.environ.get("CONTACTBOT_RPC_SERVER", "deltachat-rpc-server"),
            require_verified=os.environ.get("CONTACTBOT_REQUIRE_VERIFIED", "true").casefold()
            not in {"0", "false", "no"},
            log_level=os.environ.get("CONTACTBOT_LOG_LEVEL", "INFO").upper(),
        )

    def actor_key(self, address: str) -> str:
        return hmac.new(
            self.secret, normalize_address(address).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def is_bootstrap_admin(self, address: str) -> bool:
        normalized = normalize_address(address)
        return any(hmac.compare_digest(normalized, configured) for configured in self.system_admins)

    def is_bootstrap_admin_contact(self, contact_id: int) -> bool:
        return contact_id in self.system_admin_contact_ids


def normalize_address(address: str) -> str:
    return address.strip().casefold()
