"""Inspection helpers for official MinerU output directories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MARKDOWN_REF_RE = re.compile(r"""!\[[^\]]*\]\(([^)]+)\)|\[[^\]]+\]\(([^)]+)\)""")


@dataclass(frozen=True)
class OfficialOutputSummary:
    root: Path
    markdown_count: int
    content_list_count: int
    middle_json_count: int
    model_json_count: int
    image_count: int
    non_empty_image_count: int
    broken_markdown_refs: tuple[str, ...]

    @property
    def has_core_artifacts(self) -> bool:
        return (
            self.markdown_count > 0
            and self.content_list_count > 0
            and self.middle_json_count > 0
            and self.model_json_count > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "markdown_count": self.markdown_count,
            "content_list_count": self.content_list_count,
            "middle_json_count": self.middle_json_count,
            "model_json_count": self.model_json_count,
            "image_count": self.image_count,
            "non_empty_image_count": self.non_empty_image_count,
            "broken_markdown_refs": list(self.broken_markdown_refs),
            "has_core_artifacts": self.has_core_artifacts,
        }


def _is_local_ref(ref: str) -> bool:
    return not ref.startswith(("http://", "https://", "mailto:", "#", "data:"))


def find_broken_markdown_refs(markdown_path: Path) -> list[str]:
    text = markdown_path.read_text(encoding="utf-8", errors="replace")
    broken: list[str] = []
    for match in MARKDOWN_REF_RE.findall(text):
        ref = match[0] or match[1]
        if _is_local_ref(ref) and not (markdown_path.parent / ref).exists():
            broken.append(ref)
    return broken


def summarize_official_output(root: Path) -> OfficialOutputSummary:
    markdown_files = list(root.rglob("*.md"))
    content_list_files = list(root.rglob("*content_list*.json"))
    middle_json_files = list(root.rglob("*middle*.json"))
    model_json_files = list(root.rglob("*model*.json"))
    image_files = list(root.rglob("*.jpg")) + list(root.rglob("*.png"))
    broken_refs: list[str] = []
    for markdown_path in markdown_files:
        broken_refs.extend(find_broken_markdown_refs(markdown_path))
    return OfficialOutputSummary(
        root=root,
        markdown_count=len(markdown_files),
        content_list_count=len(content_list_files),
        middle_json_count=len(middle_json_files),
        model_json_count=len(model_json_files),
        image_count=len(image_files),
        non_empty_image_count=sum(1 for path in image_files if path.stat().st_size > 0),
        broken_markdown_refs=tuple(broken_refs),
    )
