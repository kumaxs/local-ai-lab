from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import semantic_reflow  # noqa: E402


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff"
    b"\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _picture_document(count: int = 8, *, duplicate_caption: bool = False) -> dict:
    texts = []
    pictures = []
    for index in range(count):
        caption = (
            "Figure duplicate caption"
            if duplicate_caption
            else f"Figure {index + 1}: unique source caption {index + 1}"
        )
        texts.append(
            {
                "self_ref": f"#/texts/{index}",
                "label": "caption",
                "text": caption,
            }
        )
        pictures.append(
            {
                "self_ref": f"#/pictures/{index}",
                "label": "picture",
                "captions": [{"$ref": f"#/texts/{index}"}],
                "image": None,
            }
        )
    return {"texts": texts, "pictures": pictures}


def _write_surfaces(
    output_dir: Path,
    document: dict,
    *,
    markdown_count: int = 8,
) -> None:
    html_figures = []
    markdown_lines = []
    for index, text in enumerate(document["texts"]):
        caption = text["text"]
        html_figures.append(
            f'<figure><figcaption><div class="caption">{caption}</div></figcaption>'
            f'<img src="data:image/png;base64,AAAA"></figure>'
        )
        if index < markdown_count:
            markdown_lines.append(caption)
    (output_dir / "document.html").write_text(
        "<html><body>" + "\n".join(html_figures) + "</body></html>\n",
        encoding="utf-8",
    )
    (output_dir / "document.md").write_text(
        "\n\n".join(markdown_lines) + "\n",
        encoding="utf-8",
    )


def _write_crops(output_dir: Path, count: int = 8) -> None:
    pictures_dir = output_dir / "pictures"
    pictures_dir.mkdir()
    for index in range(1, count + 1):
        (pictures_dir / f"picture_{index}.png").write_bytes(_PNG_BYTES)


def _picture_metadata(
    documents: list[dict],
    *,
    expected_flags: list[bool] | None = None,
) -> dict:
    records = []
    global_index = 0
    for document in documents:
        for node in document.get("pictures") or []:
            global_index += 1
            raw_ref = node.get("self_ref")
            source_ref = semantic_reflow._cjk_picture_source_ref(node)
            records.append(
                {
                    "index": global_index,
                    "global_index": global_index,
                    "source_ref": source_ref,
                    "source_asset": f"pictures/picture_{global_index}.png",
                    "self_ref": raw_ref if isinstance(raw_ref, str) else raw_ref,
                    "part_index": node.get("_local_ai_lab_chunk_part_index"),
                    "machine_binding_expected": (
                        expected_flags[global_index - 1]
                        if expected_flags is not None
                        else True
                    ),
                }
            )
    return {"structural_visual_provenance_manifest": {"pictures": records}}


def _bind(output_dir: Path, documents: list[dict]) -> dict:
    return semantic_reflow._bind_cjk_picture_source_assets(
        output_dir,
        documents,
        _picture_metadata(documents),
    )


class _RenderPictureSource:
    def page_size(self, _page_no: int) -> tuple[float, float]:
        return 100.0, 100.0

    def text(self, _prov: dict, *, layout: bool = False, padding: float = 0.0) -> str:
        del layout, padding
        return ""


