"""SQLite control-plane storage with small, explicit migrations."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional


SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Own one SQLite connection per context and apply migrations on open."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        for attempt in range(5):
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
                return
            except sqlite3.OperationalError as error:
                self.connection.rollback()
                if "locked" not in str(error).lower() or attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
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
        if current < 2:
            self._migration_two()
            self.connection.execute("UPDATE schema_version SET version = 2")
            self.connection.commit()
        if current < 3:
            self._migration_three()
            self.connection.execute("UPDATE schema_version SET version = 3")
            self.connection.commit()
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(oauth_sessions)")}
        if "pkce_verifier_hash" not in columns:
            self.connection.execute("ALTER TABLE oauth_sessions ADD COLUMN pkce_verifier_hash TEXT")
            self.connection.commit()
        if "pkce_verifier_ref" not in columns:
            self.connection.execute("ALTER TABLE oauth_sessions ADD COLUMN pkce_verifier_ref TEXT")
            self.connection.commit()

    def _migration_three(self) -> None:
        """Add non-secret protected-artifact metadata; payload stays external."""
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS protected_artifacts (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                account_id TEXT,
                message_id TEXT,
                attachment_name TEXT,
                artifact_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                capture_mode TEXT NOT NULL,
                backend TEXT NOT NULL,
                backend_ref TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                retention_until TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_protected_artifacts_message
                ON protected_artifacts(message_id);
            CREATE TABLE IF NOT EXISTS capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artifact_id TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_code TEXT,
                run_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(artifact_id) REFERENCES protected_artifacts(id)
            );
            """
        )
        # Existing installs created credentials.account_id without UNIQUE, but
        # upsert_credential relies on ON CONFLICT(account_id). Keep one row per
        # account before enforcing uniqueness so Connect can succeed.
        self.connection.execute(
            """DELETE FROM credentials
               WHERE rowid NOT IN (
                   SELECT MAX(rowid) FROM credentials GROUP BY account_id
               )"""
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_account_id ON credentials(account_id)"
        )
        self.connection.commit()

    def _migration_two(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL UNIQUE, message_date TEXT, sender TEXT, subject TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, email TEXT NOT NULL, normalized TEXT NOT NULL, UNIQUE(message_id, normalized), FOREIGN KEY(message_id) REFERENCES messages(id));
            CREATE TABLE IF NOT EXISTS phone_numbers (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, value_redacted TEXT NOT NULL, normalized TEXT NOT NULL, UNIQUE(message_id, normalized), FOREIGN KEY(message_id) REFERENCES messages(id));
            CREATE TABLE IF NOT EXISTS attachments (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, filename TEXT NOT NULL, mime_type TEXT, size_bytes INTEGER, sha256 TEXT, status TEXT NOT NULL DEFAULT 'metadata', UNIQUE(message_id, filename), FOREIGN KEY(message_id) REFERENCES messages(id));
            CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, domain TEXT NOT NULL, source TEXT NOT NULL, detail_redacted TEXT, UNIQUE(message_id, domain, source, detail_redacted), FOREIGN KEY(message_id) REFERENCES messages(id));
            CREATE INDEX IF NOT EXISTS idx_messages_provider_date ON messages(provider, message_date);
            CREATE INDEX IF NOT EXISTS idx_phone_numbers_normalized ON phone_numbers(normalized);
            CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON attachments(sha256);
        """)

    def persist_normalized_result(self, result) -> str:
        """Persist redacted normalized metadata idempotently; return local message id."""
        from .search import normalize_result
        normalized = normalize_result(result)
        if not normalized.message_id:
            raise ValueError("normalized result requires a message id")
        local_id = f"{normalized.provider}:{normalized.message_id}"
        with self.transaction() as connection:
            connection.execute("""INSERT INTO messages(id,provider,external_id,message_date,sender,subject,created_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET message_date=excluded.message_date,sender=excluded.sender,subject=excluded.subject""",
                (local_id, normalized.provider, normalized.message_id, normalized.message_date, normalized.sender, normalized.subject, utc_now()))
            for evidence in normalized.evidence:
                connection.execute("INSERT OR IGNORE INTO evidence(message_id,domain,source,detail_redacted) VALUES(?,?,?,?)",
                    (local_id, str(evidence.get("domain", "")), str(evidence.get("source", "")), str(evidence.get("detail", evidence.get("detail_redacted", "")))))
            for attachment in normalized.attachments:
                connection.execute("""INSERT OR IGNORE INTO attachments(message_id,filename,mime_type,size_bytes,sha256,status) VALUES(?,?,?,?,?,?)""",
                    (local_id, str(attachment.get("filename", "")), str(attachment.get("mime_type", "")), attachment.get("size_bytes"), attachment.get("sha256"), str(attachment.get("status", "metadata"))))
            return local_id

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
                account_id TEXT NOT NULL UNIQUE,
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
                pkce_verifier_hash TEXT,
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

    def upsert_account(self, account_id: str, provider_id: str, email: str, label: str,
                       status: str = "needs_authorization") -> str:
        """Create or update a non-secret provider account record."""
        if not account_id or not email or not label:
            raise ValueError("account id, email, and label are required")
        if status not in {"pending", "connected", "authorizing", "needs_authorization", "error", "disabled"}:
            raise ValueError("unsupported account status")
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET provider_id=excluded.provider_id,
                   email=excluded.email, label=excluded.label, status=excluded.status""",
                (account_id, provider_id, email.strip(), label.strip(), status, utc_now()),
            )
        return account_id

    def list_accounts(self):
        """Return account metadata only; credentials and session material are excluded."""
        return self.connection.execute(
            "SELECT id, provider_id, email, label, status, last_verified_at, created_at "
            "FROM accounts_v2 ORDER BY created_at DESC"
        ).fetchall()

    def upsert_credential(self, account_id: str, secret_ref: str, scopes: list[str]) -> None:
        self.connection.execute(
            """INSERT INTO credentials(id, account_id, secret_ref, scopes_json, status, created_at)
               VALUES (?, ?, ?, ?, 'active', ?)
               ON CONFLICT(account_id) DO UPDATE SET secret_ref=excluded.secret_ref,
               scopes_json=excluded.scopes_json, status='active', revoked_at=NULL""",
            (uuid.uuid4().hex, account_id, secret_ref, json.dumps(scopes), utc_now()),
        )
        self.connection.commit()

    def credential_for_account(self, account_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM credentials WHERE account_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()

    def update_account_health(self, account_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE accounts_v2 SET status=?, last_verified_at=? WHERE id=?", (status, utc_now() if status == "connected" else None, account_id))

    def mark_oauth_session(self, session_id: str, status: str, error_code: Optional[str] = None) -> bool:
        """Update OAuth control-plane state without storing authorization values."""
        if status not in {"pending", "consumed", "completed", "failed", "expired"}:
            raise ValueError("unsupported OAuth session status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE oauth_sessions SET status=?, completed_at=?, error_code=? WHERE id=?",
                (status, utc_now() if status != "pending" else None, error_code, session_id),
            )
            return cursor.rowcount == 1

    def create_oauth_session(
        self,
        provider_id: str,
        state_hash: str,
        redirect_uri: str,
        expires_at: str,
        account_hint: Optional[str] = None,
        pkce_verifier_hash: Optional[str] = None,
        pkce_verifier_ref: Optional[str] = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        self.connection.execute(
            """INSERT INTO oauth_sessions
               (id, provider_id, account_hint, state_hash, redirect_uri, status,
                created_at, expires_at, pkce_verifier_hash, pkce_verifier_ref)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (session_id, provider_id, account_hint, state_hash, redirect_uri, utc_now(), expires_at, pkce_verifier_hash, pkce_verifier_ref),
        )
        self.connection.commit()
        return session_id

    def consume_oauth_state(self, state_hash: str) -> Optional[sqlite3.Row]:
        """Atomically claim a pending, non-expired OAuth state for one callback."""
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM oauth_sessions
                   WHERE state_hash = ? AND status = 'pending' AND expires_at > ?""",
                (state_hash, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE oauth_sessions SET status='consumed', completed_at=? WHERE id=? AND status='pending'",
                (now, row["id"]),
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

    def claim_job(self, job_id: str) -> bool:
        """Atomically move a queued job to running."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE jobs_v2 SET status = 'running', started_at = ?
                   WHERE id = ? AND status = 'queued'""",
                (utc_now(), job_id),
            )
            return cursor.rowcount == 1

    def finish_job(self, job_id: str, status: str, error_code: Optional[str] = None) -> None:
        """Finish a running job with a constrained terminal status."""
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("job must finish as completed, failed, or cancelled")
        with self.transaction() as connection:
            connection.execute(
                """UPDATE jobs_v2 SET status = ?, finished_at = ?, error_code = ?
                   WHERE id = ? AND status = 'running'""",
                (status, utc_now(), error_code, job_id),
            )

    def cancel_job(self, job_id: str) -> bool:
        """Mark a queued job cancelled before a worker claims it."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs_v2 SET status='cancelled', finished_at=?, error_code=? "
                "WHERE id=? AND status='queued'", (utc_now(), "cancelled_by_user", job_id)
            )
            if cursor.rowcount:
                connection.execute(
                    "INSERT INTO job_events(job_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                    (job_id, "cancelled", json.dumps({"reason": "cancelled_by_user"}), utc_now())
                )
            return cursor.rowcount == 1

    def retry_job(self, job_id: str) -> Optional[str]:
        """Create a new queued job from a failed/cancelled job without exposing its config."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT account_id, status, config_snapshot_json FROM jobs_v2 WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] not in {"failed", "cancelled"}:
                return None
            new_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO jobs_v2(id,account_id,status,config_snapshot_json,created_at) VALUES(?,?,?,?,?)",
                (new_id, row["account_id"], "queued", row["config_snapshot_json"], utc_now()),
            )
            return new_id

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        """Return a job snapshot without exposing secret configuration values."""
        return self.connection.execute(
            "SELECT * FROM jobs_v2 WHERE id = ?", (job_id,)
        ).fetchone()

    def add_job_event(self, job_id: str, event_type: str, payload: Mapping[str, object]) -> None:
        """Append a structured, non-secret job event."""
        self.connection.execute(
            """INSERT INTO job_events(job_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (job_id, event_type, json.dumps(dict(payload), sort_keys=True), utc_now()),
        )
        self.connection.commit()

    def prune_job_events(self, keep: int = 500) -> int:
        """Bound event retention while preserving the newest events."""
        if keep < 1:
            raise ValueError("keep must be positive")
        cursor = self.connection.execute(
            "DELETE FROM job_events WHERE id NOT IN (SELECT id FROM job_events ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        self.connection.commit()
        return cursor.rowcount

    def update_account_health(self, account_id: str, status: str, verified_at: Optional[str] = None) -> bool:
        """Persist non-secret provider health state after an operator-approved check."""
        allowed = {"connected", "authorizing", "needs_authorization", "error", "disabled"}
        if status not in allowed:
            raise ValueError("unsupported account health status")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE accounts_v2 SET status=?, last_verified_at=? WHERE id=?",
                (status, verified_at or utc_now(), account_id),
            )
            return cursor.rowcount == 1

    def list_job_events(self, job_id: str, limit: int = 100, before_id: Optional[int] = None):
        """Return bounded event pages using an opaque numeric cursor."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query = "SELECT id, event_type, payload_json, created_at FROM job_events WHERE job_id=?"
        params = [job_id]
        if before_id is not None:
            query += " AND id < ?"
            params.append(int(before_id))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.connection.execute(query, params).fetchall()