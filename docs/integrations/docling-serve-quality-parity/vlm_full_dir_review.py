#!/usr/bin/env python3
"""Run Docling VlmPipeline over a PDF directory for review comparison.

This is an evaluation helper only. It does not replace the Docling Serve
standard-pipeline adapter.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
import fcntl
from pathlib import Path
from typing import Any


DEFAULT_INPUT_DIR = Path("/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline/test_pdfs")
DEFAULT_ARTIFACTS_PATH = Path("/Users/zeyuan/.cache/docling/models")
GRANITE_MLX_CACHE = DEFAULT_ARTIFACTS_PATH / "ibm-granite--granite-docling-258M-mlx"
SOURCE_REFERENCE_PATH = "source.pdf"
MAX_SOURCE_COPY_BYTES = 128 * 1024 * 1024
QUARANTINE_README_NAME = "QUARANTINE_README.txt"
QUARANTINE_RETENTION_COUNT = 2
QUARANTINE_README_TEXT = (
    "This directory contains stale or failed VLM review output.\n"
    "It is excluded from the active job contract; only the sibling job\n"
    "directory with status.json and metadata.json is consumable.\n"
)
PUBLISH_LOCK_SUFFIX = ".vlm_publish.lock"


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_regular_and_readable(path: Path) -> bool:
    if not _is_regular_file(path):
        return False
    return os.access(path, os.R_OK)


def _file_sha256(path: Path) -> str | None:
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _file_size(path: Path) -> int | None:
    try:
        return os.lstat(path).st_size
    except OSError:
        return None


def _prepare_input_file_metadata(
    pdf: Path, output_dir: Path
) -> tuple[dict[str, object], bool]:
    metadata: dict[str, object] = {
        "input_file": str(pdf),
    }
    metadata["input_file_readable"] = _is_regular_and_readable(pdf)
    if not metadata["input_file_readable"]:
        metadata["input_file_reference_mode"] = "missing_input"
        metadata["input_file_reference"] = str(output_dir / SOURCE_REFERENCE_PATH)
        metadata["input_file_reference_error"] = "input_pdf_not_readable"
        metadata["input_file_reference_verified"] = False
        return metadata, False

    metadata["input_file_size_bytes"] = _file_size(pdf)
    source_size = metadata["input_file_size_bytes"]
    source_size_int = int(source_size) if isinstance(source_size, int) else None
    metadata["input_file_reference"] = str(output_dir / SOURCE_REFERENCE_PATH)

    reference_path = output_dir / SOURCE_REFERENCE_PATH
    reference_mode = "none"
    reference_reason: str | None = None
    reference_verified = False

    if reference_path.exists():
        if not _is_regular_file(reference_path):
            reference_mode = "existing_unverified"
            reference_reason = "input_file_reference_not_regular"
        else:
            reference_mode = "existing"
            reference_sha = _file_sha256(reference_path)
            input_sha256 = _file_sha256(pdf)
            metadata["input_sha256"] = input_sha256
            if reference_sha is None:
                reference_reason = "input_file_reference_not_readable"
            elif input_sha256 is None:
                reference_reason = "input_file_checksum_unavailable"
            elif reference_sha != input_sha256:
                reference_mode = "existing_mismatch"
                reference_reason = (
                    f"source_reference_sha_mismatch:"
                    f"{str(reference_sha)}!= {str(input_sha256)}"
                )
            else:
                reference_verified = True
    else:
        if source_size_int is None:
            reference_reason = "input_file_size_unavailable"
        elif source_size_int > MAX_SOURCE_COPY_BYTES:
            reference_reason = (
                f"input_file_too_large_to_snapshot:{source_size_int}>{MAX_SOURCE_COPY_BYTES}"
            )
        else:
            copied = _snapshot_pdf_to_reference(pdf, reference_path)
            if copied is not None:
                metadata["input_sha256"] = copied
                reference_mode = "copied"
                reference_verified = True
            else:
                reference_reason = "input_file_reference_copy_failed"

    metadata["input_file_reference"] = str(reference_path)
    metadata["input_file_reference_mode"] = reference_mode
    metadata["input_file_reference_verified"] = reference_verified
    if reference_mode == "none" and reference_reason is not None:
        metadata["input_file_reference_error"] = reference_reason
    if reference_mode in {"existing", "existing_mismatch", "copied"} and not reference_verified:
        metadata["input_file_reference_error"] = reference_reason

    return metadata, reference_verified


def _snapshot_pdf_to_reference(pdf: Path, reference_path: Path) -> str | None:
    """Copy regular input PDF to an immutable snapshot, returning its hash."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    read_flags = os.O_RDONLY | nofollow
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    source_fd: int | None = None
    dest_fd: int | None = None
    tmp_created = False
    tmp_path = reference_path.with_name(
        f".{reference_path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    )
    hasher = hashlib.sha256()
    copied_bytes = 0
    try:
        source_fd = os.open(str(pdf), read_flags)
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return None
        dest_fd = os.open(str(tmp_path), write_flags, 0o600)
        tmp_created = True
        os.fchmod(dest_fd, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            copied_bytes += len(chunk)
            if copied_bytes > MAX_SOURCE_COPY_BYTES:
                raise ValueError("source snapshot exceeds max size during copy")
            hasher.update(chunk)
            written = 0
            while written < len(chunk):
                chunk_written = os.write(dest_fd, chunk[written:])
                if chunk_written <= 0:
                    raise OSError("short write while snapshotting source pdf")
                written += chunk_written
        os.fsync(dest_fd)
        digest = hasher.hexdigest()
        os.replace(tmp_path, reference_path)
        return digest
    except (OSError, ValueError):
        if tmp_created:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if dest_fd is not None:
            os.close(dest_fd)


def _path_exists(path: Path) -> bool:
    """Return true for regular paths and dangling symlinks alike."""

    return path.exists() or path.is_symlink()


def _probe_existing_source_reference(
    pdf: Path, output_dir: Path
) -> tuple[dict[str, object] | None, bool]:
    """Inspect an old ``source.pdf`` without accepting or changing it.

    Worker attempts are always built in a fresh directory.  This probe lets us
    report a source mismatch accurately before quarantining the old directory;
    a mismatched source is never carried into a new attempt.
    """

    reference_path = output_dir / SOURCE_REFERENCE_PATH
    if not _path_exists(reference_path):
        return None, True

    metadata: dict[str, object] = {
        "input_file": str(pdf),
        "input_file_reference": str(reference_path),
        "input_file_readable": _is_regular_and_readable(pdf),
        "input_file_reference_mode": "existing",
        "input_file_reference_verified": False,
    }
    if not metadata["input_file_readable"]:
        metadata["input_file_reference_mode"] = "existing_unverified"
        metadata["input_file_reference_error"] = "input_pdf_not_readable"
        return metadata, False
    if not _is_regular_file(reference_path):
        metadata["input_file_reference_mode"] = "existing_unverified"
        metadata["input_file_reference_error"] = "input_file_reference_not_regular"
        return metadata, False

    input_sha = _file_sha256(pdf)
    reference_sha = _file_sha256(reference_path)
    metadata["input_sha256"] = input_sha
    metadata["input_file_size_bytes"] = _file_size(pdf)
    if input_sha is None:
        metadata["input_file_reference_mode"] = "existing_unverified"
        metadata["input_file_reference_error"] = "input_file_checksum_unavailable"
        return metadata, False
    if reference_sha is None:
        metadata["input_file_reference_mode"] = "existing_unverified"
        metadata["input_file_reference_error"] = "input_file_reference_not_readable"
        return metadata, False
    if reference_sha != input_sha:
        metadata["input_file_reference_mode"] = "existing_mismatch"
        metadata["input_file_reference_error"] = (
            "source_reference_sha_mismatch:"
            f"{reference_sha}!={input_sha}"
        )
        return metadata, False

    metadata["input_file_reference_verified"] = True
    return metadata, True


def _fresh_output_conflicts(output_dir: Path, input_identity: dict[str, object]) -> list[str]:
    if not output_dir.exists():
        return []
    if not output_dir.is_dir():
        return [output_dir.name]

    allowed_reference = bool(
        input_identity.get("input_file_reference_verified")
        and input_identity.get("input_file_reference_mode") in {"existing", "copied"}
    )
    allowed_entries = {SOURCE_REFERENCE_PATH} if allowed_reference else set()
    return sorted(
        path.name
        for path in output_dir.iterdir()
        if path.name not in allowed_entries and path.name != ""
    )


def _create_staging_dir(output_dir: Path) -> Path:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir.parent / (
        f".{output_dir.name}.vlm_staging_{os.getpid()}_{time.time_ns()}"
    )
    staging_dir.mkdir()
    return staging_dir


def _publish_lock_path(output_dir: Path) -> Path:
    return output_dir.parent / f".{output_dir.name}{PUBLISH_LOCK_SUFFIX}"


def _acquire_publish_lock(output_dir: Path) -> tuple[int, Path]:
    lock_path = _publish_lock_path(output_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        try:
            if stat.S_ISLNK(os.lstat(lock_path).st_mode):
                raise FileExistsError(f"publish lock path is a symlink: {lock_path}")
        except FileNotFoundError:
            pass
    flags |= nofollow
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if nofollow and exc.errno == errno.ELOOP:
            raise FileExistsError(f"publish lock path is a symlink: {lock_path}") from exc
        raise
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise FileExistsError(f"publish lock is not regular: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN}:
                raise FileExistsError(f"output publish lock is held: {lock_path}") from exc
            raise
        return descriptor, lock_path
    except Exception:
        os.close(descriptor)
        raise


def _publish_staging_output(
    staging_dir: Path, output_dir: Path, lock_descriptor: int | None = None
) -> None:
    if staging_dir == output_dir:
        return
    lock_acquired = False
    if lock_descriptor is None:
        lock_descriptor, _ = _acquire_publish_lock(output_dir)
        lock_acquired = True
    try:
        if _path_exists(output_dir):
            raise FileExistsError(
                f"output directory appeared during publish: {output_dir}"
            )
        # Both paths are siblings on the same filesystem.  Renaming the
        # complete attempt directory makes success/failure publication atomic:
        # readers never observe a mixture of old pages/tables and new files.
        staging_dir.replace(output_dir)
    finally:
        if lock_acquired:
            os.close(lock_descriptor)
        # When called from run_worker/summarize_timeout, caller owns
        # the descriptor lifetime.


def _cleanup_staging_dir(staging_dir: Path) -> None:
    if staging_dir.exists() and staging_dir.is_dir() and not staging_dir.is_symlink():
        shutil.rmtree(staging_dir)


def _write_quarantine_readme(quarantine_path: Path) -> None:
    """Create the quarantine note without ever following a nested symlink."""

    readme_path = quarantine_path / QUARANTINE_README_NAME
    try:
        existing = os.lstat(readme_path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            # ``unlink`` removes the directory entry itself and does not touch
            # a target outside the quarantine tree.
            readme_path.unlink()
        else:
            # A unique quarantine path should not already contain our marker.
            # Refuse to overwrite a regular file (or any other object) rather
            # than making assumptions about stale user data.
            raise FileExistsError(
                f"quarantine marker already exists: {readme_path}"
            )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    descriptor = os.open(readme_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(QUARANTINE_README_TEXT)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _remove_path_without_following_symlinks(path: Path) -> None:
    """Remove one retention candidate while treating symlinks as leaves."""

    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        path.unlink()
        return
    for child in path.iterdir():
        _remove_path_without_following_symlinks(child)
    path.rmdir()


def _prune_quarantine_retention(output_dir: Path) -> list[str]:
    """Keep only the newest bounded set of this job's quarantine siblings."""

    prefix = f".{output_dir.name}.vlm_quarantine_"
    parent = output_dir.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        return []
    candidates = [
        candidate
        for candidate in parent.iterdir()
        if candidate.name.startswith(prefix) and candidate.parent == parent
    ]

    def newest_key(candidate: Path) -> tuple[int, str]:
        try:
            return (int(os.lstat(candidate).st_mtime_ns), candidate.name)
        except OSError:
            return (-1, candidate.name)

    candidates.sort(key=newest_key, reverse=True)
    removed: list[str] = []
    for candidate in candidates[QUARANTINE_RETENTION_COUNT:]:
        try:
            _remove_path_without_following_symlinks(candidate)
            removed.append(candidate.name)
        except OSError:
            # Retention is best effort.  A concurrent reader/owner or a
            # permission boundary must never make us delete outside the
            # explicitly matched quarantine sibling.
            continue
    return removed


def _quarantine_stale_output_dir(output_dir: Path) -> Path | None:
    """Move an old job directory aside as one atomic, inspectable unit."""

    if not _path_exists(output_dir):
        return None
    quarantine_path = output_dir.parent / (
        f".{output_dir.name}.vlm_quarantine_{os.getpid()}_{time.time_ns()}"
    )
    output_dir.replace(quarantine_path)
    if quarantine_path.is_dir() and not quarantine_path.is_symlink():
        _write_quarantine_readme(quarantine_path)
    _prune_quarantine_retention(output_dir)
    return quarantine_path


def _quarantine_stale_contract_outputs(output_dir: Path) -> list[str]:
    """Backward-compatible wrapper for callers of the old helper name.

    The previous implementation moved only a few document files and therefore
    allowed stale pages/tables to survive.  Keep the symbol for external review
    scripts, but give it the new all-assets quarantine semantics.
    """

    quarantine_path = _quarantine_stale_output_dir(output_dir)
    return [quarantine_path.name] if quarantine_path is not None else []


def _remove_orphan_staging_dirs(output_root: Path, job_id: str) -> list[str]:
    """Remove abandoned staging attempts after a killed worker timeout."""

    removed: list[str] = []
    prefix = f".{job_id}.vlm_staging_"
    if not output_root.exists() or not output_root.is_dir():
        return removed
    for candidate in output_root.iterdir():
        if not candidate.name.startswith(prefix):
            continue
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            removed.append(candidate.name)
        except OSError:
            # A concurrent attempt owns the directory; leave it intact rather
            # than risking deletion of a live worker's evidence.
            continue
    return removed


def _normalise_identity_reference(
    identity: dict[str, object], output_dir: Path
) -> dict[str, object]:
    normalized = dict(identity)
    normalized["input_file_reference"] = str(output_dir / SOURCE_REFERENCE_PATH)
    return normalized


def _annotate_attempt_identity(
    identity: dict[str, object],
    output_dir: Path,
    *,
    previous_source_verified: bool | None,
    source_recreated: bool,
    quarantine_path: Path | None,
) -> dict[str, object]:
    normalized = _normalise_identity_reference(identity, output_dir)
    normalized["input_file_reference_recreated"] = source_recreated
    if previous_source_verified is not None:
        normalized["previous_source_reference_verified"] = previous_source_verified
    if quarantine_path is not None:
        normalized["stale_output_quarantine"] = str(quarantine_path)
    return normalized


def _write_attempt_failure(
    attempt_dir: Path,
    output_dir: Path,
    input_identity: dict[str, object],
    model_used: str,
    warnings: list[str],
    errors: list[str],
    runtime: float,
    *,
    route: str = "B_evaluation_only",
    preserve_source: bool = False,
    extra_metadata: dict[str, object] | None = None,
) -> None:
    """Write only a current, explicitly failed contract to ``attempt_dir``."""

    if not preserve_source:
        source_path = attempt_dir / SOURCE_REFERENCE_PATH
        if _path_exists(source_path):
            if source_path.is_dir() and not source_path.is_symlink():
                shutil.rmtree(source_path)
            else:
                source_path.unlink()
    metadata = {
        **_normalise_identity_reference(input_identity, output_dir),
        "parser": "docling_vlm_pipeline",
        "route": route,
        "job_id": output_dir.name,
        "output_dir": str(output_dir),
        "model_used": model_used,
        "pages_processed": 0,
        "runtime_seconds": runtime,
        "contains_html": False,
        "contains_markdown": False,
        "contains_json": False,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    status = {
        "ok": False,
        "success_class": "failure",
        "warnings": warnings,
        "errors": errors,
        "output_dir": str(output_dir),
    }
    (attempt_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (attempt_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _publish_failure_attempt(
    attempt_dir: Path,
    output_dir: Path,
    input_identity: dict[str, object],
    pdf: Path,
    model_used: str,
    warnings: list[str],
    errors: list[str],
    runtime: float,
    *,
    preserve_source: bool,
    lock_descriptor: int | None = None,
    extra_metadata: dict[str, object] | None = None,
) -> int:
    if preserve_source and not _path_exists(attempt_dir / SOURCE_REFERENCE_PATH):
        refreshed_identity, refreshed_ok = _prepare_input_file_metadata(pdf, attempt_dir)
        if refreshed_ok:
            input_identity = refreshed_identity
        else:
            preserve_source = False
    _write_attempt_failure(
        attempt_dir,
        output_dir,
        input_identity,
        model_used,
        warnings,
        errors,
        runtime,
        preserve_source=preserve_source,
        extra_metadata=extra_metadata,
    )
    _publish_staging_output(
        attempt_dir, output_dir, lock_descriptor=lock_descriptor
    )
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--artifacts-path", type=Path, default=DEFAULT_ARTIFACTS_PATH)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--document-timeout", type=float, default=1500.0)
    parser.add_argument("--worker-pdf", type=Path, default=None)
    parser.add_argument("--worker-job-id", default=None)
    return parser.parse_args()


def safe_job_id(pdf: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", pdf.stem).strip("-")
    return stem or "document"


def iter_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_nodes(child)


def label_counts(document_json: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in iter_nodes(document_json):
        if isinstance(node, dict) and isinstance(node.get("label"), str):
            label = node["label"].lower()
            counts[label] = counts.get(label, 0) + 1
    return counts


def extract_label_nodes(document_json: Any, label: str) -> list[dict[str, Any]]:
    wanted = label.lower()
    return [
        node
        for node in iter_nodes(document_json)
        if isinstance(node, dict) and str(node.get("label", "")).lower() == wanted
    ]


def table_grid(table: dict[str, Any]) -> list[list[str]]:
    cells = ((table.get("data") or {}).get("table_cells") or [])
    max_row = 0
    max_col = 0
    for cell in cells:
        max_row = max(max_row, int(cell.get("end_row_offset_idx") or 0))
        max_col = max(max_col, int(cell.get("end_col_offset_idx") or 0))
    grid = [["" for _ in range(max_col)] for _ in range(max_row)]
    for cell in cells:
        row = int(cell.get("start_row_offset_idx") or 0)
        col = int(cell.get("start_col_offset_idx") or 0)
        if 0 <= row < max_row and 0 <= col < max_col:
            grid[row][col] = str(cell.get("text") or "")
    return grid


def write_table_artifacts(output_dir: Path, tables: list[dict[str, Any]]) -> int:
    tables_dir = output_dir / "tables"
    count = 0
    for index, table in enumerate(tables, start=1):
        tables_dir.mkdir(exist_ok=True)
        (tables_dir / f"table_{index}.json").write_text(
            json.dumps(table, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        grid = table_grid(table)
        if grid:
            with (tables_dir / f"table_{index}.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                csv.writer(handle).writerows(grid)
            rows = [
                "<tr>"
                + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row)
                + "</tr>"
                for row in grid
            ]
            (tables_dir / f"table_{index}.html").write_text(
                "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
                "<table>"
                + "\n".join(rows)
                + "</table></body></html>\n",
                encoding="utf-8",
            )
        count += 1
    return count


def write_page_images(doc: Any, output_dir: Path) -> int:
    pages = getattr(doc, "pages", None) or {}
    count = 0
    pages_dir = output_dir / "pages"
    for page_no, page in pages.items():
        image_ref = getattr(page, "image", None)
        pil_image = getattr(image_ref, "pil_image", None)
        if pil_image is None:
            continue
        pages_dir.mkdir(exist_ok=True)
        pil_image.save(pages_dir / f"page_{page_no}.png")
        count += 1
    return count


def write_review_index(output_dir: Path, metadata: dict[str, Any], status: dict[str, Any]) -> None:
    def links(pattern: str) -> str:
        items = sorted(output_dir.glob(pattern))
        return "".join(
            f'<li><a href="{html.escape(str(path.relative_to(output_dir)))}">'
            f'{html.escape(str(path.relative_to(output_dir)))}</a></li>'
            for path in items
            if path.is_file()
        )

    warnings = "".join(
        f"<li>{html.escape(str(warning))}</li>" for warning in status.get("warnings", [])
    )
    output_dir.joinpath("review_index.html").write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
        f"<h1>VLM review: {html.escape(str(metadata['job_id']))}</h1>"
        '<p><a href="document.html">document.html</a> | '
        '<a href="document.md">document.md</a> | '
        '<a href="document.json">document.json</a></p>'
        f"<h2>Warnings</h2><ul>{warnings}</ul>"
        f"<h2>Pages</h2><ul>{links('pages/page_*.png')}</ul>"
        f"<h2>Tables</h2><ul>{links('tables/table_*.*')}</ul>"
        "</body></html>\n",
        encoding="utf-8",
    )


def model_selection(artifacts_path: Path) -> tuple[str, list[str], bool]:
    warnings: list[str] = []
    granite_cache = artifacts_path / "ibm-granite--granite-docling-258M-mlx"
    if granite_cache.exists():
        return "granite_docling_mlx", warnings, True
    warnings.append(f"missing_required_local_mlx_model_cache:{granite_cache}")
    return "granite_docling_mlx", warnings, False


def run_worker(args: argparse.Namespace) -> int:
    assert args.worker_pdf is not None
    pdf = args.worker_pdf
    job_id = args.worker_job_id or safe_job_id(pdf)
    output_dir = args.output_root / job_id
    model_used, warnings, model_available = model_selection(args.artifacts_path)
    start = time.perf_counter()
    lock_descriptor: int | None = None

    try:
        lock_descriptor, _ = _acquire_publish_lock(output_dir)
    except FileExistsError:
        return 1

    try:
        # Inspect the old source before moving the complete directory aside.  A
        # mismatch is reported as ``existing_mismatch`` but the old source is never
        # reused or left in the active output directory.
        old_identity, old_reference_ok = _probe_existing_source_reference(pdf, output_dir)
        stale_quarantine: Path | None = None
        try:
            stale_quarantine = _quarantine_stale_output_dir(output_dir)
        except OSError as exc:
            identity = old_identity or {
                "input_file": str(pdf),
                "input_file_readable": _is_regular_and_readable(pdf),
                "input_file_reference": str(output_dir / SOURCE_REFERENCE_PATH),
                "input_file_reference_verified": False,
                "input_file_reference_mode": "unavailable",
                "input_file_reference_error": f"output_quarantine_failed:{exc}",
            }
            failure_dir = _create_staging_dir(output_dir)
            _write_attempt_failure(
                failure_dir,
                output_dir,
                identity,
                model_used,
                warnings,
                [f"stale output quarantine failed: {exc}"],
                time.perf_counter() - start,
            )
            _publish_staging_output(failure_dir, output_dir, lock_descriptor=lock_descriptor)
            return 1

        if stale_quarantine is not None:
            warnings = warnings + [f"stale_output_quarantined:{stale_quarantine.name}"]

        # An old source mismatch is a hard failure.  Publishing a failure-only
        # directory (without source.pdf) prevents downstream code from treating
        # the stale source as evidence for a later retry.
        if old_identity is not None and not old_reference_ok:
            failure_dir = _create_staging_dir(output_dir)
            old_identity = _annotate_attempt_identity(
                old_identity,
                output_dir,
                previous_source_verified=False,
                source_recreated=False,
                quarantine_path=stale_quarantine,
            )
            _write_attempt_failure(
                failure_dir,
                output_dir,
                old_identity,
                model_used,
                warnings,
                [
                    "source identity verification failed: "
                    f"{old_identity.get('input_file_reference_error', 'unknown')}"
                ],
                time.perf_counter() - start,
                preserve_source=False,
            )
            _publish_staging_output(failure_dir, output_dir, lock_descriptor=lock_descriptor)
            return 1

        work_dir = _create_staging_dir(output_dir)
        input_identity, reference_ok = _prepare_input_file_metadata(pdf, work_dir)
        if old_identity is not None and old_reference_ok and reference_ok:
            # Preserve the audit fact that the previous source.pdf was verified,
            # while the active source itself is freshly materialized in staging.
            input_identity["input_file_reference_mode"] = "existing"
        input_identity = _annotate_attempt_identity(
            input_identity,
            output_dir,
            previous_source_verified=old_reference_ok if old_identity is not None else None,
            source_recreated=reference_ok,
            quarantine_path=stale_quarantine,
        )
        if not reference_ok:
            _write_attempt_failure(
                work_dir,
                output_dir,
                input_identity,
                model_used,
                warnings,
                [
                    "source identity verification failed: "
                    f"{input_identity.get('input_file_reference_error', 'unknown')}"
                ],
                time.perf_counter() - start,
                preserve_source=False,
            )
            _publish_staging_output(work_dir, output_dir, lock_descriptor=lock_descriptor)
            return 1

        if not model_available:
            _write_attempt_failure(
                work_dir,
                output_dir,
                input_identity,
                model_used,
                warnings,
                ["Required local MLX VLM model cache is missing; not downloading."],
                time.perf_counter() - start,
                preserve_source=True,
            )
            _publish_staging_output(work_dir, output_dir, lock_descriptor=lock_descriptor)
            return 1

        source_for_converter = work_dir / SOURCE_REFERENCE_PATH
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("DOCLING_ARTIFACTS_PATH", str(args.artifacts_path))

        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
            from docling.datamodel.vlm_engine_options import MlxVlmEngineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.pipeline.vlm_pipeline import VlmPipeline
            from docling_core.types.doc import ImageRefMode

            pipeline_options = VlmPipelineOptions(
                artifacts_path=args.artifacts_path,
                document_timeout=args.document_timeout,
                images_scale=2.0,
                generate_page_images=True,
                generate_picture_images=True,
                vlm_options=VlmConvertOptions.from_preset(
                    "granite_docling", engine_options=MlxVlmEngineOptions()
                ),
            )
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_cls=VlmPipeline,
                        pipeline_options=pipeline_options,
                    )
                }
            )
            result = converter.convert(source_for_converter)
            doc = result.document
            artifacts_dir = work_dir / "artifacts"
            doc.save_as_markdown(
                work_dir / "document.md",
                artifacts_dir=artifacts_dir,
                image_mode=ImageRefMode.REFERENCED,
            )
            doc.save_as_html(
                work_dir / "document.html",
                artifacts_dir=artifacts_dir,
                image_mode=ImageRefMode.REFERENCED,
            )
            doc.save_as_json(
                work_dir / "document.json",
                artifacts_dir=artifacts_dir,
                image_mode=ImageRefMode.REFERENCED,
                indent=2,
            )
            document_json = doc.export_to_dict()
            labels = label_counts(document_json)
            tables = extract_label_nodes(document_json, "table")
            table_artifact_count = write_table_artifacts(work_dir, tables)
            page_image_count = write_page_images(doc, work_dir)
            runtime = time.perf_counter() - start
            output_contains_html = (work_dir / "document.html").exists()
            output_contains_markdown = (work_dir / "document.md").exists()
            output_contains_json = (work_dir / "document.json").exists()
            metadata = {
                **input_identity,
                "parser": "docling_vlm_pipeline",
                "route": "B_evaluation_only",
                "job_id": job_id,
                "output_dir": str(output_dir),
                "model_used": model_used,
                "pipeline_cls": "VlmPipeline",
                "pages_processed": len(getattr(doc, "pages", {}) or {}),
                "runtime_seconds": runtime,
                "label_counts": labels,
                "table_count": len(tables),
                "formula_count": labels.get("formula", 0),
                "picture_count": labels.get("picture", 0),
                "page_image_count": page_image_count,
                "table_artifact_count": table_artifact_count,
                "contains_html": output_contains_html,
                "contains_markdown": output_contains_markdown,
                "contains_json": output_contains_json,
            }
            status = {
                "ok": True,
                "success_class": "success",
                "warnings": warnings,
                "errors": [],
                "output_dir": str(output_dir),
            }
            write_review_index(work_dir, metadata, status)
            (work_dir / "metadata.json").write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            (work_dir / "status.json").write_text(
                json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            _publish_staging_output(work_dir, output_dir, lock_descriptor=lock_descriptor)
            return 0
        except Exception as exc:
            runtime = time.perf_counter() - start
            failure_dir = _create_staging_dir(output_dir)
            failure_identity, failure_reference_ok = _prepare_input_file_metadata(
                source_for_converter, failure_dir
            )
            failure_identity = _annotate_attempt_identity(
                failure_identity,
                output_dir,
                previous_source_verified=old_reference_ok if old_identity is not None else None,
                source_recreated=failure_reference_ok,
                quarantine_path=stale_quarantine,
            )
            _publish_failure_attempt(
                failure_dir,
                output_dir,
                failure_identity,
                source_for_converter,
                model_used,
                warnings,
                [f"{exc.__class__.__name__}: {exc}"],
                runtime,
                preserve_source=failure_reference_ok,
                lock_descriptor=lock_descriptor,
                extra_metadata={
                    "pipeline_cls": "VlmPipeline",
                    "traceback": traceback.format_exc(),
                },
            )
            _cleanup_staging_dir(work_dir)
            return 1
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def summarize_row(pdf: Path, job_id: str, output_dir: Path, elapsed: float) -> dict[str, Any]:
    metadata = load_json(output_dir / "metadata.json")
    status = load_json(output_dir / "status.json")
    labels = metadata.get("label_counts") or {}
    return {
        "input_filename": pdf.name,
        "input_path": str(pdf),
        "job_id": job_id,
        "output_dir": str(output_dir),
        "model_used": metadata.get("model_used"),
        "pages_processed": metadata.get("pages_processed"),
        "ok": bool(status.get("ok")),
        "success_class": status.get("success_class") or "failure",
        "runtime_seconds": metadata.get("runtime_seconds", elapsed),
        "warnings": status.get("warnings") or [],
        "failure_reason": "; ".join(status.get("errors") or []) or None,
        "contains_html": bool(metadata.get("contains_html")),
        "contains_markdown": bool(metadata.get("contains_markdown")),
        "contains_json": bool(metadata.get("contains_json")),
        "table_count": metadata.get("table_count") or labels.get("table", 0),
        "formula_count": metadata.get("formula_count") or labels.get("formula", 0),
        "picture_count": metadata.get("picture_count") or labels.get("picture", 0),
        "page_image_count": metadata.get("page_image_count", 0),
    }


def summarize_timeout(pdf: Path, job_id: str, output_dir: Path, elapsed: float) -> dict[str, Any]:
    output_root = output_dir.parent
    try:
        lock_descriptor, _ = _acquire_publish_lock(output_dir)
    except FileExistsError:
        row = summarize_row(pdf, job_id, output_dir, elapsed)
        row["ok"] = False
        row["success_class"] = "failure"
        row["warnings"] = row.get("warnings") or []
        row["failure_reason"] = "publish lock is held"
        return row

    try:
        _remove_orphan_staging_dirs(output_root, job_id)
        old_identity, old_reference_ok = _probe_existing_source_reference(pdf, output_dir)
        warnings: list[str] = []
        stale_quarantine = _quarantine_stale_output_dir(output_dir)
        if stale_quarantine is not None:
            warnings.append(f"stale_output_quarantined:{stale_quarantine.name}")

        attempt_dir = _create_staging_dir(output_dir)
        if old_identity is not None and not old_reference_ok:
            identity = _annotate_attempt_identity(
                old_identity,
                output_dir,
                previous_source_verified=False,
                source_recreated=False,
                quarantine_path=stale_quarantine,
            )
            errors = [
                "source identity verification failed: "
                f"{identity.get('input_file_reference_error', 'unknown')}"
            ]
            _write_attempt_failure(
                attempt_dir,
                output_dir,
                identity,
                "granite_docling_mlx",
                warnings,
                errors,
                elapsed,
                preserve_source=False,
            )
        else:
            input_identity, reference_ok = _prepare_input_file_metadata(pdf, attempt_dir)
            input_identity = _annotate_attempt_identity(
                input_identity,
                output_dir,
                previous_source_verified=old_reference_ok if old_identity is not None else None,
                source_recreated=reference_ok,
                quarantine_path=stale_quarantine,
            )
            if not reference_ok:
                _write_attempt_failure(
                    attempt_dir,
                    output_dir,
                    input_identity,
                    "granite_docling_mlx",
                    warnings,
                    [
                        "source identity verification failed: "
                        f"{input_identity.get('input_file_reference_error', 'unknown')}"
                    ],
                    elapsed,
                    preserve_source=False,
                )
            else:
                _write_attempt_failure(
                    attempt_dir,
                    output_dir,
                    input_identity,
                    "granite_docling_mlx",
                    warnings,
                    [f"timeout after {elapsed:.1f}s"],
                    elapsed,
                    preserve_source=True,
                )
                status_path = attempt_dir / "status.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                status["success_class"] = "timeout"
                status_path.write_text(
                    json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
                )
        _publish_staging_output(
            attempt_dir, output_dir, lock_descriptor=lock_descriptor
        )
        return summarize_row(pdf, job_id, output_dir, elapsed)
    finally:
        os.close(lock_descriptor)


def write_markdown_summary(output_root: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Docling VLM Full Directory Review",
        "",
        "Route: B evaluation only. This does not replace Route A.",
        f"PDF count: {len(rows)}",
        f"Completed: {sum(1 for row in rows if row['ok'])}",
        f"Failed: {sum(1 for row in rows if not row['ok'])}",
        f"Timeouts: {sum(1 for row in rows if row['success_class'] == 'timeout')}",
        "",
        "| PDF | job_id | ok | class | model | pages | runtime | outputs | tables | formulas | images | output | warnings/failure |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        outputs = ",".join(
            name
            for name, present in [
                ("html", row["contains_html"]),
                ("md", row["contains_markdown"]),
                ("json", row["contains_json"]),
            ]
            if present
        )
        warnings = row["failure_reason"] or "; ".join(row.get("warnings") or [])
        lines.append(
            "| {pdf} | {job} | {ok} | {cls} | {model} | {pages} | {runtime:.1f}s | "
            "{outputs} | {tables} | {formulas} | {images} | {out} | {warnings} |".format(
                pdf=row["input_filename"],
                job=row["job_id"],
                ok=row["ok"],
                cls=row["success_class"],
                model=row["model_used"],
                pages=row["pages_processed"],
                runtime=float(row["runtime_seconds"] or 0.0),
                outputs=outputs or "none",
                tables=row["table_count"],
                formulas=row["formula_count"],
                images=row["page_image_count"],
                out=row["output_dir"],
                warnings=str(warnings).replace("|", "\\|")[:500],
            )
        )
    (output_root / "run_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_worker_python(python_bin: str) -> str | None:
    """Return a clear error when the selected worker cannot import Docling."""
    try:
        result = subprocess.run(
            [python_bin, "-c", "import docling"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{python_bin}: {exc}"
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "import docling failed"
    return f"{python_bin}: {detail}"


def run_batch(args: argparse.Namespace) -> int:
    args.output_root.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(args.input_dir.glob("*.pdf"))
    rows: list[dict[str, Any]] = []
    for index, pdf in enumerate(pdfs, start=1):
        job_id = safe_job_id(pdf)
        output_dir = args.output_root / job_id
        stdout_path = args.output_root / f"{job_id}.vlm_stdout.txt"
        stderr_path = args.output_root / f"{job_id}.vlm_stderr.txt"
        cmd = [
            args.python,
            str(Path(__file__).resolve()),
            "--output-root",
            str(args.output_root),
            "--artifacts-path",
            str(args.artifacts_path),
            "--document-timeout",
            str(args.document_timeout),
            "--worker-pdf",
            str(pdf),
            "--worker-job-id",
            job_id,
        ]
        print(f"[{index}/{len(pdfs)}] {pdf.name}", flush=True)
        start = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=args.timeout_seconds,
                check=False,
            )
            elapsed = time.perf_counter() - start
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
            rows.append(summarize_row(pdf, job_id, output_dir, elapsed))
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "", encoding="utf-8")
            rows.append(summarize_timeout(pdf, job_id, output_dir, elapsed))

        (args.output_root / "run_summary.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        write_markdown_summary(args.output_root, rows)
    return 0


def main() -> int:
    args = parse_args()
    if args.worker_pdf is not None:
        return run_worker(args)
    worker_error = validate_worker_python(args.python)
    if worker_error:
        print(f"Worker Python preflight failed: {worker_error}", file=sys.stderr)
        return 2
    return run_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())
