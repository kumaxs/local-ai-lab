from __future__ import annotations

from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
import sys
from unittest.mock import patch
from importlib import util as _importlib_util

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdf_structure_inventory as module  # noqa: E402


def _safe_line_text(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item)

_HAS_PDFPARSER = _importlib_util.find_spec("pdfplumber") is not None


def _make_pdf(path: Path, page_text: str = "Integration test") -> None:
    content_bytes = f"BT /F1 24 Tf 1 0 0 1 72 700 Tm ({page_text}) Tj ET".encode("latin1")
    objects = []

    objects.append(
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    )
    objects.append(
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    )
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    )
    objects.append(
        f"4 0 obj\n<< /Length {len(content_bytes)} >>\nstream\n".encode("latin1")
        + content_bytes
        + b"\nendstream\nendobj\n"
    )
    objects.append(
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    )

    cursor = len("%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(cursor)
        cursor += len(obj)

    pdf_parts = ["%PDF-1.4\n".encode("latin1")]
    pdf_parts.extend(objects)
    pdf_parts.append(b"xref\n0 6\n")
    pdf_parts.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf_parts.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf_parts.append(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{cursor}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(b"".join(pdf_parts))


class PdfStructureInventoryUnitTests(unittest.TestCase):
    @unittest.skipUnless(_HAS_PDFPARSER, "pdfplumber is unavailable")
    def test_unknown_cid_marker_does_not_make_readable_page_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "unknown-token.pdf"
            _make_pdf(pdf_path, "Readable text with (cid:173) marker")

            health = module._document_text_health(pdf_path)

        self.assertEqual("healthy", health["status"])
        self.assertEqual("unknown_tokens", health["reason"])
        self.assertTrue(health["pages"][0]["healthy"])
        self.assertIn("unknown_tokens", health["pages"][0]["reasons"])

    def test_algorithm_heading_detected(self) -> None:
        nodes = [
            {"text": "Algorithm 1", "page_no": 1, "index": 0},
            {"text": "Require Input", "page_no": 1, "index": 1},
            {"text": "1: x = 1", "page_no": 1, "index": 2},
            {"text": "2: y = x", "page_no": 1, "index": 3},
            {"text": "3: if x > y", "page_no": 1, "index": 4},
        ]
        records = module._classify_algorithm_records(nodes)
        self.assertEqual(1, len(records))
        self.assertEqual("algorithm", records[0]["kind"])
        self.assertEqual("high", records[0]["confidence"])

    def test_algorithm_prose_like_header_rejected(self) -> None:
        nodes = [
            {"text": "Algorithm 1 is designed to achieve...", "page_no": 1, "index": 0},
            {"text": "This paragraph uses many words and does not contain any numbered or control lines.", "page_no": 1, "index": 1},
        ]
        self.assertEqual([], module._classify_algorithm_records(nodes))

    def test_algorithm_heading_without_space_accepted(self) -> None:
        nodes = [
            {"text": "Algorithm1PubTables-1 M Canonicalization", "page_no": 1, "index": 0},
            {"text": "Require Input", "page_no": 1, "index": 1},
            {"text": "1) x = 1", "page_no": 1, "index": 2},
            {"text": "2) y = x", "page_no": 1, "index": 3},
            {"text": "3) if x > y", "page_no": 1, "index": 4},
        ]
        records = module._classify_algorithm_records(nodes)
        self.assertEqual(1, len(records))
        self.assertEqual("algorithm", records[0]["kind"])
        self.assertEqual("high", records[0]["confidence"])

    def test_definition_algorithm_can_continue_to_numbered_steps_on_next_page(self) -> None:
        nodes = [
            {
                "text": "Definition 6. A decoding algorithm",
                "page_no": 7,
                "index": 0,
                "source": "pdf_lines",
                "bbox": {"l": 70, "r": 310, "t": 120, "b": 108},
            },
            {
                "text": "takes the following steps:",
                "page_no": 7,
                "index": 1,
                "source": "pdf_lines",
                "bbox": {"l": 70, "r": 260, "t": 140, "b": 128},
            },
            {
                "text": "7",
                "page_no": 7,
                "index": 2,
                "source": "pdf_lines",
                "bbox": {"l": 300, "r": 306, "t": 760, "b": 750},
            },
            {
                "text": "1. Compute the row support.",
                "page_no": 8,
                "index": 3,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 300, "t": 80, "b": 68},
            },
            {
                "text": "2. Compute the column support.",
                "page_no": 8,
                "index": 4,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 320, "t": 98, "b": 86},
            },
            {
                "text": "3. Peel the candidate sets.",
                "page_no": 8,
                "index": 5,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 300, "t": 116, "b": 104},
            },
            {
                "text": "4. Return the correction.",
                "page_no": 8,
                "index": 6,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 292, "t": 134, "b": 122},
            },
            {
                "text": "Theorem 1. The decoder succeeds.",
                "page_no": 8,
                "index": 7,
                "source": "pdf_lines",
                "bbox": {"l": 70, "r": 290, "t": 170, "b": 158},
            },
        ]

        records = module._classify_algorithm_records(nodes)

        self.assertEqual(1, len(records))
        self.assertEqual("algorithm", records[0]["kind"])
        self.assertIn("4. Return the correction.", records[0]["text"])
        self.assertNotIn("\n7\n", records[0]["text"])
        self.assertNotIn("Theorem 1", records[0]["text"])

        self.assertEqual(
            records[0]["page_span"],
            {"start_page": 7, "end_page": 8, "pages": [7, 8]},
        )
        self.assertEqual(
            [item["page_no"] for item in records[0]["page_bboxes"]],
            [7, 8],
        )
        self.assertEqual(
            {item["page_no"] for item in records[0]["node_sources"]},
            {7, 8},
        )

    def test_add_to_counts_preserves_cross_page_algorithm_evidence(self) -> None:
        nodes = [
            {
                "text": "Definition 6. A decoding algorithm",
                "page_no": 7,
                "index": 0,
                "source": "pdf_lines",
                "bbox": {"l": 70, "r": 310, "t": 120, "b": 108},
            },
            {
                "text": "takes the following steps:",
                "page_no": 7,
                "index": 1,
                "source": "pdf_lines",
                "bbox": {"l": 70, "r": 260, "t": 140, "b": 128},
            },
            {
                "text": "1. Compute the row support.",
                "page_no": 8,
                "index": 2,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 300, "t": 80, "b": 68},
            },
            {
                "text": "2. Compute the column support.",
                "page_no": 8,
                "index": 3,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 320, "t": 98, "b": 86},
            },
            {
                "text": "3. Peel the candidate sets.",
                "page_no": 8,
                "index": 4,
                "source": "pdf_lines",
                "bbox": {"l": 88, "r": 300, "t": 116, "b": 104},
            },
        ]
        record = module._classify_algorithm_records(nodes)[0]
        counts = {kind: module._build_base_counts() for kind in module.KIND_ORDER}
        module._add_to_counts(counts, [record])
        persisted = counts["algorithm"]["records"][0]
        self.assertEqual(persisted["page_span"]["pages"], [7, 8])
        self.assertEqual(
            [item["page_no"] for item in persisted["page_bboxes"]],
            [7, 8],
        )
        self.assertTrue(all(item["source"] == "pdf_lines" for item in persisted["node_sources"]))

    def test_definition_algorithm_rejects_unrelated_numbered_list_without_continuation_cue(self) -> None:
        nodes = [
            {"text": "Definition 2. A routing algorithm", "page_no": 1, "index": 0},
            {"text": "The definition is discussed below.", "page_no": 1, "index": 1},
            {"text": "1. First unrelated property.", "page_no": 2, "index": 2},
            {"text": "2. Second unrelated property.", "page_no": 2, "index": 3},
            {"text": "3. Third unrelated property.", "page_no": 2, "index": 4},
        ]

        self.assertEqual([], module._classify_algorithm_records(nodes))

    def test_bert_input_label_block_is_code_not_algorithm(self) -> None:
        nodes = [
            {"text": "Input = [CLS]", "page_no": 1, "index": 0},
            {"text": "Input = this sentence", "page_no": 1, "index": 1},
            {"text": "Label = IsNext", "page_no": 1, "index": 2},
            {"text": "Label = NotNext", "page_no": 1, "index": 3},
        ]
        algorithm_records = module._classify_algorithm_records(nodes)
        self.assertEqual([], algorithm_records)

        algorithm_indexes = {
            index
            for record in algorithm_records
            for index in (record.get("line_indexes") or [])
            if isinstance(index, int)
        }
        code_records = module._classify_code_records(nodes, set(algorithm_indexes))

        self.assertEqual(1, len(code_records))
        self.assertEqual("code", code_records[0]["kind"])
        self.assertEqual("high", code_records[0]["confidence"])

    def test_table_caption_with_row_body_detected(self) -> None:
        lines = [
            {"text": "Table 5:", "page_no": 1, "index": 0},
            {"text": "Model    AUC    Params", "page_no": 1, "index": 1},
            {"text": "M1       0.91   10", "page_no": 1, "index": 2},
            {"text": "M2       0.87   12", "page_no": 1, "index": 3},
            {"text": "M3       0.83   14", "page_no": 1, "index": 4},
        ]
        records = module._classify_table_records(lines)
        self.assertEqual(1, len(records))
        self.assertEqual("table", records[0]["kind"])
        self.assertIn(records[0]["confidence"], {"high", "ambiguous"})

    def test_table_caption_cn_punct_patterns_detected(self) -> None:
        lines = [
            {"text": "Figure 1: summary", "page_no": 1, "index": 0},
            {"text": "表!(数据集简介", "page_no": 2, "index": 1},
            {"text": "表#(实验环境", "page_no": 2, "index": 2},
            {"text": "表,(不同模型的5lL值", "page_no": 2, "index": 3},
            {"text": "表+(不同模型的5 LL值", "page_no": 3, "index": 4},
            {"text": "表)(智慧学习环境中的准确率对比", "page_no": 3, "index": 5},
            {"text": "Random table 1: summary", "page_no": 4, "index": 6},
        ]
        records = module._classify_table_records(lines)
        self.assertEqual(5, len(records))
        texts = [r["text"] for r in records]
        self.assertIn("表!(数据集简介", texts)
        self.assertIn("表,(不同模型的5lL值", texts)
        self.assertNotIn("Random table 1: summary", texts)

    def test_bert_table_false_positive_rejected(self) -> None:
        lines = [
            {
                "text": "Table 6. In this table, we report the average Dev",
                "page_no": 8,
                "index": 0,
                "width": 600.0,
                "bbox": {"l": 48.0, "r": 420.0, "t": 540.0, "b": 528.0},
                "fonts": ["Helvetica"],
            },
            {
                "text": "Set of results continue below",
                "page_no": 8,
                "index": 1,
                "width": 600.0,
                "bbox": {"l": 48.0, "r": 340.0, "t": 510.0, "b": 498.0},
                "fonts": ["Helvetica"],
            },
        ]
        records = module._classify_table_records(lines)
        self.assertEqual(0, len(records))

    def test_formula_regressions_from_bert_false_positives(self) -> None:
        lines = [
            {
                "text": "A=16,TotalParameters=340M).",
                "page_no": 1,
                "index": 0,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 380.0, "t": 760.0, "b": 744.0},
                "fonts": ["Times"],
            },
            {
                "text": "asT ∈ RH. the unchanged i-th token 10% of the time. Then,",
                "page_no": 1,
                "index": 1,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 430.0, "t": 730.0, "b": 714.0},
                "fonts": ["Times"],
            },
            {
                "text": "This is directly comparable to OpenAI GPT, but Vaswani et al. (2017) is (L=6, H=1024, A=16)",
                "page_no": 1,
                "index": 2,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 540.0, "t": 700.0, "b": 684.0},
                "fonts": ["Times"],
            },
            {
                "text": "WefirstexaminetheimpactbroughtbytheNSP is (L=64, H=512, A=2) with 235M parameters",
                "page_no": 1,
                "index": 3,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 560.0, "t": 670.0, "b": 654.0},
                "fonts": ["Times"],
            },
            {
                "text": "MLMdoesconvergemarginallyslowerthanaleft- use Adam with learning rate of 1e-4, β = 0.9,",
                "page_no": 1,
                "index": 4,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 560.0, "t": 640.0, "b": 624.0},
                "fonts": ["Times"],
            },
        ]
        records = module._classify_formula_records(lines)
        self.assertEqual(0, len(records))

    def test_formula_display_vs_inline(self) -> None:
        lines = [
            {
                "text": "x + y = z",
                "page_no": 1,
                "index": 0,
                "width": 600.0,
                "bbox": {"l": 20.0, "r": 160.0, "t": 800.0, "b": 780.0},
                "fonts": ["Helvetica"],
            },
            {
                "text": "E = m c^2 (1)",
                "page_no": 1,
                "index": 1,
                "width": 600.0,
                "bbox": {"l": 220.0, "r": 360.0, "t": 740.0, "b": 720.0},
                "fonts": ["Cambria Math"],
            },
            {
                "text": "y_k = 3",
                "page_no": 1,
                "index": 2,
                "width": 600.0,
                "bbox": {"l": 30.0, "r": 140.0, "t": 700.0, "b": 680.0},
                "fonts": ["Times"],
            },
        ]
        records = module._classify_formula_records(lines)
        formula_texts = {record["text"] for record in records}
        self.assertIn("E = m c^2 (1)", formula_texts)
        self.assertNotIn("inline: x + y = z", formula_texts)

    def test_formula_bert_long_parenthetical_and_comparison_rejected(self) -> None:
        lines = [
            {
                "text": "and the final hidden state is encoded as (2)",
                "page_no": 1,
                "index": 0,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 460.0, "t": 300.0, "b": 280.0},
                "fonts": ["Times"],
            },
            {
                "text": "i,j j≥i i j",
                "page_no": 1,
                "index": 1,
                "width": 600.0,
                "bbox": {"l": 180.0, "r": 280.0, "t": 250.0, "b": 230.0},
                "fonts": ["Times"],
            },
        ]
        records = module._classify_formula_records(lines)
        self.assertEqual([], records)

    def test_formula_hyphen_word_dash_not_operator(self) -> None:
        lines = [
            {
                "text": "ing,(2)hypothesis-premisepairsinentailment,(3)",
                "page_no": 1,
                "index": 0,
                "width": 600.0,
                "bbox": {"l": 72.0, "r": 290.0, "t": 300.0, "b": 280.0},
                "fonts": ["Times"],
            }
        ]
        records = module._classify_formula_records(lines)
        self.assertEqual([], records)

    def test_formula_right_column_display_fallback_detected(self) -> None:
        lines = [
            {
                "text": "The baseline in this section is GriTS (A,B)= i,j i,j i,j , (1)",
                "page_no": 1,
                "index": 1,
                "width": 612.0,
                "bbox": {"l": 343.0, "r": 545.0, "t": 380.0, "b": 368.0},
                "fonts": ["Times"],
            },
            {
                "text": "some paragraph continues below",
                "page_no": 1,
                "index": 2,
                "width": 612.0,
                "bbox": {"l": 70.0, "r": 560.0, "t": 352.0, "b": 340.0},
                "fonts": ["Times"],
            },
            {
                "text": "further context for explanation",
                "page_no": 1,
                "index": 3,
                "width": 612.0,
                "bbox": {"l": 70.0, "r": 360.0, "t": 410.0, "b": 398.0},
                "fonts": ["Times"],
            },
        ]
        records = module._classify_formula_records(lines)
        self.assertEqual(1, len(records))
        self.assertEqual("formula", records[0]["kind"])
        self.assertEqual("high", records[0]["confidence"])

    def test_formula_right_column_display_fallback_from_transformer_source(self) -> None:
        source_path = Path("/Users/zeyuan/Projects/local-ai-lab/.runtime/review/paper-regression-2026-08-10/sources/old/table-heavy-ai-table-transformer.pdf")
        if not source_path.exists():
            self.skipTest("transformer review source PDF not available")
        lines = module._extract_pdf_lines(source_path)
        target_text = "GriTS (A,B)= i,j i,j i,j , (1)"
        matches = [line for line in lines if target_text in _safe_line_text(line)]
        self.assertTrue(matches, f"missing target formula line: {target_text!r}")
        records = module._classify_formula_records(lines)
        self.assertTrue(any(target_text in _safe_line_text(r) for r in records))

    def test_synthetic_pdf_inventory_smoke(self) -> None:
        if not _HAS_PDFPARSER:
            self.skipTest("pdfplumber dependency not installed in this environment")

        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "smoke.pdf"
            _make_pdf(pdf_path)
            result = module.pdf_structure_inventory(pdf_path)
        self.assertTrue(result["available"])
        self.assertIn("counts", result)
        self.assertIn("algorithm", result["counts"])
        self.assertIn("no_structure_proof", result)

    def test_image_only_health_drives_unknown_proof(self) -> None:
        def _fake_health(_: Path) -> dict:
            return {
                "available": True,
                "status": "unknown",
                "reason": "image_only",
                "page_count": 1,
                "page_no_continuous": True,
                "pages": [
                    {
                        "page_no": 1,
                        "healthy": False,
                        "text_chars": 0,
                        "images": 1,
                        "reasons": ["image_only"],
                    }
                ],
            }

        source = {"records": []}
        with tempfile.TemporaryDirectory() as td, \
             patch("pdf_structure_inventory._document_text_health", side_effect=_fake_health), \
             patch("pdf_structure_inventory._extract_pdf_lines", return_value=[]):
            pdf_path = Path(td) / "dummy.pdf"
            _make_pdf(pdf_path)
            result = module.pdf_structure_inventory(pdf_path)

        self.assertEqual({k: "unknown" for k in module.KIND_ORDER}, result["no_structure_proof"])
        self.assertEqual("image_only", result["reason"])
        self.assertEqual("unknown", result["text_health"]["status"])

    def test_formula_unknown_token_affects_only_formula_proof(self) -> None:
        def _fake_health(_: Path) -> dict:
            return {
                "available": True,
                "status": "healthy",
                "reason": None,
                "page_count": 1,
                "page_no_continuous": True,
                "pages": [
                    {
                        "page_no": 1,
                        "healthy": True,
                        "text_chars": 230,
                        "images": 0,
                        "reasons": ["unknown_tokens"],
                    }
                ],
            }

        source = {"records": []}
        with (
            tempfile.TemporaryDirectory() as td,
            patch("pdf_structure_inventory._document_text_health", side_effect=_fake_health),
            patch(
                "pdf_structure_inventory._extract_pdf_lines",
                side_effect=[
                    [
                        {
                            "text": "We report this (cid:173) in table 1",
                            "label": "text",
                            "page_no": 1,
                            "index": 0,
                            "bbox": {
                                "l": 40.0,
                                "r": 430.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "TOPLEFT",
                            },
                            "width": 612.0,
                            "height": 792.0,
                            "fonts": ["Times"],
                            "spans": [{"text": "We report this (cid:173) in table 1", "fontname": "Times"}],
                            "words": [
                                {
                                    "text": "We",
                                    "x0": 40.0,
                                    "x1": 60.0,
                                    "size": 11.0,
                                    "fontname": "Times",
                                    "top": 680.0,
                                    "bottom": 692.0,
                                }
                            ],
                        }
                    ],
                ],
            ),
        ):
            pdf_path = Path(td) / "formula_unknown_inline.pdf"
            _make_pdf(pdf_path)
            inline_result = module.pdf_structure_inventory(pdf_path)

            self.assertEqual("healthy", inline_result["no_structure_proof"]["formula"])

        with (
            tempfile.TemporaryDirectory() as td,
            patch("pdf_structure_inventory._document_text_health", side_effect=_fake_health),
            patch(
                "pdf_structure_inventory._extract_pdf_lines",
                side_effect=[
                    [
                        {
                            "text": "E = mc^2 (cid:173)",
                            "label": "text",
                            "page_no": 1,
                            "index": 0,
                            "bbox": {
                                "l": 180.0,
                                "r": 390.0,
                                "t": 300.0,
                                "b": 280.0,
                                "coord_origin": "TOPLEFT",
                            },
                            "width": 612.0,
                            "height": 792.0,
                            "fonts": ["Cambria Math"],
                            "spans": [
                                {
                                    "text": "E",
                                    "fontname": "Cambria Math",
                                    "size": 18.0,
                                    "x0": 180.0,
                                    "x1": 190.0,
                                    "top": 280.0,
                                    "bottom": 300.0,
                                },
                                {
                                    "text": "=",
                                    "fontname": "Cambria Math",
                                    "size": 18.0,
                                    "x0": 196.0,
                                    "x1": 204.0,
                                    "top": 280.0,
                                    "bottom": 300.0,
                                },
                                {
                                    "text": "m",
                                    "fontname": "Cambria Math",
                                    "size": 18.0,
                                    "x0": 208.0,
                                    "x1": 220.0,
                                    "top": 280.0,
                                    "bottom": 300.0,
                                },
                                {
                                    "text": "c",
                                    "fontname": "Cambria Math",
                                    "size": 18.0,
                                    "x0": 222.0,
                                    "x1": 232.0,
                                    "top": 280.0,
                                    "bottom": 300.0,
                                },
                                {
                                    "text": "^2",
                                    "fontname": "Cambria Math",
                                    "size": 14.0,
                                    "x0": 234.0,
                                    "x1": 244.0,
                                    "top": 280.0,
                                    "bottom": 300.0,
                                },
                                {
                                    "text": "(cid:173)",
                                    "fontname": "Cambria Math",
                                    "size": 18.0,
                                    "x0": 360.0,
                                    "x1": 390.0,
                                    "top": 280.0,
                                    "bottom": 300.0,
                                },
                            ],
                            "words": [
                                {"text": "E", "x0": 180.0, "x1": 190.0, "size": 18.0, "fontname": "Cambria Math", "top": 280.0, "bottom": 300.0},
                                {"text": "=", "x0": 196.0, "x1": 204.0, "size": 18.0, "fontname": "Cambria Math", "top": 280.0, "bottom": 300.0},
                                {"text": "mc^2", "x0": 208.0, "x1": 234.0, "size": 18.0, "fontname": "Cambria Math", "top": 280.0, "bottom": 300.0},
                                {"text": "(cid:173)", "x0": 360.0, "x1": 390.0, "size": 18.0, "fontname": "Cambria Math", "top": 280.0, "bottom": 300.0},
                            ],
                        }
                    ],
                ],
            ),
        ):
            pdf_path = Path(td) / "formula_unknown_centered.pdf"
            _make_pdf(pdf_path)
            centered_result = module.pdf_structure_inventory(pdf_path)

            self.assertEqual("unknown", centered_result["no_structure_proof"]["formula"])

        self.assertEqual("healthy", inline_result["no_structure_proof"]["algorithm"])
        self.assertEqual("healthy", inline_result["no_structure_proof"]["table"])
        self.assertEqual("healthy", inline_result["no_structure_proof"]["code"])

    def test_line_extraction_exception_yields_unavailable(self) -> None:
        source = {"records": []}

        with (
            tempfile.TemporaryDirectory() as td,
            patch("pdf_structure_inventory._document_text_health") as mock_health,
            patch("pdf_structure_inventory._extract_pdf_lines", side_effect=RuntimeError("boom")),
        ):
            mock_health.return_value = {
                "available": True,
                "status": "healthy",
                "reason": None,
                "page_count": 1,
                "page_no_continuous": True,
                "pages": [
                    {
                        "page_no": 1,
                        "healthy": True,
                        "text_chars": 200,
                        "images": 0,
                        "reasons": [],
                    }
                ],
            }
            pdf_path = Path(td) / "boom.pdf"
            _make_pdf(pdf_path)
            result = module.pdf_structure_inventory(pdf_path)

        self.assertFalse(result["available"])
        self.assertEqual("line_extraction_failed:RuntimeError", result["reason"])
        self.assertEqual({k: "unknown" for k in module.KIND_ORDER}, result["no_structure_proof"])

    def test_line_extraction_empty_lines_marks_unknown(self) -> None:
        source = {"records": []}

        with (
            tempfile.TemporaryDirectory() as td,
            patch("pdf_structure_inventory._document_text_health") as mock_health,
            patch("pdf_structure_inventory._extract_pdf_lines", return_value=[]),
        ):
            mock_health.return_value = {
                "available": True,
                "status": "healthy",
                "reason": None,
                "page_count": 1,
                "page_no_continuous": True,
                "pages": [
                    {
                        "page_no": 1,
                        "healthy": True,
                        "text_chars": 200,
                        "images": 0,
                        "reasons": [],
                    }
                ],
            }
            pdf_path = Path(td) / "empty_lines.pdf"
            _make_pdf(pdf_path)
            result = module.pdf_structure_inventory(pdf_path)

        self.assertTrue(result["available"])
        self.assertEqual("line_extraction_no_lines", result["reason"])
        self.assertEqual({k: "unknown" for k in module.KIND_ORDER}, result["no_structure_proof"])

    def test_collect_nodes_from_document_json_is_readonly_and_no_double_offset(self) -> None:
        document_json = {
            "chunks": [
                {
                    "page_range": [7, 8],
                    "document": {
                        "pages": {"1": {}, "2": {}},
                        "tables": [
                            {
                                "text": "global page should stay",
                                "prov": [{"page_no": 7, "bbox": {"l": 0, "r": 1, "t": 2, "b": 3}}],
                            },
                            {
                                "text": "local page should offset",
                                "prov": [{"page_no": 2, "bbox": {"l": 0, "r": 1, "t": 2, "b": 3}}],
                            },
                        ],
                    },
                }
            ]
        }

        original_document_json = deepcopy(document_json)
        _, table_nodes, _ = module._collect_nodes_from_document_json(document_json)

        self.assertEqual(2, len(table_nodes))
        self.assertEqual(7, table_nodes[0]["page_no"])
        self.assertEqual(8, table_nodes[1]["page_no"])
        self.assertEqual(
            7,
            original_document_json["chunks"][0]["document"]["tables"][0]["prov"][0]["page_no"],
        )
        self.assertEqual(
            2,
            original_document_json["chunks"][0]["document"]["tables"][1]["prov"][0]["page_no"],
        )

    def test_has_math_font_handles_tuple_fonts_and_spans(self) -> None:
        node = {
            "fonts": ("Times New Roman", "Times-Bold"),
            "spans": (
                {"fontname": "Times New Roman"},
                {"fontname": "Cambria Math"},
                {"fontname": "Times New Roman"},
            ),
        }
        ratio = module._has_math_font(node)
        self.assertGreater(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)

        missing_fonts = {
            "spans": (
                {"fontname": "Times New Roman"},
                {"fontname": "Latin Modern Math"},
            )
        }
        missing_ratio = module._has_math_font(missing_fonts)
        self.assertGreater(missing_ratio, 0.0)
        self.assertLessEqual(missing_ratio, 1.0)
