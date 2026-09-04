from __future__ import annotations

import copy
import os
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
    @staticmethod
    def _simple_fraction_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        runs = [
            {
                "text": "1",
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {"l": 10.0, "r": 13.0, "t": 60.0, "b": 55.0},
            },
            {
                "text": "N",
                "size": 7.0,
                "fontname": "CMMI7",
                "bbox": {"l": 9.0, "r": 14.0, "t": 45.0, "b": 40.0},
            },
        ]
        spans = [
            {
                "name": "geometry_math_span-fraction_rule-p1-occ0-x9",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "bbox": {"l": 8.0, "r": 15.0, "t": 61.0, "b": 39.0},
                "repair_bbox": {"l": 8.0, "r": 15.0, "t": 61.0, "b": 39.0},
                "source_text": "1N",
            }
        ]
        return runs, spans

    def _cross_row_hole_fixture(
        self,
        *,
        candidate_text: str = "2144 2764",
        target_text: str | None = None,
        row_centers: tuple[tuple[float, float], ...] | None = None,
        rows: int = 2,
        cols: int = 4,
        chunked: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        class HoleSource:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = False

            def lines(self, _prov: dict[str, Any], *, padding: float = 0.0) -> list[dict[str, Any]]:
                self.calls += 1
                return [
                    {"text": "2144", "top": 10.0, "bottom": 20.0, "x0": 61.0, "x1": 74.0},
                    {"text": "2764", "top": 30.0, "bottom": 40.0, "x0": 61.0, "x1": 74.0},
                ]

            def close(self) -> None:
                self.closed = True

        source = HoleSource()
        if row_centers is None:
            row_centers = tuple((15.0, 35.0) for _ in range(max(cols - 1, 0)))
        cells: list[dict[str, Any]] = []
        for row in range(rows):
            for col in range(cols):
                if row == 1 and col == 3 and target_text is None:
                    continue
                text = f"r{row}c{col}"
                top = 10.0 + row * 20.0
                bottom = 20.0 + row * 20.0
                if col < len(row_centers):
                    current_center, next_center = row_centers[col]
                    center = current_center if row == 0 else next_center
                    top, bottom = center - 5.0, center + 5.0
                if row == 0 and col == 3:
                    text = candidate_text
                    top, bottom = 10.0, 40.0
                if row == 1 and col == 3:
                    text = target_text or ""
                cells.append(
                    {
                        "start_row_offset_idx": row,
                        "end_row_offset_idx": row + 1,
                        "start_col_offset_idx": col,
                        "end_col_offset_idx": col + 1,
                        "text": text,
                        "bbox": {
                            "l": col * 20.0,
                            "r": col * 20.0 + 15.0,
                            "t": top,
                            "b": bottom,
                            "coord_origin": "TOPLEFT",
                        },
                    }
                )
        table = {
            "self_ref": "#/tables/0",
            "label": "table",
            "prov": [{"page_no": 1}],
            "data": {
                "num_rows": rows,
                "num_cols": cols,
                "table_cells": cells,
                "grid": [
                    [
                        (candidate_text if row == 0 and col == 3 else "" if row == 1 and col == 3 else f"r{row}c{col}")
                        for col in range(cols)
                    ]
                    for row in range(rows)
                ],
            },
        }
        document: dict[str, Any] = {
            "schema_name": "local_ai_lab_docling_serve_chunked" if chunked else "docling_document",
            "tables": [table],
        }
        if chunked:
            document["chunks"] = [{"page_range": [1, 1], "document": {"tables": [table]}}]
        return document, source

    def test_table_cross_row_hole_repair_splits_numeric_cell_and_updates_grid(self):
        document, source = self._cross_row_hole_fixture()
        original = copy.deepcopy(document)
        metadata: dict[str, Any] = {}
        status: dict[str, Any] = {"ok": True, "quality_signals": {}}
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(b"source")
            handle.flush()
            diagnostic = semantic_reflow.repair_table_cross_row_holes(
                document,
                Path(handle.name),
                metadata=metadata,
                status=status,
                source_reader=source,
            )
        self.assertTrue(diagnostic["applied"])
        self.assertEqual(1, diagnostic["accepted_count"])
        self.assertFalse(source.closed)
        data = document["tables"][0]["data"]
        by_slot = {
            (cell["start_row_offset_idx"], cell["start_col_offset_idx"]): cell["text"]
            for cell in data["table_cells"]
        }
        self.assertEqual("2144", by_slot[(0, 3)])
        self.assertEqual("2764", by_slot[(1, 3)])
        self.assertEqual("2144", data["grid"][0][3])
        self.assertEqual("2764", data["grid"][1][3])
        self.assertNotEqual(original, document)
        self.assertEqual(diagnostic, metadata["table_cross_row_hole_repair"])
        self.assertEqual(diagnostic, status["quality_signals"]["table_cross_row_hole_repair"])
        self.assertRegex(diagnostic["source_pdf_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("/private/", repr(diagnostic))

    def test_table_cross_row_hole_repair_normalizes_malformed_quality_signals(self):
        document, source = self._cross_row_hole_fixture()
        status: dict[str, Any] = {"ok": True, "quality_signals": []}
        diagnostic = semantic_reflow.repair_table_cross_row_holes(
            document,
            None,
            status=status,
            source_reader=source,
        )
        self.assertTrue(diagnostic["applied"])
        self.assertIsInstance(status["quality_signals"], dict)
        self.assertEqual(
            diagnostic,
            status["quality_signals"]["table_cross_row_hole_repair"],
        )
        slots = {
            (cell["start_row_offset_idx"], cell["start_col_offset_idx"]): cell["text"]
            for cell in document["tables"][0]["data"]["table_cells"]
        }
        self.assertEqual("2144", slots[(0, 3)])
        self.assertEqual("2764", slots[(1, 3)])

    def test_table_cross_row_hole_repair_rejects_non_lossless_source_text(self):
        document, source = self._cross_row_hole_fixture(candidate_text="2144 9999")
        original = copy.deepcopy(document)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(
            document,
            None,
            source_reader=source,
        )
        self.assertFalse(diagnostic["applied"])
        self.assertTrue(any(item["reason"] == "source_text_not_lossless" for item in diagnostic["rejected"]))
        self.assertEqual(original, document)

    def test_table_cross_row_hole_repair_updates_dictionary_grid_cells(self):
        document, source = self._cross_row_hole_fixture()
        grid = document["tables"][0]["data"]["grid"]
        grid[0][3] = {"text": "2144 2764", "start_row_offset_idx": 0, "start_col_offset_idx": 3}
        grid[1][3] = {"text": ""}
        diagnostic = semantic_reflow.repair_table_cross_row_holes(document, None, source_reader=source)
        self.assertTrue(diagnostic["applied"])
        grid = document["tables"][0]["data"]["grid"]
        self.assertEqual("2144", grid[0][3]["text"])
        self.assertEqual("2764", grid[1][3]["text"])
        self.assertEqual(1, grid[1][3]["start_row_offset_idx"])
        self.assertEqual(3, grid[1][3]["start_col_offset_idx"])
        self.assertEqual("TOPLEFT", grid[1][3]["bbox"]["coord_origin"])

    def test_table_cross_row_hole_repair_rejects_invalid_grid_geometry(self):
        cases = (
            (0, {"text": "2144 2764", "row_span": "garbage"}),
            (1, {"text": "", "start_row_offset_idx": 99}),
        )
        for grid_row, grid_value in cases:
            with self.subTest(grid_row=grid_row):
                document, source = self._cross_row_hole_fixture()
                document["tables"][0]["data"]["grid"][grid_row][3] = grid_value
                original = copy.deepcopy(document)
                diagnostic = semantic_reflow.repair_table_cross_row_holes(
                    document,
                    None,
                    source_reader=source,
                )
                self.assertFalse(diagnostic["applied"])
                self.assertEqual(original, document)
                self.assertTrue(
                    any("grid" in item["reason"] for item in diagnostic["rejected"])
                )

    def test_table_cross_row_hole_repair_rejects_fractional_cell_offsets(self):
        document, source = self._cross_row_hole_fixture()
        candidate = document["tables"][0]["data"]["table_cells"][3]
        candidate["start_row_offset_idx"] = 0.2
        candidate["end_row_offset_idx"] = 1.2
        original = copy.deepcopy(document)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(
            document,
            None,
            source_reader=source,
        )
        self.assertFalse(diagnostic["applied"])
        self.assertEqual(original, document)
        self.assertEqual(0, source.calls)

    @unittest.skipIf(os.name == "nt", "FIFO safety probe requires POSIX")
    def test_table_cross_row_hole_repair_rejects_non_regular_source(self):
        document, _source = self._cross_row_hole_fixture()
        original = copy.deepcopy(document)
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "source.pdf"
            os.mkfifo(fifo)
            diagnostic = semantic_reflow.repair_table_cross_row_holes(document, fifo)
        self.assertFalse(diagnostic["applied"])
        self.assertEqual("source_pdf_unavailable", diagnostic["reason"])
        self.assertEqual(original, document)

    def test_inline_fraction_rule_inserts_unique_division_boundary(self):
        runs, spans = self._simple_fraction_fixture()
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            "x = 1 N + r",
            runs,
            spans,
        )
        self.assertEqual("x = 1/N + r", repaired)
        self.assertEqual({spans[0]["name"]}, names)

    def test_inline_fraction_rule_rejects_ambiguous_fallback_alignment(self):
        runs, spans = self._simple_fraction_fixture()
        fallback = "left 1 N and right 1 N"
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            fallback,
            runs,
            spans,
        )
        self.assertEqual(fallback, repaired)
        self.assertEqual(set(), names)

        runs[1]["text"] = "1"
        overlapping = "1 1 1"
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            overlapping,
            runs,
            spans,
        )
        self.assertEqual(overlapping, repaired)
        self.assertEqual(set(), names)

    def test_inline_fraction_rule_rejects_private_control_glyph(self):
        for glyph in ("\x10", "\uf8eb", "\U000f0000"):
            with self.subTest(glyph=repr(glyph)):
                runs, spans = self._simple_fraction_fixture()
                runs.append(
                    {
                        "text": glyph,
                        "size": 7.0,
                        "fontname": "CMEX10",
                        "bbox": {"l": 11.0, "r": 12.0, "t": 59.0, "b": 56.0},
                    }
                )
                fallback = "x = 1 N"
                repaired, names = semantic_reflow._repair_inline_fraction_rule(
                    fallback,
                    runs,
                    spans,
                )
                self.assertEqual(fallback, repaired)
                self.assertEqual(set(), names)

    def test_inline_fraction_rule_rejects_cross_word_alignment(self):
        runs, spans = self._simple_fraction_fixture()
        runs[0]["text"] = "a"
        runs[1]["text"] = "b"
        for fallback in ("a big", "data a big", "plain ab token"):
            with self.subTest(fallback=fallback):
                repaired, names = semantic_reflow._repair_inline_fraction_rule(
                    fallback,
                    runs,
                    spans,
                )
                self.assertEqual(fallback, repaired)
                self.assertEqual(set(), names)

    def test_inline_fraction_rule_ignores_adjacent_line_bbox_edge_overlap(self):
        def run(
            text: str,
            left: float,
            right: float,
            center: float,
            *,
            size: float = 8.0,
            font: str = "CMR8",
            half_height: float = 3.0,
        ) -> dict[str, Any]:
            return {
                "text": text,
                "size": size,
                "fontname": font,
                "bbox": {
                    "l": left,
                    "r": right,
                    "t": center + half_height,
                    "b": center - half_height,
                },
            }

        runs = [
            run("d", 133.4, 137.5, 92.6),
            run("e", 138.1, 141.3, 91.6),
            run("t", 141.7, 144.4, 92.3),
            run("B", 146.7, 152.6, 92.6, font="CMMI8"),
            run("′", 153.4, 155.0, 94.4, size=6.0, font="CMSY6"),
            run("d", 134.7, 138.9, 84.5),
            run("e", 139.4, 142.7, 83.5),
            run("t", 143.1, 145.7, 84.2),
            run("B", 148.1, 154.0, 84.5, font="CMMI8"),
            # This glyph belongs to the line above.  Its box overlaps the
            # repair box edge, but its center is outside the fraction.
            run("ℓ", 147.4, 150.7, 100.1, font="CMMI8", half_height=4.0),
        ]
        spans = [
            {
                "name": "geometry_math_span-fraction_rule-p9-occ0-x124",
                "reason": "fraction_rule",
                "rule_y": 88.3,
                "bbox": {"l": 124.5, "r": 175.5, "t": 104.1, "b": 76.2},
                "repair_bbox": {
                    "l": 133.1,
                    "r": 155.8,
                    "t": 97.4,
                    "b": 81.5,
                },
            }
        ]

        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            "det B ′ det B",
            runs,
            spans,
        )

        self.assertEqual("det B ′/det B", repaired)
        self.assertEqual({spans[0]["name"]}, names)

    def test_inline_fraction_rule_keeps_printable_cmex_operator_in_denominator(self):
        def run(text: str, font: str, left: float, right: float, center: float) -> dict[str, Any]:
            return {
                "text": text,
                "size": 7.0,
                "fontname": font,
                "bbox": {
                    "l": left,
                    "r": right,
                    "t": center + 2.0,
                    "b": center - 2.0,
                },
            }

        runs = [
            run("e", "CMMI8", 10.0, 14.0, 57.0),
            run("S", "CMMI6", 14.1, 17.0, 59.0),
            run("T", "CMMI6", 17.1, 21.0, 59.0),
            run("i", "CMMI6", 20.0, 22.0, 56.0),
            run("P", "CMEX8", 8.0, 12.0, 43.0),
            run("j", "CMMI6", 11.5, 14.0, 41.0),
            run("e", "CMMI8", 14.1, 18.0, 43.0),
            run("S", "CMMI6", 18.1, 21.0, 45.0),
            run("T", "CMMI6", 21.1, 25.0, 45.0),
            run("j", "CMMI6", 24.0, 26.0, 42.0),
        ]
        spans = [
            {
                "name": "geometry_math_span-fraction_rule-p1-occ0-x8",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "bbox": {"l": 7.0, "r": 27.0, "t": 62.0, "b": 38.0},
                "repair_bbox": {"l": 7.0, "r": 27.0, "t": 62.0, "b": 38.0},
            }
        ]
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            "P_i = e S T i ∑ j e S T j .",
            runs,
            spans,
        )
        self.assertEqual("P_i = e S T i/∑ j e S T j .", repaired)
        self.assertEqual({spans[0]["name"]}, names)

    def test_inline_math_run_normalizes_only_known_cmex_operator_slots(self):
        self.assertEqual(
            "∑",
            semantic_reflow._normalize_inline_math_run_text(
                {"text": "P", "fontname": "CMEX10"}
            ),
        )
        self.assertFalse(
            semantic_reflow._inline_math_control_character(
                {"text": "P", "fontname": "CMEX10"}
            )
        )
        self.assertTrue(
            semantic_reflow._inline_math_control_character(
                {"text": "+", "fontname": "CMEX10"}
            )
        )

    def test_math_aware_text_does_not_duplicate_raw_cmex_sum_slot(self):
        def run(
            text: str,
            font: str,
            size: float,
            left: float,
            right: float,
            top: float,
            bottom: float,
        ) -> dict[str, Any]:
            return {
                "text": text,
                "fontname": font,
                "size": size,
                "bbox": {"l": left, "r": right, "t": top, "b": bottom},
            }

        runs = [
            run("P", "CMEX10", 9.96, 236.45, 245.84, 488.07, 478.11),
            run("N", "CMMI7", 6.97, 246.89, 252.50, 491.00, 484.90),
            run("i", "CMMI7", 6.97, 246.73, 249.60, 483.00, 476.80),
            run("=", "CMR7", 6.97, 249.71, 256.00, 482.50, 476.20),
            run("1", "CMR7", 6.97, 256.10, 259.30, 483.10, 476.70),
            run("r", "CMMI10", 9.96, 261.76, 266.20, 487.70, 477.80),
            run("i", "CMMI7", 6.97, 266.29, 269.30, 484.50, 478.20),
        ]
        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._math_aware_diagnostics = {}
        reader._pypdfium_characters = lambda _page_no, _bbox: runs
        reader._inline_math_span_evidence = lambda _prov, _runs: []
        prov = {
            "page_no": 1,
            "bbox": {"l": 230.0, "r": 275.0, "t": 495.0, "b": 470.0},
        }
        repaired = reader.math_aware_text(
            prov,
            "∑ N i =1 r i",
            similarity_threshold=0.0,
        )
        self.assertEqual("∑_{i=1}^N r_i", repaired)
        self.assertNotIn("∑ P", repaired)

    def test_inline_fraction_rule_rejects_wrong_cmex_operator_or_punctuation(self):
        def run(text: str, font: str, center: float) -> dict[str, Any]:
            return {
                "text": text,
                "size": 7.0,
                "fontname": font,
                "bbox": {
                    "l": 10.0,
                    "r": 14.0,
                    "t": center + 2.0,
                    "b": center - 2.0,
                },
            }

        runs = [
            run("e", "CMMI8", 57.0),
            run("P", "CMEX8", 43.0),
        ]
        spans = [
            {
                "name": "geometry_math_span-fraction_rule-p1-occ0-x8",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "bbox": {"l": 7.0, "r": 17.0, "t": 62.0, "b": 38.0},
                "repair_bbox": {"l": 7.0, "r": 17.0, "t": 62.0, "b": 38.0},
            }
        ]
        for fallback in ("e + text", "e ∏ text"):
            with self.subTest(fallback=fallback):
                repaired, names = semantic_reflow._repair_inline_fraction_rule(
                    fallback,
                    runs,
                    spans,
                )
                self.assertEqual(fallback, repaired)
                self.assertEqual(set(), names)

    @staticmethod
    def _solidus_run(
        text: str,
        left: float,
        right: float,
        center: float,
        *,
        font: str = "CMMI10",
        size: float = 10.0,
    ) -> dict[str, Any]:
        return {
            "text": text,
            "size": size,
            "fontname": font,
            "bbox": {
                "l": left,
                "r": right,
                "t": center + 3.0,
                "b": center - 3.0,
            },
        }

    def test_inline_solidus_glyph_restores_unique_math_gap(self):
        runs = [
            self._solidus_run("x", 10.0, 14.0, 50.0),
            self._solidus_run("/", 15.0, 18.0, 50.0),
            self._solidus_run("y", 19.0, 23.0, 50.0),
        ]
        repaired, names = semantic_reflow._repair_inline_solidus_glyph(
            "x y",
            runs,
        )
        self.assertEqual("x/y", repaired)
        self.assertEqual(1, len(names))
        self.assertTrue(next(iter(names)).startswith("geometry_solidus-"))

    def test_inline_solidus_glyph_ignores_ordinary_font_slash(self):
        runs = [
            self._solidus_run("x", 10.0, 14.0, 50.0, font="CMR10"),
            self._solidus_run("/", 15.0, 18.0, 50.0, font="CMR10"),
            self._solidus_run("y", 19.0, 23.0, 50.0, font="CMR10"),
        ]
        fallback = "x y"
        repaired, names = semantic_reflow._repair_inline_solidus_glyph(
            fallback,
            runs,
        )
        self.assertEqual(fallback, repaired)
        self.assertEqual(set(), names)

    def test_inline_solidus_glyph_rejects_repeated_context(self):
        runs = [
            self._solidus_run("a", 1.0, 4.0, 50.0),
            self._solidus_run("x", 5.0, 8.0, 50.0),
            self._solidus_run("/", 9.0, 12.0, 50.0),
            self._solidus_run("y", 13.0, 16.0, 50.0),
            self._solidus_run("b", 17.0, 20.0, 50.0),
        ]
        fallback = "a x y b; a x y b"
        repaired, names = semantic_reflow._repair_inline_solidus_glyph(
            fallback,
            runs,
        )
        self.assertEqual(fallback, repaired)
        self.assertEqual(set(), names)

    def test_inline_solidus_glyph_is_idempotent_when_fallback_has_slash(self):
        runs = [
            self._solidus_run("x", 10.0, 14.0, 50.0),
            self._solidus_run("/", 15.0, 18.0, 50.0),
            self._solidus_run("y", 19.0, 23.0, 50.0),
        ]
        fallback = "x / y"
        repaired, names = semantic_reflow._repair_inline_solidus_glyph(
            fallback,
            runs,
        )
        self.assertEqual(fallback, repaired)
        self.assertEqual(1, len(names))
        self.assertEqual(1, repaired.count("/"))

    def test_short_fraction_rule_is_detected_with_small_bar(self):
        class FakePage:
            height = 100.0
            lines = [
                {
                    "x0": 50.0,
                    "x1": 54.234,
                    "top": 48.0,
                    "bottom": 48.0,
                }
            ]

        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = SimpleNamespace(pages=[FakePage()])
        runs = [
            self._solidus_run("q", 44.0, 48.0, 52.0, font="CMMI10", size=10.0),
            self._solidus_run("1", 50.0, 52.0, 56.0, font="CMR6", size=6.0),
            self._solidus_run("2", 50.0, 52.0, 48.0, font="CMR6", size=6.0),
        ]
        spans = reader._inline_math_span_evidence(
            {
                "page_no": 1,
                "bbox": {
                    "l": 40.0,
                    "r": 70.0,
                    "t": 70.0,
                    "b": 30.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            },
            runs,
        )
        self.assertEqual(1, len(spans))
        self.assertEqual("fraction_rule", spans[0]["reason"])

    def test_short_fraction_rule_rejects_overbar_and_underline(self):
        class FakePage:
            height = 100.0

            def __init__(self, runs_on_one_side):
                self.lines = [
                    {
                        "x0": 50.0,
                        "x1": 54.234,
                        "top": 48.0,
                        "bottom": 48.0,
                    }
                ]

        for label, runs in (
            (
                "overbar",
                [self._solidus_run("x", 50.0, 53.0, 56.0, font="CMMI10")],
            ),
            (
                "underline",
                [self._solidus_run("x", 50.0, 53.0, 48.0, font="CMMI10")],
            ),
        ):
            with self.subTest(label=label):
                reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
                reader._pdf = SimpleNamespace(pages=[FakePage(runs)])
                spans = reader._inline_math_span_evidence(
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 40.0,
                            "r": 70.0,
                            "t": 70.0,
                            "b": 30.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    },
                    runs,
                )
                self.assertEqual([], spans)

    def test_inline_fraction_rule_repairs_disjoint_unique_multi_spans(self):
        def run(text: str, left: float, right: float, center: float) -> dict[str, Any]:
            return {
                "text": text,
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {
                    "l": left,
                    "r": right,
                    "t": center + 2.0,
                    "b": center - 2.0,
                },
            }

        runs = [
            run("1", 10.0, 13.0, 55.0),
            run("N", 10.0, 14.0, 45.0),
            run("3", 30.0, 33.0, 55.0),
            run("M", 30.0, 34.0, 45.0),
        ]
        spans = [
            {
                "name": "fraction-one",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "repair_bbox": {"l": 9.0, "r": 15.0, "t": 58.0, "b": 42.0},
            },
            {
                "name": "fraction-two",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "repair_bbox": {"l": 29.0, "r": 35.0, "t": 58.0, "b": 42.0},
            },
        ]
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            "x = 1 N ; y = 3 M",
            runs,
            spans,
        )
        self.assertEqual("x = 1/N ; y = 3/M", repaired)
        self.assertEqual({"fraction-one", "fraction-two"}, names)

    def test_inline_fraction_rule_rejects_overlapping_or_ambiguous_multi_spans(self):
        runs = [
            {
                "text": "1",
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {"l": 10.0, "r": 13.0, "t": 57.0, "b": 53.0},
            },
            {
                "text": "N",
                "size": 7.0,
                "fontname": "CMR7",
                "bbox": {"l": 10.0, "r": 14.0, "t": 47.0, "b": 43.0},
            },
        ]
        overlapping = [
            {
                "name": "first",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "repair_bbox": {"l": 9.0, "r": 15.0, "t": 58.0, "b": 42.0},
            },
            {
                "name": "second",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "repair_bbox": {"l": 12.0, "r": 18.0, "t": 58.0, "b": 42.0},
            },
        ]
        fallback = "x = 1 N"
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            fallback,
            runs,
            overlapping,
        )
        self.assertEqual(fallback, repaired)
        self.assertEqual(set(), names)

        disjoint = [
            {
                "name": "first",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "repair_bbox": {"l": 9.0, "r": 15.0, "t": 58.0, "b": 42.0},
            },
            {
                "name": "second",
                "reason": "fraction_rule",
                "rule_y": 50.0,
                "repair_bbox": {"l": 19.0, "r": 25.0, "t": 58.0, "b": 42.0},
            },
        ]
        repaired, names = semantic_reflow._repair_inline_fraction_rule(
            "x = 1 N ; y = 1 N",
            runs,
            disjoint,
        )
        self.assertEqual("x = 1 N ; y = 1 N", repaired)
        self.assertEqual(set(), names)

    def test_math_aware_text_marks_repaired_fraction_resolved(self):
        runs, spans = self._simple_fraction_fixture()
        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._math_aware_diagnostics = {}
        reader._pypdfium_characters = lambda _page_no, _bbox: runs
        reader._inline_math_span_evidence = lambda _prov, _runs: spans
        prov = {
            "page_no": 1,
            "bbox": {"l": 0.0, "r": 20.0, "t": 70.0, "b": 30.0},
        }
        repaired = reader.math_aware_text(
            prov,
            "x = 1 N",
            similarity_threshold=0.0,
        )
        self.assertEqual("x = 1/N", repaired)
        diagnostic = reader._math_aware_diagnostics[reader._math_diagnostic_key(prov)]
        self.assertEqual([], diagnostic["unresolved_clusters"])
        self.assertEqual([spans[0]["name"]], diagnostic["repaired_names"])
        self.assertTrue(diagnostic["math_span_regions"][0]["resolved"])

    def test_table_cross_row_hole_repair_rejects_occupied_target(self):
        document, source = self._cross_row_hole_fixture(target_text="1234")
        original = copy.deepcopy(document)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(document, None, source_reader=source)
        self.assertFalse(diagnostic["applied"])
        self.assertEqual(original, document)
        self.assertTrue(any(item["reason"] == "target_occupied_or_merged" for item in diagnostic["rejected"]))

    def test_table_cross_row_hole_repair_rejects_inconsistent_peer_centers(self):
        document, source = self._cross_row_hole_fixture(
            row_centers=((15.0, 35.0), (15.0, 35.0), (24.0, 44.0), (15.0, 35.0))
        )
        original = copy.deepcopy(document)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(document, None, source_reader=source)
        self.assertFalse(diagnostic["applied"])
        self.assertEqual(original, document)
        self.assertTrue(any(item["reason"] == "peer_row_centers_inconsistent" for item in diagnostic["rejected"]))

    def test_table_cross_row_hole_repair_skips_valid_two_by_two_multiline_cell(self):
        document, source = self._cross_row_hole_fixture(rows=2, cols=2, candidate_text="left one left two")
        original = copy.deepcopy(document)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(document, None, source_reader=source)
        self.assertFalse(diagnostic["applied"])
        self.assertEqual("no_eligible_candidate", diagnostic["reason"])
        self.assertEqual(0, source.calls)
        self.assertEqual(original, document)

    def test_table_cross_row_hole_repair_skips_chunked_document(self):
        document, source = self._cross_row_hole_fixture(chunked=True)
        original = copy.deepcopy(document)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(document, None, source_reader=source)
        self.assertFalse(diagnostic["applied"])
        self.assertEqual("chunked_document_skipped", diagnostic["reason"])
        self.assertEqual(0, source.calls)
        self.assertEqual(original, document)

    def test_table_cross_row_hole_repair_no_tables_and_bounds_are_noops(self):
        no_tables: dict[str, Any] = {"schema_name": "docling_document", "tables": []}
        diagnostic = semantic_reflow.repair_table_cross_row_holes(no_tables, None)
        self.assertTrue(diagnostic["ok"])
        self.assertEqual("no_tables", diagnostic["reason"])
        bounded, source = self._cross_row_hole_fixture(rows=257, cols=4)
        original = copy.deepcopy(bounded)
        diagnostic = semantic_reflow.repair_table_cross_row_holes(bounded, None, source_reader=source)
        self.assertFalse(diagnostic["applied"])
        self.assertEqual(original, bounded)
        self.assertEqual(0, source.calls)

    def test_algorithm_grouping_cannot_promote_picture_contained_ocr(self):
        def prov(left: float, right: float, top: float, bottom: float) -> list[dict[str, Any]]:
            return [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": left,
                        "r": right,
                        "t": top,
                        "b": bottom,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ]

        document = {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/groups/0"},
                ]
            },
            "texts": [
                {
                    "label": "text",
                    "self_ref": "#/texts/0",
                    "text": "Algorithm 1 Picture annotation",
                    "prov": prov(110.0, 260.0, 620.0, 600.0),
                },
                {
                    "label": "text",
                    "self_ref": "#/texts/1",
                    "text": "1: return a visual label",
                    "prov": prov(115.0, 255.0, 590.0, 570.0),
                },
            ],
            "groups": [
                {
                    "label": "list",
                    "children": [{"$ref": "#/texts/1"}],
                }
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": prov(100.0, 300.0, 650.0, 500.0),
                }
            ],
        }

        blocks, consumed = semantic_reflow._algorithm_group_blocks(
            document,
            _TableSource(),
        )
        self.assertEqual({}, blocks)
        self.assertEqual(set(), consumed)
        collected = semantic_reflow._collect_items(document, _TableSource())
        self.assertFalse(
            any(
                item.kind in {"algorithm", "code", "text"}
                and "Algorithm 1" in str((item.node or {}).get("text") or "")
                for item in collected
            )
        )

        document["pictures"][0]["prov"] = prov(350.0, 500.0, 650.0, 500.0)
        blocks, consumed = semantic_reflow._algorithm_group_blocks(
            document,
            _TableSource(),
        )
        self.assertIn("#/texts/0", blocks)
        self.assertTrue({"#/texts/0", "#/groups/0", "#/texts/1"}.issubset(consumed))

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

    def test_repaired_inline_math_has_paragraph_scope_source_region(self):
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
            inline_math_source_scope="inline_math_repaired",
            inline_math_source_reason="inline_math_repaired",
        )

        self.assertEqual(
            semantic_reflow._inline_math_source_region_records(
                item,
                part_index=0,
            ),
            [
                {
                    "anchor": "inline-math-text-1",
                    "page_no": 1,
                    "bbox": {"l": 0.0, "r": 100.0, "t": 100.0, "b": 80.0},
                    "repair_bbox": None,
                    "source_text": "The repaired notation is readable.",
                    "crop_clip_bounds": None,
                    "collection_index": None,
                    "rank": 1.0,
                    "part_index": 0,
                    "unresolved": False,
                    "scope": "paragraph",
                    "reason": "inline_math_repaired",
                    "unresolved_source_text": None,
                    "fallback_whole_paragraph": False,
                }
            ],
        )

    def test_same_text_node_same_column_multiple_provs_union_into_single_paragraph_region(self):
        class _InlineEvidenceSource:
            _pypdf = object()
            _math_aware_diagnostics: dict[
                tuple[int, float, float, float, float], dict[str, Any]
            ] = {}

            def __init__(self, text: str):
                self.text_content = text

            def text(self, prov, *, layout=False, padding=0.0):
                charspan = prov.get("charspan")
                if (
                    isinstance(charspan, list)
                    and len(charspan) == 2
                    and all(isinstance(value, int) for value in charspan)
                ):
                    start, end = charspan
                    return self.text_content[start:end]
                return ""

            @staticmethod
            def _math_diagnostic_key(_prov):
                return None

            @staticmethod
            def math_aware_text(_prov, value):
                return value

            @staticmethod
            def inline_math_evidence(_prov):
                return True

        left_first = "LeftA"
        left_second = "LeftB"
        right = "RightC"
        text = left_first + left_second + right
        source = _InlineEvidenceSource(text)
        first_span = {
            "l": 50.0,
            "r": 160.0,
            "t": 700.0,
            "b": 680.0,
            "coord_origin": "BOTTOMLEFT",
        }
        second_span = {
            "l": 80.0,
            "r": 170.0,
            "t": 700.0,
            "b": 680.0,
            "coord_origin": "BOTTOMLEFT",
        }
        third_span = {
            "l": 360.0,
            "r": 430.0,
            "t": 700.0,
            "b": 680.0,
            "coord_origin": "BOTTOMLEFT",
        }
        document = {
            "pages": {"1": {"size": {"width": 600.0, "height": 800.0}}},
            "texts": [
                {
                    "label": "text",
                    "text": text,
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, len(left_first)],
                            "bbox": first_span,
                        },
                        {
                            "page_no": 1,
                            "charspan": [
                                len(left_first),
                                len(left_first) + len(left_second),
                            ],
                            "bbox": second_span,
                        },
                        {
                            "page_no": 1,
                            "charspan": [
                                len(left_first) + len(left_second),
                                len(text),
                            ],
                            "bbox": third_span,
                        },
                    ],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }

        items = semantic_reflow._collect_items(document, source)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].kind, "text")
        self.assertEqual(items[1].kind, "text")
        regions = [
            region
            for item in items
            for region in semantic_reflow._inline_math_source_region_records(
                item,
                part_index=0,
            )
            if region.get("anchor")
        ]
        self.assertEqual(len(regions), 2)

        left_item = next(
            item
            for item in items
            if float(item.bbox["r"]) <= 300.0 and float(item.bbox["l"]) < 300.0
        )
        right_item = next(
            item
            for item in items
            if float(item.bbox["l"]) >= 300.0
        )
        left_region = next(region for region in regions if region["scope"] == "paragraph" and float(region["bbox"]["r"]) <= 300.0)
        right_region = next(region for region in regions if region["scope"] == "paragraph" and float(region["bbox"]["l"]) >= 300.0)

        self.assertEqual(left_item.source_text, left_first + left_second)
        self.assertEqual(
            left_item.bbox,
            {"l": 50.0, "r": 170.0, "t": 700.0, "b": 680.0, "coord_origin": "BOTTOMLEFT"},
        )
        self.assertEqual(
            left_region["crop_clip_bounds"],
            {"l": 0.0, "r": 300.0, "t": 800.0, "b": 0.0, "coord_origin": "BOTTOMLEFT"},
        )
        self.assertEqual(left_region["source_text"], left_first + left_second)
        self.assertEqual(
            right_item.bbox,
            {"l": 360.0, "r": 430.0, "t": 700.0, "b": 680.0, "coord_origin": "BOTTOMLEFT"},
        )
        self.assertEqual(right_item.source_text, right)
        self.assertEqual(
            right_region["crop_clip_bounds"],
            {"l": 300.0, "r": 600.0, "t": 800.0, "b": 0.0, "coord_origin": "BOTTOMLEFT"},
        )
        self.assertEqual(right_region["source_text"], right)
        self.assertEqual(len({region["anchor"] for region in regions}), 2)

    def test_grouped_inline_math_prioritizes_unresolved_member_diagnostic(self):
        node = {"label": "text", "text": "Evidence Unresolved"}
        evidence = semantic_reflow.FlowItem(
            kind="text",
            node=node,
            rank=1.0,
            page_no=1,
            bbox={"l": 40.0, "r": 140.0, "t": 700.0, "b": 680.0},
            prov={"page_no": 1},
            source_text="Evidence",
            collection_index=0,
            inline_math_source_anchor="evidence-anchor",
            inline_math_source_scope="inline_math_evidence",
            inline_math_source_reason="inline_math_evidence",
            inline_math_source_unresolved=False,
            inline_math_unresolved_regions=[{"reason": "inline_math_evidence"}],
        )
        unresolved = semantic_reflow.FlowItem(
            kind="text",
            node=node,
            rank=1.5,
            page_no=1,
            bbox={"l": 40.0, "r": 150.0, "t": 660.0, "b": 640.0},
            prov={"page_no": 1},
            source_text="Unresolved",
            collection_index=0,
            inline_math_source_anchor="unresolved-anchor",
            inline_math_source_scope="inline_math_unresolved",
            inline_math_source_reason="inline_math_unresolved",
            inline_math_source_unresolved=True,
            inline_math_unresolved_regions=[
                {
                    "name": "geometry_script-x-sup-empty-run1",
                    "bbox": {"l": 80.0, "r": 90.0, "t": 660.0, "b": 650.0},
                    "reason": "inline_math_unresolved",
                }
            ],
        )
        document = {
            "pages": {"1": {"size": {"width": 600.0, "height": 800.0}}}
        }

        grouped = semantic_reflow._group_text_node_paragraph_items(
            document,
            [evidence, unresolved],
        )

        self.assertEqual(1, len(grouped))
        item = grouped[0]
        self.assertTrue(item.inline_math_source_unresolved)
        self.assertEqual("unresolved-anchor", item.inline_math_source_anchor)
        self.assertEqual("inline_math_unresolved", item.inline_math_source_scope)
        self.assertEqual("inline_math_unresolved", item.inline_math_source_reason)
        self.assertEqual(
            "geometry_script-x-sup-empty-run1",
            item.inline_math_unresolved_regions[0]["name"],
        )
        self.assertEqual(
            "inline_math_unresolved",
            item.inline_math_unresolved_regions[0]["reason"],
        )

    def test_same_text_node_same_column_multiple_provs_without_inline_math_stay_unmerged(self):
        class _PlainSource:
            _pypdf = None
            _math_aware_diagnostics: dict[
                tuple[int, float, float, float, float], dict[str, Any]
            ] = {}

            def __init__(self, text: str):
                self.text_content = text

            def text(self, prov, *, layout=False, padding=0.0):
                charspan = prov.get("charspan")
                if (
                    isinstance(charspan, list)
                    and len(charspan) == 2
                    and all(isinstance(value, int) for value in charspan)
                ):
                    start, end = charspan
                    return self.text_content[start:end]
                return ""

            @staticmethod
            def _math_diagnostic_key(_prov):
                return None

            @staticmethod
            def math_aware_text(_prov, value):
                return value

            @staticmethod
            def inline_math_evidence(_prov):
                return False

        text = "alpha beta"
        source = _PlainSource(text)
        document = {
            "pages": {"1": {"size": {"width": 600.0, "height": 800.0}}},
            "texts": [
                {
                    "label": "text",
                    "text": text,
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, 5],
                            "bbox": {
                                "l": 50.0,
                                "r": 160.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                        {
                            "page_no": 1,
                            "charspan": [6, 10],
                            "bbox": {
                                "l": 190.0,
                                "r": 250.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                    ],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }

        items = semantic_reflow._collect_items(document, source)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].source_text, "alpha")
        self.assertEqual(items[1].source_text, "beta")

    def test_inline_math_source_merges_preserve_charspan_spacing(self):
        class _InlineEvidenceSource:
            _pypdf = object()
            _math_aware_diagnostics: dict[
                tuple[int, float, float, float, float], dict[str, Any]
            ] = {}

            def __init__(self, text: str):
                self.text_content = text

            def text(self, prov, *, layout=False, padding=0.0):
                charspan = prov.get("charspan")
                if (
                    isinstance(charspan, list)
                    and len(charspan) == 2
                    and all(isinstance(value, int) for value in charspan)
                ):
                    start, end = charspan
                    return self.text_content[start:end]
                return ""

            @staticmethod
            def _math_diagnostic_key(_prov):
                return None

            @staticmethod
            def math_aware_text(_prov, value):
                return value

            @staticmethod
            def inline_math_evidence(_prov):
                return True

        left = "Alpha "
        right = "Beta"
        text = left + right
        source = _InlineEvidenceSource(text)
        document = {
            "pages": {"1": {"size": {"width": 600.0, "height": 800.0}}},
            "texts": [
                {
                    "label": "text",
                    "text": text,
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, len(left)],
                            "bbox": {
                                "l": 50.0,
                                "r": 150.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                        {
                            "page_no": 1,
                            "charspan": [len(left), len(text)],
                            "bbox": {
                                "l": 155.0,
                                "r": 220.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                    ],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }

        items = semantic_reflow._collect_items(document, source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_text, "Alpha Beta")

    def test_inline_math_source_preserves_transformed_span_text_when_merging(self):
        class _InlineEvidenceSource:
            _pypdf = object()
            _math_aware_diagnostics: dict[
                tuple[int, float, float, float, float], dict[str, Any]
            ] = {}

            def __init__(self, text: str):
                self.text_content = text

            def text(self, prov, *, layout=False, padding=0.0):
                charspan = prov.get("charspan")
                if (
                    isinstance(charspan, list)
                    and len(charspan) == 2
                    and all(isinstance(value, int) for value in charspan)
                ):
                    start, end = charspan
                    return self.text_content[start:end]
                return ""

            @staticmethod
            def _math_diagnostic_key(_prov):
                return None

            @staticmethod
            def math_aware_text(_prov, value):
                if value == "x y":
                    return "x_y"
                return value

            @staticmethod
            def inline_math_evidence(_prov):
                return True

        text = "Plain x y tail"
        source = _InlineEvidenceSource(text)
        document = {
            "pages": {"1": {"size": {"width": 600.0, "height": 800.0}}},
            "texts": [
                {
                    "label": "text",
                    "text": text,
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, 6],
                            "bbox": {
                                "l": 40.0,
                                "r": 130.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                        {
                            "page_no": 1,
                            "charspan": [6, 9],
                            "bbox": {
                                "l": 140.0,
                                "r": 180.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                        {
                            "page_no": 1,
                            "charspan": [9, 14],
                            "bbox": {
                                "l": 185.0,
                                "r": 255.0,
                                "t": 700.0,
                                "b": 680.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                    ],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        }

        items = semantic_reflow._collect_items(document, source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_text, "Plain x_y tail")
        regions = semantic_reflow._inline_math_source_region_records(items[0], part_index=0)
        self.assertEqual(regions[0]["source_text"], "Plain x_y tail")

    def test_inline_math_evidence_generates_paragraph_scope_source_region(self):
        item = semantic_reflow.FlowItem(
            kind="text",
            node={"label": "text"},
            rank=1.0,
            page_no=1,
            bbox={"l": 10.0, "r": 120.0, "t": 210.0, "b": 160.0},
            prov={"page_no": 1, "bbox": {}},
            source_text="Formula-style proof fragment.",
            inline_math_source_anchor="inline-math-text-evidence",
            inline_math_source_scope="inline_math_evidence",
            inline_math_source_reason="inline_math_evidence",
            inline_math_unresolved_regions=[{"reason": "inline_math_evidence"}],
        )
        regions = semantic_reflow._inline_math_source_region_records(
            item,
            part_index=1,
        )
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["anchor"], "inline-math-text-evidence")
        self.assertEqual(regions[0]["scope"], "paragraph")
        self.assertFalse(regions[0]["unresolved"])
        self.assertEqual(regions[0]["bbox"], item.bbox)
        self.assertEqual(regions[0]["part_index"], 1)
        self.assertIsNone(regions[0]["crop_clip_bounds"])
        self.assertEqual(
            regions[0]["source_text"],
            item.source_text,
        )

    def test_inline_math_repaired_evidence_or_unresolved_uses_paragraph_region(self):
        tight = semantic_reflow.FlowItem(
            kind="text",
            node={"label": "text"},
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 100.0, "t": 100.0, "b": 80.0},
            prov={"page_no": 1, "bbox": {}},
            source_text="A span needs review.",
            inline_math_source_anchor="inline-math-text-2",
            inline_math_source_scope="inline_math_unresolved",
            inline_math_source_reason="inline_math_unresolved_geometry",
            inline_math_source_unresolved=True,
            inline_math_unresolved_regions=[
                {"reason": "fraction_span", "source_text": "A span needs review."}
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
            inline_math_source_scope="inline_math_unresolved",
            inline_math_source_reason="inline_math_unresolved",
            inline_math_source_unresolved=True,
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

        self.assertEqual(tight_records[0]["bbox"], tight.bbox)
        self.assertEqual(fallback_records[0]["bbox"], fallback.bbox)
        self.assertEqual(tight_records[0]["source_text"], tight.source_text)
        self.assertEqual(
            tight_records[0]["unresolved_source_text"],
            tight.inline_math_unresolved_regions[0]["source_text"],
        )
        self.assertIsNone(fallback_records[0]["unresolved_source_text"])
        self.assertTrue(tight_records[0]["unresolved"])
        self.assertTrue(fallback_records[0]["unresolved"])
        self.assertEqual(tight_records[0]["scope"], "paragraph")
        self.assertEqual(fallback_records[0]["scope"], "paragraph")
        self.assertIsNone(tight_records[0]["crop_clip_bounds"])
        self.assertIsNone(fallback_records[0]["crop_clip_bounds"])

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

    def test_equation_number_prefers_formula_adjacent_label_geometry(self):
        class FakeCrop:
            def __init__(self, lines):
                self._lines = lines

            def extract_text_lines(self, **_kwargs):
                return self._lines

        class FakePage:
            width = 612.0
            height = 792.0

            def __init__(self, lines):
                self._lines = lines

            def crop(self, *_args, **_kwargs):
                return FakeCrop(self._lines)

        def make_char(text, left, right, top, bottom):
            return {
                "text": text,
                "x0": left,
                "x1": right,
                "top": top,
                "bottom": bottom,
            }

        lines = [
            {
                "x0": 430.0,
                "x1": 556.73,
                "top": 196.0,
                "bottom": 186.0,
                "text": "a+b=(4)",
                "chars": [
                    make_char("a", 430.0, 438.0, 196.0, 186.0),
                    make_char("+", 441.0, 445.0, 196.0, 186.0),
                    make_char("b", 449.0, 458.0, 196.0, 186.0),
                    make_char("=", 460.0, 464.0, 196.0, 186.0),
                    make_char("(", 543.99, 548.2, 196.0, 186.0),
                    make_char("4", 548.2, 552.0, 196.0, 186.0),
                    make_char(")", 552.0, 556.73, 196.0, 186.0),
                ],
            },
            {
                "x0": 240.0,
                "x1": 291.0,
                "top": 150.0,
                "bottom": 140.0,
                "text": "x=(6)",
                "chars": [
                    make_char("x", 240.0, 246.0, 150.0, 140.0),
                    make_char("=", 250.0, 253.0, 150.0, 140.0),
                    make_char("(", 278.0, 282.0, 150.0, 140.0),
                    make_char("6", 282.0, 286.0, 150.0, 140.0),
                    make_char(")", 286.0, 291.0, 150.0, 140.0),
                ],
            },
            {
                "x0": 360.0,
                "x1": 386.0,
                "top": 100.0,
                "bottom": 90.0,
                "text": "f(0)",
                "chars": [
                    make_char("f", 360.0, 365.0, 100.0, 90.0),
                    make_char("(", 372.0, 376.0, 100.0, 90.0),
                    make_char("0", 376.0, 381.0, 100.0, 90.0),
                    make_char(")", 381.0, 386.0, 100.0, 90.0),
                ],
            },
            {
                "x0": 100.0,
                "x1": 176.0,
                "top": 60.0,
                "bottom": 50.0,
                "text": "v=(4)(7)",
                "chars": [
                    make_char("v", 100.0, 107.0, 60.0, 50.0),
                    make_char("=", 108.0, 111.0, 60.0, 50.0),
                    make_char("(", 140.0, 143.0, 60.0, 50.0),
                    make_char("4", 143.0, 146.0, 60.0, 50.0),
                    make_char(")", 146.0, 150.0, 60.0, 50.0),
                    make_char("(", 160.0, 163.0, 60.0, 50.0),
                    make_char("7", 163.0, 167.0, 60.0, 50.0),
                    make_char(")", 167.0, 176.0, 60.0, 50.0),
                ],
            },
            {
                "x0": 543.99,
                "x1": 556.73,
                "top": 30.0,
                "bottom": 20.0,
                "text": "(9)",
                "chars": [
                    make_char("(", 543.99, 548.2, 30.0, 20.0),
                    make_char("9", 548.2, 552.0, 30.0, 20.0),
                    make_char(")", 552.0, 556.73, 30.0, 20.0),
                ],
            },
            {
                "x0": 543.99,
                "x1": 556.73,
                "top": 682.0,
                "bottom": 692.0,
                "text": "(9)",
                "chars": [
                    make_char("(", 543.99, 548.2, 682.0, 692.0),
                    make_char("9", 548.2, 552.0, 682.0, 692.0),
                    make_char(")", 552.0, 556.73, 682.0, 692.0),
                ],
            },
        ]
        reader = semantic_reflow.SourceReader.__new__(semantic_reflow.SourceReader)
        reader._pdf = SimpleNamespace(pages=[FakePage(lines)])
        reader._pypdf = None

        self.assertEqual(
            4,
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 452.0,
                        "r": 556.73,
                        "t": 606.0,
                        "b": 596.0,
                        "coord_origin": "BOTTOMLEFT",
                    },
                },
            ),
        )
        self.assertEqual(
            6,
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 240.0,
                        "r": 291.0,
                        "t": 150.0,
                        "b": 140.0,
                        "coord_origin": "TOPLEFT",
                    },
                },
            ),
        )
        self.assertIsNone(
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 360.0,
                        "r": 386.0,
                        "t": 100.0,
                        "b": 90.0,
                        "coord_origin": "TOPLEFT",
                    },
                },
            ),
        )
        self.assertIsNone(
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 240.0,
                        "r": 268.0,
                        "t": 30.0,
                        "b": 20.0,
                        "coord_origin": "TOPLEFT",
                    },
                },
            ),
        )
        self.assertIsNone(
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {"l": 240.0, "r": "invalid", "t": 30.0, "b": 20.0},
                },
            ),
        )
        self.assertIsNone(
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 100.0,
                        "r": 140.0,
                        "t": 60.0,
                        "b": 50.0,
                        "coord_origin": "TOPLEFT",
                    },
                },
            ),
        )
        self.assertIsNone(
            reader.equation_number(
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 543.99,
                        "r": 556.73,
                        "t": 100.0,
                        "b": 110.0,
                        "coord_origin": "TOPLEFT",
                    },
                },
            ),
        )

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
                self.assertEqual(result["mode"], "preserve_existing_cjk_body_source_visual_authoritative")
                self.assertFalse(result["machine_surface_ok"])
                self.assertFalse(result["applied"])
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

    def test_cjk_legacy_formula_failure_preserves_existing_surfaces(self):
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
            stable_formula_marker = "FORMULA_LOCK_MARKER_CJK_V1"
            formula_body = r"l_q=O(l_q)\\times W_l (4)"
            expected_html_body = (
                "<html><head></head><body><p>中文正文。</p>"
                f'<div class="docling-formula-second-pass" data-formula-index="4">'
                f"<pre class=\"docling-formula-tex\">{formula_body}</pre>"
                "</div>"
                f"<!-- {stable_formula_marker}:4 -->"
                "</body></html>"
            )
            expected_markdown_body = (
                "中文正文。\n\n"
                f"$$\n{formula_body}\n$$\n"
                f"<!-- {stable_formula_marker}:4 -->\n"
            )
            with tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory)
                (output_dir / "document.html").write_text(
                    expected_html_body,
                    encoding="utf-8",
                )
                (output_dir / "document.md").write_text(
                    expected_markdown_body,
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
                self.assertFalse(result["machine_surface_ok"])
                self.assertEqual(
                    result["mode"],
                    "preserve_existing_cjk_body_source_visual_authoritative",
                )
                self.assertFalse(result["applied"])
                self.assertEqual(result["counts"]["formulas"], 2)
                self.assertEqual(result["dropped_formula_artifacts"], [])
                self.assertEqual(html_text, expected_html_body)
                self.assertEqual(markdown_text, expected_markdown_body)
                self.assertNotIn("source-formula-anchor", html_text)
                self.assertNotIn("source-formula-anchor", markdown_text)
                self.assertIn(stable_formula_marker, html_text)
                self.assertIn(stable_formula_marker, markdown_text)
                self.assertEqual(result["inline_math_source_region_count"], 0)
                self.assertTrue(
                    any(
                        warning.startswith(
                            "cjk_machine_formula_normalization_unavailable:"
                        )
                        for warning in status["warnings"]
                    )
                )
                self.assertTrue(
                    any(
                        "cjk_semantic_fallback_disabled" in warning
                        for warning in status["warnings"]
                    )
                )
                self.assertEqual(status["success_class"], "degraded_success")

                # Prove a second rebuild does not overwrite the stable patched
                # legacy formula body.
                status = {
                    "ok": True,
                    "success_class": "success",
                    "warnings": [],
                    "quality_signals": {},
                }
                metadata = {}
                second_result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    output_dir / "paper.pdf",
                    metadata,
                    status,
                )
                second_html = (output_dir / "document.html").read_text(
                    encoding="utf-8"
                )
                second_markdown = (output_dir / "document.md").read_text(
                    encoding="utf-8"
                )

                self.assertEqual(html_text, second_html)
                self.assertEqual(markdown_text, second_markdown)
                self.assertEqual(second_result["mode"], result["mode"])
                self.assertFalse(second_result["machine_surface_ok"])
                self.assertFalse(second_result["applied"])
                self.assertEqual(second_result["counts"]["formulas"], 2)
                self.assertEqual(second_result["dropped_formula_artifacts"], [])
                self.assertEqual(second_result["inline_math_source_region_count"], 0)
                self.assertEqual(second_html, expected_html_body)
                self.assertEqual(second_markdown, expected_markdown_body)
                self.assertTrue(
                    any(
                        warning.startswith(
                            "cjk_machine_formula_normalization_unavailable:"
                        )
                        for warning in status["warnings"]
                    )
                )
                self.assertTrue(
                    any(
                        "cjk_semantic_fallback_disabled" in warning
                        for warning in status["warnings"]
                    )
                )
        finally:
            semantic_reflow.SourceReader = original_source_reader
            semantic_reflow._normalize_legacy_formula_surfaces = original_normalizer

    def test_cjk_inline_math_source_region_uses_paragraph_bbox_and_trigger_bbox(self):
        class CJKSource:
            _pypdf = None
            _math_aware_diagnostics: dict[Any, Any] = {}

            @staticmethod
            def _pypdfium_characters(_page_no: int, _bbox: dict[str, Any]) -> list[dict[str, Any]]:
                return [
                    {
                        "text": "∑",
                        "bbox": {
                            "l": 120.0,
                            "r": 131.0,
                            "t": 746.0,
                            "b": 736.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ]

            @staticmethod
            def text(_prov: dict[str, Any], *, layout: bool = False, padding: float = 0.0) -> str:
                del layout, padding
                return "本文含有 ER′x 的示例。"

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            node_text = "本文含有 ER′x 的示例。"
            (output_dir / "document.html").write_text(
                f"<html><body>{node_text}</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(f"{node_text}\n", encoding="utf-8")

            paragraph_bbox = {
                "l": 44.0,
                "r": 352.0,
                "t": 752.0,
                "b": 699.0,
                "coord_origin": "BOTTOMLEFT",
            }
            document = {
                "texts": [
                    {
                        "label": "text",
                        "text": node_text,
                        "prov": [
                            {
                                "page_no": 1,
                                "bbox": paragraph_bbox,
                            }
                        ],
                    }
                ]
            }

            regions = semantic_reflow._collect_cjk_inline_math_source_regions(
                output_dir,
                document,
                CJKSource(),
            )["regions"]

            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0]["scope"], "paragraph")
            self.assertEqual(regions[0]["bbox"], paragraph_bbox)
            self.assertEqual(
                regions[0]["trigger_bbox"],
                {
                    "l": 120.0,
                    "r": 131.0,
                    "t": 746.0,
                    "b": 736.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            )
            self.assertEqual(regions[0]["binding_mode"], "inline")
            self.assertEqual(
                regions[0]["source_text"],
                "本文含有 ER′x 的示例。",
            )
            self.assertGreaterEqual(regions[0]["trigger_bbox"]["l"], regions[0]["bbox"]["l"])
            self.assertLessEqual(regions[0]["trigger_bbox"]["r"], regions[0]["bbox"]["r"])
            self.assertGreaterEqual(regions[0]["trigger_bbox"]["t"], regions[0]["bbox"]["b"])
            self.assertLessEqual(regions[0]["trigger_bbox"]["b"], regions[0]["bbox"]["t"])

    def test_remove_review_evidence_from_primary_surfaces_strips_source_disclosures_only(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            html_text = (
                "<html><body>"
                '<details class="docling-source-disclosure docling-table-source-disclosure">'
                '<summary>Compare table crop</summary>'
                "<p>table source</p>"
                "</details>"
                '<details class="docling-source-disclosure docling-formula-source-disclosure">'
                '<summary>Compare formula crop</summary>'
                "<p>formula source</p>"
                "</details>"
                "<details class='docling-source-disclosure docling-code-source-disclosure'>"
                "<summary>Compare code crop</summary>"
                "<p>code source</p>"
                "</details>"
                "<details class='docling-source-disclosure docling-algorithm-source-disclosure'>"
                "<summary>Compare algorithm crop</summary>"
                "<p>algorithm source</p>"
                "</details>"
                "<details class='docling-source-disclosure docling-inline-math-source'>"
                "<summary>Compare inline notation with the original PDF</summary>"
                "<p>inline math source</p>"
                "</details>"
                '<details><summary>LaTeX</summary><span>x=1</span></details>'
                '<section class="docling-table-source-evidence-appendix">table appendix</section>'
                '<section class="docling-formula-source-evidence-appendix">formula appendix</section>'
                '<section class="docling-code-source-evidence-appendix">code appendix</section>'
                '<section class="docling-algorithm-source-evidence-appendix">algorithm appendix</section>'
                '<section class="docling-inline-math-source-appendix">inline appendix</section>'
                '<div class="docling-formula-source">old formula link</div>'
                "</body></html>"
            )
            md_text = (
                "Body.\n"
                '<details><summary>LaTeX</summary><span>x=1</span></details>\n'
                "```text\n"
                '<details class="docling-source-disclosure docling-code-source-disclosure">'
                "<summary>In code fence should keep</summary>"
                "<p>fenced code source</p>"
                "</details>\n"
                "```\n"
                "~~~markdown\n"
                "<details class='docling-source-disclosure docling-formula-source-disclosure'>"
                "<summary>In alt fence should keep</summary>"
                "<p>tilde fenced code source</p>"
                "</details>\n"
                "~~~\n"
                '<details class="docling-source-disclosure docling-inline-math-source"><summary>Compare inline notation with the original PDF</summary>'
                "\n![inline](inline.png)\n"
                "</details>\n"
                "## Original table renderings\n"
                "table appendix\n"
                "## Original formula renderings\n"
                "formula appendix\n"
                "## Original code renderings\n"
                "code appendix\n"
                "## Original algorithm renderings\n"
                "algorithm appendix\n"
                "## Inline math source review appendix\n"
                "inline appendix\n"
            )
            (output_dir / "document.html").write_text(html_text, encoding="utf-8")
            (output_dir / "document.md").write_text(md_text, encoding="utf-8")

            first = semantic_reflow._remove_review_evidence_from_primary_surfaces(output_dir)
            first_html = (output_dir / "document.html").read_text(encoding="utf-8")
            first_md = (output_dir / "document.md").read_text(encoding="utf-8")

            self.assertEqual(first["html_source_disclosure_removed"], 5)
            self.assertEqual(first["markdown_source_disclosure_removed"], 1)
            self.assertIn('<details><summary>LaTeX</summary><span>x=1</span></details>', first_html)
            self.assertIn('<details><summary>LaTeX</summary><span>x=1</span></details>', first_md)
            self.assertNotIn("docling-source-disclosure", first_html)
            self.assertNotIn("Compare table crop", first_html)
            self.assertNotIn("Compare formula crop", first_html)
            self.assertNotIn("Compare code crop", first_html)
            self.assertNotIn("Compare algorithm crop", first_html)
            self.assertNotIn("Compare inline notation with the original PDF", first_html)
            self.assertNotIn("Compare table crop", first_md)
            self.assertNotIn("Compare formula crop", first_md)
            self.assertNotIn("Compare code crop", first_md)
            self.assertNotIn("Compare algorithm crop", first_md)
            self.assertNotIn("Compare inline notation with the original PDF", first_md)
            self.assertNotIn(
                '<details class="docling-source-disclosure docling-inline-math-source">',
                first_md,
            )
            self.assertNotIn(
                "<details class='docling-source-disclosure docling-inline-math-source'>",
                first_md,
            )
            self.assertNotIn("table source", first_html)
            self.assertNotIn("formula source", first_html)
            self.assertNotIn("code source", first_html)
            self.assertNotIn("algorithm source", first_html)
            self.assertNotIn("inline math source", first_html)
            self.assertNotIn("![inline](inline.png)", first_md)
            self.assertIn(
                '<details class="docling-source-disclosure docling-code-source-disclosure"><summary>In code fence should keep</summary><p>fenced code source</p></details>',
                first_md,
            )
            self.assertIn(
                "<details class='docling-source-disclosure docling-formula-source-disclosure'><summary>In alt fence should keep</summary><p>tilde fenced code source</p></details>",
                first_md,
            )
            self.assertNotIn("docling-inline-math-source-appendix", first_html)
            self.assertNotIn("## Original table renderings", first_md)
            self.assertNotIn("## Inline math source review appendix", first_md)

            second = semantic_reflow._remove_review_evidence_from_primary_surfaces(output_dir)
            second_html = (output_dir / "document.html").read_text(encoding="utf-8")
            second_md = (output_dir / "document.md").read_text(encoding="utf-8")

            self.assertEqual(first_html, second_html)
            self.assertEqual(first_md, second_md)
            self.assertEqual(second["html_source_disclosure_removed"], 0)
            self.assertEqual(second["markdown_source_disclosure_removed"], 0)
            self.assertEqual(
                len(re.findall(r"<details><summary>LaTeX</summary><span>x=1</span></details>", first_html)),
                1,
            )
            self.assertEqual(
                len(re.findall(r"<details><summary>LaTeX</summary><span>x=1</span></details>", first_md)),
                1,
            )

    def test_remove_review_evidence_from_primary_surfaces_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            md_text = (
                "Body.\n"
                '<details class="docling-source-disclosure docling-inline-math-source"><summary>Compare inline notation with the original PDF</summary>'
                "\n![inline](inline.png)\n"
                "</details>\n"
                "```text\n"
                "## Original table renderings\n"
                '<details class="docling-source-disclosure docling-code-source-disclosure">'
                "<summary>In backtick fence should stay</summary>"
                "<p>fenced code source</p>"
                "</details>\n"
                "```\n"
                "~~~markdown\n"
                "## Inline math source review appendix\n"
                "<details class='docling-source-disclosure docling-formula-source-disclosure'>"
                "<summary>In tilde fence should stay</summary>"
                "<p>tilde fenced code source</p>"
                "</details>\n"
                "~~~\n"
                "## Original table renderings\n"
                "outside appendix\n"
                "## Original formula renderings\n"
                "outside formula appendix\n"
            )
            (output_dir / "document.md").write_text(md_text, encoding="utf-8")

            first = semantic_reflow._remove_review_evidence_from_primary_surfaces(output_dir)
            first_md = (output_dir / "document.md").read_text(encoding="utf-8")

            self.assertEqual(first["markdown_source_disclosure_removed"], 1)
            self.assertEqual(first["markdown_appendices_removed"], 2)
            self.assertNotIn("![inline](inline.png)", first_md)
            self.assertEqual(first_md.count("## Original table renderings"), 1)
            self.assertEqual(first_md.count("## Inline math source review appendix"), 1)
            self.assertEqual(
                first_md.count('<details class="docling-source-disclosure docling-code-source-disclosure">'),
                1,
            )
            self.assertEqual(
                first_md.count("<details class='docling-source-disclosure docling-formula-source-disclosure'>"),
                1,
            )
            self.assertIn("```text", first_md)
            self.assertIn("~~~markdown", first_md)

    def test_subfigure_inline_label_provenance_drops_node_with_secondary_spans(self):
        class EmptyPhysicalSource:
            _pypdf = None
            _math_aware_diagnostics: dict[Any, Any] = {}

            @staticmethod
            def text(_prov: dict[str, Any], *, layout=False, padding: float = 0.0) -> str:
                del layout, padding
                return ""

        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "(b) Entity table Right-handed Left-handed",
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, 16],
                            "bbox": {
                                "l": 10.0,
                                "r": 12.0,
                                "t": 110.0,
                                "b": 90.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                        {
                            "page_no": 1,
                            "charspan": [17, 41],
                            "bbox": {
                                "l": 150.0,
                                "r": 250.0,
                                "t": 110.0,
                                "b": 90.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                    ],
                },
                {
                    "label": "text",
                    "text": "(c) Matrix table Right-handed Left-handed",
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": [0, 16],
                            "bbox": {
                                "l": 9.0,
                                "r": 14.0,
                                "t": 132.0,
                                "b": 110.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                        {
                            "page_no": 1,
                            "charspan": [17, 41],
                            "bbox": {
                                "l": 145.0,
                                "r": 255.0,
                                "t": 132.0,
                                "b": 110.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        },
                    ],
                },
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 0.0,
                                "r": 80.0,
                                "t": 120.0,
                                "b": 80.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        }

        items = semantic_reflow._collect_items(document, EmptyPhysicalSource())

        self.assertEqual(len(items), 0)


if __name__ == "__main__":
    unittest.main()
