from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from region_quality_gate import (
    REGION_STATUSES,
    _body_identity_sha,
    _document_nodes,
    _formula_raw_content_sha256,
    _manifest_entry,
    _node_body_identity,
    _table_topology_diagnostics,
    evaluate_regions,
)


SOURCE_BYTES = b"source"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()
EVIDENCE_SHA = hashlib.sha256(b"evidence").hexdigest()
BBOX = {"l": 10, "r": 110, "t": 120, "b": 180, "coord_origin": "TOPLEFT"}


def _candidate(source_ref: str, image: str, *, index: int = 1) -> dict[str, object]:
    return {
        "source_ref": source_ref,
        "image": image,
        "page_no": 1,
        "bbox": BBOX,
        "provenance_verified": True,
        "provenance_reasons": [],
        "table_index": index,
        "algorithm_index": index,
        "code_index": index,
    }


def _structural_signals() -> dict[str, object]:
    table_ref = "#/tables/0"
    algorithm_ref = "#/texts/algorithm-0"
    code_ref = "#/texts/code-0"
    table = _candidate(table_ref, "tables/table_1.png")
    algorithm = _candidate(algorithm_ref, "algorithms/algorithm_1.png")
    code = _candidate(code_ref, "code_blocks/code_block_1.png")
    common = {
        "html_bound_source_refs": [table_ref],
        "markdown_bound_source_refs": [table_ref],
        "html_body_identity_verified_refs": [table_ref],
        "markdown_body_identity_verified_refs": [table_ref],
        "html_body_identity_mismatch_refs": [],
        "markdown_body_identity_mismatch_refs": [],
        "provenance_verified_refs": [table_ref],
        "provenance_mismatch_refs": [],
    }
    return {
        "table_source_expected_refs": [table_ref],
        "table_source_body_identity_expected_refs": [table_ref],
        "table_source_exact_coverage": True,
        "table_source_html_bound_refs": common["html_bound_source_refs"],
        "table_source_markdown_bound_refs": common["markdown_bound_source_refs"],
        "table_source_html_body_identity_verified_refs": common[
            "html_body_identity_verified_refs"
        ],
        "table_source_markdown_body_identity_verified_refs": common[
            "markdown_body_identity_verified_refs"
        ],
        "table_source_html_body_identity_mismatch_refs": [],
        "table_source_markdown_body_identity_mismatch_refs": [],
        "table_source_provenance_verified_refs": [table_ref],
        "table_source_provenance_mismatch_refs": [],
        "structured_table_source_renderings": {"candidates": [table]},
        "algorithm_source_expected_refs": [algorithm_ref],
        "algorithm_source_html_bound_refs": [algorithm_ref],
        "algorithm_source_markdown_bound_refs": [algorithm_ref],
        "algorithm_source_html_body_identity_verified_refs": [algorithm_ref],
        "algorithm_source_markdown_body_identity_verified_refs": [algorithm_ref],
        "algorithm_source_html_body_identity_mismatch_refs": [],
        "algorithm_source_markdown_body_identity_mismatch_refs": [],
        "algorithm_source_provenance_verified_refs": [algorithm_ref],
        "algorithm_source_provenance_mismatch_refs": [],
        "algorithm_source_exact_coverage": True,
        "algorithm_source_renderings": {
            "candidates": [{**algorithm, "algorithm_index": 1}]
        },
        "code_source_expected_refs": [code_ref],
        "code_source_html_bound_refs": [code_ref],
        "code_source_markdown_bound_refs": [code_ref],
        "code_source_html_body_identity_verified_refs": [code_ref],
        "code_source_markdown_body_identity_verified_refs": [code_ref],
        "code_source_html_body_identity_mismatch_refs": [],
        "code_source_markdown_body_identity_mismatch_refs": [],
        "code_source_provenance_verified_refs": [code_ref],
        "code_source_provenance_mismatch_refs": [],
        "code_source_exact_coverage": True,
        "code_source_renderings": {"candidates": [{**code, "code_index": 1}]},
        "formula_source_expected_indexes": [1],
        "formula_source_html_indexes": [1],
        "formula_source_markdown_indexes": [1],
        "formula_source_missing_indexes": [],
        "formula_source_unexpected_indexes": [],
        "formula_source_duplicate_html_anchor_indexes": [],
        "formula_source_duplicate_markdown_anchor_indexes": [],
        "formula_source_html_appendix_indexes": [],
        "formula_source_markdown_appendix_indexes": [],
        "formula_source_renderings": {
            "candidates": [
                {
                    "formula_index": 1,
                    "selected": "source",
                    "selected_image": "formulas/formula_1.png",
                    "source_image": "formulas/formula_1.png",
                    "source_provenance_verified": True,
                    "context_provenance_verified": False,
                    "page_no": 1,
                    "bbox": BBOX,
                    "source_reasons": [],
                }
            ]
        },
        "inline_math_source_expected_anchors": ["inline:1"],
        "inline_math_source_html_anchors": ["inline:1"],
        "inline_math_source_markdown_anchors": ["inline:1"],
        "inline_math_source_missing_crop_anchors": [],
        "inline_math_source_missing_html_anchors": [],
        "inline_math_source_missing_markdown_anchors": [],
        "inline_math_source_duplicate_html_anchors": [],
        "inline_math_source_duplicate_markdown_anchors": [],
        "inline_math_source_renderings": {
            "candidates": [
                {
                    "anchor": "inline:1",
                    "image": "inline_math/0001-inline-1.png",
                    "page_no": 1,
                    "bbox": BBOX,
                    "unresolved": False,
                }
            ]
        },
    }


