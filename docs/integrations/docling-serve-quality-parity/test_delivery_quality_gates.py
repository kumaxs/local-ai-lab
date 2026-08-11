from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import quality_parity_adapter as adapter


class DeliveryQualityGateRegressionTests(unittest.TestCase):
    def test_corrupt_formula_png_cannot_pass_with_stale_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "formulas").mkdir()
            (output_dir / "formulas" / "formula_1.png").write_bytes(b"not a png")
            (output_dir / "document.html").write_text(
                '<div class="formula"><math><mi>x</mi></math></div>'
                '<!-- source-formula-anchor:1 -->',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\nx\n$$\n<!-- source-formula-anchor:1 -->\n",
                encoding="utf-8",
            )
            formulas = [{"label": "formula", "text": "x"}]
            evidence = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                expected_indexes={1},
                formula_crop_diagnostics=[
                    {
                        "index": 1,
                        "source": {"pixel_width": 1000, "pixel_height": 100},
                    }
                ],
            )

        self.assertEqual(evidence["missing_candidate_indexes"], [1])
        self.assertEqual(evidence["html_covered_indexes"], [])
        self.assertEqual(evidence["markdown_covered_indexes"], [])

    def test_blank_formula_png_cannot_satisfy_source_visual_gate(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "formulas").mkdir()
            Image.new("RGB", (120, 60), "white").save(
                output_dir / "formulas" / "formula_1.png"
            )
            (output_dir / "document.html").write_text(
                '<math><mi>x</mi></math><!-- source-formula-anchor:1 -->',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                '$$\nx\n$$\n<!-- source-formula-anchor:1 -->',
                encoding="utf-8",
            )

            evidence = adapter.append_formula_source_renderings(
                output_dir,
                [{"label": "formula", "text": "x"}],
                expected_indexes={1},
            )

        self.assertEqual([1], evidence["missing_candidate_indexes"])
        self.assertEqual([], evidence["html_covered_indexes"])

    def test_formula_markers_inside_code_are_not_formula_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "document.json").write_text(
                json.dumps({"texts": []}), encoding="utf-8"
            )
            (output_dir / "document.html").write_text(
                '<section class="code-listing"><pre><code>'
                'echo $$ &lt;formula&gt; &lt;!-- formula-not-decoded --&gt;'
                "</code></pre></section>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "```bash\necho $$ '<formula>' '<!-- formula-not-decoded -->'\n```\n",
                encoding="utf-8",
            )
            status = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {"counts": {"formulas": 0}},
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [],
                        "formula_source_html_indexes": [],
                        "formula_source_markdown_indexes": [],
                    },
                },
            }
            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["markdown_math_delimiter_count"], 0)
        self.assertEqual(result["raw_model_token_count"], 0)
        self.assertEqual(result["undecoded_placeholder_count"], 0)

    def test_bbox_pixel_crop_supports_both_coordinate_origins(self) -> None:
        bottom_left = adapter._bbox_pixel_crop_box(
            {
                "l": 10,
                "r": 20,
                "t": 90,
                "b": 80,
                "coord_origin": "BOTTOMLEFT",
            },
            page_width=100,
            page_height=100,
            image_width=1000,
            image_height=1000,
            padding=0,
        )
        top_left = adapter._bbox_pixel_crop_box(
            {
                "l": 10,
                "r": 20,
                "t": 10,
                "b": 20,
                "coord_origin": "TOPLEFT",
            },
            page_width=100,
            page_height=100,
            image_width=1000,
            image_height=1000,
            padding=0,
        )

        self.assertEqual(bottom_left, (100, 100, 200, 200))
        self.assertEqual(top_left, bottom_left)

    def test_filename_substring_does_not_invent_cn_formula_gap(self) -> None:
        result = adapter.formula_review_diagnostics(
            [],
            Path("/tmp/unused-output"),
            {"json_content": {"texts": [], "pages": {}}},
            Path("fooCN.pdf"),
            [],
        )

        self.assertEqual(result["missing_formula_diagnostics"], [])
        self.assertFalse(result["cn_section_2_3_diagnostic_summary"]["applies"])

    def test_zero_formula_surface_rejects_stale_source_formula_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "document.json").write_text(
                json.dumps({"texts": []}), encoding="utf-8"
            )
            (output_dir / "document.html").write_text(
                "<html><body><p>No formula.</p></body></html>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text(
                "No formula.\n", encoding="utf-8"
            )
            status = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {"counts": {"formulas": 0}},
                    "final_source_visuals": {
                        "formula_source_expected_indexes": [1],
                        "formula_source_html_indexes": [1],
                        "formula_source_markdown_indexes": [1],
                        "formula_source_html_ref_count": 1,
                        "formula_source_markdown_ref_count": 1,
                    },
                },
            }
            result = adapter.validate_final_formula_surfaces(
                output_dir, {}, status
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "incomplete_formula_source_visual_coverage",
            result["failure_reasons"],
        )

    def test_generated_asset_preflight_rejects_reused_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "formulas").mkdir()
            (output_dir / "formulas" / "formula_1.png").write_bytes(b"old")

            roots = adapter._preexisting_generated_asset_roots(output_dir)

        self.assertEqual(roots, ["formulas"])

    def test_fresh_output_preflight_rejects_stale_sidecar_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "review_index.html").write_text(
                "old", encoding="utf-8"
            )

            entries = adapter._preexisting_output_entries(output_dir)

        self.assertEqual(entries, ["review_index.html"])

    def test_algorithm_bbox_and_reading_order_support_topleft(self) -> None:
        nodes = [
            {
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 20,
                            "r": 80,
                            "t": 100,
                            "b": 120,
                            "coord_origin": "TOPLEFT",
                        },
                    }
                ]
            },
            {
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 20,
                            "r": 90,
                            "t": 130,
                            "b": 150,
                            "coord_origin": "TOPLEFT",
                        },
                    }
                ]
            },
        ]

        merged = adapter._merge_bbox_geometry(nodes)
        ordered = sorted(enumerate(nodes), key=adapter._algorithm_cluster_reading_key)

        self.assertEqual(
            merged,
            {
                "l": 20.0,
                "r": 90.0,
                "t": 100.0,
                "b": 150.0,
                "width": 70.0,
                "height": 50.0,
                "coord_origin": "TOPLEFT",
                "aspect_width_over_height": 1.4,
            },
        )
        self.assertEqual([index for index, _node in ordered], [0, 1])

    def test_bbox_union_preserves_origin_and_refuses_mixed_coordinates(self) -> None:
        top_left = adapter._bbox_union(
            [
                {"l": 10, "r": 20, "t": 100, "b": 120, "coord_origin": "TOPLEFT"},
                {"l": 8, "r": 30, "t": 130, "b": 150, "coord_origin": "TOPLEFT"},
            ]
        )
        bottom_left = adapter._bbox_union(
            [
                {"l": 10, "r": 20, "t": 700, "b": 680, "coord_origin": "BOTTOMLEFT"},
                {"l": 8, "r": 30, "t": 670, "b": 650, "coord_origin": "BOTTOMLEFT"},
            ]
        )
        mixed = adapter._bbox_union(
            [
                {"l": 1, "r": 2, "t": 3, "b": 4, "coord_origin": "TOPLEFT"},
                {"l": 1, "r": 2, "t": 4, "b": 3, "coord_origin": "BOTTOMLEFT"},
            ]
        )

        self.assertEqual(top_left["t"], 100)
        self.assertEqual(top_left["b"], 150)
        self.assertEqual(top_left["height"], 50)
        self.assertEqual(bottom_left["t"], 700)
        self.assertEqual(bottom_left["b"], 650)
        self.assertEqual(bottom_left["height"], 50)
        self.assertIsNone(mixed)

    def test_formula_second_pass_off_does_not_mutate_route_a_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            document_json = '{"texts":[{"label":"formula","text":"x+y"}]}'
            document_html = '<div class="formula">x+y</div>'
            document_md = '$$\nx+y\n$$\n'
            (output_dir / "document.json").write_text(document_json, encoding="utf-8")
            (output_dir / "document.html").write_text(document_html, encoding="utf-8")
            (output_dir / "document.md").write_text(document_md, encoding="utf-8")
            metadata = {}
            status = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            args = SimpleNamespace(
                formula_second_pass_policy="off",
                formula_second_pass_route_b_dir=None,
                formula_policy="granite_transformers",
                enable_formula_mlx=False,
            )

            adapter.run_optional_formula_second_pass(
                output_dir, metadata, status, args
            )

            self.assertEqual(
                document_json,
                (output_dir / "document.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                document_html,
                (output_dir / "document.html").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                document_md,
                (output_dir / "document.md").read_text(encoding="utf-8"),
            )
            self.assertFalse(metadata["formula_second_pass_applied"])
            self.assertEqual(
                "formula_second_pass_policy_off",
                metadata["formula_second_pass"]["reason"],
            )

    def test_local_reference_audit_rejects_path_traversal_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "job"
            output_dir.mkdir()
            outside = root / "secret.png"
            outside.write_bytes(b"secret")
            document = {
                "html_content": (
                    '<img src="../secret.png">'
                    f'<a href="{outside}">absolute</a>'
                ),
                "md_content": "",
            }

            broken = adapter.broken_local_refs(output_dir, document)

            self.assertIn("../secret.png", broken)
            self.assertIn(str(outside), broken)


if __name__ == "__main__":
    unittest.main()
