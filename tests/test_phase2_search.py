import tempfile
import unittest
from pathlib import Path

from core.search import SearchPlan, build_gmail_query, normalize_result
from core.storage import Database
from core.artifacts import LocalAESGCMArtifactBackend, assess_relevance, capture_payload


class Phase2SearchTests(unittest.TestCase):
    def test_promotional_candidate_is_suppressed(self):
        assessment = assess_relevance({"subject": "Weekly crypto newsletter - bonus offer", "safe_context": "unsubscribe"})
        self.assertTrue(assessment.suppressed)
        self.assertEqual(assessment.classification, "promotional")

    def test_strong_wallet_signal_overrides_promotional_footer(self):
        assessment = assess_relevance({"subject": "Your wallet.dat backup", "safe_context": "unsubscribe"})
        self.assertFalse(assessment.suppressed)
        self.assertIn("strong_wallet_or_recovery_signal", assessment.reasons)

    def test_metadata_only_capture_never_requires_backend(self):
        result = capture_payload(None, b"synthetic", metadata={}, mode="metadata-only")
        self.assertEqual(result["status"], "metadata-only")
        self.assertEqual(result["size_bytes"], 9)

    def test_capture_requires_encrypted_backend(self):
        with self.assertRaises(RuntimeError):
            capture_payload(None, b"synthetic", metadata={}, mode="capture-selected")

    def test_capture_backend_receives_payload_only_through_capture_api(self):
        class Backend:
            def __init__(self):
                self.payload = None

            def put(self, payload, *, metadata):
                self.payload = (payload, dict(metadata))
                return "opaque-ref"

        backend = Backend()
        result = capture_payload(backend, b"synthetic", metadata={"type": "test"}, mode="capture-selected")
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["artifact_ref"], "opaque-ref")
        self.assertEqual(backend.payload[0], b"synthetic")

    def test_aes_gcm_backend_round_trip_and_opaque_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = LocalAESGCMArtifactBackend(directory)
            result = capture_payload(backend, b"synthetic protected payload", metadata={"type": "test"}, mode="capture-selected")
            self.assertEqual(backend.get(result["artifact_ref"]), b"synthetic protected payload")
            self.assertNotIn(b"synthetic protected payload", Path(directory, result["artifact_ref"] + ".json").read_bytes())

    def test_schema_three_contains_protected_metadata_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                names = {row["name"] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"protected_artifacts", "capture_events"}.issubset(names))

    def test_gmail_query_uses_provider_side_filters(self):
        plan = SearchPlan(("content",), query="invoice", after="2013/01/01", before="2018/12/31", sender="a@example.test", subject="receipt", mime="pdf")
        self.assertEqual(build_gmail_query(plan), '(invoice) from:a@example.test subject:receipt filename:pdf after:2013/01/01 before:2018/12/31')

    def test_normalized_result_preserves_non_secret_metadata(self):
        result = normalize_result({"provider": "Gmail", "id": "m1", "categories": ["content"]})
        self.assertEqual(result.message_id, "m1")
        self.assertEqual(result.domains, ("content",))

    def test_schema_two_tables_migrate_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                names = {row["name"] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"messages", "contacts", "phone_numbers", "attachments", "evidence"}.issubset(names))

    def test_normalized_persistence_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "state.db") as database:
                record = {"provider": "Gmail", "message_id": "m1", "subject": "safe", "domains": ("content",), "evidence": ({"domain": "content", "source": "message", "detail": "matched"},), "attachments": ({"filename": "note.txt", "status": "metadata"},)}
                database.persist_normalized_result(record)
                database.persist_normalized_result(record)
                self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()