class CJKPictureBindingTests(unittest.TestCase):
    def test_generic_picture_renderer_emits_escaped_html_and_chunk_markers(self) -> None:
        node = {
            "self_ref": "#/pictures/0&\"",
            "_local_ai_lab_chunk_part_index": 2,
            "captions": [{"$ref": "#/texts/0"}],
            "_semantic_picture_path": "pictures/picture_1.png",
        }
        document = {
            "name": "Generic picture",
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "caption",
                    "text": "Figure 1: source caption",
                }
            ],
        }
        item = semantic_reflow.FlowItem(
            kind="picture",
            node=node,
            rank=1.0,
            page_no=1,
            bbox={"l": 0.0, "r": 40.0, "t": 40.0, "b": 0.0},
            prov={"page_no": 1, "bbox": {"l": 0.0, "r": 40.0, "t": 40.0, "b": 0.0}},
            collection_index=0,
        )

        html_text, markdown_text, counts = semantic_reflow._render(
            [item],
            document,
            _RenderPictureSource(),
        )

        self.assertEqual(1, counts["pictures"])
        self.assertIn(
            'data-source-ref="chunk:2:#/pictures/0&amp;&quot;"',
            html_text,
        )
        self.assertIn(
            "![Figure 1: source caption](pictures/picture_1.png)\n"
            "<!-- source-picture-ref:chunk:2:#/pictures/0&\" -->",
            markdown_text,
        )

    def test_oversized_embedded_picture_is_rejected_before_decode(self) -> None:
        max_encoded_chars = (
            (semantic_reflow._CJK_PICTURE_MAX_ASSET_BYTES + 2) // 3
        ) * 4
        encoded = "A" * (max_encoded_chars + 1)
        document = {
            "pictures": [
                {
                    "image": {"uri": f"data:image/png;base64,{encoded}"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with mock.patch.object(
                semantic_reflow.base64,
                "b64decode",
                side_effect=AssertionError("oversized payload reached decoder"),
            ) as decode:
                result = semantic_reflow._materialize_picture_assets(
                    output_dir,
                    [document],
                )

            self.assertEqual({"written": 0, "skipped": 1}, result)
            decode.assert_not_called()
            self.assertFalse((output_dir / "pictures").exists())
            self.assertNotIn(
                "_semantic_picture_path",
                document["pictures"][0],
            )

    def test_oversized_decoder_result_is_rejected_without_writing(self) -> None:
        document = {
            "pictures": [
                {
                    "image": {"uri": "data:image/png;base64,AAAA"},
                }
            ]
        }

        class OversizedPayload:
            def __bool__(self) -> bool:
                return True

            def __len__(self) -> int:
                return semantic_reflow._CJK_PICTURE_MAX_ASSET_BYTES + 1

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with mock.patch.object(
                semantic_reflow.base64,
                "b64decode",
                return_value=OversizedPayload(),
            ) as decode:
                result = semantic_reflow._materialize_picture_assets(
                    output_dir,
                    [document],
                )

            self.assertEqual({"written": 0, "skipped": 1}, result)
            decode.assert_called_once_with("AAAA", validate=False)
            self.assertFalse((output_dir / "pictures").exists())
            self.assertNotIn(
                "_semantic_picture_path",
                document["pictures"][0],
            )

    def test_parent_directory_swap_cannot_escape_picture_materialization(self) -> None:
        document = {
            "pictures": [
                {
                    "image": {"uri": "data:image/png;base64,AAAA"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            pictures_dir = output_dir / "pictures"
            renamed_dir = output_dir / "pictures-original"
            outside_dir = output_dir / "outside"
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if (
                    not swapped
                    and os.fspath(path) == "picture_1.png"
                    and flags & os.O_CREAT
                ):
                    swapped = True
                    pictures_dir.rename(renamed_dir)
                    outside_dir.mkdir()
                    pictures_dir.symlink_to(outside_dir, target_is_directory=True)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(semantic_reflow.os, "open", side_effect=racing_open):
                result = semantic_reflow._materialize_picture_assets(
                    output_dir,
                    [document],
                )

            self.assertTrue(swapped)
            self.assertEqual({"written": 0, "skipped": 1}, result)
            self.assertFalse((outside_dir / "picture_1.png").exists())
            self.assertFalse((renamed_dir / "picture_1.png").exists())
            self.assertNotIn("_semantic_picture_path", document["pictures"][0])

    def test_oversized_existing_crop_is_not_reused(self) -> None:
        document = {"pictures": [{"image": None}]}
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            crop = output_dir / "pictures" / "picture_1.png"
            crop.parent.mkdir()
            with crop.open("wb") as handle:
                handle.truncate(semantic_reflow._CJK_PICTURE_MAX_ASSET_BYTES + 1)

            result = semantic_reflow._materialize_picture_assets(
                output_dir,
                [document],
            )

            self.assertEqual({"written": 0, "skipped": 1}, result)
            self.assertNotIn("_semantic_picture_path", document["pictures"][0])

    def test_eight_unique_captioned_pictures_bind_both_surfaces(self) -> None:
        document = _picture_document()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir)
            _write_surfaces(output_dir, document)

            result = _bind(output_dir, [document])

            self.assertTrue(result["ok"])
            self.assertEqual(result["fully_bound_count"], 8)
            self.assertEqual(result["html_bound_count"], 8)
            self.assertEqual(result["markdown_bound_count"], 8)
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertEqual(html_text.count('data-source-ref="#/pictures/'), 8)
            self.assertEqual(markdown_text.count("source-picture-ref:#/pictures/"), 8)
            for index in range(1, 9):
                self.assertEqual(html_text.count(f'src="pictures/picture_{index}.png"'), 1)
                self.assertEqual(markdown_text.count(f"](pictures/picture_{index}.png)"), 1)

    def test_duplicate_caption_fails_closed_without_surface_mutation(self) -> None:
        document = _picture_document(duplicate_caption=True)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 2)
            _write_surfaces(output_dir, document, markdown_count=2)
            before_html = (output_dir / "document.html").read_bytes()
            before_markdown = (output_dir / "document.md").read_bytes()

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["fully_bound_count"], 0)
            self.assertIn("picture_caption_ambiguous", result["failure_reasons"])
            self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
            self.assertEqual(before_markdown, (output_dir / "document.md").read_bytes())

    def test_unsafe_crop_path_fails_closed(self) -> None:
        document = _picture_document(1)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            pictures_dir = output_dir / "pictures"
            pictures_dir.mkdir()
            outside = output_dir / "outside.png"
            outside.write_bytes(_PNG_BYTES)
            (pictures_dir / "picture_1.png").symlink_to(outside)
            _write_surfaces(output_dir, document, markdown_count=1)
            before_html = (output_dir / "document.html").read_bytes()
            before_markdown = (output_dir / "document.md").read_bytes()

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertIn("picture_asset_missing_or_unsafe", result["failure_reasons"])
            self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
            self.assertEqual(before_markdown, (output_dir / "document.md").read_bytes())

    def test_binding_is_idempotent(self) -> None:
        document = _picture_document()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir)
            _write_surfaces(output_dir, document)
            first = _bind(output_dir, [document])
            html_after_first = (output_dir / "document.html").read_bytes()
            markdown_after_first = (output_dir / "document.md").read_bytes()

            second = _bind(output_dir, [document])

            self.assertEqual(first, second)
            self.assertEqual(html_after_first, (output_dir / "document.html").read_bytes())
            self.assertEqual(markdown_after_first, (output_dir / "document.md").read_bytes())

    def test_missing_markdown_caption_does_not_bind_that_side(self) -> None:
        document = _picture_document(2)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 2)
            _write_surfaces(output_dir, document, markdown_count=1)

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["html_bound_count"], 2)
            self.assertEqual(result["markdown_bound_count"], 1)
            second = result["records"][1]
            self.assertTrue(second["html_bound"])
            self.assertFalse(second["markdown_bound"])
            self.assertIn("picture_markdown_caption_candidate_missing", second["reasons"])
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertNotIn("pictures/picture_2.png", markdown_text)

    def test_picture_node_limit_fails_closed_before_asset_materialization(self) -> None:
        document = _picture_document(129)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_surfaces(output_dir, document, markdown_count=0)

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["records"], [])
            self.assertIn("picture_node_limit_exceeded", result["failure_reasons"])
            self.assertFalse((output_dir / "pictures").exists())

    def test_caption_size_limit_fails_closed(self) -> None:
        document = _picture_document(1)
        document["texts"][0]["text"] = "x" * (
            semantic_reflow._CJK_PICTURE_MAX_CAPTION_CHARS + 1
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            _write_surfaces(output_dir, document, markdown_count=1)
            before_html = (output_dir / "document.html").read_bytes()
            before_markdown = (output_dir / "document.md").read_bytes()

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertIn(
                "picture_caption_size_limit_exceeded",
                result["failure_reasons"],
            )
            self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
            self.assertEqual(
                before_markdown,
                (output_dir / "document.md").read_bytes(),
            )

    def test_unsafe_source_ref_fails_closed_without_rewriting_identity(self) -> None:
        for unsafe_ref in (
            "#/pictures/0--unsafe",
            "#/pictures/0\nunsafe",
            "#/pictures/0\runsafe",
            "#/pictures/0\x00unsafe",
        ):
            with self.subTest(unsafe_ref=repr(unsafe_ref)):
                document = _picture_document(1)
                document["pictures"][0]["self_ref"] = unsafe_ref
                with tempfile.TemporaryDirectory() as directory:
                    output_dir = Path(directory)
                    _write_crops(output_dir, 1)
                    _write_surfaces(output_dir, document, markdown_count=1)
                    before_html = (output_dir / "document.html").read_bytes()
                    before_markdown = (output_dir / "document.md").read_bytes()

                    result = _bind(output_dir, [document])

                    self.assertFalse(result["ok"])
                    self.assertTrue(
                        {
                            "picture_self_ref_missing_or_invalid",
                            "picture_manifest_identity_mismatch",
                        }
                        & set(result["failure_reasons"])
                    )
                    self.assertEqual(
                        before_html,
                        (output_dir / "document.html").read_bytes(),
                    )
                    self.assertEqual(
                        before_markdown,
                        (output_dir / "document.md").read_bytes(),
                    )
                    self.assertIsNone(
                        semantic_reflow._cjk_picture_source_ref(
                            document["pictures"][0]
                        )
                    )

    def test_markdown_alt_is_fixed_and_unsafe_caption_lines_are_ignored(self) -> None:
        document = _picture_document(1)
        document["texts"][0]["text"] = "Figure 1: caption [unsafe]"
        caption = document["texts"][0]["text"]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            _write_surfaces(output_dir, document, markdown_count=0)
            (output_dir / "document.md").write_text(
                "\n".join(
                    (
                        f"<!-- {caption} -->",
                        f"<div>{caption}</div>",
                        f"    {caption}",
                        f"\t{caption}",
                        caption,
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            result = _bind(output_dir, [document])

            self.assertTrue(result["ok"])
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertIn("![Source picture 1](pictures/picture_1.png)", markdown_text)
            self.assertNotIn("![Figure 1: caption [unsafe]]", markdown_text)
            self.assertEqual(markdown_text.count("pictures/picture_1.png"), 1)

    def test_html_failure_does_not_block_markdown_binding(self) -> None:
        document = _picture_document(1)
        caption = document["texts"][0]["text"]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            (output_dir / "document.html").write_text(
                f"<figure><figcaption>{caption}</figcaption>"
                f"<figcaption>{caption}</figcaption>"
                '<img src="data:image/png;base64,AAAA"></figure>\n',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                caption + "\n",
                encoding="utf-8",
            )

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["html_bound_count"], 0)
            self.assertEqual(result["markdown_bound_count"], 1)
            self.assertIn(
                "picture_html_caption_candidate_missing",
                result["failure_reasons"],
            )
            self.assertIn(
                "pictures/picture_1.png",
                (output_dir / "document.md").read_text(encoding="utf-8"),
            )

    def test_markdown_failure_does_not_block_html_binding(self) -> None:
        document = _picture_document(1)
        caption = document["texts"][0]["text"]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            (output_dir / "document.html").write_text(
                f'<figure><figcaption>{caption}</figcaption>'
                '<img src="data:image/png;base64,AAAA"></figure>\n',
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(
                "A different paragraph without the picture caption.\n",
                encoding="utf-8",
            )

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["html_bound_count"], 1)
            self.assertEqual(result["markdown_bound_count"], 0)
            self.assertIn(
                "picture_markdown_caption_candidate_missing",
                result["failure_reasons"],
            )
            self.assertIn(
                'src="pictures/picture_1.png"',
                (output_dir / "document.html").read_text(encoding="utf-8"),
            )

    def test_top_level_picture_failure_warns_even_without_records(self) -> None:
        document = _picture_document(0)

        class CJKSource:
            def __init__(self, _input_file: Path) -> None:
                pass

            def language_profile(self, *, page_limit: int = 3) -> dict[str, int]:
                del page_limit
                return {"cjk_characters": 200, "latin_characters": 100}

            def close(self) -> None:
                pass

        picture_failure = {
            "ok": False,
            "materialization": {"written": 0, "skipped": 0},
            "records": [],
            "html_bound_count": 0,
            "markdown_bound_count": 0,
            "fully_bound_count": 0,
            "unbound_count": 0,
            "failure_reasons": ["picture_surface_size_limit_exceeded"],
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            status = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            metadata: dict[str, object] = {}
            with (
                mock.patch.object(semantic_reflow, "SourceReader", CJKSource),
                mock.patch.object(
                    semantic_reflow,
                    "_document_parts_with_global_pages",
                    return_value=(document, [(None, document)]),
                ),
                mock.patch.object(
                    semantic_reflow,
                    "_remove_review_evidence_from_primary_surfaces",
                    return_value={"removed": 0},
                ),
                mock.patch.object(
                    semantic_reflow,
                    "_collect_cjk_inline_math_source_regions",
                    return_value={
                        "regions": [],
                        "missing": [],
                        "binding_diagnostics": [],
                        "appendix_anchor_count": 0,
                    },
                ),
                mock.patch.object(
                    semantic_reflow,
                    "_normalize_legacy_formula_surfaces",
                    return_value={"applied": False},
                ),
                mock.patch.object(
                    semantic_reflow,
                    "_bind_cjk_picture_source_assets",
                    return_value=picture_failure,
                ),
            ):
                result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    output_dir / "input.pdf",
                    metadata,
                    status,
                )

            self.assertFalse(result["ok"])
            self.assertFalse(status["ok"])
            self.assertEqual(status["success_class"], "degraded_failure")
            self.assertIn(
                "cjk_picture_source_failure:picture_surface_size_limit_exceeded",
                status["warnings"],
            )

    def test_rebuild_cjk_path_passes_manifest_to_picture_binder(self) -> None:
        document = _picture_document(1)

        class CJKSource:
            def __init__(self, _input_file: Path) -> None:
                pass

            def language_profile(self, *, page_limit: int = 3) -> dict[str, int]:
                del page_limit
                return {"cjk_characters": 200, "latin_characters": 100}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            _write_surfaces(output_dir, document, markdown_count=1)
            status = {
                "ok": True,
                "success_class": "success",
                "warnings": [],
                "quality_signals": {},
            }
            metadata = _picture_metadata([document])
            with (
                mock.patch.object(semantic_reflow, "SourceReader", CJKSource),
                mock.patch.object(
                    semantic_reflow,
                    "_remove_review_evidence_from_primary_surfaces",
                    return_value={"removed": 0},
                ),
                mock.patch.object(
                    semantic_reflow,
                    "_collect_cjk_inline_math_source_regions",
                    return_value={
                        "regions": [],
                        "missing": [],
                        "binding_diagnostics": [],
                        "appendix_anchor_count": 0,
                    },
                ),
                mock.patch.object(
                    semantic_reflow,
                    "_normalize_legacy_formula_surfaces",
                    return_value={"applied": False},
                ),
            ):
                result = semantic_reflow.rebuild_semantic_surfaces(
                    output_dir,
                    document,
                    output_dir / "input.pdf",
                    metadata,
                    status,
                )

            self.assertTrue(result["ok"])
            binding = result["picture_source_binding"]
            self.assertTrue(binding["ok"])
            self.assertEqual(binding["fully_bound_count"], 1)
            self.assertIn(
                'src="pictures/picture_1.png"',
                (output_dir / "document.html").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "![Source picture 1](pictures/picture_1.png)",
                (output_dir / "document.md").read_text(encoding="utf-8"),
            )

    def test_multi_chunk_local_refs_use_chunk_qualified_markers(self) -> None:
        first = _picture_document(1)
        second = _picture_document(1)
        first["texts"][0]["text"] = "Figure A: first chunk"
        second["texts"][0]["text"] = "Figure B: second chunk"
        first["pictures"][0]["_local_ai_lab_chunk_part_index"] = 0
        second["pictures"][0]["_local_ai_lab_chunk_part_index"] = 1
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 2)
            _write_surfaces(output_dir, first, markdown_count=1)
            second_html = (
                output_dir / "document.html"
            ).read_text(encoding="utf-8")
            second_markdown = (
                output_dir / "document.md"
            ).read_text(encoding="utf-8")
            caption_b = second["texts"][0]["text"]
            second_html = second_html.replace(
                "</body>",
                f'<figure><figcaption>{caption_b}</figcaption>'
                '<img src="data:image/png;base64,AAAA"></figure></body>',
            )
            second_markdown += caption_b + "\n"
            (output_dir / "document.html").write_text(second_html, encoding="utf-8")
            (output_dir / "document.md").write_text(second_markdown, encoding="utf-8")

            result = _bind(output_dir, [first, second])

            self.assertTrue(result["ok"])
            self.assertEqual(result["fully_bound_count"], 2)
            self.assertEqual(
                [record["source_ref"] for record in result["records"]],
                ["chunk:0:#/pictures/0", "chunk:1:#/pictures/0"],
            )
            html_text = (output_dir / "document.html").read_text(encoding="utf-8")
            markdown_text = (output_dir / "document.md").read_text(encoding="utf-8")
            self.assertIn('data-source-ref="chunk:0:#/pictures/0"', html_text)
            self.assertIn('data-source-ref="chunk:1:#/pictures/0"', html_text)
            self.assertIn("source-picture-ref:chunk:0:#/pictures/0", markdown_text)
            self.assertIn("source-picture-ref:chunk:1:#/pictures/0", markdown_text)

    def test_non_string_or_invalid_chunk_part_fails_closed(self) -> None:
        invalid_values = (123, b"#/pictures/0", True, -1, 1.5, "1", None)
        for self_ref, part_index in ((123, 0), ("#/pictures/0", "123")):
            with self.subTest(self_ref=self_ref, part_index=part_index):
                document = _picture_document(1)
                document["pictures"][0]["self_ref"] = self_ref
                document["pictures"][0]["_local_ai_lab_chunk_part_index"] = part_index
                with tempfile.TemporaryDirectory() as directory:
                    output_dir = Path(directory)
                    _write_crops(output_dir, 1)
                    _write_surfaces(output_dir, document, markdown_count=1)
                    before_html = (output_dir / "document.html").read_bytes()
                    before_markdown = (output_dir / "document.md").read_bytes()

                    result = _bind(output_dir, [document])

                    self.assertFalse(result["ok"])
                    self.assertTrue(
                        {
                            "picture_self_ref_missing_or_invalid",
                            "picture_manifest_identity_mismatch",
                        }
                        & set(result["failure_reasons"])
                    )
                    self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
                    self.assertEqual(
                        before_markdown,
                        (output_dir / "document.md").read_bytes(),
                    )
        for part_index in invalid_values[2:]:
            with self.subTest(part_index=repr(part_index)):
                document = _picture_document(1)
                document["pictures"][0]["_local_ai_lab_chunk_part_index"] = part_index
                with tempfile.TemporaryDirectory() as directory:
                    output_dir = Path(directory)
                    _write_crops(output_dir, 1)
                    _write_surfaces(output_dir, document, markdown_count=1)
                    result = _bind(output_dir, [document])
                    self.assertFalse(result["ok"])
                    self.assertTrue(
                        {
                            "picture_self_ref_missing_or_invalid",
                            "picture_manifest_identity_mismatch",
                        }
                        & set(result["failure_reasons"])
                    )

    def test_commented_html_figure_is_not_a_candidate(self) -> None:
        document = _picture_document(1)
        caption = document["texts"][0]["text"]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            (output_dir / "document.html").write_text(
                "<!-- "
                f'<figure><figcaption>{caption}</figcaption>'
                '<img src="data:image/png;base64,AAAA"></figure>'
                " -->\n",
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(caption + "\n", encoding="utf-8")

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["html_bound_count"], 0)
            self.assertEqual(result["markdown_bound_count"], 1)
            self.assertIn(
                "picture_html_caption_candidate_missing",
                result["failure_reasons"],
            )
            self.assertNotIn(
                "data-source-ref",
                (output_dir / "document.html").read_text(encoding="utf-8"),
            )

    def test_html_candidate_flood_fails_closed(self) -> None:
        document = _picture_document(1)
        caption = document["texts"][0]["text"]
        commented_figure = (
            "<!-- <figure><figcaption>ignored</figcaption>"
            '<img src="data:image/png;base64,AAAA"></figure> -->\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            (output_dir / "document.html").write_text(
                commented_figure * (semantic_reflow._CJK_PICTURE_MAX_HTML_FIGURES + 1),
                encoding="utf-8",
            )
            (output_dir / "document.md").write_text(caption + "\n", encoding="utf-8")
            before_html = (output_dir / "document.html").read_bytes()
            before_markdown = (output_dir / "document.md").read_bytes()

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["records"], [])
            self.assertIn(
                "picture_html_candidate_limit_exceeded",
                result["failure_reasons"],
            )
            self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
            self.assertEqual(
                before_markdown,
                (output_dir / "document.md").read_bytes(),
            )

    def test_manifest_missing_with_picture_nodes_fails_closed(self) -> None:
        document = _picture_document(1)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            _write_surfaces(output_dir, document, markdown_count=1)

            result = semantic_reflow._bind_cjk_picture_source_assets(
                output_dir,
                [document],
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["records"], [])
            self.assertIn(
                "picture_manifest_missing_or_invalid",
                result["failure_reasons"],
            )

    def test_nonexpected_pictures_are_ignored(self) -> None:
        document = _picture_document(2)
        document["pictures"][0]["parent"] = {"$ref": "#/formulas/0"}
        document["pictures"][1]["content_layer"] = "furniture"
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 2)
            _write_surfaces(output_dir, document, markdown_count=2)
            before_html = (output_dir / "document.html").read_bytes()
            before_markdown = (output_dir / "document.md").read_bytes()
            metadata = _picture_metadata([document], expected_flags=[False, False])

            result = semantic_reflow._bind_cjk_picture_source_assets(
                output_dir,
                [document],
                metadata,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["records"], [])
            self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
            self.assertEqual(
                before_markdown,
                (output_dir / "document.md").read_bytes(),
            )

    def test_expected_captionless_picture_fails_closed(self) -> None:
        document = _picture_document(1)
        document["pictures"][0].pop("captions")
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            _write_surfaces(output_dir, document, markdown_count=1)
            before_html = (output_dir / "document.html").read_bytes()
            before_markdown = (output_dir / "document.md").read_bytes()

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertEqual(result["fully_bound_count"], 0)
            self.assertIn(
                "picture_caption_ref_missing_or_ambiguous",
                result["failure_reasons"],
            )
            self.assertEqual(before_html, (output_dir / "document.html").read_bytes())
            self.assertEqual(
                before_markdown,
                (output_dir / "document.md").read_bytes(),
            )

    def test_surface_symlink_or_nonregular_is_rejected_before_read(self) -> None:
        document = _picture_document(1)
        for surface_name in ("document.html", "document.md"):
            for surface_kind in ("symlink", "directory"):
                with self.subTest(surface_name=surface_name, surface_kind=surface_kind):
                    with tempfile.TemporaryDirectory() as directory:
                        output_dir = Path(directory)
                        _write_crops(output_dir, 1)
                        _write_surfaces(output_dir, document, markdown_count=1)
                        surface_path = output_dir / surface_name
                        surface_path.unlink()
                        if surface_kind == "symlink":
                            outside = output_dir / "outside.txt"
                            outside.write_text("caption", encoding="utf-8")
                            surface_path.symlink_to(outside)
                        else:
                            surface_path.mkdir()

                        result = _bind(output_dir, [document])

                        self.assertFalse(result["ok"])
                        self.assertIn(
                            "picture_surface_not_regular",
                            result["failure_reasons"],
                        )

    def test_surface_byte_limit_is_checked_before_read(self) -> None:
        document = _picture_document(1)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _write_crops(output_dir, 1)
            _write_surfaces(output_dir, document, markdown_count=1)
            with (output_dir / "document.html").open("wb") as handle:
                handle.truncate(semantic_reflow._CJK_PICTURE_MAX_SURFACE_CHARS + 1)

            result = _bind(output_dir, [document])

            self.assertFalse(result["ok"])
            self.assertIn(
                "picture_surface_size_limit_exceeded",
                result["failure_reasons"],
            )


if __name__ == "__main__":
    unittest.main()
