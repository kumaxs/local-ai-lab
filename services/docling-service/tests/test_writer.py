from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_service.converter import placeholder_convert  # noqa: E402
from docling_service.contract import REQUIRED_SUCCESS_OUTPUTS, STATUS_SUCCESS  # noqa: E402
from docling_service.writer import write_docling_outputs  # noqa: E402


UUID_ONE = "550e8400-e29b-41d4-a716-446655440000"
UUID_TWO = "7d444840-9dc0-4c18-93b9-43a4f33c0c18"

METADATA_FIELDS = {
    "job_uuid",
    "display_name",
    "original_name",
    "source_name",
    "input_file_path",
    "input_sha256",
    "file_size_bytes",
    "input_mtime",
    "detected_format",
    "page_count",
    "docling_version",
    "image_export_mode",
    "requested_outputs",
    "generated_outputs",
    "link_count",
    "table_count",
    "asset_count",
}

STATUS_FIELDS = {
    "job_uuid",
    "status",
    "started_at",
    "finished_at",
    "duration_seconds",
    "input_file_path",
    "input_sha256",
    "output_dir",
    "outputs_written",
    "warnings",
    "error_code",
    "error_message",
}


class WriterTests(unittest.TestCase):
    def make_pdf(self, tmpdir: str, name: str = "sample.pdf") -> Path:
        path = Path(tmpdir) / name
        path.write_bytes(b"%PDF-1.4\n% placeholder test pdf\n")
        return path

    def test_placeholder_outputs_and_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_pdf = self.make_pdf(tmpdir)
            output_root = Path(tmpdir) / "outputs"
            result = placeholder_convert(
                job_uuid=UUID_ONE,
                input_file_path=input_pdf,
                output_root=output_root,
                display_name="same-name.pdf",
            )

            output_dir = Path(result["output_dir"])
            self.assertEqual(output_dir.name, UUID_ONE)
            for filename in REQUIRED_SUCCESS_OUTPUTS:
                self.assertTrue((output_dir / filename).exists(), filename)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))

            self.assertTrue(METADATA_FIELDS.issubset(metadata.keys()))
            self.assertTrue(STATUS_FIELDS.issubset(status.keys()))
            self.assertEqual(metadata["job_uuid"], UUID_ONE)
            self.assertEqual(status["job_uuid"], UUID_ONE)
            self.assertEqual(status["status"], STATUS_SUCCESS)
            self.assertIn("placeholder_conversion_only", status["warnings"])

            copied_original = output_dir / input_pdf.name
            self.assertFalse(copied_original.exists())

    def test_same_file_with_different_uuid_creates_different_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_pdf = self.make_pdf(tmpdir)
            output_root = Path(tmpdir) / "outputs"

            first = placeholder_convert(
                job_uuid=UUID_ONE,
                input_file_path=input_pdf,
                output_root=output_root,
                display_name="duplicate.pdf",
            )
            second = placeholder_convert(
                job_uuid=UUID_TWO,
                input_file_path=input_pdf,
                output_root=output_root,
                display_name="duplicate.pdf",
            )

            self.assertNotEqual(Path(first["output_dir"]), Path(second["output_dir"]))
            self.assertTrue(Path(first["output_dir"]).exists())
            self.assertTrue(Path(second["output_dir"]).exists())

            first_metadata = json.loads(
                (Path(first["output_dir"]) / "metadata.json").read_text(encoding="utf-8")
            )
            second_metadata = json.loads(
                (Path(second["output_dir"]) / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_metadata["display_name"], "duplicate.pdf")
            self.assertEqual(second_metadata["display_name"], "duplicate.pdf")
            self.assertEqual(first_metadata["input_sha256"], second_metadata["input_sha256"])
            self.assertNotEqual(first_metadata["job_uuid"], second_metadata["job_uuid"])

    def test_write_docling_outputs_with_optional_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_pdf = self.make_pdf(tmpdir)
            output_root = Path(tmpdir) / "outputs"
            result = write_docling_outputs(
                job_uuid=UUID_ONE,
                input_file_path=input_pdf,
                output_root=output_root,
                display_name="docling.pdf",
                conversion={
                    "markdown": "# Converted\n",
                    "html": "<html><body><h1>Converted</h1></body></html>\n",
                    "document_dict": {"pages": [{"page_no": 1}], "body": "Converted"},
                    "text": "Converted\n",
                    "doctags": "<document>\n",
                    "warnings": ["text_export_failed"],
                    "docling_version": "2.95.0",
                },
            )

            output_dir = Path(result["output_dir"])
            for filename in (
                "document.md",
                "document.html",
                "document.json",
                "metadata.json",
                "status.json",
                "text.txt",
                "doctags.txt",
            ):
                self.assertTrue((output_dir / filename).exists(), filename)

            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))

            self.assertEqual(metadata["docling_version"], "2.95.0")
            self.assertEqual(metadata["page_count"], 1)
            self.assertEqual(status["status"], STATUS_SUCCESS)
            self.assertEqual(status["warnings"], ["text_export_failed"])
            self.assertEqual(metadata["generated_outputs"], status["outputs_written"])
            self.assertIn("text.txt", metadata["generated_outputs"])
            self.assertIn("doctags.txt", metadata["generated_outputs"])
            self.assertFalse((output_dir / input_pdf.name).exists())


if __name__ == "__main__":
    unittest.main()
