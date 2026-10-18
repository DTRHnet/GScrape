import hashlib
import tempfile
import unittest
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ports import allocate_loopback_port
from core.storage import Database
from core.async_jobs import AsyncJobRunner
from core.providers import DemoProvider


class CoreControlPlaneTests(unittest.TestCase):
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