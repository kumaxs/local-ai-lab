"""Small, local MinerU VLM+MLX evaluation helpers."""

from __future__ import annotations

import json
import os
import resource
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LAYOUT_IMAGE_SIZE = (1036, 1036)


@dataclass(frozen=True)
class PageProbeResult:
    sample_name: str
    page_number: int
    model_path: str
    wall_seconds: float
    max_rss_kb: int
    block_count: int
    equation_count: int
    table_count: int
    image_count: int
    text_count: int
    markdown_preview: str
    raw_blocks: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_name": self.sample_name,
            "page_number": self.page_number,
            "model_path": self.model_path,
            "wall_seconds": self.wall_seconds,
            "max_rss_kb": self.max_rss_kb,
            "block_count": self.block_count,
            "equation_count": self.equation_count,
            "table_count": self.table_count,
            "image_count": self.image_count,
            "text_count": self.text_count,
            "markdown_preview": self.markdown_preview,
            "raw_blocks": self.raw_blocks,
        }


def render_pdf_page(pdf_path: Path, page_number: int, zoom: float = 2.0):
    import fitz

    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_number - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        from PIL import Image

        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def block_to_dict(block: Any) -> dict[str, Any]:
    return {
        "type": getattr(block, "type", None),
        "bbox": getattr(block, "bbox", None),
        "angle": getattr(block, "angle", None),
        "content": getattr(block, "content", None),
    }


def blocks_to_markdown(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        block_type = str(getattr(block, "type", "") or "")
        content = getattr(block, "content", None)
        if block_type == "equation" and content:
            parts.append(f"$$\n{content}\n$$")
        elif block_type == "table" and content:
            parts.append(str(content))
        elif block_type == "image":
            parts.append("[image region]")
        elif content:
            parts.append(str(content))
    return "\n\n".join(parts)


def prepare_mlx_compat_model_dir(model_path: Path) -> Path:
    """Create a small temporary compatibility view for the current MLX model export.

    The tested bf16 export needs the same tied-embedding config flattening used by
    mineru-vl-utils, and its processor_config advertises Qwen3 image/video
    processors while the model itself is Qwen2-VL. Keeping this as a temp view
    avoids mutating the model cache.
    """
    config_path = model_path / "config.json"
    processor_path = model_path / "processor_config.json"
    if not config_path.exists():
        return model_path

    compat_dir = Path(tempfile.mkdtemp(prefix="mineru-service-mlx-compat-"))
    for child in model_path.iterdir():
        if child.name in {"config.json", "processor_config.json"}:
            continue
        os.symlink(child, compat_dir / child.name, target_is_directory=child.is_dir())

    config = json.loads(config_path.read_text(encoding="utf-8"))
    text_config = config.get("text_config")
    if isinstance(text_config, dict):
        for key, value in text_config.items():
            config[key] = value
    (compat_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    if processor_path.exists():
        processor_config = json.loads(processor_path.read_text(encoding="utf-8"))
        image_processor = processor_config.get("image_processor")
        if isinstance(image_processor, dict) and image_processor.get("image_processor_type") == "Qwen3VLImageProcessor":
            image_processor["image_processor_type"] = "Qwen2VLImageProcessor"
        video_processor = processor_config.get("video_processor")
        if isinstance(video_processor, dict) and video_processor.get("video_processor_type") == "Qwen3VLVideoProcessor":
            video_processor["video_processor_type"] = "Qwen2VLVideoProcessor"
        (compat_dir / "processor_config.json").write_text(
            json.dumps(processor_config, indent=2),
            encoding="utf-8",
        )

    return compat_dir


def run_page_probe(*, pdf_path: Path, page_number: int, model_path: Path) -> PageProbeResult:
    from mineru_vl_utils import MinerUClient
    from mlx_vlm import load as mlx_load

    started = time.time()
    image = render_pdf_page(pdf_path, page_number)
    prepared_model_path = prepare_mlx_compat_model_dir(model_path)
    model, processor = mlx_load(str(prepared_model_path))
    client = MinerUClient(
        backend="mlx-engine",
        model=model,
        processor=processor,
        layout_image_size=LAYOUT_IMAGE_SIZE,
        use_tqdm=False,
    )
    result = client.two_step_extract(image)
    blocks = list(result)
    markdown = blocks_to_markdown(blocks)
    counts: dict[str, int] = {}
    for block in blocks:
        block_type = str(getattr(block, "type", "") or "")
        counts[block_type] = counts.get(block_type, 0) + 1
    return PageProbeResult(
        sample_name=pdf_path.name,
        page_number=page_number,
        model_path=str(model_path),
        wall_seconds=round(time.time() - started, 3),
        max_rss_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        block_count=len(blocks),
        equation_count=counts.get("equation", 0),
        table_count=counts.get("table", 0),
        image_count=counts.get("image", 0),
        text_count=counts.get("text", 0),
        markdown_preview=markdown[:4000],
        raw_blocks=[block_to_dict(block) for block in blocks],
    )


def write_probe_result(path: Path, result: PageProbeResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
