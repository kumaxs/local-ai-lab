from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_service import docling_adapter  # noqa: E402
from docling_service.cli import build_parser, main  # noqa: E402
from docling_service.contract import STATUS_FAILED_CONVERSION  # noqa: E402


VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"


class AdapterTests(unittest.TestCase):
    def make_pdf(self, tmpdir: str) -> Path:
        path = Path(tmpdir) / "sample.pdf"
        path.write_bytes(b"%PDF-1.4\n% adapter test pdf\n")
        return path

    def test_default_converter_is_placeholder(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--job-uuid",
                VALID_UUID,
                "--input-file-path",
                "/tmp/sample.pdf",
            ]
        )
        self.assertEqual(args.converter, "placeholder")

    def test_adapter_import_is_safe_without_docling(self) -> None:
        self.assertTrue(hasattr(docling_adapter, "is_docling_available"))

    def test_is_docling_available_returns_bool(self) -> None:
        self.assertIsInstance(docling_adapter.is_docling_available(), bool)

    def test_docling_converter_controlled_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = self.make_pdf(tmpdir)
            output_root = Path(tmpdir) / "outputs"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--job-uuid",
                        VALID_UUID,
                        "--input-file-path",
                        str(pdf_path),
                        "--output-root",
                        str(output_root),
                        "--converter",
                        "docling",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertNotEqual(exit_code, 0)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], STATUS_FAILED_CONVERSION)
            self.assertEqual(payload["error"]["code"], "docling_conversion_unavailable")
            self.assertNotIn("Traceback", stdout.getvalue())
            self.assertFalse((output_root / VALID_UUID).exists())


if __name__ == "__main__":
    unittest.main()
