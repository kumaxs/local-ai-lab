"""Artifact writing helpers for the docling-service skeleton."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contract import REQUIRED_SUCCESS_OUTPUTS, STATUS_SUCCESS
from .quality import CONVERSION_POLICY, count_tables, extract_table_dicts, relative_output


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def resolve_output_root(output_root: str | Path | None = None) -> Path:
    if output_root is None:
        return Path.cwd() / "artifacts" / "docling-service"
    return Path(output_root).expanduser().resolve()


def output_dir_for_job(job_uuid: str, output_root: str | Path | None = None) -> Path:
    return resolve_output_root(output_root) / job_uuid


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_metadata(
    *,
    job_uuid: str,
    input_file_path: str | Path,
    input_sha256: str,
    image_export_mode: str,
    requested_outputs: list[str] | None,
    generated_outputs: list[str],
    display_name: str | None = None,
    original_name: str | None = None,
    source_name: str | None = None,
    detected_format: str | None = "pdf",
    page_count: int | None = None,
    docling_version: str | None = None,
    link_count: int | None = 0,
    table_count: int | None = 0,
    asset_count: int | None = 0,
    conversion_policy: str | None = None,
    ocr_fallback_used: bool | None = None,
    text_quality_gxx_count: int | None = None,
    text_quality_gxx_density: float | None = None,
    table_artifact_count: int | None = None,
    table_image_count: int | None = None,
    formula_count: int | None = None,
    formula_placeholder_count: int | None = None,
    formula_asset_count: int | None = None,
    formula_context_asset_count: int | None = None,
    formula_placeholder_link_count: int | None = None,
    formula_source_link_count: int | None = None,
    formula_enrichment_enabled: bool | None = None,
    formula_model: str | None = None,
) -> dict[str, Any]:
    path = Path(input_file_path)
    stat = path.stat()
    return {
        "job_uuid": job_uuid,
        "display_name": display_name,
        "original_name": original_name,
        "source_name": source_name,
        "input_file_path": str(path.resolve()),
        "input_sha256": input_sha256,
        "file_size_bytes": stat.st_size,
        "input_mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "detected_format": detected_format,
        "page_count": page_count,
        "docling_version": docling_version,
        "image_export_mode": image_export_mode,
        "requested_outputs": requested_outputs or REQUIRED_SUCCESS_OUTPUTS.copy(),
        "generated_outputs": generated_outputs,
        "link_count": link_count,
        "table_count": table_count,
        "asset_count": asset_count,
        "conversion_policy": conversion_policy,
        "ocr_fallback_used": ocr_fallback_used,
        "text_quality_gxx_count": text_quality_gxx_count,
        "text_quality_gxx_density": text_quality_gxx_density,
        "table_artifact_count": table_artifact_count,
        "table_image_count": table_image_count,
        "formula_count": formula_count,
        "formula_placeholder_count": formula_placeholder_count,
        "formula_asset_count": formula_asset_count,
        "formula_context_asset_count": formula_context_asset_count,
        "formula_placeholder_link_count": formula_placeholder_link_count,
        "formula_source_link_count": formula_source_link_count,
        "formula_enrichment_enabled": formula_enrichment_enabled,
        "formula_model": formula_model,
    }


def build_status(
    *,
    job_uuid: str,
    status: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    input_file_path: str | Path | None,
    input_sha256: str | None,
    output_dir: str | Path | None,
    outputs_written: list[str] | None = None,
    warnings: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    conversion_policy: str | None = None,
    ocr_fallback_used: bool | None = None,
    text_quality_gxx_count: int | None = None,
    text_quality_gxx_density: float | None = None,
    table_count: int | None = None,
    asset_count: int | None = None,
    generated_outputs: list[str] | None = None,
    table_artifact_count: int | None = None,
    table_image_count: int | None = None,
    formula_count: int | None = None,
    formula_placeholder_count: int | None = None,
    formula_asset_count: int | None = None,
    formula_context_asset_count: int | None = None,
    formula_placeholder_link_count: int | None = None,
    formula_source_link_count: int | None = None,
    formula_enrichment_enabled: bool | None = None,
    formula_model: str | None = None,
) -> dict[str, Any]:
    return {
        "job_uuid": job_uuid,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "input_file_path": str(Path(input_file_path).resolve()) if input_file_path else None,
        "input_sha256": input_sha256,
        "output_dir": str(Path(output_dir).resolve()) if output_dir else None,
        "outputs_written": outputs_written or [],
        "warnings": warnings or [],
        "error_code": error_code,
        "error_message": error_message,
        "conversion_policy": conversion_policy,
        "ocr_fallback_used": ocr_fallback_used,
        "text_quality_gxx_count": text_quality_gxx_count,
        "text_quality_gxx_density": text_quality_gxx_density,
        "table_count": table_count,
        "asset_count": asset_count,
        "generated_outputs": generated_outputs or outputs_written or [],
        "table_artifact_count": table_artifact_count,
        "table_image_count": table_image_count,
        "formula_count": formula_count,
        "formula_placeholder_count": formula_placeholder_count,
        "formula_asset_count": formula_asset_count,
        "formula_context_asset_count": formula_context_asset_count,
        "formula_placeholder_link_count": formula_placeholder_link_count,
        "formula_source_link_count": formula_source_link_count,
        "formula_enrichment_enabled": formula_enrichment_enabled,
        "formula_model": formula_model,
    }


def _safe_page_count(document_dict: dict[str, Any] | None, result: Any | None = None) -> int | None:
    if document_dict:
        for key in ("page_count", "num_pages", "number_of_pages"):
            value = document_dict.get(key)
            if isinstance(value, int):
                return value
        pages = document_dict.get("pages")
        if isinstance(pages, list):
            return len(pages)
        if isinstance(pages, dict):
            return len(pages)

    if result is not None:
        for attr in ("page_count", "num_pages", "number_of_pages"):
            value = getattr(result, attr, None)
            if isinstance(value, int):
                return value
    return None


def _table_objects(document: Any | None) -> list[Any]:
    tables = getattr(document, "tables", None)
    if isinstance(tables, list):
        return tables
    return []


def _text_item_label(item: Any) -> str:
    label = getattr(item, "label", "")
    value = getattr(label, "value", label)
    return str(value).lower()


def _text_item_text(item: Any) -> str:
    for attr in ("text", "orig", "content"):
        value = getattr(item, attr, None)
        if value:
            return str(value)
    return ""


def _formula_objects(document: Any | None) -> list[Any]:
    if document is None:
        return []
    texts = getattr(document, "texts", None)
    if not isinstance(texts, list):
        return []
    formulas: list[Any] = []
    for item in texts:
        label = _text_item_label(item)
        text = _text_item_text(item)
        if "formula" in label or "Formula not decoded" in text:
            formulas.append(item)
    return formulas


def _count_document_dict_formulas(document_dict: dict[str, Any]) -> int:
    count = 0

    def walk(value: Any) -> None:
        nonlocal count
        if isinstance(value, dict):
            label = str(value.get("label", "")).lower()
            text = str(value.get("text") or value.get("orig") or "")
            if "formula" in label or "Formula not decoded" in text:
                count += 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document_dict)
    return count


def _count_formula_placeholders(*texts: str | None) -> int:
    return sum(str(text).count("Formula not decoded") for text in texts if text)


def _count_formula_object_placeholders(document: Any | None) -> int:
    return sum(1 for item in _formula_objects(document) if "Formula not decoded" in _text_item_text(item))


def _formula_item_prov(item: Any) -> Any | None:
    prov = getattr(item, "prov", None)
    if isinstance(prov, list) and prov:
        return prov[0]
    return None


def _page_image_for_formula(document: Any | None, item: Any) -> tuple[Any | None, Any | None]:
    prov = _formula_item_prov(item)
    page_no = getattr(prov, "page_no", None)
    pages = getattr(document, "pages", None)
    if page_no is None or not isinstance(pages, dict):
        return None, None
    page = pages.get(page_no)
    page_image = getattr(page, "image", None)
    pil_image = getattr(page_image, "pil_image", None)
    return page, pil_image


def _page_dimension(value: Any, names: tuple[str, ...]) -> float | None:
    for name in names:
        found = getattr(value, name, None)
        if isinstance(found, (int, float)) and found > 0:
            return float(found)
    return None


def _page_size_points(page: Any, pil_image: Any) -> tuple[float, float] | None:
    size = getattr(page, "size", None)
    if size is not None:
        width = _page_dimension(size, ("width", "w"))
        height = _page_dimension(size, ("height", "h"))
        if width and height:
            return width, height
    return None


def _crop_formula_from_page(
    *,
    document: Any | None,
    item: Any,
    output_path: Path,
    padding_px: int,
    min_height_px: int,
) -> bool:
    prov = _formula_item_prov(item)
    bbox = getattr(prov, "bbox", None)
    if bbox is None:
        return False
    page, page_image = _page_image_for_formula(document, item)
    if page is None or page_image is None:
        return False
    size_points = _page_size_points(page, page_image)
    if size_points is None:
        return False

    page_width_pt, page_height_pt = size_points
    page_width_px, page_height_px = page_image.size
    scale_x = page_width_px / page_width_pt
    scale_y = page_height_px / page_height_pt
    left = float(getattr(bbox, "l"))
    right = float(getattr(bbox, "r"))
    top = float(getattr(bbox, "t"))
    bottom = float(getattr(bbox, "b"))
    origin = str(getattr(getattr(bbox, "coord_origin", ""), "value", getattr(bbox, "coord_origin", ""))).lower()

    x1 = min(left, right) * scale_x
    x2 = max(left, right) * scale_x
    if "bottomleft" in origin:
        y1 = (page_height_pt - max(top, bottom)) * scale_y
        y2 = (page_height_pt - min(top, bottom)) * scale_y
    else:
        y1 = min(top, bottom) * scale_y
        y2 = max(top, bottom) * scale_y

    if y2 - y1 < min_height_px:
        extra = (min_height_px - (y2 - y1)) / 2
        y1 -= extra
        y2 += extra

    crop_box = (
        max(0, int(x1) - padding_px),
        max(0, int(y1) - padding_px),
        min(page_width_px, int(x2) + padding_px),
        min(page_height_px, int(y2) + padding_px),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return False
    cropped = page_image.crop(crop_box)
    cropped.save(output_path, format="PNG")
    return output_path.exists() and output_path.stat().st_size > 0


def _write_table_artifacts(
    *,
    output_dir: Path,
    document_dict: dict[str, Any],
    document: Any | None,
) -> tuple[list[str], list[str]]:
    tables = extract_table_dicts(document_dict)
    table_objects = _table_objects(document)
    warnings: list[str] = []
    written: list[str] = []
    if not tables:
        if count_tables(document_dict):
            warnings.append("table_extraction_limited_no_cell_data_exported")
        return written, warnings

    tables_dir = output_dir / "tables"
    for index, table in enumerate(tables, start=1):
        json_path = tables_dir / f"table_{index}.json"
        write_json(json_path, table)
        written.append(relative_output(json_path, output_dir))

        table_object = table_objects[index - 1] if index - 1 < len(table_objects) else None
        for method_name, suffix in (
            ("export_to_markdown", "md"),
            ("export_to_html", "html"),
        ):
            method = getattr(table_object, method_name, None)
            if method is None:
                continue
            try:
                exported = method(doc=document)
            except Exception:
                try:
                    exported = method()
                except Exception:
                    warnings.append(f"table_{index}_{suffix}_export_failed")
                    continue
            if exported:
                export_path = tables_dir / f"table_{index}.{suffix}"
                export_path.write_text(str(exported), encoding="utf-8")
                written.append(relative_output(export_path, output_dir))

    return written, warnings


def _asset_label(path: str) -> str:
    stem = Path(path).stem.replace("_", " ")
    return stem[:1].upper() + stem[1:]


def _render_review_appendix(
    *,
    output_dir: Path,
    table_outputs: list[str],
    asset_outputs: list[str],
) -> str:
    table_html_outputs = [path for path in table_outputs if path.endswith(".html")]
    table_json_outputs = [path for path in table_outputs if path.endswith(".json")]
    table_md_outputs = [path for path in table_outputs if path.endswith(".md")]
    visible_assets = [
        path
        for path in asset_outputs
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        and not path.startswith("assets/docling-html/")
    ]
    formula_assets = [path for path in visible_assets if re.match(r"assets/formula_\d+(_context)?\.png$", path)]
    non_formula_assets = [path for path in visible_assets if path not in set(formula_assets)]

    if not table_outputs and not visible_assets:
        return ""

    parts = [
        "<hr>",
        '<section id="docling-review-artifacts">',
        "<h1>Review Artifacts</h1>",
        "<p>Derived visual and table artifacts generated by docling-service for manual review.</p>",
    ]

    if formula_assets:
        parts.extend(
            [
                "<h2>Formula Review</h2>",
                '<div class="docling-review-formulas">',
            ]
        )
        for path in formula_assets:
            escaped_path = html.escape(path, quote=True)
            label = html.escape(_asset_label(path))
            parts.append(
                "<figure>"
                f'<a href="{escaped_path}"><img src="{escaped_path}" alt="{label}" loading="lazy"></a>'
                f"<figcaption>{label}</figcaption>"
                "</figure>"
            )
        parts.append("</div>")

    if non_formula_assets:
        parts.extend(
            [
                "<h2>Visual Assets</h2>",
                '<div class="docling-review-assets">',
            ]
        )
        for path in non_formula_assets:
            escaped_path = html.escape(path, quote=True)
            label = html.escape(_asset_label(path))
            parts.append(
                "<figure>"
                f'<a href="{escaped_path}"><img src="{escaped_path}" alt="{label}" loading="lazy"></a>'
                f"<figcaption>{label}</figcaption>"
                "</figure>"
            )
        parts.append("</div>")

    if table_outputs:
        parts.append("<h2>Table Artifacts</h2>")
        if table_html_outputs:
            for path in table_html_outputs:
                escaped_path = html.escape(path, quote=True)
                label = html.escape(Path(path).stem.replace("_", " ").title())
                try:
                    table_html = (output_dir / path).read_text(encoding="utf-8")
                except OSError:
                    table_html = ""
                parts.extend(
                    [
                        '<section class="docling-review-table">',
                        f'<h3><a href="{escaped_path}">{label}</a></h3>',
                        table_html,
                        "</section>",
                    ]
                )
        linked_outputs = table_json_outputs + table_md_outputs
        if linked_outputs:
            parts.append("<ul>")
            for path in linked_outputs:
                escaped_path = html.escape(path, quote=True)
                label = html.escape(path)
                parts.append(f'<li><a href="{escaped_path}">{label}</a></li>')
            parts.append("</ul>")

    parts.append("</section>")
    return "\n".join(parts)


def _link_formula_placeholders(document_html: str, asset_outputs: list[str]) -> str:
    formula_targets = [
        path
        for path in asset_outputs
        if re.match(r"assets/formula_\d+_context\.png$", path)
    ]
    if not formula_targets or "Formula not decoded" not in document_html:
        return document_html

    def sort_key(path: str) -> int:
        match = re.search(r"formula_(\d+)_context\.png$", path)
        return int(match.group(1)) if match else 0

    formula_targets.sort(key=sort_key)
    index = 0

    def replace(_match: re.Match[str]) -> str:
        nonlocal index
        target = formula_targets[min(index, len(formula_targets) - 1)]
        index += 1
        escaped_target = html.escape(target, quote=True)
        return (
            f'<a class="docling-formula-placeholder" href="{escaped_target}">'
            f"Formula not decoded (review formula {index})"
            "</a>"
        )

    return re.sub(r"Formula not decoded", replace, document_html)


def _formula_review_targets(asset_outputs: list[str]) -> dict[int, dict[str, str]]:
    targets: dict[int, dict[str, str]] = {}
    for path in asset_outputs:
        match = re.match(r"assets/formula_(\d+)(_context)?\.png$", path)
        if not match:
            continue
        index = int(match.group(1))
        key = "context" if match.group(2) else "source"
        targets.setdefault(index, {})[key] = path
    return targets


def _formula_source_links(index: int, targets: dict[str, str]) -> str:
    parts: list[str] = []
    for key, label in (("source", "source image"), ("context", "context crop")):
        path = targets.get(key)
        if not path:
            continue
        escaped_path = html.escape(path, quote=True)
        escaped_label = html.escape(label)
        parts.append(f'<a href="{escaped_path}">{escaped_label}</a>')
    if not parts:
        return ""
    return (
        f' <span class="docling-formula-source" data-formula-index="{index}">'
        + " | ".join(parts)
        + "</span>"
    )


def _inject_formula_source_links(
    *,
    document_html: str,
    document: Any | None,
    asset_outputs: list[str],
) -> str:
    targets_by_index = _formula_review_targets(asset_outputs)
    if not targets_by_index:
        return document_html

    formulas = _formula_objects(document)
    linked_indexes: set[int] = set()
    updated_html = document_html

    for index, formula in enumerate(formulas, start=1):
        if index not in targets_by_index:
            continue
        formula_text = _text_item_text(formula).strip()
        if not formula_text or "Formula not decoded" in formula_text:
            continue
        source_links = _formula_source_links(index, targets_by_index[index])
        if not source_links:
            continue
        candidates = [formula_text, html.escape(formula_text)]
        for candidate in candidates:
            if not candidate or candidate not in updated_html:
                continue
            updated_html = updated_html.replace(candidate, candidate + source_links, 1)
            linked_indexes.add(index)
            break

    unmatched_indexes = [
        index
        for index, formula in enumerate(formulas, start=1)
        if index not in linked_indexes
        and index in targets_by_index
        and "Formula not decoded" not in _text_item_text(formula)
    ]
    if not unmatched_indexes:
        return updated_html

    def replace_math(match: re.Match[str]) -> str:
        if not unmatched_indexes:
            return match.group(0)
        index = unmatched_indexes.pop(0)
        source_links = _formula_source_links(index, targets_by_index[index])
        if not source_links:
            return match.group(0)
        linked_indexes.add(index)
        return match.group(0) + source_links

    return re.sub(r"(</math>|</span>)", replace_math, updated_html, count=len(unmatched_indexes))


def _inject_review_appendix(document_html: str, appendix: str) -> str:
    if not appendix:
        return document_html
    styles = """
