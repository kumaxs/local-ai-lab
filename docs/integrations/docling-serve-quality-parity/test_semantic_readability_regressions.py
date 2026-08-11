from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import semantic_reflow  # noqa: E402


class _TableSource:
    def logical_lines(self, _prov, *, padding=0.0):
        return []

    def page_size(self, _page_no):
        return 612.0, 792.0

    def text(self, _prov, *, layout=False, padding=0.0):
        return ""


class _CidSource:
    _pypdf = None
    _math_aware_diagnostics = {}

    @staticmethod
    def text(_prov, *, layout=False, padding=0.0):
        return "The unknown symbol (cid:52) is source-authoritative."

    @staticmethod
    def _math_diagnostic_key(_prov):
        return None

    @staticmethod
    def math_aware_text(_prov, value):
        return value

    @staticmethod
    def inline_math_evidence(_prov):
        return False


class _EmptyPhysicalSource(_CidSource):
    @staticmethod
    def text(_prov, *, layout=False, padding=0.0):
        return ""


def _table_item(cells, *, rows, cols):
    return semantic_reflow.FlowItem(
        kind="table",
        node={
            "label": "table",
            "data": {
                "num_rows": rows,
                "num_cols": cols,
                "table_cells": cells,
            },
        },
        rank=1.0,
        page_no=1,
        bbox={"l": 0.0, "r": 300.0, "t": 700.0, "b": 400.0},
        prov={
            "page_no": 1,
            "bbox": {
                "l": 0.0,
                "r": 300.0,
                "t": 700.0,
                "b": 400.0,
                "coord_origin": "BOTTOMLEFT",
            },
        },
        source_text="",
    )