def _fixture(root: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    (root / "source.pdf").write_bytes(SOURCE_BYTES)
    for path in (
        "pages/page_1.png",
        "tables/table_1.png",
        "algorithms/algorithm_1.png",
        "code_blocks/code_block_1.png",
        "formulas/formula_1.png",
        "inline_math/0001-inline-1.png",
        "pictures/picture_1.png",
    ):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"evidence")
    document = {
        "tables": [
            {
                "label": "table",
                "self_ref": "#/tables/0",
                "prov": [{"page_no": 1, "bbox": BBOX}],
                "data": {
                    "num_rows": 2,
                    "num_cols": 2,
                    "table_cells": [
                        {
                            "start_row_offset_idx": 0,
                            "end_row_offset_idx": 1,
                            "start_col_offset_idx": 0,
                            "end_col_offset_idx": 1,
                            "row_span": 1,
                            "col_span": 1,
                            "text": "Metric",
                            "bbox": {"l": 10, "r": 50, "t": 10, "b": 20},
                        },
                        {
                            "start_row_offset_idx": 0,
                            "end_row_offset_idx": 1,
                            "start_col_offset_idx": 1,
                            "end_col_offset_idx": 2,
                            "row_span": 1,
                            "col_span": 1,
                            "text": "Value",
                            "bbox": {"l": 50, "r": 90, "t": 10, "b": 20},
                        },
                        {
                            "start_row_offset_idx": 1,
                            "end_row_offset_idx": 2,
                            "start_col_offset_idx": 0,
                            "end_col_offset_idx": 1,
                            "row_span": 1,
                            "col_span": 1,
                            "text": "Accuracy",
                            "bbox": {"l": 10, "r": 50, "t": 22, "b": 32},
                        },
                        {
                            "start_row_offset_idx": 1,
                            "end_row_offset_idx": 2,
                            "start_col_offset_idx": 1,
                            "end_col_offset_idx": 2,
                            "row_span": 1,
                            "col_span": 1,
                            "text": "98.5",
                            "bbox": {"l": 50, "r": 90, "t": 22, "b": 32},
                        },
                    ],
                },
            }
        ],
        "pictures": [
            {
                "label": "picture",
                "self_ref": "#/pictures/0",
                "prov": [{"page_no": 1, "bbox": BBOX}],
            }
        ],
        "texts": [
            {
                "label": "code",
                "text": "Algorithm 1\n1: initialize\n2: return",
                "self_ref": "#/texts/algorithm-0",
                "prov": [{"page_no": 1, "bbox": BBOX}],
            },
            {
                "label": "code",
                "text": "def solve(x):\n    return x + 1",
                "self_ref": "#/texts/code-0",
                "prov": [{"page_no": 1, "bbox": BBOX}],
            },
            {
                "label": "formula",
                "text": "x_i = y_i + 1",
                "self_ref": "#/texts/formula-0",
                "prov": [{"page_no": 1, "bbox": BBOX}],
            },
        ],
    }
    quality = {
        "primary_surface": {
            "counts": {
                "tables": 1,
                "algorithms": 1,
                "code_blocks": 1,
                "formulas": 1,
            },
            "inline_math_source_regions": [
                {
                    "anchor": "inline:1",
                    "page_no": 1,
                    "bbox": BBOX,
                    "source_text": "x_i",
                    "binding_mode": "inline",
                    "unresolved": False,
                }
            ],
        },
        "final_source_visuals": _structural_signals(),
        "structural_quarantine_qc": {
            "candidates": [
                {
                    "kind": "visual_annotation",
                    "label": "quarantined_visual_annotation",
                    "page_no": 1,
                    "bbox": BBOX,
                    "picture_overlap": True,
                    "action": "quarantine_from_main_text_flow",
                    "final_output_residual_surfaces": [],
                    "evidence": "pages/page_1.png",
                    "text": "figure OCR",
                },
                {
                    "kind": "page_header",
                    "label": "quarantined_page_header",
                    "page_no": 1,
                    "bbox": BBOX,
                    "picture_overlap": False,
                    "action": "quarantine_from_main_text_flow",
                    "final_output_residual_surfaces": [],
                    "evidence": "pages/page_1.png",
                    "text": "Proceedings header",
                },
            ]
        },
    }
    status = {
        "ok": True,
        "quality_signals": quality,
        "warnings": [],
    }
    table_node = document["tables"][0]
    algorithm_node, code_node, formula_node = document["texts"]

    def manifest_entry(kind, index, source_ref, path, node):
        body_identity = _node_body_identity(kind, node)
        return {
            "kind": kind,
            "index": index,
            "source_ref": source_ref,
            "self_ref": node["self_ref"],
            "part_index": None,
            "page_no": 1,
            "node_bbox": BBOX,
            "asset_path": path,
            "asset_sha256": EVIDENCE_SHA,
            "visual_pdf_sha256": SOURCE_SHA,
            "structural_body_identity_sha256": _body_identity_sha(
                kind, body_identity
            ),
            "source_node_bindings": [
                {
                    "source_ref": source_ref,
                    "self_ref": node["self_ref"],
                    "part_index": None,
                    "page_no": 1,
                    "bbox": BBOX,
                    "body_identity_sha256": _body_identity_sha(
                        "algorithm-source-node" if kind == "algorithm" else kind,
                        _node_body_identity(kind, node),
                    ),
                }
            ],
        }

    metadata = {
        "visual_evidence_input_sha256": SOURCE_SHA,
        "generated_outputs": [],
        "structural_visual_provenance_manifest": {
            "visual_pdf_sha256": SOURCE_SHA,
            "tables": [
                manifest_entry(
                    "table", 1, "#/tables/0", "tables/table_1.png", table_node
                )
            ],
            "algorithms": [
                manifest_entry(
                    "algorithm",
                    1,
                    "#/texts/algorithm-0",
                    "algorithms/algorithm_1.png",
                    algorithm_node,
                )
            ],
            "code": [
                manifest_entry(
                    "code",
                    1,
                    "#/texts/code-0",
                    "code_blocks/code_block_1.png",
                    code_node,
                )
            ],
        },
        "formula_crop_diagnostics": [
            {
                "index": 1,
                "page_no": 1,
                "bbox": BBOX,
                "source_pdf_sha256": SOURCE_SHA,
                "formula_content_identity_sha256": hashlib.sha256(
                    b"formula-content"
                ).hexdigest(),
                "formula_raw_content_sha256": _formula_raw_content_sha256(
                    formula_node["text"]
                ),
                "source": {
                    "path": "formulas/formula_1.png",
                    "page_no": 1,
                    "bbox": BBOX,
                    "asset_sha256": EVIDENCE_SHA,
                    "source_pdf_sha256": SOURCE_SHA,
                    "formula_content_identity_sha256": hashlib.sha256(
                        b"formula-content"
                    ).hexdigest(),
                    "formula_raw_content_sha256": _formula_raw_content_sha256(
                        formula_node["text"]
                    ),
                },
            }
        ],
    }
    return document, metadata, status


