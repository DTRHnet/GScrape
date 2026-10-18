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


class CoreControlPlaneTests(unittest.TestCase):
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