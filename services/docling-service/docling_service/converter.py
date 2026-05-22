"""Placeholder converter for the initial docling-service skeleton."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .docling_adapter import DoclingAdapterError, convert_with_docling
from .writer import write_placeholder_outputs


def placeholder_convert(
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
    """Write placeholder outputs without invoking Docling or fetching links."""
    return write_placeholder_outputs(
        job_uuid=job_uuid,
        input_file_path=input_file_path,
        output_root=output_root,
        display_name=display_name,
        original_name=original_name,
        source_name=source_name,
        image_export_mode=image_export_mode,
        requested_outputs=requested_outputs,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
    )


def docling_convert(
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
    """Attempt the real Docling adapter path.

    The adapter currently performs availability probing and returns controlled
    errors until real Docling output mapping is implemented.
    """
    try:
        return convert_with_docling(
            job_uuid=job_uuid,
            input_file_path=input_file_path,
            output_root=output_root,
            display_name=display_name,
            original_name=original_name,
            source_name=source_name,
            image_export_mode=image_export_mode,
            requested_outputs=requested_outputs,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
        )
    except DoclingAdapterError:
        raise