class SemanticReadabilityRegressionTests(unittest.TestCase):
    def test_low_similarity_cross_column_crop_keeps_clean_paragraph(self):
        clean = (
            "This paragraph explains the estimator and remains readable "
            "across the page."
        )
        cross_column = "T\nh\ne\nA\nr\nbitrary\ncolumn\ntext"
        selected, diagnostic = semantic_reflow._choose_readable_source_text(
            clean,
            cross_column,
        )

        self.assertEqual(selected, clean)
        self.assertEqual(diagnostic["selected"], "clean_slice")
        self.assertIn("physical", diagnostic["reason"])

    def test_low_similarity_uses_physical_only_when_materially_clearer(self):
        selected, diagnostic = semantic_reflow._choose_readable_source_text(
            "t\nh\ni\ns\ni\ns\na\nb\na\nd\ns\np\na\nn",
            "The physical PDF paragraph is complete and readable.",
        )

        self.assertEqual(
            selected,
            "The physical PDF paragraph is complete and readable.",
        )
        self.assertEqual(
            diagnostic["reason"],
            "physical_source_materially_more_readable",
        )

    def test_short_clean_span_does_not_absorb_neighboring_prose(self):
        selected, diagnostic = semantic_reflow._choose_readable_source_text(
            "x",
            "The physical crop also contains the neighboring paragraph.",
        )

        self.assertEqual(selected, "x")
        self.assertEqual(diagnostic["reason"], "short_clean_slice_preserved")

    def test_short_multi_token_math_span_does_not_absorb_neighboring_prose(self):
        selected, diagnostic = semantic_reflow._choose_readable_source_text(
            "x\ny\nz\nq\nr\ns\nu\nv",
            "The physical crop also contains the neighboring paragraph.",
        )

        self.assertEqual(selected, "x\ny\nz\nq\nr\ns\nu\nv")
        self.assertEqual(diagnostic["reason"], "short_clean_slice_preserved")

    def test_long_clean_span_rejects_two_character_cross_column_runs(self):
        clean = (
            "The complete character span explains the probability model, its "
            "normalization denominator, and the resulting training objective. "
        ) * 5
        fragmented = "\n".join(
            ["Th", "e ", "pr", "ob", "ab", "il", "it", "y "] * 14
        )

        selected, diagnostic = semantic_reflow._choose_readable_source_text(
            clean,
            fragmented,
        )

        self.assertEqual(selected, clean.strip())
        self.assertEqual(diagnostic["selected"], "clean_slice")
        self.assertEqual(
            diagnostic["reason"],
            "physical_source_rejected_cross_column_singletons",
        )
        self.assertTrue(diagnostic["physical"]["cross_column_suspect"])

    def test_short_unknown_cid_span_does_not_absorb_neighboring_prose(self):
        selected, diagnostic = semantic_reflow._choose_readable_source_text(
            "(cid:52)",
            "Neighboring prose appears before the unknown (cid:52) glyph.",
        )

        self.assertEqual(selected, "(cid:52)")
        self.assertEqual(diagnostic["reason"], "short_clean_slice_preserved")

    def test_merged_header_preserves_html_spans_and_marks_markdown_degradation(self):
        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 2,
                    "text": "Model and score",
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "A",
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "0.9",
                },
            ],
            rows=2,
            cols=2,
        )
        html_text, markdown, _counts = semantic_reflow._render(
            [item],
            {"name": "Merged table"},
            _TableSource(),
        )

        self.assertIn('<th colspan="2">Model and score</th>', html_text)
        self.assertIn(
            "machine-surface: degraded; merged table cells require",
            markdown,
        )

    def test_table_header_roles_render_multirow_columns_row_headers_and_sections(self):
        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 3,
                    "text": "Metrics",
                    "column_header": True,
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Model",
                    "column_header": True,
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "Score",
                    "column_header": True,
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 2,
                    "end_col_offset_idx": 3,
                    "text": "Time",
                    "column_header": True,
                },
                {
                    "start_row_offset_idx": 2,
                    "end_row_offset_idx": 3,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 3,
                    "text": "Group A",
                    "row_section": True,
                },
                {
                    "start_row_offset_idx": 3,
                    "end_row_offset_idx": 4,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Model-A",
                    "row_header": True,
                },
                {
                    "start_row_offset_idx": 3,
                    "end_row_offset_idx": 4,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "0.9",
                },
                {
                    "start_row_offset_idx": 3,
                    "end_row_offset_idx": 4,
                    "start_col_offset_idx": 2,
                    "end_col_offset_idx": 3,
                    "text": "12s",
                },
            ],
            rows=4,
            cols=3,
        )

        grid, header_rows, placements = semantic_reflow._table_cell_layout(
            _TableSource(),
            item,
        )
        self.assertEqual(header_rows, 2)
        self.assertEqual(len(grid), 4)
        by_text = {placement["text"]: placement for placement in placements}
        self.assertEqual(by_text["Metrics"]["header_role"], "col")
        self.assertEqual(by_text["Group A"]["header_role"], "rowgroup")
        self.assertEqual(by_text["Model-A"]["header_role"], "row")

        html_text, markdown, _counts = semantic_reflow._render(
            [item],
            {"name": "Role-aware table"},
            _TableSource(),
        )

        self.assertIn('<th scope="col" colspan="3">Metrics</th>', html_text)
        self.assertIn('<th scope="col">Model</th>', html_text)
        self.assertIn('<th scope="col">Score</th>', html_text)
        self.assertIn('<th scope="rowgroup" colspan="3">Group A</th>', html_text)
        self.assertIn('<th scope="row">Model-A</th>', html_text)
        self.assertIn("machine-surface: degraded; merged table cells require", markdown)

    def test_explicit_table_roles_do_not_promote_unflagged_cells_to_headers(self):
        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Header",
                    "column_header": True,
                },
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "Unflagged top cell",
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Row label",
                    "row_header": True,
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "Body",
                },
            ],
            rows=2,
            cols=2,
        )

        html_text, _markdown, _counts = semantic_reflow._render(
            [item],
            {"name": "Sparse role table"},
            _TableSource(),
        )

        self.assertIn('<th scope="col">Header</th>', html_text)
        self.assertIn("<td>Unflagged top cell</td>", html_text)
        self.assertIn('<th scope="row">Row label</th>', html_text)
        self.assertIn("<td>Body</td>", html_text)

    def test_rowspan_covering_a_whole_row_does_not_emit_an_empty_tr(self):
        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 2,
                    "text": "Shared heading",
                    "column_header": True,
                }
            ],
            rows=2,
            cols=2,
        )

        html_text, _markdown, _counts = semantic_reflow._render(
            [item],
            {"name": "Rowspan table"},
            _TableSource(),
        )

        self.assertEqual(html_text.count("<tr>"), 1)
        self.assertEqual(html_text.count("</tr>"), 1)
        self.assertIn(
            '<th scope="col" rowspan="2" colspan="2">Shared heading</th>',
            html_text,
        )

    def test_sparse_merged_header_keeps_leading_blank_html_cell(self):
        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 3,
                    "text": "Dev Set",
                    "column_header": True,
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Task",
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "EM",
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 2,
                    "end_col_offset_idx": 3,
                    "text": "F1",
                },
            ],
            rows=2,
            cols=3,
        )

        html_text, _markdown, _counts = semantic_reflow._render(
            [item],
            {"name": "Sparse leading cell"},
            _TableSource(),
        )

        first_row = re.search(r"<tr>(.*?)</tr>", html_text, flags=re.S)
        self.assertIsNotNone(first_row)
        assert first_row is not None
        self.assertRegex(first_row.group(1), r"<(?:td|th)[^>]*></(?:td|th)>")
        self.assertIn('colspan="2">Dev Set</th>', first_row.group(1))

    def test_multiline_two_by_two_stays_declared_grid(self):
        class MultilineSource(_TableSource):
            def logical_lines(self, prov, *, padding=0.0):
                return (
                    ["left one", "left two", "left three"]
                    if prov["bbox"]["l"] < 100
                    else ["right one", "right two", "right three"]
                )

        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Left",
                    "bbox": {"l": 0, "r": 90, "t": 700, "b": 600},
                },
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "Right",
                    "bbox": {"l": 100, "r": 190, "t": 700, "b": 600},
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "A",
                    "bbox": {"l": 0, "r": 90, "t": 590, "b": 500},
                },
                {
                    "start_row_offset_idx": 1,
                    "end_row_offset_idx": 2,
                    "start_col_offset_idx": 1,
                    "end_col_offset_idx": 2,
                    "text": "B",
                    "bbox": {"l": 100, "r": 190, "t": 590, "b": 500},
                },
            ],
            rows=2,
            cols=2,
        )

        grid, header_rows = semantic_reflow._table_grid(MultilineSource(), item)

        self.assertEqual(header_rows, 1)
        self.assertEqual(len(grid), 2)
        self.assertEqual(len(grid[0]), 2)
        self.assertEqual(grid[0][0], "Left")
        self.assertEqual(grid[0][1], "Right")
        self.assertEqual(grid[1][0], "A")
        self.assertEqual(grid[1][1], "B")

    def test_table_cell_layout_does_not_override_declared_text_without_safe_equivalence(self):
        class EquivalenceAwareSource(_TableSource):
            def logical_lines(self, prov, *, padding=0.0):
                if prov["bbox"]["l"] < 100:
                    return ["Mouse enhancers", "Coding vs. intergenic"]
                return ["Right"]

        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Mouse enhancers",
                    "bbox": {"l": 0, "r": 90, "t": 700, "b": 600},
                },
            ],
            rows=1,
            cols=1,
        )

        grid, header_rows = semantic_reflow._table_grid(
            EquivalenceAwareSource(),
            item,
        )

        self.assertEqual(header_rows, 1)
        self.assertEqual(grid, [["Mouse enhancers"]])

    def test_table_cell_layout_allows_equivalent_multiline_override(self):
        class EquivalenceAwareSource(_TableSource):
            def logical_lines(self, prov, *, padding=0.0):
                return ["Image/MNIST", "C2/C4"]

        item = _table_item(
            [
                {
                    "start_row_offset_idx": 0,
                    "end_row_offset_idx": 1,
                    "start_col_offset_idx": 0,
                    "end_col_offset_idx": 1,
                    "text": "Image/MNIST C 2 / C 4",
                    "bbox": {"l": 0, "r": 190, "t": 700, "b": 600},
                },
            ],
            rows=1,
            cols=1,
        )

        grid, header_rows = semantic_reflow._table_grid(
            EquivalenceAwareSource(),
            item,
        )

        self.assertEqual(header_rows, 1)
        self.assertEqual(grid[0][0], "Image/MNIST\nC2/C4")

    def test_unknown_cid_is_preserved_only_in_source_backed_mode_and_gets_crop(self):
        self.assertEqual(
            semantic_reflow._clean_glyph_text(
                "(cid:52)",
                preserve_unknown_cid=True,
            ),
            "(cid:52)",
        )
        node = {
            "label": "text",
            "text": "The unknown symbol (cid:52) is source-authoritative.",
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 80.0,
                        "r": 320.0,
                        "t": 700.0,
                        "b": 680.0,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ],
        }
        document = {
            "texts": [node],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }

        items = semantic_reflow._collect_items(document, _CidSource())

        self.assertEqual(len(items), 1)
        self.assertIn("(cid:52)", items[0].source_text)
        self.assertTrue(items[0].inline_math_source_anchor)
        self.assertEqual(
            items[0].inline_math_unresolved_regions[0]["reason"],
            "unknown_cid_requires_source_crop",
        )

    def test_empty_physical_source_keeps_valid_docling_charspan(self):
        clean = "A valid Docling span survives an empty PDF text layer."
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": clean,
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, len(clean)],
                            "bbox": {
                                "l": 80.0,
                                "r": 320.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }

        items = semantic_reflow._collect_items(document, _EmptyPhysicalSource())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_text, clean)
        self.assertEqual(
            items[0].source_readability_diagnostic["reason"],
            "physical_source_unavailable",
        )

    def test_tiny_formula_source_mismatch_is_dropped_but_matching_variable_survives(self):
        class TinyFormulaSource(_TableSource):
            def text(self, prov, *, layout=False, padding=0.0):
                return "]" if prov.get("page_no") == 1 else "x"

        def formula(text, page_no):
            return {
                "label": "formula",
                "text": text,
                "prov": [
                    {
                        "page_no": page_no,
                        "bbox": {
                            "l": 10.0,
                            "r": 13.0,
                            "t": 30.0,
                            "b": 20.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }

        dropped = []
        document = {
            "texts": [formula("I", 1), formula("x", 2)],
            "body": {
                "children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]
            },
        }

        items = semantic_reflow._collect_items(
            document,
            TinyFormulaSource(),
            dropped_formula_artifacts=dropped,
        )

        self.assertEqual(
            ["x"],
            [item.node["text"] for item in items if item.kind == "formula"],
        )
        self.assertEqual(["compact_formula_fragment"], [item["reason"] for item in dropped])

    def test_overlapping_script_fragment_is_not_an_allowed_compact_dropout(self):
        class ParentCropSource(_TableSource):
            def text(self, prov, *, layout=False, padding=0.0):
                return "x"

        def formula(text):
            return {
                "label": "formula",
                "text": text,
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 10.0,
                            "r": 14.0,
                            "t": 30.0,
                            "b": 20.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }

        dropped = []
        document = {
            "texts": [formula("x"), formula("^{ -1 }")],
            "body": {
                "children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]
            },
        }

        items = semantic_reflow._collect_items(
            document,
            ParentCropSource(),
            dropped_formula_artifacts=dropped,
        )

        self.assertEqual(
            ["x"],
            [item.node["text"] for item in items if item.kind == "formula"],
        )
        self.assertEqual("unmerged_formula_script", dropped[0]["reason"])

    def test_repaired_only_inline_math_has_no_source_region(self):
        item = semantic_reflow.FlowItem(
            kind="text",
            node={"label": "text"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 80.0},
            prov={"page_no": 1, "bbox": {}},
            source_text="The repaired notation is readable.",
            inline_math_repaired=True,
            inline_math_source_anchor="inline-math-text-1",
        )

        self.assertEqual(
            semantic_reflow._inline_math_source_region_records(
                item,
                part_index=0,
            ),
            [],
        )

    def test_unresolved_inline_math_uses_tight_bbox_or_explicit_fallback(self):
        tight = semantic_reflow.FlowItem(
            kind="text",
            node={"label": "text"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 80.0},
            prov={"page_no": 1, "bbox": {}},
            source_text="A span needs review.",
            inline_math_source_anchor="inline-math-text-2",
            inline_math_unresolved_regions=[
                {
                    "bbox": {"l": 40.0, "r": 55.0, "t": 98.0, "b": 84.0},
                    "reason": "fraction_span",
                }
            ],
        )
        fallback = semantic_reflow.FlowItem(
            kind="text",
            node={"label": "text"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 80.0},
            prov={"page_no": 1, "bbox": {}},
            source_text="A span needs review.",
            inline_math_source_anchor="inline-math-text-3",
            inline_math_unresolved_regions=[{"reason": "missing_geometry"}],
        )

        tight_records = semantic_reflow._inline_math_source_region_records(
            tight,
            part_index=0,
        )
        fallback_records = semantic_reflow._inline_math_source_region_records(
            fallback,
            part_index=0,
        )

        self.assertEqual(tight_records[0]["bbox"]["l"], 40.0)
        self.assertFalse(tight_records[0]["fallback_whole_paragraph"])
        self.assertEqual(fallback_records[0]["bbox"], fallback.bbox)
        self.assertTrue(fallback_records[0]["fallback_whole_paragraph"])

    def test_legacy_formula_labels_keep_decimal_equation_number_separate_from_raw_ordinal(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "document.html").write_text(
                (
                    "<html><head></head><body>"
                    '<div class="docling-formula-second-pass" '
                    'data-formula-index="A.15">'
                    '<pre class="docling-formula-tex">x=y\\quad(A.15)</pre>'
                    "</div>"
                    '<div class="docling-formula-second-pass" '
                    'data-formula-index="2.1">'
                    '<pre class="docling-formula-tex">a=b\\quad(2.1)</pre>'
                    "</div></body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$x=y\\quad(A.15)$$\n\n$$a=b\\quad(2.1)$$\n",
                encoding="utf-8",
            )

            result = semantic_reflow._normalize_legacy_formula_surfaces(output_dir)
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["equation_numbers"], ["A.15", "2.1"])
        self.assertIn('data-formula-index="1"', html_text)
        self.assertIn('data-formula-index="2"', html_text)
        self.assertNotIn('data-formula-index="A.15"', html_text)
        self.assertIn('<span class="equation-number">(A.15)</span>', html_text)
        self.assertIn('<span class="equation-number">(2.1)</span>', html_text)
        self.assertIn("source-formula-anchor:1", html_text)
        self.assertIn("source-formula-anchor:2", html_text)
        self.assertIn(r"\tag{A.15}", markdown_text)
        self.assertIn(r"\tag{2.1}", markdown_text)

    def test_formula_label_removal_drops_only_a_dangling_format_wrapper(self):
        class FormulaSource(_TableSource):
            def equation_number(self, _prov):
                return 1

        item = semantic_reflow.FlowItem(
            kind="formula",
            node={"text": r"\mathbf A=\mathbf X D\mathbf Y^{\top},\quad\mathbf(1)"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 20.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {}},
        )

        tex, number = semantic_reflow._formula_tex(item, FormulaSource())

        self.assertEqual(number, 1)
        self.assertEqual(tex, r"\mathbf A=\mathbf X D\mathbf Y^{\top},")
        self.assertIsNotNone(semantic_reflow._formula_mathml(tex))

    def test_formula_label_removal_preserves_numeric_body_parentheses(self):
        class FormulaSource(_TableSource):
            def equation_number(self, _prov):
                return None

        def formula(text):
            return semantic_reflow.FlowItem(
                kind="formula",
                node={"text": text},
                rank=1.0,
                page_no=1,
                bbox={"l": 0.0, "r": 100.0, "t": 20.0, "b": 0.0},
                prov={"page_no": 1, "bbox": {}},
            )

        body_tex, body_number = semantic_reflow._formula_tex(
            formula(r"f(1)"), FormulaSource()
        )
        labelled_tex, labelled_number = semantic_reflow._formula_tex(
            formula(r"x+y\quad(12)"), FormulaSource()
        )

        self.assertIsNone(body_number)
        self.assertEqual(r"f(1)", body_tex)
        self.assertIsNone(labelled_number)
        self.assertEqual(r"x+y", labelled_tex)

    def test_fraction_span_excludes_adjacent_script_layers(self):
        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = SimpleNamespace(
            pages=[
                SimpleNamespace(
                    height=100.0,
                    lines=[{"x0": 40.0, "x1": 78.0, "top": 48.0, "bottom": 48.0}],
                )
            ]
        )
        runs = [
            {
                "text": "P",
                "size": 8.0,
                "fontname": "CMMI8",
                "bbox": {"l": 32.0, "r": 39.0, "t": 58.0, "b": 50.0},
            },
            {
                "text": "e",
                "size": 8.0,
                "fontname": "CMMI8",
                "bbox": {"l": 45.0, "r": 49.0, "t": 56.0, "b": 50.0},
            },
            {
                "text": "S",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 49.4, "r": 53.0, "t": 60.0, "b": 54.0},
            },
            {
                "text": "j",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 53.2, "r": 56.0, "t": 47.0, "b": 41.0},
            },
            {
                "text": "C_b",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 60.0, "r": 65.0, "t": 70.0, "b": 64.0},
            },
            {
                "text": "P_n",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 60.0, "r": 65.0, "t": 36.0, "b": 30.0},
            },
        ]

        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {
                    "l": 30.0,
                    "r": 85.0,
                    "t": 80.0,
                    "b": 20.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            },
            runs,
        )

        self.assertEqual(len(spans), 1)
        self.assertIn("P", spans[0]["source_text"])
        self.assertIn("j", spans[0]["source_text"])
        self.assertNotIn("C_b", spans[0]["source_text"])
        self.assertNotIn("P_n", spans[0]["source_text"])

    def test_fraction_visual_crop_does_not_block_adjacent_script_repair(self):
        class FakePage:
            height = 100.0
            lines = [
                {
                    "x0": 40.0,
                    "x1": 78.0,
                    "top": 48.0,
                    "bottom": 48.0,
                }
            ]

        class FakePdf:
            pages = [FakePage()]

        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = FakePdf()
        runs = [
            # This C_b cluster is close enough to be useful visual context,
            # but its right edge is just outside the fraction bar.  It must
            # not be suppressed merely because the source crop includes it.
            {
                "text": "C",
                "size": 10.0,
                "fontname": "CMMI10",
                "bbox": {"l": 31.0, "r": 36.0, "t": 60.0, "b": 50.0},
            },
            {
                "text": "b",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 36.2, "r": 39.0, "t": 55.0, "b": 49.0},
            },
            {
                "text": "P",
                "size": 8.0,
                "fontname": "CMMI8",
                "bbox": {"l": 32.0, "r": 39.0, "t": 58.0, "b": 50.0},
            },
            {
                "text": "e",
                "size": 8.0,
                "fontname": "CMMI8",
                "bbox": {"l": 45.0, "r": 49.0, "t": 56.0, "b": 50.0},
            },
            {
                "text": "S",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 49.4, "r": 53.0, "t": 60.0, "b": 54.0},
            },
            {
                "text": "j",
                "size": 6.0,
                "fontname": "CMMI6",
                "bbox": {"l": 53.2, "r": 56.0, "t": 47.0, "b": 41.0},
            },
        ]
        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {
                    "l": 25.0,
                    "r": 85.0,
                    "t": 70.0,
                    "b": 25.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            },
            runs,
        )

        self.assertEqual(len(spans), 1)
        visual_bbox = spans[0]["bbox"]
        repair_bbox = spans[0]["repair_bbox"]
        self.assertLess(visual_bbox["l"], repair_bbox["l"])

        diagnostics = []
        repaired, repaired_names, unresolved = semantic_reflow._inline_geometry_repair(
            "C b P e S j",
            runs,
            math_font_evidence=lambda run: "CM" in str(run.get("fontname") or ""),
            cluster_diagnostics=diagnostics,
            blocked_bboxes=[repair_bbox],
        )
        self.assertEqual(repaired, "C_b P e S j")
        self.assertIn("geometry_script-C-sub-b-run0", repaired_names)
        self.assertEqual(unresolved, set())
        self.assertFalse(diagnostics[0].get("suppressed"))

    def test_chunk_local_page_provenance_is_normalized_to_original_pdf_page(self):
        document = {
            "schema_name": "local_ai_lab_docling_serve_chunked",
            "chunks": [
                {
                    "page_range": [3, 3],
                    "document": {
                        "pages": {"1": {"size": {"width": 612, "height": 792}}},
                        "texts": [
                            {
                                "label": "text",
                                "self_ref": "#/texts/0",
                                "text": "This text belongs to physical page three.",
                                "prov": [
                                    {
                                        "page_no": 1,
                                        "bbox": {
                                            "l": 10,
                                            "r": 100,
                                            "t": 100,
                                            "b": 80,
                                            "coord_origin": "BOTTOMLEFT",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }

        normalized, parts = semantic_reflow._document_parts_with_global_pages(
            document
        )
        part_index, part = parts[0]
        node = part["texts"][0]

        self.assertEqual(part_index, 0)
        self.assertEqual(set(part["pages"]), {"3"})
        self.assertEqual(node["prov"][0]["page_no"], 3)
        self.assertEqual(node["_local_ai_lab_chunk_part_index"], 0)
        self.assertEqual(
            normalized["chunks"][0]["document"]["texts"][0]["prov"][0][
                "page_no"
            ],
            3,
        )
        # The caller's response remains immutable for status/debug retention.
        self.assertEqual(document["chunks"][0]["document"]["texts"][0]["prov"][0]["page_no"], 1)

    def test_chunk_global_page_provenance_is_not_double_offset(self):
        document = {
            "schema_name": "local_ai_lab_docling_serve_chunked",
            "chunks": [
                {
                    "page_range": [9, 16],
                    "document": {
                        "pages": {"9": {"size": {"width": 612, "height": 792}}},
                        "tables": [
                            {
                                "label": "table",
                                "self_ref": "#/tables/0",
                                "prov": [
                                    {
                                        "page_no": 9,
                                        "bbox": {
                                            "l": 10,
                                            "r": 200,
                                            "t": 300,
                                            "b": 100,
                                            "coord_origin": "BOTTOMLEFT",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }

        _normalized, parts = semantic_reflow._document_parts_with_global_pages(
            document
        )

        self.assertEqual(set(parts[0][1]["pages"]), {"9"})
        self.assertEqual(parts[0][1]["tables"][0]["prov"][0]["page_no"], 9)

    def test_chunk_qualified_structure_refs_disambiguate_repeated_self_refs(self):
        document = {
            "chunks": [
                {
                    "page_range": [1, 1],
                    "document": {
                        "pages": {"1": {"size": {"width": 612, "height": 792}}},
                        "tables": [{"label": "table", "self_ref": "#/tables/0"}],
                    },
                },
                {
                    "page_range": [2, 2],
                    "document": {
                        "pages": {"1": {"size": {"width": 612, "height": 792}}},
                        "tables": [{"label": "table", "self_ref": "#/tables/0"}],
                    },
                },
            ]
        }
        _normalized, parts = semantic_reflow._document_parts_with_global_pages(
            document
        )

        refs = []
        for part_index, part in parts:
            item = semantic_reflow.FlowItem(
                kind="table",
                node=part["tables"][0],
                rank=1.0,
                page_no=part_index + 1,
                bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
                prov={},
                collection_index=0,
            )
            refs.append(semantic_reflow._structure_block_source_ref(item))

        self.assertEqual(refs, ["chunk:0:#/tables/0", "chunk:1:#/tables/0"])
        plain_item = semantic_reflow.FlowItem(
            kind="table",
            node={"label": "table", "self_ref": "#/tables/0"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 1.0, "t": 1.0, "b": 0.0},
            prov={},
            collection_index=0,
        )
        self.assertEqual(
            semantic_reflow._structure_block_source_ref(plain_item),
            "#/tables/0",
        )

    def test_cjk_primary_surface_counts_cover_documents_with_many_tables_and_formulas(self):
        class CJKSource:
            def __init__(self, _pdf_path: Path):
                pass

            def language_profile(self, *, page_limit: int = 3) -> dict[str, int]:
                del page_limit
                return {
                    "characters": 1400,
                    "cjk_characters": 200,
                    "latin_characters": 800,
                }

            def close(self) -> None:
                return None

            def text(self, prov: dict[str, Any], *, layout: bool = False, padding: float = 0.0) -> str:
                del prov, layout, padding
                return ""

        original_source_reader = semantic_reflow.SourceReader
        original_normalizer = semantic_reflow._normalize_legacy_formula_surfaces
        semantic_reflow.SourceReader = CJKSource
        try:
            with tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory)
                (output_dir / "document.html").write_text(
                    "<html><body>中文正文保持不变。</body></html>",
                    encoding="utf-8",
                )
                (output_dir / "document.md").write_text(
                    "中文正文保持不变。\n",
                    encoding="utf-8",
                )

                table_count = 6
                formula_count = 24
                document = {
                    "tables": [{"label": "table"} for _ in range(table_count)],
                    "texts": [
                        {
                            "label": "text",
                            "text": "中文正文保持不变。",
                            "prov": [
                                {
                                    "page_no": 1,
                                    "bbox": {
                                        "l": 50.0,
                                        "r": 150.0,
                                        "t": 780.0,
                                        "b": 770.0,
                                        "coord_origin": "BOTTOMLEFT",
                                    },
                                }
                            ],
                        }
                    ],
                    "headings": [
                        {"label": "heading", "text": "附录标题", "level": 2},
                    ],
                    "code": [
                        {"label": "code", "text": "Algorithm 2"},
                        {"label": "code", "text": "print('baseline')"},
                    ],
                    "pictures": [{"label": "picture"}],
                    "formulas": [
                        {"label": "formula", "text": f"x = y_{index}"}
                        for index in range(formula_count)
                    ],
                }
                expected_counts = semantic_reflow._primary_surface_count_from_document(
                    document
                )

                status: dict[str, Any] = {
                    "ok": True,
                    "success_class": "success",
                    "warnings": [],
                    "quality_signals": {},
                }
                metadata: dict[str, Any] = {}

                result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    Path(directory) / "paper.pdf",
                    metadata,
                    status,
                )

                self.assertTrue(result["ok"])
                self.assertIn("counts", result)
                self.assertEqual(result["counts"], expected_counts)
                self.assertEqual(result["counts"]["algorithms"], 1)
                self.assertEqual(result["counts"]["code_blocks"], 1)
                self.assertEqual(result["counts"]["pictures"], 1)
                self.assertEqual(result["counts"]["headings"], 1)
                self.assertEqual(result["counts"]["text"], 1)
                self.assertEqual(result["counts"]["tables"], table_count)
                self.assertEqual(result["counts"]["formulas"], formula_count)
                self.assertEqual(result["counts"]["inline_math_repairs"], 0)
                self.assertEqual(metadata["primary_surface"], result)
                self.assertEqual(
                    status["quality_signals"].get("primary_surface"),
                    result,
                )
                self.assertEqual(
                    status["quality_signals"]["primary_surface"]["counts"],
                    expected_counts,
                )
        finally:
            semantic_reflow.SourceReader = original_source_reader
            semantic_reflow._normalize_legacy_formula_surfaces = original_normalizer

    def test_cjk_primary_surface_counts_present_when_formula_normalization_fails(self):
        class CJKSource:
            def __init__(self, _pdf_path: Path):
                pass

            def language_profile(self, *, page_limit: int = 3) -> dict[str, int]:
                del page_limit
                return {
                    "characters": 1400,
                    "cjk_characters": 200,
                    "latin_characters": 800,
                }

            def close(self) -> None:
                return None

            def text(self, prov: dict[str, Any], *, layout: bool = False, padding: float = 0.0) -> str:
                del prov, layout, padding
                return ""

        def failing_normalization(_output_dir: Path) -> dict[str, Any]:
            raise RuntimeError("reflow regression")

        original_source_reader = semantic_reflow.SourceReader
        original_normalizer = semantic_reflow._normalize_legacy_formula_surfaces
        semantic_reflow.SourceReader = CJKSource
        semantic_reflow._normalize_legacy_formula_surfaces = failing_normalization
        try:
            with tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory)
                (output_dir / "document.html").write_text(
                    "<html><body>中文正文保持不变。</body></html>",
                    encoding="utf-8",
                )
                (output_dir / "document.md").write_text(
                    "中文正文保持不变。\n",
                    encoding="utf-8",
                )

                table_count = 6
                formula_count = 24
                document = {
                    "tables": [{"label": "table"} for _ in range(table_count)],
                    "texts": [
                        {
                            "label": "text",
                            "text": "中文正文保持不变。",
                            "prov": [
                                {
                                    "page_no": 1,
                                    "bbox": {
                                        "l": 50.0,
                                        "r": 150.0,
                                        "t": 780.0,
                                        "b": 770.0,
                                        "coord_origin": "BOTTOMLEFT",
                                    },
                                }
                            ],
                        }
                    ],
                    "headings": [
                        {"label": "heading", "text": "附录标题", "level": 2},
                    ],
                    "code": [
                        {"label": "code", "text": "Algorithm 2"},
                        {"label": "code", "text": "print('baseline')"},
                    ],
                    "pictures": [{"label": "picture"}],
                    "formulas": [
                        {"label": "formula", "text": f"x = y_{index}"}
                        for index in range(formula_count)
                    ],
                }
                expected_counts = semantic_reflow._primary_surface_count_from_document(
                    document
                )

                status: dict[str, Any] = {
                    "ok": True,
                    "success_class": "success",
                    "warnings": [],
                    "quality_signals": {},
                }
                metadata: dict[str, Any] = {}

                result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    Path(directory) / "paper.pdf",
                    metadata,
                    status,
                )

                self.assertFalse(result["applied"])
                self.assertTrue(result["ok"])
                self.assertIn("counts", result)
                self.assertEqual(result["counts"], expected_counts)
                self.assertEqual(result["counts"]["algorithms"], 1)
                self.assertEqual(result["counts"]["code_blocks"], 1)
                self.assertEqual(result["counts"]["pictures"], 1)
                self.assertEqual(result["counts"]["headings"], 1)
                self.assertEqual(result["counts"]["text"], 1)
                self.assertEqual(result["counts"]["tables"], table_count)
                self.assertEqual(result["counts"]["formulas"], formula_count)
                self.assertTrue(
                    any(
                        warning.startswith(
                            "cjk_machine_formula_normalization_unavailable:"
                        )
                        for warning in status["warnings"]
                    )
                )
                self.assertEqual(
                    status["quality_signals"].get("primary_surface"),
                    result,
                )
                self.assertEqual(
                    status["quality_signals"]["primary_surface"]["counts"],
                    expected_counts,
                )
        finally:
            semantic_reflow.SourceReader = original_source_reader
            semantic_reflow._normalize_legacy_formula_surfaces = original_normalizer

    def test_cjk_legacy_formula_failure_uses_source_backed_semantic_reflow(self):
        class CompleteCJKSource:
            _pypdf = None
            _math_aware_diagnostics: dict[Any, Any] = {}

            def __init__(self, _pdf_path: Path):
                self.closed = False

            def language_profile(self, *, page_limit: int = 3) -> dict[str, int]:
                del page_limit
                return {
                    "characters": 1400,
                    "cjk_characters": 400,
                    "latin_characters": 600,
                }

            def close(self) -> None:
                self.closed = True

            def text(
                self,
                _prov: dict[str, Any],
                *,
                layout: bool = False,
                padding: float = 0.0,
            ) -> str:
                del layout, padding
                return ""

            def math_aware_text(self, _prov: dict[str, Any], value: str) -> str:
                return value

            def inline_math_evidence(self, _prov: dict[str, Any]) -> bool:
                return False

            def equation_number(self, _prov: dict[str, Any]) -> int | None:
                return None

            def page_size(self, _page_no: int) -> tuple[float, float]:
                return 612.0, 792.0

            def logical_lines(
                self,
                _prov: dict[str, Any],
                *,
                padding: float = 0.0,
            ) -> list[dict[str, Any]]:
                del padding
                return []

            def _pypdfium_characters(
                self,
                _page_no: int,
                _bbox: dict[str, Any],
            ) -> list[dict[str, Any]]:
                return []

        def failing_normalization(_output_dir: Path) -> dict[str, Any]:
            raise RuntimeError("legacy surface omitted formulas")

        original_source_reader = semantic_reflow.SourceReader
        original_normalizer = semantic_reflow._normalize_legacy_formula_surfaces
        semantic_reflow.SourceReader = CompleteCJKSource
        semantic_reflow._normalize_legacy_formula_surfaces = failing_normalization
        try:
            with tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory)
                (output_dir / "document.html").write_text(
                    "<html><head></head><body><p>中文正文。</p></body></html>",
                    encoding="utf-8",
                )
                (output_dir / "document.md").write_text(
                    "中文正文。\n",
                    encoding="utf-8",
                )
                bbox = {
                    "l": 50.0,
                    "r": 250.0,
                    "t": 700.0,
                    "b": 680.0,
                    "coord_origin": "BOTTOMLEFT",
                }
                document = {
                    "name": "中文测试",
                    "body": {
                        "children": [
                            {"$ref": "#/texts/0"},
                            {"$ref": "#/texts/1"},
                            {"$ref": "#/texts/2"},
                        ]
                    },
                    "texts": [
                        {
                            "self_ref": "#/texts/0",
                            "label": "text",
                            "text": "中文正文。",
                            "prov": [{"page_no": 1, "bbox": bbox}],
                        },
                        {
                            "self_ref": "#/texts/1",
                            "label": "formula",
                            "text": "x=y",
                            "prov": [{"page_no": 1, "bbox": bbox}],
                        },
                        {
                            "self_ref": "#/texts/2",
                            "label": "formula",
                            "text": "( 2 )",
                            "prov": [{"page_no": 1, "bbox": bbox}],
                        },
                    ],
                    "tables": [],
                    "pictures": [],
                    "pages": {"1": {"size": {"width": 612, "height": 792}}},
                }
                metadata: dict[str, Any] = {}
                status: dict[str, Any] = {
                    "ok": True,
                    "success_class": "success",
                    "warnings": [],
                    "quality_signals": {},
                }

                result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    output_dir / "paper.pdf",
                    metadata,
                    status,
                )
                html_text = (output_dir / "document.html").read_text(
                    encoding="utf-8"
                )
                markdown_text = (output_dir / "document.md").read_text(
                    encoding="utf-8"
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "cjk_semantic_source_reflow_fallback")
            self.assertTrue(result["machine_surface_ok"])
            self.assertEqual(result["counts"]["formulas"], 1)
            self.assertEqual(
                result["dropped_formula_artifacts"][0]["raw_formula_index"],
                2,
            )
            self.assertEqual(
                result["dropped_formula_artifacts"][0]["reason"],
                "standalone_equation_number",
            )
            self.assertIn("source-formula-anchor:1", html_text)
            self.assertNotIn("source-formula-anchor:2", html_text)
            self.assertIn("source-formula-anchor:1", markdown_text)
            self.assertEqual(status["success_class"], "degraded_success")
            self.assertTrue(
                any(
                    warning.startswith("cjk_semantic_source_reflow_fallback:")
                    for warning in status["warnings"]
                )
            )
        finally:
            semantic_reflow.SourceReader = original_source_reader
            semantic_reflow._normalize_legacy_formula_surfaces = original_normalizer


if __name__ == "__main__":
    unittest.main()
