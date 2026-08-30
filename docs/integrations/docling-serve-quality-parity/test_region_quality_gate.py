from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

import region_quality_gate as gate
from region_quality_gate import (
    REGION_STATUSES,
    _body_identity_sha,
    _candidate_map,
    _code_body_identity,
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
                    "source_text": "x_i",
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
            {
                "label": "text",
                "text": "The inline expression x_i appears in this paragraph.",
                "self_ref": "#/texts/inline-0",
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
            "inline_math_source_region_count": 1,
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
    algorithm_node, code_node, formula_node = document["texts"][:3]
    algorithm_record = {
        "source_ref": algorithm_node["self_ref"],
        "text": algorithm_node["text"],
        "layout": None,
        "page_no": 1,
        "bbox": BBOX,
        "source_node_bindings": [
            {
                "source_ref": algorithm_node["self_ref"],
                "self_ref": algorithm_node["self_ref"],
                "part_index": None,
                "page_no": 1,
                "bbox": BBOX,
                "body_identity_kind": "node_text",
                "body_identity_sha256": _body_identity_sha(
                    "algorithm-source-node",
                    _node_body_identity("algorithm", algorithm_node),
                ),
            }
        ],
    }
    (root / "algorithm_blocks.json").write_text(
        json.dumps([algorithm_record]), encoding="utf-8"
    )

    def manifest_entry(kind, index, source_ref, path, node):
        body_identity = (
            gate._algorithm_expected_body_identity(algorithm_record)
            if kind == "algorithm"
            else _node_body_identity(kind, node)
        )
        return {
            "kind": kind,
            "index": index,
            "source_ref": source_ref,
            "self_ref": "" if kind == "algorithm" else node["self_ref"],
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
                    **(
                        {"body_identity_kind": "node_text"}
                        if kind == "algorithm"
                        else {}
                    ),
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
    def test_default_record_id_digest_remains_backward_compatible(self):
        self.assertEqual(
            "formula:5c3c7524a65d1daf",
            gate._record_id("formula", "#/texts/1", 1, 1),
        )
        self.assertNotEqual(
            gate._record_id("picture_ocr", "picture_ocr:1", 1, 1),
            gate._record_id(
                "picture_ocr",
                "picture_ocr:1",
                1,
                1,
                namespace="quarantine-derived",
            ),
        )

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

    def test_quarantine_duplicate_residual_evidence_merges_order_independently(self):
        outputs = []
        for reverse in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document, metadata, status = _fixture(root)
                base = status["quality_signals"]["structural_quarantine_qc"]["candidates"][0]
                clean = dict(base)
                clean["source_ref"] = "picture:dup"
                clean["final_output_residual_surfaces"] = []
                dirty = dict(clean)
                dirty["final_output_residual_surfaces"] = ["document.html"]
                candidates = [dirty, clean]
                if reverse:
                    candidates.reverse()
                status["quality_signals"]["structural_quarantine_qc"]["candidates"] = candidates
                result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
                record = next(
                    item
                    for item in result["records"]
                    if item["kind"] == "picture_ocr" and item["source_ref"] == "picture:dup"
                )
                outputs.append((record, result["failure_reasons"]))
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("main_flow_residual", outputs[0][0]["reasons"])
        self.assertFalse(outputs[0][0]["status"] == "verified_semantic")

    def test_quarantine_duplicate_conflicting_geometry_fails_conservatively(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = status["quality_signals"]["structural_quarantine_qc"]["candidates"][0]
            first = dict(base, source_ref="picture:conflict")
            second = dict(first, bbox={"l": 20, "r": 120, "t": 120, "b": 180, "coord_origin": "TOPLEFT"})
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [first, second]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            record = next(
                item
                for item in result["records"]
                if item["kind"] == "picture_ocr" and item["source_ref"] == "picture:conflict"
            )
            self.assertIn("quarantine_duplicate_evidence_conflict", record["reasons"])
            self.assertFalse(result["ok"])

    def test_quarantine_without_source_ref_keeps_distinct_geometry_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            base.pop("source_ref", None)
            first = dict(base)
            second = dict(
                base,
                bbox={
                    "l": 220,
                    "r": 320,
                    "t": 120,
                    "b": 180,
                    "coord_origin": "TOPLEFT",
                },
            )
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(2, len(picture_records))
            self.assertTrue(
                all(
                    "quarantine_duplicate_evidence_conflict" not in item["reasons"]
                    for item in picture_records
                )
            )

    def test_quarantine_without_source_ref_merges_same_geometry_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            base.pop("source_ref", None)
            first = dict(base)
            second = dict(base, final_output_residual_surfaces=["document.md"])
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(1, len(picture_records))
            self.assertIn("document.md", picture_records[0]["signals"]["residual_surfaces"])
            self.assertNotIn(
                "quarantine_duplicate_evidence_conflict",
                picture_records[0]["reasons"],
            )

    def test_quarantine_invalid_explicit_source_ref_does_not_collide_with_legal_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            first = dict(base, source_ref="picture:foo bar")
            second = dict(base, source_ref="picture:foo_bar")
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(2, len(picture_records))
            invalid = [
                item
                for item in picture_records
                if "quarantine_source_ref_invalid" in item["reasons"]
            ]
            valid = [
                item for item in picture_records if item["source_ref"] == "picture:foo_bar"
            ]
            self.assertEqual(1, len(invalid))
            self.assertEqual(1, len(valid))
            self.assertFalse(result["ok"])

    def test_quarantine_overlong_explicit_source_refs_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            long_prefix = "picture:" + ("a" * 172)
            first = dict(base, source_ref=long_prefix + "X")
            second = dict(base, source_ref=long_prefix + "Y")
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(2, len(picture_records))
            self.assertTrue(
                all(
                    "quarantine_source_ref_invalid" in item["reasons"]
                    and item["status"] == "unresolved"
                    for item in picture_records
                )
            )
            self.assertFalse(result["ok"])

    def test_quarantine_normalized_explicit_source_refs_do_not_collide_with_legal_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            first = dict(base, source_ref="picture:trim ")
            second = dict(base, source_ref="picture:trim")
            third = dict(base, source_ref="picture:null\x00")
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
                third,
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(3, len(picture_records))
            invalid = [
                item
                for item in picture_records
                if "quarantine_source_ref_invalid" in item["reasons"]
            ]
            valid = [
                item for item in picture_records if item["source_ref"] == "picture:trim"
            ]
            self.assertEqual(2, len(invalid))
            self.assertEqual(1, len(valid))
            self.assertFalse(result["ok"])

    def test_quarantine_invalid_and_legal_fallback_visible_refs_keep_unique_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            first = dict(base, source_ref="picture:foo bar")
            second = dict(base, source_ref="picture_ocr:1")
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]

            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(2, len(picture_records))
            self.assertEqual(2, len({item["id"] for item in picture_records}))

    def test_quarantine_without_source_ref_missing_bbox_stays_separate_and_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = dict(status["quality_signals"]["structural_quarantine_qc"]["candidates"][0])
            base.pop("source_ref", None)
            base.pop("bbox", None)
            first = dict(base)
            second = dict(base)
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            picture_records = [
                item for item in result["records"] if item["kind"] == "picture_ocr"
            ]
            self.assertEqual(2, len(picture_records))
            self.assertTrue(
                all(
                    "quarantine_bbox_missing_or_invalid" in item["reasons"]
                    and item["status"] == "unresolved"
                    for item in picture_records
                )
            )
            self.assertFalse(result["ok"])

    def test_non_quarantine_candidate_is_ignored_without_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                {
                    "kind": "text",
                    "label": "text",
                    "page_no": 1,
                    "text": "ordinary body paragraph",
                }
            ]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            quarantine_records = [
                item
                for item in result["records"]
                if item["kind"] in {"picture_ocr", "header_footer"}
            ]
            self.assertEqual([], quarantine_records)
            self.assertTrue(result["ok"])

    def test_quarantine_non_object_candidate_is_critical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [None]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            marker = next(
                item
                for item in result["records"]
                if item["source_ref"] == "picture_ocr:quarantine-schema-item"
            )
            self.assertIn("quarantine_candidate_invalid", marker["reasons"])
            self.assertFalse(result["ok"])

    def test_quarantine_distinct_source_refs_are_not_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            base = status["quality_signals"]["structural_quarantine_qc"]["candidates"][0]
            first = dict(base, source_ref="picture:first")
            second = dict(base, source_ref="picture:second")
            status["quality_signals"]["structural_quarantine_qc"]["candidates"] = [
                first,
                second,
            ]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            refs = {
                item["source_ref"]
                for item in result["records"]
                if item["kind"] == "picture_ocr"
            }
            self.assertIn("picture:first", refs)
            self.assertIn("picture:second", refs)

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
            table_node = document["tables"][0]
            table_node["data"].update(
                {"num_rows": 0, "num_cols": 0, "table_cells": []}
            )
            identity = _node_body_identity("table", table_node)
            entry = metadata["structural_visual_provenance_manifest"]["tables"][0]
            entry["structural_body_identity_sha256"] = _body_identity_sha(
                "table", identity
            )
            entry["source_node_bindings"][0]["body_identity_sha256"] = (
                _body_identity_sha("table", identity)
            )
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
                {"generated_outputs": [], "input_sha256": SOURCE_SHA},
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

    def test_root_swap_during_atomic_publish_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            original_replace = gate.os.replace
            moved = root.parent / f"{root.name}-moved"
            swapped = False

            def swap_then_replace(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    root.rename(moved)
                    root.mkdir()
                return original_replace(*args, **kwargs)

            gate.os.replace = swap_then_replace
            try:
                result = evaluate_regions(root, document, metadata, status)
            finally:
                gate.os.replace = original_replace
                replacement_has_regions = (root / "regions.json").exists()
                if moved.exists():
                    root.rmdir()
                    moved.rename(root)
            self.assertFalse(result["ok"])
            self.assertTrue(swapped)
            self.assertTrue(
                any("output_root_changed" in reason for reason in result["failure_reasons"])
            )
            self.assertFalse(replacement_has_regions)

    def test_root_context_set_failure_closes_the_pinned_descriptor(self):
        class FailingContext:
            def __init__(self, error_type):
                self.error_type = error_type
                self.fd = None

            def set(self, value):
                self.fd = value[1]
                raise self.error_type("injected context failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root)
            original_context = gate._ROOT_CONTEXT
            for error_type in (OSError, RuntimeError):
                failing = FailingContext(error_type)
                gate._ROOT_CONTEXT = failing
                try:
                    with self.assertRaises(error_type):
                        evaluate_regions(root, write_sidecars=False)
                finally:
                    gate._ROOT_CONTEXT = original_context
                self.assertIsNotNone(failing.fd)
                with self.assertRaises(OSError):
                    gate.os.fstat(failing.fd)

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

    def test_manifest_non_object_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            manifest = metadata["structural_visual_provenance_manifest"]
            manifest["tables"] = [None, *manifest["tables"]]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("structural_manifest_entry_invalid", table["reasons"])
            self.assertFalse(result["ok"])

    def test_formula_diagnostic_non_object_and_oversize_are_bounded_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            metadata["formula_crop_diagnostics"] = [
                *metadata["formula_crop_diagnostics"],
                None,
                *([None] * 1001),
            ]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            formula = next(record for record in result["records"] if record["kind"] == "formula")
            self.assertIn("formula_crop_diagnostics_too_many", formula["reasons"])
            self.assertIn("formula_crop_diagnostic_invalid", formula["reasons"])
            self.assertFalse(result["ok"])

    def test_algorithm_production_records_and_source_image_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            algorithm = dict(visuals["algorithm_source_renderings"]["candidates"][0])
            algorithm.pop("image", None)
            algorithm["source_image"] = "algorithms/algorithm_1.png"
            visuals["algorithm_source_renderings"] = {"records": [algorithm]}
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            algorithm_record = next(record for record in result["records"] if record["kind"] == "algorithm")
            self.assertEqual("verified_semantic", algorithm_record["status"])
            self.assertTrue(result["ok"])

    def test_algorithm_empty_candidates_do_not_hide_production_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            algorithm = dict(visuals["algorithm_source_renderings"]["candidates"][0])
            algorithm.pop("image", None)
            algorithm["source_image"] = "algorithms/algorithm_1.png"
            visuals["algorithm_source_renderings"] = {
                "candidates": [],
                "records": [algorithm],
            }
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            record = next(item for item in result["records"] if item["kind"] == "algorithm")
            self.assertEqual("verified_semantic", record["status"])
            self.assertTrue(result["ok"])

    def test_algorithm_alias_evidence_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            candidate = dict(visuals["algorithm_source_renderings"]["candidates"][0])
            conflicting = dict(candidate)
            conflicting["source_image"] = "algorithms/missing.png"
            conflicting["bbox"] = {
                "l": 20,
                "r": 120,
                "t": 120,
                "b": 180,
                "coord_origin": "TOPLEFT",
            }
            visuals["algorithm_source_renderings"]["records"] = [conflicting]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            record = next(item for item in result["records"] if item["kind"] == "algorithm")
            self.assertIn("candidate_alias_evidence_conflict", record["reasons"])
            self.assertFalse(result["ok"])

    def test_candidate_asset_hash_uses_the_pinned_output_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            candidate = status["quality_signals"]["final_source_visuals"][
                "structured_table_source_renderings"
            ]["candidates"][0]
            candidate["asset_sha256"] = EVIDENCE_SHA
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertEqual("verified_semantic", table["status"])
            self.assertTrue(result["ok"])

    def test_algorithm_like_table_candidate_is_excluded_from_table_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            extra = _candidate("#/tables/algorithm-like", "tables/table_1.png", index=2)
            extra["original_label"] = "algorithm_like_table"
            visuals["structured_table_source_renderings"]["candidates"].append(extra)
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table_record = next(record for record in result["records"] if record["kind"] == "table")
            self.assertEqual("verified_semantic", table_record["status"])
            self.assertTrue(result["ok"])

    def test_ordinary_table_caption_with_algorithm_word_is_not_reclassified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            candidate = status["quality_signals"]["final_source_visuals"][
                "structured_table_source_renderings"
            ]["candidates"][0]
            candidate["caption"] = "Algorithm comparison and pseudocode notation"
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertEqual("verified_semantic", table["status"])
            self.assertTrue(result["ok"])

    def test_algorithm_table_source_ref_is_excluded_without_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            algorithm_ref = "#/tables/2"
            visuals["structured_table_source_renderings"]["candidates"].append(
                _candidate(algorithm_ref, "tables/table_1.png", index=2)
            )
            visuals["algorithm_source_expected_refs"] = [algorithm_ref]
            visuals["algorithm_source_renderings"] = {
                "records": [
                    {
                        "source_ref": algorithm_ref,
                        "source_image": "algorithms/algorithm_1.png",
                        "page_no": 1,
                        "bbox": BBOX,
                    }
                ]
            }
            table_map = _candidate_map(visuals, "table")
            algorithm_map = _candidate_map(visuals, "algorithm")
            self.assertNotIn(algorithm_ref, table_map)
            self.assertIn(algorithm_ref, algorithm_map)

    def test_numbered_code_identity_matches_adapter_join_contract(self):
        self.assertEqual(
            _code_body_identity("1 x\n2 y"),
            _code_body_identity("1 x 2 y"),
        )
        self.assertEqual("x y", _code_body_identity("1 x\n2 y"))

    def test_numbered_code_manifest_binding_is_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            code_node = document["texts"][1]
            code_node["text"] = "1 x = 1\n2 y = 2"
            identity = _node_body_identity("code", code_node)
            entry = metadata["structural_visual_provenance_manifest"]["code"][0]
            entry["structural_body_identity_sha256"] = _body_identity_sha("code", identity)
            entry["source_node_bindings"][0]["body_identity_sha256"] = _body_identity_sha(
                "code", identity
            )
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            code = next(record for record in result["records"] if record["kind"] == "code")
            self.assertEqual("verified_semantic", code["status"])
            self.assertTrue(result["ok"])

    def test_cross_page_algorithm_candidate_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            candidate = status["quality_signals"]["final_source_visuals"][
                "algorithm_source_renderings"
            ]["candidates"][0]
            candidate["page_span"] = {"start_page": 1, "end_page": 2, "pages": [1, 2]}
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            algorithm = next(record for record in result["records"] if record["kind"] == "algorithm")
            self.assertIn("algorithm_cross_page_unsupported", algorithm["reasons"])
            self.assertFalse(result["ok"])

    def test_single_page_algorithm_span_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            candidate = status["quality_signals"]["final_source_visuals"][
                "algorithm_source_renderings"
            ]["candidates"][0]
            candidate["page_span"] = {"start_page": 1, "end_page": 1, "pages": [1]}
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            algorithm = next(record for record in result["records"] if record["kind"] == "algorithm")
            self.assertNotIn("algorithm_cross_page_unsupported", algorithm["reasons"])
            self.assertTrue(result["ok"])

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

    def test_nested_quality_signals_invalid_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            (root / "document.json").write_text(json.dumps(document), encoding="utf-8")
            metadata["quality_signals"] = ["not-an-object"]
            status["quality_signals"] = ["not-an-object"]
            metadata_bytes = json.dumps(metadata, sort_keys=True).encode("utf-8")
            status_bytes = json.dumps(status, sort_keys=True).encode("utf-8")
            (root / "metadata.json").write_bytes(metadata_bytes)
            (root / "status.json").write_bytes(status_bytes)
            result = evaluate_regions(root, write_sidecars=True)
            self.assertFalse(result["ok"])
            self.assertIn("metadata_quality_signals_invalid", result["failure_reasons"])
            self.assertIn("status_quality_signals_invalid", result["failure_reasons"])
            self.assertEqual(metadata_bytes, (root / "metadata.json").read_bytes())
            self.assertEqual(status_bytes, (root / "status.json").read_bytes())

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

    def test_source_hash_declaration_is_required_even_when_source_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.pdf").write_bytes(SOURCE_BYTES)
            result = evaluate_regions(
                root,
                {"pictures": []},
                {"generated_outputs": []},
                {"ok": True, "quality_signals": {}, "warnings": []},
                write_sidecars=False,
            )
            self.assertIn("source_hash_declaration_missing", result["failure_reasons"])
            self.assertFalse(result["ok"])

    def test_malformed_explicit_pdf_inventory_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            result = evaluate_regions(
                root,
                document,
                metadata,
                status,
                pdf_inventory=[],
                write_sidecars=False,
            )
            self.assertIn("pdf_inventory_invalid", result["failure_reasons"])
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

    def test_explicit_empty_structural_declaration_does_not_fallback_to_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["table_source_expected_refs"] = []
            status["quality_signals"]["primary_surface"]["counts"]["tables"] = 0
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("expected_region_refs_empty_declaration", table["reasons"])
            self.assertFalse(result["ok"])

    def test_explicit_null_structural_declaration_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["table_source_expected_refs"] = None
            visuals["structured_table_source_renderings"]["candidates"] = []
            status["quality_signals"]["primary_surface"]["counts"]["tables"] = 0
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("expected_region_refs_invalid", table["reasons"])
            self.assertFalse(result["ok"])

    def test_formula_extra_diagnostic_index_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            metadata["formula_crop_diagnostics"].append(
                {"index": 2, "page_no": 1, "source": {"path": "formulas/formula_1.png"}}
            )
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            formula = next(record for record in result["records"] if record["kind"] == "formula")
            self.assertIn("formula_crop_diagnostic_extra_index", formula["reasons"])
            self.assertFalse(result["ok"])

    def test_formula_invalid_only_index_declaration_emits_a_critical_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["formula_source_expected_indexes"] = ["not-an-index"]
            visuals["formula_source_renderings"] = {"candidates": []}
            metadata["formula_crop_diagnostics"] = []
            records = gate._formula_region_records(
                root,
                visuals,
                metadata,
                {"formulas": 0},
                document_json={"texts": []},
                source_sha=SOURCE_SHA,
            )
            self.assertEqual(1, len(records))
            self.assertIn("formula_expected_index_invalid", records[0]["reasons"])
            self.assertEqual("unresolved", records[0]["status"])

    def test_empty_formula_and_inline_declarations_reject_extra_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["formula_source_expected_indexes"] = []
            status["quality_signals"]["primary_surface"]["counts"]["formulas"] = 0
            visuals["inline_math_source_expected_anchors"] = []
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            formula = next(record for record in result["records"] if record["kind"] == "formula")
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("formula_expected_indexes_empty_or_missing", formula["reasons"])
            self.assertIn("inline_math_expected_anchors_empty", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_missing_inline_declaration_rejects_regions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["final_source_visuals"].pop(
                "inline_math_source_expected_anchors"
            )
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("inline_math_expected_anchors_missing", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_inline_final_paragraph_binding_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            document["texts"] = [
                node for node in document["texts"] if node.get("self_ref") != "#/texts/inline-0"
            ]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("inline_math_final_node_binding_missing_or_ambiguous", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_inline_math_presentation_normalization_and_unchunked_part_zero_bind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            node = document["texts"][3]
            node["self_ref"] = "#/texts/111"
            node["text"] = (
                "Each generator s j has action α s j and model P θ in this paragraph."
            )
            region = status["quality_signals"]["primary_surface"][
                "inline_math_source_regions"
            ][0]
            region.update(
                {
                    "source_text": "Each generator s_j has action α_{s_j} and model P_θ in this paragraph.",
                    "collection_index": 111,
                    "part_index": 0,
                    "unresolved": False,
                }
            )
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate["source_text"] = region["source_text"]
            candidate["part_index"] = 0
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertEqual("verified_semantic", inline["status"])
            self.assertTrue(result["ok"])

    def test_inline_long_paragraph_binding_uses_full_text_not_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            node = document["texts"][3]
            node["self_ref"] = "#/texts/321"
            long_prefix = "leading context " * 30
            source_text = "alpha_{s_j} <= beta_{t_k}"
            node["text"] = long_prefix + source_text + " trailing explanation"
            region = status["quality_signals"]["primary_surface"]["inline_math_source_regions"][0]
            region.update(
                {
                    "source_text": source_text,
                    "collection_index": 321,
                    "part_index": 0,
                    "unresolved": False,
                }
            )
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate.update({"source_text": source_text, "part_index": 0})

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            self.assertEqual("verified_semantic", inline["status"])
            self.assertTrue(result["ok"])

            document, metadata, status = _fixture(root)
            node = document["texts"][3]
            node["self_ref"] = "#/texts/321"
            node["text"] = long_prefix + "alpha_{s_j} >= beta_{t_k}" + " trailing explanation"
            region = status["quality_signals"]["primary_surface"]["inline_math_source_regions"][0]
            region.update(
                {
                    "source_text": source_text,
                    "collection_index": 321,
                    "part_index": 0,
                    "unresolved": False,
                }
            )
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate.update({"source_text": source_text, "part_index": 0})

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            self.assertIn(
                "inline_math_final_node_binding_missing_or_ambiguous",
                inline["reasons"],
            )
            self.assertFalse(result["ok"])

    def test_inline_binding_text_at_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            limit_text = "a" * gate.MAX_BINDING_TEXT_CHARS
            node = document["texts"][3]
            node["self_ref"] = "#/texts/654"
            node["text"] = limit_text
            region = status["quality_signals"]["primary_surface"]["inline_math_source_regions"][0]
            region.update(
                {
                    "source_text": limit_text,
                    "collection_index": 654,
                    "part_index": 0,
                    "unresolved": False,
                }
            )
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate.update({"source_text": limit_text, "part_index": 0})

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            self.assertIn("inline_math_binding_text_truncated", inline["reasons"])
            self.assertTrue(inline["signals"]["binding_text_truncated"])
            self.assertFalse(result["ok"])

    def test_inline_invalid_explicit_source_ref_does_not_alias_to_valid_node(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            document["texts"][3]["self_ref"] = "#/texts/foo_bar"
            document["texts"][3]["text"] = "alpha_{s_j} and beta_{t_k} in body"
            status["quality_signals"]["primary_surface"]["inline_math_source_regions"] = [
                {
                    "anchor": "inline:1",
                    "page_no": 1,
                    "bbox": BBOX,
                    "source_text": "alpha_{s_j}",
                    "binding_mode": "inline",
                    "source_ref": "#/texts/foo bar",
                },
                {
                    "anchor": "inline:2",
                    "page_no": 1,
                    "bbox": BBOX,
                    "source_text": "alpha_{s_j}",
                    "binding_mode": "inline",
                    "source_ref": "#/texts/foo_bar",
                },
            ]
            status["quality_signals"]["primary_surface"]["inline_math_source_region_count"] = 2
            status["quality_signals"]["final_source_visuals"].update(
                {
                    "inline_math_source_expected_anchors": ["inline:1", "inline:2"],
                    "inline_math_source_html_anchors": ["inline:1", "inline:2"],
                    "inline_math_source_markdown_anchors": ["inline:1", "inline:2"],
                    "inline_math_source_renderings": {
                        "candidates": [
                            {
                                "anchor": "inline:1",
                                "image": "inline_math/0001-inline-1.png",
                                "page_no": 1,
                                "bbox": BBOX,
                                "source_text": "alpha_{s_j}",
                                "source_ref": "#/texts/foo bar",
                            },
                            {
                                "anchor": "inline:2",
                                "image": "inline_math/0001-inline-1.png",
                                "page_no": 1,
                                "bbox": BBOX,
                                "source_text": "alpha_{s_j}",
                                "source_ref": "#/texts/foo_bar",
                            },
                        ]
                    },
                }
            )

            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)

            records = [item for item in result["records"] if item["kind"] == "inline_math"]
            self.assertEqual(2, len(records))
            invalid = next(item for item in records if item["source_ref"] == "inline:1")
            self.assertIn("inline_math_source_ref_invalid", invalid["reasons"])
            self.assertFalse(result["ok"])

    def test_inline_occurrences_cannot_reuse_same_final_node(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            document["texts"][3]["self_ref"] = "#/texts/shared"
            document["texts"][3]["text"] = "alpha_{s_j} and beta_{t_k} in body"
            status["quality_signals"]["primary_surface"]["inline_math_source_regions"] = [
                {
                    "anchor": "inline:1",
                    "page_no": 1,
                    "bbox": BBOX,
                    "source_text": "alpha_{s_j}",
                    "binding_mode": "inline",
                    "source_ref": "#/texts/shared",
                },
                {
                    "anchor": "inline:2",
                    "page_no": 1,
                    "bbox": BBOX,
                    "source_text": "alpha_{s_j}",
                    "binding_mode": "inline",
                    "source_ref": "#/texts/shared",
                },
            ]
            status["quality_signals"]["primary_surface"]["inline_math_source_region_count"] = 2
            status["quality_signals"]["final_source_visuals"].update(
                {
                    "inline_math_source_expected_anchors": ["inline:1", "inline:2"],
                    "inline_math_source_html_anchors": ["inline:1", "inline:2"],
                    "inline_math_source_markdown_anchors": ["inline:1", "inline:2"],
                    "inline_math_source_renderings": {
                        "candidates": [
                            {
                                "anchor": "inline:1",
                                "image": "inline_math/0001-inline-1.png",
                                "page_no": 1,
                                "bbox": BBOX,
                                "source_text": "alpha_{s_j}",
                                "source_ref": "#/texts/shared",
                            },
                            {
                                "anchor": "inline:2",
                                "image": "inline_math/0001-inline-1.png",
                                "page_no": 1,
                                "bbox": BBOX,
                                "source_text": "alpha_{s_j}",
                                "source_ref": "#/texts/shared",
                            },
                        ]
                    },
                }
            )

            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)

            records = [item for item in result["records"] if item["kind"] == "inline_math"]
            self.assertEqual(2, len(records))
            self.assertTrue(all("inline_math_final_node_reused" in item["reasons"] for item in records))
            self.assertTrue(all(item["signals"]["final_node_unique"] is False for item in records))
            self.assertFalse(result["ok"])

    def test_inline_binding_tail_difference_past_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            prefix = "a" * gate.MAX_BINDING_TEXT_CHARS
            source_text = prefix + "DIFF"
            node = document["texts"][3]
            node["self_ref"] = "#/texts/655"
            node["text"] = prefix
            region = status["quality_signals"]["primary_surface"]["inline_math_source_regions"][0]
            region.update(
                {
                    "source_text": source_text,
                    "collection_index": 655,
                    "part_index": 0,
                    "unresolved": False,
                }
            )
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate.update({"source_text": source_text, "part_index": 0})

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )

            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            self.assertIn("inline_math_binding_text_truncated", inline["reasons"])
            self.assertTrue(inline["signals"]["region_source_text_truncated"])
            self.assertTrue(inline["signals"]["candidate_source_text_truncated"])
            self.assertTrue(inline["signals"]["node_text_truncated"])
            self.assertTrue(inline["signals"]["binding_text_truncated"])
            self.assertFalse(result["ok"])

    def test_inline_candidate_must_match_region_occurrence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            other = root / "inline_math" / "other.png"
            other.write_bytes(b"other-evidence")
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate.update(
                {
                    "image": "inline_math/other.png",
                    "asset_sha256": hashlib.sha256(b"other-evidence").hexdigest(),
                    "page_no": 99,
                    "bbox": {
                        "l": 20,
                        "r": 30,
                        "t": 30,
                        "b": 20,
                        "coord_origin": "TOPLEFT",
                    },
                    "source_text": "UNRELATED BODY",
                }
            )
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("inline_math_candidate_page_mismatch", inline["reasons"])
            self.assertIn("inline_math_candidate_bbox_mismatch", inline["reasons"])
            self.assertIn("inline_math_candidate_body_mismatch", inline["reasons"])
            self.assertFalse(result["ok"])

            document, metadata, status = _fixture(root)
            candidate = status["quality_signals"]["final_source_visuals"][
                "inline_math_source_renderings"
            ]["candidates"][0]
            candidate.pop("source_text", None)
            candidate["image"] = "inline_math/other.png"
            candidate["asset_sha256"] = hashlib.sha256(b"other-evidence").hexdigest()
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("inline_math_candidate_body_missing", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_inline_candidate_container_and_geometry_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["inline_math_source_renderings"]["candidates"] = {"bad": True}
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("inline_math_candidates_invalid", inline["reasons"])
            self.assertFalse(result["ok"])

            document, metadata, status = _fixture(root)
            primary = status["quality_signals"]["primary_surface"]
            visuals = status["quality_signals"]["final_source_visuals"]
            primary["inline_math_source_regions"][0].pop("bbox", None)
            visuals["inline_math_source_renderings"]["candidates"][0].pop("bbox", None)
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn(
                "inline_math_final_node_binding_missing_or_ambiguous",
                inline["reasons"],
            )
            self.assertFalse(result["ok"])

    def test_invalid_reference_items_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["table_source_expected_refs"] = [123]
            visuals["inline_math_source_expected_anchors"] = [None]
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("expected_region_ref_item_invalid", table["reasons"])
            self.assertIn("inline_math_expected_anchor_item_invalid", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_inline_invalid_region_anchor_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["primary_surface"]["inline_math_source_regions"][0]["anchor"] = None
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            inline = next(record for record in result["records"] if record["kind"] == "inline_math")
            self.assertIn("inline_math_region_anchor_invalid", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_structural_non_object_candidate_is_critical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            status["quality_signals"]["final_source_visuals"][
                "structured_table_source_renderings"
            ]["candidates"].append(None)
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("structural_candidate_invalid", table["reasons"])
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

    def test_table_geometry_work_is_bounded_for_hostile_spans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            cell = document["tables"][0]["data"]["table_cells"][0]
            cell.update(
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1_000_000,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1_000_000,
                    "row_span": 1_000_000,
                    "col_span": 1_000_000,
                }
            )
            result = evaluate_regions(root, document, metadata, status, write_sidecars=False)
            table = next(record for record in result["records"] if record["kind"] == "table")
            self.assertIn("table_geometry_work_limit", table["reasons"])
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

    def test_surface_binding_arrays_are_bounded_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["table_source_html_bound_refs"] = ["#/tables/0"] * 1002
            visuals["formula_source_html_indexes"] = [1] * 1002
            visuals["inline_math_source_html_anchors"] = ["inline:1"] * 1002

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(item for item in result["records"] if item["kind"] == "table")
            formula = next(item for item in result["records"] if item["kind"] == "formula")
            inline = next(item for item in result["records"] if item["kind"] == "inline_math")

            self.assertIn("structural_binding_refs_too_many", table["reasons"])
            self.assertIn("formula_surface_indexes_too_many", formula["reasons"])
            self.assertIn("inline_math_binding_refs_too_many", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_surface_binding_arrays_reject_internal_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["table_source_html_bound_refs"] = ["#/tables/0", "#/tables/0"]
            visuals["formula_source_html_indexes"] = [1, 1]
            visuals["inline_math_source_html_anchors"] = ["inline:1", "inline:1"]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(item for item in result["records"] if item["kind"] == "table")
            formula = next(item for item in result["records"] if item["kind"] == "formula")
            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            self.assertIn("structural_binding_refs_duplicate", table["reasons"])
            self.assertIn("formula_surface_indexes_duplicate", formula["reasons"])
            self.assertIn("inline_math_binding_refs_duplicate", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_diagnostic_reference_arrays_are_subsets_of_expected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["table_empty_fallback_expected_refs"] = ["#/tables/999"]
            visuals["formula_source_missing_indexes"] = [99]
            visuals["inline_math_source_missing_crop_anchors"] = ["inline:999"]

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(item for item in result["records"] if item["kind"] == "table")
            formula = next(item for item in result["records"] if item["kind"] == "formula")
            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            self.assertIn("unexpected_empty_table_fallback_refs", table["reasons"])
            self.assertIn("formula_unexpected_diagnostic_indexes", formula["reasons"])
            self.assertIn("inline_math_unexpected_diagnostic_anchors", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_manifest_required_identity_fields_fail_closed(self):
        mutations = {
            "missing_kind": lambda entry: entry.pop("kind"),
            "wrong_self_ref": lambda entry: entry.__setitem__(
                "self_ref", "#/tables/wrong"
            ),
            "missing_part_index": lambda entry: entry.pop("part_index"),
            "wrong_binding_source_ref": lambda entry: entry[
                "source_node_bindings"
            ][0].__setitem__("source_ref", "#/tables/wrong"),
            "invalid_binding_body_kind": lambda entry: entry[
                "source_node_bindings"
            ][0].__setitem__("body_identity_kind", "table_grid"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document, metadata, status = _fixture(root)
                entry = metadata["structural_visual_provenance_manifest"]["tables"][0]
                mutate(entry)
                result = evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )
                table = next(
                    item for item in result["records"] if item["kind"] == "table"
                )
                self.assertEqual("unresolved", table["status"])
                self.assertFalse(result["ok"])

    def test_algorithm_manifest_body_hash_is_bound_to_semantic_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            entry = metadata["structural_visual_provenance_manifest"]["algorithms"][0]
            entry["structural_body_identity_sha256"] = "a" * 64

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            algorithm = next(
                item for item in result["records"] if item["kind"] == "algorithm"
            )
            self.assertIn(
                "structural_manifest_body_hash_mismatch", algorithm["reasons"]
            )
            self.assertFalse(result["ok"])

    def test_algorithm_manifest_binds_every_semantic_contributor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            extra_node = {
                "label": "code",
                "text": "3: finalize",
                "self_ref": "#/texts/algorithm-extra",
                "prov": [{"page_no": 1, "bbox": BBOX}],
            }
            document["texts"].append(extra_node)
            sidecar = json.loads((root / "algorithm_blocks.json").read_text())
            sidecar[0]["source_node_bindings"].append(
                {
                    "source_ref": extra_node["self_ref"],
                    "self_ref": extra_node["self_ref"],
                    "part_index": None,
                    "page_no": 1,
                    "bbox": BBOX,
                    "body_identity_kind": "node_text",
                    "body_identity_sha256": _body_identity_sha(
                        "algorithm-source-node",
                        _node_body_identity("algorithm", extra_node),
                    ),
                }
            )
            (root / "algorithm_blocks.json").write_text(
                json.dumps(sidecar), encoding="utf-8"
            )

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            algorithm = next(
                item for item in result["records"] if item["kind"] == "algorithm"
            )
            self.assertIn(
                "algorithm_source_node_binding_set_mismatch",
                algorithm["reasons"],
            )
            self.assertFalse(result["ok"])

    def test_semantic_algorithm_sidecar_cannot_hide_cross_page_span(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            sidecar = json.loads((root / "algorithm_blocks.json").read_text())
            sidecar[0]["page_span"] = {
                "start_page": 1,
                "end_page": 2,
                "pages": [1, 2],
            }
            sidecar[0]["page_bboxes"] = [
                {"page_no": 1, "bbox": BBOX},
                {"page_no": 2, "bbox": BBOX},
            ]
            (root / "algorithm_blocks.json").write_text(
                json.dumps(sidecar), encoding="utf-8"
            )

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            algorithm = next(
                item for item in result["records"] if item["kind"] == "algorithm"
            )
            self.assertIn("algorithm_cross_page_unsupported", algorithm["reasons"])
            self.assertFalse(result["ok"])

    def test_algorithm_binding_kind_must_match_final_node_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            entry = metadata["structural_visual_provenance_manifest"]["algorithms"][0]
            manifest_binding = entry["source_node_bindings"][0]
            manifest_binding["body_identity_kind"] = "table_grid"
            manifest_binding["body_identity_sha256"] = _body_identity_sha(
                "algorithm-source-node", ""
            )
            sidecar = json.loads((root / "algorithm_blocks.json").read_text())
            semantic_binding = sidecar[0]["source_node_bindings"][0]
            semantic_binding["body_identity_kind"] = "table_grid"
            semantic_binding["body_identity_sha256"] = manifest_binding[
                "body_identity_sha256"
            ]
            (root / "algorithm_blocks.json").write_text(
                json.dumps(sidecar), encoding="utf-8"
            )

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            algorithm = next(
                item for item in result["records"] if item["kind"] == "algorithm"
            )
            self.assertIn(
                "algorithm_source_node_body_identity_kind_mismatch",
                algorithm["reasons"],
            )
            self.assertFalse(result["ok"])

    def test_algorithm_table_grid_contributor_cannot_use_empty_or_sparse_fallback(self):
        """A table contributor is strict even when the ordinary table uses fallback."""

        cases = ("empty", "partial", "sparse", "complete")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document, metadata, status = _fixture(root)
                table = document["tables"][0]
                table_ref = table["self_ref"]
                data = table["data"]
                if case == "empty":
                    data.update(num_rows=1, num_cols=1, table_cells=[])
                elif case in {"partial", "sparse"}:
                    retained = [data["table_cells"][0]]
                    if case == "sparse":
                        retained = [dict(retained[0])]
                        retained[0].update(
                            start_row_offset_idx=1,
                            end_row_offset_idx=2,
                            start_col_offset_idx=1,
                            end_col_offset_idx=2,
                        )
                    data.update(num_rows=2, num_cols=2, table_cells=retained)

                table_identity = _node_body_identity("table", table)
                table_entry = metadata["structural_visual_provenance_manifest"][
                    "tables"
                ][0]
                table_entry["structural_body_identity_sha256"] = _body_identity_sha(
                    "table", table_identity
                )
                table_entry["source_node_bindings"][0]["body_identity_sha256"] = (
                    _body_identity_sha("table", table_identity)
                )

                # Synchronise both algorithm evidence stores to the table node,
                # as an attacker would when trying to promote a malformed grid.
                algorithm_entry = metadata["structural_visual_provenance_manifest"][
                    "algorithms"
                ][0]
                manifest_binding = algorithm_entry["source_node_bindings"][0]
                manifest_binding.update(
                    {
                        "source_ref": table_ref,
                        "self_ref": table_ref,
                        "body_identity_kind": "table_grid",
                        "body_identity_sha256": _body_identity_sha(
                            "algorithm-source-node", table_identity
                        ),
                    }
                )
                sidecar = json.loads((root / "algorithm_blocks.json").read_text())
                semantic_binding = sidecar[0]["source_node_bindings"][0]
                semantic_binding.update(
                    {
                        "source_ref": table_ref,
                        "self_ref": table_ref,
                        "body_identity_kind": "table_grid",
                        "body_identity_sha256": manifest_binding[
                            "body_identity_sha256"
                        ],
                    }
                )
                (root / "algorithm_blocks.json").write_text(
                    json.dumps(sidecar), encoding="utf-8"
                )

                # Keep the algorithm record's own text identity intact; only
                # its source contributor is the table.  Explicitly marking the
                # table as an empty visual fallback must not relax this path.
                algorithm_entry["structural_body_identity_sha256"] = _body_identity_sha(
                    "algorithm", gate._algorithm_expected_body_identity(sidecar[0])
                )
                visuals = status["quality_signals"]["final_source_visuals"]
                visuals["table_empty_fallback_expected_refs"] = [table_ref]
                visuals["table_source_body_identity_expected_refs"] = []
                visuals["table_source_html_body_identity_verified_refs"] = []
                visuals["table_source_markdown_body_identity_verified_refs"] = []

                result = evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )
                algorithm = next(
                    record for record in result["records"] if record["kind"] == "algorithm"
                )
                if case == "complete":
                    self.assertEqual("verified_semantic", algorithm["status"])
                    self.assertTrue(result["ok"])
                else:
                    self.assertEqual("unresolved", algorithm["status"])
                    self.assertIn(
                        "algorithm_source_node_body_identity_kind_mismatch",
                        algorithm["reasons"],
                    )
                    self.assertFalse(result["ok"])

    def test_inline_math_operators_are_identity_bearing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            primary = status["quality_signals"]["primary_surface"]
            visuals = status["quality_signals"]["final_source_visuals"]
            primary["inline_math_source_regions"][0]["source_text"] = "x+y"
            visuals["inline_math_source_renderings"]["candidates"][0][
                "source_text"
            ] = "x+y"
            document["texts"][3]["text"] = "The inline expression xy is wrong."

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            inline = next(
                item for item in result["records"] if item["kind"] == "inline_math"
            )
            self.assertIn(
                "inline_math_final_node_binding_missing_or_ambiguous",
                inline["reasons"],
            )
            self.assertFalse(result["ok"])

    def test_inline_math_candidate_cannot_truncate_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            visuals = status["quality_signals"]["final_source_visuals"]
            visuals["inline_math_source_renderings"]["candidates"][0][
                "source_text"
            ] = "x"

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            inline = next(
                item for item in result["records"] if item["kind"] == "inline_math"
            )
            self.assertIn("inline_math_candidate_body_mismatch", inline["reasons"])
            self.assertFalse(result["ok"])

    def test_empty_structural_declarations_cannot_hide_final_artifacts(self):
        cases = (
            ("table", "tables", "structured_table_source_renderings", "tables"),
            ("algorithm", "algorithms", "algorithm_source_renderings", "algorithms"),
            ("code", "code_blocks", "code_source_renderings", "code"),
        )
        for kind, count_key, payload_key, manifest_key in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document, metadata, status = _fixture(root)
                visuals = status["quality_signals"]["final_source_visuals"]
                visuals[f"{kind}_source_expected_refs"] = []
                payload = visuals[payload_key]
                if isinstance(payload, dict):
                    payload["candidates"] = []
                    if kind == "algorithm":
                        payload["records"] = []
                status["quality_signals"]["primary_surface"]["counts"][count_key] = 0
                metadata["structural_visual_provenance_manifest"][manifest_key] = []

                result = evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )
                record = next(
                    item for item in result["records"] if item["kind"] == kind
                )
                self.assertIn("final_document_ref_set_mismatch", record["reasons"])
                self.assertFalse(result["ok"])

    def test_nonempty_table_requires_semantic_cell_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            table_node = document["tables"][0]
            table_node["data"]["table_cells"] = []
            identity = _node_body_identity("table", table_node)
            entry = metadata["structural_visual_provenance_manifest"]["tables"][0]
            entry["structural_body_identity_sha256"] = _body_identity_sha(
                "table", identity
            )
            entry["source_node_bindings"][0]["body_identity_sha256"] = (
                _body_identity_sha("table", identity)
            )

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(item for item in result["records"] if item["kind"] == "table")
            self.assertIn("table_cell_geometry_missing", table["reasons"])
            self.assertFalse(result["ok"])

    def test_table_cell_occupancy_must_cover_declared_grid(self):
        for retained_cell_count in (1, 2, 3):
            with self.subTest(retained=retained_cell_count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document, metadata, status = _fixture(root)
                table_node = document["tables"][0]
                table_node["data"]["table_cells"] = table_node["data"][
                    "table_cells"
                ][:retained_cell_count]
                identity = _node_body_identity("table", table_node)
                entry = metadata["structural_visual_provenance_manifest"]["tables"][0]
                entry["structural_body_identity_sha256"] = _body_identity_sha(
                    "table", identity
                )
                entry["source_node_bindings"][0]["body_identity_sha256"] = (
                    _body_identity_sha("table", identity)
                )

                result = evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )
                table = next(
                    item for item in result["records"] if item["kind"] == "table"
                )
                self.assertIn("table_cell_occupancy_incomplete", table["reasons"])
                self.assertFalse(result["ok"])

    def test_empty_table_requires_explicit_fallback_even_with_bad_dimensions(self):
        dimensions = (("2.0", 2), (None, 2), ("bad", None), (0, 2), (0, 0))
        for rows, cols in dimensions:
            with self.subTest(rows=rows, cols=cols), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document, metadata, status = _fixture(root)
                table_node = document["tables"][0]
                table_node["data"].update(
                    {"num_rows": rows, "num_cols": cols, "table_cells": []}
                )
                identity = _node_body_identity("table", table_node)
                entry = metadata["structural_visual_provenance_manifest"]["tables"][0]
                entry["structural_body_identity_sha256"] = _body_identity_sha(
                    "table", identity
                )
                entry["source_node_bindings"][0]["body_identity_sha256"] = (
                    _body_identity_sha("table", identity)
                )

                result = evaluate_regions(
                    root, document, metadata, status, write_sidecars=False
                )
                table = next(
                    item for item in result["records"] if item["kind"] == "table"
                )
                self.assertIn("table_cell_geometry_missing", table["reasons"])
                self.assertFalse(result["ok"])

    def test_explicit_null_counts_and_quarantine_are_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            primary = status["quality_signals"]["primary_surface"]
            primary["counts"]["tables"] = None
            primary["inline_math_source_region_count"] = None
            status["quality_signals"]["structural_quarantine_qc"][
                "candidates"
            ] = None

            result = evaluate_regions(
                root, document, metadata, status, write_sidecars=False
            )
            table = next(item for item in result["records"] if item["kind"] == "table")
            inline = next(item for item in result["records"] if item["kind"] == "inline_math")
            quarantine = next(
                item
                for item in result["records"]
                if item["source_ref"] == "picture_ocr:quarantine-schema"
            )
            self.assertIn("expected_region_count_invalid", table["reasons"])
            self.assertIn("inline_math_expected_count_invalid", inline["reasons"])
            self.assertIn("quarantine_candidates_invalid", quarantine["reasons"])
            self.assertFalse(result["ok"])

    def test_caller_state_is_persisted_after_sidecar_publish_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, metadata, status = _fixture(root)
            (root / "metadata.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
            (root / "status.json").write_text(json.dumps(status), encoding="utf-8")
            original_atomic_json = gate._atomic_json

            def fail_quality_signals(path, payload):
                if path.name == "quality_signals.json":
                    return "injected_publish_failure"
                return original_atomic_json(path, payload)

            gate._atomic_json = fail_quality_signals
            try:
                result = evaluate_regions(root, document, metadata, status)
            finally:
                gate._atomic_json = original_atomic_json

            persisted_status = json.loads(
                (root / "status.json").read_text(encoding="utf-8")
            )
            persisted_regions = json.loads(
                (root / "regions.json").read_text(encoding="utf-8")
            )
            self.assertFalse(result["ok"])
            self.assertFalse(persisted_status["ok"])
            self.assertEqual("degraded_failure", persisted_status["success_class"])
            self.assertFalse(persisted_regions["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
