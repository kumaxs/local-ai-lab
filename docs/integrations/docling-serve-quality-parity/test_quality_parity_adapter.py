from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402
import formula_only_second_pass as formula_second_pass  # noqa: E402


class EnglishReviewPolishTests(unittest.TestCase):
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
        self.assertIn("<template", html)
        self.assertNotIn("<span>arXiv:2506.22084v1 [cs.LG]</span>", html)
        self.assertIn("publication_template_noise", html)


if __name__ == "__main__":
    unittest.main()
