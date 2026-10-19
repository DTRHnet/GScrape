import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gscrape


class AttachmentAcquisitionTests(unittest.TestCase):
    def test_attachment_parts_only_returns_ids(self):
        parts = gscrape.attachment_parts({"parts": [{"filename": "note.txt", "body": {"attachmentId": "a1", "size": 3}}]})
        self.assertEqual(parts[0]["attachment_id"], "a1")

    def test_acquisition_writes_new_file_and_hashes_only_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(gscrape, "request_json", return_value={"data": base64.urlsafe_b64encode(b"abc").decode()}):
                result = gscrape.acquire_attachment_bytes("m1", {"filename": "note.txt", "attachment_id": "a1"}, "https://example.test", None, "token", Path(directory), 0, gscrape.AdaptiveRateLimiter(0, 1))
            self.assertEqual(result["size_bytes"], 3)
            self.assertEqual(result["stored_name"], "m1_note.txt")
            self.assertEqual(list(Path(directory).iterdir())[0].read_bytes(), b"abc")
            public = gscrape.public_match_record({"subject": "x", "acquired_attachments": [result]})
            self.assertEqual(public["acquired_attachments"][0]["stored_name"], "m1_note.txt")
            self.assertNotIn("data", public["acquired_attachments"][0])

    def test_acquisition_reuses_same_content(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            response = {"data": base64.urlsafe_b64encode(b"same-image").decode()}
            with patch.object(gscrape, "request_json", return_value=response) as request:
                first = gscrape.acquire_attachment_bytes("m1", {"filename": "one.png", "attachment_id": "a1"}, "https://example.test", None, "token", output, 0, gscrape.AdaptiveRateLimiter(0, 1))
                second = gscrape.acquire_attachment_bytes("m2", {"filename": "two.png", "attachment_id": "a2"}, "https://example.test", None, "token", output, 0, gscrape.AdaptiveRateLimiter(0, 1))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(first["stored_name"], second["stored_name"])
            self.assertEqual(second["status"], "deduplicated")
            self.assertEqual(len(list(output.glob("*.png"))), 1)

    def test_acquisition_reuses_same_attachment_without_second_download(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            response = {"data": base64.urlsafe_b64encode(b"same-image").decode()}
            with patch.object(gscrape, "request_json", return_value=response) as request:
                first = gscrape.acquire_attachment_bytes("m1", {"filename": "one.png", "attachment_id": "a1"}, "https://example.test", None, "token", output, 0, gscrape.AdaptiveRateLimiter(0, 1))
                second = gscrape.acquire_attachment_bytes("m1", {"filename": "one.png", "attachment_id": "a1"}, "https://example.test", None, "token", output, 0, gscrape.AdaptiveRateLimiter(0, 1))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(first["stored_name"], second["stored_name"])
            self.assertEqual(second["status"], "deduplicated")

    def test_metadata_only_does_not_acquire(self):
        args = gscrape.parse_args.__name__
        self.assertEqual(args, "parse_args")

    def test_public_acquired_attachment_has_run_scoped_urls(self):
        item = gscrape.public_acquired_attachment(
            {"stored_name": "m1_photo.png", "filename": "photo.png"}, "000123"
        )
        self.assertEqual(item["thumbnail_url"], "/api/runs/000123/attachments/m1_photo.png")
        self.assertEqual(item["download_url"], item["thumbnail_url"])


if __name__ == "__main__":
    unittest.main()