"""Lazy Docling adapter for the dependency spike.

This module must remain importable when Docling is not installed. It does not
shell out to the Docling CLI and does not fetch external URLs.
"""

from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DoclingAdapterError(RuntimeError):
    """Controlled adapter error safe to expose as a short CLI message."""


def _import_docling() -> Any:
    try:
        return import_module("docling")
    except ImportError as exc:
        raise DoclingAdapterError("docling package is not installed") from exc


def _load_document_converter() -> type[Any]:
    try:
        module = import_module("docling.document_converter")
    except ImportError as exc:
        raise DoclingAdapterError("docling DocumentConverter is not available") from exc
    return module.DocumentConverter


def _make_document_converter() -> Any:
    DocumentConverter = _load_document_converter()
    try:
        document_converter = import_module("docling.document_converter")
        accelerator_options = import_module("docling.datamodel.accelerator_options")
        base_models = import_module("docling.datamodel.base_models")
        pipeline_options = import_module("docling.datamodel.pipeline_options")
        artifacts_path = Path.home() / ".cache" / "docling" / "models"
        options = pipeline_options.PdfPipelineOptions(
            accelerator_options=accelerator_options.AcceleratorOptions(device="cpu"),
            do_ocr=False,
            do_table_structure=False,
            artifacts_path=str(artifacts_path) if artifacts_path.is_dir() else None,
        )
        return DocumentConverter(
            allowed_formats=[base_models.InputFormat.PDF],
            format_options={
                base_models.InputFormat.PDF: document_converter.PdfFormatOption(
                    pipeline_options=options,
                ),
            },
        )
    except Exception:
        return DocumentConverter()


def is_docling_available() -> bool:
    try:
        _import_docling()
    except DoclingAdapterError:
        return False
    return True


def get_docling_version() -> str | None:
    try:
        return metadata.version("docling")
    except metadata.PackageNotFoundError:
        return None


def _is_remote_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https", "ftp"}


def _optional_export(document: Any, method_names: tuple[str, ...], label: str) -> tuple[Any | None, str | None]:
    for method_name in method_names:
        method = getattr(document, method_name, None)
        if method is None:
            continue
        try:
            return method(), None
        except Exception:
            return None, f"{label}_export_failed"
    return None, None


def convert_with_docling(
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
    """Run Docling's Python API and return exported document payloads."""
    _ = (
        job_uuid,
        output_root,
        display_name,
        original_name,
        source_name,
        image_export_mode,
        requested_outputs,
        started_at,
        finished_at,
        duration_seconds,
    )
    input_value = str(input_file_path)
    if _is_remote_url(input_value):
        raise DoclingAdapterError("input_file_path must be a local file path")

    input_path = Path(input_file_path).expanduser()
    if not input_path.exists() or not input_path.is_file():
        raise DoclingAdapterError("input_file_path must exist and be a local file")

    docling_version = get_docling_version()

    try:
        converter = _make_document_converter()
        result = converter.convert(input_path)
        document = result.document
    except Exception as exc:
        raise DoclingAdapterError(f"docling conversion failed: {exc.__class__.__name__}") from exc

    try:
        markdown = document.export_to_markdown()
        html = document.export_to_html()
        document_dict = document.export_to_dict()
    except Exception as exc:
        raise DoclingAdapterError(f"docling core export failed: {exc.__class__.__name__}") from exc

    warnings: list[str] = []
    text, warning = _optional_export(document, ("export_to_text",), "text")
    if warning:
        warnings.append(warning)

    doctags, warning = _optional_export(
        document,
        ("export_to_doctags", "export_to_document_tokens"),
        "doctags",
    )
    if warning:
        warnings.append(warning)

    return {
        "markdown": markdown,
        "html": html,
        "document_dict": document_dict,
        "text": text,
        "doctags": doctags,
        "warnings": warnings,
        "docling_version": docling_version,
        "result": result,
    }
