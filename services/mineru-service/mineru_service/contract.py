"""Local AI Lab contract writer for MinerU VLM+MLX outputs."""

from __future__ import annotations

import html
import json
import re
import resource
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evaluation import LAYOUT_IMAGE_SIZE, blocks_to_markdown, prepare_mlx_compat_model_dir, render_pdf_page
from .model_registry import LocalModel


LOCAL_REF_RE = re.compile(r"""(?:src|href)=["']([^"']+)["']""", re.IGNORECASE)


@dataclass
class MinerUContractResult:
    output_dir: Path
    metadata: dict[str, Any]
    status: dict[str, Any]


@dataclass
class OutputRegistry:
    root: Path
    outputs_written: list[str] = field(default_factory=list)

    def register(self, path: Path) -> None:
        self.outputs_written.append(path.relative_to(self.root).as_posix())


def parse_page_range(page_range: str | None, page_count: int) -> list[int]:
    if not page_range:
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for part in page_range.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            pages.update(range(start, end + 1))
        else:
            pages.add(int(token))
    return [page for page in sorted(pages) if 1 <= page <= page_count]


def get_pdf_page_count(pdf_path: Path) -> int:
    import fitz

    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def normalize_bbox(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        keys = ("x0", "y0", "x1", "y1")
        if all(key in value for key in keys):
            return [float(value[key]) for key in keys]
        keys = ("l", "t", "r", "b")
        if all(key in value for key in keys):
            return [float(value[key]) for key in keys]
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]
    for names in (("x0", "y0", "x1", "y1"), ("left", "top", "right", "bottom")):
        if all(hasattr(value, name) for name in names):
            return [float(getattr(value, name)) for name in names]
    return None


def clamp_bbox(bbox: list[float], width: int, height: int, padding: int) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = bbox
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    left = max(0, int(x0) - padding)
    top = max(0, int(y0) - padding)
    right = min(width, int(x1) + padding)
    bottom = min(height, int(y1) + padding)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def block_to_content_item(block: Any, page_number: int, index: int) -> dict[str, Any]:
    block_type = str(getattr(block, "type", "") or "unknown")
    content = getattr(block, "content", None)
    bbox = normalize_bbox(getattr(block, "bbox", None))
    item: dict[str, Any] = {
        "page_number": page_number,
        "index": index,
        "type": block_type,
        "bbox": bbox,
        "content": content,
        "assets": {},
    }
    if block_type in {"text", "equation"}:
        item["text"] = content
    if block_type == "table":
        item["html"] = content
    return item


