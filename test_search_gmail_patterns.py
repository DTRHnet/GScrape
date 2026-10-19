import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

import dashboard_server

MODULE_PATH = Path(__file__).with_name("gscrape.py")
spec = importlib.util.spec_from_file_location("gscrape", MODULE_PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class PatternTests(unittest.TestCase):
    def test_artifact_capture_defaults_are_fail_closed(self):
        config = dashboard_server.validate_config({})
        self.assertFalse(config.get("acquire_attachments", True))

    def test_adaptive_rate_limiter_paces_and_caps_cooldown(self):
        now = [0.0]
        sleeps = []

        def clock():
            return now[0]

        def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        limiter = scanner.AdaptiveRateLimiter(2, max_delay=3, clock=clock, sleeper=sleeper)
        limiter.wait()
        limiter.wait()
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(limiter.penalize(), 1.0)
        self.assertEqual(limiter.penalize(), 2.0)
        self.assertEqual(limiter.penalize(), 3.0)

    def test_adaptive_rate_limiter_zero_disables_proactive_pacing(self):
        limiter = scanner.AdaptiveRateLimiter(0)
        self.assertEqual(limiter.interval, 0.0)
        self.assertEqual(limiter.wait(), 0.0)

    def test_dashboard_accepts_adjustable_rate_limits(self):
        config = dashboard_server.validate_config({
            "requests_per_second": "0.5",
            "rate_limit_max_delay": "90",
        })
        self.assertEqual(config["requests_per_second"], 0.5)
        self.assertEqual(config["rate_limit_max_delay"], 90.0)

    def test_dashboard_attachment_filters_require_attachment_discovery(self):
        config = dashboard_server.validate_config({
            "attachment_extensions": ["pdf", "dat"],
            "attachment_filename_regex": r"invoice-\d+\.pdf$",
        })
        self.assertFalse(config["attachment_discovery"])
        self.assertEqual(config["attachment_extensions"], [])
        section = dashboard_server.validate_config({
            "categories": ["attachments"],
            "attachment_extensions": ["pdf"],
        })
        self.assertTrue(section["attachment_discovery"])
        self.assertEqual(section["categories"], [])
        enabled = dashboard_server.validate_config({
            "attachment_discovery": True,
            "attachment_extensions": ["pdf"],
        })
        self.assertTrue(enabled["attachment_discovery"])
        self.assertEqual(enabled["attachment_extensions"], [".pdf"])

    def test_dashboard_allows_attachment_only_search(self):
        config = dashboard_server.validate_config({
            "attachment_discovery": True,
            "attachment_extensions": ["pdf"],
        })
        self.assertEqual(config["categories"], [])
        self.assertFalse(config["acquire_attachments"])

    def test_dashboard_scan_command_contains_opt_in_acquisition_path(self):
        source = Path(dashboard_server.__file__).read_text(encoding="utf-8")
        run_scan = source[source.index("def run_scan"):source.index("class Handler")]
        self.assertIn('"--acquire-attachments"', run_scan)
        self.assertIn('"--attachment-output-dir"', run_scan)

    def test_explicit_acquisition_requires_attachment_discovery(self):
        with self.assertRaises(ValueError):
            dashboard_server.validate_config({"acquire_attachments": True})

    def test_explicit_acquisition_is_preserved(self):
        config = dashboard_server.validate_config({
            "attachment_discovery": True,
            "attachment_extensions": ["png"],
            "acquire_attachments": True,
        })
        self.assertTrue(config["attachment_discovery"])
        self.assertTrue(config["acquire_attachments"])

    def test_account_connection_requires_matching_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session.env"
            session.write_text("GMAIL_REFRESH_TOKEN=[REDACTED]\n")
            row = {"session_file": str(session), "email": "mayhemservers@gmail.com"}
            original = dashboard_server.subprocess.run
            class Result:
                returncode = 0
                stdout = '{"emailAddress":"other@example.com"}'
                stderr = ""
            dashboard_server.subprocess.run = lambda *args, **kwargs: Result()
            try:
                self.assertFalse(dashboard_server.account_is_connected(row))
            finally:
                dashboard_server.subprocess.run = original

    def test_attachment_only_query_pass_is_has_attachment(self):
        self.assertEqual(
            scanner.domains_for_categories([], True),
            ["attachments"],
        )
        self.assertEqual(scanner.attachment_discovery_query([]), "has:attachment")
        self.assertEqual(
            scanner.attachment_discovery_query(["jpg", "jpeg", "png", "gif", "webp"]),
            "has:attachment filename:(jpg OR jpeg OR png OR gif OR webp)",
        )
        self.assertEqual(
            scanner.attachment_discovery_query([".pdf"]),
            "has:attachment filename:pdf",
        )

    def test_dashboard_validates_contact_filters(self):
        config = dashboard_server.validate_config({
            "domains": ["contacts"],
            "contact_email_regex": r".+@example\.com$",
            "contact_email_domain": "example.com",
        })
        self.assertEqual(config["contact_email_domain"], "example.com")
        with self.assertRaises(ValueError):
            dashboard_server.validate_config({
                "domains": ["contacts"],
                "contact_email_domain": "example.com; DROP TABLE contacts",
            })

    def test_dashboard_rejects_unsafe_rate_limit_values(self):
        with self.assertRaises(ValueError):
            dashboard_server.validate_config({"requests_per_second": "11"})
        with self.assertRaises(ValueError):
            dashboard_server.validate_config({"rate_limit_max_delay": "0"})
    def assertCategoryMatch(self, category, positive, negative=()):
        for value in positive:
            with self.subTest(category=category, value=value, expected=True):
                self.assertIn(category, scanner.matched_categories(value))
        for value in negative:
            with self.subTest(category=category, value=value, expected=False):
                self.assertNotIn(category, scanner.matched_categories(value))

    def test_each_filter_type_has_positive_and_boundary_cases(self):
        self.assertCategoryMatch(
            "gourl",
            ("gourl.io", "Go URL dot io", "g o-u-r-l [dot] io"),
            ("gourlio", "notgourl.io", "gourl.example"),
        )
        self.assertCategoryMatch(
            "wallet",
            ("wallet", "Bitcoin Core", "hardware wallet", "Ledger"),
            ("notawallet", "walletish", "ledgering"),
        )
        self.assertCategoryMatch(
            "seed",
            ("seed phrase", "recovery phrase", "BIP-39", "private key"),
            ("seedling", "recoveryphrases", "public key"),
        )
        self.assertCategoryMatch(
            "wallet-file",
            ("wallet.dat", "WalBackup.DAT", "keystore.json", "wal-copy.dat"),
            ("wallet.datx", "notes.txt"),
        )
        self.assertCategoryMatch(
            "crypto-address",
            ("0x" + "a" * 40, "1" + "A" * 33, "bc1" + "q" * 30, "ltc1" + "q" * 30),
            ("0x" + "a" * 39, "hello0x" + "a" * 40),
        )
        self.assertCategoryMatch(
            "identity",
            ("jstacey", "jonstacey", "jstacy", "jonstacy", "johnstacy", "johnstacey"),
            ("notjonstacey", "jon-stacey", "staceyjon"),
        )
        self.assertCategoryMatch(
            "phone",
            ("(416) 555-0123", "+1 416 555 0123", "4165550123"),
            ("1165550123", "123-45-6789", "416-555-012"),
        )

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

    def test_attachment_filter_supports_multiple_extensions_and_regex(self):
        self.assertTrue(scanner.attachment_filter_matches(
            ["invoice-2024.PDF", "notes.txt"], ["pdf", ".dat"], r"invoice-\d+\.pdf$"
        ))
        self.assertFalse(scanner.attachment_filter_matches(["notes.txt"], ["pdf"]))

    def test_attachment_extension_validation(self):
        self.assertEqual(scanner.normalize_attachment_extensions(["PDF", ".dat", "pdf"]), [".pdf", ".dat"])
        with self.assertRaises(ValueError):
            scanner.normalize_attachment_extensions(["../secret"])

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

    def test_dashboard_leaves_date_window_unrestricted_when_unset(self):
        config = dashboard_server.validate_config({"attachment_discovery": True})
        self.assertEqual(config["after_date"], "")
        self.assertEqual(config["before_date"], "")
        self.assertEqual(config["max_messages"], 10000)

    def test_dashboard_treats_blank_max_messages_as_unbounded(self):
        config = dashboard_server.validate_config({
            "attachment_discovery": True,
            "max_messages": "",
        })
        self.assertEqual(config["max_messages"], 10000)

    def test_dashboard_honors_explicit_date_and_message_limits(self):
        config = dashboard_server.validate_config({
            "attachment_discovery": True,
            "after_date": "2013/01/01",
            "before_date": "2018/12/31",
            "max_messages": 200,
        })
        self.assertEqual(config["after_date"], "2013/01/01")
        self.assertEqual(config["before_date"], "2018/12/31")
        self.assertEqual(config["max_messages"], 200)


if __name__ == "__main__":
    unittest.main()
