"""SQLite control-plane storage with small, explicit migrations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Own one SQLite connection per context and apply migrations on open."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def migrate(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = self.connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        current = int(row[0]) if row else 0
        if current < 1:
            self._migration_one()
            if row is None:
                self.connection.execute("INSERT INTO schema_version(version) VALUES (1)")
            else:
                self.connection.execute("UPDATE schema_version SET version = 1")
            self.connection.commit()
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {current} is newer than supported {SCHEMA_VERSION}")

    def _migration_one(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                version TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts_v2 (
                id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                email TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_verified_at TEXT,
                UNIQUE(provider_id, email),
                FOREIGN KEY(provider_id) REFERENCES providers(id)
            );
            CREATE TABLE IF NOT EXISTS credentials (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                secret_ref TEXT NOT NULL UNIQUE,
                scopes_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_refresh_at TEXT,
                revoked_at TEXT,
                FOREIGN KEY(account_id) REFERENCES accounts_v2(id)
            );
            CREATE TABLE IF NOT EXISTS oauth_sessions (
                id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                account_hint TEXT,
                state_hash TEXT NOT NULL UNIQUE,
                redirect_uri TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                FOREIGN KEY(provider_id) REFERENCES providers(id)
            );
            CREATE TABLE IF NOT EXISTS jobs_v2 (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                status TEXT NOT NULL,
                config_snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_code TEXT,
                FOREIGN KEY(account_id) REFERENCES accounts_v2(id)
            );
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs_v2(id)
            );
            """
        )

    def ensure_provider(self, provider_id: str, name: str, version: str = "0.1.0") -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO providers(id, name, version, created_at) VALUES (?, ?, ?, ?)",
            (provider_id, name, version, utc_now()),
        )
        self.connection.commit()

    def create_oauth_session(
        self,
        provider_id: str,
        state_hash: str,
        redirect_uri: str,
        expires_at: str,
        account_hint: Optional[str] = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        self.connection.execute(
            """INSERT INTO oauth_sessions
               (id, provider_id, account_hint, state_hash, redirect_uri, status,
                created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (session_id, provider_id, account_hint, state_hash, redirect_uri, utc_now(), expires_at),
        )
        self.connection.commit()
        return session_id

    def consume_oauth_state(self, state_hash: str) -> Optional[sqlite3.Row]:
        """Atomically claim a pending, non-expired OAuth state."""
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM oauth_sessions
                   WHERE state_hash = ? AND status = 'pending' AND expires_at > ?""",
                (state_hash, utc_now()),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE oauth_sessions SET status = 'consumed' WHERE id = ? AND status = 'pending'",
                (row["id"],),
            )
            return row

    def create_job(self, account_id: str, config: Mapping[str, object]) -> str:
        job_id = uuid.uuid4().hex
        self.connection.execute(
            """INSERT INTO jobs_v2
               (id, account_id, status, config_snapshot_json, created_at)
               VALUES (?, ?, 'queued', ?, ?)""",
            (job_id, account_id, json.dumps(dict(config), sort_keys=True), utc_now()),
        )
        self.connection.commit()
        return job_id