class RegionQualityGateTests(unittest.TestCase):
    def test_all_region_kinds_are_deterministic_and_sidecars_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            first = evaluate_regions(root, document, metadata, status)
            second = evaluate_regions(
                root,
                document,
                metadata,
                status,
                write_sidecars=False,
            )
            self.assertTrue(first["ok"])
            self.assertEqual(first["records"], second["records"])
            self.assertTrue(
                {record["status"] for record in first["records"]}
                <= set(REGION_STATUSES)
            )
            self.assertLessEqual(first["record_count"], 1000)
            self.assertTrue((root / "regions.json").is_file())
            self.assertTrue((root / "quality_signals.json").is_file())
            self.assertIn("regions.json", metadata["generated_outputs"])
            self.assertIn("quality_signals.json", metadata["generated_outputs"])
            self.assertIn("region_quality_gate", status["quality_signals"])
            self.assertEqual(status["ok"], True)
            hard_kinds = {"table", "algorithm", "code", "formula", "inline_math"}
            self.assertTrue(
                all(
                    record["critical"]
                    for record in first["records"]
                    if record["kind"] in hard_kinds
                )
            )

    def test_picture_overlap_and_header_footer_residuals_are_critical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["structural_quarantine_qc"]["candidates"][0][
                "final_output_residual_surfaces"
            ] = ["document.html"]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            failures = [
                record
                for record in result["records"]
                if record["kind"] == "picture_ocr"
            ]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["status"], "unresolved")
            self.assertTrue(failures[0]["critical"])
            self.assertFalse(result["ok"])
            self.assertFalse(status["ok"])
            self.assertEqual(status["success_class"], "degraded_failure")

    def test_structural_and_inline_missing_bindings_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["code_source_markdown_bound_refs"] = []
            visuals["inline_math_source_missing_crop_anchors"] = ["inline:1"]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            failed_kinds = {
                record["kind"]
                for record in result["records"]
                if record["status"] == "unresolved" and record["critical"]
            }
            self.assertIn("code", failed_kinds)
            self.assertIn("inline_math", failed_kinds)
            self.assertFalse(result["ok"])

    def test_approved_formula_appendix_drop_remains_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            status["quality_signals"]["primary_surface"]["counts"]["formulas"] = 0
            visuals["formula_source_dropped_artifacts"] = [
                {"raw_formula_index": 1, "reason": "compact_formula_fragment"}
            ]
            visuals["formula_source_html_indexes"] = []
            visuals["formula_source_markdown_indexes"] = []
            visuals["formula_source_html_appendix_indexes"] = [1]
            visuals["formula_source_markdown_appendix_indexes"] = [1]
            candidate = visuals["formula_source_renderings"]["candidates"][0]
            candidate["selected_image"] = None
            candidate["diagnostic_image"] = "formulas/formula_1.png"
            candidate["source_reasons"] = ["compact_formula_fragment"]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            formula_records = [
                record for record in result["records"] if record["kind"] == "formula"
            ]
            self.assertEqual(len(formula_records), 1)
            self.assertEqual(formula_records[0]["status"], "verified_semantic")
            self.assertTrue(formula_records[0]["signals"]["approved_dropped_formula"])

    def test_empty_table_visual_fallback_is_not_treated_as_missing_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            table_ref = "#/tables/0"
            visuals["structured_table_source_renderings"]["candidates"] = []
            visuals["table_source_body_identity_expected_refs"] = []
            visuals["table_source_html_body_identity_verified_refs"] = []
            visuals["table_source_markdown_body_identity_verified_refs"] = []
            visuals["table_empty_fallback_expected_refs"] = [table_ref]
            visuals["empty_table_visual_fallbacks"] = {
                "candidates": [
                    {
                        "source_ref": table_ref,
                        "image": "tables/table_1.png",
                        "page_no": 1,
                        "bbox": BBOX,
                        "provenance_verified": True,
                        "provenance_reasons": [],
                    }
                ]
            }
            table_records = [
                record
                for record in evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )["records"]
                if record["kind"] == "table"
            ]
            self.assertEqual(len(table_records), 1)
            self.assertEqual(table_records[0]["status"], "verified_semantic")
            self.assertTrue(table_records[0]["signals"]["table_topology"]["empty_visual_fallback"])

    def test_repeated_tall_numeric_cells_flag_collapsed_visual_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            cells = document["tables"][0]["data"]["table_cells"]
            cells.extend(
                [
                    {
                        "start_row_offset_idx": 1,
                        "end_row_offset_idx": 2,
                        "start_col_offset_idx": column,
                        "end_col_offset_idx": column + 1,
                        "row_span": 1,
                        "col_span": 1,
                        "text": values,
                        "bbox": {
                            "l": 90 + column * 40,
                            "r": 125 + column * 40,
                            "t": 22,
                            "b": 52,
                        },
                    }
                    for column, values in ((2, "98.5 90.5 95.2"), (3, "98.2 87.7 93.2"))
                ]
            )

            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")

            self.assertEqual("unresolved", table["status"])
            self.assertIn("table_row_likely_collapsed", table["reasons"])
            self.assertFalse(result["ok"])

    def test_single_row_cell_spanning_next_row_center_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            cells = document["tables"][0]["data"]["table_cells"]
            cells.append(
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 2,
                    "end_col_offset_idx": 3,
                    "row_span": 1,
                    "col_span": 1,
                    "text": "2144 2764",
                    "bbox": {"l": 90, "r": 125, "t": 10, "b": 32},
                }
            )

            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")

            self.assertEqual("unresolved", table["status"])
            self.assertIn(
                "table_cell_crosses_semantic_row_boundary",
                table["reasons"],
            )
            self.assertFalse(result["ok"])

    def test_record_limit_fails_even_for_noncritical_picture_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = {
                "pictures": [
                    {
                        "label": "picture",
                        "self_ref": f"#/pictures/{index}",
                        "prov": [{"page_no": 1, "bbox": BBOX}],
                    }
                    for index in range(1001)
                ]
            }
            result = evaluate_regions(
                root,
                document,
                {"generated_outputs": []},
                {"ok": True, "quality_signals": {}, "warnings": []},
                max_records=1000,
                write_sidecars=False,
            )
            self.assertTrue(result["truncated"])
            self.assertEqual(result["record_count"], 1000)
            self.assertFalse(result["ok"])
            self.assertIn("region_record_limit_exceeded", result["failure_reasons"])

    def test_missing_bare_picture_asset_is_advisory_unresolved_not_visual_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.pdf").write_bytes(SOURCE_BYTES)
            document = {
                "pictures": [
                    {
                        "label": "picture",
                        "self_ref": "#/pictures/0",
                        "prov": [{"page_no": 1, "bbox": BBOX}],
                    }
                ]
            }
            result = evaluate_regions(
                root,
                document,
                {"generated_outputs": []},
                {"ok": True, "quality_signals": {}, "warnings": []},
                write_sidecars=False,
            )
            picture = next(record for record in result["records"] if record["kind"] == "picture")
            self.assertEqual("unresolved", picture["status"])
            self.assertFalse(picture["critical"])
            self.assertFalse(picture["signals"]["visual_evidence_present"])
            self.assertTrue(result["ok"])

    def test_dry_run_does_not_advertise_unwritten_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)

            evaluate_regions(root, document, metadata, status, write_sidecars=False)

            self.assertNotIn("regions.json", metadata["generated_outputs"])
            self.assertNotIn("quality_signals.json", metadata["generated_outputs"])
            self.assertFalse((root / "regions.json").exists())
            self.assertFalse((root / "quality_signals.json").exists())

    def test_sidecar_symlink_fails_closed_without_following_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            outside = root.parent / f"{root.name}-outside.json"
            outside.write_text("untouched", encoding="utf-8")
            (root / "quality_signals.json").symlink_to(outside)
            try:
                result = evaluate_regions(root, document, metadata, status)
                persisted_regions = json.loads(
                    (root / "regions.json").read_text(encoding="utf-8")
                )
                self.assertFalse(result["ok"])
                self.assertFalse(persisted_regions["ok"])
                self.assertEqual("untouched", outside.read_text(encoding="utf-8"))
                self.assertIn("regions.json", metadata["generated_outputs"])
                self.assertNotIn("quality_signals.json", metadata["generated_outputs"])
            finally:
                outside.unlink(missing_ok=True)

    def test_sidecar_payload_is_json_and_has_no_unbounded_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            result = evaluate_regions(root, document, metadata, status)
            payload = json.loads((root / "regions.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["record_count"], result["record_count"])
            self.assertTrue(all(record["text_preview"] is None or len(record["text_preview"]) <= 180 for record in payload["records"]))

    def test_strict_numeric_page_rejects_float_and_bool(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = {
                "pictures": [
                    {
                        "label": "picture",
                        "self_ref": "#/pictures/0",
                        "prov": [{"page_no": 1.0, "bbox": BBOX}],
                    },
                    {
                        "label": "picture",
                        "self_ref": "#/pictures/1",
                        "prov": [{"page_no": True, "bbox": BBOX}],
                    },
                ]
            }
            result = evaluate_regions(
                root,
                document,
                {"generated_outputs": []},
                {"ok": True, "quality_signals": {}, "warnings": []},
                write_sidecars=False,
            )
            self.assertTrue(all(record["page_no"] is None for record in result["records"]))
            self.assertTrue(all("invalid_page_no" in record["reasons"] for record in result["records"]))

    def test_duplicate_refs_and_count_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            table_ref = "#/tables/0"
            visuals["table_source_expected_refs"] = [table_ref, table_ref]
            status["quality_signals"]["primary_surface"]["counts"]["tables"] = 2
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("expected_region_refs_duplicate", table["reasons"])
            self.assertIn("expected_region_count_mismatch", table["reasons"])
            self.assertFalse(result["ok"])

    def test_partial_expected_count_without_duplicate_refs_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["primary_surface"]["counts"]["tables"] = 2

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(record for record in result["records"] if record["kind"] == "table")

            self.assertIn("expected_region_count_mismatch", table["reasons"])
            self.assertFalse(result["ok"])

    def test_extra_structural_candidate_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["structured_table_source_renderings"]["candidates"].append(
                _candidate("#/tables/extra", "tables/table_1.png", index=2)
            )

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(record for record in result["records"] if record["kind"] == "table")

            self.assertIn("candidate_source_ref_set_mismatch", table["reasons"])
            self.assertFalse(result["ok"])

    def test_deleted_final_structural_node_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            document["texts"] = [
                node
                for node in document["texts"]
                if node["self_ref"] != "#/texts/algorithm-0"
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            algorithm = next(
                record for record in result["records"] if record["kind"] == "algorithm"
            )

            self.assertIn("final_document_node_missing", algorithm["reasons"])
            self.assertFalse(result["ok"])

    def test_missing_structural_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            metadata.pop("structural_visual_provenance_manifest")

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            structural = [
                record
                for record in result["records"]
                if record["kind"] in {"table", "algorithm", "code"}
            ]

            self.assertTrue(
                all(
                    "structural_provenance_manifest_missing" in record["reasons"]
                    for record in structural
                )
            )
            self.assertFalse(result["ok"])

    def test_wrong_kind_asset_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            (root / "metadata.json").write_bytes(b"evidence")
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["formula_source_renderings"]["candidates"][0][
                "selected_image"
            ] = "metadata.json"

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            formula = next(
                record for record in result["records"] if record["kind"] == "formula"
            )

            self.assertIn("source_asset_kind_mismatch", formula["reasons"])
            self.assertFalse(result["ok"])

    def test_symlinked_evidence_asset_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            outside = root.parent / f"{root.name}-outside-evidence.png"
            outside.write_bytes(b"outside")
            link = root / "formulas" / "formula_link.png"
            link.symlink_to(outside)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["formula_source_renderings"]["candidates"][0][
                "selected_image"
            ] = "formulas/formula_link.png"
            metadata["formula_crop_diagnostics"][0]["source"][
                "path"
            ] = "formulas/formula_link.png"
            try:
                result = evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )
                formula = next(
                    record
                    for record in result["records"]
                    if record["kind"] == "formula"
                )
                self.assertIn("unsafe_or_missing_source_asset", formula["reasons"])
                self.assertEqual(outside.read_bytes(), b"outside")
                self.assertFalse(result["ok"])
            finally:
                outside.unlink(missing_ok=True)

    def test_visual_annotation_without_overlap_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            candidate = status["quality_signals"]["structural_quarantine_qc"][
                "candidates"
            ][0]
            candidate.pop("picture_overlap")

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            picture_ocr = next(
                record for record in result["records"] if record["kind"] == "picture_ocr"
            )

            self.assertIn("picture_overlap_unproven", picture_ocr["reasons"])
            self.assertFalse(result["ok"])

    def test_oversized_residual_surface_list_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["structural_quarantine_qc"]["candidates"][0][
                "final_output_residual_surfaces"
            ] = [f"surface-{index}" for index in range(33)]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            picture_ocr = next(
                record for record in result["records"] if record["kind"] == "picture_ocr"
            )

            self.assertIn("residual_surface_schema_invalid", picture_ocr["reasons"])
            self.assertLessEqual(
                len(picture_ocr["signals"]["residual_surfaces"]), 32
            )
            self.assertFalse(result["ok"])

    def test_manifest_algorithm_alias_is_resolved(self):
        entry = {"source_ref": "#/texts/algorithm-0", "kind": "algorithm"}
        metadata = {"structural_visual_provenance_manifest": {"algorithms": [entry]}}
        self.assertEqual(_manifest_entry(metadata, "algorithm", "#/texts/algorithm-0"), entry)

    def test_source_identity_conflict_and_actual_source_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.pdf").write_bytes(b"source")
            result = evaluate_regions(
                root,
                {"pictures": []},
                {"input_sha256": "b" * 64},
                {"ok": True, "quality_signals": {}, "warnings": []},
                write_sidecars=False,
            )
            self.assertFalse(result["ok"])
            self.assertIn("source_pdf_actual_hash_mismatch", result["failure_reasons"])

    def test_missing_source_pdf_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            (root / "source.pdf").unlink()

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            self.assertIn("source_pdf_missing_or_unsafe", result["failure_reasons"])
            self.assertFalse(result["ok"])

    def test_malformed_state_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "document.json").write_text("{}", encoding="utf-8")
            metadata_bytes = b"{not-json"
            status_bytes = b"[not-an-object]"
            (root / "metadata.json").write_bytes(metadata_bytes)
            (root / "status.json").write_bytes(status_bytes)
            result = evaluate_regions(root, write_sidecars=True)
            self.assertFalse(result["ok"])
            self.assertEqual((root / "metadata.json").read_bytes(), metadata_bytes)
            self.assertEqual((root / "status.json").read_bytes(), status_bytes)

    def test_valid_non_object_state_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "document.json").write_text("{}", encoding="utf-8")
            (root / "source.pdf").write_bytes(SOURCE_BYTES)
            metadata_bytes = b"[]"
            status_bytes = b"null"
            (root / "metadata.json").write_bytes(metadata_bytes)
            (root / "status.json").write_bytes(status_bytes)

            result = evaluate_regions(root, write_sidecars=True)

            self.assertFalse(result["ok"])
            self.assertIn("metadata_json_invalid", result["failure_reasons"])
            self.assertIn("status_json_invalid", result["failure_reasons"])
            self.assertEqual((root / "metadata.json").read_bytes(), metadata_bytes)
            self.assertEqual((root / "status.json").read_bytes(), status_bytes)

    def test_empty_document_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.pdf").write_bytes(SOURCE_BYTES)

            result = evaluate_regions(
                root,
                {},
                {"input_sha256": SOURCE_SHA},
                {"ok": True, "quality_signals": {}, "warnings": []},
                write_sidecars=False,
            )

            self.assertIn("document_json_empty", result["failure_reasons"])
            self.assertFalse(result["ok"])

    def test_formula_and_inline_extra_candidates_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            extra_formula = dict(
                visuals["formula_source_renderings"]["candidates"][0]
            )
            extra_formula["formula_index"] = 2
            visuals["formula_source_renderings"]["candidates"].append(extra_formula)
            visuals["inline_math_source_renderings"]["candidates"].append(
                {
                    "anchor": "inline:extra",
                    "image": "inline_math/0001-inline-1.png",
                    "page_no": 1,
                    "bbox": BBOX,
                }
            )

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            formula = next(
                record for record in result["records"] if record["kind"] == "formula"
            )
            inline = next(
                record for record in result["records"] if record["kind"] == "inline_math"
            )

            self.assertIn("formula_candidate_index_set_mismatch", formula["reasons"])
            self.assertIn("inline_math_candidate_anchor_set_mismatch", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_table_bounds_span_and_overlap_are_critical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            cells = document["tables"][0]["data"]["table_cells"]
            cells[0]["end_col_offset_idx"] = 3
            cells[0]["col_span"] = 3
            cells[1]["row_span"] = 2
            cells[3]["start_row_offset_idx"] = 99
            cells[3]["end_row_offset_idx"] = 100
            cells.append(dict(cells[2]))
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("table_cell_bounds_invalid", table["reasons"])
            self.assertIn("table_cell_span_invalid", table["reasons"])
            self.assertIn("table_cell_overlap", table["reasons"])
            self.assertFalse(result["ok"])

    def test_legal_uncertainty_and_range_cells_do_not_trigger_collapsed_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            data = document["tables"][0]["data"]
            data["num_cols"] = 4
            data["table_cells"].extend(
                [
                    {
                        "start_row_offset_idx": 1,
                        "end_row_offset_idx": 2,
                        "start_col_offset_idx": col,
                        "end_col_offset_idx": col + 1,
                        "row_span": 1,
                        "col_span": 1,
                        "text": text,
                        "bbox": {"l": 90 + col * 40, "r": 125 + col * 40, "t": 22, "b": 52},
                    }
                    for col, text in ((2, "98.5 ± 0.2"), (3, "[98.2, 99.1]"))
                ]
            )
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertNotIn("table_row_likely_collapsed", table["reasons"])

    def test_chunk_part_source_refs_are_deterministic_and_bounded(self):
        document = {
            "chunks": [
                {
                    "page_range": [2, 2],
                    "document": {"texts": [{"label": "text", "self_ref": "#/texts/0"}]},
                },
                {
                    "page_range": [1, 1],
                    "document": {"texts": [{"label": "text", "self_ref": "#/texts/0"}]},
                },
            ]
        }
        refs = [item["source_ref"] for item in _document_nodes(document, {"text"})]
        self.assertEqual(sorted(refs), ["chunk:0:#/texts/0", "chunk:1:#/texts/0"])

    def test_chunk_table_topology_uses_canonical_chunk_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            document, _metadata, _status = _fixture(Path(temporary))
            table = document["tables"][0]
            table["_local_ai_lab_chunk_part_index"] = 2
            table["data"]["table_cells"].extend(
                [
                    {
                        "start_row_offset_idx": 1,
                        "end_row_offset_idx": 2,
                        "start_col_offset_idx": column,
                        "end_col_offset_idx": column + 1,
                        "row_span": 1,
                        "col_span": 1,
                        "text": values,
                        "bbox": {
                            "l": 90 + column * 40,
                            "r": 125 + column * 40,
                            "t": 22,
                            "b": 52,
                        },
                    }
                    for column, values in (
                        (2, "98.5 90.5 95.2"),
                        (3, "98.2 87.7 93.2"),
                    )
                ]
            )
            table["data"]["num_cols"] = 4
            chunked = {
                "chunks": [
                    {
                        "page_range": [1, 1],
                        "document": {"tables": [table]},
                    }
                ]
            }

            diagnostics = _table_topology_diagnostics(chunked)

            self.assertIn("chunk:2:#/tables/0", diagnostics)
            self.assertIn(
                "table_row_likely_collapsed",
                diagnostics["chunk:2:#/tables/0"]["reasons"],
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