<style>
#docling-review-artifacts { margin-top: 3rem; padding-top: 1rem; border-top: 2px solid #444; }
.docling-review-assets { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
.docling-review-assets figure { margin: 0; break-inside: avoid; }
.docling-review-assets img { max-width: 100%; height: auto; border: 1px solid #ccc; background: #fff; }
.docling-review-formulas { display: grid; grid-template-columns: 1fr; gap: 1rem; }
.docling-review-formulas figure { margin: 0; break-inside: avoid; }
.docling-review-formulas img { max-width: 100%; height: auto; border: 1px solid #555; background: #fff; }
.docling-formula-placeholder { font-weight: 600; color: #7a1f1f; background: #fff5d6; padding: 0.05rem 0.2rem; }
.docling-formula-source { display: inline-flex; gap: 0.35rem; margin-left: 0.35rem; font-size: 0.82em; white-space: nowrap; }
.docling-formula-source a { color: #245b89; text-decoration: underline; }
.docling-review-table { overflow-x: auto; margin: 1.5rem 0; }
.docling-review-table table { border-collapse: collapse; width: max-content; max-width: 100%; }
.docling-review-table th, .docling-review-table td { border: 1px solid #bbb; padding: 0.35rem 0.5rem; vertical-align: top; }
</style>
"""
    if "</head>" in document_html:
        document_html = document_html.replace("</head>", styles + "\n</head>", 1)
    else:
        document_html = styles + "\n" + document_html
    if "</body>" in document_html:
        return document_html.replace("</body>", appendix + "\n</body>", 1)
    return document_html + "\n" + appendix + "\n"


def _write_document_html(
    *,
    output_dir: Path,
    document: Any | None,
    fallback_html: str,
    table_outputs: list[str],
    asset_outputs: list[str],
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    html_path = output_dir / "document.html"
    html_asset_outputs: list[str] = []

    save_as_html = getattr(document, "save_as_html", None)
    if callable(save_as_html):
        try:
            image_ref_mode = __import__(
                "docling_core.types.doc",
                fromlist=["ImageRefMode"],
            ).ImageRefMode
            previous_cwd = Path.cwd()
            os.chdir(output_dir)
            try:
                save_as_html(
                    "document.html",
                    artifacts_dir=Path("assets") / "docling-html",
                    image_mode=image_ref_mode.REFERENCED,
                )
            finally:
                os.chdir(previous_cwd)
            html_asset_root = output_dir / "assets" / "docling-html"
            if html_asset_root.exists():
                html_asset_outputs = [
                    relative_output(path, output_dir)
                    for path in sorted(html_asset_root.rglob("*"))
                    if path.is_file()
                ]
        except Exception:
            warnings.append("document_html_referenced_export_failed")
            html_path.write_text(fallback_html, encoding="utf-8")
    else:
        warnings.append("document_html_referenced_export_unavailable")
        html_path.write_text(fallback_html, encoding="utf-8")

    if not html_path.exists():
        html_path.write_text(fallback_html, encoding="utf-8")

    document_html = html_path.read_text(encoding="utf-8")
    document_html = _link_formula_placeholders(document_html, asset_outputs)
    document_html = _inject_formula_source_links(
        document_html=document_html,
        document=document,
        asset_outputs=asset_outputs,
    )
    appendix = _render_review_appendix(
        output_dir=output_dir,
        table_outputs=table_outputs,
        asset_outputs=asset_outputs,
    )
    document_html = _inject_review_appendix(document_html, appendix)
    html_path.write_text(document_html, encoding="utf-8")

    if asset_outputs and "<img" not in document_html:
        warnings.append("document_html_asset_links_limited_no_img_tags")
    if table_outputs and "tables/" not in document_html:
        warnings.append("document_html_table_links_limited")

    return ["document.html"] + html_asset_outputs, warnings


def _iter_asset_candidates(document: Any | None) -> list[tuple[str, Any]]:
    if document is None:
        return []
    candidates: list[tuple[str, Any]] = []
    pages = getattr(document, "pages", None)
    if isinstance(pages, dict):
        for page_no, page in sorted(pages.items(), key=lambda item: str(item[0])):
            candidates.append((f"page_{page_no}", getattr(page, "image", None)))
    for label, items in (
        ("picture", getattr(document, "pictures", None)),
        ("table", getattr(document, "tables", None)),
    ):
        if isinstance(items, list):
            for index, item in enumerate(items, start=1):
                candidates.append((f"{label}_{index}", item))
    return candidates


def _image_from_candidate(candidate: Any, document: Any | None) -> Any:
    image = getattr(candidate, "image", None)
    if image is not None:
        return image
    get_image = getattr(candidate, "get_image", None)
    if callable(get_image):
        try:
            return get_image(doc=document)
        except TypeError:
            try:
                return get_image(document)
            except TypeError:
                return get_image()
    return candidate


def _write_image_candidate(path: Path, image: Any) -> bool:
    if image is None:
        return False
    pil_image = getattr(image, "pil_image", None)
    if pil_image is not None:
        pil_image.save(path, format="PNG")
        return path.exists() and path.stat().st_size > 0
    save = getattr(image, "save", None)
    if callable(save):
        save(path)
        return path.exists() and path.stat().st_size > 0
    uri = getattr(image, "uri", None)
    if uri:
        source = Path(str(uri))
        if source.exists() and source.is_file():
            shutil.copyfile(source, path)
            return path.exists() and path.stat().st_size > 0
    return False


def _write_asset_artifacts(*, output_dir: Path, document: Any | None) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    written: list[str] = []
    candidates = _iter_asset_candidates(document)
    if not candidates:
        warnings.append("asset_extraction_unavailable_no_docling_image_candidates")
        return written, warnings

    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    for index, formula in enumerate(_formula_objects(document), start=1):
        formula_path = assets_dir / f"formula_{index}.png"
        context_path = assets_dir / f"formula_{index}_context.png"
        try:
            wrote_formula = _crop_formula_from_page(
                document=document,
                item=formula,
                output_path=formula_path,
                padding_px=20,
                min_height_px=96,
            )
            if not wrote_formula:
                image = _image_from_candidate(formula, document)
                wrote_formula = _write_image_candidate(formula_path, image)
            if wrote_formula:
                written.append(relative_output(formula_path, output_dir))
        except Exception:
            warnings.append(f"formula_{index}_asset_export_failed")
        try:
            if _crop_formula_from_page(
                document=document,
                item=formula,
                output_path=context_path,
                padding_px=120,
                min_height_px=220,
            ):
                written.append(relative_output(context_path, output_dir))
        except Exception:
            warnings.append(f"formula_{index}_context_asset_export_failed")

    for label, candidate in candidates:
        if candidate is None:
            continue
        path = assets_dir / f"{label}.png"
        try:
            image = _image_from_candidate(candidate, document)
            if _write_image_candidate(path, image):
                written.append(relative_output(path, output_dir))
        except Exception:
            warnings.append(f"{label}_asset_export_failed")

    if not written:
        warnings.append("asset_extraction_limited_no_image_files_written")
        try:
            assets_dir.rmdir()
        except OSError:
            pass
    return written, warnings


def write_docling_outputs(
    *,
    job_uuid: str,
    input_file_path: str | Path,
    conversion: dict[str, Any],
    output_root: str | Path | None = None,
    display_name: str | None = None,
    original_name: str | None = None,
    source_name: str | None = None,
    image_export_mode: str = "referenced",
    requested_outputs: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    input_path = Path(input_file_path).resolve()
    job_output_dir = output_dir_for_job(job_uuid, output_root)
    job_output_dir.mkdir(parents=True, exist_ok=True)

    input_sha256 = sha256_file(input_path)
    document_dict = conversion["document_dict"]
    if not isinstance(document_dict, dict):
        raise ValueError("document_dict must be a dict")

    written: list[str] = []
    (job_output_dir / "document.md").write_text(str(conversion["markdown"]), encoding="utf-8")
    written.append("document.md")
    write_json(job_output_dir / "document.json", document_dict)
    written.append("document.json")

    text = conversion.get("text")
    if text is not None:
        (job_output_dir / "text.txt").write_text(str(text), encoding="utf-8")
        written.append("text.txt")

    doctags = conversion.get("doctags")
    if doctags is not None:
        (job_output_dir / "doctags.txt").write_text(str(doctags), encoding="utf-8")
        written.append("doctags.txt")

    warnings = list(conversion.get("warnings") or [])
    table_outputs, table_warnings = _write_table_artifacts(
        output_dir=job_output_dir,
        document_dict=document_dict,
        document=conversion.get("document"),
    )
    written.extend(table_outputs)
    warnings.extend(table_warnings)

    asset_outputs, asset_warnings = _write_asset_artifacts(
        output_dir=job_output_dir,
        document=conversion.get("document"),
    )
    written.extend(asset_outputs)
    warnings.extend(asset_warnings)

    html_outputs, html_warnings = _write_document_html(
        output_dir=job_output_dir,
        document=conversion.get("document"),
        fallback_html=str(conversion["html"]),
        table_outputs=table_outputs,
        asset_outputs=asset_outputs,
    )
    written[1:1] = html_outputs
    warnings.extend(html_warnings)

    generated_outputs = written + ["metadata.json", "status.json"]
    table_count = count_tables(document_dict)
    table_artifact_count = len(table_outputs)
    asset_count = len(asset_outputs) + len([path for path in html_outputs if path != "document.html"])
    table_image_count = len(
        [path for path in asset_outputs if path.startswith("assets/table_") and path.lower().endswith(".png")]
    )
    formula_asset_count = len(
        [path for path in asset_outputs if re.match(r"assets/formula_\d+\.png$", path)]
    )
    formula_context_asset_count = len(
        [path for path in asset_outputs if re.match(r"assets/formula_\d+_context\.png$", path)]
    )
    formula_count = max(
        len(_formula_objects(conversion.get("document"))),
        _count_document_dict_formulas(document_dict),
    )
    final_html = (job_output_dir / "document.html").read_text(encoding="utf-8")
    formula_placeholder_count = max(
        _count_formula_placeholders(str(conversion.get("markdown") or "")),
        _count_formula_placeholders(str(conversion.get("html") or "")),
        _count_formula_placeholders(str(conversion.get("text") or "")),
        _count_formula_placeholders(final_html),
        _count_formula_object_placeholders(conversion.get("document")),
    )
    formula_placeholder_link_count = final_html.count("Formula not decoded (review formula")
    formula_source_link_count = final_html.count('class="docling-formula-source"')

    if table_count and table_image_count == 0:
        page_images = [
            path for path in asset_outputs if path.startswith("assets/page_") and path.lower().endswith(".png")
        ]
        if page_images:
            warnings.append("table_crop_unavailable_page_images_available_for_review")
        else:
            warnings.append("table_visual_review_unavailable_no_table_or_page_images")
    if formula_placeholder_count:
        if formula_asset_count and formula_context_asset_count:
            warnings.append("formula_decode_limited_high_res_review_crops_written")
        elif formula_asset_count:
            warnings.append("formula_decode_limited_review_crops_written")
        else:
            warnings.append("formula_decode_limited_no_formula_review_crops")

    metadata = build_metadata(
        job_uuid=job_uuid,
        display_name=display_name,
        original_name=original_name,
        source_name=source_name,
        input_file_path=input_path,
        input_sha256=input_sha256,
        image_export_mode=image_export_mode,
        requested_outputs=requested_outputs,
        generated_outputs=generated_outputs,
        detected_format="pdf",
        page_count=_safe_page_count(document_dict, conversion.get("result")),
        docling_version=conversion.get("docling_version"),
        link_count=None,
        table_count=table_count,
        asset_count=asset_count,
        conversion_policy=conversion.get("conversion_policy", CONVERSION_POLICY),
        ocr_fallback_used=bool(conversion.get("ocr_fallback_used")),
        text_quality_gxx_count=conversion.get("text_quality_gxx_count"),
        text_quality_gxx_density=conversion.get("text_quality_gxx_density"),
        table_artifact_count=table_artifact_count,
        table_image_count=table_image_count,
        formula_count=formula_count,
        formula_placeholder_count=formula_placeholder_count,
        formula_asset_count=formula_asset_count,
        formula_context_asset_count=formula_context_asset_count,
        formula_placeholder_link_count=formula_placeholder_link_count,
        formula_source_link_count=formula_source_link_count,
        formula_enrichment_enabled=conversion.get("formula_enrichment_enabled"),
        formula_model=conversion.get("formula_model"),
    )
    write_json(job_output_dir / "metadata.json", metadata)

    actual_started_at = started_at or utc_now_iso()
    actual_finished_at = finished_at or utc_now_iso()
    status = build_status(
        job_uuid=job_uuid,
        status=STATUS_SUCCESS,
        started_at=actual_started_at,
        finished_at=actual_finished_at,
        duration_seconds=duration_seconds,
        input_file_path=input_path,
        input_sha256=input_sha256,
        output_dir=job_output_dir,
        outputs_written=generated_outputs,
        warnings=warnings,
        error_code=None,
        error_message=None,
        conversion_policy=conversion.get("conversion_policy", CONVERSION_POLICY),
        ocr_fallback_used=bool(conversion.get("ocr_fallback_used")),
        text_quality_gxx_count=conversion.get("text_quality_gxx_count"),
        text_quality_gxx_density=conversion.get("text_quality_gxx_density"),
        table_count=table_count,
        asset_count=asset_count,
        generated_outputs=generated_outputs,
        table_artifact_count=table_artifact_count,
        table_image_count=table_image_count,
        formula_count=formula_count,
        formula_placeholder_count=formula_placeholder_count,
        formula_asset_count=formula_asset_count,
        formula_context_asset_count=formula_context_asset_count,
        formula_placeholder_link_count=formula_placeholder_link_count,
        formula_source_link_count=formula_source_link_count,
        formula_enrichment_enabled=conversion.get("formula_enrichment_enabled"),
        formula_model=conversion.get("formula_model"),
    )
    write_json(job_output_dir / "status.json", status)

    return {
        "output_dir": job_output_dir,
        "metadata_path": job_output_dir / "metadata.json",
        "status_path": job_output_dir / "status.json",
        "metadata": metadata,
        "status": status,
        "outputs_written": generated_outputs,
    }


def write_placeholder_outputs(
    *,
    job_uuid: str,
    input_file_path: str | Path,
    output_root: str | Path | None = None,
    display_name: str | None = None,
    original_name: str | None = None,
    source_name: str | None = None,
    image_export_mode: str = "referenced",
    requested_outputs: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    input_path = Path(input_file_path).resolve()
    job_output_dir = output_dir_for_job(job_uuid, output_root)
    job_output_dir.mkdir(parents=True, exist_ok=True)

    input_sha256 = sha256_file(input_path)
    generated_outputs = REQUIRED_SUCCESS_OUTPUTS.copy()

    document_html = (
        "<!doctype html>\n"
        "<html><body>\n"
        "<h1>Placeholder conversion</h1>\n"
        "<p>This is not a real Docling conversion.</p>\n"
        "</body></html>\n"
    )
    document_md = (
        "# Placeholder conversion\n\n"
        "This is not a real Docling conversion.\n"
    )
    document_json = {
        "job_uuid": job_uuid,
        "placeholder": True,
        "converter": "placeholder",
        "message": "This is not a real Docling conversion.",
    }

    (job_output_dir / "document.html").write_text(document_html, encoding="utf-8")
    (job_output_dir / "document.md").write_text(document_md, encoding="utf-8")
    write_json(job_output_dir / "document.json", document_json)

    metadata = build_metadata(
        job_uuid=job_uuid,
        display_name=display_name,
        original_name=original_name,
        source_name=source_name,
        input_file_path=input_path,
        input_sha256=input_sha256,
        image_export_mode=image_export_mode,
        requested_outputs=requested_outputs,
        generated_outputs=generated_outputs,
        detected_format="pdf",
        docling_version=None,
        link_count=0,
        table_count=0,
        asset_count=0,
        conversion_policy=None,
        ocr_fallback_used=None,
        text_quality_gxx_count=None,
        text_quality_gxx_density=None,
    )
    write_json(job_output_dir / "metadata.json", metadata)

    actual_started_at = started_at or utc_now_iso()
    actual_finished_at = finished_at or utc_now_iso()
    status = build_status(
        job_uuid=job_uuid,
        status=STATUS_SUCCESS,
        started_at=actual_started_at,
        finished_at=actual_finished_at,
        duration_seconds=duration_seconds,
        input_file_path=input_path,
        input_sha256=input_sha256,
        output_dir=job_output_dir,
        outputs_written=generated_outputs,
        warnings=["placeholder_conversion_only"],
        error_code=None,
        error_message=None,
    )
    write_json(job_output_dir / "status.json", status)

    return {
        "output_dir": job_output_dir,
        "metadata_path": job_output_dir / "metadata.json",
        "status_path": job_output_dir / "status.json",
        "metadata": metadata,
        "status": status,
        "outputs_written": generated_outputs,
    }
