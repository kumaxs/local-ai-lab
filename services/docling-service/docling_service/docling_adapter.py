"""Lazy Docling adapter for the dependency spike.

This module must remain importable when Docling is not installed. It does not
shell out to the Docling CLI and does not fetch external URLs.
"""

from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .quality import CONVERSION_POLICY, measure_gxx_quality


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


def _make_document_converter(profile: str) -> Any:
    DocumentConverter = _load_document_converter()
    try:
        document_converter = import_module("docling.document_converter")
        accelerator_options = import_module("docling.datamodel.accelerator_options")
        base_models = import_module("docling.datamodel.base_models")
        pipeline_options = import_module("docling.datamodel.pipeline_options")
        artifacts_path = Path.home() / ".cache" / "docling" / "models"
        option_kwargs: dict[str, Any] = {
            "accelerator_options": accelerator_options.AcceleratorOptions(device="cpu"),
            "do_ocr": False,
            "do_table_structure": False,
            "generate_page_images": False,
            "generate_picture_images": False,
            "generate_table_images": False,
            "images_scale": 1.0,
            "artifacts_path": str(artifacts_path) if artifacts_path.is_dir() else None,
        }
        if profile == "quality_first":
            option_kwargs.update(
                {
                    "do_table_structure": True,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "generate_table_images": True,
                }
            )
        elif profile == "quality_without_table_structure":
            option_kwargs.update(
                {
                    "generate_page_images": True,
                    "generate_picture_images": True,
                }
            )
        elif profile == "ocr_fallback_mac":
            option_kwargs.update(
                {
                    "do_ocr": True,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "ocr_options": pipeline_options.OcrMacOptions(
                        lang=["zh-Hans", "zh-Hant", "en-US"],
                        force_full_page_ocr=True,
                    ),
                }
            )
        elif profile == "ocr_fallback_auto":
            option_kwargs.update(
                {
                    "do_ocr": True,
                    "generate_page_images": True,
                    "generate_picture_images": True,
                    "ocr_options": pipeline_options.OcrAutoOptions(
                        lang=["chinese", "english"],
                        force_full_page_ocr=True,
                    ),
                }
            )
        options = pipeline_options.PdfPipelineOptions(**option_kwargs)
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


def _export_document_payload(
    *,
    result: Any,
    docling_version: str | None,
    warnings: list[str],
    profile: str,
) -> dict[str, Any]:
    document = result.document
    try:
        markdown = document.export_to_markdown()
        html = document.export_to_html()
        document_dict = document.export_to_dict()
    except Exception as exc:
        raise DoclingAdapterError(f"docling core export failed: {exc.__class__.__name__}") from exc

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

    text_quality = measure_gxx_quality(markdown, html, text)
    return {
        "markdown": markdown,
        "html": html,
        "document_dict": document_dict,
        "document": document,
        "text": text,
        "doctags": doctags,
        "warnings": warnings,
        "docling_version": docling_version,
        "result": result,
        "conversion_policy": CONVERSION_POLICY,
        "conversion_profile": profile,
        "ocr_fallback_used": False,
        "text_quality_gxx_count": text_quality.gxx_count,
        "text_quality_gxx_density": text_quality.gxx_density,
        "text_quality_failed": text_quality.failed,
    }


def _run_docling_profile(input_path: Path, profile: str, docling_version: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        converter = _make_document_converter(profile)
        result = converter.convert(input_path)
    except Exception as exc:
        raise DoclingAdapterError(f"docling {profile} conversion failed: {exc.__class__.__name__}") from exc
    return _export_document_payload(
        result=result,
        docling_version=docling_version,
        warnings=warnings,
        profile=profile,
    )


def _convert_quality_first(input_path: Path, docling_version: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        conversion = _run_docling_profile(input_path, "quality_first", docling_version)
    except DoclingAdapterError as exc:
        warnings.append(f"table_or_asset_quality_profile_unavailable: {exc}")
        try:
            conversion = _run_docling_profile(
                input_path,
                "quality_without_table_structure",
                docling_version,
            )
        except DoclingAdapterError as fallback_exc:
            warnings.append(f"asset_generation_profile_unavailable: {fallback_exc}")
            conversion = _run_docling_profile(input_path, "compatibility", docling_version)
        conversion["warnings"] = warnings + list(conversion.get("warnings") or [])

    if conversion.get("text_quality_failed"):
        conversion.setdefault("warnings", []).append(
            "text_quality_failed_gxx_density; attempting OCR fallback"
        )
        for profile in ("ocr_fallback_mac", "ocr_fallback_auto"):
            try:
                fallback = _run_docling_profile(input_path, profile, docling_version)
            except DoclingAdapterError as exc:
                conversion.setdefault("warnings", []).append(f"{profile}_failed: {exc}")
                continue
            fallback_quality = measure_gxx_quality(
                fallback.get("markdown"),
                fallback.get("html"),
                fallback.get("text"),
            )
            current_quality = measure_gxx_quality(
                conversion.get("markdown"),
                conversion.get("html"),
                conversion.get("text"),
            )
            if (
                not fallback_quality.failed
                or fallback_quality.gxx_count < current_quality.gxx_count
            ):
                fallback["ocr_fallback_used"] = True
                fallback["warnings"] = list(conversion.get("warnings") or []) + [
                    f"{profile}_used_after_gxx_quality_failure"
                ] + list(fallback.get("warnings") or [])
                return fallback
            conversion.setdefault("warnings", []).append(
                f"{profile}_did_not_improve_gxx_quality"
            )

    return conversion


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

    return _convert_quality_first(input_path, docling_version)
