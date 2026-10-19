import hashlib
import tempfile
import unittest
import time
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ports import allocate_loopback_port
from core.storage import Database
from core.async_jobs import AsyncJobRunner
from core.providers import DemoProvider, GmailProvider
from dashboard_server import reserve_run_directory
from dashboard_server import inspect_legacy_session
from dashboard_server import gmail_authorization_url
from core.secrets import LocalFileSecretStore, SecretRef


class CoreControlPlaneTests(unittest.TestCase):
    def test_shared_account_upsert_and_oauth_state_are_non_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("gmail", "Gmail")
                database.upsert_account("account-1", "gmail", "user@example.com", "Personal")
                accounts = database.list_accounts()
                self.assertEqual(accounts[0]["email"], "user@example.com")
                self.assertNotIn("token", dict(accounts[0]))
                session_id = database.create_oauth_session(
                    "gmail", hashlib.sha256(b"state").hexdigest(),
                    "http://127.0.0.1:8765/", "2999-01-01T00:00:00+00:00", "user@example.com",
                )
                self.assertTrue(database.mark_oauth_session(session_id, "failed", "operator_required"))
                row = database.connection.execute("SELECT status, error_code FROM oauth_sessions WHERE id=?", (session_id,)).fetchone()
                self.assertEqual(tuple(row), ("failed", "operator_required"))

    def test_oauth_state_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("gmail", "Gmail")
                session_id = database.create_oauth_session(
                    "gmail", hashlib.sha256(b"state-once").hexdigest(),
                    "http://127.0.0.1:8765/", "2999-01-01T00:00:00+00:00", "user@example.com",
                )
                self.assertEqual(database.consume_oauth_state(hashlib.sha256(b"state-once").hexdigest())["id"], session_id)
                self.assertIsNone(database.consume_oauth_state(hashlib.sha256(b"state-once").hexdigest()))

    def test_legacy_session_inspection_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "sessions"
            session_dir.mkdir()
            session = session_dir / "account.env"
            session.write_text("GMAIL_REFRESH_TOKEN=secret-value\nGMAIL_ACCESS_TOKEN=other-secret\n")
            import dashboard_server
            original = dashboard_server.SESSION_DIR
            dashboard_server.SESSION_DIR = session_dir
            try:
                result = inspect_legacy_session(session)
            finally:
                dashboard_server.SESSION_DIR = original
            self.assertTrue(result["present"])
            self.assertTrue(result["has_refresh_token"])
            self.assertNotIn("secret-value", str(result))

    def test_authorization_url_contains_state_but_no_secret(self):
        import os
        import dashboard_server
        previous = os.environ.get("GOOGLE_CLIENT_ID")
        os.environ["GOOGLE_CLIENT_ID"] = "client-id.apps.googleusercontent.com"
        try:
            url = gmail_authorization_url("session-1", "state-value", "user@example.com", "challenge")
            bare = gmail_authorization_url("session-1", "state-value", "user@example.com")
        finally:
            if previous is None:
                os.environ.pop("GOOGLE_CLIENT_ID", None)
            else:
                os.environ["GOOGLE_CLIENT_ID"] = previous
        self.assertIn("state=state-value", url)
        self.assertNotIn("client_secret", url)
        self.assertIn("redirect_uri=", url)
        self.assertIn("code_challenge=challenge", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertNotIn("code_challenge=", bare)
        self.assertIn(dashboard_server.OAUTH_REDIRECT_URI.replace(":", "%3A").replace("/", "%2F"), url)

    def test_oauth_exchange_errors_are_classified(self):
        import urllib.error
        import dashboard_server

        class FakeHTTPError(urllib.error.HTTPError):
            def __init__(self, payload):
                super().__init__("https://oauth2.googleapis.com/token", 400, "Bad Request", hdrs=None, fp=None)
                self._payload = payload.encode("utf-8")

            def read(self):
                return self._payload

        classified = dashboard_server.classify_oauth_http_error(
            FakeHTTPError('{"error":"invalid_grant","error_description":"Bad Request"}')
        )
        self.assertEqual(classified.code, "invalid_grant")
        with self.assertRaises(dashboard_server.OAuthExchangeError) as raised:
            dashboard_server.exchange_gmail_code("", "id", "secret", "http://127.0.0.1/callback", "verifier")
        self.assertEqual(raised.exception.code, "missing_code")

    def test_credential_upsert_survives_repeat_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("gmail", "Gmail")
                database.upsert_account("account-1", "gmail", "user@example.com", "Personal")
                database.upsert_credential("account-1", "ref-one", ["gmail.readonly"])
                database.upsert_credential("account-1", "ref-two", ["gmail.readonly"])
                row = database.credential_for_account("account-1")
                self.assertEqual(row["secret_ref"], "ref-two")
                self.assertEqual(
                    database.connection.execute("SELECT COUNT(*) FROM credentials WHERE account_id='account-1'").fetchone()[0],
                    1,
                )

    def test_legacy_credentials_table_gains_account_unique_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version(version) VALUES (2);
                CREATE TABLE providers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, version TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
                );
                CREATE TABLE accounts_v2 (
                    id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, email TEXT NOT NULL,
                    label TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    last_verified_at TEXT, UNIQUE(provider_id, email)
                );
                CREATE TABLE credentials (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    secret_ref TEXT NOT NULL UNIQUE, scopes_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    last_refresh_at TEXT, revoked_at TEXT
                );
                CREATE TABLE oauth_sessions (
                    id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, account_hint TEXT,
                    state_hash TEXT NOT NULL UNIQUE, redirect_uri TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    completed_at TEXT, error_code TEXT
                );
                CREATE TABLE jobs_v2 (
                    id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, account_id TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, error_code TEXT, request_json TEXT NOT NULL,
                    result_ref TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE job_events (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, ts TEXT NOT NULL,
                    level TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL
                );
                INSERT INTO providers VALUES ('gmail','Gmail','1',1,'t');
                INSERT INTO accounts_v2 VALUES ('acct1','gmail','u@example.com','U','needs_authorization','t',NULL);
                INSERT INTO credentials VALUES ('c1','acct1','ref1','["s"]','active','t',NULL,NULL);
                INSERT INTO credentials VALUES ('c2','acct1','ref2','["s"]','active','t',NULL,NULL);
                """
            )
            connection.commit()
            connection.close()
            with Database(path) as database:
                database.upsert_credential("acct1", "ref3", ["gmail.readonly"])
                self.assertEqual(database.credential_for_account("acct1")["secret_ref"], "ref3")
                self.assertEqual(
                    database.connection.execute("SELECT COUNT(*) FROM credentials").fetchone()[0],
                    1,
                )

    def test_oauth_result_page_returns_control_without_secrets(self):
        import dashboard_server
        page = dashboard_server.oauth_result_page(
            ok=True,
            title="Account connected",
            message="Authorization complete.",
            account_id="account-1",
        )
        self.assertIn("Return to GScrape settings", page)
        self.assertIn("oauth=connected", page)
        self.assertIn("account_id=account-1", page)
        self.assertNotIn("refresh_token", page)
        self.assertNotIn("client_secret", page)
        failed = dashboard_server.oauth_result_page(
            ok=False,
            title="Token exchange failed",
            message="Authorization code was invalid, expired, or already used. Start Connect again.",
            error_code="invalid_grant",
        )
        self.assertIn("oauth=failed", failed)
        self.assertIn("oauth_error=invalid_grant", failed)
        self.assertIn("oauth_message=", failed)
        self.assertNotIn("refresh_token", failed)

    def test_local_secret_store_is_restricted_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalFileSecretStore(Path(directory) / "secrets")
            ref = store.put_json({"refresh_token": "synthetic-token"})
            path = Path(directory) / "secrets" / (ref.id + ".json")
            self.assertEqual(store.get_json(ref)["refresh_token"], "synthetic-token")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("synthetic-token", ref.id)
            store.delete(ref)
            self.assertFalse(path.exists())

    def test_gmail_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = GmailProvider(Path(directory) / "missing.py", Path(directory))
            calls = []
            original = __import__("core.providers", fromlist=["subprocess"]).subprocess.run
            class Result:
                returncode = 1
                stderr = ""
            module = __import__("core.providers", fromlist=["subprocess"])
            module.subprocess.run = lambda *args, **kwargs: (calls.append(1) or Result())
            try:
                provider._run_bounded(["synthetic"], attempts=2)
            finally:
                module.subprocess.run = original
            self.assertEqual(len(calls), 2)

    def test_gmail_session_identity_must_match_selected_account(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = GmailProvider(Path(directory) / "missing.py", Path(directory))
            provider.validate_session_identity("User@Example.com", {"emailAddress": "user@example.com"})
            with self.assertRaises(ValueError):
                provider.validate_session_identity("user@example.com", {"emailAddress": "other@example.com"})

    def test_gmail_execute_verifies_profile_before_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session.env"
            session.write_text("GMAIL_REFRESH_TOKEN=[REDACTED]\n")
            scanner = root / "gscrape.py"
            scanner.write_text("")
            provider = GmailProvider(scanner, root, python_executable="python")
            calls = []
            module = __import__("core.providers", fromlist=["subprocess"])
            original = module.subprocess.run
            class Result:
                returncode = 0
                stdout = '{"emailAddress":"user@example.com"}'
                stderr = ""
            module.subprocess.run = lambda command, **kwargs: (calls.append(command) or Result())
            try:
                provider.execute({"account_id": "a", "expected_email": "user@example.com", "env_file": str(session), "categories": ["identity"]})
            finally:
                module.subprocess.run = original
            self.assertEqual(calls[0][2], "--profile-check")
            self.assertEqual(calls[1][2], "--categories")

    def test_gmail_job_preserves_attachment_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session.env"
            session.write_text("GMAIL_REFRESH_TOKEN=[REDACTED]\n")
            provider = GmailProvider(root / "scanner.py", root)
            config = provider._validate({
                "env_file": str(session),
                "expected_email": "user@example.com",
                "categories": [],
                "attachment_discovery": True,
                "attachment_extensions": ["jpg", ".jpeg"],
                "attachment_filename_regex": r".*\.(?:jpg|jpeg)$",
            })
            self.assertTrue(config["attachment_discovery"])
            self.assertEqual(config["attachment_extensions"], ["jpg", ".jpeg"])

    def test_event_retention_keeps_newest_events(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                database.ensure_provider("demo", "Demo")
                database.connection.execute(
                    "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("account-1", "demo", "example@example.invalid", "Example", "connected", "now"),
                )
                database.connection.commit()
                job_id = database.create_job("account-1", {"operation": "noop"})
                for index in range(4):
                    database.add_job_event(job_id, "progress", {"index": index})
                self.assertEqual(database.prune_job_events(2), 2)
                rows = database.connection.execute("SELECT payload_json FROM job_events ORDER BY id").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertIn('"index": 3', rows[-1][0])

    def test_event_pagination_and_account_health_update(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                database.ensure_provider("demo", "Demo")
                database.connection.execute(
                    "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("account-1", "demo", "example@example.invalid", "Example", "connected", "now"),
                )
                database.connection.commit()
                job_id = database.create_job("account-1", {"operation": "noop"})
                for index in range(3):
                    database.add_job_event(job_id, "progress", {"index": index})
                first = database.list_job_events(job_id, 2)
                self.assertEqual(len(first), 2)
                second = database.list_job_events(job_id, 2, first[-1]["id"])
                self.assertEqual(len(second), 1)
                self.assertTrue(database.update_account_health("account-1", "needs_authorization"))
                self.assertEqual(database.connection.execute("SELECT status FROM accounts_v2 WHERE id='account-1'").fetchone()[0], "needs_authorization")

    def test_database_can_open_after_wal_sidecars_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("demo", "Demo")
            with Database(path) as database:
                self.assertIsNotNone(database.connection.execute("SELECT 1").fetchone())

    def test_result_directory_collision_advances_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            import dashboard_server
            original = dashboard_server.RESULTS_DIR
            dashboard_server.RESULTS_DIR = Path(directory)
            try:
                (Path(directory) / "000001").mkdir()
                run_id, path = reserve_run_directory("000001")
                self.assertEqual(run_id, "000002")
                self.assertTrue(path.is_dir())
            finally:
                dashboard_server.RESULTS_DIR = original

    def test_transaction_retries_transient_locked_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                with Database(Path(directory) / "other.db") as other:
                    database.ensure_provider("demo", "Demo")
                    other.ensure_provider("demo", "Demo")
                    database.connection.execute("BEGIN IMMEDIATE")
                    database.connection.execute("INSERT INTO providers(id,name,version,created_at) VALUES ('x','X','1','now')")
                    database.connection.commit()
                    with other.transaction():
                        other.connection.execute("SELECT 1")

    def test_database_migrates_and_creates_queued_job_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                database.ensure_provider("gmail", "Gmail")
                database.connection.execute(
                    "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("account-1", "gmail", "example@example.invalid", "Example", "connected", "now"),
                )
                database.connection.commit()
                job_id = database.create_job("account-1", {"profile": "default", "limit": 10})
                row = database.connection.execute(
                    "SELECT status, config_snapshot_json FROM jobs_v2 WHERE id = ?", (job_id,)
                ).fetchone()
                self.assertEqual(row["status"], "queued")
                self.assertIn('"limit": 10', row["config_snapshot_json"])

    def test_oauth_state_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                database.ensure_provider("gmail", "Gmail")
                state_hash = hashlib.sha256(b"synthetic-state").hexdigest()
                expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                database.create_oauth_session("gmail", state_hash, "http://127.0.0.1/callback", expires)
                self.assertIsNotNone(database.consume_oauth_state(state_hash))
                self.assertIsNone(database.consume_oauth_state(state_hash))

    def test_expired_oauth_state_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                database.ensure_provider("gmail", "Gmail")
                state_hash = hashlib.sha256(b"expired-state").hexdigest()
                expires = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                database.create_oauth_session("gmail", state_hash, "http://127.0.0.1/callback", expires)
                self.assertIsNone(database.consume_oauth_state(state_hash))

    def test_loopback_port_is_valid(self):
        port = allocate_loopback_port()
        self.assertGreaterEqual(port, 1)
        self.assertLessEqual(port, 65535)

    def test_async_job_runner_persists_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("gmail", "Gmail")
                database.connection.execute(
                    "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("account-1", "gmail", "example@example.invalid", "Example", "connected", "now"),
                )
                database.connection.commit()
                job_id = database.create_job("account-1", {"operation": "scan"})
            runner = AsyncJobRunner(str(path))
            try:
                result = runner.submit(job_id, DemoProvider().execute).result(timeout=3)
                self.assertEqual(result["provider"], "demo")
                self.assertEqual(result["status"], "ok")
            finally:
                runner.shutdown()
            with Database(path) as database:
                self.assertEqual(database.get_job(job_id)["status"], "completed")

    def test_async_job_runner_persists_failure_without_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("gmail", "Gmail")
                database.connection.execute(
                    "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("account-1", "gmail", "example@example.invalid", "Example", "connected", "now"),
                )
                database.connection.commit()
                job_id = database.create_job("account-1", {"token": "not-persisted-by-runner"})
            runner = AsyncJobRunner(str(path))
            try:
                with self.assertRaises(RuntimeError):
                    runner.submit(job_id, lambda _config: (_ for _ in ()).throw(RuntimeError("synthetic failure"))).result(timeout=3)
            finally:
                runner.shutdown()
            with Database(path) as database:
                row = database.get_job(job_id)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_code"], "RuntimeError")

    def test_cancel_and_retry_are_durable_and_events_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with Database(path) as database:
                database.ensure_provider("demo", "Demo")
                database.connection.execute(
                    "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("account-1", "demo", "example@example.invalid", "Example", "connected", "now"),
                )
                database.connection.commit()
                job_id = database.create_job("account-1", {"operation": "fail", "secret": "hidden"})
                self.assertTrue(database.cancel_job(job_id))
                self.assertEqual(database.get_job(job_id)["status"], "cancelled")
                retry_id = database.retry_job(job_id)
                self.assertIsNotNone(retry_id)
                self.assertEqual(database.get_job(retry_id)["status"], "queued")
                event = database.connection.execute("SELECT payload_json FROM job_events WHERE job_id=?", (job_id,)).fetchone()
                self.assertNotIn("hidden", event[0])


if __name__ == "__main__":
    unittest.main()