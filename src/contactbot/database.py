from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()

    def initialize(self) -> None:
        schema = files("contactbot").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._lock:
            self.connection.executescript(schema)
            columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(outbox)").fetchall()
            }
            if "requires_encryption" not in columns:
                self.connection.execute(
                    """ALTER TABLE outbox ADD COLUMN requires_encryption INTEGER NOT NULL
                       DEFAULT 0 CHECK (requires_encryption IN (0, 1))"""
                )
            network_columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(networks)").fetchall()
            }
            if "join_code_ciphertext" not in network_columns:
                self.connection.execute("ALTER TABLE networks ADD COLUMN join_code_ciphertext TEXT")
            membership_columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(memberships)").fetchall()
            }
            if "share_link_ciphertext" not in membership_columns:
                self.connection.execute(
                    "ALTER TABLE memberships ADD COLUMN share_link_ciphertext TEXT"
                )
            request_columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(contact_requests)").fetchall()
            }
            if "request_kind" not in request_columns:
                self.connection.execute(
                    """ALTER TABLE contact_requests ADD COLUMN request_kind TEXT NOT NULL
                       DEFAULT 'legacy' CHECK (request_kind IN ('legacy', 'vcard'))"""
                )
            if "vcard_contact_id" not in columns:
                self.connection.execute("ALTER TABLE outbox ADD COLUMN vcard_contact_id INTEGER")
            self.connection.execute(
                "UPDATE memberships SET share_link_ciphertext=NULL WHERE share_link_ciphertext IS NOT NULL"
            )
            self.connection.execute(
                "DELETE FROM pending_actions WHERE action='guide_share_link'"
            )
            self.connection.execute("PRAGMA user_version = 6")
            self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self.connection.close()
