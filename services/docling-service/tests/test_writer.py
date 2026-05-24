from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_service.converter import placeholder_convert  # noqa: E402
from docling_service.contract import REQUIRED_SUCCESS_OUTPUTS, STATUS_SUCCESS  # noqa: E402
from docling_service.quality import count_tables, measure_gxx_quality  # noqa: E402
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
    "conversion_policy",
    "ocr_fallback_used",
    "text_quality_gxx_count",
    "text_quality_gxx_density",
    "table_count",
    "asset_count",
    "generated_outputs",
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
    "conversion_policy",
    "ocr_fallback_used",
    "text_quality_gxx_count",
    "text_quality_gxx_density",
}


class FakePILImage:
    size = (600, 800)

    def crop(self, box: tuple[int, int, int, int]) -> "FakePILImage":
        return self

    def save(self, path: Path, format: str | None = None) -> None:
        path.write_bytes(b"fake png bytes")


class FakeImage:
    pil_image = FakePILImage()

    def save(self, path: Path) -> None:
        path.write_bytes(b"fake png bytes")


class FakePage:
    image = FakeImage()

    class size:
        width = 300
        height = 400


class FakeTable:
    def export_to_html(self, doc: object | None = None) -> str:
        if doc is None:
            return ""
        return "<table><tr><td>cell</td></tr></table>"

    def export_to_markdown(self, doc: object | None = None) -> str:
        if doc is None:
            return ""
        return "| value |\n| --- |\n| cell |\n"

    def get_image(self, doc: object | None = None) -> FakeImage:
        return FakeImage()


class FakeFormula:
    label = "formula"
    text = "Formula not decoded"

    class prov_item:
        page_no = 1

        class bbox:
            l = 50
            t = 250
            r = 140
            b = 230
            coord_origin = "BOTTOMLEFT"

    prov = [prov_item()]

    def get_image(self, doc: object | None = None) -> FakeImage:
        return FakeImage()


