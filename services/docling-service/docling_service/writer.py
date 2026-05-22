"""Artifact writing helpers for the docling-service skeleton."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contract import REQUIRED_SUCCESS_OUTPUTS, STATUS_SUCCESS


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
    (job_output_dir / "document.html").write_text(str(conversion["html"]), encoding="utf-8")
    written.append("document.html")
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

    generated_outputs = written + ["metadata.json", "status.json"]
    warnings = list(conversion.get("warnings") or [])

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
        table_count=None,
        asset_count=0,
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
