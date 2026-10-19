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


if __name__ == "__main__":
    unittest.main()