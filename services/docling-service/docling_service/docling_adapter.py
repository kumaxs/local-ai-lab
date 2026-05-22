"""Lazy Docling adapter for the dependency spike.

This module must remain importable when Docling is not installed. It does not
shell out to the Docling CLI and does not fetch external URLs.
"""

from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path
from typing import Any


class DoclingAdapterError(RuntimeError):
    """Controlled adapter error safe to expose as a short CLI message."""


def _import_docling() -> Any:
    try:
        return import_module("docling")
    except ImportError as exc:
        raise DoclingAdapterError("docling package is not installed") from exc


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
    """Real conversion placeholder for the adapter spike.

    The Docling Python API is intentionally not wired until the output writer
    contract can be mapped cleanly. Import availability is checked lazily so the
    module remains safe in environments without Docling.
    """
    _ = (
        job_uuid,
        input_file_path,
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
    _import_docling()
    raise DoclingAdapterError("docling adapter is available but real conversion is not implemented")
