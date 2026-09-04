from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402


def _source_table(
    rows: int,
    cols: int,
    cells: list[dict[str, object]],
    *,
    grid: list[list[object]] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "num_rows": rows,
        "num_cols": cols,
        "table_cells": cells,
    }
    if grid is not None:
        data["grid"] = grid
    return {"self_ref": "#/tables/0", "data": data}


def _cell(row: int, col: int, text: str, *, rowspan: int = 1, colspan: int = 1) -> dict[str, object]:
    return {
        "start_row_offset_idx": row,
        "end_row_offset_idx": row + rowspan,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col + colspan,
        "row_span": rowspan,
        "col_span": colspan,
        "text": text,
    }


class CjkTableBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cells = [
            _cell(0, 0, "A"),
            _cell(0, 1, "B"),
            _cell(1, 0, "C"),
            _cell(1, 1, "D"),
        ]
        self.source = _source_table(2, 2, self.cells)
        self.identity = adapter._table_source_topology_identity(self.source)
        assert self.identity

    def test_exact_unique_html_and_markdown_bind(self) -> None:
        html = (
            "<html><body><p>正文</p>"
            "<table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>C</td><td>D</td></tr></table>"
            "</body></html>"
        )
        markdown = "| A | B |\n|---|---|\n| C | D |\n"
        bound_html, html_refs = adapter._auto_bind_cjk_html_tables(
            html, {"#/tables/0": self.identity}
        )
        bound_markdown, markdown_refs = adapter._auto_bind_cjk_markdown_tables(
            markdown, {"#/tables/0": self.identity}
        )
        self.assertEqual({"#/tables/0"}, html_refs)
        self.assertEqual({"#/tables/0"}, markdown_refs)
        self.assertIn('data-source-ref="#/tables/0"', bound_html)
        self.assertTrue(bound_markdown.startswith("<!-- source-table-ref:#/tables/0 -->"))

    def test_duplicate_output_or_source_is_ambiguous(self) -> None:
        one = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>"
        html = f"<html><body>{one}{one}</body></html>"
        bound_html, refs = adapter._auto_bind_cjk_html_tables(
            html, {"#/tables/0": self.identity}
        )
        self.assertEqual(set(), refs)
        self.assertNotIn("data-source-ref", bound_html)

        markdown = "| A | B |\n|---|---|\n| C | D |\n"
        bound_markdown, refs = adapter._auto_bind_cjk_markdown_tables(
            markdown,
            {"#/tables/0": self.identity, "#/tables/1": self.identity},
        )
        self.assertEqual(set(), refs)
        self.assertNotIn("source-table-ref:", bound_markdown)

    def test_html_and_markdown_fail_independently(self) -> None:
        html = "<html><body><table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table></body></html>"
        markdown = "| A | X |\n|---|---|\n| C | D |\n"
        bound_html, html_refs = adapter._auto_bind_cjk_html_tables(
            html, {"#/tables/0": self.identity}
        )
        bound_markdown, markdown_refs = adapter._auto_bind_cjk_markdown_tables(
            markdown, {"#/tables/0": self.identity}
        )
        self.assertEqual({"#/tables/0"}, html_refs)
        self.assertEqual(set(), markdown_refs)
        self.assertIn("data-source-ref", bound_html)
        self.assertNotIn("source-table-ref:", bound_markdown)

    def test_existing_markers_are_idempotent_and_not_rebound(self) -> None:
        html = (
            '<html><body><table data-source-ref="#/tables/0">'
            "<tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr>"
            "</table></body></html>"
        )
        markdown = (
            "<!-- source-table-ref:#/tables/0 -->\n"
            "| A | B |\n|---|---|\n| C | D |\n"
        )
        self.assertEqual(
            (html, set()),
            adapter._auto_bind_cjk_html_tables(html, {"#/tables/0": self.identity}),
        )
        self.assertEqual(
            (markdown, set()),
            adapter._auto_bind_cjk_markdown_tables(
                markdown, {"#/tables/0": self.identity}
            ),
        )

    def test_explicit_blank_grid_matches_rendered_empty_cells(self) -> None:
        source = _source_table(
            2,
            2,
            [_cell(0, 0, "A"), _cell(1, 1, "D")],
            grid=[["A", ""], ["", "D"]],
        )
        identity = adapter._table_source_topology_identity(source)
        self.assertIsNotNone(identity)
        html = "<html><body><table><tr><td>A</td><td></td></tr><tr><td></td><td>D</td></tr></table></body></html>"
        markdown = "| A |  |\n|---|---|\n|  | D |\n"
        self.assertEqual(
            {"#/tables/0"},
            adapter._auto_bind_cjk_html_tables(html, {"#/tables/0": identity})[1],
        )
        self.assertEqual(
            {"#/tables/0"},
            adapter._auto_bind_cjk_markdown_tables(
                markdown, {"#/tables/0": identity}
            )[1],
        )

    def test_docling_blank_grid_cell_with_coordinates_is_supported(self) -> None:
        visible = [
            _cell(0, 0, "A"),
            _cell(0, 1, "B"),
            _cell(1, 0, "C"),
        ]
        blank = {
            **_cell(1, 1, ""),
            "bbox": None,
            "column_header": False,
            "row_header": False,
            "row_section": False,
            "fillable": False,
        }
        source = _source_table(
            2,
            2,
            visible,
            grid=[[visible[0], visible[1]], [visible[2], blank]],
        )
        identity = adapter._table_source_topology_identity(source)
        self.assertIsNotNone(identity)
        html = (
            "<html><body><table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>C</td><td></td></tr></table></body></html>"
        )
        self.assertEqual(
            {"#/tables/0"},
            adapter._auto_bind_cjk_html_tables(
                html, {"#/tables/0": identity}
            )[1],
        )

    def test_blank_grid_dict_must_match_slot_and_known_schema(self) -> None:
        visible = [
            _cell(0, 0, "A"),
            _cell(0, 1, "B"),
            _cell(1, 0, "C"),
        ]
        base_blank = {
            **_cell(1, 1, ""),
            "bbox": None,
            "column_header": False,
            "row_header": False,
            "row_section": False,
            "fillable": False,
        }
        for name, mutation in {
            "wrong_slot": {"start_col_offset_idx": 0},
            "non_unit_span": {"col_span": 2, "end_col_offset_idx": 3},
            "semantic_flag": {"column_header": True},
            "unknown_field": {"unexpected": "trusted"},
        }.items():
            with self.subTest(name=name):
                blank = {**base_blank, **mutation}
                source = _source_table(
                    2,
                    2,
                    visible,
                    grid=[[visible[0], visible[1]], [visible[2], blank]],
                )
                self.assertIsNone(adapter._table_source_topology_identity(source))

    def test_html_br_preserves_source_cell_line_boundary(self) -> None:
        source = _source_table(1, 1, [_cell(0, 0, "alpha\nbeta")])
        identity = adapter._table_source_topology_identity(source)
        self.assertIsNotNone(identity)
        html = (
            "<html><body><table><tr><td>alpha<br>beta</td></tr>"
            "</table></body></html>"
        )
        self.assertEqual(
            {"#/tables/0"},
            adapter._auto_bind_cjk_html_tables(
                html, {"#/tables/0": identity}
            )[1],
        )

    def test_hidden_raw_and_code_cell_text_cannot_auto_bind(self) -> None:
        source = _source_table(1, 1, [_cell(0, 0, "secret")])
        identity = adapter._table_source_topology_identity(source)
        self.assertIsNotNone(identity)
        for tag in (
            "script",
            "style",
            "template",
            "noscript",
            "textarea",
            "pre",
            "code",
        ):
            with self.subTest(tag=tag):
                html = (
                    "<html><body><table><tr><td>"
                    f"<{tag}>secret</{tag}>"
                    "</td></tr></table></body></html>"
                )
                bound, refs = adapter._auto_bind_cjk_html_tables(
                    html, {"#/tables/0": identity}
                )
                self.assertEqual(set(), refs)
                self.assertNotIn("data-source-ref", bound)

    def test_normal_inline_formatting_cell_text_still_auto_binds(self) -> None:
        source = _source_table(1, 1, [_cell(0, 0, "visible text")])
        identity = adapter._table_source_topology_identity(source)
        self.assertIsNotNone(identity)
        html = (
            "<html><body><table><tr><td>"
            "<strong>visible</strong> <em>text</em>"
            "</td></tr></table></body></html>"
        )
        self.assertEqual(
            {"#/tables/0"},
            adapter._auto_bind_cjk_html_tables(
                html, {"#/tables/0": identity}
            )[1],
        )

    def test_source_cell_text_must_be_scalar(self) -> None:
        source = _source_table(1, 1, [_cell(0, 0, "A")])
        source["data"]["table_cells"][0]["text"] = {"value": "A"}  # type: ignore[index]
        self.assertIsNone(adapter._table_source_topology_identity(source))

    def test_nonempty_missing_source_cell_does_not_become_blank(self) -> None:
        source = _source_table(
            2,
            2,
            [_cell(0, 0, "A"), _cell(1, 1, "D")],
            grid=[["A", "B"], ["", "D"]],
        )
        self.assertIsNone(adapter._table_source_topology_identity(source))

    def test_merged_topology_mismatch_fails_closed(self) -> None:
        source = _source_table(
            2,
            2,
            [_cell(0, 0, "H", colspan=2), _cell(1, 0, "A"), _cell(1, 1, "B")],
        )
        identity = adapter._table_source_topology_identity(source)
        assert identity
        html = "<html><body><table><tr><th>H</th><th>H</th></tr><tr><td>A</td><td>B</td></tr></table></body></html>"
        markdown = "| H | H |\n|---|---|\n| A | B |\n"
        self.assertEqual(
            set(),
            adapter._auto_bind_cjk_html_tables(html, {"#/tables/0": identity})[1],
        )
        self.assertEqual(
            set(),
            adapter._auto_bind_cjk_markdown_tables(
                markdown, {"#/tables/0": identity}
            )[1],
        )

    def test_merged_covered_grid_slots_are_not_synthetic_cells(self) -> None:
        source = _source_table(
            2,
            2,
            [_cell(0, 0, "H", colspan=2), _cell(1, 0, "A"), _cell(1, 1, "B")],
            # The covered slot may be represented as an empty grid value, but
            # it belongs to the spanning H cell rather than a new blank cell.
            grid=[["H", ""], ["A", "B"]],
        )
        identity = adapter._table_source_topology_identity(source)
        self.assertIsNotNone(identity)
        html = "<html><body><table><tr><th colspan=\"2\">H</th></tr><tr><td>A</td><td>B</td></tr></table></body></html>"
        self.assertEqual(
            {"#/tables/0"},
            adapter._auto_bind_cjk_html_tables(html, {"#/tables/0": identity})[1],
        )

    def test_resource_limit_fails_closed(self) -> None:
        html = "<html><body><table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table></body></html>"
        with patch.object(adapter, "_TABLE_BIND_MAX_DOCUMENT_CHARS", 16):
            bound, refs = adapter._auto_bind_cjk_html_tables(
                html, {"#/tables/0": self.identity}
            )
        self.assertEqual(html, bound)
        self.assertEqual(set(), refs)

    def test_non_body_code_and_appendix_tables_are_excluded(self) -> None:
        table = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>"
        html = (
            "<head>" + table + "</head>"
            "<body><pre>" + table + "</pre>"
            '<section class="docling-table-source-evidence-appendix">'
            + table
            + "</section>"
            + table
            + "</body>"
        )
        bound, refs = adapter._auto_bind_cjk_html_tables(
            html, {"#/tables/0": self.identity}
        )
        self.assertEqual({"#/tables/0"}, refs)
        self.assertEqual(1, bound.count('data-source-ref="#/tables/0"'))

    def test_strict_source_topology_rejects_loose_numbers(self) -> None:
        loose = _source_table(2, 2, self.cells)
        loose["data"]["num_rows"] = "02"  # type: ignore[index]
        self.assertIsNone(adapter._table_source_topology_identity(loose))
        loose = _source_table(2, 2, self.cells)
        loose["data"]["num_cols"] = True  # type: ignore[index]
        self.assertIsNone(adapter._table_source_topology_identity(loose))


if __name__ == "__main__":
    unittest.main()
