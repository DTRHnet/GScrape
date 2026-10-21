import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gscrape


class ReportingAnalysisTests(unittest.TestCase):
    def test_promotional_records_are_marked_and_excluded_from_category_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matches.jsonl"
            rows = [
                {"event": "gmail-crypto-discovery-match", "subject": "Weekly crypto newsletter - bonus offer", "safe_context": "unsubscribe", "categories": ["wallet"]},
                {"event": "gmail-crypto-discovery-match", "subject": "Your wallet.dat backup", "categories": ["wallet"], "attachment_names": ["wallet.dat"]},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            records = gscrape.load_match_records(path)
            self.assertTrue(records[0]["suppressed"])
            summary = gscrape.summarize_matches(records)
            self.assertEqual(summary["suppressed_promotional_count"], 1)
            self.assertEqual(summary["category_counts"], {"wallet": 1})

    def test_llm_payload_is_redacted_and_category_scoped(self):
        records = [{"categories": ["wallet"], "subject": "safe", "body": "secret", "suppressed": False}]
        payload = gscrape.build_llm_analysis_payload(records, "wallet")
        self.assertEqual(len(payload["records"]), 1)
        self.assertNotIn("body", payload["records"][0])

    @patch("gscrape.urllib.request.urlopen")
    def test_llm_client_uses_compatible_chat_endpoint(self, urlopen):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self): return b'{"choices":[{"message":{"content":"analysis"}}]}'
        urlopen.return_value = Response()
        self.assertEqual(gscrape.analyze_with_llm({}, "https://example.test/v1", "key", "model"), "analysis")
        self.assertTrue(urlopen.called)


if __name__ == "__main__":
    unittest.main()