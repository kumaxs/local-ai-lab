from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402


class CjkFormulaBindingTests(unittest.TestCase):
    def test_formula_missing_by_surface_reports_side_and_union_contract(self) -> None:
        self.assertEqual(
            {
                "formula_source_missing_html_indexes": [2],
                "formula_source_missing_markdown_indexes": [],
                "formula_source_missing_indexes": [2],
            },
            adapter._formula_source_missing_indexes_by_surface(
                {1, 2}, {1}, {1, 2}
            ),
        )
        self.assertEqual(
            {
                "formula_source_missing_html_indexes": [1, 2],
                "formula_source_missing_markdown_indexes": [1, 2],
                "formula_source_missing_indexes": [1, 2],
            },
            adapter._formula_source_missing_indexes_by_surface(
                {1, 2}, set(), set()
            ),
        )

    def test_nested_second_pass_div_uses_complete_pre_payload(self) -> None:
        html = (
            "<html><body>"
            '<div class="docling-formula-second-pass" data-formula-index="10">'
            '<div class="docling-formula-second-pass-label">Formula 10 patched</div>'
            '<div class="docling-formula-render">\\[x = y \\quad ( 1 0 )\\]</div>'
            '<pre class="docling-formula-tex"><code>x = y \\quad ( 1 0 )</code></pre>'
            "</div>"
            "</body></html>"
        )

        self.assertEqual(
            {10: ["x=y"]},
            adapter._html_formula_occurrence_identities(html),
        )

    def test_mathml_annotation_excludes_source_span_disclosure_text(self) -> None:
        html = (
            "<html><body><div><math display=\"block\"><mrow>"
            "<mi>x</mi><mo>=</mo><mi>y</mi>"
            "</mrow><annotation encoding=\"TeX\">x = y "
            '<span class="docling-formula-source" data-formula-index="1">'
            '<a href="formulas/formula_1.png">source image</a>'
            "</span></annotation></math></div></body></html>"
        )

        self.assertEqual(
            {1: ["x=y"]},
            adapter._html_formula_occurrence_identities(html),
        )

    def test_append_binds_unique_inner_mathml_marker_in_place(self) -> None:
        html = (
            "<html><body><div><math display=\"block\"><mrow>"
            "<mi>x</mi><mo>=</mo><mi>y</mi>"
            "</mrow><annotation encoding=\"TeX\">x = y "
            '<span class="docling-formula-source" data-formula-index="1">'
            "source image</span></annotation></math></div></body></html>"
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "document.html").write_text(html, encoding="utf-8")
            candidate = {
                "formula_index": 1,
                "selected_image": "formulas/formula_1.png",
                "selected": "crop",
            }
            with patch.object(adapter, "_formula_indexed_candidates", return_value=[candidate]):
                result = adapter.append_formula_source_renderings(
                    output,
                    [{"text": "x=y"}],
                    expected_indexes={1},
                )
            rendered = (output / "document.html").read_text(encoding="utf-8")
        self.assertEqual([1], result["html_covered_indexes"])
        self.assertEqual([], result["html_appendix_indexes"])
        self.assertEqual(1, rendered.count("docling-formula-source-disclosure"))
        self.assertLess(rendered.index("</math>"), rendered.index("docling-formula-source-disclosure"))

    def test_duplicate_or_wrong_mathml_identity_fails_closed(self) -> None:
        duplicate = (
            "<html><body>"
            '<math><annotation encoding="TeX">x=y<span class="docling-formula-source" data-formula-index="1"/></annotation></math>'
            '<math><annotation encoding="TeX">x=y<span class="docling-formula-source" data-formula-index="1"/></annotation></math>'
            "</body></html>"
        )
        wrong = (
            "<html><body>"
            '<math><annotation encoding="TeX">x=z<span class="docling-formula-source" data-formula-index="1"/></annotation></math>'
            "</body></html>"
        )
        for html in (duplicate, wrong):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                (output / "document.html").write_text(html, encoding="utf-8")
                candidate = {
                    "formula_index": 1,
                    "selected_image": "formulas/formula_1.png",
                    "selected": "crop",
                }
                with patch.object(adapter, "_formula_indexed_candidates", return_value=[candidate]):
                    result = adapter.append_formula_source_renderings(
                        output,
                        [{"text": "x=y"}],
                        expected_indexes={1},
                    )
                rendered = (output / "document.html").read_text(encoding="utf-8")
            self.assertEqual([], result["html_covered_indexes"])
            self.assertEqual([1], result["html_appendix_indexes"])
            self.assertIn("Unmatched original formula renderings", rendered)

    def test_markdown_positional_fallback_requires_bidirectional_identity_uniqueness(self) -> None:
        candidate = {
            "formula_index": 1,
            "selected_image": "formulas/formula_1.png",
            "selected": "crop",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "document.md").write_text("$$x=y$$\n", encoding="utf-8")
            with patch.object(adapter, "_formula_indexed_candidates", return_value=[candidate]):
                unique = adapter.append_formula_source_renderings(
                    output, [{"text": "x=y"}], expected_indexes={1}
                )
            self.assertEqual([1], unique["markdown_covered_indexes"])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "document.md").write_text("$$x=y$$\n\n$$x=y$$\n", encoding="utf-8")
            with patch.object(adapter, "_formula_indexed_candidates", return_value=[candidate]):
                duplicate = adapter.append_formula_source_renderings(
                    output, [{"text": "x=y"}], expected_indexes={1}
                )
            self.assertEqual([], duplicate["markdown_covered_indexes"])
            self.assertEqual([1], duplicate["markdown_appendix_indexes"])

    def test_data_equation_and_anchor_comments_are_supported(self) -> None:
        html = (
            "<html><body>"
            '<div class="formula" data-equation="1">'
            "<details><summary>LaTeX</summary><code>x=y</code></details>"
            "</div>"
            '<div class="formula"><details><summary>LaTeX</summary>'
            "<code>z=1</code></details></div>"
            "<!-- source-formula-anchor:2 -->"
            "</body></html>"
        )

        self.assertEqual(
            {1: ["x=y"], 2: ["z=1"]},
            adapter._html_formula_occurrence_identities(html),
        )

    def test_empty_data_equation_does_not_poison_strong_anchor(self) -> None:
        html = (
            '<html><body><div class="formula" data-equation="">'
            "<details><summary>LaTeX</summary><code>x=y</code></details>"
            "</div><!-- source-formula-anchor:1 --></body></html>"
        )
        self.assertEqual({1: ["x=y"]}, adapter._html_formula_occurrence_identities(html))

    def test_display_equation_number_cannot_create_phantom_anchor_index(self) -> None:
        html = (
            '<html><body><div class="formula" data-equation="7">'
            "<details><summary>LaTeX</summary><code>x=y</code></details>"
            "</div><!-- source-formula-anchor:1 --></body></html>"
        )
        self.assertEqual({1: ["x=y"]}, adapter._html_formula_occurrence_identities(html))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "document.html").write_text(html, encoding="utf-8")
            candidate = {
                "formula_index": 1,
                "selected_image": "formulas/formula_1.png",
                "selected": "crop",
            }
            with patch.object(adapter, "_formula_indexed_candidates", return_value=[candidate]):
                result = adapter.append_formula_source_renderings(
                    output,
                    [{"text": "x=y"}],
                    expected_indexes={1},
                )
            rendered = (output / "document.html").read_text(encoding="utf-8")
        self.assertEqual([1], result["html_covered_indexes"])
        self.assertEqual([], result["html_identity_mismatch_indexes"])
        self.assertEqual([1], result["expected_indexes"])
        self.assertNotIn("source-formula-anchor:7", rendered)

    def test_alphanumeric_display_number_does_not_poison_strong_anchor(self) -> None:
        html = (
            '<html><body><div class="formula" data-equation="A.15">'
            "<details><summary>LaTeX</summary><code>x=y</code></details>"
            "</div><!-- source-formula-anchor:1 --></body></html>"
        )
        self.assertEqual(
            {1: ["x=y"]},
            adapter._html_formula_occurrence_identities(html),
        )

    def test_multiple_strong_markers_in_one_math_occurrence_fail_closed(self) -> None:
        invalid = "__invalid_formula_occurrence__"
        for inner_index, expected in (
            ("1", {1: [invalid]}),
            ("2", {1: [invalid], 2: [invalid]}),
        ):
            with self.subTest(inner_index=inner_index):
                html = (
                    '<math data-formula-index="1"><annotation encoding="TeX">'
                    "x=y"
                    '<span class="docling-formula-source" '
                    f'data-formula-index="{inner_index}"></span>'
                    "</annotation></math>"
                )
                self.assertEqual(
                    expected,
                    adapter._html_formula_occurrence_identities(html),
                )

    def test_nested_generic_formula_roots_fail_closed(self) -> None:
        invalid = "__invalid_formula_occurrence__"
        for inner_index, expected in (
            ("1", {1: [invalid, invalid]}),
            ("2", {1: [invalid], 2: [invalid]}),
        ):
            with self.subTest(inner_index=inner_index):
                html = (
                    '<div data-formula-index="1">'
                    f'<div data-formula-index="{inner_index}">x=y</div>'
                    "</div>"
                )
                self.assertEqual(
                    expected,
                    adapter._html_formula_occurrence_identities(html),
                )

    def test_anchor_comment_must_be_adjacent_to_its_formula_block(self) -> None:
        distant = (
            '<div class="formula"><details><summary>LaTeX</summary>'
            "<code>x=y</code></details></div>"
            "<p>正文</p><!-- source-formula-anchor:1 -->"
        )
        adjacent = (
            '<div class="formula"><details><summary>LaTeX</summary>'
            "<code>x=y</code></details></div>\n"
            "<!-- source-formula-anchor:1 -->"
        )

        self.assertEqual({}, adapter._html_formula_occurrence_identities(distant))
        self.assertEqual(
            {1: ["x=y"]},
            adapter._html_formula_occurrence_identities(adjacent),
        )

    def test_duplicate_conflicting_and_unclosed_markers_fail_closed(self) -> None:
        duplicate = (
            '<div class="formula" data-formula-index="1" '
            'data-formula-index="1"><code>x=y</code></div>'
        )
        conflicting = (
            '<div class="formula" data-formula-index="1" data-equation="2">'
            "<details><summary>LaTeX</summary><code>x=y</code></details></div>"
        )
        unclosed = (
            '<div class="formula" data-formula-index="1">'
            "<pre class=\"docling-formula-tex\">x=y</div>"
        )
        mismatched = (
            '<div class="formula" data-formula-index="1">'
            "<pre class=\"docling-formula-tex\">x=y</bad></pre></div>"
        )
        strong_anchor_conflict = (
            '<div class="formula" data-formula-index="1">'
            "<details><summary>LaTeX</summary><code>x=y</code></details>"
            "</div><!-- source-formula-anchor:2 -->"
        )
        naked_code = (
            '<div class="formula" data-formula-index="1">'
            "<code>x=y</code></div>"
        )

        invalid = "__invalid_formula_occurrence__"
        self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(duplicate))
        self.assertEqual(
            # data-equation is a display hint and is ignored when a strong
            # data-formula-index is present.
            {1: ["x=y"]},
            adapter._html_formula_occurrence_identities(conflicting),
        )
        self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(unclosed))
        self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(mismatched))
        self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(naked_code))
        self.assertEqual(
            {1: [invalid], 2: [invalid]},
            adapter._html_formula_occurrence_identities(strong_anchor_conflict),
        )

    def test_appendix_and_figure_formula_markers_do_not_count(self) -> None:
        html = (
            "<html><body>"
            '<div class="formula" data-formula-index="1">'
            "<details><summary>LaTeX</summary><code>x=y</code></details></div>"
            '<section class="docling-formula-source-evidence-appendix">'
            '<details class="docling-formula-source-disclosure" '
            'data-formula-index="1"><figure><code>wrong</code></figure>'
            "</details></section>"
            '<figure data-formula-index="1"><code>wrong-again</code></figure>'
            "</body></html>"
        )

        self.assertEqual(
            {1: ["x=y"]},
            adapter._html_formula_occurrence_identities(html),
        )

    def test_depth_node_and_text_budgets_fail_closed_without_crashing(self) -> None:
        valid = (
            '<div class="formula" data-formula-index="1">'
            "<details><summary>LaTeX</summary><code>x=y</code></details></div>"
        )
        deep = valid + "<div>" * 8 + "</div>" * 8
        many_nodes = valid + "<span></span>" * 8
        large_text = (
            '<div class="formula" data-formula-index="1">'
            "<details><summary>LaTeX</summary><code>x=y</code></details>"
            + ("X" * 16)
            + "</div>"
        )
        invalid = "__invalid_formula_occurrence__"

        with patch.object(adapter._FormulaHtmlOccurrenceParser, "_MAX_STACK_DEPTH", 4):
            self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(deep))
        with patch.object(adapter._FormulaHtmlOccurrenceParser, "_MAX_NODES", 6):
            self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(many_nodes))
        with patch.object(adapter._FormulaHtmlOccurrenceParser, "_MAX_TOTAL_TEXT_CHARS", 8):
            self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(large_text))
        with patch.object(adapter._FormulaHtmlOccurrenceParser, "_MAX_STORED_TEXT_CHARS", 2):
            self.assertEqual({1: [invalid]}, adapter._html_formula_occurrence_identities(valid))


if __name__ == "__main__":
    unittest.main()