def write_json(path: Path, payload: Any, registry: OutputRegistry | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if registry:
        registry.register(path)


def write_text(path: Path, text: str, registry: OutputRegistry | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if registry:
        registry.register(path)


def crop_block_asset(
    *,
    image: Any,
    bbox: list[float] | None,
    output_path: Path,
    padding: int,
    registry: OutputRegistry,
) -> str | None:
    if bbox is None:
        return None
    clamped = clamp_bbox(bbox, image.width, image.height, padding)
    if clamped is None:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.crop(clamped).save(output_path)
    registry.register(output_path)
    return output_path.relative_to(registry.root).as_posix()


def render_contract_html(*, title: str, content_items: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.55;margin:0;background:#f7f7f4;color:#202124}",
        "main{max-width:1040px;margin:0 auto;padding:24px}",
        "section.page{background:#fff;border:1px solid #d8d6ce;margin:0 0 18px;padding:18px}",
        ".block{margin:0 0 14px}.meta{color:#68645d;font-size:13px}.formula,.table-wrap{overflow:auto;background:#fbfaf7;border:1px solid #ddd8cc;padding:10px}",
        "img.review{max-width:100%;height:auto;border:1px solid #d8d6ce;background:white}.asset-links a{margin-right:12px}",
        "table{border-collapse:collapse}td,th{border:1px solid #b8b8b8;padding:4px 6px;vertical-align:top}",
        "</style>",
        "</head>",
        "<body><main>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p class=\"meta\">Parser: {html.escape(str(metadata.get('parser')))}; backend: {html.escape(str(metadata.get('backend')))}; pages: {metadata.get('processed_page_count')}/{metadata.get('page_count')}</p>",
    ]
    current_page: int | None = None
    for item in content_items:
        page_number = int(item.get("page_number") or 0)
        if page_number != current_page:
            if current_page is not None:
                parts.append("</section>")
            current_page = page_number
            page_image = (item.get("assets") or {}).get("page_image")
            page_link = f' <a href="{html.escape(str(page_image))}">page image</a>' if page_image else ""
            parts.append(f'<section class="page" id="page-{page_number}"><h2>Page {page_number}{page_link}</h2>')

        block_type = str(item.get("type") or "unknown")
        content = item.get("content") or ""
        assets = item.get("assets") or {}
        parts.append(f'<div class="block {html.escape(block_type)}">')
        if block_type == "equation":
            parts.append(f'<div class="formula"><pre>{html.escape(str(content))}</pre></div>')
        elif block_type == "table":
            parts.append('<div class="table-wrap">')
            text = str(content)
            if "<table" in text.lower():
                parts.append(text)
            else:
                parts.append(f"<pre>{html.escape(text)}</pre>")
            parts.append("</div>")
        elif block_type == "image":
            thumb = assets.get("crop") or assets.get("context")
            if thumb:
                parts.append(f'<img class="review" src="{html.escape(str(thumb))}" alt="image region">')
            else:
                parts.append("<p>[image region detected]</p>")
        elif content:
            parts.append(f"<p>{html.escape(str(content))}</p>")
        else:
            parts.append(f"<p>[{html.escape(block_type)} region]</p>")

        links = []
        for label, ref in assets.items():
            if label == "page_image":
                continue
            links.append(f'<a href="{html.escape(str(ref))}">{html.escape(str(label).replace("_", " "))}</a>')
        if links:
            parts.append(f'<p class="asset-links">{" ".join(links)}</p>')
        parts.append("</div>")

    if current_page is not None:
        parts.append("</section>")
    parts.extend(["</main></body></html>"])
    return "\n".join(parts)


def find_broken_local_refs(html_path: Path) -> list[str]:
    text = html_path.read_text(encoding="utf-8")
    broken: list[str] = []
    for ref in LOCAL_REF_RE.findall(text):
        if ref.startswith(("http://", "https://", "data:", "mailto:", "#")):
            continue
        if not (html_path.parent / ref).exists():
            broken.append(ref)
    return broken


def convert_pdf_to_contract(
    *,
    pdf_path: Path,
    output_dir: Path,
    model: LocalModel,
    page_range: str | None = None,
    render_zoom: float = 2.0,
) -> MinerUContractResult:
    from mineru_vl_utils import MinerUClient
    from mlx_vlm import load as mlx_load

    started = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)
    registry = OutputRegistry(output_dir)
    stem = pdf_path.stem
    official_dir = output_dir / "official" / stem / "vlm"
    official_images = official_dir / "images"
    assets_dir = output_dir / "assets"
    pages_dir = assets_dir / "pages"
    formula_dir = assets_dir / "formulas"
    table_dir = assets_dir / "tables"
    image_dir = assets_dir / "images"

    page_count = get_pdf_page_count(pdf_path)
    pages = parse_page_range(page_range, page_count)
    warnings: list[str] = []
    if not pages:
        warnings.append("No pages selected for processing.")

    prepared_model_path = prepare_mlx_compat_model_dir(model.local_path)
    mlx_model, processor = mlx_load(str(prepared_model_path))
    client = MinerUClient(
        backend="mlx-engine",
        model=mlx_model,
        processor=processor,
        layout_image_size=LAYOUT_IMAGE_SIZE,
        use_tqdm=False,
    )

    all_items: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    counts = {"equation": 0, "table": 0, "image": 0, "text": 0}

    for page_number in pages:
        page_started = time.time()
        image = render_pdf_page(pdf_path, page_number, zoom=render_zoom)
        page_image = pages_dir / f"page_{page_number:03d}.png"
        page_image.parent.mkdir(parents=True, exist_ok=True)
        image.save(page_image)
        registry.register(page_image)

        blocks = list(client.two_step_extract(image))
        page_items: list[dict[str, Any]] = []
        for index, block in enumerate(blocks, start=1):
            item = block_to_content_item(block, page_number, index)
            block_type = item["type"]
            if block_type in counts:
                counts[block_type] += 1
            bbox = item.get("bbox")
            if block_type == "equation":
                counts["equation"] += 0 if block_type in counts else 1
                crop = crop_block_asset(
                    image=image,
                    bbox=bbox,
                    output_path=formula_dir / f"page_{page_number:03d}_formula_{index:03d}.png",
                    padding=24,
                    registry=registry,
                )
                context = crop_block_asset(
                    image=image,
                    bbox=bbox,
                    output_path=formula_dir / f"page_{page_number:03d}_formula_{index:03d}_context.png",
                    padding=96,
                    registry=registry,
                )
                if crop:
                    item["assets"]["source_image"] = crop
                if context:
                    item["assets"]["context_crop"] = context
            elif block_type == "table":
                crop = crop_block_asset(
                    image=image,
                    bbox=bbox,
                    output_path=table_dir / f"page_{page_number:03d}_table_{index:03d}.png",
                    padding=40,
                    registry=registry,
                )
                if crop:
                    item["assets"]["table_crop"] = crop
            elif block_type == "image":
                crop = crop_block_asset(
                    image=image,
                    bbox=bbox,
                    output_path=image_dir / f"page_{page_number:03d}_image_{index:03d}.png",
                    padding=24,
                    registry=registry,
                )
                if crop:
                    item["assets"]["crop"] = crop
            item["assets"]["page_image"] = page_image.relative_to(output_dir).as_posix()
            page_items.append(item)

        all_items.extend(page_items)
        page_summaries.append(
            {
                "page_number": page_number,
                "wall_seconds": round(time.time() - page_started, 3),
                "image_size": [image.width, image.height],
                "layout_image_size": list(LAYOUT_IMAGE_SIZE),
                "block_count": len(page_items),
                "formula_count": sum(1 for item in page_items if item["type"] == "equation"),
                "table_count": sum(1 for item in page_items if item["type"] == "table"),
                "image_count": sum(1 for item in page_items if item["type"] == "image"),
            }
        )

    markdown = "\n\n".join(
        [f"# {stem}", ""]
        + [
            f"## Page {page_number}\n\n"
            + blocks_to_markdown(
                [
                    type("BlockView", (), {"type": item["type"], "content": item.get("content")})()
                    for item in all_items
                    if item["page_number"] == page_number
                ]
            )
            for page_number in pages
        ]
    ).strip() + "\n"

    write_text(official_dir / f"{stem}.md", markdown, registry)
    write_json(official_dir / f"{stem}_content_list_v2.json", all_items, registry)
    write_json(official_dir / f"{stem}_content_list.json", all_items, registry)
    write_json(
        official_dir / f"{stem}_middle.json",
        {"pages": page_summaries, "content_source": f"{stem}_content_list_v2.json"},
        registry,
    )
    write_json(
        official_dir / f"{stem}_model.json",
        {"model": model.to_dict(), "backend": "local_vlm_mlx", "layout_image_size": list(LAYOUT_IMAGE_SIZE)},
        registry,
    )
    official_images.mkdir(parents=True, exist_ok=True)
    for asset_path in sorted(assets_dir.rglob("*.png")):
        target = official_images / asset_path.name
        if not target.exists():
            shutil.copy2(asset_path, target)
            registry.register(target)

    write_text(output_dir / "document.md", markdown, registry)
    write_json(
        output_dir / "document.json",
        {
            "source": f"official/{stem}/vlm/{stem}_content_list_v2.json",
            "pages": page_summaries,
            "content": all_items,
        },
        registry,
    )

    metadata = {
        "parser": "mineru",
        "parser_policy": "local_vlm_mlx_review_contract",
        "backend": "local_vlm_mlx",
        "pipeline_backend_enabled": False,
        "hybrid_backend_enabled": False,
        "exo_enabled": False,
        "model_id": model.model_id,
        "model_local_path": str(model.local_path),
        "quantization": model.quantization,
        "page_count": page_count,
        "processed_page_count": len(pages),
        "processed_pages": pages,
        "layout_image_size": list(LAYOUT_IMAGE_SIZE),
        "content_pass_image_policy": f"rendered page images at zoom {render_zoom}; MinerU content crops use natural rendered dimensions",
        "runtime_seconds": round(time.time() - started, 3),
        "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "formula_count": counts["equation"],
        "table_count": counts["table"],
        "image_region_count": counts["image"],
        "text_block_count": counts["text"],
        "asset_count": len(list(assets_dir.rglob("*.png"))) if assets_dir.exists() else 0,
        "official_artifact_paths_used": [
            f"official/{stem}/vlm/{stem}.md",
            f"official/{stem}/vlm/{stem}_content_list_v2.json",
            f"official/{stem}/vlm/{stem}_content_list.json",
            f"official/{stem}/vlm/{stem}_middle.json",
            f"official/{stem}/vlm/{stem}_model.json",
            f"official/{stem}/vlm/images/",
        ],
        "known_limitations": [
            "This is a first multi-page review wrapper around MinerU VLM+MLX blocks, not a production parser.",
            "Image/table/formula crops depend on MinerU block bounding boxes being present and in rendered-page coordinates.",
            "Inline formulas embedded in text may not have separate crop assets when MinerU does not emit a formula block.",
        ],
        "warnings": warnings,
    }
    html_text = render_contract_html(title=stem, content_items=all_items, metadata=metadata)
    write_text(output_dir / "document.html", html_text, registry)
    broken_refs = find_broken_local_refs(output_dir / "document.html")
    metadata["broken_local_refs_count"] = len(broken_refs)
    final_outputs = sorted(set(registry.outputs_written + ["metadata.json", "status.json"]))
    metadata["generated_outputs"] = final_outputs
    metadata["outputs_written"] = final_outputs
    write_json(output_dir / "metadata.json", metadata, registry)
    status = {
        "ok": len(pages) > 0 and len(broken_refs) == 0,
        "parser": "mineru",
        "backend": "local_vlm_mlx",
        "warnings": warnings + ([f"Broken local HTML refs: {broken_refs}"] if broken_refs else []),
        "broken_local_refs": broken_refs,
        "outputs_written": final_outputs,
        "official_artifacts_wrapped": True,
    }
    write_json(output_dir / "status.json", status, registry)
    return MinerUContractResult(output_dir=output_dir, metadata=metadata, status=status)
