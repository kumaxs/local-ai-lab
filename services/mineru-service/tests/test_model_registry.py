from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
