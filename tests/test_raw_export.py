import base64
import json
import tempfile
import unittest
from pathlib import Path

from core.raw_export import RawMailboxDatabase, attachment_matches_type, export_gmail_mailbox, matches_attachment_filter
from core.header_analysis import analyze_email_headers


class RawExportTests(unittest.TestCase):
    def test_raw_database_isolated_and_preserves_message_and_attachment_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = {
                "id": "m1", "threadId": "t1", "internalDate": "123",
                "payload": {"headers": [{"name": "Subject", "value": "unredacted"}], "parts": [
                    {"filename": "note.txt", "mimeType": "text/plain", "body": {"attachmentId": "a1", "size": 3}}
                ]},
            }
            pages = [{"messages": [{"id": "m1"}]}]

            def request(url, api_key, access_token, params, **kwargs):
                if params.get("format") == "raw":
                    return {"raw": base64.urlsafe_b64encode(b"Subject: unredacted\\r\\n\\r\\nbody").decode()}
                if params.get("format") == "full":
                    return message
                if "/attachments/" in url:
                    return {"data": base64.urlsafe_b64encode(b"abc").decode()}
                return pages.pop(0) if pages else {}

            result = export_gmail_mailbox(
                database_path=root / "raw-mailbox.db", blob_root=root / "blobs",
                account="me", query="", max_messages=10, request_json=request,
                access_token="token", include_attachments=True,
            )
            self.assertEqual(result["status"], "completed")
            with RawMailboxDatabase(root / "raw-mailbox.db") as database:
                row = database.connection.execute("SELECT raw_api_json, raw_rfc822 FROM raw_messages").fetchone()
                self.assertIn("unredacted", row["raw_api_json"])
                self.assertIn(b"Subject: unredacted", row["raw_rfc822"])
                attachment = database.connection.execute("SELECT download_status, sha256, blob_path FROM raw_attachments").fetchone()
                self.assertEqual(attachment["download_status"], "downloaded")
                self.assertTrue((root / "blobs" / attachment["blob_path"]).is_file())

    def test_attachment_filter(self):
        self.assertTrue(matches_attachment_filter("report.PDF", ["pdf"], ""))
        self.assertTrue(attachment_matches_type("photo.jpg", "image/jpeg", "image"))
        self.assertTrue(attachment_matches_type("report.docx", "application/octet-stream", "document"))
        self.assertFalse(attachment_matches_type("report.zip", "application/zip", "image"))
        self.assertFalse(matches_attachment_filter("report.txt", ["pdf"], ""))
        self.assertTrue(matches_attachment_filter("report.txt", [], r"\.txt$"))

    def test_resume_does_not_duplicate_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            message = {"id": "m1", "payload": {}}

            def request(url, api_key, access_token, params, **kwargs):
                calls.append((url, params))
                if params.get("format") == "raw":
                    return {"raw": base64.urlsafe_b64encode(b"raw").decode()}
                if params.get("format") == "full":
                    return message
                return {"messages": [{"id": "m1"}]}

            first = export_gmail_mailbox(database_path=root / "db", blob_root=root / "blobs", account="me", query="", max_messages=1, request_json=request, access_token="x")
            second = export_gmail_mailbox(database_path=root / "db", blob_root=root / "blobs", account="me", query="", max_messages=1, request_json=request, access_token="x", export_id=first["export_id"])
            with RawMailboxDatabase(root / "db") as database:
                self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0], 1)
            self.assertEqual(second["status"], "completed")

    def test_read_helpers_page_messages_and_join_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RawMailboxDatabase(root / "db") as database:
                export_id = database.create_or_resume(None, "me", "", False, "")
                first = database.put_message(export_id, {"id": "m1", "threadId": "t1", "internalDate": "200"}, b"one")
                second = database.put_message(export_id, {"id": "m2", "threadId": "t2", "internalDate": "100"}, b"two")
                database.put_attachment(first, {"filename": "note.txt", "mime_type": "text/plain", "size_bytes": 3}, status="downloaded", content=b"abc")
                database.connection.commit()

                page, total = database.list_messages(export_id, limit=1, offset=0)
                self.assertEqual(len(page), 1)
                self.assertEqual(total, 2)
                self.assertEqual(page[0]["provider_message_id"], "m1")
                self.assertEqual(page[0]["attachment_count"], 1)
                self.assertEqual(database.message(export_id, first)["raw_rfc822"], b"one")
                attachments = database.attachments(export_id, first)
                self.assertEqual(len(attachments), 1)
                self.assertEqual(attachments[0]["content"], b"abc")
                self.assertEqual(database.list_messages(export_id, limit=1, offset=1)[0][0]["provider_message_id"], "m2")

    def test_message_filters_match_subject_sender_and_attachment_category(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_raw = b"From: Alice <alice@example.test>\r\nSubject: Project photo\r\n\r\nbody"
            second_raw = b"From: Bob <bob@example.test>\r\nSubject: Quarterly report\r\n\r\nbody"
            with RawMailboxDatabase(root / "db") as database:
                export_id = database.create_or_resume(None, "me", "", False, "")
                first = database.put_message(export_id, {"id": "m1", "internalDate": "200"}, first_raw)
                second = database.put_message(export_id, {"id": "m2", "internalDate": "100"}, second_raw)
                database.put_attachment(first, {"filename": "photo.jpg", "mime_type": "image/jpeg"}, status="metadata-only")
                database.put_attachment(second, {"filename": "report.pdf", "mime_type": "application/pdf"}, status="metadata-only")
                database.connection.commit()

                rows, total = database.list_messages(export_id, subject="photo")
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]["provider_message_id"], "m1")
                rows, total = database.list_messages(export_id, sender="BOB@EXAMPLE.TEST")
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]["provider_message_id"], "m2")
                rows, total = database.list_messages(export_id, attachment_type="image")
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]["provider_message_id"], "m1")

    def test_header_analysis_flags_authentication_and_address_mismatches(self):
        raw = (
            b"From: Billing <billing@example.com>\r\n"
            b"Reply-To: payments@evil.example\r\n"
            b"Return-Path: <bounce@evil.example>\r\n"
            b"Message-ID: <abc@evil.example>\r\n"
            b"Date: Tue, 22 Oct 2026 12:00:00 +0000\r\n"
            b"Authentication-Results: mx.example; spf=fail; dkim=none; dmarc=fail\r\n"
            b"Received: from attacker.example (203.0.113.10) by mx.example; Tue, 22 Oct 2026 12:00:00 +0000\r\n"
            b"Subject: Urgent payment\r\n\r\nbody"
        )
        result = analyze_email_headers(raw)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertEqual(result["risk_level"], "high")
        self.assertIn("from-reply-domain-mismatch", codes)
        self.assertIn("from-return-path-domain-mismatch", codes)
        self.assertIn("spf-fail", codes)
        self.assertIn("dmarc-fail", codes)
        self.assertEqual(result["received_hops"][0]["ips"], ["203.0.113.10"])
        self.assertEqual(result["fields"]["subject"], "Urgent payment")
        self.assertEqual(result["fields"]["from"][0]["address"], "billing@example.com")
        self.assertEqual(result["fields"]["reply_to"][0]["address"], "payments@evil.example")

    def test_header_analysis_handles_missing_raw_bytes(self):
        result = analyze_email_headers(None)
        self.assertFalse(result["available"])
        self.assertEqual(result["risk_level"], "unknown")


if __name__ == "__main__":
    unittest.main()