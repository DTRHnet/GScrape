import tempfile
import unittest
from pathlib import Path

from core.search import SearchPlan, build_gmail_query, normalize_result
from core.storage import Database


class Phase2SearchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()