class FakeDocument:
    pages = {1: FakePage()}
    pictures: list[object] = []
    tables = [FakeTable()]
    texts = [FakeFormula()]

    def save_as_html(self, filename: str, artifacts_dir: Path | None = None, image_mode: object = None) -> None:
        Path(filename).write_text(
            "<html><head><title>Converted</title></head><body>"
            "<h1>Converted</h1><p>Formula not decoded</p><p><!-- image placeholder --></p>"
            "</body></html>\n",
            encoding="utf-8",
        )


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
                    "html": "<html><body><h1>Converted</h1><p>Formula not decoded</p></body></html>\n",
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
            self.assertIn("text_export_failed", status["warnings"])
            self.assertIn("asset_extraction_unavailable_no_docling_image_candidates", status["warnings"])
            self.assertEqual(metadata["generated_outputs"], status["outputs_written"])
            self.assertIn("text.txt", metadata["generated_outputs"])
            self.assertIn("doctags.txt", metadata["generated_outputs"])
            self.assertFalse((output_dir / input_pdf.name).exists())

    def test_high_gxx_density_fails_quality(self) -> None:
        text = " ".join(["/G21"] * 25)
        quality = measure_gxx_quality(text)
        self.assertTrue(quality.failed)
        self.assertEqual(quality.gxx_count, 25)

    def test_low_gxx_density_passes_quality(self) -> None:
        text = "/G21 " + ("readable text " * 1000)
        quality = measure_gxx_quality(text)
        self.assertFalse(quality.failed)
        self.assertEqual(quality.gxx_count, 1)

    def test_table_count_from_document_dict(self) -> None:
        self.assertEqual(
            count_tables(
                {
                    "tables": [
                        {"label": "table", "data": {"num_rows": 2, "num_cols": 2}},
                        {"label": "table", "data": {"num_rows": 1, "num_cols": 3}},
                    ]
                }
            ),
            2,
        )
        self.assertEqual(
            count_tables({"body": {"children": [{"label": "table", "self_ref": "#/tables/0"}]}}),
            1,
        )

    def test_table_json_and_asset_outputs_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_pdf = self.make_pdf(tmpdir)
            output_root = Path(tmpdir) / "outputs"
            result = write_docling_outputs(
                job_uuid=UUID_ONE,
                input_file_path=input_pdf,
                output_root=output_root,
                conversion={
                    "markdown": "# Converted\n",
                    "html": "<html><body><h1>Converted</h1><p>Formula not decoded</p></body></html>\n",
                    "document_dict": {
                        "pages": [{"page_no": 1}],
                        "tables": [
                            {
                                "label": "table",
                                "data": {
                                    "num_rows": 1,
                                    "num_cols": 1,
                                    "table_cells": [{"text": "cell", "row_span": 1, "col_span": 1}],
                                },
                            }
                        ],
                    },
                    "document": FakeDocument(),
                    "text": "Converted\n",
                    "warnings": [],
                    "docling_version": "2.95.0",
                    "conversion_policy": "quality_first",
                    "ocr_fallback_used": False,
                    "text_quality_gxx_count": 0,
                    "text_quality_gxx_density": 0.0,
                },
            )

            output_dir = Path(result["output_dir"])
            self.assertTrue((output_dir / "tables" / "table_1.json").exists())
            self.assertTrue((output_dir / "tables" / "table_1.html").exists())
            self.assertTrue((output_dir / "tables" / "table_1.md").exists())
            self.assertTrue((output_dir / "assets" / "page_1.png").exists())
            self.assertTrue((output_dir / "assets" / "table_1.png").exists())
            self.assertTrue((output_dir / "assets" / "formula_1.png").exists())
            self.assertTrue((output_dir / "assets" / "formula_1_context.png").exists())
            document_html = (output_dir / "document.html").read_text(encoding="utf-8")
            self.assertIn('src="assets/page_1.png"', document_html)
            self.assertIn('src="assets/table_1.png"', document_html)
            self.assertIn('src="assets/formula_1.png"', document_html)
            self.assertIn('src="assets/formula_1_context.png"', document_html)
            self.assertIn('href="assets/formula_1_context.png"', document_html)
            self.assertIn("Formula not decoded (review formula 1)", document_html)
            self.assertIn('href="tables/table_1.html"', document_html)
            self.assertIn("<table><tr><td>cell</td></tr></table>", document_html)
            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["conversion_policy"], "quality_first")
            self.assertEqual(metadata["table_count"], 1)
            self.assertEqual(metadata["asset_count"], 4)
            self.assertEqual(metadata["table_image_count"], 1)
            self.assertEqual(metadata["formula_count"], 1)
            self.assertEqual(metadata["formula_asset_count"], 1)
            self.assertEqual(metadata["formula_context_asset_count"], 1)
            self.assertEqual(metadata["formula_placeholder_link_count"], 1)
            self.assertGreater(metadata["formula_placeholder_count"], 0)
            self.assertEqual(status["table_count"], 1)
            self.assertEqual(status["asset_count"], 4)
            self.assertEqual(status["table_image_count"], 1)
            self.assertEqual(status["formula_asset_count"], 1)
            self.assertEqual(status["formula_context_asset_count"], 1)
            self.assertEqual(status["formula_placeholder_link_count"], 1)
            self.assertEqual(status["generated_outputs"], status["outputs_written"])
            self.assertIn("tables/table_1.json", metadata["generated_outputs"])
            self.assertIn("tables/table_1.html", metadata["generated_outputs"])
            self.assertIn("assets/page_1.png", status["outputs_written"])
            self.assertIn("assets/table_1.png", status["outputs_written"])
            self.assertIn("assets/formula_1.png", status["outputs_written"])
            self.assertIn("assets/formula_1_context.png", status["outputs_written"])
            self.assertIn("formula_decode_limited_high_res_review_crops_written", status["warnings"])


if __name__ == "__main__":
    unittest.main()
