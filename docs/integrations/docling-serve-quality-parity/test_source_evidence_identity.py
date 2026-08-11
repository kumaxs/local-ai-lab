from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import quality_parity_adapter as adapter


def _visible_png(path: Path, size: tuple[int, int] = (120, 60)) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    left = min(12, max(0, size[0] // 4))
    top = min(15, max(0, size[1] // 4))
    right = max(left, size[0] - left - 1)
    bottom = max(top, size[1] - top - 1)
    draw.rectangle((left, top, right, bottom), fill="black")
    image.save(path)


def _table_data(rows: list[list[str]]) -> dict[str, object]:
    cells = []
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            cells.append(
                {
                    "start_row_offset_idx": row_index,
                    "end_row_offset_idx": row_index + 1,
                    "start_col_offset_idx": column_index,
                    "end_col_offset_idx": column_index + 1,
                    "text": value,
                }
            )
    return {
        "num_rows": len(rows),
        "num_cols": max((len(row) for row in rows), default=0),
        "table_cells": cells,
    }


def _formula_crop_diagnostic(
    output_dir: Path,
    index: int,
    *,
    formula: dict[str, object],
    source_pdf_sha256: str = "a" * 64,
) -> dict[str, object]:
    prov = adapter.first_prov(formula) or {}
    page_no = int(prov.get("page_no") or 0)
    bbox = adapter.bbox_geometry(prov)
    if page_no <= 0 or bbox is None:
        raise AssertionError("formula provenance fixture must include page and bbox")
    identity_sha256 = adapter._formula_content_identity_sha256(
        str(formula.get("text") or "")
    )
    raw_identity_sha256 = adapter._formula_raw_content_sha256(
        str(formula.get("text") or "")
    )
    result: dict[str, object] = {
        "index": index,
        "page_no": page_no,
        "bbox": dict(bbox),
        "source_pdf_sha256": source_pdf_sha256,
        "formula_content_identity_sha256": identity_sha256,
        "formula_raw_content_sha256": raw_identity_sha256,
    }
    for kind, suffix in (("source", ""), ("context", "_context")):
        path = output_dir / "formulas" / f"formula_{index}{suffix}.png"
        if not path.is_file():
            continue
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        result[kind] = {
            "path": f"formulas/formula_{index}{suffix}.png",
            "page_no": page_no,
            "bbox": dict(bbox),
            "asset_sha256": adapter.file_sha256(path),
            "source_pdf_sha256": source_pdf_sha256,
            "formula_content_identity_sha256": identity_sha256,
            "formula_raw_content_sha256": raw_identity_sha256,
            "pixel_width": width,
            "pixel_height": height,
            "page_size": {"width": 100.0, "height": 100.0},
        }
    return result


def _formula_node(text: str, *, page_no: int = 1) -> dict[str, object]:
    return {
        "label": "formula",
        "text": text,
        "prov": [
            {
                "page_no": page_no,
                "bbox": {
                    "l": 5.0,
                    "r": 95.0,
                    "t": 95.0,
                    "b": 5.0,
                    "coord_origin": "BOTTOMLEFT",
                },
            }
        ],
    }


def _structural_gate_fixture(
    output_dir: Path,
    kind: str,
) -> tuple[dict[str, object], dict[str, object], Path, Path]:
    (output_dir / "pages").mkdir()
    page_path = output_dir / "pages" / "page_1.png"
    _visible_png(page_path, size=(100, 100))
    bbox = {
        "l": 10.0,
        "r": 80.0,
        "t": 80.0,
        "b": 20.0,
        "coord_origin": "BOTTOMLEFT",
    }
    source_ref = f"#/{kind}/0"
    if kind == "table":
        node: dict[str, object] = {
            "self_ref": source_ref,
            "label": "table",
            "data": _table_data([["x + y"]]),
            "prov": [{"page_no": 1, "bbox": bbox}],
        }
        body_identity = adapter._table_grid_body_identity(adapter.table_grid(node))
        asset_relative = "tables/table_1.png"
        manifest_inventory = "tables"
    else:
        node = {
            "self_ref": source_ref,
            "label": "code",
            "text": (
                "Algorithm 1: Input: x while x < y do update x return x"
                if kind == "algorithm"
                else "x = x + 1"
            ),
            "prov": [{"page_no": 1, "bbox": bbox}],
        }
        body_identity = adapter._structural_body_identity(str(node["text"]))
        asset_relative = (
            "algorithms/algorithm_1.png"
            if kind == "algorithm"
            else "code_blocks/code_block_1.png"
        )
        manifest_inventory = "algorithms" if kind == "algorithm" else "code"
    asset_path = output_dir / asset_relative
    asset_path.parent.mkdir()
    _visible_png(asset_path, size=(70, 60))
    (output_dir / "document.json").write_text(
        json.dumps(
            {
                "pages": {"1": {"size": {"width": 100, "height": 100}}},
                "tables": [node] if kind == "table" else [],
                "texts": [] if kind == "table" else [node],
            }
        ),
        encoding="utf-8",
    )
    visual_sha = "a" * 64
    semantic_sha = "b" * 64
    metric = {
        "path": asset_relative,
        "page_no": 1,
        "bbox": bbox,
        "asset_sha256": adapter.file_sha256(asset_path),
        "source_pdf_sha256": visual_sha,
        "page_image_path": "pages/page_1.png",
        "page_image_sha256": adapter.file_sha256(page_path),
        "padding_px": 0,
        "pixel_box": adapter._bbox_pixel_crop_box(
            bbox,
            page_width=100,
            page_height=100,
            image_width=100,
            image_height=100,
            padding=0,
        ),
        "page_size": {"width": 100, "height": 100},
        "page_image_size": {"width": 100, "height": 100},
    }
    bindings = None
    manifest_node = node
    if kind == "algorithm":
        binding = adapter._algorithm_source_node_binding(
            node,
            source_ref=source_ref,
        )
        bindings = [binding]
        manifest_node = None
    entry = adapter._structural_visual_provenance_entry(
        kind=kind,
        index=1,
        source_ref=source_ref,
        node=manifest_node,
        body_identity=body_identity,
        metric=metric,
        node_bbox=bbox,
        part_index=None,
        semantic_pdf_sha256=semantic_sha,
        conversion_pdf_sha256=semantic_sha,
        semantic_page_size={"width": 100, "height": 100},
        crop_coordinate_page_size={"width": 100, "height": 100},
        source_node_bindings=bindings,
    )
    if kind == "algorithm":
        record = {
            "label": "Algorithm 1",
            "text": str(node["text"]),
            "original_text": str(node["text"]),
            "page_no": 1,
            "bbox": adapter.bbox_geometry(adapter.first_prov(node)),
            "source_ref": source_ref,
            "source_image": asset_relative,
            "source_node_bindings": bindings,
        }
        # The manifest must bind the same normalized semantic block that the
        # final algorithm record exposes.
        entry["structural_body_identity_sha256"] = (
            adapter._structural_body_identity_sha256(
                "algorithm",
                adapter._algorithm_expected_body_identity(record),
            )
        )
        (output_dir / "algorithm_blocks.json").write_text(
            json.dumps([record]), encoding="utf-8"
        )
        document_html = (
            "<html><body>" + adapter._algorithm_record_html(record) + "</body></html>"
        )
        document_markdown = adapter._algorithm_record_markdown(record)
    elif kind == "table":
        document_html = (
            '<html><body><figure class="semantic-table">'
            f'<table data-source-ref="{source_ref}"><tr><td>x + y</td></tr></table>'
            '</figure><figure class="docling-table-source-evidence">'
            f'<span hidden data-source-ref="{source_ref}"></span>'
            f'<img src="{asset_relative}"></figure></body></html>'
        )
        document_markdown = (
            f"<!-- source-table-ref:{source_ref} -->\n\n"
            "| x + y |\n|---|\n\n"
            f"![Table source rendering]({asset_relative})\n"
        )
    else:
        document_html = (
            '<html><body><section class="code-listing">'
            f'<pre data-source-ref="{source_ref}"><code>x = x + 1</code></pre>'
            '</section><figure class="docling-code-source-evidence">'
            f'<span hidden data-source-ref="{source_ref}"></span>'
            f'<img src="{asset_relative}"></figure></body></html>'
        )
        document_markdown = (
            f"<!-- source-code-ref:{source_ref} -->\n\n"
            "```text\nx = x + 1\n```\n\n"
            f"![Code source rendering]({asset_relative})\n"
        )
    (output_dir / "document.html").write_text(
        document_html,
        encoding="utf-8",
    )
    (output_dir / "document.md").write_text(
        document_markdown,
        encoding="utf-8",
    )
    page_manifest = {
        "page_no": 1,
        "path": "pages/page_1.png",
        "page_size": {"width": 100, "height": 100},
        "semantic_page_size": {"width": 100, "height": 100},
        "page_image_size": {"width": 100, "height": 100},
        "page_image_sha256": adapter.file_sha256(page_path),
        "visual_pdf_sha256": visual_sha,
    }
    manifest = {
        "version": adapter.STRUCTURAL_VISUAL_PROVENANCE_VERSION,
        "visual_pdf_sha256": visual_sha,
        "pages": {"1": page_manifest},
        "tables": [],
        "code": [],
        "algorithms": [],
    }
    manifest[manifest_inventory] = [entry]
    prefix = f"{kind}_source"
    source_visuals: dict[str, object] = {
        f"{prefix}_candidate_count": 1,
        f"{prefix}_expected_indexes": [1],
        f"{prefix}_html_indexes": [1],
        f"{prefix}_markdown_indexes": [1],
        f"{prefix}_valid_image_indexes": [1],
        f"{prefix}_expected_refs": [source_ref],
        f"{prefix}_html_bound_refs": [source_ref],
        f"{prefix}_markdown_bound_refs": [source_ref],
        f"{prefix}_body_identity_expected_refs": [source_ref],
        f"{prefix}_html_body_identity_verified_refs": [source_ref],
        f"{prefix}_markdown_body_identity_verified_refs": [source_ref],
        f"{prefix}_html_body_identity_mismatch_refs": [],
        f"{prefix}_markdown_body_identity_mismatch_refs": [],
        f"{prefix}_provenance_verified_refs": [source_ref],
        f"{prefix}_provenance_mismatch_refs": [],
    }
    if kind == "table":
        source_visuals.update(
            {
                "table_source_reclassified_algorithm_indexes": [],
                "table_source_unexpected_indexes": [],
                "table_empty_fallback_expected_refs": [],
                "table_empty_fallback_exact_coverage": True,
            }
        )
    if kind == "algorithm":
        source_visuals["algorithm_source_renderings"] = {
            "discarded_candidate_count": 0,
            "records": [],
        }
    metadata: dict[str, object] = {
        "visual_evidence_input_sha256": visual_sha,
        "conversion_input_sha256": semantic_sha,
        "structural_visual_provenance_manifest": manifest,
    }
    counts = {"tables": 0, "algorithms": 0, "code_blocks": 0}
    counts[{"table": "tables", "algorithm": "algorithms", "code": "code_blocks"}[kind]] = 1
    status: dict[str, object] = {
        "ok": True,
        "success_class": "success",
        "warnings": [],
        "quality_signals": {
            "primary_surface": {"counts": counts},
            "final_source_visuals": source_visuals,
        },
    }
    return metadata, status, asset_path, page_path


class SourceEvidenceIdentityTests(unittest.TestCase):
    def test_structural_crop_provenance_uses_declared_pdf_render_scale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            (output_dir / "tables").mkdir()
            page_path = output_dir / "pages" / "page_1.png"
            asset_path = output_dir / "tables" / "table_1.png"
            _visible_png(page_path, size=(1191, 1684))
            _visible_png(asset_path, size=(828, 293))
            visual_sha = "a" * 64
            semantic_sha = "b" * 64
            node_bbox = {
                "l": 305.54302978515625,
                "r": 546.3214721679688,
                "t": 164.81390380859375,
                "b": 98.556884765625,
                "coord_origin": "BOTTOMLEFT",
            }
            crop_bbox = {
                "l": 205.54302978515625,
                "r": 646.3214721679688,
                "t": 180.81390380859375,
                "b": 82.556884765625,
                "coord_origin": "BOTTOMLEFT",
            }
            page_size = {"width": 595.2000122070312, "height": 841.7999877929688}
            pixel_box = adapter._bbox_pixel_crop_box(
                crop_bbox,
                page_width=page_size["width"],
                page_height=page_size["height"],
                image_width=1191,
                image_height=1684,
                padding=48,
                render_scale=2.0,
            )
            self.assertEqual((363, 1273, 1191, 1566), pixel_box)
            node = {
                "self_ref": "#/tables/0",
                "label": "table",
                "data": _table_data([["cell"]]),
                "prov": [{"page_no": 1, "bbox": node_bbox}],
            }
            body_identity = adapter._table_grid_body_identity(
                adapter.table_grid(node)
            )
            metric = {
                "path": "tables/table_1.png",
                "page_no": 1,
                "bbox": crop_bbox,
                "asset_sha256": adapter.file_sha256(asset_path),
                "source_pdf_sha256": visual_sha,
                "page_image_path": "pages/page_1.png",
                "page_image_sha256": adapter.file_sha256(page_path),
                "padding_px": 48,
                "render_scale": 2.0,
                "pixel_box": pixel_box,
                "page_size": page_size,
                "page_image_size": {"width": 1191, "height": 1684},
            }
            entry = adapter._structural_visual_provenance_entry(
                kind="table",
                index=1,
                source_ref="#/tables/0",
                node=node,
                body_identity=body_identity,
                metric=metric,
                node_bbox=node_bbox,
                part_index=None,
                semantic_pdf_sha256=semantic_sha,
                conversion_pdf_sha256=semantic_sha,
                semantic_page_size=page_size,
                crop_coordinate_page_size=page_size,
            )

            verified, reasons = adapter._structural_provenance_verify(
                output_dir,
                entry,
                current_source_ref="#/tables/0",
                current_page_no=1,
                current_bbox=node_bbox,
                current_body_identity=body_identity,
                current_self_ref="#/tables/0",
                current_part_index=None,
                current_semantic_page_size=page_size,
                expected_visual_pdf_sha256=visual_sha,
                expected_semantic_pdf_sha256=semantic_sha,
                expected_asset_path="tables/table_1.png",
                expected_kind="table",
            )

        self.assertTrue(verified, reasons)
        self.assertEqual([], reasons)

    def test_table_source_evidence_binds_by_stable_source_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            _visible_png(output_dir / "tables" / "table_1.png")
            source_ref = "#/tables/0"
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                f'<table data-source-ref="{source_ref}"><tr><td>A</td></tr></table>'
                "</figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-table-ref:{source_ref} -->\n\n| A |\n|---|\n| 1 |\n",
                encoding="utf-8",
            )
            table = {
                "self_ref": source_ref,
                "prov": [{"page_no": 1}],
                "data": _table_data([["A"], ["1"]]),
            }

            result = adapter.append_structured_table_source_renderings(
                output_dir,
                {"tables": [table]},
                [table],
            )

            self.assertEqual([source_ref], result["html_bound_source_refs"])
            self.assertEqual([source_ref], result["markdown_bound_source_refs"])
            self.assertEqual([], result["html_unbound_source_refs"])
            self.assertIn("table_1.png", (output_dir / "document.html").read_text())

    def test_table_source_appendix_does_not_count_as_occurrence_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            _visible_png(output_dir / "tables" / "table_1.png")
            source_ref = "#/tables/0"
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                '<table data-source-ref="#/tables/wrong"><tr><td>A</td></tr></table>'
                "</figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "<!-- source-table-ref:#/tables/wrong -->\n\n| A |\n|---|\n| 1 |\n",
                encoding="utf-8",
            )
            table = {
                "self_ref": source_ref,
                "prov": [{"page_no": 1}],
                "data": _table_data([["A"], ["1"]]),
            }

            result = adapter.append_structured_table_source_renderings(
                output_dir,
                {"tables": [table]},
                [table],
            )

            self.assertEqual([], result["html_bound_source_refs"])
            self.assertEqual([], result["markdown_bound_source_refs"])
            self.assertEqual([source_ref], result["html_unbound_source_refs"])
            self.assertEqual([source_ref], result["markdown_unbound_source_refs"])
            self.assertIn("Original table renderings", (output_dir / "document.html").read_text())

    def test_chunk_duplicate_table_refs_bind_distinct_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            for index in (1, 2):
                _visible_png(output_dir / "tables" / f"table_{index}.png")
            raw_ref = "#/tables/0"
            refs = [f"chunk:{index}:{raw_ref}" for index in (0, 1)]
            bodies = ["left + right", "score != label"]
            (output_dir / "document.html").write_text(
                "<html><body>"
                + "".join(
                    '<figure class="semantic-table">'
                    f'<table data-source-ref="{source_ref}"><tr><td>{body}</td></tr>'
                    "</table></figure>"
                    for source_ref, body in zip(refs, bodies)
                )
                + "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "\n\n".join(
                    f"<!-- source-table-ref:{source_ref} -->\n\n"
                    f"| {body} |\n|---|"
                    for source_ref, body in zip(refs, bodies)
                ),
                encoding="utf-8",
            )
            document = {
                "schema_name": "local_ai_lab_docling_serve_chunked",
                "chunks": [
                    {
                        "page_range": [page_no, page_no],
                        "document": {
                            "pages": {
                                "1": {"size": {"width": 100, "height": 100}}
                            },
                            "tables": [
                                {
                                    "self_ref": raw_ref,
                                    "label": "table",
                                    "data": _table_data([[body]]),
                                    "prov": [{"page_no": 1}],
                                }
                            ],
                        },
                    }
                    for page_no, body in zip((3, 4), bodies)
                ],
            }
            normalized = adapter._document_with_resolved_page_provenance(document)
            tables = adapter.extract_table_nodes(normalized)

            result = adapter.append_structured_table_source_renderings(
                output_dir, normalized, tables
            )

            self.assertEqual(refs, result["candidate_source_refs"])
            self.assertEqual(refs, result["html_bound_source_refs"])
            self.assertEqual(refs, result["markdown_bound_source_refs"])
            self.assertEqual(refs, result["html_body_identity_verified_refs"])
            self.assertEqual(refs, result["markdown_body_identity_verified_refs"])
            self.assertEqual([], result["html_body_identity_mismatch_refs"])
            self.assertEqual([], result["markdown_body_identity_mismatch_refs"])

    def test_code_source_evidence_binds_by_stable_source_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_1.png", (200, 200))
            source_ref = "#/texts/3"
            (output_dir / "document.html").write_text(
                '<html><body><section class="code-listing">'
                f'<pre data-source-ref="{source_ref}"><code>print(1)</code></pre>'
                "</section></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-code-ref:{source_ref} -->\n"
                "> ~~~python\n> print(1)\n> ~~~\n",
                encoding="utf-8",
            )
            code = {
                "self_ref": source_ref,
                "label": "code",
                "text": "print(1)",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 5,
                            "r": 95,
                            "t": 95,
                            "b": 5,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
            document = {
                "pages": {"1": {"size": {"width": 100, "height": 100}}},
                "texts": [code],
            }

            result = adapter.append_code_source_renderings(output_dir, document)

            self.assertEqual([source_ref], result["html_bound_source_refs"])
            self.assertEqual([source_ref], result["markdown_bound_source_refs"])
            self.assertEqual([], result["html_unbound_source_refs"])
            self.assertEqual(
                [source_ref], result["markdown_body_identity_verified_refs"]
            )

    def test_chunk_local_code_page_maps_to_global_source_crop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_3.png", (200, 200))
            raw_source_ref = "#/texts/0"
            source_ref = f"chunk:0:{raw_source_ref}"
            body = "result = left + right"
            (output_dir / "document.html").write_text(
                '<html><body><section class="code-listing">'
                f'<pre data-source-ref="{source_ref}"><code>{body}</code></pre>'
                "</section></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-code-ref:{source_ref} -->\n```python\n{body}\n```\n",
                encoding="utf-8",
            )
            code = {
                "self_ref": raw_source_ref,
                "label": "code",
                "text": body,
                "prov": [
                    {
                        # Docling chunk payloads may restart provenance page
                        # numbers at one even though rendered pages are global.
                        "page_no": 1,
                        "bbox": {
                            "l": 5,
                            "r": 95,
                            "t": 95,
                            "b": 5,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
            document = {
                "schema_name": "local_ai_lab_docling_serve_chunked",
                "chunks": [
                    {
                        "page_range": [3, 3],
                        "document": {
                            "pages": {
                                "1": {"size": {"width": 100, "height": 100}}
                            },
                            "texts": [code],
                        },
                    }
                ],
            }

            result = adapter.append_code_source_renderings(output_dir, document)

            self.assertEqual(1, result["candidate_count"])
            self.assertEqual(3, result["candidates"][0]["page_no"])
            self.assertEqual(1, result["candidates"][0]["source_page_no"])
            self.assertTrue(output_dir.joinpath(result["source_images"][0]).is_file())
            self.assertEqual([source_ref], result["html_bound_source_refs"])
            self.assertEqual([source_ref], result["markdown_bound_source_refs"])
            self.assertEqual(
                [source_ref], result["html_body_identity_verified_refs"]
            )
            self.assertEqual(
                [source_ref], result["markdown_body_identity_verified_refs"]
            )

    def test_chunk_global_page_inventory_does_not_double_offset(self) -> None:
        # Production chunk responses normally preserve global keys/provenance;
        # the local-page compatibility branch must not remap page 9 to page 17.
        document = {
            "schema_name": "local_ai_lab_docling_serve_chunked",
            "chunks": [
                {
                    "page_range": [9, 16],
                    "document": {
                        "pages": {
                            "9": {
                                "page_no": 9,
                                "size": {"width": 612, "height": 792},
                            }
                        }
                    },
                }
            ],
        }

        inventory = adapter._document_page_size_inventory(document)

        self.assertEqual([9], sorted(inventory["page_records"]))
        self.assertEqual(
            9,
            adapter._resolve_document_page_number(
                inventory, 9, part_index=0
            ),
        )

    def test_chunk_local_table_formula_picture_crops_use_physical_page(self) -> None:
        from PIL import Image, ImageDraw

        class FakePage:
            def __init__(self, page_no: int) -> None:
                self.page_no = page_no

            def get_size(self) -> tuple[int, int]:
                return (100, 100)

            def render(self, *, scale: float) -> object:
                page_no = self.page_no

                class Bitmap:
                    def to_pil(self) -> Image.Image:
                        colors = {
                            1: (255, 190, 190),
                            2: (190, 255, 190),
                            3: (190, 190, 255),
                        }
                        image = Image.new(
                            "RGB",
                            (int(100 * scale), int(100 * scale)),
                            colors[page_no],
                        )
                        ImageDraw.Draw(image).rectangle(
                            (70, 70, 130, 130), fill="black"
                        )
                        return image

                return Bitmap()

        class FakePdfDocument:
            def __init__(self, _path: str) -> None:
                self.pages = [FakePage(index) for index in range(1, 4)]

            def __len__(self) -> int:
                return len(self.pages)

            def __getitem__(self, index: int) -> FakePage:
                return self.pages[index]

            def close(self) -> None:
                return None

        bbox = {
            "l": 25,
            "r": 75,
            "t": 75,
            "b": 25,
            "coord_origin": "BOTTOMLEFT",
        }
        table = {
            "self_ref": "#/tables/0",
            "label": "table",
            "data": _table_data([["cell"]]),
            "prov": [{"page_no": 1, "bbox": bbox}],
        }
        formula = {
            "self_ref": "#/texts/0",
            "label": "formula",
            "text": "x + y",
            "prov": [{"page_no": 1, "bbox": bbox}],
        }
        picture = {
            "self_ref": "#/pictures/0",
            "label": "picture",
            "prov": [{"page_no": 1, "bbox": bbox}],
        }
        raw_document = {
            "schema_name": "local_ai_lab_docling_serve_chunked",
            "chunks": [
                {
                    "page_range": [3, 3],
                    "document": {
                        "pages": {
                            "1": {"size": {"width": 100, "height": 100}}
                        },
                        "tables": [table],
                        "texts": [formula],
                        "pictures": [picture],
                    },
                }
            ],
        }
        document = adapter._document_with_resolved_page_provenance(raw_document)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            fake_pdfium = SimpleNamespace(PdfDocument=FakePdfDocument)
            with patch.dict(sys.modules, {"pypdfium2": fake_pdfium}):
                counts, warnings, metrics, manifest = adapter.render_page_images_and_crops(
                    output_dir / "source.pdf",
                    output_dir,
                    adapter.extract_table_nodes(document),
                    adapter.extract_label_nodes(document, "formula"),
                    adapter.extract_label_nodes(document, "picture"),
                )

            self.assertEqual([], warnings)
            self.assertEqual(1, counts["table_image_count"])
            self.assertEqual(1, len(manifest["tables"]))
            self.assertEqual(1, counts["formula_asset_count"])
            self.assertEqual(1, counts["picture_artifact_count"])
            self.assertEqual(3, metrics[0]["page_no"])
            self.assertEqual(3, metrics[0]["source"]["page_no"])
            with Image.open(output_dir / "formulas" / "formula_1.png") as crop:
                self.assertEqual((190, 190, 255), crop.convert("RGB").getpixel((0, 0)))
            self.assertTrue((output_dir / "tables" / "table_1.png").is_file())
            self.assertTrue((output_dir / "pictures" / "picture_1.png").is_file())
        self.assertEqual(1, table["prov"][0]["page_no"])

    def test_chunk_algorithm_page_and_crop_are_global_and_refs_are_qualified(self) -> None:
        raw_ref = "#/texts/0"

        def chunk_document(raw_page_no: int, page_range: list[int]) -> dict[str, object]:
            node = {
                "self_ref": raw_ref,
                "label": "code",
                "text": (
                    "Require: alpha while theta not converged do "
                    "update theta return theta"
                ),
                "prov": [
                    {
                        "page_no": raw_page_no,
                        "bbox": {
                            "l": 5,
                            "r": 95,
                            "t": 95,
                            "b": 5,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
            return {
                "schema_name": "local_ai_lab_docling_serve_chunked",
                "chunks": [
                    {
                        "page_range": page_range,
                        "document": {
                            "pages": {
                                str(raw_page_no): {
                                    "size": {"width": 100, "height": 100}
                                }
                            },
                            "texts": [node],
                        },
                    }
                ],
            }

        local_record = adapter._algorithm_candidate_records(
            chunk_document(1, [3, 3]), Path("missing.pdf")
        )[0]
        global_record = adapter._algorithm_candidate_records(
            chunk_document(3, [3, 4]), Path("missing.pdf")
        )[0]

        self.assertEqual(3, local_record["page_no"])
        self.assertEqual(3, global_record["page_no"])
        self.assertEqual(f"chunk:0:{raw_ref}", local_record["source_ref"])

        class FakePdf:
            pages = [
                SimpleNamespace(width=100, height=100),
                SimpleNamespace(width=100, height=100),
                SimpleNamespace(width=100, height=100),
            ]

            def __enter__(self) -> "FakePdf":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_3.png", (200, 200))
            fake_pdfplumber = SimpleNamespace(open=lambda _path: FakePdf())
            with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
                written = adapter._write_algorithm_source_crops(
                    output_dir,
                    output_dir / "source.pdf",
                    [local_record],
                )

            self.assertEqual(["algorithms/algorithm_1.png"], written)
            self.assertEqual(3, local_record["source_image_page_no"])

    def test_algorithm_crop_binds_distinct_visual_and_conversion_pdf_hashes(self) -> None:
        raw_document = {
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "code",
                    "text": "Algorithm 1: Input: x while x < y do update x return x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 90,
                                "t": 90,
                                "b": 10,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }
        document = adapter._document_with_resolved_page_provenance(raw_document)
        record = adapter._algorithm_candidate_records(
            document, Path("missing.pdf")
        )[0]

        class FakePdf:
            pages = [SimpleNamespace(width=100, height=100)]

            def __enter__(self) -> "FakePdf":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            page_path = output_dir / "pages" / "page_1.png"
            _visible_png(page_path, (200, 200))
            visual_pdf = output_dir / "visual.pdf"
            conversion_pdf = output_dir / "conversion.pdf"
            visual_pdf.write_bytes(b"visual-pdf-contract")
            conversion_pdf.write_bytes(b"conversion-pdf-contract")
            visual_sha = adapter.file_sha256(visual_pdf)
            conversion_sha = adapter.file_sha256(conversion_pdf)
            metadata: dict[str, object] = {
                "generated_outputs": [],
                "visual_evidence_input_sha256": visual_sha,
                "conversion_input_sha256": conversion_sha,
                "structural_visual_provenance_manifest": {
                    "version": adapter.STRUCTURAL_VISUAL_PROVENANCE_VERSION,
                    "visual_pdf_sha256": visual_sha,
                    "pages": {
                        "1": {
                            "page_no": 1,
                            "path": "pages/page_1.png",
                            "page_size": {"width": 100, "height": 100},
                            "semantic_page_size": {"width": 100, "height": 100},
                            "page_image_size": {"width": 200, "height": 200},
                            "page_image_sha256": adapter.file_sha256(page_path),
                            "visual_pdf_sha256": visual_sha,
                        }
                    },
                    "tables": [],
                    "code": [],
                    "algorithms": [],
                },
            }
            fake_pdfplumber = SimpleNamespace(open=lambda _path: FakePdf())
            with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
                written, entries, diagnostics = adapter._write_algorithm_source_crops(
                    output_dir,
                    visual_pdf,
                    [record],
                    document_json=document,
                    semantic_pdf_path=conversion_pdf,
                    metadata=metadata,
                    return_provenance=True,
                )

            self.assertEqual(["algorithms/algorithm_1.png"], written)
            self.assertEqual({}, diagnostics)
            self.assertEqual(1, len(entries))
            self.assertEqual(visual_sha, entries[0]["visual_pdf_sha256"])
            self.assertEqual(conversion_sha, entries[0]["semantic_pdf_sha256"])
            self.assertEqual(conversion_sha, entries[0]["conversion_pdf_sha256"])
            self.assertTrue(record["provenance_verified"])
            self.assertTrue(output_dir.joinpath(written[0]).is_file())

    def test_table_body_identity_rejects_changed_cell_with_same_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            _visible_png(output_dir / "tables" / "table_1.png")
            source_ref = "#/tables/0"
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                f'<table data-source-ref="{source_ref}">'
                "<tr><th>value</th></tr><tr><td>left - right</td></tr>"
                "</table></figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-table-ref:{source_ref} -->\n\n"
                "| value |\n|---|\n| left - right |\n",
                encoding="utf-8",
            )
            table = {
                "self_ref": source_ref,
                "data": _table_data([["value"], ["left + right"]]),
            }

            result = adapter.append_structured_table_source_renderings(
                output_dir,
                {"tables": [table]},
                [table],
            )

            self.assertEqual([source_ref], result["html_bound_source_refs"])
            self.assertEqual([source_ref], result["markdown_bound_source_refs"])
            self.assertEqual(
                [source_ref], result["html_body_identity_mismatch_refs"]
            )
            self.assertEqual(
                [source_ref], result["markdown_body_identity_mismatch_refs"]
            )

    def test_table_body_identity_ignores_soft_breaks_inside_same_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            _visible_png(output_dir / "tables" / "table_1.png")
            source_ref = "#/tables/0"
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                f'<table data-source-ref="{source_ref}"><tr><td>'
                "x<sub>i</sub> = left<br>+ right"
                "</td></tr></table></figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-table-ref:{source_ref} -->\n\n"
                "| x_i = left<br>+ right |\n|---|\n",
                encoding="utf-8",
            )
            table = {
                "self_ref": source_ref,
                "data": _table_data([["x_i = left + right"]]),
            }

            result = adapter.append_structured_table_source_renderings(
                output_dir, {"tables": [table]}, [table]
            )

            self.assertEqual(
                [source_ref], result["html_body_identity_verified_refs"]
            )
            self.assertEqual(
                [source_ref], result["markdown_body_identity_verified_refs"]
            )
            self.assertEqual([], result["html_body_identity_mismatch_refs"])
            self.assertEqual([], result["markdown_body_identity_mismatch_refs"])

    def test_table_identity_ignores_link_and_footnote_presentation_only(self) -> None:
        source = [["SciTSR [3]", "PDF ∗", "C 2", "O(mn 2)"]]
        markdown = adapter._split_markdown_table_row(
            "| [SciTSR [3]](#ref-7) | "
            '<sup id="fnref-table-1"><a href="#fn-table-1">∗</a></sup> PDF | '
            "C2 | O(mn2) |"
        )
        # Keep the marker in the same visible order as the source cell.
        markdown[1] = "PDF " + markdown[1].removesuffix(" PDF").strip()

        self.assertEqual(
            adapter._table_grid_body_identity(source),
            adapter._table_grid_body_identity([markdown]),
        )

    def test_code_body_identity_rejects_operator_and_variable_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_1.png", (200, 200))
            source_ref = "#/texts/3"
            changed = "total = left - wrong"
            (output_dir / "document.html").write_text(
                '<html><body><section class="code-listing">'
                f'<pre data-source-ref="{source_ref}"><code>{changed}</code></pre>'
                "</section></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-code-ref:{source_ref} -->\n```python\n{changed}\n```\n",
                encoding="utf-8",
            )
            code = {
                "self_ref": source_ref,
                "label": "code",
                "text": "total = left + right",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 5,
                            "r": 95,
                            "t": 95,
                            "b": 5,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
            document = {
                "pages": {"1": {"size": {"width": 100, "height": 100}}},
                "texts": [code],
            }

            result = adapter.append_code_source_renderings(output_dir, document)

            self.assertEqual([source_ref], result["html_bound_source_refs"])
            self.assertEqual(
                [source_ref], result["html_body_identity_mismatch_refs"]
            )
            self.assertEqual(
                [source_ref], result["markdown_body_identity_mismatch_refs"]
            )

    def test_code_body_identity_accepts_semantic_number_prefix_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_1.png", (200, 200))
            source_ref = "#/texts/4"
            body = "print(left + right)\nreturn left"
            (output_dir / "document.html").write_text(
                '<html><body><section class="code-listing">'
                f'<pre data-source-ref="{source_ref}"><code>{body}</code></pre>'
                "</section></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-code-ref:{source_ref} -->\n```python\n{body}\n```\n",
                encoding="utf-8",
            )
            code = {
                "self_ref": source_ref,
                "label": "code",
                "text": "1 print(left + right) 2 return left",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 5,
                            "r": 95,
                            "t": 95,
                            "b": 5,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
            result = adapter.append_code_source_renderings(
                output_dir,
                {
                    "pages": {"1": {"size": {"width": 100, "height": 100}}},
                    "texts": [code],
                },
            )

            self.assertEqual(
                [source_ref], result["html_body_identity_verified_refs"]
            )
            self.assertEqual(
                [source_ref], result["markdown_body_identity_verified_refs"]
            )

    def test_code_body_identity_ignores_only_physical_line_wrapping(self) -> None:
        expected = "Input = [CLS] first [SEP] second [SEP] Label = NotNext"
        wrapped = "Input = [CLS] first [SEP]\n    second [SEP] Label = NotNext"

        self.assertEqual(
            adapter._code_body_identity(expected),
            adapter._code_body_identity(wrapped),
        )
        self.assertNotEqual(
            adapter._code_body_identity(expected),
            adapter._code_body_identity(wrapped.replace("NotNext", "IsNext")),
        )

    def test_algorithm_body_identity_rejects_changed_body_with_same_steps(self) -> None:
        source_ref = "#/texts/8"
        record = {
            "label": "Algorithm 1",
            "text": "1: x = x + 1\n2: return x",
            "source": "test_record",
            "source_ref": source_ref,
        }
        expected = adapter._algorithm_expected_body_identity(record)
        original_html = adapter._algorithm_record_html(record)
        original_markdown = adapter._algorithm_record_markdown(record)
        self.assertEqual(
            expected,
            adapter._algorithm_html_body_identity_for_source_ref(
                original_html, source_ref
            ),
        )
        self.assertEqual(
            expected,
            adapter._algorithm_markdown_body_identity_for_source_ref(
                original_markdown, source_ref
            ),
        )
        html_text = original_html.replace("x + 1", "x - 1")
        markdown_text = original_markdown.replace("x + 1", "x - 1")

        self.assertEqual(
            ["1", "2"],
            re.findall(r"(?m)^\s*(\d+)\s*:", record["text"]),
        )
        self.assertNotEqual(
            expected,
            adapter._algorithm_html_body_identity_for_source_ref(
                html_text, source_ref
            ),
        )
        self.assertNotEqual(
            expected,
            adapter._algorithm_markdown_body_identity_for_source_ref(
                markdown_text, source_ref
            ),
        )

    def test_algorithm_identity_reads_normal_semantic_section_and_optional_colons(self) -> None:
        source_ref = "#/tables/2"
        record = {
            "text": "Require: x\n1: y = x + 1\n2: return y",
            "source_ref": source_ref,
        }
        document_html = (
            '<section class="algorithm"><pre data-source-ref="#/tables/2"><code>'
            "Require: x\n"
            '<span class="line-number">1</span> y = x + 1\n'
            '<span class="line-number">2</span> return y'
            "</code></pre></section>"
        )

        self.assertEqual(
            adapter._algorithm_expected_body_identity(record),
            adapter._algorithm_html_body_identity_for_source_ref(
                document_html,
                source_ref,
            ),
        )

    def test_algorithm_source_image_binds_to_normal_semantic_occurrence(self) -> None:
        source_ref = "#/tables/2"
        record = {
            "label": "Algorithm 1 Demo",
            "source_ref": source_ref,
            "source_image": "algorithms/algorithm_1.png",
        }
        document_html = (
            '<html><body><section class="algorithm">'
            f'<pre data-source-ref="{source_ref}"><code>'
            '<span class="line-number">1</span> return x'
            "</code></pre></section></body></html>"
        )
        document_markdown = (
            f"<!-- source-algorithm-ref:{source_ref} -->\n"
            '<pre class="algorithm"><code>'
            '<span class="line-number">1</span> return x'
            "</code></pre>\n"
        )

        updated_html, html_changed = (
            adapter._bind_algorithm_source_evidence_html(
                document_html,
                record,
            )
        )
        updated_markdown, markdown_changed = (
            adapter._bind_algorithm_source_evidence_markdown(
                document_markdown,
                record,
            )
        )

        self.assertTrue(html_changed)
        self.assertTrue(markdown_changed)
        html_range = adapter._html_section_range_for_source_ref(
            updated_html,
            css_class="algorithm",
            source_ref=source_ref,
        )
        markdown_range = adapter._markdown_marker_range(
            updated_markdown,
            kind="algorithm",
            source_ref=source_ref,
        )
        self.assertIsNotNone(html_range)
        self.assertIsNotNone(markdown_range)
        assert html_range is not None
        assert markdown_range is not None
        self.assertIn(
            record["source_image"],
            updated_html[html_range[0] : html_range[1]],
        )
        self.assertIn(
            record["source_image"],
            updated_markdown[markdown_range[0] : markdown_range[1]],
        )
        self.assertEqual(
            adapter._algorithm_body_identity("1 return x"),
            adapter._algorithm_html_body_identity_for_source_ref(
                updated_html,
                source_ref,
            ),
        )

    def test_masked_lm_input_label_example_is_not_an_algorithm(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "texts": [
                {
                    "self_ref": "#/texts/418",
                    "label": "code",
                    "text": (
                        "Input: The capital of France is [MASK].\n"
                        "Label: Paris\n"
                        "Input IDs: 101 1996 3007 102"
                    ),
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 5,
                                "r": 95,
                                "t": 95,
                                "b": 5,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        records = adapter._algorithm_candidate_records(
            document, Path("missing.pdf")
        )

        self.assertEqual([], records)

    def test_structural_gate_reports_body_identity_mismatch_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            source_ref = "#/texts/8"
            (output_dir / "document.html").write_text(
                '<html><body><section class="algorithm">'
                '<span class="line-number">1</span>'
                '<span class="line-number">2</span></section></body></html>',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "```text\n1: x = x - 1\n2: return x\n```\n",
                encoding="utf-8",
            )
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"tables": 0, "algorithms": 1, "code_blocks": 0}
                    },
                    "final_source_visuals": {
                        "table_source_expected_indexes": [],
                        "algorithm_source_candidate_count": 1,
                        "algorithm_source_expected_indexes": [1],
                        "algorithm_source_html_indexes": [1],
                        "algorithm_source_markdown_indexes": [1],
                        "algorithm_source_valid_image_indexes": [1],
                        "algorithm_source_expected_refs": [source_ref],
                        "algorithm_source_html_bound_refs": [source_ref],
                        "algorithm_source_markdown_bound_refs": [source_ref],
                        "algorithm_source_body_identity_expected_refs": [source_ref],
                        "algorithm_source_html_body_identity_verified_refs": [],
                        "algorithm_source_markdown_body_identity_verified_refs": [],
                        "algorithm_source_html_body_identity_mismatch_refs": [source_ref],
                        "algorithm_source_markdown_body_identity_mismatch_refs": [source_ref],
                        "algorithm_source_renderings": {
                            "records": [{"numbered_steps": [1, 2]}]
                        },
                        "code_source_expected_indexes": [],
                        "code_source_candidate_count": 0,
                    },
                },
            }

            result = adapter.validate_final_structural_surfaces(
                output_dir, metadata, status
            )

            self.assertFalse(result["ok"])
            self.assertEqual([source_ref], result["body_identity_mismatch_refs"])
            self.assertIn(
                "structural_body_identity_mismatch", result["failure_reasons"]
            )

    def test_structural_gate_rejects_omitted_nonempty_body_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            source_ref = "#/tables/0"
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                f'<table data-source-ref="{source_ref}"><tr><td>WRONG</td></tr>'
                "</table></figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-table-ref:{source_ref} -->\n| WRONG |\n|---|\n",
                encoding="utf-8",
            )
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"tables": 1, "algorithms": 0, "code_blocks": 0}
                    },
                    "final_source_visuals": {
                        "table_source_candidate_count": 1,
                        "table_source_reclassified_algorithm_indexes": [],
                        "table_source_expected_indexes": [1],
                        "table_source_html_indexes": [1],
                        "table_source_markdown_indexes": [1],
                        "table_source_valid_image_indexes": [1],
                        "table_source_unexpected_indexes": [],
                        "table_source_expected_refs": [source_ref],
                        "table_source_html_bound_refs": [source_ref],
                        "table_source_markdown_bound_refs": [source_ref],
                        "table_source_body_identity_expected_refs": [],
                        "table_source_html_body_identity_verified_refs": [],
                        "table_source_markdown_body_identity_verified_refs": [],
                        "table_source_html_body_identity_mismatch_refs": [],
                        "table_source_markdown_body_identity_mismatch_refs": [],
                        "table_empty_fallback_expected_refs": [],
                        "table_empty_fallback_exact_coverage": True,
                        "algorithm_source_expected_indexes": [],
                        "algorithm_source_candidate_count": 0,
                        "code_source_expected_indexes": [],
                        "code_source_candidate_count": 0,
                    },
                },
            }

            result = adapter.validate_final_structural_surfaces(
                output_dir, {}, status
            )

            self.assertFalse(result["ok"])
            self.assertIn(
                "incomplete_table_source_visual_coverage",
                result["failure_reasons"],
            )

    def test_cn_mutation_precedes_final_inventory_and_is_caught_by_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            _visible_png(output_dir / "tables" / "table_1.png")
            source_ref = "#/tables/0"
            document = {
                "tables": [
                    {
                        "self_ref": source_ref,
                        "label": "table",
                        "data": _table_data([["A + B"]]),
                    }
                ],
                "texts": [],
            }
            (output_dir / "document.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            (output_dir / "document.html").write_text(
                '<html><body><figure class="semantic-table">'
                f'<table data-source-ref="{source_ref}"><tr><td>A + B</td></tr>'
                "</table></figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-table-ref:{source_ref} -->\n\n| A + B |\n|---|\n",
                encoding="utf-8",
            )
            metadata: dict[str, object] = {"generated_outputs": []}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {
                            "tables": 1,
                            "algorithms": 0,
                            "code_blocks": 0,
                            "formulas": 0,
                        }
                    }
                },
            }
            events: list[str] = []
            restored_table_labels: list[str] = []

            def mutate_after_cn(*args: object, **_kwargs: object) -> None:
                events.append("cn")
                mutated = json.loads(
                    (output_dir / "document.json").read_text(encoding="utf-8")
                )
                mutated["tables"][0]["label"] = "quarantined_page_header"
                (output_dir / "document.json").write_text(
                    json.dumps(mutated), encoding="utf-8"
                )
                baseline_status = args[2]
                baseline_status["ok"] = False
                baseline_status["success_class"] = "degraded_failure"
                baseline_status.setdefault("warnings", []).append(
                    "cn_accepted_baseline_regression:simulated"
                )

            real_restore = adapter.restore_final_delivery_visuals
            real_formula = adapter.validate_final_formula_surfaces
            real_structural = adapter.validate_final_structural_surfaces
            real_reconcile = adapter.reconcile_final_surface_status

            def restore(*args: object, **kwargs: object) -> dict[str, object]:
                events.append("restore")
                restored_table_labels.extend(
                    str(node.get("label") or "")
                    for node in adapter.iter_nodes(args[1])
                    if isinstance(node, dict) and "data" in node
                )
                return real_restore(*args, **kwargs)

            def formula(*args: object, **kwargs: object) -> dict[str, object]:
                events.append("formula")
                return real_formula(*args, **kwargs)

            def structural(*args: object, **kwargs: object) -> dict[str, object]:
                events.append("structural")
                return real_structural(*args, **kwargs)

            def reconcile(*args: object, **kwargs: object) -> dict[str, object]:
                events.append("reconcile")
                return real_reconcile(*args, **kwargs)

            with (
                patch.object(
                    adapter,
                    "record_cn_accepted_baseline",
                    side_effect=mutate_after_cn,
                ),
                patch.object(
                    adapter, "restore_final_delivery_visuals", side_effect=restore
                ),
                patch.object(
                    adapter, "validate_final_formula_surfaces", side_effect=formula
                ),
                patch.object(
                    adapter,
                    "validate_final_structural_surfaces",
                    side_effect=structural,
                ),
                patch.object(
                    adapter, "reconcile_final_surface_status", side_effect=reconcile
                ),
            ):
                result = adapter._finalize_delivery_surfaces(
                    output_dir,
                    document,
                    output_dir / "missing.pdf",
                    output_dir / "missing.pdf",
                    metadata,
                    status,
                    Namespace(),
                    pdf_inventory=None,
                )

            self.assertEqual(
                ["cn", "restore", "formula", "structural", "reconcile"],
                events,
            )
            self.assertFalse(result["structural"]["ok"])
            self.assertEqual(["quarantined_page_header"], restored_table_labels)
            self.assertEqual(0, result["visuals"]["table_source_candidate_count"])
            self.assertIn(
                "semantic_table_inventory_mismatch",
                result["structural"]["failure_reasons"],
            )
            self.assertTrue(
                any(
                    str(warning).startswith("final_structural_surface_failed:")
                    for warning in status["warnings"]
                )
            )

    def test_empty_table_fallback_and_nonempty_body_both_pass_exact_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "tables").mkdir()
            (output_dir / "pages").mkdir()
            page_path = output_dir / "pages" / "page_1.png"
            _visible_png(page_path, size=(100, 100))
            for index in (1, 2):
                _visible_png(output_dir / "tables" / f"table_{index}.png")
            empty_ref = "#/tables/0"
            body_ref = "#/tables/1"
            (output_dir / "document.html").write_text(
                "<html><body>"
                '<figure class="semantic-table">'
                f'<table data-source-ref="{empty_ref}"></table></figure>'
                '<figure class="semantic-table">'
                f'<table data-source-ref="{body_ref}"><tr><td>A + B</td></tr>'
                "</table></figure></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                f"<!-- source-table-ref:{empty_ref} -->\n\n"
                f"<!-- source-table-ref:{body_ref} -->\n\n"
                "| A + B |\n|---|\n",
                encoding="utf-8",
            )
            empty_table = {
                "self_ref": empty_ref,
                "label": "table",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 10,
                            "r": 40,
                            "t": 50,
                            "b": 30,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
                "data": {"num_rows": 0, "num_cols": 0, "table_cells": []},
            }
            body_table = {
                "self_ref": body_ref,
                "label": "table",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 50,
                            "r": 90,
                            "t": 50,
                            "b": 30,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
                "data": _table_data([["A + B"]]),
            }
            document = {
                "pages": {"1": {"size": {"width": 100, "height": 100}}},
                "tables": [empty_table, body_table],
                "texts": [],
            }
            (output_dir / "document.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            visual_sha = "a" * 64
            semantic_sha = "b" * 64
            page_manifest = {
                "page_no": 1,
                "path": "pages/page_1.png",
                "page_size": {"width": 100, "height": 100},
                "semantic_page_size": {"width": 100, "height": 100},
                "page_image_size": {"width": 100, "height": 100},
                "page_image_sha256": adapter.file_sha256(page_path),
                "visual_pdf_sha256": visual_sha,
            }
            table_entries = []
            for index, table in enumerate((empty_table, body_table), start=1):
                bbox = table["prov"][0]["bbox"]
                pixel_box = adapter._bbox_pixel_crop_box(
                    bbox,
                    page_width=100,
                    page_height=100,
                    image_width=100,
                    image_height=100,
                    padding=0,
                )
                metric = {
                    "path": f"tables/table_{index}.png",
                    "page_no": 1,
                    "bbox": bbox,
                    "asset_sha256": adapter.file_sha256(
                        output_dir / "tables" / f"table_{index}.png"
                    ),
                    "source_pdf_sha256": visual_sha,
                    "page_image_path": "pages/page_1.png",
                    "page_image_sha256": adapter.file_sha256(page_path),
                    "padding_px": 0,
                    "pixel_box": pixel_box,
                    "page_size": {"width": 100, "height": 100},
                    "page_image_size": {"width": 100, "height": 100},
                }
                table_entries.append(
                    adapter._structural_visual_provenance_entry(
                        kind="table",
                        index=index,
                        source_ref=str(table["self_ref"]),
                        node=table,
                        body_identity=adapter._table_grid_body_identity(
                            adapter.table_grid(table)
                        ),
                        metric=metric,
                        node_bbox=bbox,
                        part_index=None,
                        semantic_pdf_sha256=semantic_sha,
                        conversion_pdf_sha256=semantic_sha,
                        semantic_page_size={"width": 100, "height": 100},
                        crop_coordinate_page_size={
                            "width": 100,
                            "height": 100,
                        },
                    )
                )
            metadata: dict[str, object] = {
                "generated_outputs": [],
                "visual_evidence_input_sha256": visual_sha,
                "conversion_input_sha256": semantic_sha,
                "structural_visual_provenance_manifest": {
                    "version": adapter.STRUCTURAL_VISUAL_PROVENANCE_VERSION,
                    "visual_pdf_sha256": visual_sha,
                    "pages": {"1": page_manifest},
                    "tables": table_entries,
                    "code": [],
                    "algorithms": [],
                },
            }
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"tables": 2, "algorithms": 0, "code_blocks": 0}
                    }
                },
            }

            visuals = adapter.restore_final_delivery_visuals(
                output_dir,
                document,
                output_dir / "missing.pdf",
                metadata,
                status,
            )
            result = adapter.validate_final_structural_surfaces(
                output_dir, metadata, status
            )

            self.assertTrue(visuals["table_empty_fallback_exact_coverage"])
            self.assertEqual([empty_ref], visuals["table_empty_fallback_expected_refs"])
            self.assertEqual([body_ref], visuals["table_source_body_identity_expected_refs"])
            self.assertTrue(visuals["table_source_exact_coverage"])
            self.assertTrue(result["ok"], result["failure_reasons"])

    def test_structural_provenance_gate_rejects_missing_swapped_and_wrong_pdf(self) -> None:
        for kind in ("table", "code", "algorithm"):
            with self.subTest(kind=kind, case="fresh"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    metadata, status, _asset_path, _page_path = (
                        _structural_gate_fixture(Path(tmpdir), kind)
                    )
                    result = adapter.validate_final_structural_surfaces(
                        Path(tmpdir), metadata, status
                    )
                    self.assertTrue(result["ok"], result["failure_reasons"])
            with self.subTest(kind=kind, case="missing_manifest"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    metadata, status, _asset_path, _page_path = (
                        _structural_gate_fixture(Path(tmpdir), kind)
                    )
                    metadata.pop("structural_visual_provenance_manifest")
                    result = adapter.validate_final_structural_surfaces(
                        Path(tmpdir), metadata, status
                    )
                    self.assertFalse(result["ok"])
                    self.assertIn(
                        f"{kind}_source_provenance_mismatch",
                        result["failure_reasons"],
                    )
            with self.subTest(kind=kind, case="swapped_asset"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    metadata, status, asset_path, _page_path = (
                        _structural_gate_fixture(Path(tmpdir), kind)
                    )
                    _visible_png(asset_path, size=(80, 70))
                    result = adapter.validate_final_structural_surfaces(
                        Path(tmpdir), metadata, status
                    )
                    self.assertFalse(result["ok"])
                    diagnostics = status["quality_signals"]["final_source_visuals"][
                        f"{kind}_source_provenance_diagnostics"
                    ]
                    self.assertIn("asset_sha256_mismatch", next(iter(diagnostics.values())))
            with self.subTest(kind=kind, case="wrong_visual_pdf"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    metadata, status, _asset_path, _page_path = (
                        _structural_gate_fixture(Path(tmpdir), kind)
                    )
                    metadata["visual_evidence_input_sha256"] = "c" * 64
                    result = adapter.validate_final_structural_surfaces(
                        Path(tmpdir), metadata, status
                    )
                    self.assertFalse(result["ok"])
                    diagnostics = status["quality_signals"]["final_source_visuals"][
                        f"{kind}_source_provenance_diagnostics"
                    ]
                    self.assertTrue(
                        any("visual_pdf_sha256_mismatch" in reason for reason in next(iter(diagnostics.values())))
                    )

    def test_structural_gate_recomputes_current_inventory_and_surface_body(self) -> None:
        for kind in ("table", "code", "algorithm"):
            with self.subTest(kind=kind, case="extra_current_ref"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    output_dir = Path(tmpdir)
                    metadata, status, _asset_path, _page_path = (
                        _structural_gate_fixture(output_dir, kind)
                    )
                    if kind == "algorithm":
                        document = json.loads(
                            (output_dir / "document.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        document["texts"].append(
                            {
                                "self_ref": "#/algorithm/extra",
                                "label": "code",
                                "text": (
                                    "Algorithm 2: Input: z while z < q do "
                                    "update z return z"
                                ),
                                "prov": [
                                    {
                                        "page_no": 1,
                                        "bbox": {
                                            "l": 12.0,
                                            "r": 82.0,
                                            "t": 82.0,
                                            "b": 22.0,
                                            "coord_origin": "BOTTOMLEFT",
                                        },
                                    }
                                ],
                            }
                        )
                        (output_dir / "document.json").write_text(
                            json.dumps(document),
                            encoding="utf-8",
                        )
                    else:
                        document = json.loads(
                            (output_dir / "document.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        extra_node = {
                            "self_ref": f"#/{kind}/extra",
                            "label": "table" if kind == "table" else "code",
                            "prov": [
                                {
                                    "page_no": 1,
                                    "bbox": {
                                        "l": 12.0,
                                        "r": 82.0,
                                        "t": 82.0,
                                        "b": 22.0,
                                        "coord_origin": "BOTTOMLEFT",
                                    },
                                }
                            ],
                        }
                        if kind == "table":
                            extra_node["data"] = _table_data([["z = 2"]])
                            document["tables"].append(extra_node)
                        else:
                            extra_node["text"] = "z = z + 2"
                            document["texts"].append(extra_node)
                        (output_dir / "document.json").write_text(
                            json.dumps(document),
                            encoding="utf-8",
                        )
                    result = adapter.validate_final_structural_surfaces(
                        output_dir,
                        metadata,
                        status,
                    )
                    self.assertFalse(result["ok"])
                    self.assertIn(
                        f"{kind}_source_provenance_mismatch",
                        result["failure_reasons"],
                    )

            body_text = {
                "table": "x + y",
                "code": "x = x + 1",
                "algorithm": "return x",
            }[kind]
            for surface, filename in (
                ("html", "document.html"),
                ("markdown", "document.md"),
            ):
                with self.subTest(kind=kind, case=f"mutated_{surface}_body"):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        output_dir = Path(tmpdir)
                        metadata, status, _asset_path, _page_path = (
                            _structural_gate_fixture(output_dir, kind)
                        )
                        path = output_dir / filename
                        original = path.read_text(encoding="utf-8")
                        self.assertIn(body_text, original)
                        path.write_text(
                            original.replace(body_text, "WRONG BODY", 1),
                            encoding="utf-8",
                        )
                        result = adapter.validate_final_structural_surfaces(
                            output_dir,
                            metadata,
                            status,
                        )
                        self.assertFalse(result["ok"])
                        self.assertIn(
                            f"{kind}_body_identity_mismatch",
                            result["failure_reasons"],
                        )

        for kind in ("code", "algorithm"):
            with self.subTest(kind=kind, case="extra_current_ref_without_prov"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    output_dir = Path(tmpdir)
                    metadata, status, _asset_path, _page_path = (
                        _structural_gate_fixture(output_dir, kind)
                    )
                    document = json.loads(
                        (output_dir / "document.json").read_text(encoding="utf-8")
                    )
                    document["texts"].append(
                        {
                            "self_ref": f"#/{kind}/missing-prov",
                            "label": "code",
                            "text": (
                                "Algorithm 2: Input: z while z < q do return z"
                                if kind == "algorithm"
                                else "z = z + 2"
                            ),
                        }
                    )
                    (output_dir / "document.json").write_text(
                        json.dumps(document),
                        encoding="utf-8",
                    )
                    result = adapter.validate_final_structural_surfaces(
                        output_dir,
                        metadata,
                        status,
                    )
                    self.assertFalse(result["ok"])
                    self.assertIn(
                        f"{kind}_source_provenance_mismatch",
                        result["failure_reasons"],
                    )

    def test_code_page_image_swap_and_algorithm_geometry_or_contributor_change_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            metadata, status, _asset_path, page_path = _structural_gate_fixture(
                output_dir, "code"
            )
            _visible_png(page_path, size=(110, 100))
            result = adapter.validate_final_structural_surfaces(
                output_dir, metadata, status
            )
            self.assertFalse(result["ok"])
            diagnostics = status["quality_signals"]["final_source_visuals"][
                "code_source_provenance_diagnostics"
            ]
            self.assertIn("page_image_sha256_mismatch", next(iter(diagnostics.values())))

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            metadata, status, _asset_path, _page_path = _structural_gate_fixture(
                output_dir, "algorithm"
            )
            manifest = metadata["structural_visual_provenance_manifest"]
            manifest["pages"]["1"]["page_size"] = {
                "width": 200,
                "height": 200,
            }
            result = adapter.validate_final_structural_surfaces(
                output_dir, metadata, status
            )
            self.assertFalse(result["ok"])
            diagnostics = status["quality_signals"]["final_source_visuals"][
                "algorithm_source_provenance_diagnostics"
            ]
            self.assertIn(
                "page_manifest_geometry_incompatible",
                next(iter(diagnostics.values())),
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            metadata, status, _asset_path, _page_path = _structural_gate_fixture(
                output_dir, "algorithm"
            )
            document = json.loads((output_dir / "document.json").read_text())
            document["texts"][0]["text"] = "Algorithm 1: Input changed return z"
            (output_dir / "document.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            result = adapter.validate_final_structural_surfaces(
                output_dir, metadata, status
            )
            self.assertFalse(result["ok"])
            diagnostics = status["quality_signals"]["final_source_visuals"][
                "algorithm_source_provenance_diagnostics"
            ]
            self.assertTrue(
                any(
                    "algorithm_source_node_body_mismatch" in reason
                    for reason in next(iter(diagnostics.values()))
                )
            )

    def test_unsorted_chunks_keep_formula_crop_indexes_in_semantic_page_order(self) -> None:
        late = _formula_node("b = 3", page_no=1)
        late["self_ref"] = "#/texts/0"
        early = _formula_node("a = 1", page_no=1)
        early["self_ref"] = "#/texts/0"
        raw_document = {
            "schema_name": "local_ai_lab_docling_serve_chunked",
            "chunks": [
                {
                    "page_range": [3, 3],
                    "document": {
                        "pages": {"1": {"size": {"width": 100, "height": 100}}},
                        "texts": [late],
                    },
                },
                {
                    "page_range": [1, 1],
                    "document": {
                        "pages": {"1": {"size": {"width": 100, "height": 100}}},
                        "texts": [early],
                    },
                },
            ],
        }
        normalized = adapter._document_with_resolved_page_provenance(raw_document)
        formulas = adapter.extract_label_nodes(normalized, "formula")
        self.assertEqual(["a = 1", "b = 3"], [item["text"] for item in formulas])
        self.assertEqual([1, 3], [(adapter.first_prov(item) or {})["page_no"] for item in formulas])
        self.assertEqual(
            ["chunk:0:#/texts/0", "chunk:1:#/texts/0"],
            [
                adapter._structural_node_source_ref(
                    item,
                    kind="formula",
                    fallback_index=index,
                )
                for index, item in enumerate(formulas)
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            for index in (1, 2):
                _visible_png(output_dir / "formulas" / f"formula_{index}.png")
                _visible_png(
                    output_dir / "formulas" / f"formula_{index}_context.png"
                )
            (output_dir / "document.html").write_text(
                '<html><body><div class="formula" data-formula-index="1">'
                "a = 1</div><!-- source-formula-anchor:1 -->"
                '<div class="formula" data-formula-index="2">'
                "b = 3</div><!-- source-formula-anchor:2 -->"
                "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\na = 1\n$$\n<!-- source-formula-anchor:1 -->\n\n"
                "$$\nb = 3\n$$\n<!-- source-formula-anchor:2 -->\n",
                encoding="utf-8",
            )
            diagnostics = [
                _formula_crop_diagnostic(output_dir, index, formula=formula)
                for index, formula in enumerate(formulas, start=1)
            ]
            result = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                formula_crop_diagnostics=diagnostics,
                expected_indexes={1, 2},
                metadata={"visual_evidence_input_sha256": "a" * 64},
            )
            self.assertEqual([1, 2], result["html_covered_indexes"], result)
            self.assertEqual([1, 2], result["markdown_covered_indexes"])
            self.assertEqual([1, 3], [item["page_no"] for item in result["candidates"]])

    def test_algorithm_cluster_contributor_bindings_are_bijective_and_distinct_tables_survive(self) -> None:
        first = {
            "self_ref": "#/texts/0",
            "label": "text",
            "text": "Input: x",
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 10,
                        "r": 80,
                        "t": 80,
                        "b": 60,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ],
        }
        second = {
            "self_ref": "#/texts/1",
            "label": "text",
            "text": "return x",
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 10,
                        "r": 80,
                        "t": 55,
                        "b": 35,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ],
        }
        document = {"texts": [first, second]}
        bindings = [
            adapter._algorithm_source_node_binding(
                node,
                source_ref=str(node["self_ref"]),
            )
            for node in (first, second)
        ]
        verified, reasons, nodes = adapter._algorithm_source_node_bindings_verify(
            document, bindings
        )
        self.assertTrue(verified, reasons)
        self.assertEqual(2, len(nodes))
        second["text"] = "return y"
        verified, reasons, _nodes = adapter._algorithm_source_node_bindings_verify(
            document, bindings
        )
        self.assertFalse(verified)
        self.assertTrue(any("body_mismatch" in reason for reason in reasons))

        tables = []
        for index, left in enumerate((10, 110)):
            tables.append(
                {
                    "self_ref": f"#/tables/{index}",
                    "label": "table",
                    "data": _table_data([["Algorithm 1", "return x"]]),
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": left,
                                "r": left + 80,
                                "t": 80,
                                "b": 20,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            )
        with (
            patch.object(
                adapter,
                "_pdf_text_for_bbox",
                return_value=(
                    "Algorithm 1: Input: x while x < y do update x return x"
                ),
            ),
            patch.object(adapter, "_pdf_algorithm_layout_for_bbox", return_value=None),
        ):
            records = adapter._algorithm_candidate_records(
                {"texts": [], "tables": tables}, Path(__file__)
            )
        table_records = [record for record in records if record.get("table_index")]
        self.assertEqual(2, len(table_records))
        self.assertEqual(
            {"#/tables/0", "#/tables/1"},
            {record["source_ref"] for record in table_records},
        )

    def test_dropped_formula_raw_index_uses_verified_appendix_without_shifting_survivors(self) -> None:
        def run_case(
            *,
            wrong_body: bool = False,
            missing_raw_hash: bool = False,
            duplicate: bool = False,
            overlap: bool = False,
            out_of_range: bool = False,
        ) -> dict[str, object]:
            output_dir = Path(tempfile.mkdtemp())
            self.addCleanup(lambda: __import__("shutil").rmtree(output_dir, ignore_errors=True))
            (output_dir / "formulas").mkdir()
            formulas = [
                _formula_node("x = 1"),
                _formula_node("(2)"),
                _formula_node("z = 3"),
            ]
            for index, formula in enumerate(formulas, start=1):
                formula["self_ref"] = f"#/texts/{index - 1}"
                _visible_png(output_dir / "formulas" / f"formula_{index}.png")
                _visible_png(
                    output_dir / "formulas" / f"formula_{index}_context.png"
                )
            (output_dir / "document.html").write_text(
                '<html><body><div class="formula" data-formula-index="1">'
                "x = 1</div><!-- source-formula-anchor:1 -->"
                '<div class="formula" data-formula-index="3">'
                "z = 3</div><!-- source-formula-anchor:3 -->"
                "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\nx = 1\n$$\n<!-- source-formula-anchor:1 -->\n\n"
                "$$\nz = 3\n$$\n<!-- source-formula-anchor:3 -->\n",
                encoding="utf-8",
            )
            diagnostics = [
                _formula_crop_diagnostic(output_dir, index, formula=formula)
                for index, formula in enumerate(formulas, start=1)
            ]
            if missing_raw_hash:
                diagnostics[1].pop("formula_raw_content_sha256", None)
                for key in ("source", "context"):
                    diagnostics[1][key].pop("formula_raw_content_sha256", None)
            document = {
                "pages": {"1": {"size": {"width": 100, "height": 100}}},
                "texts": formulas,
                "tables": [],
            }
            (output_dir / "document.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            metadata: dict[str, object] = {
                "generated_outputs": [],
                "formula_crop_diagnostics": diagnostics,
                "visual_evidence_input_sha256": "a" * 64,
                "conversion_input_sha256": "b" * 64,
                "structural_visual_provenance_manifest": {
                    "version": adapter.STRUCTURAL_VISUAL_PROVENANCE_VERSION,
                    "visual_pdf_sha256": "a" * 64,
                    "pages": {},
                    "tables": [],
                    "code": [],
                    "algorithms": [],
                },
            }
            artifact_index = 99 if out_of_range else (1 if overlap else 2)
            artifact_formula = formulas[0 if overlap else 1]
            artifact = {
                "raw_formula_index": artifact_index,
                "reason": "standalone_equation_number",
                "text": (
                    "(9)"
                    if wrong_body
                    else str(artifact_formula.get("text") or "")
                ),
                "page_no": 1,
                "bbox": (adapter.first_prov(artifact_formula) or {})["bbox"],
            }
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {
                            "formulas": 2,
                            "tables": 0,
                            "algorithms": 0,
                            "code_blocks": 0,
                        },
                        "dropped_formula_artifacts": (
                            [artifact, dict(artifact)] if duplicate else [artifact]
                        ),
                    }
                },
            }
            return adapter.restore_final_delivery_visuals(
                output_dir,
                document,
                output_dir / "missing-conversion.pdf",
                metadata,
                status,
                visual_pdf_path=output_dir / "missing-visual.pdf",
            )

        valid = run_case()
        self.assertEqual([1, 3], valid["formula_source_html_indexes"], valid)
        self.assertEqual([1, 3], valid["formula_source_markdown_indexes"])
        self.assertEqual([2], valid["formula_source_allowed_dropped_indexes"])
        self.assertIn(2, valid["formula_source_html_appendix_indexes"])
        self.assertIn(2, valid["formula_source_markdown_appendix_indexes"])
        self.assertEqual([], valid["formula_source_disallowed_dropped_artifacts"])

        for invalid in (
            run_case(wrong_body=True),
            run_case(missing_raw_hash=True),
            run_case(duplicate=True),
            run_case(overlap=True),
            run_case(out_of_range=True),
        ):
            self.assertEqual([], invalid["formula_source_allowed_dropped_indexes"])
            self.assertTrue(invalid["formula_source_disallowed_dropped_artifacts"])

    def test_legacy_review_policy_fails_closed_without_trusted_route_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            failed_route_b = root / "failed-route-b"
            failed_route_b.mkdir()
            (failed_route_b / "document.json").write_text("{}", encoding="utf-8")
            (failed_route_b / "status.json").write_text(
                json.dumps({"ok": False}), encoding="utf-8"
            )
            for case, route_b_dir in (("missing", None), ("failed", failed_route_b)):
                with self.subTest(case=case):
                    output_dir = root / f"job-{case}"
                    output_dir.mkdir()
                    originals = {
                        "document.html": b"<html><body>original</body></html>",
                        "document.md": b"original markdown",
                        "document.json": b'{"texts": []}',
                    }
                    for name, payload in originals.items():
                        (output_dir / name).write_bytes(payload)
                    metadata: dict[str, object] = {"generated_outputs": []}
                    status: dict[str, object] = {
                        "ok": True,
                        "success_class": "success",
                        "warnings": [],
                        "quality_signals": {},
                    }
                    args = Namespace(
                        formula_second_pass_policy="review",
                        formula_second_pass_route_b_dir=route_b_dir,
                        formula_policy="granite_mlx",
                        enable_formula_mlx=False,
                    )

                    with (
                        patch.object(adapter, "run_formula_second_pass") as run_second_pass,
                        patch.object(
                            adapter, "apply_current_formula_display_fallback"
                        ) as fallback,
                    ):
                        adapter.run_optional_formula_second_pass(
                            output_dir, metadata, status, args
                        )

                    self.assertEqual("off", metadata["formula_second_pass_policy"])
                    self.assertFalse(metadata["formula_second_pass_applied"])
                    run_second_pass.assert_not_called()
                    fallback.assert_not_called()
                    for name, payload in originals.items():
                        self.assertEqual(payload, (output_dir / name).read_bytes())

            trusted_route_b = root / "trusted-route-b"
            trusted_route_b.mkdir()
            (trusted_route_b / "document.json").write_text("{}", encoding="utf-8")
            (trusted_route_b / "status.json").write_text(
                json.dumps({"ok": True}), encoding="utf-8"
            )
            (trusted_route_b / "metadata.json").write_text(
                json.dumps({"job_id": "current-job"}), encoding="utf-8"
            )
            self.assertEqual(
                "apply-all",
                adapter.effective_formula_second_pass_policy(
                    Namespace(
                        formula_second_pass_policy="review",
                        formula_second_pass_route_b_dir=trusted_route_b,
                        job_id="current-job",
                    )
                ),
            )
            self.assertEqual(
                "apply-all",
                adapter.effective_formula_second_pass_policy(
                    Namespace(
                        formula_second_pass_policy="apply",
                        formula_second_pass_route_b_dir=None,
                    )
                ),
            )

    def test_auto_route_b_rejects_missing_or_stale_metadata_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def route(name: str, metadata: dict[str, object] | None) -> Path:
                path = root / name
                path.mkdir()
                (path / "document.json").write_text("{}", encoding="utf-8")
                (path / "status.json").write_text(
                    json.dumps({"ok": True}), encoding="utf-8"
                )
                if metadata is not None:
                    (path / "metadata.json").write_text(
                        json.dumps(metadata), encoding="utf-8"
                    )
                return path

            cases = {
                "missing": route("missing", None),
                "stale": route("stale", {"job_id": "previous-job"}),
                "malformed": route("malformed", {"job_id": 7}),
            }
            for case, route_b_dir in cases.items():
                with self.subTest(case=case):
                    self.assertEqual(
                        "off",
                        adapter.effective_formula_second_pass_policy(
                            Namespace(
                                formula_second_pass_policy="auto",
                                formula_second_pass_route_b_dir=route_b_dir,
                                job_id="current-job",
                                sample_name=None,
                            )
                        ),
                    )

            matching = route("matching", {"job_id": "current-job"})
            self.assertEqual(
                "apply-all",
                adapter.effective_formula_second_pass_policy(
                    Namespace(
                        formula_second_pass_policy="auto",
                        formula_second_pass_route_b_dir=matching,
                        job_id=None,
                        sample_name="current-job",
                    )
                ),
            )

    def test_spaced_equation_label_cannot_select_visible_context_crop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            _visible_png(
                output_dir / "formulas" / "formula_1.png",
                size=(8, 8),
            )
            _visible_png(output_dir / "formulas" / "formula_1_context.png")
            (output_dir / "document.html").write_text(
                '<html><body><div class="formula formula-tex-fallback">'
                "<code>(1 2)</code></div>"
                "<!-- source-formula-anchor:1 --></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\n(1 2)\n$$\n<!-- source-formula-anchor:1 -->\n",
                encoding="utf-8",
            )
            formulas = [_formula_node("(1 2)")]
            crop_diagnostics = [
                _formula_crop_diagnostic(output_dir, 1, formula=formulas[0])
            ]

            candidates = adapter._formula_indexed_candidates(
                output_dir,
                formulas,
                formula_crop_diagnostics=crop_diagnostics,
            )
            rendered = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                expected_indexes={1},
                formula_crop_diagnostics=crop_diagnostics,
            )

            self.assertIsNone(candidates[0]["selected"])
            self.assertEqual(
                "standalone_equation_number_has_no_formula_body",
                candidates[0]["selection_reason"],
            )
            self.assertIn("standalone_equation_number", candidates[0]["source_reasons"])
            self.assertEqual([1], rendered["missing_candidate_indexes"])
            self.assertEqual([1], rendered["missing_html_indexes"])
            self.assertEqual([1], rendered["missing_markdown_indexes"])

            regular_formula = _formula_node("(x + 2)")
            regular = adapter._formula_indexed_candidates(
                output_dir,
                [regular_formula],
                formula_crop_diagnostics=[
                    _formula_crop_diagnostic(
                        output_dir,
                        1,
                        formula=regular_formula,
                    )
                ],
            )
            self.assertEqual("context", regular[0]["selected"])

    def test_formula_context_allows_benign_geometry_diagnostics_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            _visible_png(output_dir / "formulas" / "formula_1.png", (8, 8))
            _visible_png(
                output_dir / "formulas" / "formula_1_context.png", (120, 60)
            )
            formula = _formula_node(
                    r"p(y_i\mid x_i)=\frac{\exp(s_i)}"
                    r"{\sum_j\exp(s_j)}+\lambda\lVert w\rVert_2^2"
            )
            benign_reasons = [
                "bbox_too_thin_for_complex_formula",
                "bbox_crosses_expected_column_boundary",
                "near_page_bottom_context_needed",
                "source_crop_likely_too_thin",
                "source_crop_likely_useless_for_review",
                "formula_text_too_long",
                "repeated_fraction_pattern",
            ]

            candidates = adapter._formula_indexed_candidates(
                output_dir,
                [formula],
                formula_crop_diagnostics=[
                    _formula_crop_diagnostic(
                        output_dir,
                        1,
                        formula=formula,
                    )
                ],
                suspicious_formula_diagnostics=[
                    {"index": 1, "reasons": benign_reasons}
                ],
            )

            self.assertEqual("context", candidates[0]["selected"])
            self.assertEqual(
                "visible_direct_source_too_small_using_verified_context",
                candidates[0]["selection_reason"],
            )
            self.assertEqual(sorted(benign_reasons), candidates[0]["source_reasons"])

    def test_formula_context_rejects_cjk_prose_and_bare_number_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            for index in (1, 2):
                _visible_png(
                    output_dir / "formulas" / f"formula_{index}.png", (8, 8)
                )
                _visible_png(
                    output_dir / "formulas" / f"formula_{index}_context.png",
                    (120, 60),
                )

            formulas = [
                _formula_node("这是正文标签"),
                _formula_node("12"),
            ]
            candidates = adapter._formula_indexed_candidates(
                output_dir,
                formulas,
                formula_crop_diagnostics=[
                    _formula_crop_diagnostic(
                        output_dir,
                        index,
                        formula=formula,
                    )
                    for index, formula in enumerate(formulas, start=1)
                ],
            )

            self.assertIsNone(candidates[0]["selected"])
            self.assertEqual(
                "context_diagnostic_only_formula_body_invalid",
                candidates[0]["selection_reason"],
            )
            self.assertIsNone(candidates[1]["selected"])
            self.assertEqual(
                "formula_number_only_has_no_formula_body",
                candidates[1]["selection_reason"],
            )

    def test_formula_crop_provenance_rejects_stale_pdf_asset_and_reordered_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            source_path = output_dir / "formulas" / "formula_1.png"
            _visible_png(source_path)
            formula = _formula_node("x_i=y_i+1")
            diagnostic = _formula_crop_diagnostic(
                output_dir,
                1,
                formula=formula,
                source_pdf_sha256="a" * 64,
            )

            wrong_pdf = adapter._formula_indexed_candidates(
                output_dir,
                [formula],
                formula_crop_diagnostics=[diagnostic],
                expected_visual_pdf_sha256="b" * 64,
            )
            self.assertIsNone(wrong_pdf[0]["selected"])
            self.assertFalse(wrong_pdf[0]["source_provenance_verified"])

            reordered = adapter._formula_indexed_candidates(
                output_dir,
                [_formula_node("x_i=y_i-1")],
                formula_crop_diagnostics=[diagnostic],
                expected_visual_pdf_sha256="a" * 64,
            )
            self.assertIsNone(reordered[0]["selected"])
            self.assertFalse(reordered[0]["source_provenance_verified"])

            _visible_png(source_path, size=(121, 61))
            stale_asset = adapter._formula_indexed_candidates(
                output_dir,
                [formula],
                formula_crop_diagnostics=[diagnostic],
                expected_visual_pdf_sha256="a" * 64,
            )
            self.assertIsNone(stale_asset[0]["selected"])
            self.assertFalse(stale_asset[0]["source_provenance_verified"])

    def test_final_polish_does_not_apply_paper_specific_cjk_rewrite(self) -> None:
        source_text = "获取历史时刻知识状态的权重力；未知中文正文保持原样。"
        html_text = f"<p>{source_text}</p>"

        patched_html, applied = adapter._patch_html_text_corrections(html_text)

        self.assertEqual(html_text, patched_html)
        self.assertEqual([], applied)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.md").write_text(source_text, encoding="utf-8")
            self.assertEqual(
                [], adapter._patch_markdown_formula_blocks(output_dir, {})
            )
            self.assertEqual(
                source_text,
                (output_dir / "document.md").read_text(encoding="utf-8"),
            )

    def test_structural_gate_rejects_raw_candidate_when_semantic_count_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "document.html").write_text(
                "<html><body></body></html>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text("", encoding="utf-8")
            metadata: dict[str, object] = {}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "primary_surface": {
                        "counts": {"tables": 0, "algorithms": 0, "code_blocks": 0}
                    },
                    "final_source_visuals": {
                        "table_source_expected_indexes": [],
                        "algorithm_source_expected_indexes": [],
                        "algorithm_source_candidate_count": 1,
                        "code_source_expected_indexes": [],
                        "code_source_candidate_count": 0,
                    },
                },
            }

            result = adapter.validate_final_structural_surfaces(
                output_dir, metadata, status
            )

            self.assertFalse(result["ok"])
            self.assertIn(
                "semantic_algorithm_inventory_mismatch",
                result["failure_reasons"],
            )

    def test_formula_identity_ignores_decimal_and_appendix_equation_labels(self) -> None:
        body = r"x_{n}=y_{n}"
        self.assertEqual(
            adapter._formula_content_identity(body),
            adapter._formula_content_identity(body + r"\quad ( 2 . 2 )"),
        )
        self.assertEqual(
            adapter._formula_content_identity(body),
            adapter._formula_content_identity(body + r"\quad ( A . 1 5 )"),
        )

    def test_formula_occurrence_uses_pdf_reconstructed_semantic_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            _visible_png(output_dir / "formulas" / "formula_1.png")
            raw_formula = _formula_node(
                r"\mathbf A=\mathbf X\mathbf D\mathbf Y^{\top},"
                r"\quad\mathbf(1)"
            )
            semantic_tex = r"\mathbf A=\mathbf X\mathbf D\mathbf Y^{\top},"
            (output_dir / "document.html").write_text(
                '<html><body><div class="formula">'
                '<details><summary>LaTeX</summary><code>'
                + semantic_tex
                + "</code></details></div>"
                "<!-- source-formula-anchor:1 --></body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$\n"
                + semantic_tex
                + "\n$$\n<!-- source-formula-anchor:1 -->\n",
                encoding="utf-8",
            )

            result = adapter.append_formula_source_renderings(
                output_dir,
                [raw_formula],
                formula_crop_diagnostics=[
                    _formula_crop_diagnostic(
                        output_dir,
                        1,
                        formula=raw_formula,
                    )
                ],
                expected_indexes={1},
                primary={
                    "semantic_formula_tex_by_index": {1: semantic_tex}
                },
            )

            self.assertEqual([1], result["html_covered_indexes"])
            self.assertEqual([1], result["markdown_covered_indexes"])
            self.assertEqual([], result["html_identity_mismatch_indexes"])
            self.assertEqual([], result["markdown_identity_mismatch_indexes"])

    def test_formula_identity_preserves_all_rows_from_docling_payload(self) -> None:
        raw = (
            r"a=b <formula><loc_1><loc_2>"
            r"\begin{array}{rl}a&=b\\c&=d\\e&=f\end{array}"
        )
        recovered = adapter._formula_source_text(raw)

        self.assertIn(r"c&=d", recovered)
        self.assertIn(r"e&=f", recovered)
        self.assertEqual(
            adapter._formula_content_identity(recovered),
            adapter._formula_content_identity(
                r"\begin{array}{rl}a&=b\\c&=d\\e&=f\end{array}"
            ),
        )

    def test_formula_source_evidence_rejects_swapped_formula_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "formulas").mkdir()
            for index in (1, 2):
                _visible_png(output_dir / "formulas" / f"formula_{index}.png")
            (output_dir / "document.html").write_text(
                "<html><body>"
                '<div class="formula" data-formula-index="1">'
                '<details><summary>LaTeX</summary><code>y=z</code></details></div>'
                "<!-- source-formula-anchor:1 -->"
                '<div class="formula" data-formula-index="2">'
                '<details><summary>LaTeX</summary><code>x=y</code></details></div>'
                "<!-- source-formula-anchor:2 -->"
                "</body></html>",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "$$y=z$$\n<!-- source-formula-anchor:1 -->\n"
                "$$x=y$$\n<!-- source-formula-anchor:2 -->\n",
                encoding="utf-8",
            )

            formulas = [_formula_node("x=y"), _formula_node("y=z")]
            result = adapter.append_formula_source_renderings(
                output_dir,
                formulas,
                formula_crop_diagnostics=[
                    _formula_crop_diagnostic(
                        output_dir,
                        index,
                        formula=formula,
                    )
                    for index, formula in enumerate(formulas, start=1)
                ],
            )

            self.assertEqual([], result["html_covered_indexes"])
            self.assertEqual([], result["markdown_covered_indexes"])
            self.assertEqual([1, 2], result["html_identity_mismatch_indexes"])
            self.assertEqual([1, 2], result["markdown_identity_mismatch_indexes"])

    def test_blank_inline_crop_is_missing_not_covered(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            Image.new("RGB", (200, 200), "white").save(
                output_dir / "pages" / "page_1.png"
            )
            marker = "<!-- source-inline-math-anchor:inline-1 -->"
            (output_dir / "document.html").write_text(
                f"<html><body><p>x</p>{marker}</body></html>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text(marker, encoding="utf-8")
            document = {"pages": {"1": {"size": {"width": 100, "height": 100}}}}
            region = {
                "anchor": "inline-1",
                "page_no": 1,
                "bbox": {
                    "l": 10,
                    "r": 20,
                    "t": 20,
                    "b": 10,
                    "coord_origin": "BOTTOMLEFT",
                },
                "unresolved": True,
            }

            result = adapter.append_inline_math_source_renderings(
                output_dir, document, [region]
            )

            self.assertEqual(["inline-1"], result["missing_crop_anchors"])
            self.assertEqual([], result["html_covered_anchors"])
            self.assertEqual([], result["markdown_covered_anchors"])

    def test_unresolved_inline_crop_is_visible_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_1.png", (200, 200))
            marker = "<!-- source-inline-math-anchor:inline-1 -->"
            (output_dir / "document.html").write_text(
                f"<html><body><p>x</p>{marker}</body></html>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text(marker, encoding="utf-8")
            document = {"pages": {"1": {"size": {"width": 100, "height": 100}}}}
            region = {
                "anchor": "inline-1",
                "page_no": 1,
                "bbox": {
                    "l": 5,
                    "r": 95,
                    "t": 95,
                    "b": 5,
                    "coord_origin": "BOTTOMLEFT",
                },
                "unresolved": True,
            }

            result = adapter.append_inline_math_source_renderings(
                output_dir, document, [region]
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")

            self.assertEqual(["inline-1"], result["unresolved_anchors"])
            self.assertIn("<details open", html_text)
            self.assertIn("Machine transcription incomplete", html_text)
            self.assertIn("<details open", markdown_text)

    def test_chunk_local_unresolved_inline_uses_global_page_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "pages").mkdir()
            _visible_png(output_dir / "pages" / "page_4.png", (200, 200))
            marker = "<!-- source-inline-math-anchor:inline-chunk -->"
            (output_dir / "document.html").write_text(
                f"<html><body><p>x</p>{marker}</body></html>", encoding="utf-8"
            )
            (output_dir / "document.md").write_text(marker, encoding="utf-8")
            document = {
                "schema_name": "local_ai_lab_docling_serve_chunked",
                "chunks": [
                    {
                        "page_range": [4, 4],
                        "document": {
                            "pages": {
                                "1": {"size": {"width": 100, "height": 100}}
                            }
                        },
                    }
                ],
            }
            region = {
                "anchor": "inline-chunk",
                "part_index": 0,
                "page_no": 1,
                "bbox": {
                    "l": 5,
                    "r": 95,
                    "t": 95,
                    "b": 5,
                    "coord_origin": "BOTTOMLEFT",
                },
                "unresolved": True,
            }

            result = adapter.append_inline_math_source_renderings(
                output_dir, document, [region]
            )

            self.assertEqual(1, result["candidate_count"])
            self.assertEqual(4, result["candidates"][0]["page_no"])
            self.assertEqual(1, result["candidates"][0]["source_page_no"])
            self.assertEqual(["inline-chunk"], result["html_covered_anchors"])
            self.assertEqual(["inline-chunk"], result["markdown_covered_anchors"])
            self.assertEqual(["inline-chunk"], result["unresolved_anchors"])
            self.assertTrue(output_dir.joinpath(result["source_images"][0]).is_file())

    def test_apply_all_gate_failure_restores_primary_contract(self) -> None:
        self._assert_primary_apply_rolls_back(raise_from_html_patch=False)

    def test_apply_all_exception_restores_primary_contract(self) -> None:
        self._assert_primary_apply_rolls_back(raise_from_html_patch=True)

    def test_failed_second_pass_summary_preserves_bounded_identity_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "job"
            route_b_dir = Path(tmpdir) / "route-b"
            output_dir.mkdir()
            route_b_dir.mkdir()
            metadata: dict[str, object] = {"generated_outputs": []}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            args = Namespace(
                formula_second_pass_policy="apply-all",
                formula_second_pass_route_b_dir=route_b_dir,
                formula_second_pass_output_dir=output_dir / "formula_second_pass",
                formula_second_pass_review_candidate_dir=[],
                formula_second_pass_guarded_fallback_dir=[],
                formula_second_pass_guarded_fallback_eq=[],
                formula_policy="granite_mlx",
                enable_formula_mlx=False,
            )
            replacement_log = [
                {"formula_no": index, "status": "no_match"}
                for index in range(1, 26)
            ]
            raw_result = {
                "ok": False,
                "error": "formula_second_pass_source_evidence_incomplete",
                "route_a_formula_count": 25,
                "route_b_formula_count": 25,
                "route_job_identity_check": {
                    "ok": False,
                    "route_a_job_id": "job-a",
                    "route_b_job_id": "job-b",
                },
                "route_b_source_identity_check": {
                    "ok": False,
                    "reason": "visual_pdf_sha256_mismatch",
                },
                "evidence_gaps": [
                    {"formula_no": 1, "missing_evidence": ["route_b_bbox"]}
                ],
                "replacement_log": replacement_log,
            }

            with patch.object(
                adapter,
                "run_formula_second_pass",
                return_value=raw_result,
            ):
                adapter.run_optional_formula_second_pass(
                    output_dir,
                    metadata,
                    status,
                    args,
                )

            summary = metadata["formula_second_pass"]
            self.assertEqual(
                raw_result["route_job_identity_check"],
                summary["route_job_identity_check"],
            )
            self.assertEqual(
                raw_result["route_b_source_identity_check"],
                summary["route_b_source_identity_check"],
            )
            self.assertEqual(raw_result["evidence_gaps"], summary["evidence_gaps"])
            self.assertEqual(25, summary["replacement_log_count"])
            self.assertEqual(20, len(summary["replacement_log"]))
            self.assertTrue(summary["replacement_log_truncated"])
            self.assertEqual(
                summary,
                status["quality_signals"]["formula_second_pass"],
            )
            self.assertFalse(status["ok"])

    def test_outer_second_pass_exception_converges_and_restores_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "job"
            output_dir.mkdir()
            originals = {
                "document.html": b"<html><body>original</body></html>",
                "document.md": b"original markdown",
                "document.json": b'{"texts": []}',
            }
            for name, payload in originals.items():
                (output_dir / name).write_bytes(payload)
            metadata: dict[str, object] = {"generated_outputs": []}
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            args = Namespace()

            def explode(*_args: object, **_kwargs: object) -> None:
                (output_dir / "document.md").write_text("partial", encoding="utf-8")
                raise OSError("disk full")

            with patch.object(adapter, "run_optional_formula_second_pass", side_effect=explode):
                result = adapter.run_optional_formula_second_pass_safely(
                    output_dir, metadata, status, args
                )

            self.assertFalse(result["ok"])
            self.assertTrue(result["primary_restored"])
            for name, payload in originals.items():
                self.assertEqual(payload, (output_dir / name).read_bytes())
            self.assertFalse(status["ok"])
            self.assertTrue((output_dir / "status.json").is_file())

    def test_job_output_dir_rejects_traversal_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            outside = Path(tmpdir) / "outside"
            root.mkdir()
            outside.mkdir()
            with self.assertRaisesRegex(ValueError, "one_path_component"):
                adapter._job_output_dir(root, "../../outside")
            with self.assertRaisesRegex(ValueError, "one_path_component"):
                adapter._job_output_dir(root, str(outside))
            link = root / "linked-job"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink_not_allowed"):
                adapter._job_output_dir(root, "linked-job")
            root_link = Path(tmpdir) / "root-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "output_root_symlink_not_allowed"):
                adapter._safe_output_child(
                    root_link,
                    root_link / "job",
                    label="job_output_dir",
                )

    def _assert_primary_apply_rolls_back(self, *, raise_from_html_patch: bool) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "job"
            route_b_dir = Path(tmpdir) / "route-b"
            sidecar_dir = output_dir / "formula_second_pass"
            output_dir.mkdir()
            route_b_dir.mkdir()
            sidecar_dir.mkdir()
            originals = {
                "document.html": b"<html><body>original</body></html>",
                "document.md": b"original markdown",
                "document.json": b'{"texts": []}',
                "formulas.tex": b"% original formula source\n",
            }
            for name, payload in originals.items():
                (output_dir / name).write_bytes(payload)
            (sidecar_dir / "document.md").write_text("patched markdown", encoding="utf-8")
            (sidecar_dir / "document.json").write_text(
                json.dumps({"texts": [{"label": "formula", "text": "x=y"}]}),
                encoding="utf-8",
            )
            for name in ("second_pass_summary.json", "review_index.html"):
                (sidecar_dir / name).write_text("{}", encoding="utf-8")
            metadata: dict[str, object] = {
                "generated_outputs": [],
                "formula_latex_sources": {"written": True, "formula_count": 0},
            }
            status: dict[str, object] = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {
                    "formula_latex_sources": {"written": True, "formula_count": 0}
                },
            }
            args = Namespace(
                formula_second_pass_policy="apply-all",
                formula_second_pass_route_b_dir=route_b_dir,
                formula_second_pass_output_dir=sidecar_dir,
                formula_second_pass_review_candidate_dir=[],
                formula_second_pass_guarded_fallback_dir=[],
                formula_second_pass_guarded_fallback_eq=[],
                formula_policy="granite_mlx",
                enable_formula_mlx=False,
                input_file=Path(tmpdir) / "paper.pdf",
                cn_ocr_parity=False,
                legacy_cn_accepted_baseline=False,
            )
            second_pass_result = {
                "ok": True,
                "route_a_formula_count": 1,
                "route_b_formula_count": 1,
                "suspicious_formula_count": 1,
                "second_pass_attempted_count": 1,
                "replaced_count": 1,
                "no_match_count": 0,
                "fallback_count": 0,
                "replacement_log": [{"formula_no": 1, "status": "replaced"}],
            }
            html_patch = (
                RuntimeError("injected html failure")
                if raise_from_html_patch
                else {"ok": False, "missing_indexes": [1]}
            )
            with (
                patch.object(adapter, "run_formula_second_pass", return_value=second_pass_result),
                patch.object(adapter, "write_formula_latex_sources", return_value={}),
                patch.object(
                    adapter,
                    "patch_document_html_for_formula_second_pass",
                    side_effect=html_patch if isinstance(html_patch, Exception) else None,
                    return_value=html_patch if isinstance(html_patch, dict) else None,
                ),
                patch.object(
                    adapter,
                    "synchronize_formula_contract_outputs",
                    return_value={"ok": True},
                ),
                patch.object(
                    adapter,
                    "apply_cn_final_document_polish",
                    return_value={"ok": True, "applied": False},
                ),
                patch.object(
                    adapter,
                    "validate_formula_second_pass_html",
                    return_value={"ok": False, "missing_replacements": [1]},
                ),
                patch.object(
                    adapter,
                    "apply_markdown_main_flow_supplement",
                    return_value={"ok": True},
                ),
            ):
                adapter.run_optional_formula_second_pass(output_dir, metadata, status, args)

            for name, payload in originals.items():
                self.assertEqual(payload, (output_dir / name).read_bytes())
            self.assertFalse(metadata["formula_second_pass_applied"])
            self.assertFalse(status["quality_signals"]["formula_second_pass_applied"])
            self.assertEqual(
                {"written": True, "formula_count": 0},
                metadata["formula_latex_sources"],
            )
            self.assertEqual(
                {"written": True, "formula_count": 0},
                status["quality_signals"]["formula_latex_sources"],
            )
            self.assertFalse(status["ok"])


if __name__ == "__main__":
    unittest.main()
