from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import formula_only_second_pass as fsp  # noqa: E402


def _write_evidence_png(
    path: Path,
    *,
    size: tuple[int, int] = (64, 32),
) -> None:
    from PIL import Image, ImageDraw

    width, height = size
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    inset_x = max(1, width // 8)
    inset_y = max(1, height // 4)
    draw.rectangle(
        (inset_x, inset_y, width - inset_x - 1, height - inset_y - 1),
        fill="black",
    )
    draw.line(
        (
            min(width - 2, inset_x + 2),
            height // 2,
            max(1, width - inset_x - 3),
            height // 2,
        ),
        fill="white",
        width=max(1, min(2, height // 8)),
    )
    image.save(path, format="PNG")


def _pdf_fixture_bytes(
    content: bytes,
    preamble: bytes = b"",
    *,
    page_count: int = 1,
) -> bytes:
    page_count = max(1, int(page_count))
    marker = hashlib.sha256(content).hexdigest().encode("ascii")
    payload = bytearray(preamble + b"%PDF-1.4\n% fixture-" + marker + b"\n")
    page_object_ids = list(range(3, 3 + page_count))
    content_object_id = 3 + page_count
    kids = " ".join(f"{object_id} 0 R" for object_id in page_object_ids)
    grid_commands = ["0 G", "0.8 w"]
    grid_commands.extend(
        f"{x} 5 m {x} 115 l S" for x in range(5, 100, 10)
    )
    grid_commands.extend(
        f"5 {y} m 95 {y} l S" for y in range(5, 120, 10)
    )
    stream = ("\n".join(grid_commands) + "\n").encode("ascii")
    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        (
            f"2 0 obj\n<< /Type /Pages /Kids [{kids}] "
            f"/Count {page_count} >>\nendobj\n"
        ).encode("ascii"),
    ]
    objects.extend(
        (
            f"{object_id} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 100 120] /Resources << >> "
            f"/Contents {content_object_id} 0 R >>\nendobj\n"
        ).encode("ascii")
        for object_id in page_object_ids
    )
    objects.append(
        (
            f"{content_object_id} 0 obj\n<< /Length {len(stream)} >>\nstream\n"
        ).encode("ascii")
        + stream
        + b"endstream\nendobj\n"
    )
    offsets: list[int] = []
    for pdf_object in objects:
        offsets.append(len(payload))
        payload.extend(pdf_object)
    xref_offset = len(payload)
    object_count = len(objects) + 1
    payload.extend(
        f"xref\n0 {object_count}\n0000000000 65535 f \n".encode("ascii")
    )
    for offset in offsets:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {object_count} /Root 1 0 R >>\nstartxref\n".encode(
            "ascii"
        )
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(payload)


def _write_pdf(
    path: Path,
    content: bytes,
    *,
    preamble: bytes = b"",
    page_count: int = 1,
) -> str:
    content = _pdf_fixture_bytes(content, preamble, page_count=page_count)
    path.write_bytes(content)
    hasher = hashlib.sha256()
    hasher.update(content)
    return hasher.hexdigest()


def _make_temp_route(
    directory: Path,
    formula_text: str,
    page_no: int = 1,
    formula_node: bool = True,
    include_metadata_sha: str | None = None,
    pdf_content: bytes | None = None,
    pdf_relpath: Path | None = None,
    input_file: str | Path | None = None,
    include_evidence: bool = True,
) -> str:
    (directory / "document.md").write_text(f"$$\n{formula_text}\n$$\n", encoding="utf-8")

    metadata: dict[str, str] = {}
    if input_file is not None:
        metadata["input_file"] = str(input_file)
    elif pdf_content is None:
        metadata["input_file"] = "/tmp/never-exists.pdf"
    if include_metadata_sha is not None:
        metadata["input_sha256"] = include_metadata_sha
    elif "input_file" not in metadata:
        metadata["input_file"] = str(directory / "source.pdf")
        if pdf_relpath is not None:
            metadata["input_file"] = str(pdf_relpath)

    if pdf_content is None and pdf_relpath is None:
        pdf_path = directory / "source.pdf"
    else:
        pdf_path = pdf_relpath if pdf_relpath is not None else directory / "source.pdf"
    if pdf_content is not None:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        computed_sha = _write_pdf(
            pdf_path,
            pdf_content,
            page_count=max(1, page_no),
        )
        if include_metadata_sha is None:
            metadata["input_sha256"] = computed_sha

    if formula_node:
        document = {
            "pages": {
                str(page_no): {
                    "size": {"width": 100.0, "height": 120.0},
                }
            },
            "children": [
                {
                    "label": "formula",
                    "text": formula_text,
                    "prov": [
                        {
                            "page_no": page_no,
                            "bbox": {
                                "l": 10,
                                "r": 20,
                                "t": 100,
                                "b": 80,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ]
        }
    else:
        document = {"children": []}
    (directory / "document.json").write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (directory / "status.json").write_text(
        json.dumps({"ok": True, "success_class": "success"}),
        encoding="utf-8",
    )
    if formula_node and include_evidence:
        (directory / "formulas").mkdir(exist_ok=True)
        (directory / "pages").mkdir(exist_ok=True)
        _write_evidence_png(directory / "formulas" / "formula_1.png")
        _write_evidence_png(directory / "formulas" / "formula_1_context.png")
        _write_evidence_png(
            directory / "pages" / f"page_{page_no}.png",
            size=(128, 128),
        )
    return str(directory)


def _formula_node(
    text: str,
    page_no: int = 1,
    l: float = 10.0,
    r: float = 20.0,
    t: float = 20.0,
    b: float = 40.0,
    eq: int | None = None,
    page_order: int = 0,
) -> dict[str, object]:
    width = 100.0
    height = 120.0
    return {
        "text": text,
        "page_no": page_no,
        "bbox_norm": {
            "l": l,
            "r": r,
            "t": t,
            "b": b,
        },
        "bbox_rel": {
            "l": l / width,
            "r": r / width,
            "t": t / height,
            "b": b / height,
        },
        "geometry_verified": True,
        "main_eq": eq,
        "page_order": page_order,
        "nearby_text": [],
        "nearby_before": [],
        "nearby_after": [],
        "reading_order": 0,
        "formula_no": 1,
    }


def _chunked_formula_document(formulas_by_page: dict[int, str]) -> dict[str, object]:
    chunks: list[dict[str, object]] = []
    for global_page, formula_text in formulas_by_page.items():
        chunks.append(
            {
                "page_range": [global_page, global_page],
                "document": {
                    "pages": {
                        "1": {"size": {"width": 100.0, "height": 120.0}}
                    },
                    "children": [
                        {
                            "label": "formula",
                            "text": formula_text,
                            "prov": [
                                {
                                    "page_no": 1,
                                    "bbox": {
                                        "l": 10,
                                        "r": 20,
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
        )
    return {
        "schema_name": "local_ai_lab_docling_serve_chunked",
        "chunks": chunks,
    }


class MatchRouteBPolicyTests(unittest.TestCase):
    def test_no_eq_same_bbox_sim_zero_rejected(self) -> None:
        route_a_formulas = [_formula_node("x", eq=None)]
        route_b_formulas = [_formula_node("y", eq=None)]

        matches = fsp.match_route_b_to_route_a(route_a_formulas, route_b_formulas)

        self.assertEqual(matches, {})
        best_score = fsp._anchor_match_score(route_a_formulas[0], route_b_formulas[0])[0]
        best_evidence = fsp._anchor_match_score(route_a_formulas[0], route_b_formulas[0])[1]
        reasons = fsp.route_b_match_rejection_reasons(
            score=best_score,
            formula_similarity=best_evidence["formula_similarity"],
            exact_eq_match=False,
            score_margin=best_evidence["score_margin"] if "score_margin" in best_evidence else None,
            minimum_score=max(35.0, 0.5 * 60),
            sim_threshold=0.5,
        )
        self.assertIn("formula_similarity_too_low", reasons)

    def test_no_eq_high_similarity_accepted(self) -> None:
        route_a_formulas = [_formula_node("x + y")]
        route_b_formulas = [_formula_node("x + y + z")]

        matches = fsp.match_route_b_to_route_a(route_a_formulas, route_b_formulas)

        self.assertIn(0, matches)

    def test_no_eq_match_requires_geometry_anchor(self) -> None:
        route_a = _formula_node("x+y")
        route_b = _formula_node("x+y+z")
        route_a["bbox_norm"] = None

        matches = fsp.match_route_b_to_route_a([route_a], [route_b])

        self.assertEqual({}, matches)

    def test_exact_eq_without_body_similarity_is_rejected(self) -> None:
        route_a_formulas = [_formula_node("garbage text (5)", eq=5)]
        route_b_formulas = [_formula_node("different body", eq=5)]

        matches = fsp.match_route_b_to_route_a(route_a_formulas, route_b_formulas)

        self.assertEqual({}, matches)

    def test_spaced_equation_tag_does_not_inflate_body_similarity(self) -> None:
        route_a_formulas = [_formula_node("garbage ( 1 6 )", eq=16)]
        route_b_formulas = [_formula_node("different ( 1 6 )", eq=16)]

        matches = fsp.match_route_b_to_route_a(route_a_formulas, route_b_formulas)

        self.assertEqual({}, matches)

    def test_exact_eq_with_body_similarity_is_accepted(self) -> None:
        route_a_formulas = [_formula_node("x+y+z (5)", eq=5)]
        route_b_formulas = [_formula_node("x+y+z+w (5)", eq=5)]

        matches = fsp.match_route_b_to_route_a(route_a_formulas, route_b_formulas)

        self.assertIn(0, matches)

    def test_exact_eq_same_tag_wrongish_body_is_rejected_by_conservative_threshold(self) -> None:
        route_a_formulas = [_formula_node("x + y + z + w (5)", eq=5)]
        route_b_formulas = [_formula_node("integral of unrelated function (5)", eq=5)]

        matches = fsp.match_route_b_to_route_a(route_a_formulas, route_b_formulas)

        self.assertEqual({}, matches)

    def test_trailing_equation_label_strip_preserves_body_parenthesized_numbers(self) -> None:
        route_a = _formula_node("x + (1) (5)", eq=5)
        route_b = _formula_node("x + (2) (5)", eq=5)

        self.assertEqual({}, fsp.match_route_b_to_route_a([route_a], [route_b]))
        self.assertEqual(
            0.0,
            fsp.formula_body_similarity("x + (1) (5)", "x + (2) (5)"),
        )
        self.assertNotEqual(
            fsp._markdown_identity_body("x + (1) (5)"),
            fsp._markdown_identity_body("x + (2) (5)"),
        )
        self.assertEqual(
            "x + (1)",
            fsp._strip_trailing_display_equation_label("x + (1) (5)"),
        )

    def test_duplicate_same_page_exact_equation_candidates_are_ambiguous(self) -> None:
        route_a = _formula_node("x+y+z (5)", eq=5)
        route_b = [
            _formula_node("x+y+z+w (5)", eq=5, page_order=0),
            _formula_node("x+y+z+w (5)", eq=5, page_order=1),
        ]

        self.assertEqual({}, fsp.match_route_b_to_route_a([route_a], route_b))

    def test_duplicate_exact_equation_with_clear_geometry_margin_is_accepted(self) -> None:
        route_a = _formula_node("x+y+z (5)", eq=5)
        best = _formula_node("x+y+z+w (5)", eq=5, page_order=0)
        weaker = _formula_node(
            "x+y+z+w (5)",
            eq=5,
            page_order=1,
            t=27.0,
            b=47.0,
        )

        matches = fsp.match_route_b_to_route_a([route_a], [best, weaker])

        self.assertIn(0, matches)
        self.assertGreaterEqual(
            matches[0]["anchor_match"]["exact_equation_score_margin"],
            fsp.EXACT_EQUATION_DUPLICATE_MIN_SCORE_MARGIN,
        )

    def test_exact_eq_requires_verified_geometry(self) -> None:
        route_a = _formula_node("x+y (5)", eq=5)
        route_b = _formula_node("x+y+z (5)", eq=5)
        route_b["geometry_verified"] = False

        self.assertEqual({}, fsp.match_route_b_to_route_a([route_a], [route_b]))

    def test_relative_y_offset_near_point_one_is_rejected(self) -> None:
        route_a = _formula_node("x+y+z (5)", eq=5)
        route_b = _formula_node("x+y+z+w (5)", eq=5)
        route_b["bbox_norm"] = {"l": 10.0, "r": 20.0, "t": 32.0, "b": 52.0}
        route_b["bbox_rel"] = {
            "l": 0.1,
            "r": 0.2,
            "t": (20.0 / 120.0) + 0.10,
            "b": (40.0 / 120.0) + 0.10,
        }

        self.assertEqual({}, fsp.match_route_b_to_route_a([route_a], [route_b]))
        _, evidence = fsp._anchor_match_score(route_a, route_b)
        self.assertFalse(evidence["geometry_y_within_match_limit"])

    def test_equivalent_relative_geometry_across_page_sizes_is_accepted(self) -> None:
        route_a_doc = {
            "pages": {"1": {"size": {"width": 100.0, "height": 120.0}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x+y+z (5)",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10.0,
                                "r": 20.0,
                                "t": 100.0,
                                "b": 80.0,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }
        route_b_doc = {
            "pages": {"1": {"size": {"width": 200.0, "height": 360.0}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x+y+z+w (5)",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 20.0,
                                "r": 40.0,
                                "t": 60.0,
                                "b": 120.0,
                                "coord_origin": "TOPLEFT",
                            },
                        }
                    ],
                }
            ],
        }
        route_b_sizes = fsp._document_page_sizes(route_b_doc)
        route_a = fsp.extract_formulas(
            route_a_doc,
            target_page_sizes=route_b_sizes,
        )
        route_b = fsp.extract_formulas(
            route_b_doc,
            target_page_sizes=route_b_sizes,
        )

        self.assertEqual(route_a[0]["bbox_rel"], route_b[0]["bbox_rel"])
        self.assertIn(0, fsp.match_route_b_to_route_a(route_a, route_b))


class FormulaNormalizationConservatismTests(unittest.TestCase):
    def test_normalize_formula_candidate_keeps_spaced_softmax_like_variable(self) -> None:
        candidate = "x = s o f t m a x"
        self.assertEqual(
            fsp.normalize_formula_candidate(candidate),
            candidate,
        )

    def test_normalize_formula_candidate_keeps_zero_times_W_pattern(self) -> None:
        candidate = "f = 0 ( A^2 ) \\times W"
        self.assertEqual(
            fsp.normalize_formula_candidate(candidate),
            "f = 0 ( A^2 ) \\times W",
        )

    def test_extract_formulas_uses_declared_non_a4_page_height(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 612, "height": 792}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 20,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(document)[0]

        self.assertEqual(
            formula["bbox_norm"],
            {"l": 20.0, "r": 40.0, "t": 184.0, "b": 224.0},
        )

    def test_extract_formulas_scales_to_declared_route_b_page_size(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 612, "height": 792}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 20,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(
            document,
            target_page_sizes={1: (918.0, 1188.0)},
        )[0]

        self.assertEqual(
            formula["bbox_norm"],
            {"l": 15.0, "r": 30.0, "t": 138.0, "b": 168.0},
        )

    def test_extract_formulas_drops_cross_route_geometry_without_target_page(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 612, "height": 792}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 20,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(document, target_page_sizes={})[0]

        self.assertIsNone(formula["bbox_norm"])

    def test_extract_formulas_does_not_guess_missing_coordinate_origin(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 612, "height": 792}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 10, "r": 20, "t": 700, "b": 680},
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(document)[0]

        self.assertIsNone(formula["bbox_norm"])

    def test_extract_formulas_malformed_page_number_fails_closed_without_throwing(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 612, "height": 792}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": "not-a-page",
                            "bbox": {
                                "l": 10,
                                "r": 20,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(document)[0]

        self.assertIsNone(formula["page_no"])
        self.assertIsNone(formula["bbox_norm"])
        self.assertFalse(formula["geometry_verified"])
        self.assertEqual("page_no_invalid", formula["geometry_reason"])

    def test_extract_formulas_missing_page_size_fails_closed(self) -> None:
        document = {
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "r": 20,
                                "t": 700,
                                "b": 680,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(document)[0]

        self.assertIsNone(formula["bbox_norm"])
        self.assertFalse(formula["geometry_verified"])
        self.assertEqual("source_page_size_missing", formula["geometry_reason"])

    def test_extract_formulas_rejects_out_of_bounds_bbox(self) -> None:
        document = {
            "pages": {"1": {"size": {"width": 100, "height": 120}}},
            "texts": [
                {
                    "label": "formula",
                    "text": "x",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 95,
                                "r": 101,
                                "t": 100,
                                "b": 90,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }

        formula = fsp.extract_formulas(document)[0]

        self.assertIsNone(formula["bbox_norm"])
        self.assertFalse(formula["geometry_verified"])
        self.assertEqual("bbox_out_of_source_page_bounds", formula["geometry_reason"])

    def test_extract_formulas_rejects_incomplete_or_degenerate_bbox(self) -> None:
        for bbox in (
            {"l": 10, "r": 20, "t": 700, "coord_origin": "BOTTOMLEFT"},
            {"l": 10, "r": 10, "t": 700, "b": 680, "coord_origin": "BOTTOMLEFT"},
        ):
            with self.subTest(bbox=bbox):
                document = {
                    "pages": {"1": {"size": {"width": 612, "height": 792}}},
                    "texts": [
                        {
                            "label": "formula",
                            "text": "x",
                            "prov": [{"page_no": 1, "bbox": bbox}],
                        }
                    ],
                }
                self.assertIsNone(fsp.extract_formulas(document)[0]["bbox_norm"])

    def test_extract_formulas_rejects_vertical_order_for_declared_origin(self) -> None:
        invalid_by_origin = {
            "BOTTOMLEFT": {"l": 10, "r": 20, "t": 80, "b": 100},
            "TOPLEFT": {"l": 10, "r": 20, "t": 100, "b": 80},
        }
        for origin, bbox in invalid_by_origin.items():
            with self.subTest(origin=origin):
                document = {
                    "pages": {"1": {"size": {"width": 100, "height": 120}}},
                    "texts": [
                        {
                            "label": "formula",
                            "text": "x",
                            "prov": [
                                {
                                    "page_no": 1,
                                    "bbox": {**bbox, "coord_origin": origin},
                                }
                            ],
                        }
                    ],
                }

                formula = fsp.extract_formulas(document)[0]

                self.assertIsNone(formula["bbox_norm"])
                self.assertFalse(formula["geometry_verified"])
                self.assertEqual(
                    "bbox_vertical_order_invalid",
                    formula["geometry_reason"],
                )


class FormulaSecondPassFailClosedTests(unittest.TestCase):
    def test_chunk_local_pages_are_normalized_and_part_indexes_preserved(self) -> None:
        raw_document = _chunked_formula_document(
            {4: "c+d \\quad (4)", 3: "a+b \\quad (3)"}
        )

        normalized = fsp._normalize_document_chunk_pages(raw_document)
        page_sizes = fsp._document_page_sizes(normalized)
        formulas = fsp.extract_formulas(
            normalized,
            target_page_sizes=page_sizes,
        )

        self.assertEqual([3, 4], [formula["page_no"] for formula in formulas])
        self.assertEqual([0, 1], [formula["part_index"] for formula in formulas])
        self.assertTrue(all(formula["geometry_verified"] for formula in formulas))
        self.assertEqual({3, 4}, set(page_sizes))
        self.assertEqual(
            [3, 4],
            [
                chunk["document"]["children"][0]["prov"][0]["page_no"]
                for chunk in normalized["chunks"]
            ],
        )
        self.assertEqual(
            [1, 1],
            [
                chunk["document"]["children"][0]["prov"][0]["page_no"]
                for chunk in raw_document["chunks"]
            ],
        )

    def test_chunk_already_global_page_is_not_double_offset(self) -> None:
        raw_document = {
            "schema_name": "local_ai_lab_docling_serve_chunked",
            "chunks": [
                {
                    "page_range": [9, 16],
                    "document": {
                        "pages": {
                            "9": {
                                "size": {"width": 100.0, "height": 120.0}
                            }
                        },
                        "children": [
                            {
                                "label": "formula",
                                "text": "a+b \\quad (9)",
                                "prov": [
                                    {
                                        "page_no": 9,
                                        "bbox": {
                                            "l": 10,
                                            "r": 20,
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

        normalized = fsp._normalize_document_chunk_pages(raw_document)
        page_sizes = fsp._document_page_sizes(normalized)
        formulas = fsp.extract_formulas(normalized, target_page_sizes=page_sizes)

        self.assertEqual({9: (100.0, 120.0)}, page_sizes)
        self.assertEqual(9, formulas[0]["page_no"])
        self.assertEqual(0, formulas[0]["part_index"])
        self.assertTrue(formulas[0]["geometry_verified"])
        self.assertEqual(
            9,
            normalized["chunks"][0]["document"]["children"][0]["prov"][0][
                "page_no"
            ],
        )
        self.assertEqual(
            9,
            raw_document["chunks"][0]["document"]["children"][0]["prov"][0][
                "page_no"
            ],
        )

    def test_apply_all_matches_two_local_page_one_chunks_on_global_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(
                route_a, "placeholder", page_no=4, pdf_content=b"same"
            )
            _make_temp_route(
                route_b, "placeholder+z", page_no=4, pdf_content=b"same"
            )
            route_documents = (
                (
                    route_a,
                    _chunked_formula_document(
                        {4: "c+d \\quad (4)", 3: "a+b \\quad (3)"}
                    ),
                    "$$a+b \\quad (3)$$\n\n$$c+d \\quad (4)$$\n",
                ),
                (
                    route_b,
                    _chunked_formula_document(
                        {4: "c+d+w \\quad (4)", 3: "a+b+z \\quad (3)"}
                    ),
                    "$$a+b+z \\quad (3)$$\n\n$$c+d+w \\quad (4)$$\n",
                ),
            )
            for route, document, markdown in route_documents:
                (route / "document.json").write_text(
                    json.dumps(document),
                    encoding="utf-8",
                )
                (route / "document.md").write_text(markdown, encoding="utf-8")
                _write_evidence_png(route / "formulas" / "formula_2.png")
                _write_evidence_png(
                    route / "pages" / "page_3.png", size=(128, 128)
                )
                _write_evidence_png(
                    route / "pages" / "page_4.png", size=(128, 128)
                )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(
                [3, 4],
                [entry["page_no"] for entry in result["replacement_log"]],
            )
            self.assertEqual(
                [0, 1],
                [entry["anchor_part_index"] for entry in result["replacement_log"]],
            )
            self.assertEqual(
                [0, 1],
                [entry["route_b_part_index"] for entry in result["replacement_log"]],
            )
            for expected_page, expected_part, entry in zip(
                (3, 4),
                (0, 1),
                result["replacement_log"],
            ):
                for evidence_key in ("route_a_evidence", "route_b_evidence"):
                    binding = entry[evidence_key]["audit_binding"]
                    self.assertEqual(expected_page, binding["formula_page_no"])
                    self.assertEqual(expected_part, binding["formula_part_index"])
                    self.assertIsInstance(binding["formula_bbox"], dict)
            self.assertIn("a+b+z", result["replacement_log"][0]["route_b_candidate"])
            self.assertIn("c+d+w", result["replacement_log"][1]["route_b_candidate"])
            patched = json.loads(
                (output / "document.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [3, 4],
                [
                    chunk["document"]["children"][0]["prov"][0]["page_no"]
                    for chunk in patched["chunks"]
                ],
            )

    def test_looping_metadata_source_path_is_ignored_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            route = Path(tmpdir) / "route"
            route.mkdir()
            loop = route / "loop.pdf"
            loop.symlink_to(loop)

            candidates = fsp._iter_pdf_candidate_paths(
                route, {"input_file": str(loop)}
            )

            self.assertNotIn(loop, candidates)

    def test_apply_all_rejects_symlinked_route_contract_files(self) -> None:
        for route_label in ("route_a", "route_b"):
            for contract_name in (
                "document.json",
                "status.json",
                "metadata.json",
                "document.md",
                "source.pdf",
            ):
                with self.subTest(route=route_label, contract=contract_name):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        base = Path(tmpdir)
                        route_a, route_b, output = (
                            base / "a",
                            base / "b",
                            base / "out",
                        )
                        for path in (route_a, route_b, output):
                            path.mkdir()
                        _make_temp_route(route_a, "x+y (1)", pdf_content=b"same")
                        _make_temp_route(route_b, "x+y (1)", pdf_content=b"same")
                        route = route_a if route_label == "route_a" else route_b
                        contract_path = route / contract_name
                        external = base / f"external-{route_label}-{contract_name}"
                        external.write_bytes(contract_path.read_bytes())
                        contract_path.unlink()
                        contract_path.symlink_to(external)

                        result = fsp.run_formula_second_pass(
                            route_a,
                            route_b,
                            output,
                            apply_all=True,
                        )

                        self.assertFalse(result["ok"])
                        self.assertEqual(
                            "route_contract_path_security_violation",
                            result["error"],
                        )
                        self.assertEqual(route_label, result["route"])
                        self.assertEqual("symlink_not_allowed", result["reason"])
                        self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_symlinked_external_metadata_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y (1)", pdf_content=b"same")
            real_pdf = base / "real.pdf"
            source_sha = _write_pdf(real_pdf, b"same")
            linked_pdf = base / "linked.pdf"
            linked_pdf.symlink_to(real_pdf)
            metadata = json.loads(
                (route_a / "metadata.json").read_text(encoding="utf-8")
            )
            metadata["input_file"] = str(linked_pdf)
            metadata["input_sha256"] = source_sha
            (route_a / "metadata.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                "route_contract_path_security_violation",
                result["error"],
            )
            self.assertEqual("symlink_not_allowed", result["reason"])

    def test_apply_all_rejects_pdf_candidate_escaping_through_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            external_dir = base / "external"
            for path in (route_a, route_b, output, external_dir):
                path.mkdir()
            _make_temp_route(route_a, "x+y (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y (1)", pdf_content=b"same")
            (route_a / "source.pdf").unlink()
            source_sha = _write_pdf(external_dir / "source.pdf", b"same")
            (route_a / "nested").symlink_to(external_dir, target_is_directory=True)
            metadata = json.loads(
                (route_a / "metadata.json").read_text(encoding="utf-8")
            )
            metadata["input_file"] = "nested/source.pdf"
            metadata["input_sha256"] = source_sha
            (route_a / "metadata.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                "route_contract_path_security_violation",
                result["error"],
            )
            self.assertEqual("symlink_not_allowed", result["reason"])

    def test_explicit_equation_number_mismatch_is_not_matched(self) -> None:
        route_a = [_formula_node("x+y", eq=5)]
        route_b = [_formula_node("x+y", eq=6)]

        self.assertEqual({}, fsp.match_route_b_to_route_a(route_a, route_b))

    def test_apply_all_rejects_nonempty_route_a_with_empty_route_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "", formula_node=False, pdf_content=b"same")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
        self.assertEqual("route_b_formula_inventory_empty_route_a_nonempty", result["error"])

    def test_apply_all_rejects_missing_or_malformed_route_b_page_geometry(self) -> None:
        malformed_page_maps = (
            None,
            {},
            {"1": {"size": {"width": "not-a-number", "height": 120.0}}},
        )
        for pages in malformed_page_maps:
            with self.subTest(pages=pages):
                with tempfile.TemporaryDirectory() as tmpdir:
                    base = Path(tmpdir)
                    route_a, route_b, output = (
                        base / "a",
                        base / "b",
                        base / "out",
                    )
                    for path in (route_a, route_b, output):
                        path.mkdir()
                    _make_temp_route(route_a, "x+y+z (5)", pdf_content=b"same")
                    _make_temp_route(route_b, "x+y+z+w (5)", pdf_content=b"same")
                    route_b_doc = json.loads(
                        (route_b / "document.json").read_text(encoding="utf-8")
                    )
                    if pages is None:
                        route_b_doc.pop("pages", None)
                    else:
                        route_b_doc["pages"] = pages
                    (route_b / "document.json").write_text(
                        json.dumps(route_b_doc),
                        encoding="utf-8",
                    )

                    result = fsp.run_formula_second_pass(
                        route_a,
                        route_b,
                        output,
                        apply_all=True,
                    )

                    self.assertFalse(result["ok"])
                    self.assertEqual(
                        "route_b_formula_coverage_incomplete",
                        result["error"],
                    )
                    self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_partial_formula_coverage_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")
            document = json.loads((route_a / "document.json").read_text(encoding="utf-8"))
            document["children"].append(
                {
                    "label": "formula",
                    "text": "z",
                    "prov": [{"page_no": 1, "bbox": {"l": 30, "r": 40, "t": 100, "b": 80}}],
                }
            )
            (route_a / "document.json").write_text(json.dumps(document), encoding="utf-8")
            (route_a / "document.md").write_text("$$x$$\n\n$$z$$\n", encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_formula_coverage_incomplete", result["error"])
            self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.md").exists())

    def test_apply_all_rejects_missing_markdown_formula_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            for route, text in (
                (route_a, "u+v \\quad (2)"),
                (route_b, "u+v+w \\quad (2)"),
            ):
                document = json.loads((route / "document.json").read_text(encoding="utf-8"))
                document["children"].append(
                    {
                        "label": "formula",
                        "text": text,
                        "prov": [
                            {
                                "page_no": 1,
                                "bbox": {"l": 30, "r": 40, "t": 70, "b": 50},
                            }
                        ],
                    }
                )
                (route / "document.json").write_text(json.dumps(document), encoding="utf-8")
            # Route A advertises two formulas in JSON but exposes only the first
            # Markdown display block. apply-all must not publish partial output.
            (route_a / "document.md").write_text("$$x+y \\quad (1)$$\n", encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_a_markdown_formula_inventory_mismatch", result["error"])
            self.assertEqual(2, result["route_a_formula_count"])
            self.assertEqual(1, result["route_a_markdown_formula_count"])
            self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.md").exists())

    def test_markdown_replacement_uses_equation_identity_when_blocks_are_reordered(self) -> None:
        route_a_formulas = [
            {"formula_no": 1, "main_eq": 1, "text": "x+y \\quad (1)"},
            {"formula_no": 2, "main_eq": 2, "text": "u+v \\quad (2)"},
        ]
        replacement_log = [
            {
                "formula_no": 1,
                "eq_number": 1,
                "status": "replaced",
                "route_b_candidate": "x+y+z \\quad (1)",
            },
            {
                "formula_no": 2,
                "eq_number": 2,
                "status": "replaced",
                "route_b_candidate": "u+v+w \\quad (2)",
            },
        ]
        markdown = "$$u+v \\quad (2)$$\n\n$$x+y \\quad (1)$$\n"

        patched = fsp.patch_document_md(markdown, route_a_formulas, replacement_log)

        self.assertLess(patched.index("u+v+w"), patched.index("x+y+z"))
        self.assertTrue(
            all(entry.get("markdown_anchor_status") == "replaced_at_anchor" for entry in replacement_log)
        )

    def test_apply_all_ignores_display_math_inside_algorithm_code_fences(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "dummy", pdf_content=b"same")
            _make_temp_route(route_b, "dummy+z", pdf_content=b"same")
            fenced_prefix = (
                "```algorithm\n"
                "$$dummy$$\n"
                "```\n\n"
                "- ~~~~text\n"
                "  $$other$$\n"
                "  ~~~~\n\n"
                "> ````code\n"
                "> $$quoted$$\n"
                "> ````\n\n"
            )
            route_a_markdown = fenced_prefix + "Body formula:\n\n$$dummy$$\n"
            route_b_markdown = fenced_prefix + "Body formula:\n\n$$dummy+z$$\n"
            (route_a / "document.md").write_text(
                route_a_markdown,
                encoding="utf-8",
            )
            (route_b / "document.md").write_text(
                route_b_markdown,
                encoding="utf-8",
            )
            route_a_identity = fsp.validate_markdown_formula_identity(
                route_a_markdown,
                [{"formula_no": 1, "main_eq": None, "text": "dummy"}],
            )
            route_b_identity = fsp.validate_markdown_formula_identity(
                route_b_markdown,
                [{"formula_no": 1, "main_eq": None, "text": "dummy+z"}],
            )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(route_a_identity["ok"])
            self.assertEqual(1, route_a_identity["markdown_formula_count"])
            self.assertTrue(route_b_identity["ok"])
            self.assertEqual(1, route_b_identity["markdown_formula_count"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(
                "replaced_at_anchor",
                result["replacement_log"][0]["markdown_anchor_status"],
            )
            patched_markdown = (output / "document.md").read_text(encoding="utf-8")
            self.assertIn("```algorithm\n$$dummy$$\n```", patched_markdown)
            self.assertIn("- ~~~~text\n  $$other$$\n  ~~~~", patched_markdown)
            self.assertIn("> ````code\n> $$quoted$$\n> ````", patched_markdown)
            self.assertEqual(
                ["$$dummy+z$$"],
                fsp._markdown_display_formula_blocks(patched_markdown),
            )
            self.assertTrue(
                fsp.validate_markdown_formula_identity(
                    patched_markdown,
                    [{"formula_no": 1, "main_eq": None, "text": "dummy+z"}],
                )["ok"]
            )

    def test_unreadable_route_a_markdown_fails_closed_in_all_modes(self) -> None:
        for apply_all in (False, True):
            with self.subTest(apply_all=apply_all):
                with tempfile.TemporaryDirectory() as tmpdir:
                    base = Path(tmpdir)
                    route_a, route_b, output = (
                        base / "a",
                        base / "b",
                        base / "out",
                    )
                    for path in (route_a, route_b, output):
                        path.mkdir()
                    _make_temp_route(route_a, "x+y", pdf_content=b"same")
                    _make_temp_route(route_b, "x+y+z", pdf_content=b"same")
                    (route_a / "document.md").write_bytes(b"\xff")

                    result = fsp.run_formula_second_pass(
                        route_a,
                        route_b,
                        output,
                        apply_all=apply_all,
                    )

                    self.assertFalse(result["ok"])
                    self.assertEqual("route_a_markdown_unreadable", result["error"])
                    self.assertFalse((output / "document.json").exists())
                    self.assertFalse((output / "second_pass_summary.json").exists())

    def test_apply_all_rejects_hidden_markdown_formula_when_json_inventory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "", formula_node=False, pdf_content=b"same")
            _make_temp_route(route_b, "", formula_node=False, pdf_content=b"same")
            (route_a / "document.md").write_text("$$x+y$$\n", encoding="utf-8")

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertFalse(result["ok"])
            self.assertEqual("route_a_markdown_formula_inventory_mismatch", result["error"])
            self.assertEqual(0, result["route_a_formula_count"])
            self.assertEqual(1, result["route_a_markdown_formula_count"])
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_empty_formula_inventories_without_visual_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "", formula_node=False, pdf_content=b"same")
            _make_temp_route(route_b, "", formula_node=False, pdf_content=b"same")
            (route_a / "document.md").write_text("", encoding="utf-8")
            (route_b / "document.md").write_text("", encoding="utf-8")

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                "route_formula_inventory_empty_no_visual_evidence",
                result["error"],
            )
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rerenders_authoritative_evidence_without_route_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(
                route_a,
                "x+y \\quad (1)",
                pdf_content=b"same",
                include_evidence=False,
            )
            _make_temp_route(
                route_b,
                "x+y+z \\quad (1)",
                pdf_content=b"same",
                include_evidence=False,
            )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(result["ok"], result)
            entry = result["replacement_log"][0]
            for evidence_key in ("route_a_evidence", "route_b_evidence"):
                evidence = entry[evidence_key]
                self.assertIsNotNone(evidence["formula_crop"])
                self.assertIsNone(evidence["route_formula_crop"])
                self.assertEqual(
                    "authoritative_visual_pdf_rerender",
                    evidence["provenance"]["formula_crop"]["method"],
                )
                self.assertTrue(
                    fsp._packaged_evidence_is_available(evidence, output)
                )
            self.assertTrue((output / "document.json").is_file())
            self.assertTrue((output / "second_pass_summary.json").is_file())

    def test_arbitrary_bytes_cannot_satisfy_apply_all_evidence_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            for route, formula in (
                (route_a, "x+y \\quad (1)"),
                (route_b, "x+y+z \\quad (1)"),
            ):
                _make_temp_route(
                    route,
                    formula,
                    pdf_content=b"same",
                    include_evidence=False,
                )
                (route / "formulas").mkdir()
                (route / "pages").mkdir()
                (route / "formulas" / "formula_1.png").write_bytes(
                    b"formula-crop"
                )
                (route / "formulas" / "formula_1_context.png").write_bytes(
                    b"context"
                )
                (route / "pages" / "page_1.png").write_bytes(b"page")

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(result["ok"], result)
            for evidence_key in ("route_a_evidence", "route_b_evidence"):
                evidence = result["replacement_log"][0][evidence_key]
                self.assertIsNone(evidence["route_formula_crop"])
                self.assertIsNone(evidence["formula_context"])
                self.assertIsNone(evidence["full_page"])
                self.assertTrue(
                    fsp._packaged_evidence_is_available(evidence, output)
                )

    def test_context_only_cannot_satisfy_apply_all_evidence_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            for route, formula in (
                (route_a, "x+y \\quad (1)"),
                (route_b, "x+y+z \\quad (1)"),
            ):
                _make_temp_route(
                    route,
                    formula,
                    pdf_content=b"same",
                    include_evidence=False,
                )
                (route / "formulas").mkdir()
                _write_evidence_png(
                    route / "formulas" / "formula_1_context.png"
                )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(result["ok"], result)
            entry = result["replacement_log"][0]
            self.assertIsNotNone(entry["route_a_evidence"]["formula_context"])
            self.assertIsNotNone(entry["route_b_evidence"]["formula_context"])
            self.assertTrue(
                fsp._packaged_evidence_is_available(
                    entry["route_a_evidence"], output
                )
            )
            self.assertTrue((output / "document.json").is_file())

    def test_visual_evidence_requires_decodable_nonblank_image(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            corrupt = base / "corrupt.png"
            corrupt.write_bytes(b"\x89PNG\r\n\x1a\nnot-decodable")
            blank = base / "blank.png"
            Image.new("RGB", (64, 32), "white").save(blank, format="PNG")
            visible = base / "visible.png"
            _write_evidence_png(visible)
            tiny = base / "tiny.png"
            _write_evidence_png(tiny, size=(10, 10))
            undersized_page = base / "undersized-page.png"
            _write_evidence_png(undersized_page, size=(48, 48))

            self.assertFalse(fsp._visual_evidence_is_usable(corrupt))
            self.assertFalse(fsp._visual_evidence_is_usable(blank))
            self.assertFalse(fsp._visual_evidence_is_usable(tiny))
            self.assertFalse(
                fsp._visual_evidence_is_usable(undersized_page, "full_page")
            )
            self.assertTrue(
                fsp._visual_evidence_is_usable(undersized_page, "formula_crop")
            )
            self.assertTrue(fsp._visual_evidence_is_usable(visible))

    def test_unrelated_valid_png_without_rerender_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir)
            unrelated = output / "unrelated.png"
            _write_evidence_png(unrelated)
            forged_evidence = {
                "formula_crop": "unrelated.png",
                "audit_binding": {
                    "route_source_sha256": "a" * 64,
                    "formula_page_no": 1,
                    "formula_bbox": {
                        "x_center": 15.0,
                        "y_center": 30.0,
                        "width": 10.0,
                        "height": 20.0,
                    },
                    "expected_formula_index": 1,
                },
            }

            self.assertTrue(fsp._visual_evidence_is_usable(unrelated))
            self.assertFalse(
                fsp._packaged_evidence_is_available(forged_evidence, output)
            )

    def test_authoritative_crop_rejects_source_swapped_during_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route, output = base / "route", base / "out"
            route.mkdir()
            output.mkdir()
            _make_temp_route(
                route,
                "x+y \\quad (1)",
                pdf_content=b"verified-source",
                include_evidence=False,
            )
            source_sha, _source_detail = fsp._route_input_sha256(route)
            self.assertIsInstance(source_sha, str)
            replacement_pdf = base / "replacement.pdf"
            _write_pdf(replacement_pdf, b"replacement-source")
            document = fsp.load_json(route / "document.json")
            self.assertIsInstance(document, dict)
            formula = fsp.extract_formulas(document)[0]
            source_pdf = route / "source.pdf"
            real_open = os.open
            source_open_count = 0

            def open_and_swap(path, flags, *args, **kwargs):
                nonlocal source_open_count
                descriptor = real_open(path, flags, *args, **kwargs)
                if Path(path) == source_pdf:
                    source_open_count += 1
                    if source_open_count == 2:
                        replacement_pdf.replace(source_pdf)
                return descriptor

            with patch.object(fsp.os, "open", side_effect=open_and_swap):
                link, provenance = fsp._authoritative_formula_crop(
                    output,
                    "route",
                    route,
                    source_sha,
                    formula,
                    1,
                )

            self.assertIsNone(link)
            self.assertIsNone(provenance)
            self.assertGreaterEqual(source_open_count, 3)
            self.assertFalse(
                (output / "evidence/route/authoritative/formula_1_page_1.png").exists()
            )

    def test_authoritative_crop_replaces_destination_symlink_without_following(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route, output = base / "route", base / "out"
            route.mkdir()
            output.mkdir()
            _make_temp_route(
                route,
                "x+y \\quad (1)",
                pdf_content=b"verified-source",
                include_evidence=False,
            )
            source_sha, _source_detail = fsp._route_input_sha256(route)
            self.assertIsInstance(source_sha, str)
            document = fsp.load_json(route / "document.json")
            self.assertIsInstance(document, dict)
            formula = fsp.extract_formulas(document)[0]
            destination = (
                output
                / "evidence/route/authoritative/formula_1_page_1.png"
            )
            destination.parent.mkdir(parents=True)
            sentinel = base / "sentinel.png"
            sentinel_bytes = b"outside-sentinel"
            sentinel.write_bytes(sentinel_bytes)
            destination.symlink_to(sentinel)

            link, provenance = fsp._authoritative_formula_crop(
                output,
                "route",
                route,
                source_sha,
                formula,
                1,
            )

            self.assertEqual(
                "evidence/route/authoritative/formula_1_page_1.png",
                link,
            )
            self.assertIsInstance(provenance, dict)
            self.assertFalse(destination.is_symlink())
            self.assertTrue(fsp._visual_evidence_is_usable(destination, "formula_crop"))
            self.assertEqual(sentinel_bytes, sentinel.read_bytes())

    def test_tiny_crop_and_full_page_cannot_satisfy_apply_all_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            for route, formula in (
                (route_a, "x+y \\quad (1)"),
                (route_b, "x+y+z \\quad (1)"),
            ):
                _make_temp_route(
                    route,
                    formula,
                    pdf_content=b"same",
                    include_evidence=False,
                )
                (route / "formulas").mkdir()
                (route / "pages").mkdir()
                _write_evidence_png(
                    route / "formulas" / "formula_1.png", size=(10, 10)
                )
                _write_evidence_png(
                    route / "pages" / "page_1.png", size=(48, 48)
                )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(result["ok"], result)
            for evidence_key in ("route_a_evidence", "route_b_evidence"):
                evidence = result["replacement_log"][0][evidence_key]
                self.assertIsNotNone(evidence["formula_crop"])
                self.assertIsNone(evidence["route_formula_crop"])
                self.assertIsNone(evidence["full_page"])
                self.assertTrue(
                    fsp._packaged_evidence_is_available(evidence, output)
                )
            self.assertTrue((output / "document.json").is_file())

    def test_symlinked_formula_asset_cannot_satisfy_apply_all_evidence_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(
                route_a,
                "x+y \\quad (1)",
                pdf_content=b"same",
                include_evidence=False,
            )
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            external_asset = base / "external.png"
            _write_evidence_png(external_asset)
            external_bytes = external_asset.read_bytes()
            (route_a / "formulas").mkdir()
            (route_a / "formulas" / "formula_1.png").symlink_to(external_asset)

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertTrue(result["ok"], result)
            self.assertIsNone(
                result["replacement_log"][0]["route_a_evidence"][
                    "route_formula_crop"
                ]
            )
            self.assertTrue(
                fsp._packaged_evidence_is_available(
                    result["replacement_log"][0]["route_a_evidence"], output
                )
            )
            self.assertEqual(external_bytes, external_asset.read_bytes())
            self.assertTrue((output / "document.json").is_file())

    def test_markdown_replacement_rejects_similar_but_different_no_number_block(self) -> None:
        route_a_formulas = [
            {"formula_no": 1, "main_eq": None, "text": "a+b"},
        ]
        replacement_log = [
            {
                "formula_no": 1,
                "eq_number": None,
                "status": "replaced",
                "route_b_candidate": "a+b+c",
            }
        ]

        patched = fsp.patch_document_md("$$a+c$$", route_a_formulas, replacement_log)

        self.assertEqual("$$a+c$$", patched)
        self.assertEqual("anchor_missing", replacement_log[0]["markdown_anchor_status"])

    def test_markdown_replacement_rejects_one_token_difference_in_long_formula(self) -> None:
        route_a_formulas = [
            {
                "formula_no": 1,
                "main_eq": None,
                "text": "a+b+c+d+e+f+g+h+i+j",
            }
        ]
        replacement_log = [
            {
                "formula_no": 1,
                "eq_number": None,
                "status": "replaced",
                "route_b_candidate": "a+b+c+d+e+f+g+h+i+j+q",
            }
        ]

        patched = fsp.patch_document_md(
            "$$a+b+c+d+e+f+g+h+i+k$$",
            route_a_formulas,
            replacement_log,
        )

        self.assertEqual("$$a+b+c+d+e+f+g+h+i+k$$", patched)
        self.assertEqual("anchor_missing", replacement_log[0]["markdown_anchor_status"])

    def test_output_must_be_distinct_from_input_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b = base / "a", base / "b"
            route_a.mkdir()
            route_b.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")
            original = (route_a / "document.json").read_text(encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, route_a, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual(
                "formula_second_pass_output_must_be_distinct_from_input_routes",
                result["error"],
            )
            self.assertEqual(original, (route_a / "document.json").read_text(encoding="utf-8"))

    def test_output_symlink_is_rejected_without_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, external = base / "a", base / "b", base / "external"
            for path in (route_a, route_b, external):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")
            output_link = base / "out"
            output_link.symlink_to(external, target_is_directory=True)

            result = fsp.run_formula_second_pass(
                route_a, route_b, output_link, apply_all=True
            )

            self.assertFalse(result["ok"])
            self.assertEqual("formula_second_pass_output_symlink_not_allowed", result["error"])
            self.assertEqual([], list(external.iterdir()))

    def test_output_ancestor_symlink_is_rejected_without_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, external = base / "a", base / "b", base / "external"
            for path in (route_a, route_b, external):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(external, target_is_directory=True)
            output = linked_parent / "job"

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                "formula_second_pass_output_symlink_not_allowed", result["error"]
            )
            self.assertEqual([], list(external.iterdir()))

    def test_output_shared_root_symlink_is_rejected_without_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            external = base / "external"
            external.mkdir()
            shared_link = base / "shared-link"
            shared_link.symlink_to(external, target_is_directory=True)
            route_a, route_b, output = (
                shared_link / "a",
                shared_link / "b",
                shared_link / "out",
            )
            route_a.mkdir()
            route_b.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                "formula_second_pass_output_symlink_not_allowed", result["error"]
            )
            self.assertFalse((external / "out").exists())

    def test_preheld_output_lock_fails_without_mutating_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x+z", pdf_content=b"same")
            lock_path = base / ".out.formula_second_pass.lock"
            lock_bytes = b"live-owner\n"
            lock_path.write_bytes(lock_bytes)

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertFalse(result["ok"])
            self.assertEqual("formula_second_pass_output_locked", result["error"])
            self.assertEqual(lock_bytes, lock_path.read_bytes())
            self.assertEqual([], list(output.iterdir()))

    def test_symlinked_output_lock_does_not_touch_external_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x+z", pdf_content=b"same")
            sentinel = base / "sentinel.txt"
            sentinel.write_text("outside", encoding="utf-8")
            lock_path = base / ".out.formula_second_pass.lock"
            lock_path.symlink_to(sentinel)

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertFalse(result["ok"])
            self.assertEqual("formula_second_pass_output_locked", result["error"])
            self.assertTrue(lock_path.is_symlink())
            self.assertEqual("outside", sentinel.read_text(encoding="utf-8"))
            self.assertEqual([], list(output.iterdir()))

    def test_output_lock_write_failure_preserves_replacement_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x+z", pdf_content=b"same")
            lock_path = base / ".out.formula_second_pass.lock"
            competitor_bytes = b"replacement-owner\n"

            def fail_after_replacing_lock(_descriptor: int) -> None:
                lock_path.unlink()
                lock_path.write_bytes(competitor_bytes)
                raise OSError("injected lock fsync failure")

            with patch.object(fsp.os, "fsync", side_effect=fail_after_replacing_lock):
                result = fsp.run_formula_second_pass(
                    route_a, route_b, output, apply_all=True
                )

            self.assertFalse(result["ok"])
            self.assertEqual("formula_second_pass_output_lock_failed", result["error"])
            self.assertEqual(competitor_bytes, lock_path.read_bytes())
            self.assertEqual([], list(output.iterdir()))

    def test_output_lock_spans_preflight_and_rejects_nested_same_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x+z", pdf_content=b"same")
            original_cleanup = fsp._cleanup_orphan_formula_staging_dirs
            nested_results: list[dict[str, object]] = []
            triggered = False

            def cleanup_with_competitor(path: Path):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    nested_results.append(
                        fsp.run_formula_second_pass(
                            route_a, route_b, output, apply_all=True
                        )
                    )
                return original_cleanup(path)

            with patch.object(
                fsp,
                "_cleanup_orphan_formula_staging_dirs",
                side_effect=cleanup_with_competitor,
            ):
                result = fsp.run_formula_second_pass(
                    route_a, route_b, output, apply_all=True
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(1, len(nested_results))
            self.assertEqual(
                "formula_second_pass_output_locked", nested_results[0]["error"]
            )
            self.assertFalse((base / ".out.formula_second_pass.lock").exists())

    def test_output_locks_are_independent_for_different_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output_one, output_two = (
                base / "a",
                base / "b",
                base / "out-one",
                base / "out-two",
            )
            for path in (route_a, route_b, output_one, output_two):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x+z", pdf_content=b"same")
            original_cleanup = fsp._cleanup_orphan_formula_staging_dirs
            nested_results: list[dict[str, object]] = []
            triggered = False

            def cleanup_with_other_output(path: Path):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    nested_results.append(
                        fsp.run_formula_second_pass(
                            route_a, route_b, output_two, apply_all=True
                        )
                    )
                return original_cleanup(path)

            with patch.object(
                fsp,
                "_cleanup_orphan_formula_staging_dirs",
                side_effect=cleanup_with_other_output,
            ):
                result = fsp.run_formula_second_pass(
                    route_a, route_b, output_one, apply_all=True
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(1, len(nested_results))
            self.assertTrue(nested_results[0]["ok"], nested_results[0])
            self.assertTrue((output_one / "document.json").is_file())
            self.assertTrue((output_two / "document.json").is_file())
            self.assertFalse(
                (base / ".out-one.formula_second_pass.lock").exists()
            )
            self.assertFalse(
                (base / ".out-two.formula_second_pass.lock").exists()
            )

    def test_output_publication_failure_leaves_no_partial_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")

            with patch.object(fsp, "write_review_html", side_effect=OSError("disk full")):
                result = fsp.run_formula_second_pass(
                    route_a, route_b, output, apply_all=True
                )

            self.assertFalse(result["ok"])
            self.assertEqual(
                "formula_second_pass_output_publication_failed", result["error"]
            )
            self.assertEqual([], list(output.iterdir()))
            self.assertFalse((base / ".out.formula_second_pass.lock").exists())

    def test_orphan_staging_cleanup_removes_dead_owner_and_preserves_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y (1)", pdf_content=b"same")
            prefix = ".out.formula_second_pass_staging_"
            dead_staging = base / f"{prefix}99999999_dead"
            live_staging = base / f"{prefix}{os.getpid()}_live"
            old_legacy_staging = base / f"{prefix}legacy_old"
            recent_legacy_staging = base / f"{prefix}legacy_recent"
            unrelated = base / ".other.formula_second_pass_staging_99999999_dead"
            external = base / "external-staging-target"
            for path in (
                live_staging,
                old_legacy_staging,
                recent_legacy_staging,
                unrelated,
                external,
            ):
                path.mkdir()
            old_timestamp = time.time() - fsp.ORPHAN_STAGING_MIN_AGE_SECONDS - 60
            os.utime(old_legacy_staging, (old_timestamp, old_timestamp))
            (external / "sentinel").write_text("keep", encoding="utf-8")
            dead_staging.symlink_to(external, target_is_directory=True)

            with patch.object(
                fsp,
                "_process_is_alive",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                result = fsp.run_formula_second_pass(
                    route_a,
                    route_b,
                    output,
                    apply_all=True,
                )

            self.assertTrue(result["ok"])
            self.assertFalse(dead_staging.exists())
            self.assertTrue(live_staging.is_dir())
            self.assertFalse(old_legacy_staging.exists())
            self.assertTrue(recent_legacy_staging.is_dir())
            self.assertTrue(unrelated.is_dir())
            self.assertEqual("keep", (external / "sentinel").read_text(encoding="utf-8"))
            self.assertIn(
                dead_staging.name,
                result["removed_orphan_staging_entries"],
            )
            self.assertIn(
                old_legacy_staging.name,
                result["removed_orphan_staging_entries"],
            )

    def test_nonempty_output_is_rejected_without_overwriting_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")
            sentinel = output / "review_index.html"
            sentinel.write_text("old", encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("formula_second_pass_output_dir_not_empty", result["error"])
            self.assertEqual("old", sentinel.read_text(encoding="utf-8"))

    def test_input_routes_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            route = Path(tmpdir) / "route"
            output = Path(tmpdir) / "out"
            route.mkdir()
            output.mkdir()
            _make_temp_route(route, "x", pdf_content=b"same")

            result = fsp.run_formula_second_pass(route, route, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual(
                "formula_second_pass_input_routes_must_be_distinct",
                result["error"],
            )

    def test_regular_file_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_file = base / "route.json"
            route_file.write_text("{}", encoding="utf-8")
            route_b = base / "b"
            route_b.mkdir()
            _make_temp_route(route_b, "x", pdf_content=b"same")

            input_result = fsp.run_formula_second_pass(
                route_file, route_b, base / "out", apply_all=True
            )
            self.assertEqual("route_a_dir_must_be_directory", input_result["error"])

            route_a = base / "a"
            route_a.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            output_file = base / "sidecar"
            output_file.write_text("old", encoding="utf-8")
            output_result = fsp.run_formula_second_pass(
                route_a, route_b, output_file, apply_all=True
            )
            self.assertEqual(
                "formula_second_pass_output_must_be_directory",
                output_result["error"],
            )

    def test_apply_all_rejects_malformed_formula_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x", pdf_content=b"same")
            _make_temp_route(route_b, "x", pdf_content=b"same")
            document = json.loads((route_a / "document.json").read_text(encoding="utf-8"))
            document["children"][0].pop("text")
            (route_a / "document.json").write_text(json.dumps(document), encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_a_formula_inventory_malformed", result["error"])
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_stale_document_from_failed_route_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x=y", pdf_content=b"same")
            _make_temp_route(route_b, "x=y+z", pdf_content=b"same")
            (route_b / "status.json").write_text(
                json.dumps({"ok": False, "success_class": "failure"}),
                encoding="utf-8",
            )

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_status_not_successful", result["error"])
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_route_b_markdown_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            (route_b / "document.md").write_text("$$wrong \\quad (1)$$\n", encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_markdown_formula_identity_mismatch", result["error"])
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_route_b_markdown_inventory_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            route_b_doc = json.loads((route_b / "document.json").read_text(encoding="utf-8"))
            route_b_doc["children"].append(
                {
                    "label": "formula",
                    "text": "u+v \\quad (2)",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 30,
                                "r": 40,
                                "t": 70,
                                "b": 50,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            )
            (route_b / "document.json").write_text(json.dumps(route_b_doc), encoding="utf-8")
            # JSON advertises two formulas while Markdown still exposes one.

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_markdown_formula_inventory_mismatch", result["error"])
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_missing_route_b_markdown_for_formula_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            (route_b / "document.md").unlink()

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_markdown_missing", result["error"])
            self.assertFalse((output / "document.json").exists())

    def test_guarded_fallback_duplicate_same_page_equation_is_not_first_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, fallback, output = (
                base / "a",
                base / "b",
                base / "fallback",
                base / "out",
            )
            for path in (route_a, route_b, fallback, output):
                path.mkdir()
            _make_temp_route(route_a, "garbage \\quad (5)", pdf_content=b"same")
            _make_temp_route(route_b, "different \\quad (5)", pdf_content=b"same")
            _make_temp_route(fallback, "review-one \\quad (5)", pdf_content=b"same")
            fallback_doc = json.loads((fallback / "document.json").read_text(encoding="utf-8"))
            fallback_doc["children"].append(
                {
                    "label": "formula",
                    "text": "review-two \\quad (5)",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 30,
                                "r": 40,
                                "t": 70,
                                "b": 50,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            )
            (fallback / "document.json").write_text(json.dumps(fallback_doc), encoding="utf-8")

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                guarded_fallback_args=[f"reviewed={fallback}"],
                guarded_fallback_eqs={5},
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_formula_coverage_incomplete", result["error"])
            self.assertEqual(
                "guarded_fallback_ambiguous",
                result["replacement_log"][0]["status"],
            )
            self.assertFalse((output / "document.json").exists())

    def test_summary_fallback_count_counts_only_applied_guarded_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, fallback, output = (
                base / "a",
                base / "b",
                base / "fallback",
                base / "out",
            )
            for path in (route_a, route_b, fallback, output):
                path.mkdir()
            _make_temp_route(route_a, "garbage \\quad (5)", pdf_content=b"same")
            _make_temp_route(route_b, "different \\quad (5)", pdf_content=b"same")
            _make_temp_route(fallback, "x+y \\quad (5)", pdf_content=b"same")

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                guarded_fallback_args=[f"reviewed={fallback}"],
                guarded_fallback_eqs={5},
                apply_all=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(1, result["fallback_count"])
            self.assertEqual(1, result["guarded_fallback_count"])
            self.assertEqual(0, result["no_match_count"])
            entry = result["replacement_log"][0]
            for evidence_key in (
                "route_a_evidence",
                "route_b_evidence",
                "replacement_evidence",
            ):
                binding = entry[evidence_key]["audit_binding"]
                self.assertRegex(
                    str(binding["route_source_sha256"]),
                    r"^[0-9a-f]{64}$",
                )
                self.assertEqual(1, binding["formula_page_no"])
                self.assertEqual(1, binding["expected_formula_index"])
                self.assertIsInstance(binding["formula_bbox"], dict)


class RouteBIdentityCheckTests(unittest.TestCase):
    def test_apply_all_rejects_empty_route_a_with_nonempty_route_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(
                route_a,
                "",
                formula_node=False,
                pdf_content=b"same-content",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x+y",
                formula_node=True,
                pdf_content=b"same-content",
                pdf_relpath=route_b / "source.pdf",
            )

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "route_a_formula_inventory_empty_route_b_nonempty",
        )
        self.assertFalse((output / "document.json").exists())
        self.assertFalse((output / "document.md").exists())

    def test_apply_all_requires_same_input_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=b"same-content",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"same-content",
                pdf_relpath=route_b / "source.pdf",
            )

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertTrue(result["ok"])
            self.assertIsNotNone(result.get("route_b_source_identity_check"))
            self.assertTrue((output / "document.json").exists())
            self.assertTrue((output / "document.md").exists())

    def test_visual_original_pdf_identity_takes_priority_over_conversion_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            original_pdf = base / "submitted-original.pdf"
            original_sha = _write_pdf(original_pdf, b"submitted-original")
            conversions = (base / "conversion-a.pdf", base / "conversion-b.pdf")
            conversion_shas = (
                _write_pdf(conversions[0], b"conversion-a"),
                _write_pdf(conversions[1], b"conversion-b"),
            )
            for route, formula, conversion_pdf, conversion_sha in (
                (route_a, "x+y \\quad (1)", conversions[0], conversion_shas[0]),
                (route_b, "x+y+z \\quad (1)", conversions[1], conversion_shas[1]),
            ):
                _make_temp_route(
                    route,
                    formula,
                    pdf_content=None,
                    input_file=conversion_pdf,
                    include_evidence=False,
                )
                metadata = json.loads(
                    (route / "metadata.json").read_text(encoding="utf-8")
                )
                metadata.update(
                    {
                        "input_sha256": conversion_sha,
                        "conversion_input_file": str(conversion_pdf),
                        "conversion_input_sha256": conversion_sha,
                        "original_input_file": str(original_pdf),
                        "original_input_sha256": original_sha,
                        "visual_evidence_input_file": str(original_pdf),
                        "visual_evidence_input_sha256": original_sha,
                    }
                )
                (route / "metadata.json").write_text(
                    json.dumps(metadata), encoding="utf-8"
                )
                # A route-local conversion sibling may be a symlink in older
                # adapter outputs.  Once the visual-original contract exists,
                # it must neither shadow nor invalidate that authoritative PDF.
                (route / "source.pdf").symlink_to(conversion_pdf)

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertTrue(result["ok"], result)
            identity = result["route_b_source_identity_check"]
            self.assertEqual(original_sha, identity["route_a_source_sha256"])
            self.assertEqual(original_sha, identity["route_b_source_sha256"])
            self.assertEqual(
                "visual_evidence_original_pdf",
                identity["route_a_source_sha256_detail"]["identity_mode"],
            )
            self.assertTrue((route_a / "source.pdf").is_symlink())
            self.assertTrue((route_b / "source.pdf").is_symlink())
            self.assertNotEqual(conversion_shas[0], conversion_shas[1])

    def test_different_visual_original_pdfs_fail_even_with_same_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            conversion_pdf = base / "shared-conversion.pdf"
            conversion_sha = _write_pdf(conversion_pdf, b"shared-conversion")
            originals = (base / "original-a.pdf", base / "original-b.pdf")
            original_shas = (
                _write_pdf(originals[0], b"original-a"),
                _write_pdf(originals[1], b"original-b"),
            )
            for route, formula, original_pdf, original_sha in (
                (route_a, "x+y \\quad (1)", originals[0], original_shas[0]),
                (route_b, "x+y+z \\quad (1)", originals[1], original_shas[1]),
            ):
                _make_temp_route(
                    route,
                    formula,
                    pdf_content=None,
                    input_file=conversion_pdf,
                    include_evidence=False,
                )
                metadata = json.loads(
                    (route / "metadata.json").read_text(encoding="utf-8")
                )
                metadata.update(
                    {
                        "input_sha256": conversion_sha,
                        "conversion_input_file": str(conversion_pdf),
                        "conversion_input_sha256": conversion_sha,
                        "original_input_file": str(original_pdf),
                        "original_input_sha256": original_sha,
                        "visual_evidence_input_file": str(original_pdf),
                        "visual_evidence_input_sha256": original_sha,
                    }
                )
                (route / "metadata.json").write_text(
                    json.dumps(metadata), encoding="utf-8"
                )

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_identity_mismatch", result["error"])
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_rejects_same_pdf_from_different_job_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "route_a", base / "route_b", base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            for route, job_id in ((route_a, "job-a"), (route_b, "job-b")):
                metadata = json.loads(
                    (route / "metadata.json").read_text(encoding="utf-8")
                )
                metadata["job_id"] = job_id
                (route / "metadata.json").write_text(
                    json.dumps(metadata),
                    encoding="utf-8",
                )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual("route_contract_job_id_mismatch", result["error"])
            self.assertEqual(
                {"route_a_job_id": "job-a", "route_b_job_id": "job-b"},
                result["route_job_identity_check"],
            )
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_requires_symmetric_nonempty_job_ids(self) -> None:
        cases = (
            ("missing", False, None),
            ("empty", True, ""),
            ("null", True, None),
        )
        for case, route_b_declares_job_id, route_b_job_id in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmpdir:
                    base = Path(tmpdir)
                    route_a = base / "route_a"
                    route_b = base / "route_b"
                    output = base / "output"
                    for path in (route_a, route_b, output):
                        path.mkdir()
                    _make_temp_route(
                        route_a,
                        "x+y \\quad (1)",
                        pdf_content=b"same",
                    )
                    _make_temp_route(
                        route_b,
                        "x+y+z \\quad (1)",
                        pdf_content=b"same",
                    )
                    route_a_metadata = json.loads(
                        (route_a / "metadata.json").read_text(encoding="utf-8")
                    )
                    route_a_metadata["job_id"] = "job-a"
                    (route_a / "metadata.json").write_text(
                        json.dumps(route_a_metadata),
                        encoding="utf-8",
                    )
                    if route_b_declares_job_id:
                        route_b_metadata = json.loads(
                            (route_b / "metadata.json").read_text(encoding="utf-8")
                        )
                        route_b_metadata["job_id"] = route_b_job_id
                        (route_b / "metadata.json").write_text(
                            json.dumps(route_b_metadata),
                            encoding="utf-8",
                        )

                    result = fsp.run_formula_second_pass(
                        route_a,
                        route_b,
                        output,
                        apply_all=True,
                    )

                    self.assertFalse(result["ok"])
                    self.assertEqual(
                        "route_contract_job_id_mismatch",
                        result["error"],
                    )
                    self.assertEqual(
                        "job-a",
                        result["route_job_identity_check"]["route_a_job_id"],
                    )
                    self.assertIsNone(
                        result["route_job_identity_check"]["route_b_job_id"]
                    )
                    self.assertFalse((output / "document.json").exists())

    def test_pdf_header_scan_accepts_signature_after_short_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_pdf = Path(tmpdir) / "source.pdf"
            _write_pdf(source_pdf, b"valid-preamble-pdf", preamble=b"p" * 1019)

            self.assertTrue(fsp._has_pdf_header(source_pdf))

    def test_pdf_header_scan_rejects_missing_or_too_late_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            candidates = {
                "missing": b"ordinary regular bytes, not a PDF",
                "outside_scan_window": _pdf_fixture_bytes(
                    b"valid-but-header-too-late",
                    preamble=b"p" * 1024,
                ),
                "header_only_not_parseable": b"%PDF-1.7\n%%EOF\n",
            }
            for name, payload in candidates.items():
                with self.subTest(name=name):
                    source_pdf = base / f"{name}.pdf"
                    source_pdf.write_bytes(payload)
                    self.assertFalse(fsp._has_pdf_header(source_pdf))

    def test_arbitrary_regular_bytes_are_not_accepted_as_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "route_a", base / "route_b", base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            invalid_pdf = b"ordinary regular bytes, not a PDF"
            invalid_sha = hashlib.sha256(invalid_pdf).hexdigest()
            for route in (route_a, route_b):
                (route / "source.pdf").write_bytes(invalid_pdf)
                metadata = json.loads(
                    (route / "metadata.json").read_text(encoding="utf-8")
                )
                metadata["input_sha256"] = invalid_sha
                (route / "metadata.json").write_text(
                    json.dumps(metadata),
                    encoding="utf-8",
                )

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual("route_b_identity_unverified", result["error"])
            identity = result["route_b_source_identity_check"]
            self.assertEqual(
                "invalid_pdf",
                identity["route_a_source_sha256_detail"]["status"],
            )
            self.assertEqual(
                "input_pdf_header_missing",
                identity["route_b_source_sha256_detail"]["reason"],
            )
            self.assertFalse((output / "document.json").exists())

    def test_apply_all_blocks_unverified_identity_and_does_not_patch_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=None,
                pdf_relpath=None,
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"pdf-b",
                pdf_relpath=route_b / "source.pdf",
            )

            (route_a / "metadata.json").unlink()
            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "route_b_identity_unverified")
            self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.md").exists())

    def test_apply_all_rejects_guarded_fallback_from_different_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, fallback, output = (
                base / "a",
                base / "b",
                base / "fallback",
                base / "out",
            )
            for path in (route_a, route_b, fallback, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (5)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (5)", pdf_content=b"same")
            _make_temp_route(fallback, "stale \\quad (5)", pdf_content=b"different")

            result = fsp.run_formula_second_pass(
                route_a,
                route_b,
                output,
                guarded_fallback_args=[f"legacy={fallback}"],
                guarded_fallback_eqs={5},
                apply_all=True,
            )

            self.assertFalse(result["ok"])
            self.assertEqual("guarded_fallback_identity_unverified", result["error"])
            self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.md").exists())

    def test_apply_all_packages_review_evidence_inside_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a, route_b, output = base / "a", base / "b", base / "out"
            for path in (route_a, route_b, output):
                path.mkdir()
            _make_temp_route(route_a, "x+y \\quad (1)", pdf_content=b"same")
            _make_temp_route(route_b, "x+y+z \\quad (1)", pdf_content=b"same")
            for route in (route_a, route_b):
                (route / "formulas").mkdir(exist_ok=True)
                (route / "pages").mkdir(exist_ok=True)
                _write_evidence_png(route / "formulas" / "formula_1.png")
                _write_evidence_png(
                    route / "formulas" / "formula_1_context.png"
                )
                _write_evidence_png(
                    route / "pages" / "page_1.png", size=(128, 128)
                )

            result = fsp.run_formula_second_pass(
                route_a, route_b, output, apply_all=True
            )

            self.assertTrue(result["ok"])
            entry = result["replacement_log"][0]
            identity = result["route_b_source_identity_check"]
            for route_label, evidence in (
                ("route_a", entry["route_a_evidence"]),
                ("route_b", entry["route_b_evidence"]),
            ):
                for key in ("formula_crop", "formula_context", "full_page"):
                    relative = evidence[key]
                    self.assertIsNotNone(relative)
                    self.assertNotIn("..", str(relative))
                    self.assertTrue((output / str(relative)).is_file())
                    self.assertTrue(
                        fsp._visual_evidence_is_usable(output / str(relative), key)
                    )
                binding = evidence["audit_binding"]
                self.assertEqual(
                    identity[f"{route_label}_source_sha256"],
                    binding["route_source_sha256"],
                )
                self.assertEqual(1, binding["formula_page_no"])
                self.assertEqual(1, binding["expected_formula_index"])
                self.assertIsInstance(binding["formula_bbox"], dict)

    def test_apply_all_with_relative_input_file_uses_cwd_and_route_dir_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            original_cwd = Path.cwd()
            try:
                os.chdir(base)
                shared_pdf = Path("shared_input.pdf")
                shared_sha = _write_pdf(shared_pdf, b"shared-content")
                _make_temp_route(
                    route_a,
                    "x + y",
                    pdf_content=None,
                    input_file="shared_input.pdf",
                )
                _make_temp_route(
                    route_b,
                    "x + y",
                    pdf_content=None,
                    input_file="shared_input.pdf",
                )
                for route in (route_a, route_b):
                    metadata = json.loads(
                        (route / "metadata.json").read_text(encoding="utf-8")
                    )
                    metadata["input_sha256"] = shared_sha
                    (route / "metadata.json").write_text(
                        json.dumps(metadata), encoding="utf-8"
                    )

                result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
                self.assertTrue(result["ok"])
            finally:
                os.chdir(original_cwd)

    def test_apply_all_uses_route_dir_relative_input_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=None,
                input_file="relative-route.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=None,
                input_file="relative-route.pdf",
            )
            shared_sha = _write_pdf(
                route_a / "relative-route.pdf", b"route-rel-content"
            )
            self.assertEqual(
                shared_sha,
                _write_pdf(route_b / "relative-route.pdf", b"route-rel-content"),
            )
            for route in (route_a, route_b):
                metadata = json.loads(
                    (route / "metadata.json").read_text(encoding="utf-8")
                )
                metadata["input_sha256"] = shared_sha
                (route / "metadata.json").write_text(
                    json.dumps(metadata), encoding="utf-8"
                )

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertTrue(result["ok"])

    def test_apply_all_rejects_stale_metadata_only_hash_without_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            stale_sha = "a" * 64
            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=None,
                input_file="missing.pdf",
                include_metadata_sha=stale_sha,
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=None,
                input_file="missing.pdf",
                include_metadata_sha=stale_sha,
            )
            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "route_b_identity_unverified")

    def test_apply_all_rejects_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=b"source-a",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"source-a",
                pdf_relpath=route_b / "source.pdf",
            )

            metadata = json.loads((route_a / "metadata.json").read_text(encoding="utf-8"))
            metadata["input_sha256"] = "f" * 64
            (route_a / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "route_b_identity_mismatch")
            self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.md").exists())

    def test_route_local_source_mismatch_cannot_be_hidden_by_external_metadata_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route = base / "route"
            route.mkdir()
            external = base / "external.pdf"
            external_sha = _write_pdf(external, b"expected")
            _write_pdf(route / "source.pdf", b"stale-route-local")
            (route / "metadata.json").write_text(
                json.dumps(
                    {
                        "input_file": str(external),
                        "input_sha256": external_sha,
                    }
                ),
                encoding="utf-8",
            )

            sha, detail = fsp._route_input_sha256(route)

            self.assertIsNone(sha)
            self.assertEqual("metadata_mismatch", detail["status"])
            self.assertEqual(str(route / "source.pdf"), detail["candidate_path"])

    def test_unverified_source_reference_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            route = Path(tmpdir) / "route"
            route.mkdir()
            source_sha = _write_pdf(route / "source.pdf", b"expected")
            (route / "metadata.json").write_text(
                json.dumps(
                    {
                        "input_sha256": source_sha,
                        "input_file_reference": str(route / "source.pdf"),
                        "input_file_reference_verified": False,
                        "input_file_reference_mode": "existing_mismatch",
                    }
                ),
                encoding="utf-8",
            )

            sha, detail = fsp._route_input_sha256(route)

            self.assertIsNone(sha)
            self.assertEqual("metadata_mismatch", detail["status"])
            self.assertEqual("input_file_reference_not_verified", detail["reason"])

    def test_apply_all_supports_nested_source_sha256_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=b"nested-source",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"nested-source",
                pdf_relpath=route_b / "source.pdf",
            )

            # Replace placeholder checksums with nested source.sha256 metadata fields.
            # (placeholder content was overwritten from computed bytes sha in helper file writes)
            shared_sha = hashlib.sha256(
                (route_a / "source.pdf").read_bytes()
            ).hexdigest()
            metadata_a = {
                "source": {
                    "sha256": shared_sha,
                },
                "input_file": str(route_a / "source.pdf"),
            }
            metadata_b = {
                "source": {
                    "sha256": shared_sha,
                },
                "input_file": str(route_b / "source.pdf"),
            }
            (route_a / "metadata.json").write_text(json.dumps(metadata_a, ensure_ascii=False), encoding="utf-8")
            (route_b / "metadata.json").write_text(json.dumps(metadata_b, ensure_ascii=False), encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertTrue(result["ok"])

    def test_apply_all_supports_literal_source_dot_sha256_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=b"literal-source",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"literal-source",
                pdf_relpath=route_b / "source.pdf",
            )

            metadata_a = json.loads((route_a / "metadata.json").read_text(encoding="utf-8"))
            metadata_b = json.loads((route_b / "metadata.json").read_text(encoding="utf-8"))
            shared_sha = hashlib.sha256(
                (route_a / "source.pdf").read_bytes()
            ).hexdigest()
            metadata_a["source.sha256"] = shared_sha
            metadata_b["source.sha256"] = shared_sha
            (route_a / "metadata.json").write_text(json.dumps(metadata_a, ensure_ascii=False), encoding="utf-8")
            (route_b / "metadata.json").write_text(json.dumps(metadata_b, ensure_ascii=False), encoding="utf-8")

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertTrue(result["ok"])

    def test_apply_all_blocks_when_source_pdf_checksums_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=b"source-a",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"source-b",
                pdf_relpath=route_b / "source.pdf",
            )

            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "route_b_identity_mismatch")
            self.assertFalse((output / "document.json").exists())
            self.assertFalse((output / "document.md").exists())

    def test_non_apply_all_is_not_blocked_by_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            route_a = base / "route_a"
            route_b = base / "route_b"
            output = base / "output"
            for path in (route_a, route_b, output):
                path.mkdir()

            _make_temp_route(
                route_a,
                "x + y",
                pdf_content=b"source-a",
                pdf_relpath=route_a / "source.pdf",
            )
            _make_temp_route(
                route_b,
                "x + y",
                pdf_content=b"source-b",
                pdf_relpath=route_b / "source.pdf",
            )

            # Remove metadata to avoid accidental identity check from metadata sha in this mode.
            (route_a / "metadata.json").unlink()
            (route_b / "metadata.json").unlink()
            result = fsp.run_formula_second_pass(route_a, route_b, output, apply_all=False)
            self.assertTrue(result["ok"])
            self.assertTrue((output / "document.json").exists())
