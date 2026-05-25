from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mineru_service.contract import find_broken_local_refs, parse_page_range, render_contract_html
from mineru_service.evaluation import LAYOUT_IMAGE_SIZE, blocks_to_markdown
from mineru_service.model_registry import default_models, registry_snapshot


class FakeBlock:
    def __init__(self, block_type: str, content: str | None) -> None:
        self.type = block_type
        self.content = content


class MinerUServiceTests(unittest.TestCase):
    def test_registry_reports_missing_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = registry_snapshot(Path(tmpdir))
        self.assertEqual(len(snapshot), 2)
        self.assertFalse(snapshot[0]["present"])
        self.assertIn("source_repo", snapshot[0])

    def test_registry_health_check_detects_present_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = default_models(Path(tmpdir))[0]
            model.local_path.mkdir(parents=True)
            for name in model.health_check_files:
                (model.local_path / name).write_text("{}", encoding="utf-8")
            self.assertTrue(model.present)
            self.assertGreaterEqual(model.to_dict()["disk_size_bytes"], 8)

    def test_layout_protocol_constant(self) -> None:
        self.assertEqual(LAYOUT_IMAGE_SIZE, (1036, 1036))

    def test_blocks_to_markdown_preserves_equations_and_tables(self) -> None:
        markdown = blocks_to_markdown(
            [
                FakeBlock("text", "hello"),
                FakeBlock("equation", "E=mc^2"),
                FakeBlock("table", "<table><tr><td>x</td></tr></table>"),
                FakeBlock("image", None),
            ]
        )
        self.assertIn("hello", markdown)
        self.assertIn("$$\nE=mc^2\n$$", markdown)
        self.assertIn("<table>", markdown)
        self.assertIn("[image region]", markdown)

    def test_page_range_parser_bounds_pages(self) -> None:
        self.assertEqual(parse_page_range("1,3-5,99", 5), [1, 3, 4, 5])
        self.assertEqual(parse_page_range(None, 3), [1, 2, 3])

    def test_contract_html_uses_valid_relative_asset_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asset = root / "assets" / "formulas" / "formula_1.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"png")
            html_path = root / "document.html"
            html_path.write_text(
                render_contract_html(
                    title="sample",
                    metadata={"parser": "mineru", "backend": "local_vlm_mlx", "processed_page_count": 1, "page_count": 1},
                    content_items=[
                        {
                            "page_number": 1,
                            "type": "equation",
                            "content": "E=mc^2",
                            "assets": {"source_image": "assets/formulas/formula_1.png"},
                        }
                    ],
                ),
                encoding="utf-8",
            )
            self.assertEqual(find_broken_local_refs(html_path), [])

    def test_contract_html_detects_broken_relative_asset_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = Path(tmpdir) / "document.html"
            html_path.write_text('<a href="assets/missing.png">missing</a>', encoding="utf-8")
            self.assertEqual(find_broken_local_refs(html_path), ["assets/missing.png"])

    def test_registry_never_selects_pipeline_hybrid_or_exo(self) -> None:
        snapshot = registry_snapshot(Path("/missing"))
        self.assertIn("mlx", snapshot[0]["expected_runtime"].lower())
        self.assertNotIn("exo", snapshot[0]["expected_runtime"].lower())
        self.assertNotIn("pipeline", snapshot[0]["expected_runtime"].lower())


if __name__ == "__main__":
    unittest.main()
