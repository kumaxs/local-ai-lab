from __future__ import annotations

import tempfile
import sys
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_parity_adapter as adapter  # noqa: E402
import formula_only_second_pass as formula_second_pass  # noqa: E402


class EnglishReviewPolishTests(unittest.TestCase):
    def test_cn_apply_all_resolves_to_accepted_apply_policy(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/CN.pdf"),
            cn_ocr_parity=True,
            formula_second_pass_policy="apply-all",
        )

        self.assertTrue(adapter.is_cn_accepted_path(args))
        self.assertEqual(adapter.effective_formula_second_pass_policy(args), "apply")

    def test_english_apply_all_policy_remains_isolated(self) -> None:
        args = Namespace(
            input_file=Path("/tmp/two-col-arxiv-ai-lora.pdf"),
            cn_ocr_parity=False,
            formula_second_pass_policy="apply-all",
        )

        self.assertFalse(adapter.is_cn_accepted_path(args))
        self.assertEqual(adapter.effective_formula_second_pass_policy(args), "apply-all")

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

    def test_cn_accepted_formula_sources_restore_reviewed_map(self) -> None:
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
        self.assertEqual(sources[3], "route_b")
        self.assertEqual(sources[5], "guarded_fallback")
        self.assertEqual(sources[1], "reviewed_cross_column_repair")
        self.assertEqual(sources[13], "reviewed_formula_13_repair")
        self.assertNotIn("trailing", texts[1])
        self.assertEqual(
            texts[13],
            adapter.normalize_formula_candidate(adapter.CN_ACCEPTED_FORMULA_13),
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
        self.assertIn("small_text_bottom_footnote_marker_candidate", qc["candidates"][0]["reasons"])

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
        self.assertIn("<template", html)
        self.assertNotIn("<span>arXiv:2506.22084v1 [cs.LG]</span>", html)
        self.assertIn("publication_template_noise", html)


if __name__ == "__main__":
    unittest.main()
