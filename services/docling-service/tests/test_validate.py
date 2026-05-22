from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_service.contract import (  # noqa: E402
    STATUS_FAILED_INVALID_INPUT,
    STATUS_FAILED_UNSUPPORTED_FORMAT,
)
from docling_service.validate import validate_request, validate_uuid4  # noqa: E402


VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"


class ValidateTests(unittest.TestCase):
    def test_valid_uuid4_passes(self) -> None:
        self.assertTrue(validate_uuid4(VALID_UUID))

    def test_invalid_uuid_fails(self) -> None:
        self.assertFalse(validate_uuid4("not-a-uuid"))
        result = validate_request(job_uuid="not-a-uuid", input_file_path="/tmp/example.pdf")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_FAILED_INVALID_INPUT)
        self.assertEqual(result.error_code, "invalid_job_uuid")

    def test_remote_url_rejected(self) -> None:
        result = validate_request(job_uuid=VALID_UUID, input_file_path="https://example.com/a.pdf")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "remote_url_not_allowed")

    def test_unsupported_image_mode_rejected(self) -> None:
        result = validate_request(
            job_uuid=VALID_UUID,
            input_file_path="/tmp/example.pdf",
            image_export_mode="inline",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_image_export_mode")

    def test_timeout_less_than_or_equal_zero_rejected(self) -> None:
        result = validate_request(
            job_uuid=VALID_UUID,
            input_file_path="/tmp/example.pdf",
            timeout_seconds=0,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_timeout_seconds")

    def test_timeout_greater_than_300_rejected(self) -> None:
        result = validate_request(
            job_uuid=VALID_UUID,
            input_file_path="/tmp/example.pdf",
            timeout_seconds=301,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_timeout_seconds")

    def test_missing_file_rejected(self) -> None:
        result = validate_request(job_uuid=VALID_UUID, input_file_path="/tmp/missing-file.pdf")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "input_file_not_found")

    def test_unsupported_extension_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.txt"
            path.write_text("not a pdf", encoding="utf-8")
            result = validate_request(job_uuid=VALID_UUID, input_file_path=str(path))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_FAILED_UNSUPPORTED_FORMAT)
        self.assertEqual(result.error_code, "unsupported_format")


if __name__ == "__main__":
    unittest.main()
