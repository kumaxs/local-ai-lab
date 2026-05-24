"""Artifact writing helpers for the docling-service skeleton."""

from __future__ import annotations

import hashlib
import html
import json
import os
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

    if not table_outputs and not visible_assets:
        return ""

    parts = [
        "<hr>",
        '<section id="docling-review-artifacts">',
        "<h1>Review Artifacts</h1>",
        "<p>Derived visual and table artifacts generated by docling-service for manual review.</p>",
    ]

    if visible_assets:
        parts.extend(
            [
                "<h2>Visual Assets</h2>",
                '<div class="docling-review-assets">',
            ]
        )
        for path in visible_assets:
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


def _inject_review_appendix(document_html: str, appendix: str) -> str:
    if not appendix:
        return document_html
    styles = """
<style>
#docling-review-artifacts { margin-top: 3rem; padding-top: 1rem; border-top: 2px solid #444; }
.docling-review-assets { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
.docling-review-assets figure { margin: 0; break-inside: avoid; }
.docling-review-assets img { max-width: 100%; height: auto; border: 1px solid #ccc; background: #fff; }
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
                candidates.append((f"{label}_{index}", getattr(item, "image", None)))
    return candidates


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
    for label, image in candidates:
        if image is None:
            continue
        path = assets_dir / f"{label}.png"
        try:
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
    asset_count = len(asset_outputs) + len([path for path in html_outputs if path != "document.html"])

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
