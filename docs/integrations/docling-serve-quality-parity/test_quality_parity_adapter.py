from __future__ import annotations

import json
import tempfile
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402
import formula_only_second_pass as formula_second_pass  # noqa: E402


class EnglishReviewPolishTests(unittest.TestCase):
    def test_image_only_pdf_finds_same_batch_text_layer_recovery_source(self) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "born-digital.pdf"
            scan = root / "rasterized-scan.pdf"
            other = root / "wrong-page-count.pdf"

            doc = fitz.open()
            for page_no in range(2):
                page = doc.new_page(width=300, height=400)
                for line_no in range(30):
                    page.insert_text(
                        (36, 30 + line_no * 11),
                        (
                            f"Recoverable source page {page_no + 1} line {line_no}. "
                            "citation formula reference paragraph with text layer."
                        ),
                        fontsize=8,
                    )
            doc.save(source)
            doc.close()

            src_doc = fitz.open(source)
            scan_doc = fitz.open()
            for page in src_doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
                new_page = scan_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(page.rect, pixmap=pix)
            scan_doc.save(scan)
            scan_doc.close()
            src_doc.close()

            wrong_doc = fitz.open()
            wrong_doc.new_page(width=300, height=400).insert_text((36, 80), "wrong " * 300)
            wrong_doc.save(other)
            wrong_doc.close()

            recovery = adapter.find_text_layer_recovery_source(scan)

        self.assertTrue(recovery["applied"])
        self.assertEqual(Path(recovery["source_path"]).name, "born-digital.pdf")
        self.assertEqual(recovery["reason"], "same_batch_text_layer_source_matched")
        self.assertLessEqual(recovery["page_size_distance"], 2.0)

    def test_non_image_only_pdf_does_not_use_text_layer_recovery(self) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "normal.pdf"
            doc = fitz.open()
            doc.new_page(width=300, height=400).insert_text((36, 80), "normal text " * 300)
            doc.save(source)
            doc.close()

            recovery = adapter.find_text_layer_recovery_source(source)

        self.assertFalse(recovery["applied"])
        self.assertEqual(recovery["reason"], "not_image_only_pdf")

    def test_cn_apply_all_uses_same_formula_policy_as_english(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/CN.pdf"),
            cn_ocr_parity=False,
            formula_second_pass_policy="apply-all",
        )

        self.assertTrue(adapter.effective_cn_ocr_parity(args))
        self.assertTrue(adapter.is_cn_accepted_path(args))
        self.assertEqual(adapter.effective_formula_second_pass_policy(args), "apply-all")

    def test_english_apply_all_policy_remains_isolated(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/two-col-arxiv-ai-lora.pdf"),
            cn_ocr_parity=False,
            formula_second_pass_policy="apply-all",
        )

        self.assertFalse(adapter.is_cn_accepted_path(args))
        self.assertEqual(adapter.effective_formula_second_pass_policy(args), "apply-all")

    def test_cn_baseline_rejects_final_output_with_too_few_visible_cn_chars(self) -> None:
        formulas = [
            {"label": "formula", "text": rf"x_{{{number}}} = y \quad ( {number} )"}
            for number in range(1, 25)
        ]
        document = {
            "texts": [
                {"label": "text", "text": "中" * adapter.CN_ACCEPTED_BASELINE["minimum_cn_character_count"]},
                *formulas,
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text("abc\n", encoding="utf-8")
            (output_dir / "document.html").write_text(
                "<html><body><p>abc</p></body></html>",
                encoding="utf-8",
            )
            diagnostics = adapter.cn_accepted_baseline_diagnostics(output_dir)

        self.assertFalse(diagnostics["ok"])
        self.assertIn("final_markdown_cn_character_count=0", diagnostics["reasons"])
        self.assertIn("final_html_cn_character_count=0", diagnostics["reasons"])

    def test_cn_accepted_baseline_diagnostics(self) -> None:
        formulas = [
            {"label": "formula", "text": rf"x_{{{number}}} = y \quad ( {number} )"}
            for number in range(1, 25)
        ]
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "获取历史时刻知识状态的权重为"
                        + "知识状态与习题嵌入表示" * 1000
                    ),
                },
                *formulas,
            ]
        }
        final_text = (
            "获取历史时刻知识状态的权重为"
            + "知识状态与习题嵌入表示" * 1000
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                final_text,
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                f"<html><body><p>{final_text}</p></body></html>",
                encoding="utf-8",
            )

            diagnostics = adapter.cn_accepted_baseline_diagnostics(output_dir)

        self.assertTrue(diagnostics["ok"])
        self.assertEqual(diagnostics["gxx_count"], 0)
        self.assertEqual(diagnostics["formula_count"], 24)
        self.assertEqual(diagnostics["equation_numbers"], list(range(1, 25)))

    def test_cn_accepted_baseline_rejects_gxx_and_shifted_formula_sequence(self) -> None:
        formulas = [
            {"label": "formula", "text": rf"x_{{{number}}} = y \quad ( {number} )"}
            for number in range(1, 25)
        ]
        formulas[13]["text"] = r"x = y \quad ( 13 )"
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "/G01" + "知识状态与习题嵌入表示" * 1000,
                },
                *formulas,
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "获取历史时刻知识状态的权重为",
                encoding="utf-8",
            )

            diagnostics = adapter.cn_accepted_baseline_diagnostics(output_dir)

        self.assertFalse(diagnostics["ok"])
        self.assertIn("gxx_count=1", diagnostics["reasons"])
        self.assertIn("formula_equation_sequence_mismatch", diagnostics["reasons"])

    def test_autolinks_visible_plain_urls(self) -> None:
        html, count = adapter._autolink_plain_urls(
            '<p>Code at https://github.com/microsoft/LoRA .</p>'
        )

        self.assertEqual(count, 1)
        self.assertIn(
            '<a href="https://github.com/microsoft/LoRA">'
            "https://github.com/microsoft/LoRA</a>",
            html,
        )

    def test_links_mathml_formula_blocks_by_order(self) -> None:
        html, count = adapter.inject_formula_source_links_by_mathml_order(
            '<div><math display="block"></math></div>',
            {1: {"source": "formulas/formula_1.png", "context": "formulas/formula_1_context.png"}},
        )

        self.assertEqual(count, 1)
        self.assertIn('data-formula-index="1"', html)
        self.assertIn("formulas/formula_1_context.png", html)

    def test_footnote_diagnostics_flag_split_fragments(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Author ∗ Name",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 120,
                                "r": 124,
                                "t": 90,
                                "b": 85,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ]
        }

        diagnostics = adapter.footnote_review_diagnostics(document)

        self.assertEqual(len(diagnostics), 1)
        self.assertIn("isolated_numeric_footnote_fragment", diagnostics[0]["reasons"])
        self.assertIn("near_page_bottom_footnote", diagnostics[0]["reasons"])
        self.assertIn("anchor_content_marker_mismatch", diagnostics[0]["reasons"])

    def test_first_page_footnote_recovery_merges_hyphenated_fragments(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [{"page_no": 1, "bbox": {"l": 120, "r": 124, "t": 90, "b": 85}}],
                },
                {
                    "label": "footnote",
                    "text": "1 mance significantly as shown in Appendix A.",
                    "prov": [{"page_no": 1, "bbox": {"l": 108, "r": 271, "t": 79, "b": 60}}],
                },
                {
                    "label": "footnote",
                    "text": (
                        "Compared to V1, this draft includes better baselines, "
                        "fine-tuning boosts its perfor-"
                    ),
                    "prov": [{"page_no": 1, "bbox": {"l": 124, "r": 504, "t": 88, "b": 70}}],
                },
            ]
        }

        diagnostics = adapter.first_page_footnote_recovery_diagnostics(document)
        recoverable = [item for item in diagnostics if item.get("action") == "diagnostic_only_generic_quarantine_preferred"]
        evidence_only = [item for item in diagnostics if not item.get("safe_to_apply")]

        self.assertEqual(len(recoverable), 1)
        self.assertIn("performance significantly", recoverable[0]["recovered_text"])
        self.assertFalse(recoverable[0]["safe_to_apply"])
        self.assertEqual(evidence_only[-1]["footnote_number"], "0")

    def test_first_page_footnote_html_recovery_is_evidence_only(self) -> None:
        diagnostics = [
            {
                "page_no": 1,
                "footnote_number": "1",
                "lead_fragment": "Compared to V1, fine-tuning boosts its perfor-",
                "tail_fragment": "1 mance significantly as shown in Appendix A.",
                "recovered_text": (
                    "1 Compared to V1, fine-tuning boosts its performance significantly "
                    "as shown in Appendix A."
                ),
                "action": "html_recovery_preserve_original_fragments",
                "safe_to_apply": True,
            }
        ]
        document_html = (
            "<p>1 mance significantly as shown in Appendix A.</p>\n"
            "<p>Compared to V1, fine-tuning boosts its perfor-</p>"
        )

        updated, applied = adapter.apply_first_page_footnote_html_recovery(
            document_html,
            diagnostics,
        )

        self.assertEqual(updated, document_html)
        self.assertEqual(applied, [])
        self.assertFalse(diagnostics[0]["safe_to_apply"])
        self.assertEqual(diagnostics[0]["action"], "diagnostic_only_generic_quarantine_preferred")

    def test_formula_number_qc_recovers_spaced_number(self) -> None:
        formulas = [
            {
                "label": "formula",
                "text": r"x = y \quad ( 1 0 )",
                "prov": [{"page_no": 2}],
            }
        ]
        html = '<div><math display="block"><mi>x</mi><mo>=</mo><mi>y</mi></math></div>'

        diagnostics = adapter.formula_number_qc_diagnostics(formulas, html)

        self.assertEqual(len(diagnostics), 1)
        self.assertTrue(diagnostics[0]["safe_to_recover"])
        self.assertEqual(diagnostics[0]["recovered_number"], 10)
        self.assertIn("equation_number_recoverable_from_formula_text", diagnostics[0]["reasons"])

    def test_formula_tex_qc_sanitizes_bare_alignment_markers(self) -> None:
        formulas = [
            {
                "label": "formula",
                "text": r"m_i^\ell & = \bigoplus_j m_{ij}^\ell , & ( 1 2 )",
                "prov": [{"page_no": 5}],
            }
        ]

        diagnostics = adapter.formula_tex_qc_diagnostics(formulas)
        display_text, reasons = adapter.sanitize_formula_display_text(formulas[0]["text"])

        self.assertEqual(len(diagnostics), 1)
        self.assertIn("bare_alignment_marker_without_alignment_environment", reasons)
        self.assertNotIn("&", display_text)
        self.assertEqual(diagnostics[0]["action"], "sanitize_display_tex_preserve_raw_tex")

    def test_formula_tex_qc_unwraps_single_array_for_display(self) -> None:
        formula = (
            r"\begin{array} { r } { \min _ { G } \max _ { D } V ( D , G ) = "
            r"\mathbb { E } _ { x \sim p _ { d a t a } ( x ) } [ \log D ( x ) ] } "
            r"\end{array} \quad ( 1 )"
        )

        display_text, reasons = adapter.sanitize_formula_display_text(formula)

        self.assertIn("unwrapped_single_formula_array_for_display", reasons)
        self.assertNotIn(r"\begin{array}", display_text)
        self.assertNotIn("unnecessary_single_formula_array", adapter._formula_output_safety_reasons(formula))
        self.assertIn(r"\quad ( 1 )", display_text)

    def test_algorithm_array_is_not_rendered_as_formula(self) -> None:
        formula = (
            r"\begin{array}{lll}\text {Input:} & \alpha & \text {stepsize}\\"
            r"\\ \text {Output:} & \theta_t & \text {parameters}\\"
            r"\\ \mathbf{while}\ t < T & \text {do update} & \end{array}"
        )

        html = adapter._render_formula_fallback_html(
            {
                "formula_no": 3,
                "status": "final_output_unsafe",
                "route_b_candidate": formula,
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        self.assertIn("docling-algorithm-block", html)
        self.assertNotIn("docling-formula-render docling-formula-preserved-source", html)
        self.assertIn("Input:", html)

    def test_formula_renderer_preserves_raw_tex_when_display_is_sanitized(self) -> None:
        html = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "qc_formula_tex_safety",
                "markdown_after": r"$$m_i^\ell & = m_{ij}^\ell , & ( 1 2 )$$",
                "display_override": r"m_i^\ell = m_{ij}^\ell , ( 1 2 )",
                "raw_tex": r"m_i^\ell & = m_{ij}^\ell , & ( 1 2 )",
            },
            Path("/tmp/out"),
            Path("/tmp/out"),
        )

        self.assertIn(r"\[m_i^\ell = m_{ij}^\ell , ( 1 2 )\]", html)
        self.assertIn(r"m_i^\ell &amp; = m_{ij}^\ell , &amp; ( 1 2 )", html)
        self.assertIn("docling-formula-display-tex", html)

    def test_cn_polish_replaces_existing_second_pass_formula_block(self) -> None:
        original = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "replaced",
                "markdown_after": r"$$old \quad (12)$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )
        replacement = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "cn_final_polish",
                "markdown_after": r"$$new \quad (12)$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        duplicate = adapter._render_second_pass_formula_html(
            {
                "formula_no": 12,
                "status": "replaced",
                "markdown_after": r"$$duplicate \quad (12)$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )

        updated, changed = adapter._replace_existing_second_pass_formula_block(
            "<html><body>" + original + duplicate + "</body></html>",
            12,
            replacement,
        )

        self.assertTrue(changed)
        self.assertIn(r"new \quad (12)", updated)
        self.assertNotIn(r"old \quad (12)", updated)
        self.assertNotIn(r"duplicate \quad (12)", updated)
        self.assertEqual(updated.count('data-formula-index="12"'), 1)

    def test_cn_formula_sources_use_general_candidate_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guarded = root / "guarded"
            sidecar = root / "sidecar"
            guarded.mkdir()
            sidecar.mkdir()
            formulas = [
                {
                    "label": "formula",
                    "text": (
                        rf"x_{{{number}}} = y \quad ( {number} ) trailing"
                        if number == 1
                        else rf"x_{{{number}}} = y \quad ( {number} )"
                    ),
                }
                for number in range(1, 25)
            ]
            (guarded / "document.json").write_text(
                adapter.json.dumps({"texts": formulas}),
                encoding="utf-8",
            )
            replacement_log = [
                {
                    "formula_no": number,
                    "status": "replaced",
                    "route_b_candidate": rf"route_b_{{{number}}}",
                }
                for number in (3, 4, 5, 7, 8, 14, 16)
            ]
            (sidecar / "second_pass_summary.json").write_text(
                adapter.json.dumps({"replacement_log": replacement_log}),
                encoding="utf-8",
            )
            args = Namespace(
                formula_second_pass_guarded_fallback_dir=[
                    f"route-a-full={guarded}"
                ]
            )

            texts, sources = adapter._cn_accepted_formula_source_texts(args, sidecar)

        self.assertEqual(len(texts), 24)
        self.assertEqual(sources[2], "guarded_fallback_full")
        self.assertIn(sources[3], {"formula_second_pass", "guarded_fallback_full"})
        self.assertIn(sources[5], {"formula_second_pass", "guarded_fallback_full"})
        self.assertIn(sources[13], {"formula_second_pass", "guarded_fallback_full"})
        self.assertEqual(adapter._compact_formula_numbers(texts[1]), [1])

    def test_cn_default_sources_prefer_clean_route_b_for_first_formulas(self) -> None:
        if not adapter._default_cn_route_b_dirs() or not adapter._default_cn_guarded_fallback_dirs():
            self.skipTest("local CN default formula sources are not available")
        args = Namespace(formula_second_pass_guarded_fallback_dir=[])

        texts, sources = adapter._cn_accepted_formula_source_texts(
            args,
            Path("/tmp/nonexistent-sidecar"),
        )

        self.assertEqual(sources[1], "route_b")
        self.assertEqual(sources[2], "route_b")
        self.assertNotIn(r"_ { \, _ { p } }", texts[1])
        self.assertEqual(
            texts[2],
            r"q ^ { \prime } _ { t } = O ( q _ { t } ) \times W _ { q } \quad ( 2 )",
        )

    def test_formula_final_canonicalization_trims_noise_and_duplicate_number(self) -> None:
        text, repairs = formula_second_pass.canonicalize_formula_output(
            r"x = y \quad ( 9 ) \quad ( 9 ) "
            + " ".join([r"\mathfrak { m }"] * 12),
            9,
        )

        self.assertEqual(text, r"x = y \quad ( 9 )")
        self.assertIn("trimmed_hallucinated_suffix", repairs)
        self.assertEqual(adapter._compact_formula_numbers(text), [9])

    def test_formula_final_canonicalization_removes_low_information_trailing_array(self) -> None:
        text, repairs = formula_second_pass.canonicalize_formula_output(
            r"h' = \operatorname{ReLU}(x) \quad "
            r"\begin{array}{ll}{K_{t-1}}\\{\,}\end{array} ( 17 )",
            17,
        )

        self.assertEqual(text, r"h' = \operatorname{ReLU}(x) \quad ( 17 )")
        self.assertIn("trimmed_low_information_trailing_array", repairs)

    def test_formula_safety_rejects_cjk_and_identical_integral_limits(self) -> None:
        self.assertIn(
            "formula_contains_cjk_prose",
            adapter._formula_output_safety_reasons(r"x = y \quad \text{其中}"),
        )
        self.assertIn(
            "identical_integral_limits",
            adapter._formula_output_safety_reasons(r"x = \int_{t-1}^{t-1} f(t)"),
        )

    def test_formula_normalization_repairs_unambiguous_one_hot_zero(self) -> None:
        self.assertEqual(
            formula_second_pass.normalize_formula_candidate(
                r"q' = 0 ( q ) \times W_q"
            ),
            r"q' = O ( q ) \times W_q",
        )
        self.assertEqual(
            formula_second_pass.normalize_formula_candidate(
                r"e _ { _ { h \rightarrow p } } = x"
            ),
            r"e _ { h \rightarrow p } = x",
        )

    def test_formula_safety_rejects_malformed_wrapper_candidates(self) -> None:
        self.assertIn(
            "malformed_nested_subscript",
            adapter._formula_output_safety_reasons(r"c' _ { \, _ { p } } = O(c_p)"),
        )
        self.assertNotIn(
            "unnecessary_single_formula_array",
            adapter._formula_output_safety_reasons(
                r"\begin{array}{r} q' = O(q) \times W \end{array}"
            ),
        )

    def test_cn_html_sequence_completion_inserts_before_next_formula(self) -> None:
        formula_14 = adapter._render_second_pass_formula_html(
            {
                "formula_no": 14,
                "status": "cn_final_polish",
                "markdown_after": "$$fourteen \\quad ( 14 )$$",
            },
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
        )
        html_text = f"<html><body>{formula_14}</body></html>"

        updated, inserted = adapter._complete_cn_formula_html_sequence(
            html_text,
            Path("/tmp/out"),
            Path("/tmp/out/formula_second_pass"),
            {
                13: r"thirteen \quad ( 13 )",
                14: r"fourteen \quad ( 14 )",
            },
            {},
        )

        self.assertEqual(inserted, [13])
        self.assertLess(
            updated.index('data-formula-index="13"'),
            updated.index('data-formula-index="14"'),
        )

    def test_apply_all_review_counts_every_formula(self) -> None:
        formulas = [
            {"label": "formula", "text": r"x = y \quad ( 1 0 )", "prov": [{"page_no": 1}]},
            {"label": "formula", "text": r"z = q", "prov": [{"page_no": 1}]},
        ]
        number_diag = [
            {
                "index": 1,
                "safe_to_recover": True,
                "recovered_number": 10,
                "reasons": ["equation_number_recoverable_from_formula_text"],
            },
            {
                "index": 2,
                "safe_to_recover": False,
                "reasons": ["display_formula_missing_equation_number"],
            },
        ]

        review = adapter.formula_second_pass_apply_all_review(formulas, number_diag, [], [1])

        self.assertEqual(review["reviewed_count"], 2)
        self.assertEqual(review["enhanced_count"], 1)
        self.assertEqual(review["evidence_only_count"], 1)

    def test_formula_second_pass_apply_all_replaces_clean_formula(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 700, "b": 680}}],
                }
            ]
        }
        route_b = [
            {
                "text": r"x = y + z \quad (1)",
                "page_no": 1,
                "main_eq": 1,
                "bbox_norm": {"l": 20, "r": 200, "t": 100, "b": 120},
                "node": {},
            }
        ]

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual(log[0]["status"], "replaced")
        self.assertEqual(patched["texts"][0]["text"], r"x = y + z \quad (1)")

    def test_formula_second_pass_keeps_duplicate_equation_matches_anchored(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"a \quad (13)",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 100, "t": 500, "b": 480}}],
                },
                {
                    "label": "formula",
                    "text": r"b \quad (14)",
                    "prov": [{"page_no": 3, "bbox": {"l": 10, "r": 100, "t": 450, "b": 430}}],
                },
            ]
        }
        route_b = [
            {
                "text": r"a + c \quad (13)",
                "page_no": 3,
                "main_eq": 13,
                "bbox_norm": {"l": 20, "r": 200, "t": 680, "b": 720},
                "node": {},
            },
            {
                "text": r"b + d \quad (14)",
                "page_no": 3,
                "main_eq": 14,
                "bbox_norm": {"l": 20, "r": 200, "t": 780, "b": 820},
                "node": {},
            },
        ]

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual([entry["status"] for entry in log], ["replaced", "replaced"])
        self.assertEqual(patched["texts"][0]["text"], r"a + c \quad (13)")
        self.assertEqual(patched["texts"][1]["text"], r"b + d \quad (14)")

    def test_formula_matching_does_not_shift_candidate_downstream(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"first \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 760, "b": 740}}],
                },
                {
                    "label": "formula",
                    "text": r"second \quad (2)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 660, "b": 640}}],
                },
            ]
        }
        route_b_doc = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"converted_second \quad (2)",
                    "prov": [{"page_no": 1, "bbox": {"l": 20, "r": 200, "t": 360, "b": 400}}],
                }
            ]
        }
        route_b = formula_second_pass.extract_formulas(route_b_doc)

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertNotEqual(log[0]["status"], "replaced")
        self.assertEqual(log[0]["formula_no"], 1)
        self.assertEqual(log[1]["status"], "replaced")
        self.assertEqual(log[1]["formula_no"], 2)
        self.assertEqual(patched["texts"][0]["text"], r"first \quad (1)")
        self.assertEqual(patched["texts"][1]["text"], r"converted_second \quad (2)")

    def test_formula_markdown_fallback_stays_at_own_anchor(self) -> None:
        markdown = "$$first \\quad (1)$$\n\n$$second \\quad (2)$$"
        entries = [
            {
                "formula_no": 1,
                "anchor_id": "formula-1-page-1-order-0",
                "status": "suspicious_no_route_b_match",
                "fallback_reason": "second_pass_not_applied:no_match",
            },
            {
                "formula_no": 2,
                "anchor_id": "formula-2-page-1-order-1",
                "status": "replaced",
                "route_b_candidate": r"converted_second \quad (2)",
                "eq_number": 2,
            },
        ]

        updated = formula_second_pass.patch_document_md(markdown, [], entries)

        self.assertLess(
            updated.index("formula-second-pass-fallback"),
            updated.index("converted_second"),
        )
        self.assertIn("$$first \\quad (1)$$", updated)
        self.assertNotIn("converted_second \\quad (2)$$\n\n$$first", updated)

    def test_failed_latex_keeps_json_formula_and_records_fallback(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 700, "b": 680}}],
                }
            ]
        }
        route_b = formula_second_pass.extract_formulas(
            {
                "texts": [
                    {
                        "label": "formula",
                        "text": r"\frac{x}{y \quad (1)",
                        "prov": [{"page_no": 1, "bbox": {"l": 20, "r": 200, "t": 280, "b": 320}}],
                    }
                ]
            }
        )

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual(log[0]["status"], "render_failed_latex")
        self.assertIn("unclosed_brace", log[0]["fallback_reason"])
        self.assertEqual(patched["texts"][0]["text"], r"x = y \quad (1)")
        self.assertEqual(
            patched["texts"][0]["local_ai_lab_formula_second_pass"]["anchor_id"],
            "formula-1-page-1-order-0",
        )

    def test_crop_only_fallback_renders_at_source_anchor(self) -> None:
        original = (
            '<html><body><div><math><annotation>'
            '<span class="docling-formula-source" data-formula-index="1">'
            '<a href="formulas/formula_1_context.png">context crop</a>'
            "</span></annotation></math></div>"
            '<div><math><annotation>'
            '<span class="docling-formula-source" data-formula-index="2"></span>'
            "</annotation></math></div></body></html>"
        )
        entry = {
            "formula_no": 1,
            "anchor_id": "formula-1-page-1-order-0",
            "status": "suspicious_no_route_b_match",
            "fallback_reason": "second_pass_not_applied:no_match",
            "route_b_candidate": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            result = adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["fallback_indexes"], [1])
        self.assertIn('data-formula-index="1"', updated)
        self.assertIn("second_pass_not_applied:no_match", updated)
        self.assertLess(
            updated.index('data-formula-index="1"'),
            updated.index('data-formula-index="2"'),
        )

    def test_missing_html_formula_uses_local_text_neighborhood(self) -> None:
        original = (
            "<html><body>"
            "<p>Paragraph immediately before the omitted formula anchor.</p>"
            "<p>Paragraph immediately after the omitted formula anchor.</p>"
            '<div><math><annotation data-formula-index="2">second</annotation></math></div>'
            "</body></html>"
        )
        entry = {
            "formula_no": 1,
            "anchor_id": "formula-1-page-1-order-0",
            "status": "suspicious_no_route_b_match",
            "fallback_reason": "second_pass_not_applied:no_match",
            "anchor_nearby_before": [
                "Paragraph immediately before the omitted formula anchor."
            ],
            "anchor_nearby_after": [
                "Paragraph immediately after the omitted formula anchor."
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            result = adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        formula_at = updated.index('data-formula-index="1"')
        self.assertLess(updated.index("immediately before"), formula_at)
        self.assertLess(formula_at, updated.index("immediately after"))
        self.assertEqual(
            result["patch_sources"][1],
            "anchor-missing-local-neighborhood-after",
        )

    def test_final_html_replaces_original_mathml_without_duplicate(self) -> None:
        original = (
            "<html><body><p>Before.</p>"
            "<div><math><annotation encoding=\"TeX\">"
            r"x = y \quad ( 1 )"
            "</annotation></math></div><p>After.</p></body></html>"
        )
        entry = {
            "formula_no": 1,
            "status": "replaced",
            "route_a_text": r"x = y \quad ( 1 )",
            "route_b_candidate": "x = y",
            "markdown_after": r"$$x = y \quad ( 1 )$$",
            "eq_number": 1,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            result = adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(updated.count('data-formula-index="1"'), 1)
        self.assertNotIn("<math", updated)
        self.assertIn(r"\quad ( 1 )", updated)

    def test_final_html_recovers_equation_number_from_original_anchor(self) -> None:
        original = (
            "<html><body><div><math><annotation encoding=\"TeX\">"
            r"x = y \quad ( 7 )"
            "</annotation></math></div></body></html>"
        )
        entry = {
            "formula_no": 1,
            "status": "replaced",
            "route_a_text": "x = y",
            "route_b_candidate": "x = y",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertEqual(entry["eq_number"], 7)
        self.assertEqual(entry["equation_number_source"], "original_rendered_anchor")
        self.assertIn(r"\quad ( 7 )", updated)

    def test_equation_numbers_recover_only_inside_bounded_sequence(self) -> None:
        entries = [
            {"formula_no": 1, "eq_number": 1},
            {"formula_no": 2, "eq_number": None},
            {"formula_no": 3, "eq_number": None},
            {"formula_no": 4, "eq_number": 4},
            {"formula_no": 5, "eq_number": None},
        ]

        recovered = adapter._infer_bounded_equation_number_sequence(entries)

        self.assertEqual(recovered, [2, 3])
        self.assertEqual([entry.get("eq_number") for entry in entries], [1, 2, 3, 4, None])
        self.assertEqual(entries[2]["equation_number_source"], "bounded_rendered_sequence")

    def test_final_html_replaces_formula_image_at_same_anchor(self) -> None:
        original = (
            '<html><body><p>Before.</p><figure><img src="formula.png" '
            'alt="q = r (13)" /></figure><p>After.</p></body></html>'
        )
        entry = {
            "formula_no": 13,
            "status": "replaced",
            "route_a_text": "q = r (13)",
            "route_b_candidate": "q = r",
            "markdown_after": r"$$q = r \quad ( 13 )$$",
            "eq_number": 13,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(original, encoding="utf-8")
            adapter.patch_document_html_for_formula_second_pass(
                output_dir,
                output_dir / "formula_second_pass",
                [entry],
            )
            updated = (output_dir / "document.html").read_text(encoding="utf-8")

        self.assertNotIn("<figure", updated)
        self.assertLess(updated.index("Before."), updated.index('data-formula-index="13"'))
        self.assertLess(updated.index('data-formula-index="13"'), updated.index("After."))

    def test_final_html_gate_rejects_visible_offset_and_image_only_fallback(self) -> None:
        html_text = (
            "<html><head></head><body>"
            '<div class="docling-formula-second-pass docling-formula-fallback" '
            'data-formula-index="2" data-formula-fallback-reason="unsafe"></div>'
            '<div class="docling-formula-second-pass" data-formula-index="1">'
            r'<div class="docling-formula-render">\[x = y\]</div></div>'
            "</body></html>"
        )
        entries = [
            {"formula_no": 1, "status": "replaced", "display_override": "x = y"},
            {
                "formula_no": 2,
                "status": "unsafe",
                "fallback_reason": "unsafe",
                "route_a_text": "bad",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(html_text, encoding="utf-8")
            result = adapter.validate_formula_second_pass_html(output_dir, entries)

        self.assertFalse(result["ok"])
        self.assertTrue(result["visible_offset"])
        self.assertEqual(result["image_only_fallback_indexes"], [2])

    def test_final_html_gate_rejects_garbled_accepted_formula(self) -> None:
        entry = {
            "formula_no": 1,
            "status": "replaced",
            "display_override": "u n k n o w n = x",
        }
        html_text = (
            "<html><head>"
            '<script id="docling-formula-second-pass-mathjax"></script>'
            "</head><body>"
            '<div class="docling-formula-second-pass" data-formula-index="1">'
            r'<div class="docling-formula-render">\[u n k n o w n = x\]</div>'
            "</div></body></html>"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(html_text, encoding="utf-8")
            (output_dir / "document.md").write_text(
                "$$u n k n o w n = x$$",
                encoding="utf-8",
            )
            (output_dir / "document.json").write_text(
                adapter.json.dumps(
                    {"texts": [{"label": "formula", "text": "u n k n o w n = x"}]}
                ),
                encoding="utf-8",
            )
            result = adapter.validate_formula_second_pass_html(output_dir, [entry])

        self.assertFalse(result["ok"])
        self.assertEqual(result["garbled_formula_indexes"], [1])

    def test_formula_alignment_diagnostics_reports_all_second_pass_gaps(self) -> None:
        diagnostics = adapter.formula_second_pass_alignment_diagnostics(
            [
                {
                    "formula_no": 13,
                    "eq_number": 13,
                    "status": "suspicious_no_route_b_match",
                    "route_a_text": "(13)",
                    "route_b_candidate": None,
                    "reasons": ["number_only_missing_body"],
                    "page_no": 4,
                    "route_a_bbox": {"x_center": 700, "y_center": 500},
                },
                {
                    "formula_no": 14,
                    "eq_number": 13,
                    "status": "replaced",
                    "route_a_text": r"bad \quad (13)",
                    "route_b_candidate": r"good \quad (13)",
                    "reasons": ["apply_all_candidate"],
                    "page_no": 4,
                    "route_a_bbox": {"x_center": 700, "y_center": 560},
                },
            ],
            15,
        )

        self.assertFalse(diagnostics["all_formulas_attempted"])
        self.assertIn(15, diagnostics["missing_attempt_indexes"])
        self.assertEqual(diagnostics["sequence_mismatch_count"], 1)
        self.assertEqual(diagnostics["duplicate_equation_number_count"], 1)
        self.assertEqual(diagnostics["missing_body_number_only_count"], 1)
        self.assertEqual(diagnostics["image_formula_not_converted_count"], 1)

    def test_formula_second_pass_apply_all_fallbacks_bad_candidate(self) -> None:
        route_a = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (1)",
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 700, "b": 680}}],
                }
            ]
        }
        route_b = [
            {
                "text": "(1)",
                "page_no": 1,
                "main_eq": 1,
                "bbox_norm": {"l": 20, "r": 200, "t": 100, "b": 120},
                "node": {},
            }
        ]

        patched, log = formula_second_pass.patch_document_json(
            route_a,
            route_b,
            apply_all=True,
        )

        self.assertEqual(log[0]["status"], "route_b_candidate_failed_quality_gate")
        self.assertEqual(patched["texts"][0]["text"], r"x = y \quad (1)")

    def test_write_formula_latex_sources_outputs_raw_and_display_tex(self) -> None:
        formulas = [
            {
                "label": "formula",
                "text": r"m_i^\ell & = m_{ij}^\ell , & ( 1 2 )",
                "prov": [{"page_no": 5}],
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = adapter.write_formula_latex_sources(Path(tmpdir), formulas)
            text = (Path(tmpdir) / "formulas.tex").read_text()

        self.assertTrue(result["written"])
        self.assertIn("% raw_tex:", text)
        self.assertIn("% display_tex:", text)
        self.assertIn("&", text)

    def test_current_formula_display_fallback_replaces_final_html_formula_block(self) -> None:
        document = {
            "texts": [
                {
                    "label": "formula",
                    "text": (
                        r"A t t e n t i o n ( Q , K , V ) = s o f t m a x "
                        r"( \frac { Q K ^ { T } } { \sqrt { d _ { k } } } ) V \quad ( 1 )"
                    ),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.json").write_text(
                adapter.json.dumps(document),
                encoding="utf-8",
            )
            (output_dir / "document.html").write_text(
                (
                    "<html><body><div><math><mi>A</mi>"
                    '<annotation data-formula-index="1">raw</annotation>'
                    "</math></div></body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                (
                    r"$$A t t e n t i o n ( Q , K , V ) = s o f t m a x "
                    r"( \frac { Q K ^ { T } } { \sqrt { d _ { k } } } ) V \quad ( 1 )$$"
                    "\n"
                ),
                encoding="utf-8",
            )
            metadata = {}
            status = {"quality_signals": {}, "warnings": [], "success_class": "degraded_success"}
            args = Namespace(input_file=Path("attention.pdf"))

            result = adapter.apply_current_formula_display_fallback(
                output_dir,
                metadata,
                status,
                args,
                reason="test_no_route_b",
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            md_text = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertIn("docling-formula-second-pass", html_text)
        self.assertIn(r"\operatorname{Attention}", html_text)
        self.assertIn(r"\operatorname{Attention}", md_text)
        self.assertIn("current_formula_display_fallback", status["quality_signals"])

    def test_header_footer_qc_flags_page_edge_noise(self) -> None:
        document = {
            "texts": [
                {
                    "label": "page_footer",
                    "text": "2",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {
                                "l": 303,
                                "r": 308,
                                "t": 48,
                                "b": 40,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
                {
                    "label": "page_header",
                    "text": "arXiv:2506.22084v1  [cs.LG]",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 18,
                                "r": 36,
                                "t": 568,
                                "b": 223,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                },
            ]
        }

        diagnostics = adapter.header_footer_qc_diagnostics(document)

        self.assertEqual(len(diagnostics), 2)
        footer = diagnostics[0]
        header = diagnostics[1]
        self.assertIn("page_number", footer["reasons"])
        self.assertIn("template_or_publication_noise", header["reasons"])
        self.assertIn("rotated_margin_header", header["reasons"])

    def test_structural_quarantine_marks_edge_and_footnote_nodes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "page_footer",
                    "text": "2",
                    "prov": [{"page_no": 2, "bbox": {"l": 303, "r": 308, "t": 48, "b": 40}}],
                },
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [{"page_no": 1, "bbox": {"l": 120, "r": 124, "t": 90, "b": 85}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 2)
        self.assertEqual(qc["unresolved_footnote_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_page_footer")
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote")

    def test_structural_quarantine_marks_plain_text_bottom_footnote_candidate(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Introduction",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 240, "t": 705, "b": 680}}],
                },
                {
                    "label": "text",
                    "text": "1 Correspondence to: author@example.org",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 360, "t": 92, "b": 82}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote_candidate")
        self.assertIn("marker_led_footnote_content_candidate", qc["candidates"][0]["reasons"])

    def test_structural_qc_does_not_treat_edge_section_heading_as_footer(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "2.3 Model Architecture",
                    "prov": [{"page_no": 3, "bbox": {"l": 318, "r": 439, "t": 138, "b": 129}}],
                }
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 0)
        self.assertEqual(document["texts"][0]["label"], "section_header")

    def test_structural_qc_quarantines_text_rendered_inside_picture(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 700, "b": 520},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "AUTHORIZATION MD.! _ MP-75",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 200, "r": 300, "t": 670, "b": 650},
                        }
                    ],
                },
                {
                    "label": "caption",
                    "text": "Figure 1: Scanned business documents",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 120, "r": 480, "t": 510, "b": 495},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(qc["candidates"][0]["kind"], "visual_annotation")
        self.assertEqual(qc["candidates"][0]["confidence"], "high")
        self.assertEqual(document["texts"][0]["label"], "quarantined_visual_annotation")
        self.assertEqual(document["texts"][1]["label"], "caption")

    def test_structural_qc_quarantines_small_ocr_text_just_above_picture_bbox(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 700, "b": 520},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "AUTHORIZATION MD.! _ MP-75",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 208, "r": 251, "t": 767, "b": 762},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "A normal paragraph outside the figure annotation zone.",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 490, "b": 450},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        candidate = qc["candidates"][0]
        self.assertEqual(candidate["kind"], "visual_annotation")
        self.assertEqual(
            candidate["picture_overlap"]["region_match"],
            "small_text_in_expanded_picture_annotation_zone",
        )
        self.assertEqual(document["texts"][1]["label"], "text")

    def test_structural_qc_quarantines_table_ocr_spilling_left_and_above_picture(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 74, "r": 276, "t": 718, "b": 643},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "ASCA",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 15, "r": 31, "t": 753, "b": 746},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "(a) Oversegmented structure annotation",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 112, "r": 238, "t": 636, "b": 629},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_visual_annotation")
        self.assertEqual(document["texts"][1]["label"], "text")

    def test_structural_qc_quarantines_duplicate_text_inside_table_bbox(self) -> None:
        document = {
            "tables": [
                {
                    "label": "table",
                    "prov": [
                        {
                            "page_no": 4,
                            "bbox": {"l": 70, "r": 520, "t": 690, "b": 610},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "AP50 AP75",
                    "prov": [
                        {
                            "page_no": 4,
                            "bbox": {"l": 400, "r": 470, "t": 675, "b": 668},
                        }
                    ],
                }
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(qc["candidates"][0]["kind"], "table_visual_annotation")
        self.assertEqual(document["texts"][0]["label"], "quarantined_table_visual_annotation")

    def test_structural_qc_removes_source_disproved_suffix_with_visual_ocr_support(self) -> None:
        legitimate = (
            "We split the dataset randomly into train validation and test sets "
            "at the document level using a standard split. This results in many "
            "tables for training and fewer tables for testing. An example"
        )
        suffix = " sroup Android rohot eel enjoyable embarrassed"
        body_bbox = {
            "l": 308,
            "r": 547,
            "t": 147,
            "b": 79,
            "width": 239,
            "height": 68,
        }
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 58, "r": 280, "t": 719, "b": 569},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": legitimate + suffix,
                    "prov": [{"page_no": 6, "bbox": body_bbox}],
                },
                {
                    "label": "text",
                    "text": "Android robot",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 20, "r": 75, "t": 700, "b": 694},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "Feel enjoyable",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 18, "r": 70, "t": 682, "b": 676},
                        }
                    ],
                },
            ],
        }
        source_line = {
            "text": legitimate,
            "bbox": body_bbox,
            "source": "pdf_text_character_baseline",
        }

        with patch.object(adapter, "_source_page_text_lines", return_value=[source_line]):
            qc = adapter.structural_noise_qc(
                document,
                {"available": True, "pages": {6: {}}},
            )

        suffix_candidate = next(
            item
            for item in qc["candidates"]
            if item["kind"] == "reading_order_table_annotation"
        )
        self.assertEqual(suffix_candidate["match_mode"], "fragment")
        self.assertEqual(suffix_candidate["text"], suffix)
        self.assertGreaterEqual(
            suffix_candidate["source_grounding"]["supporting_visual_token_count"],
            2,
        )
        self.assertEqual(document["texts"][0]["label"], "text")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                (
                    "<html><body>"
                    f"<p>{legitimate + suffix}</p>"
                    "<p>Android robot</p><p>Feel enjoyable</p>"
                    "</body></html>"
                ),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"{legitimate + suffix}\n\nAndroid robot\n\nFeel enjoyable\n",
                encoding="utf-8",
            )
            with patch.object(
                adapter,
                "_source_page_text_lines",
                return_value=[source_line],
            ):
                result = adapter.apply_structural_quarantine_to_outputs(
                    output_dir,
                    document,
                    {"available": True, "pages": {6: {}}},
                )
            final_html = adapter._visible_html_text(
                adapter._html_without_structural_content(
                    (output_dir / "document.html").read_text(encoding="utf-8")
                )
            )
            final_markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertIn(legitimate, final_html)
        self.assertIn(legitimate, final_markdown)
        self.assertNotIn("sroup Android", final_html)
        self.assertNotIn("sroup Android", final_markdown)

    def test_structural_qc_quarantines_same_page_picture_annotation_shadow(self) -> None:
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 100, "r": 500, "t": 500, "b": 250},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": "Guernsey",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 200, "r": 250, "t": 400, "b": 390},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "Guernsey",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 600, "r": 650, "t": 400, "b": 390},
                        }
                    ],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 2)
        self.assertEqual(qc["candidates"][0]["kind"], "visual_annotation")
        self.assertEqual(qc["candidates"][1]["kind"], "visual_annotation_shadow")
        self.assertEqual(
            document["texts"][1]["label"],
            "quarantined_visual_annotation_shadow",
        )

    def test_structural_qc_quarantines_abrupt_visual_suffix_without_body(self) -> None:
        body = (
            "This is a long research paragraph describing the method and its "
            "evaluation in ordinary sentence case. " * 8
            + "The model combines textual and visual information for ACUTE TOXICITY IN MICE"
        )
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": body,
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 50, "r": 300, "t": 550, "b": 100},
                        }
                    ],
                }
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        candidate = qc["candidates"][0]
        self.assertEqual(candidate["kind"], "reading_order_annotation")
        self.assertEqual(candidate["match_mode"], "fragment")
        self.assertEqual(candidate["text"], " for ACUTE TOXICITY IN MICE")
        self.assertEqual(document["texts"][0]["label"], "text")

    def test_structural_fragment_quarantine_removes_only_visible_suffix(self) -> None:
        body = "The model combines textual and visual information for ACUTE TOXICITY IN MICE"
        item = {
            "kind": "reading_order_annotation",
            "text": " for ACUTE TOXICITY IN MICE",
            "page_no": 1,
            "reasons": ["abrupt_terminal_uppercase_fragment"],
            "match_mode": "fragment",
        }

        updated, changed = adapter._replace_html_fragment_with_quarantine(
            f"<html><body><p>{body}</p></body></html>",
            item,
        )

        self.assertTrue(changed)
        self.assertIn("<p>The model combines textual and visual information</p>", updated)
        self.assertNotIn("information for ACUTE TOXICITY", adapter._visible_html_text(updated))

    def test_structural_qc_quarantines_duplicate_shadow_of_page_header(self) -> None:
        document = {
            "texts": [
                {
                    "label": "page_header",
                    "text": "23",
                    "prov": [
                        {
                            "page_no": 23,
                            "bbox": {"l": 350, "r": 366, "t": 604, "b": 594},
                        }
                    ],
                },
                {
                    "label": "text",
                    "text": "23",
                    "prov": [
                        {
                            "page_no": 23,
                            "bbox": {"l": 353, "r": 364, "t": 604, "b": 594},
                        }
                    ],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 2)
        shadow = next(item for item in qc["candidates"] if item["kind"] == "page_header_shadow")
        self.assertEqual(shadow["confidence"], "high")
        self.assertIn(
            "duplicate_text_overlaps_labeled_structural_region",
            shadow["reasons"],
        )
        self.assertEqual(document["texts"][1]["label"], "quarantined_page_header_shadow")

    def test_structural_qc_writes_evidence_sidecar_and_audits_final_output(self) -> None:
        text = "1 Correspondence to: author@example.org"
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": text,
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 360, "t": 92, "b": 82}}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(f"\n{text}\n", encoding="utf-8")
            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            sidecar = adapter.json.loads(
                (output_dir / "structural_regions.json").read_text(encoding="utf-8")
            )
            content = adapter.json.loads(
                (output_dir / "structural_content.json").read_text(encoding="utf-8")
            )
            final_html = (output_dir / "document.html").read_text(encoding="utf-8")
            final_md = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertEqual(sidecar["quarantine_candidate_count"], 1)
        self.assertEqual(sidecar["candidates"][0]["confidence"], "high")
        self.assertEqual(content["record_count"], 1)
        self.assertEqual(content["records"][0]["kind"], "footnote")
        self.assertEqual(result["html_structural_content_count"], 1)
        self.assertEqual(result["markdown_structural_content_count"], 1)
        self.assertIn("Extracted structural and visual notes", final_html)
        self.assertIn("Correspondence to: author@example.org", final_html)
        self.assertIn("## Extracted structural and visual notes", final_md)
        self.assertIn(text, final_md)
        self.assertNotIn(text, adapter._visible_html_text(
            adapter._html_without_structural_content(final_html)
        ))

    def test_structural_content_exports_high_confidence_structural_and_visual_material(self) -> None:
        candidates = [
            {
                "kind": "page_header",
                "text": "Repeated conference header",
                "page_no": 2,
                "reading_order": 1,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
                "evidence_score": 6,
                "reasons": ["repeated_header"],
            },
            {
                "kind": "visual_annotation",
                "text": "Figure OCR noise",
                "page_no": 2,
                "reading_order": 2,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
                "evidence_score": 7,
                "reasons": ["inside_picture"],
            },
            {
                "kind": "page_footer",
                "text": "Uncertain footer",
                "page_no": 2,
                "reading_order": 3,
                "action": "diagnostic_only",
                "confidence": "medium",
                "evidence_score": 2,
                "reasons": ["bottom_zone"],
            },
        ]

        records = adapter._structural_export_records(candidates)

        self.assertEqual([record["kind"] for record in records], ["page_header", "visual_annotation"])
        self.assertEqual(records[0]["text"], "Repeated conference header")
        self.assertEqual(records[1]["text"], "Figure OCR noise")

    def test_structural_content_deduplicates_same_page_shadow(self) -> None:
        candidates = [
            {
                "kind": "page_footer",
                "text": "Proceedings footer",
                "page_no": 3,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
            },
            {
                "kind": "page_footer_shadow",
                "text": "Proceedings footer",
                "page_no": 3,
                "action": "quarantine_from_main_text_flow",
                "confidence": "high",
            },
        ]

        records = adapter._structural_export_records(candidates)

        self.assertEqual(len(records), 1)

    def test_note_group_reorders_marker_attached_to_continuation_line(self) -> None:
        records = [
            {
                "index": 1,
                "kind": "footnote",
                "text": "Compared to baseline, performance was signifi-",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 124, "r": 500, "t": 89, "b": 70},
            },
            {
                "index": 2,
                "kind": "footnote",
                "text": "1 cantly better in Appendix A.",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 108, "r": 270, "t": 80, "b": 60},
            },
        ]

        groups = adapter._build_structural_note_groups(records)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["marker"], "1")
        self.assertEqual(
            groups[0]["text"],
            "Compared to baseline, performance was significantly better in Appendix A.",
        )
        self.assertEqual(groups[0]["assembly_reason"], "marker_attached_to_continuation_line")

    def test_note_group_uses_source_baselines_to_separate_adjacent_markers(self) -> None:
        characters = []

        def add_line(text: str, x: float, baseline: float) -> None:
            for char in text:
                index = len(characters)
                characters.append(
                    {
                        "index": index,
                        "text": char,
                        "font_name": "Times",
                        "font_weight": 400,
                        "font_size": 6,
                        "bbox": {
                            "l": x,
                            "r": x + 3,
                            "b": baseline,
                            "t": baseline + 6,
                            "width": 3,
                            "height": 6,
                        },
                    }
                )
                x += 3

        add_line("0", 120, 87)
        add_line("Compared to V1, this draft is improved.", 124, 82)
        add_line("1 While GPT-3 performs signifi-", 121, 72)
        add_line("cantly better.", 108, 62)
        records = [
            {
                "index": 1,
                "kind": "footnote",
                "text": "0",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 120, "r": 124, "t": 93, "b": 86, "width": 4, "height": 7},
            },
            {
                "index": 2,
                "kind": "footnote",
                "text": "Compared to V1, this draft is improved. While GPT-3 performs signifi-",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 120, "r": 500, "t": 89, "b": 68, "width": 380, "height": 21},
            },
            {
                "index": 3,
                "kind": "footnote",
                "text": "1 cantly better.",
                "page_no": 1,
                "confidence": "high",
                "bbox": {"l": 108, "r": 270, "t": 78, "b": 60, "width": 162, "height": 18},
            },
        ]
        source = {
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": characters,
                }
            }
        }

        groups = adapter._build_structural_note_groups(records, source)

        self.assertEqual([(item["marker"], item["text"]) for item in groups], [
            ("0", "Compared to V1, this draft is improved."),
            ("1", "While GPT-3 performs significantly better."),
        ])

    def test_note_group_merges_cross_page_two_column_continuation(self) -> None:
        records = [
            {
                "index": 1,
                "kind": "footnote",
                "text": "4 Bidirectional Trans-",
                "page_no": 3,
                "confidence": "high",
                "bbox": {"l": 320, "r": 525, "t": 87, "b": 77},
            },
            {
                "index": 2,
                "kind": "footnote",
                "text": "5 Another note.",
                "page_no": 4,
                "confidence": "high",
                "bbox": {"l": 320, "r": 520, "t": 108, "b": 98},
            },
            {
                "index": 3,
                "kind": "footnote",
                "text": "former continues on the next page.",
                "page_no": 4,
                "confidence": "high",
                "bbox": {"l": 72, "r": 290, "t": 105, "b": 77},
            },
        ]

        groups = adapter._build_structural_note_groups(records)
        note = next(item for item in groups if item.get("marker") == "4")

        self.assertEqual(
            note["text"],
            "Bidirectional Transformer continues on the next page.",
        )
        self.assertEqual(note["continuation_pages"], [4])
        self.assertEqual(len(note["source_bboxes"]), 2)
        self.assertNotIn(
            "former continues on the next page.",
            [item["text"] for item in groups if item.get("marker") is None],
        )

    def test_note_reference_mapping_requires_unique_same_page_marker(self) -> None:
        notes = [
            {"note_id": "note-1", "page_no": 1, "marker": "1"},
            {"note_id": "note-2", "page_no": 2, "marker": "1"},
        ]
        references = [
            {
                "page_no": 1,
                "marker": "1",
                "node_text": "Body text 1",
                "confidence": "high",
            },
            {
                "page_no": 3,
                "marker": "1",
                "node_text": "Unresolved text 1",
                "confidence": "high",
            },
        ]

        mappings, unresolved = adapter._map_note_references(notes, references)

        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["note_id"], "note-1")
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["reason"], "note_marker_not_found")

    def test_symbol_note_references_are_not_grouped_together(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "A * B † C",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 0, "r": 100, "t": 30, "b": 0},
                        }
                    ],
                }
            ]
        }
        source = {
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": [
                        {"index": 0, "text": "A", "font_size": 10, "bbox": {"l": 5, "r": 10, "t": 20, "b": 10}},
                        {"index": 1, "text": "*", "font_size": 6, "bbox": {"l": 11, "r": 14, "t": 22, "b": 16}},
                        {"index": 2, "text": "B", "font_size": 10, "bbox": {"l": 16, "r": 21, "t": 20, "b": 10}},
                        {"index": 3, "text": "†", "font_size": 6, "bbox": {"l": 22, "r": 25, "t": 22, "b": 16}},
                        {"index": 4, "text": "C", "font_size": 10, "bbox": {"l": 27, "r": 32, "t": 20, "b": 10}},
                    ],
                }
            }
        }
        notes = [
            {"page_no": 1, "marker": "*"},
            {"page_no": 1, "marker": "†"},
        ]

        references = adapter._pdf_inline_note_references(document, source, notes)

        self.assertEqual([item["marker"] for item in references], ["*", "†"])

    def test_pdf_note_reference_can_anchor_missing_text_marker_by_geometry(self) -> None:
        document = {
            "texts": [
                {
                    "label": "title",
                    "text": "Graph neural retrieval",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 10, "r": 110, "t": 28, "b": 10},
                        }
                    ],
                }
            ]
        }
        source = {
            "available": True,
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": [
                        {"index": 0, "text": "l", "font_size": 10, "bbox": {"l": 102, "r": 106, "t": 22, "b": 12}},
                        {"index": 1, "text": "1", "font_size": 6, "bbox": {"l": 112, "r": 115, "t": 28, "b": 22}},
                    ],
                }
            },
        }
        notes = [{"page_no": 1, "marker": "1", "note_id": "docling-note-p1-1-1"}]

        references = adapter._pdf_inline_note_references(document, source, notes)
        mappings, unresolved = adapter._map_note_references(notes, references)
        html_text, html_count = adapter._link_note_references_in_html(
            "<h1>Graph neural retrieval</h1>",
            mappings,
        )
        markdown, markdown_count = adapter._link_note_references_in_markdown(
            "# Graph neural retrieval\n",
            mappings,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["anchor_mode"], "append_missing_marker")
        self.assertEqual(len(unresolved), 0)
        self.assertEqual(html_count, 1)
        self.assertIn('href="#docling-note-p1-1-1"', html_text)
        self.assertEqual(markdown_count, 1)
        self.assertIn('href="#docling-note-p1-1-1"', markdown)

    def test_first_page_publication_note_fallback_links_unique_code_note_to_title(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "Retrieving Complex Tables",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 500, "t": 710, "b": 675}}],
                },
                {
                    "label": "text",
                    "text": "Author One, Author Two",
                    "prov": [{"page_no": 1, "bbox": {"l": 150, "r": 450, "t": 665, "b": 645}}],
                },
                {
                    "label": "section_header",
                    "text": "ABSTRACT",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 120, "t": 600, "b": 590}}],
                },
            ]
        }
        notes = [
            {
                "page_no": 1,
                "marker": "1",
                "note_id": "docling-note-p1-1-1",
                "text": "Code and data are available at https://example.org/repo",
            }
        ]

        references = adapter._first_page_publication_note_references(document, notes, [])
        mappings, unresolved = adapter._map_note_references(notes, references)
        html_text, count = adapter._link_note_references_in_html(
            "<h1>Retrieving Complex Tables</h1><p>Author One, Author Two</p><h2>ABSTRACT</h2>",
            mappings,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["source"], "first_page_publication_note_fallback")
        self.assertEqual(references[0]["node_text"], "Retrieving Complex Tables")
        self.assertEqual(len(unresolved), 0)
        self.assertEqual(count, 1)
        self.assertIn('href="#docling-note-p1-1-1"', html_text)

    def test_first_page_publication_note_fallback_does_not_guess_generic_notes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "Paper Title",
                    "prov": [{"page_no": 1, "bbox": {"l": 80, "r": 500, "t": 710, "b": 675}}],
                }
            ]
        }
        notes = [
            {
                "page_no": 1,
                "marker": "1",
                "note_id": "docling-note-p1-1-1",
                "text": "These authors contributed equally.",
            }
        ]

        references = adapter._first_page_publication_note_references(document, notes, [])

        self.assertEqual(references, [])

    def test_note_reference_links_html_and_markdown_with_backlink_ids(self) -> None:
        mappings = [
            {
                "page_no": 1,
                "marker": "1",
                "node_text": "Body text 1",
                "note_id": "docling-note-p1-1-1",
                "reference_id": "docling-note-p1-1-1-ref-1",
            },
            {
                "page_no": 1,
                "marker": "2",
                "node_text": "Body text 1 and 2",
                "note_id": "docling-note-p1-2-1",
                "reference_id": "docling-note-p1-2-1-ref-1",
            },
            {
                "page_no": 1,
                "marker": "1",
                "node_text": "Body text 1 and 2",
                "note_id": "docling-note-p1-1-1",
                "reference_id": "docling-note-p1-1-1-ref-2",
            },
            {
                "page_no": 2,
                "marker": "3",
                "node_text": "Research & development 3",
                "note_id": "docling-note-p2-3-1",
                "reference_id": "docling-note-p2-3-1-ref-1",
            },
        ]

        html_text, html_count = adapter._link_note_references_in_html(
            "<html><body><p>Body text 1</p><p>Body text 1 and 2</p></body></html>",
            mappings,
        )
        markdown, markdown_count = adapter._link_note_references_in_markdown(
            "Body text 1\n\nBody text 1 and 2\n\nResearch &amp; development 3\n",
            mappings,
        )

        self.assertEqual(html_count, 3)
        self.assertIn('href="#docling-note-p1-1-1"', html_text)
        self.assertIn('id="docling-note-p1-1-1-ref-1"', html_text)
        self.assertEqual(markdown_count, 4)
        self.assertIn('href="#docling-note-p1-1-1"', markdown)
        self.assertIn('href="#docling-note-p2-3-1"', markdown)

    def test_html_note_links_match_heading_and_inline_emphasis(self) -> None:
        mappings = [
            {
                "page_no": 1,
                "marker": "*",
                "node_text": "Author Name *",
                "note_id": "docling-note-p1-star-1",
                "reference_id": "docling-note-p1-star-1-ref-1",
            },
            {
                "page_no": 2,
                "marker": "3",
                "node_text": "Use BERTBASE. 3",
                "note_id": "docling-note-p2-3-1",
                "reference_id": "docling-note-p2-3-1-ref-1",
            },
        ]

        html_text, count = adapter._link_note_references_in_html(
            "<h2>Author Name *</h2><p>Use <strong>BERT</strong>BASE. 3</p>",
            mappings,
        )

        self.assertEqual(count, 2)
        self.assertIn('id="docling-note-p1-star-1-ref-1"', html_text)
        self.assertIn('id="docling-note-p2-3-1-ref-1"', html_text)
        self.assertIn("<strong>BERT</strong>BASE", html_text)

    def test_markdown_note_links_ignore_semantic_emphasis_markers(self) -> None:
        mappings = [
            {
                "page_no": 1,
                "marker": "*",
                "node_text": "Yelong Shen * Shean Wang *",
                "note_id": "docling-note-p1-star-1",
                "reference_id": "docling-note-p1-star-1-ref-1",
            },
            {
                "page_no": 1,
                "marker": "*",
                "node_text": "Yelong Shen * Shean Wang *",
                "note_id": "docling-note-p1-star-1",
                "reference_id": "docling-note-p1-star-1-ref-2",
            },
        ]

        markdown, count = adapter._link_note_references_in_markdown(
            "**Yelong Shen** * **Shean Wang** *\n",
            mappings,
        )

        self.assertEqual(count, 2)
        self.assertEqual(markdown.count('href="#docling-note-p1-star-1"'), 2)
        self.assertIn("**Yelong Shen**", markdown)
        self.assertIn("**Shean Wang**", markdown)

    def test_markdown_note_link_matches_ordered_list_visible_text(self) -> None:
        mapping = {
            "page_no": 3,
            "marker": "1",
            "node_text": "The codebase is available at GitHub. 1",
            "note_id": "docling-note-p3-1-1",
            "reference_id": "docling-note-p3-1-1-ref-1",
        }

        markdown, count = adapter._link_note_references_in_markdown(
            "3. Prior contribution.\n4. The codebase is available at GitHub. 1\n",
            [mapping],
        )

        self.assertEqual(count, 1)
        self.assertIn('id="docling-note-p3-1-1-ref-1"', markdown)
        self.assertIn("4. The codebase", markdown)

    def test_bibliography_links_merge_cross_page_continuation_without_offset(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Prior work [1] and related systems [2,3] are compared.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 500, "t": 500, "b": 470}}],
                },
                {
                    "label": "text",
                    "text": "An unresolved citation [4] remains visible.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 500, "t": 460, "b": 430}}],
                },
                {
                    "label": "section_header",
                    "text": "References",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 200, "t": 700, "b": 680}}],
                },
                {
                    "label": "list_item",
                    "text": "Alpha, A.: First reference. Journal (2020) 1",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 650, "b": 620}}],
                },
                {
                    "label": "list_item",
                    "text": "Beta, B.: A reference split across pages. In: Proceedings of the",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 100, "b": 70}}],
                },
                {
                    "label": "list_item",
                    "text": "23rd Conference. pp. 10-20 (2021) 1",
                    "prov": [{"page_no": 3, "bbox": {"l": 50, "r": 500, "t": 700, "b": 670}}],
                },
                {
                    "label": "list_item",
                    "text": "Gamma, C.: Third reference. Journal (2022) 1",
                    "prov": [{"page_no": 3, "bbox": {"l": 50, "r": 500, "t": 650, "b": 620}}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)

        self.assertTrue(diagnostics["available"])
        self.assertEqual(diagnostics["reference_count"], 3)
        self.assertEqual(diagnostics["references"][1]["continuation_count"], 1)
        self.assertEqual(diagnostics["references"][2]["number"], 3)
        self.assertEqual(diagnostics["citation_count"], 2)
        self.assertEqual(diagnostics["linked_number_count"], 3)
        self.assertEqual(diagnostics["unresolved_citation_count"], 1)
        self.assertEqual(
            diagnostics["unresolved_citations"][0]["missing_reference_numbers"],
            [4],
        )
        self.assertEqual(
            document["texts"][5]["local_ai_lab_qc"]["bibliography_reference"]["role"],
            "cross_page_continuation",
        )

    def test_bibliography_links_html_markdown_and_backlinks(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Compare [1,2].",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 200, "t": 500, "b": 480}}],
                },
                {
                    "label": "section_header",
                    "text": "References",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 200, "t": 700, "b": 680}}],
                },
                {
                    "label": "list_item",
                    "text": "Alpha, A.: First reference. (2020) 1",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 650, "b": 620}}],
                },
                {
                    "label": "list_item",
                    "text": "Beta, B.: Second reference. (2021) 1",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 500, "t": 610, "b": 580}}],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, html_references, html_citations = adapter._link_bibliography_in_html(
            (
                "<html><head></head><body><p>Compare [1,2].</p>"
                "<h2>References</h2><ul>"
                "<li>Alpha, A.: First reference. (2020) 1</li>"
                "<li>Beta, B.: Second reference. (2021) 1</li>"
                "</ul></body></html>"
            ),
            diagnostics,
        )
        markdown, md_references, md_citations = adapter._link_bibliography_in_markdown(
            (
                "Compare [1,2].\n\n## References\n\n"
                "- Alpha, A.: First reference. (2020) 1\n"
                "- Beta, B.: Second reference. (2021) 1\n"
            ),
            diagnostics,
        )

        self.assertEqual((html_references, html_citations), (2, 1))
        self.assertIn('href="#docling-reference-1"', html_text)
        self.assertIn('href="#docling-reference-2"', html_text)
        self.assertIn('id="docling-reference-1"', html_text)
        self.assertIn('href="#docling-citation-1-1"', html_text)
        self.assertEqual((md_references, md_citations), (2, 1))
        self.assertIn('href="#docling-reference-1"', markdown)
        self.assertIn('2. <a id="docling-reference-2"></a>', markdown)
        self.assertIn('href="#docling-citation-2-1"', markdown)

    def test_bibliography_links_cross_page_editor_continuation_and_table_cells(self) -> None:
        document = {
            "texts": [
                {
                    "label": "list_item",
                    "text": "Baseline [1]",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "section_header",
                    "text": "References",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "list_item",
                    "text": "Alpha, A.: First reference. (2020)",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "list_item",
                    "text": "Yim, M.: A reference ending with editors",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "list_item",
                    "text": "(eds.) Document Analysis and Recognition. (2021)",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Zhang, Z.: Final reference. (2022)",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "table_cell",
                    "text": "Model [2,3]",
                    "prov": [],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)

        self.assertEqual(diagnostics["reference_count"], 3)
        self.assertEqual(diagnostics["references"][1]["source_list_indexes"], [1, 2])
        self.assertEqual(diagnostics["references"][2]["number"], 3)

        html_text, html_references, html_citations = adapter._link_bibliography_in_html(
            (
                "<h2>References</h2><ol>"
                "<li>Alpha, A.: First reference. (2020)</li>"
                "<li>Yim, M.: A reference ending with editors</li>"
                "<li>(eds.) Document Analysis and Recognition. (2021)</li>"
                "<li>Zhang, Z.: Final reference. (2022)</li></ol>"
                "<ul><li>Baseline [1]</li></ul>"
                "<table><tr><th>Model [2,3]</th></tr></table>"
            ),
            diagnostics,
        )
        markdown, md_references, md_citations = adapter._link_bibliography_in_markdown(
            (
                "## References\n\n"
                "1. Alpha, A.: First reference. (2020)\n"
                "2. Yim, M.: A reference ending with editors\n"
                "- (eds.) Document Analysis and Recognition. (2021)\n"
                "3. Zhang, Z.: Final reference. (2022)\n\n"
                "- Baseline [1]\n\n"
                "| Model [2,3] |\n| --- |\n"
            ),
            diagnostics,
        )

        self.assertEqual((html_references, html_citations), (3, 2))
        self.assertIn('id="docling-reference-3"', html_text)
        self.assertIn('href="#docling-reference-1"', html_text)
        self.assertIn('href="#docling-reference-2"', html_text)
        self.assertIn('href="#docling-reference-3"', html_text)
        self.assertEqual((md_references, md_citations), (3, 2))
        self.assertIn('href="#docling-reference-1"', markdown)
        self.assertIn('href="#docling-reference-2"', markdown)
        self.assertIn('href="#docling-reference-3"', markdown)

    def test_bibliography_preserves_explicit_marker_order_without_double_number(self) -> None:
        document = {
            "texts": [
                {"label": "text", "text": "See [2].", "prov": [{"page_no": 1}]},
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "First",
                    "marker": "[1]",
                    "enumerated": True,
                    "orig": "[1] First",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Third",
                    "marker": "[3]",
                    "enumerated": True,
                    "orig": "[3] Third",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Second",
                    "marker": "[2]",
                    "enumerated": True,
                    "orig": "[2] Second",
                    "prov": [{"page_no": 2}],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>See [2].</p><h2>References</h2><ol>"
                "<li style=\"list-style-type: '[1] ';\">First</li>"
                "<li style=\"list-style-type: '[3] ';\">Third</li>"
                "<li style=\"list-style-type: '[2] ';\">Second</li></ol>"
            ),
            diagnostics,
        )

        self.assertEqual([item["number"] for item in diagnostics["references"]], [1, 3, 2])
        self.assertEqual((references, citations), (3, 1))
        self.assertNotIn('<span class="docling-reference-number">', html_text)
        self.assertIn('id="docling-reference-2">Second', html_text)
        self.assertIn('href="#docling-reference-2"', html_text)

    def test_cn_bibliography_repairs_mixed_and_missing_citation_brackets(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "DKVMN（8］ 模型以及后续工作〔10 提出了改进。",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "参考文献：", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "［8］ Zhang et al. Dynamic memory.",
                    "orig": "［8］ Zhang et al. Dynamic memory.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "［10］ Zong et al. Mastery speed.",
                    "orig": "［10］ Zong et al. Mastery speed.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>DKVMN（8］ 模型以及后续工作〔10 提出了改进。</p>"
                "<h2>参考文献：</h2><ul>"
                "<li>［8］ Zhang et al. Dynamic memory.</li>"
                "<li>［10］ Zong et al. Mastery speed.</li></ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["reference_count"], 2)
        self.assertEqual((references, citations), (2, 2))
        self.assertNotIn("（8］", html_text)
        self.assertNotIn("〔10 ", html_text)
        self.assertNotIn('<span class="docling-reference-number">', html_text)
        self.assertIn('href="#docling-reference-8">8</a>', html_text)
        self.assertIn('href="#docling-reference-10">10</a>', html_text)

    def test_bibliography_links_general_bracket_numeric_ranges(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "相关研究[1~3]覆盖早期方法，增量实验［1～3］补充说明，DKVMN（8）模型随后出现。"
                        "引言综述依托智慧教育平台1~3］。"
                        "其中 i∈[1,t] 且 h∈［1,N］，O'=10[11] 不是文献引用。"
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "参考文献：", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［1］ First range reference.", "orig": "［1］ First range reference.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［2］ Middle range reference.", "orig": "［2］ Middle range reference.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［3］ Last range reference.", "orig": "［3］ Last range reference.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［8］ DKVMN reference.", "orig": "［8］ DKVMN reference.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>相关研究[1~3]覆盖早期方法，增量实验［1～3］补充说明，DKVMN（8）模型随后出现。"
                "引言综述依托智慧教育平台1~3］。"
                "其中 i∈[1,t] 且 h∈［1,N］，O'=10[11] 不是文献引用。</p>"
                "<h2>参考文献：</h2><ul>"
                "<li>［1］ First range reference.</li>"
                "<li>［2］ Middle range reference.</li>"
                "<li>［3］ Last range reference.</li>"
                "<li>［8］ DKVMN reference.</li>"
                "</ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 4)
        self.assertEqual((references, citations), (4, 4))
        self.assertIn('href="#docling-reference-1">1</a>~<a', html_text)
        self.assertIn('href="#docling-reference-2" aria-label="Reference 2"></a>', html_text)
        self.assertIn('href="#docling-reference-3">3</a>', html_text)
        self.assertIn('href="#docling-reference-8">8</a>', html_text)
        self.assertIn("ocr_missing_open_citation_bracket", diagnostics["citations"][3]["mapping_evidence"])
        self.assertIn("i∈[1,t]", html_text)
        self.assertIn("h∈［1,N］", html_text)
        self.assertIn("O'=10[11]", html_text)
        self.assertIn("general_bracket_numeric_citation", diagnostics["citations"][0]["mapping_evidence"])

    def test_cn_bibliography_links_ocr_malformed_author_and_model_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "1.2 时间相关表示",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "text",
                    "text": (
                        "TCN-KT［\"！ 模型融合了基础信息。CKT！12模型建模历史知识点。"
                        "MAFKT! 3］模型描述多尺度关系。李浩君等人「51使用双向GRU。"
                    ),
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "text",
                    "text": "GKT 10 模型利用图结构。Tong等人］利用空间关系。郑浩东等人【20使用知识图。",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "参考文献：", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［11］ 王璨，刘朝晖，等.TCN-KT：个人基础与遗忘融合的时间卷积知识追踪模型［J］.", "orig": "［11］ 王璨，刘朝晖，等.TCN-KT：个人基础与遗忘融合的时间卷积知识追踪模型［J］.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［12］ Shen Shuanghong. Convolutional knowledge tracing.", "orig": "［12］ Shen Shuanghong. Convolutional knowledge tracing.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［13］ 段建设，等. MAFKT：多尺度注意力融合知识追踪模型.", "orig": "［13］ 段建设，等. MAFKT：多尺度注意力融合知识追踪模型.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［15］ 李浩君，方璇，戴海容. 基于自注意力机制和双向 GRU 神经网络的深度知识追踪优化模型.", "orig": "［15］ 李浩君，方璇，戴海容. 基于自注意力机制和双向 GRU 神经网络的深度知识追踪优化模型.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［16］ Nakagawa. Graph-based knowledge tracing.", "orig": "［16］ Nakagawa. Graph-based knowledge tracing.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［19］ Tong Shiwei. Structure-based knowledge tracing.", "orig": "［19］ Tong Shiwei. Structure-based knowledge tracing.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "［20］ 郑浩东，马华，谢颖超，等. 融合遗忘因素与记忆门的图神经网络知识追踪模型.", "orig": "［20］ 郑浩东，马华，谢颖超，等. 融合遗忘因素与记忆门的图神经网络知识追踪模型.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, _references, citations = adapter._link_bibliography_in_html(
            (
                "<h2>1.2 时间相关表示</h2>"
                "<p>TCN-KT［\"！ 模型融合了基础信息。CKT！12模型建模历史知识点。"
                "MAFKT! 3］模型描述多尺度关系。李浩君等人「51使用双向GRU。</p>"
                "<p>GKT 10 模型利用图结构。Tong等人］利用空间关系。郑浩东等人【20使用知识图。</p>"
                "<h2>参考文献：</h2><ul>"
                "<li>［11］ 王璨，刘朝晖，等.TCN-KT：个人基础与遗忘融合的时间卷积知识追踪模型［J］.</li>"
                "<li>［12］ Shen Shuanghong. Convolutional knowledge tracing.</li>"
                "<li>［13］ 段建设，等. MAFKT：多尺度注意力融合知识追踪模型.</li>"
                "<li>［15］ 李浩君，方璇，戴海容. 基于自注意力机制和双向 GRU 神经网络的深度知识追踪优化模型.</li>"
                "<li>［16］ Nakagawa. Graph-based knowledge tracing.</li>"
                "<li>［19］ Tong Shiwei. Structure-based knowledge tracing.</li>"
                "<li>［20］ 郑浩东，马华，谢颖超，等. 融合遗忘因素与记忆门的图神经网络知识追踪模型.</li>"
                "</ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 7)
        self.assertEqual(citations, 7)
        for number in [11, 12, 13, 15, 16, 19, 20]:
            self.assertIn(f'href="#docling-reference-{number}">{number}</a>', html_text)
        self.assertIn("1.2 时间相关表示", html_text)
        self.assertNotIn('href="#docling-reference-1">1</a>.2', html_text)

    def test_bibliography_links_author_year_citations_without_malformed_digit_split(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Representations are discussed in [Olah, 2014]. "
                        "Sequence models are trained as in [Graves, 2013], "
                        "then attention follows [Bahdanau et al., 2015]."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "D. Bahdanau, K. Cho, and Y. Bengio. Neural machine translation. In ICLR, 2015.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "A. Graves. Generating sequences with recurrent neural networks. arXiv:1308.0850, 2013.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "C. Olah. Deep learning, NLP, and representations. Blog, 2014.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Representations are discussed in [Olah, 2014]. "
                "Sequence models are trained as in [Graves, 2013], "
                "then attention follows [Bahdanau et al., 2015].</p>"
                "<h2>References</h2><ol>"
                "<li>D. Bahdanau, K. Cho, and Y. Bengio. Neural machine translation. In ICLR, 2015.</li>"
                "<li>A. Graves. Generating sequences with recurrent neural networks. arXiv:1308.0850, 2013.</li>"
                "<li>C. Olah. Deep learning, NLP, and representations. Blog, 2014.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 3)
        self.assertEqual((references, citations), (3, 3))
        self.assertIn('href="#docling-reference-3">Olah, 2014</a>', html_text)
        self.assertIn('href="#docling-reference-2">Graves, 2013</a>', html_text)
        self.assertIn('href="#docling-reference-1">Bahdanau et al., 2015</a>', html_text)
        self.assertNotIn("[1]ah, 2014", html_text)

    def test_bibliography_links_parenthetical_and_narrative_author_year_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Pre-training helps (Dai and Le, 2015; Peters et al., 2018a; "
                        "Radford et al., 2018; Howard and Ruder, 2018). "
                        "Paraphrase results follow (Dolan and Brockett, 2005). "
                        "Unlike Radford et al. (2018), this is bidirectional; "
                        "Peters et al. (2018a) remains feature-based."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Andrew M Dai and Quoc V Le. 2015. Semi-supervised sequence learning.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "William B Dolan and Chris Brockett. 2005. Automatically constructing a corpus.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Jeremy Howard and Sebastian Ruder. 2018. Universal language model fine-tuning.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018a. Deep contextualized word representations.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018b. Dissecting contextual word embeddings.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Alec Radford, Karthik Narasimhan, Tim Salimans, and Ilya Sutskever. 2018. Improving language understanding.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Pre-training helps (Dai and Le, 2015; Peters et al., 2018a; "
                "Radford et al., 2018; Howard and Ruder, 2018). "
                "Paraphrase results follow (Dolan and Brockett, 2005). "
                "Unlike Radford et al. (2018), this is bidirectional; "
                "Peters et al. (2018a) remains feature-based.</p>"
                "<h2>References</h2><ol>"
                "<li>Andrew M Dai and Quoc V Le. 2015. Semi-supervised sequence learning.</li>"
                "<li>William B Dolan and Chris Brockett. 2005. Automatically constructing a corpus.</li>"
                "<li>Jeremy Howard and Sebastian Ruder. 2018. Universal language model fine-tuning.</li>"
                "<li>Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018a. Deep contextualized word representations.</li>"
                "<li>Matthew Peters, Mark Neumann, and Luke Zettlemoyer. 2018b. Dissecting contextual word embeddings.</li>"
                "<li>Alec Radford, Karthik Narasimhan, Tim Salimans, and Ilya Sutskever. 2018. Improving language understanding.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 4)
        self.assertEqual(diagnostics["linked_number_count"], 7)
        self.assertEqual((references, citations), (6, 4))
        self.assertIn('href="#docling-reference-1">Dai and Le, 2015</a>', html_text)
        self.assertIn('href="#docling-reference-4">Peters et al., 2018a</a>', html_text)
        self.assertIn('href="#docling-reference-6">Radford et al., 2018</a>', html_text)
        self.assertIn('href="#docling-reference-3">Howard and Ruder, 2018</a>', html_text)
        self.assertIn('href="#docling-reference-2">Dolan and Brockett, 2005</a>', html_text)
        self.assertIn('href="#docling-reference-6">Radford et al. (2018)</a>', html_text)
        self.assertIn('href="#docling-reference-4">Peters et al. (2018a)</a>', html_text)

    def test_bibliography_links_escaped_ampersand_author_year_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "Deep models are used in vision and speech "
                        "(Deng et al., 2013; Krizhevsky et al., 2012; "
                        "Hinton & Salakhutdinov, 2006; Hinton et al., 2012a; "
                        "Graves et al., 2013). RMSProp follows "
                        "(Tieleman & Hinton, 2012)."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Deng et al. 2013. ImageNet large scale visual recognition.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Alex Krizhevsky et al. 2012. ImageNet classification.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "G. Hinton and R. Salakhutdinov. 2006. Reducing data dimensionality.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Hinton et al. 2012a. Deep neural networks for acoustic modeling.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Graves et al. 2013. Speech recognition with deep recurrent neural networks.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "Tieleman and Hinton. 2012. Lecture 6.5 RMSProp.", "prov": [{"page_no": 2}]},
            ]
        }
        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Deep models are used in vision and speech "
                "(Deng et al., 2013; Krizhevsky et al., 2012; "
                "Hinton &amp; Salakhutdinov, 2006; Hinton et al., 2012a; "
                "Graves et al., 2013). RMSProp follows "
                "(Tieleman &amp; Hinton, 2012).</p>"
                "<h2>References</h2><ol>"
                "<li>Deng et al. 2013. ImageNet large scale visual recognition.</li>"
                "<li>Alex Krizhevsky et al. 2012. ImageNet classification.</li>"
                "<li>G. Hinton and R. Salakhutdinov. 2006. Reducing data dimensionality.</li>"
                "<li>Hinton et al. 2012a. Deep neural networks for acoustic modeling.</li>"
                "<li>Graves et al. 2013. Speech recognition with deep recurrent neural networks.</li>"
                "<li>Tieleman and Hinton. 2012. Lecture 6.5 RMSProp.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual((references, citations), (6, 2))
        self.assertIn('href="#docling-reference-3">Hinton &amp; Salakhutdinov, 2006</a>', html_text)
        self.assertIn('href="#docling-reference-6">Tieleman &amp; Hinton, 2012</a>', html_text)
        self.assertIn('href="#docling-reference-1">Deng et al., 2013</a>', html_text)

    def test_bibliography_disambiguates_et_al_from_same_author_year(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Speech models improved rapidly (Graves et al., 2013).",
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "Graves, Alex. Generating sequences with recurrent neural networks. arXiv preprint arXiv:1308.0850, 2013.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "Graves, Alex, Mohamed, Abdel-rahman, and Hinton, Geoffrey. Speech recognition with deep recurrent neural networks. ICASSP, 2013.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Speech models improved rapidly (Graves et al., 2013).</p>"
                "<h2>References</h2><ol>"
                "<li>Graves, Alex. Generating sequences with recurrent neural networks. arXiv preprint arXiv:1308.0850, 2013.</li>"
                "<li>Graves, Alex, Mohamed, Abdel-rahman, and Hinton, Geoffrey. Speech recognition with deep recurrent neural networks. ICASSP, 2013.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual((references, citations), (2, 1))
        self.assertIn('href="#docling-reference-2">Graves et al., 2013</a>', html_text)

    def test_bibliography_links_comma_separated_author_year_citations(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": (
                        "RNNs were widely used for NLP "
                        "[Hochreiter and Schmidhuber, 1997, Sutskever et al., 2014]."
                    ),
                    "prov": [{"page_no": 1}],
                },
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {
                    "label": "list_item",
                    "text": "S. Hochreiter and J. Schmidhuber. Long short-term memory. Neural computation, 1997.",
                    "prov": [{"page_no": 2}],
                },
                {
                    "label": "list_item",
                    "text": "I. Sutskever, O. Vinyals, and Q. V. Le. Sequence to sequence learning. 2014.",
                    "prov": [{"page_no": 2}],
                },
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>RNNs were widely used for NLP "
                "[Hochreiter and Schmidhuber, 1997, Sutskever et al., 2014].</p>"
                "<h2>References</h2><ol>"
                "<li>S. Hochreiter and J. Schmidhuber. Long short-term memory. Neural computation, 1997.</li>"
                "<li>I. Sutskever, O. Vinyals, and Q. V. Le. Sequence to sequence learning. 2014.</li>"
                "</ol>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 1)
        self.assertEqual(diagnostics["linked_number_count"], 2)
        self.assertEqual((references, citations), (2, 1))
        self.assertIn('href="#docling-reference-1">Hochreiter and Schmidhuber, 1997</a>', html_text)
        self.assertIn('href="#docling-reference-2">Sutskever et al., 2014</a>', html_text)

    def test_bibliography_allows_numeric_citation_after_year_with_space(self) -> None:
        document = {
            "texts": [
                {"label": "text", "text": "It started in 1952 [2], but O'=10[11] is an index.", "prov": [{"page_no": 1}]},
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "[2] The chemical basis of morphogenesis.", "orig": "[2] The chemical basis of morphogenesis.", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "[11] A separate reference.", "orig": "[11] A separate reference.", "prov": [{"page_no": 2}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>It started in 1952 [2], but O'=10[11] is an index.</p>"
                "<h2>References</h2><ul>"
                "<li>[2] The chemical basis of morphogenesis.</li>"
                "<li>[11] A separate reference.</li></ul>"
            ),
            diagnostics,
        )

        self.assertEqual(diagnostics["citation_count"], 1)
        self.assertEqual((references, citations), (2, 1))
        self.assertIn('href="#docling-reference-2">2</a>', html_text)
        self.assertIn("O'=10[11]", html_text)

    def test_bibliography_keeps_numbered_section_headers_inside_references(self) -> None:
        document = {
            "texts": [
                {"label": "text", "text": "Later tools [24] and software [26] were shared [27,28].", "prov": [{"page_no": 1}]},
                {"label": "section_header", "text": "References", "prov": [{"page_no": 2}]},
                {"label": "list_item", "text": "1. Early reference.", "orig": "1. Early reference.", "prov": [{"page_no": 2}]},
                {"label": "section_header", "text": "24. GCG software suite", "prov": [{"page_no": 3}]},
                {"label": "section_header", "text": "26. Sequence manipulation suites", "prov": [{"page_no": 3}]},
                {"label": "section_header", "text": "27. Software-sharing movement", "prov": [{"page_no": 3}]},
                {"label": "section_header", "text": "28. Open software culture", "prov": [{"page_no": 3}]},
            ]
        }

        diagnostics = adapter.bibliography_diagnostics(document)
        html_text, references, citations = adapter._link_bibliography_in_html(
            (
                "<p>Later tools [24] and software [26] were shared [27,28].</p>"
                "<h2>References</h2>"
                "<li>1. Early reference.</li>"
                "<h2>24. GCG software suite</h2>"
                "<h2>26. Sequence manipulation suites</h2>"
                "<h2>27. Software-sharing movement</h2>"
                "<h2>28. Open software culture</h2>"
            ),
            diagnostics,
        )

        self.assertEqual([item["number"] for item in diagnostics["references"]], [1, 24, 26, 27, 28])
        self.assertEqual(diagnostics["citation_count"], 3)
        self.assertEqual(diagnostics["linked_number_count"], 4)
        self.assertEqual((references, citations), (5, 3))
        self.assertIn('id="docling-reference-24"', html_text)
        self.assertIn('href="#docling-reference-26">26</a>', html_text)

    def test_appendix_mentions_link_to_existing_appendix_heading(self) -> None:
        html_text, count = adapter._link_appendix_references_in_html(
            (
                '<p>Prior work <a href="#docling-reference-1">Wang et al., 2018a</a>. '
                "Detailed descriptions are included in Appendix B.1.</p>"
                "<h2>B.1 Detailed Descriptions for the GLUE Benchmark Experiments.</h2>"
            )
        )

        self.assertEqual(count, 1)
        self.assertIn('id="docling-appendix-b-1"', html_text)
        self.assertIn('<a href="#docling-reference-1">Wang et al., 2018a</a>', html_text)
        self.assertIn('href="#docling-appendix-b-1">Appendix B.1</a>', html_text)

    def test_formula_second_pass_removes_adjacent_original_mathml_duplicate(self) -> None:
        html_text, count = adapter._remove_adjacent_original_formula_duplicates(
            (
                '<div class="docling-formula-second-pass" data-formula-index="2">'
                "<div>Formula 2</div></div>\n"
                '<div><math display="block"><mi>q</mi></math></div>'
                "<p>Body text</p>"
                '<div><math display="block"><mi>x</mi></math></div>'
            ),
            {2},
        )

        self.assertEqual(count, 1)
        self.assertNotIn("<mi>q</mi>", html_text)
        self.assertIn("<mi>x</mi>", html_text)

    def test_html_superscript_note_candidate_tolerates_marker_spacing(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Yelong Shen ∗ Shean Wang",
                    "prov": [{"page_no": 1}],
                }
            ]
        }
        notes = [{"page_no": 1, "marker": "*", "note_id": "docling-note-p1-star-1"}]
        candidates = adapter._html_inline_note_references(
            document,
            '<p>Yelong Shen<sup class="docling-footnote-ref">∗</sup>Shean Wang</p>',
            notes,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["marker"], "*")
        self.assertEqual(candidates[0]["source"], "final_html_sup_element_and_same_page_node")

    def test_empty_table_with_caption_uses_source_crop_as_visible_fallback(self) -> None:
        document = {
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "caption",
                    "text": "Figure 1. Presented table.",
                    "prov": [{"page_no": 1}],
                }
            ],
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "label": "table",
                    "captions": [{"$ref": "#/texts/0"}],
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                    "prov": [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 100, "b": 50}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "tables").mkdir()
            (output_dir / "tables" / "table_1.png").write_bytes(b"png")
            (output_dir / "document.html").write_text(
                "<table><caption><div class=\"caption\">"
                "Figure 1. Presented table.</div></caption></table>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "Figure 1. Presented table.\n",
                encoding="utf-8",
            )

            result = adapter.inject_empty_table_visual_fallbacks(
                output_dir,
                document,
                document["tables"],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["html_applied_count"], 1)
        self.assertEqual(result["markdown_applied_count"], 1)
        self.assertIn("docling-table-visual-fallback", html_text)
        self.assertIn("tables/table_1.png", html_text)
        self.assertIn("![Figure 1. Presented table.](tables/table_1.png)", markdown)

    def test_footnote_superscript_polish_does_not_split_adjacent_words(self) -> None:
        updated, count = adapter._polish_footnote_superscripts(
            "<p>Yelong Shen ∗ Shean Wang</p>"
        )

        self.assertEqual(count, 1)
        self.assertIn("Shen<sup", updated)
        self.assertIn("</sup>Shean", updated)
        self.assertNotIn("She n", updated)

    def test_author_region_reorders_misplaced_author_and_splits_body_tail(self) -> None:
        document = {
            "texts": [
                {"label": "section_header", "text": "TITLE", "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 300, "t": 700, "b": 680}}]},
                {"label": "section_header", "text": "Petar *", "prov": [{"page_no": 1, "bbox": {"l": 60, "r": 150, "t": 660, "b": 650}}]},
                {"label": "text", "text": "Department A", "prov": [{"page_no": 1, "bbox": {"l": 60, "r": 180, "t": 640, "b": 630}}]},
                {"label": "text", "text": "Guillem * Centre B", "prov": [{"page_no": 1, "bbox": {"l": 320, "r": 500, "t": 660, "b": 645}}]},
                {"label": "text", "text": "Department A", "prov": [{"page_no": 1, "bbox": {"l": 320, "r": 500, "t": 650, "b": 640}}]},
                {"label": "text", "text": "g@example.org based on its state in every layer.", "prov": [{"page_no": 1, "bbox": {"l": 320, "r": 500, "t": 640, "b": 630}}]},
                {"label": "section_header", "text": "ABSTRACT", "prov": [{"page_no": 1, "bbox": {"l": 250, "r": 350, "t": 500, "b": 490}}]},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                "<h1>TITLE</h1><h2>Petar *</h2><p>Department A</p>"
                "<h2>ABSTRACT</h2><p>Body.</p><p>Guillem * Centre B</p>"
                "<p>Department A</p>"
                "<p>g@example.org based on its state in every layer.</p>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "# TITLE\n\n## Petar <sup><a href=\"#note\">*</a></sup>\n\n"
                "Department A\n\n## ABSTRACT\n\nBody.\n\n"
                "**Guillem** <sup><a href=\"#note\">*</a></sup> Centre B\n\n"
                "Department A\n\n"
                "g@example.org based on its state in every layer.\n",
                encoding="utf-8",
            )

            result = adapter.recover_first_page_author_reading_order(
                output_dir,
                document,
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertEqual(result["author_record_count"], 5)
        self.assertEqual(result["markdown_record_replacement_count"], 5)
        self.assertLess(html_text.index("Guillem"), html_text.index("ABSTRACT"))
        self.assertLess(html_text.index("g@example.org"), html_text.index("ABSTRACT"))
        self.assertEqual(html_text.count("Department A"), 2)
        self.assertLess(markdown.index("**Guillem**"), markdown.index("## ABSTRACT"))
        self.assertIn('<a href="#note">*</a>', markdown)
        self.assertGreater(
            html_text.index("based on its state in every layer."),
            html_text.index("ABSTRACT"),
        )

    def test_first_page_abstract_reorders_before_two_column_frontmatter(self) -> None:
        document = {
            "texts": [
                {"label": "title", "text": "Title", "prov": [{"page_no": 1, "bbox": {"l": 75, "r": 530, "t": 700, "b": 670}}]},
                {"label": "section_header", "text": "CCS CONCEPTS", "prov": [{"page_no": 1, "bbox": {"l": 318, "r": 400, "t": 543, "b": 534}}]},
                {"label": "section_header", "text": "KEYWORDS", "prov": [{"page_no": 1, "bbox": {"l": 318, "r": 380, "t": 483, "b": 474}}]},
                {"label": "section_header", "text": "1 INTRODUCTION", "prov": [{"page_no": 1, "bbox": {"l": 318, "r": 420, "t": 346, "b": 337}}]},
                {"label": "section_header", "text": "ABSTRACT", "prov": [{"page_no": 1, "bbox": {"l": 54, "r": 112, "t": 543, "b": 534}}]},
                {
                    "label": "text",
                    "text": "Large language models are evaluated on structured table data.",
                    "prov": [{"page_no": 1, "bbox": {"l": 54, "r": 296, "t": 528, "b": 280}}],
                },
                {
                    "label": "text",
                    "text": "∗ Contribution note.",
                    "prov": [{"page_no": 1, "bbox": {"l": 54, "r": 296, "t": 255, "b": 239}}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                "<h1>Title</h1><h2>CCS CONCEPTS</h2><p>Concepts</p>"
                "<h2>KEYWORDS</h2><p>tables</p><h2>1 INTRODUCTION</h2><p>Intro.</p>"
                "<h2>ABSTRACT</h2><p>Large language models are evaluated on structured table data.</p>"
                "<p>∗ Contribution note.</p>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "# Title\n\n## CCS CONCEPTS\n\nConcepts\n\n## KEYWORDS\n\ntables\n\n"
                "## 1 INTRODUCTION\n\nIntro.\n\n## ABSTRACT\n\n"
                "Large language models are evaluated on structured table data.\n\n"
                "∗ Contribution note.\n",
                encoding="utf-8",
            )

            result = adapter.recover_first_page_abstract_reading_order(output_dir, document)
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"])
        self.assertLess(html_text.index("ABSTRACT"), html_text.index("CCS CONCEPTS"))
        self.assertLess(markdown.index("## ABSTRACT"), markdown.index("## CCS CONCEPTS"))
        self.assertGreater(html_text.index("∗ Contribution note."), html_text.index("1 INTRODUCTION"))

    def test_semantic_emphasis_uses_pdf_font_evidence(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Normal Important result.",
                    "formatting": None,
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 10, "r": 200, "t": 100, "b": 80},
                        }
                    ],
                }
            ]
        }
        source = {
            "available": True,
            "pages": {
                1: {
                    "median_font_size": 10,
                    "characters": [
                        {
                            "index": index,
                            "text": char,
                            "bbox": {"l": 70 + index, "r": 71 + index, "t": 95, "b": 85},
                            "font_name": "Example-Bold",
                            "font_weight": 700,
                            "font_size": 10,
                        }
                        for index, char in enumerate("Important")
                    ],
                }
            },
        }

        diagnostics = adapter.semantic_emphasis_diagnostics(document, source)
        html_text, html_count = adapter._apply_semantic_spans_to_html(
            "<html><body><p>Normal Important result.</p></body></html>",
            diagnostics,
        )
        markdown, markdown_count = adapter._apply_semantic_spans_to_markdown(
            "Normal Important result.\n",
            diagnostics,
        )

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(document["texts"][0]["formatting"]["semantic_spans"][0]["styles"], ["bold"])
        self.assertEqual(html_count, 1)
        self.assertIn("<strong>Important</strong>", html_text)
        self.assertEqual(markdown_count, 1)
        self.assertIn("**Important**", markdown)

    def test_semantic_emphasis_treats_medium_font_as_bold(self) -> None:
        self.assertIn("bold", adapter._font_semantic_styles("NimbusRomNo9L-Medi", None))
        self.assertIn("bold", adapter._font_semantic_styles("Example-Medium", None))

    def test_semantic_emphasis_avoids_nested_markdown_spans(self) -> None:
        diagnostics = [
            {
                "page_no": 1,
                "node_text": "From RNNs to Transformers body.",
                "text": "From RNNs to Transformers",
                "start": 0,
                "end": 25,
                "styles": ["bold"],
            },
            {
                "page_no": 1,
                "node_text": "From RNNs to Transformers body.",
                "text": "RNN",
                "start": 5,
                "end": 8,
                "styles": ["bold"],
            },
        ]

        markdown, count = adapter._apply_semantic_spans_to_markdown(
            "From RNNs to Transformers body.\n",
            diagnostics,
        )

        self.assertEqual(count, 1)
        self.assertIn("**From RNNs to Transformers** body.", markdown)
        self.assertNotIn("**From **RNN**s", markdown)

    def test_structural_quarantine_matches_markdown_escaped_punctuation(self) -> None:
        text = "AUTHORIZATION MD.! _ MP-75"
        document = {
            "pictures": [
                {
                    "label": "picture",
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 60, "r": 560, "t": 700, "b": 520},
                        }
                    ],
                }
            ],
            "texts": [
                {
                    "label": "text",
                    "text": text,
                    "prov": [
                        {
                            "page_no": 2,
                            "bbox": {"l": 208, "r": 251, "t": 767, "b": 762},
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "AUTHORIZATION MD.! \\_ MP-75\n",
                encoding="utf-8",
            )
            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            markdown = (output_dir / "document.md").read_text(encoding="utf-8")

        self.assertEqual(result["markdown_quarantine_replacement_count"], 1)
        markdown_body = adapter._markdown_without_structural_content(markdown)
        self.assertNotIn(
            "AUTHORIZATION MD",
            adapter.re.sub(r"<!--.*?-->", "", markdown_body),
        )
        self.assertEqual(result["final_output_residual_count"], 0)

    def test_structural_quarantine_removes_figure_diagram_label_clusters(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Nx",
                    "prov": [{"page_no": 1, "bbox": {"l": 15, "r": 28, "t": 605, "b": 597}}],
                },
                {
                    "label": "text",
                    "text": "Encoding Add & Norm Feed Forward Add & Norm Multi-Head Attention Input Embedding",
                    "prov": [{"page_no": 1, "bbox": {"l": 9, "r": 47, "t": 533, "b": 523}}],
                },
                {
                    "label": "text",
                    "text": "Inputs Output Probabilities Softmax",
                    "prov": [{"page_no": 1, "bbox": {"l": 67, "r": 92, "t": 479, "b": 471}}],
                },
                {
                    "label": "caption",
                    "text": "Figure 1: The Transformer - model architecture.",
                    "prov": [{"page_no": 1, "bbox": {"l": 108, "r": 504, "t": 390, "b": 370}}],
                },
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [{"page_no": 1, "bbox": {"l": 195, "r": 417, "t": 719, "b": 398}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                "<html><body><p>Nx</p>"
                "<p>Encoding Add &amp; Norm Feed Forward Add &amp; Norm Multi-Head Attention Input Embedding</p>"
                "<p>Inputs Output Probabilities Softmax</p>"
                "<figure><figcaption>Figure 1: The Transformer - model architecture.</figcaption></figure>"
                "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "Nx\n\nEncoding Add &amp; Norm Feed Forward Add &amp; Norm Multi-Head Attention Input Embedding\n\n"
                "Inputs Output Probabilities Softmax\n\nFigure 1: The Transformer - model architecture.\n",
                encoding="utf-8",
            )

            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            body_html = adapter._html_without_structural_content(
                (output_dir / "document.html").read_text(encoding="utf-8")
            )
            markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )
            content = json.loads((output_dir / "structural_content.json").read_text(encoding="utf-8"))

        self.assertGreaterEqual(result["html_quarantine_replacement_count"], 3)
        self.assertGreaterEqual(result["markdown_quarantine_replacement_count"], 3)
        self.assertNotIn("<p>Nx</p>", body_html)
        self.assertNotIn("Encoding Add", body_html)
        self.assertNotIn("Encoding Add", markdown)
        self.assertIn("Figure 1: The Transformer", body_html)
        self.assertTrue(any(item["kind"] == "visual_annotation" for item in content["records"]))

    def test_structural_quarantine_removes_private_use_math_caption_prefix(self) -> None:
        text = "   Figure 1: Left: Schematic depiction of a model."
        document = {
            "texts": [
                {
                    "label": "caption",
                    "text": text,
                    "prov": [{"page_no": 1, "bbox": {"l": 108, "r": 506, "t": 553, "b": 500}}],
                }
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [{"page_no": 1, "bbox": {"l": 113, "r": 501, "t": 704, "b": 565}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(text + "\n", encoding="utf-8")

            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            body_html = adapter._html_without_structural_content(
                (output_dir / "document.html").read_text(encoding="utf-8")
            )
            markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )
            content = json.loads((output_dir / "structural_content.json").read_text(encoding="utf-8"))

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertNotIn("", body_html)
        self.assertNotIn("", markdown)
        self.assertIn("Figure 1: Left", body_html)
        self.assertTrue(any(item["kind"] == "math_font_noise" for item in content["records"]))

    def test_structural_quarantine_removes_standalone_private_use_math_noise(self) -> None:
        text = ""
        document = {
            "texts": [
                {
                    "label": "quarantined_visual_annotation",
                    "text": text,
                    "prov": [{"page_no": 1, "bbox": {"l": 132, "r": 153, "t": 558, "b": 548}}],
                }
            ],
            "pictures": [
                {
                    "label": "picture",
                    "prov": [{"page_no": 1, "bbox": {"l": 113, "r": 501, "t": 704, "b": 565}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "document.html").write_text(
                f"<html><body><p>{text}</p><p>Figure 1: Model.</p></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(text + "\n\nFigure 1: Model.\n", encoding="utf-8")

            result = adapter.apply_structural_quarantine_to_outputs(output_dir, document)
            body_html = adapter._html_without_structural_content(
                (output_dir / "document.html").read_text(encoding="utf-8")
            )
            markdown = adapter._markdown_without_structural_content(
                (output_dir / "document.md").read_text(encoding="utf-8")
            )
            content = json.loads((output_dir / "structural_content.json").read_text(encoding="utf-8"))

        self.assertEqual(result["final_output_residual_count"], 0)
        self.assertNotIn(text, body_html)
        self.assertNotIn(text, markdown)
        self.assertTrue(any(item["kind"] == "math_font_noise" for item in content["records"]))

    def test_structural_quarantine_does_not_relabel_formula_nodes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "formula",
                    "text": r"x = y \quad (24)",
                    "prov": [{"page_no": 2, "bbox": {"l": 80, "r": 360, "t": 92, "b": 82}}],
                },
                {
                    "label": "text",
                    "text": "1 Correspondence to: author@example.org",
                    "prov": [{"page_no": 2, "bbox": {"l": 80, "r": 360, "t": 72, "b": 62}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "formula")
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote_candidate")

    def test_structural_quarantine_preserves_full_long_footnote_text(self) -> None:
        long_text = (
            "Permission to make digital or hard copies of all or part of this work "
            "for personal or classroom use is granted without fee provided that "
            "copies are not made or distributed for profit or commercial advantage "
            "and that copies bear this notice and the full citation on the first page. "
            "Copyrights for components of this work owned by others than the author "
            "must be honored, and abstracting with credit is permitted."
        )
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": long_text,
                    "prov": [{"page_no": 1, "bbox": {"l": 40, "r": 540, "t": 190, "b": 170}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(qc["candidates"][0]["text"], long_text)
        self.assertLess(len(qc["candidates"][0]["text_preview"]), len(long_text))

    def test_structural_quarantine_marks_marker_led_contribution_footnote(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "∗ Equal contributions during internship at Microsoft Research Asia.",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 243, "t": 188, "b": 180}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_footnote_candidate")
        self.assertIn("marker_led_footnote_content_candidate", qc["candidates"][0]["reasons"])

    def test_structural_quarantine_extends_labeled_footnote_cluster_to_marker_line(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "1 Code and data are available at https://example.org/project",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 250, "t": 175, "b": 164}}],
                },
                {
                    "label": "footnote",
                    "text": "Permission to make copies is granted.",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 300, "t": 158, "b": 116}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        marker_candidate = next(
            item for item in qc["candidates"] if item["text"].startswith("1 Code")
        )
        self.assertEqual(marker_candidate["action"], "quarantine_from_main_text_flow")
        self.assertIn("same_column_footnote_cluster", marker_candidate["reasons"])
        self.assertEqual(document["texts"][0]["label"], "quarantined_footnote_candidate")

    def test_structural_quarantine_extends_labeled_footnote_cluster_to_unmarked_continuation(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "1 Please find code at https://example.com/repo.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 290, "t": 230, "b": 214}}],
                },
                {
                    "label": "text",
                    "text": "Please note that the private preview may be replaced by an official one at https://github.com/example/project.",
                    "prov": [{"page_no": 1, "bbox": {"l": 50, "r": 290, "t": 211, "b": 198}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)
        continuation = next(item for item in qc["candidates"] if item["text"].startswith("Please note"))

        self.assertEqual(continuation["kind"], "footnote_candidate")
        self.assertEqual(continuation["confidence"], "high")
        self.assertIn("same_column_footnote_continuation", continuation["reasons"])
        self.assertEqual(document["texts"][1]["label"], "quarantined_footnote_candidate")

    def test_structural_quarantine_removes_top_edge_ocr_adjacent_to_empty_tables(self) -> None:
        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "Windermere Area",
                    "prov": [{"page_no": 2, "bbox": {"l": 10, "r": 85, "t": 770, "b": 760}}],
                },
                {
                    "label": "text",
                    "text": "Normal body paragraph.",
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "r": 540, "t": 500, "b": 470}}],
                },
            ],
            "tables": [
                {
                    "label": "table",
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                    "prov": [{"page_no": 2, "bbox": {"l": 60, "r": 150, "t": 706, "b": 668}}],
                },
                {
                    "label": "table",
                    "data": {"table_cells": [], "num_rows": 0, "num_cols": 0},
                    "prov": [{"page_no": 2, "bbox": {"l": 170, "r": 270, "t": 706, "b": 668}}],
                },
            ],
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_table_visual_annotation")
        self.assertEqual(document["texts"][1]["label"], "text")
        self.assertIn(
            "text_bbox_inside_or_adjacent_to_table",
            qc["candidates"][0]["reasons"],
        )

    def test_structural_quarantine_preserves_first_page_affiliation_mislabels(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "2 University of Example, Department of AI",
                    "prov": [{"page_no": 1, "bbox": {"l": 90, "r": 410, "t": 650, "b": 630}}],
                },
                {
                    "label": "footnote",
                    "text": "5 机构智能实验室",
                    "prov": [{"page_no": 1, "bbox": {"l": 90, "r": 320, "t": 625, "b": 605}}],
                },
                {
                    "label": "footnote",
                    "text": "0",
                    "prov": [{"page_no": 1, "bbox": {"l": 120, "r": 124, "t": 90, "b": 85}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "text")
        self.assertEqual(document["texts"][1]["label"], "text")
        self.assertEqual(document["texts"][2]["label"], "quarantined_footnote")
        self.assertIn("author_affiliation_recovery", document["texts"][0]["local_ai_lab_qc"])

    def test_affiliation_recovery_does_not_preserve_contribution_footnotes(self) -> None:
        document = {
            "texts": [
                {
                    "label": "footnote",
                    "text": "∗ Equal contributions during internship at Microsoft Research Asia.",
                    "prov": [{"page_no": 1, "bbox": {"l": 53, "r": 243, "t": 188, "b": 180}}],
                },
            ]
        }

        qc = adapter.structural_noise_qc(document)

        self.assertEqual(qc["candidate_count"], 1)
        self.assertEqual(document["texts"][0]["label"], "quarantined_footnote")
        self.assertNotIn("author_affiliation_recovery", document["texts"][0].get("local_ai_lab_qc", {}))

    def test_recovers_fragmented_first_page_affiliations_from_pdf_text_layer(self) -> None:
        document = {
            "texts": [
                {
                    "label": "section_header",
                    "text": "OCR-free Document Understanding Transformer",
                    "prov": [{"page_no": 1, "bbox": {"t": 580, "b": 568}}],
                },
                {
                    "label": "text",
                    "text": "Geewook Kim 1 ∗ , Teakgyu Hong 4 †",
                    "prov": [{"page_no": 1, "bbox": {"t": 545, "b": 509}}],
                },
                {
                    "label": "text",
                    "text": "2",
                    "prov": [{"page_no": 1, "bbox": {"t": 498, "b": 493}}],
                },
                {
                    "label": "text",
                    "text": "3 NAVER AI Lab ut ut ut",
                    "prov": [{"page_no": 1, "bbox": {"t": 498, "b": 489}}],
                },
                {
                    "label": "text",
                    "text": "1 NAVER CLOVA",
                    "prov": [{"page_no": 1, "bbox": {"t": 498, "b": 489}}],
                },
                {
                    "label": "text",
                    "text": "5",
                    "prov": [{"page_no": 1, "bbox": {"t": 487, "b": 482}}],
                },
                {
                    "label": "text",
                    "text": "4 Upstage NAVER Search Tmax 6 Google 7 LBox",
                    "prov": [{"page_no": 1, "bbox": {"t": 487, "b": 478}}],
                },
                {
                    "label": "text",
                    "text": "Abstract. Body",
                    "prov": [{"page_no": 1, "bbox": {"t": 439, "b": 223}}],
                },
            ]
        }
        original_pdf_text = adapter._first_page_pdf_text
        adapter._first_page_pdf_text = lambda _path: (
            "OCR-free Document Understanding Transformer\n"
            "Geewook Kim1∗\n"
            "1NAVER CLOVA 2NAVER Search 3NAVER AI Lab\n"
            "4Upstage 5Tmax 6Google 7LBox\n"
            "Abstract. Body\n"
        )
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir)
                (out / "document.md").write_text(
                    "## OCR-free Document Understanding Transformer\n\n"
                    "Geewook Kim 1 ∗ , Teakgyu Hong 4 †\n\n"
                    "2\n\n3 NAVER AI Lab ut ut ut\n\n1 NAVER CLOVA\n\n5\n\n"
                    "4 Upstage NAVER Search Tmax 6 Google 7 LBox\n\nAbstract. Body\n",
                    encoding="utf-8",
                )
                (out / "document.html").write_text(
                    "<html><body><p>Geewook Kim 1 ∗ , Teakgyu Hong 4 †</p>"
                    "<p>2</p><p>3 NAVER AI Lab ut ut ut</p><p>1 NAVER CLOVA</p>"
                    "<p>5</p><p>4 Upstage NAVER Search Tmax 6 Google 7 LBox</p>"
                    "<p>Abstract. Body</p></body></html>",
                    encoding="utf-8",
                )
                result = adapter.recover_first_page_author_affiliations(
                    out,
                    document,
                    Path("dummy.pdf"),
                )
                md_text = (out / "document.md").read_text(encoding="utf-8")
                html_text = (out / "document.html").read_text(encoding="utf-8")
        finally:
            adapter._first_page_pdf_text = original_pdf_text

        self.assertTrue(result["applied"])
        self.assertIn("1 NAVER CLOVA 2 NAVER Search 3 NAVER AI Lab", md_text)
        self.assertIn("4 Upstage 5 Tmax 6 Google 7 LBox", md_text)
        self.assertIn("docling-author-affiliation-recovery", html_text)
        self.assertEqual(document["texts"][2]["text"].splitlines()[0], "1 NAVER CLOVA 2 NAVER Search 3 NAVER AI Lab")
        self.assertEqual(document["texts"][3]["label"], "quarantined_author_affiliation_fragment")

    def test_replace_exact_paragraph_with_quarantine_hides_text_from_render_flow(self) -> None:
        item = {
            "kind": "page_header",
            "text": "arXiv:2506.22084v1 [cs.LG]",
            "page_no": 1,
            "reasons": ["publication_template_noise"],
        }
        html, changed = adapter._replace_exact_paragraph_with_quarantine(
            "<html><body><p>Body text</p><p><span>arXiv:2506.22084v1 [cs.LG]</span></p></body></html>",
            item,
        )

        self.assertTrue(changed)
        self.assertIn("<!-- local-ai-lab structural quarantine", html)
        self.assertNotIn("<span>arXiv:2506.22084v1 [cs.LG]</span>", html)
        self.assertIn("evidence=structural_regions.json", html)

    def test_embedded_visual_ocr_noise_is_hidden_from_main_flow(self) -> None:
        long_noise = "A" * 520 + "0" * 80

        html_text, html_count = adapter._replace_embedded_visual_ocr_noise_blocks_html(
            f"<html><body><p>Body remains readable.</p><p>axis {long_noise}</p></body></html>"
        )
        markdown, md_count = adapter._replace_embedded_visual_ocr_noise_blocks_markdown(
            f"Body remains readable.\n\naxis {long_noise}\n"
        )

        self.assertEqual(html_count, 1)
        self.assertEqual(md_count, 1)
        self.assertIn("Body remains readable", html_text)
        self.assertIn("embedded_visual_ocr_noise", html_text)
        self.assertIn("embedded_visual_ocr_noise", markdown)

    def test_author_email_prefix_is_split_from_algorithm_caption(self) -> None:
        html_text, html_count = adapter._split_author_affiliation_from_body_html(
            "<p><strong>Jimmy Lei Ba</strong> University jimmy@example.edu "
            "Algorithm 1: Adam, our proposed algorithm.</p>"
        )
        markdown, md_count = adapter._split_author_affiliation_from_body_markdown(
            "**Jimmy Lei Ba** University jimmy@example.edu Algorithm 1: Adam, our proposed algorithm."
        )

        self.assertEqual(html_count, 1)
        self.assertEqual(md_count, 1)
        self.assertIn("author_affiliation_fragment", html_text)
        self.assertIn("<p>Algorithm 1: Adam", html_text)
        self.assertNotIn("Jimmy Lei Ba</strong> University", html_text)
        self.assertIn("Algorithm 1: Adam", markdown)

    def test_algorithm_code_blocks_gain_readable_line_breaks(self) -> None:
        html_text, count = adapter._normalize_algorithm_code_blocks_html(
            "<pre><code>Require: α : Stepsize Require: β 1 : Rate "
            "m 0 ← 0 while θ t not converged do t ← t + 1 return θ t</code></pre>"
        )

        self.assertEqual(count, 1)
        self.assertIn("\nRequire: β", html_text)
        self.assertIn("\nwhile θ", html_text)

    def test_visual_axis_tail_is_split_from_figure_caption(self) -> None:
        caption = (
            "Figure 2 shows the frame classification error rate on the core test set. "
            "The neural net has four fully-connected hidden layers Classification Error %"
        )
        html_text, count = adapter._quarantine_visual_axis_tail_html(f"<p>{caption}</p>")

        self.assertEqual(count, 1)
        self.assertIn("kind=visual_annotation", html_text)
        self.assertNotIn("Classification Error %</p>", html_text)


if __name__ == "__main__":
    unittest.main()
