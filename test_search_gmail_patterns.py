import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import dashboard_server

MODULE_PATH = Path(__file__).with_name("gscrape.py")
spec = importlib.util.spec_from_file_location("gscrape", MODULE_PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class PatternTests(unittest.TestCase):
    def test_gourl_obfuscated_variant(self):
        self.assertIn("gourl", scanner.matched_categories("The service is GoURL dot io."))

    def test_recovery_phrase(self):
        self.assertIn("seed", scanner.matched_categories("Store your recovery phrase securely."))

    def test_wallet_file(self):
        self.assertIn("wallet-file", scanner.matched_categories("wallet.dat attached"))

    def test_ethereum_address_candidate(self):
        self.assertIn("crypto-address", scanner.matched_categories("0x" + "a" * 40))

    def test_identity_variant(self):
        self.assertIn("identity", scanner.matched_categories("Contact jonstacey for details"))

    def test_phone_candidate(self):
        self.assertIn("phone", scanner.matched_categories("Call me at (416) 555-0123"))

    def test_attachment_filenames_only(self):
        part = {"filename": "wallet.dat", "parts": [{"filename": "backup.zip"}]}
        self.assertEqual(scanner.attachment_names(part), ["wallet.dat", "backup.zip"])

    def test_secret_like_metadata_redacted(self):
        self.assertNotIn("abcdef0123456789", scanner.redact_metadata("private key: abcdef0123456789"))

    def test_wallet_suspect_detector_ranks_attachment(self):
        result = scanner.extract_wallet_and_recovery_suspects(
            "Please keep the recovery phrase safe.",
            ["wallet.dat"],
        )
        self.assertEqual(result["wallet_files"][0]["source"], "attachment")
        self.assertEqual(result["wallet_files"][0]["confidence"], "high")
        self.assertTrue(result["has_high_confidence"])

    def test_recovery_suspect_detector_redacts_phrase_preview(self):
        words = "alpha bravo cedar delta ember fjord galaxy harbor island jacket kettle lemon"
        result = scanner.extract_wallet_and_recovery_suspects(
            f"Here is my recovery phrase: {words}. Keep it safe."
        )
        self.assertEqual(result["recovery_phrase_count"], 1)
        suspect = result["recovery_phrases"][0]
        self.assertEqual(suspect["word_count"], 12)
        self.assertNotIn("alpha", suspect["preview"])
        self.assertNotIn("lemon", suspect["preview"])
        self.assertIn("RECOVERY_PHRASE_REDACTED", suspect["context_snippet"])

    def test_email_extraction_is_deduplicated(self):
        self.assertEqual(
            scanner.extract_email_addresses("A@Example.com and a@example.com"),
            ["a@example.com"],
        )

    def test_date_parsing_is_timezone_aware(self):
        parsed = scanner.parse_email_date("Tue, 22 Jan 2013 10:00:00 -0500")
        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)

    def test_gmail_date_normalization_handles_inclusive_end(self):
        self.assertEqual(scanner.normalize_gmail_date("2018-12-31"), "2018/12/31")
        self.assertEqual(
            scanner.normalize_gmail_date("2018/12/31", inclusive_end=True),
            "2019/01/01",
        )

    def test_category_selection_supports_individual_and_all_modes(self):
        self.assertEqual(
            scanner.normalize_category_selection(["phone", "wallet-file"]),
            ["wallet-file", "phone"],
        )
        self.assertEqual(
            scanner.normalize_category_selection(category="all"),
            list(scanner.CATEGORY_NAMES),
        )

    def test_category_regex_override_is_compiled(self):
        overrides = scanner.compile_category_regexes({"phone": r"\d{3}[ -]\d{3}"})
        self.assertTrue(overrides["phone"].search("416-555"))

    def test_relevance_score_prioritizes_multiple_categories(self):
        score = scanner.score_match_relevance(
            ["identity", "wallet-file"],
            filenames=["backup.wallet"],
        )
        self.assertGreaterEqual(score, 12.0)

    def test_possible_filename_extraction(self):
        self.assertEqual(
            scanner.extract_possible_filenames("See wallet.dat and wallet.dat."),
            ["wallet.dat"],
        )

    def test_report_helpers_use_synthetic_records(self):
        records = [
            {
                "event": "gmail-crypto-discovery-match",
                "message_date": "Tue, 22 Jan 2013 10:00:00 -0500",
                "message_location": "https://mail.google.com/mail/u/0/#all/msg-1",
                "sender": "sender@example.com",
                "categories": ["identity", "wallet"],
                "subject": "Synthetic test",
                "attachment_names": [],
                "matched_search_passes": ["identity"],
                "extracted_emails": ["should-not-be-exported@example.com"],
            }
        ]
        summary = scanner.summarize_matches(records)
        self.assertEqual(summary["total_matches"], 1)
        self.assertEqual(summary["unique_message_ids"], 1)
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "matches.csv"
            json_path = Path(directory) / "matches.json"
            self.assertEqual(scanner.export_matches_csv(records, csv_path), 1)
            self.assertEqual(scanner.export_matches_json(records, json_path), 1)
            self.assertTrue(csv_path.exists())
            self.assertTrue(json_path.exists())
            exported = json.loads(json_path.read_text())
            self.assertNotIn("extracted_emails", exported[0])

    def test_dashboard_counter_reconciliation_preserves_partial_progress(self):
        current = {"pages": 12, "scanned": 202, "listed": 202, "matches": 189}
        payload = {
            "pages_scanned": 0,
            "messages_scanned": 0,
            "messages_returned": 0,
            "matches": 0,
        }
        self.assertEqual(
            dashboard_server.reconcile_scan_counter(payload, current, "pages_scanned"), 12
        )
        self.assertEqual(
            dashboard_server.reconcile_scan_counter(payload, current, "messages_scanned"), 202
        )
        self.assertEqual(
            dashboard_server.reconcile_scan_counter(payload, current, "matches"), 189
        )

    def test_dashboard_counter_reconciliation_accepts_newer_payload(self):
        current = {"pages": 2, "scanned": 10, "listed": 10, "matches": 3}
        payload = {"pages_scanned": 4, "messages_scanned": 20, "matches": 7}
        self.assertEqual(
            dashboard_server.reconcile_scan_counter(payload, current, "pages_scanned"), 4
        )
        self.assertEqual(
            dashboard_server.reconcile_scan_counter(payload, current, "messages_scanned"), 20
        )
        self.assertEqual(
            dashboard_server.reconcile_scan_counter(payload, current, "matches"), 7
        )

    def test_dashboard_defaults_to_documented_historical_window(self):
        config = dashboard_server.validate_config({})
        self.assertEqual(config["after_date"], "2013/01/01")
        self.assertEqual(config["before_date"], "2018/12/31")


if __name__ == "__main__":
    unittest.main()
