#!/usr/bin/env python3
"""Run Docling Serve parity adapter over a directory of PDFs.

This is a review-output helper, not a production n8n integration. It calls the
repo-backed parity adapter once per PDF, continues after failures, and writes
top-level run summaries for manual inspection.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import html
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any


class PreflightError(ValueError):
    """An input/output preflight check failed before conversion started."""


class ArtifactError(ValueError):
    """A generated status/metadata artifact failed its shape contract."""


MAX_JOB_ID_LENGTH = 200


def _absolute_path(path: Path) -> Path:
    """Return an absolute lexical path without resolving symlinks.

    Keeping the final path component unresolved is intentional: the corpus
    lock below must notice if a regular input is replaced by a symlink between
    preflight and conversion.
    """

    return Path(os.path.abspath(os.fspath(path)))


def _textualize(value: object) -> str:
    """Decode subprocess output regardless of TimeoutExpired's value type."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    return str(value)


def _markdown_cell(value: object) -> str:
    """Escape dynamic Markdown table content without permitting row breaks."""

    return _textualize(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a summary through a unique same-directory O_EXCL temp and replace it."""

    path = _absolute_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=os.fspath(path.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
    )


def _positive_int_argument(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int_argument(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


class OutputRootLock:
    """A non-blocking, process-exclusive lock associated with an output root."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = _absolute_path(output_root)
        # Keep the lock beside (rather than inside) the fresh output root so
        # the root remains empty until the first actual summary is published.
        self.path = self.output_root.parent / f".{self.output_root.name}.batch.lock"
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None

    def acquire(self) -> "OutputRootLock":
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                os.fspath(self.path),
                os.O_RDWR | os.O_CREAT | nofollow,
                0o600,
            )
        except OSError as exc:
            raise PreflightError(f"cannot open output-root lock {self.path}: {exc}") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(descriptor)
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {
                getattr(errno, "EACCES", None),
                getattr(errno, "EAGAIN", None),
            }:
                raise PreflightError(f"output root is already locked: {self.output_root}") from exc
            raise PreflightError(f"cannot lock output root {self.output_root}: {exc}") from exc
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            self._identity = (
                os.fstat(descriptor).st_dev,
                os.fstat(descriptor).st_ino,
            )
            self._descriptor = descriptor
            return self
        except OSError:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            # Unlink our exact inode while the descriptor is still locked.
            # Unlocking first permits another process to acquire the old path
            # before this owner removes it, creating a lock-generation race.
            try:
                current = self.path.lstat()
                if self._identity == (current.st_dev, current.st_ino):
                    self.path.unlink()
            except (FileNotFoundError, OSError):
                pass
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                self._descriptor = None
            self._identity = None

    def __enter__(self) -> "OutputRootLock":
        return self.acquire()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline/test_pdfs"),
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--expected-count",
        type=_positive_int_argument,
        default=None,
        help="Require exactly this many regular, non-symlink PDFs (a positive integer).",
    )
    parser.add_argument("--serve-url", default="http://127.0.0.1:5001")
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=_positive_int_argument, default=1800)
    parser.add_argument("--http-retries", type=_nonnegative_int_argument, default=3)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--formula-second-pass-policy",
        choices=["off", "review", "apply", "apply-all"],
        default="off",
    )
    parser.add_argument(
        "--formula-second-pass-route-b-root",
        type=Path,
        default=None,
        help="Directory containing per-sample Route B/VLM outputs named by PDF stem.",
    )
    parser.add_argument(
        "--formula-second-pass-review-candidate-root",
        action="append",
        default=[],
        help=(
            "Optional per-sample review-only candidate root as LABEL=DIR or DIR; "
            "each sample uses <DIR>/<job-id>."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-guarded-fallback-root",
        action="append",
        default=[],
        help=(
            "Optional per-sample guarded fallback root as LABEL=DIR or DIR; "
            "each sample uses <DIR>/<job-id>."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-guarded-fallback-eq",
        action="append",
        type=int,
        default=[],
        help="Reviewed equation number allowed to use guarded fallback replacement.",
    )
    parser.add_argument(
        "--cn-ocr-parity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Forward CN OCRMac parity fallback options to each adapter invocation.",
    )
    parser.add_argument(
        "--cn-ocr-request-shape",
        choices=["preset", "custom"],
        default="preset",
    )
    parser.add_argument("--cn-ocr-chunk-size", type=_positive_int_argument, default=1)
    return parser.parse_args()


def safe_job_id(pdf: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", pdf.stem).strip("._-")
    if stem in {"", ".", ".."}:
        stem = "document"
    if len(stem) > MAX_JOB_ID_LENGTH:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
        prefix_length = MAX_JOB_ID_LENGTH - len(digest) - 1
        stem = f"{stem[:prefix_length]}-{digest}".strip("._-")
    # The substitution above cannot normally create a separator, but keep the
    # contract explicit because job ids become output-directory components.
    if Path(stem).name != stem or len(Path(stem).parts) != 1:
        return "document"
    return stem


def _fingerprint_pdf(path: Path) -> dict[str, Any]:
    """Return size/SHA-256 for one regular, non-symlink PDF.

    O_NOFOLLOW plus a final lstat comparison prevents a path replacement
    during hashing from silently turning the corpus lock into a different
    input. The adapter still takes its own immutable snapshot before parsing.
    """

    path = _absolute_path(path)
    if not path.name.casefold().endswith(".pdf"):
        raise PreflightError(f"not a PDF: {path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), os.O_RDONLY | nofollow)
    except OSError as exc:
        raise PreflightError(f"cannot open regular non-symlink PDF {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            initial = os.fstat(handle.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise PreflightError(f"not a regular PDF: {path}")
            digest = hashlib.sha256()
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            final_descriptor = os.fstat(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        final_path = path.lstat()
    except OSError as exc:
        raise PreflightError(f"PDF disappeared while fingerprinting {path}: {exc}") from exc
    if stat.S_ISLNK(final_path.st_mode) or not stat.S_ISREG(final_path.st_mode):
        raise PreflightError(f"PDF is no longer a regular non-symlink file: {path}")
    identity_fields = ("st_dev", "st_ino", "st_size")
    if any(
        getattr(initial, field) != getattr(final_descriptor, field)
        or getattr(initial, field) != getattr(final_path, field)
        for field in identity_fields
    ):
        raise PreflightError(f"PDF changed while fingerprinting: {path}")
    return {
        "input_size_bytes": int(initial.st_size),
        "input_sha256": digest.hexdigest(),
    }


def _scan_pdf_members(input_dir: Path) -> tuple[list[Path], tuple[tuple[str, str], ...]]:
    """Return the locked direct-child PDF list and its name/type signature."""

    input_dir = _absolute_path(input_dir)
    try:
        directory_stat = input_dir.lstat()
    except FileNotFoundError as exc:
        raise PreflightError(f"input directory does not exist: {input_dir}") from exc
    except OSError as exc:
        raise PreflightError(f"cannot inspect input directory {input_dir}: {exc}") from exc
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise PreflightError(f"input path is not a real directory: {input_dir}")

    try:
        entries = sorted(input_dir.iterdir(), key=lambda item: (item.name.casefold(), item.name))
    except OSError as exc:
        raise PreflightError(f"cannot list input directory {input_dir}: {exc}") from exc
    pdfs: list[Path] = []
    signature: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.name.casefold().endswith(".pdf"):
            continue
        try:
            entry_stat = entry.lstat()
        except OSError as exc:
            raise PreflightError(
                f"PDF member disappeared while scanning {entry}: {exc}"
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
            raise PreflightError(
                f"PDF member must be a regular non-symlink file: {entry}"
            )
        pdfs.append(_absolute_path(entry))
        signature.append((entry.name, "regular"))
    if not pdfs:
        raise PreflightError(
            f"input directory contains no regular non-symlink PDF: {input_dir}"
        )
    return pdfs, tuple(signature)


def _discover_pdfs(input_dir: Path) -> list[Path]:
    """Find direct child PDFs that are regular files and not symlinks."""

    return _scan_pdf_members(input_dir)[0]


def _validate_job_ids(pdfs: list[Path]) -> dict[Path, str]:
    ids: dict[Path, str] = {}
    seen: dict[str, Path] = {}
    for pdf in pdfs:
        job_id = safe_job_id(pdf)
        if (
            not job_id
            or job_id in {".", ".."}
            or Path(job_id).name != job_id
            or len(Path(job_id).parts) != 1
        ):
            raise PreflightError(f"unsafe job id for input {pdf}: {job_id!r}")
        folded = job_id.casefold()
        previous = seen.get(folded)
        if previous is not None:
            raise PreflightError(
                "case-insensitive job-id collision: "
                f"{previous.name} and {pdf.name} both map to {job_id!r}"
            )
        seen[folded] = pdf
        ids[pdf] = job_id
    # These names are written by the batch helper itself. Refusing a job id
    # that would alias one of them keeps a PDF output directory from being
    # overwritten by a summary or the manual-review index.
    reserved = {
        "run_summary.json",
        "run_summary.md",
        "all_testpdf_qc_summary.md",
        "manual_review",
        "manual_review.md",
    }
    for pdf, job_id in ids.items():
        if job_id.casefold() in reserved:
            raise PreflightError(
                f"job id collides with a batch output component: {job_id!r} ({pdf.name})"
            )
    generated_files = {
        component.casefold()
        for job_id in ids.values()
        for component in (
            f"{job_id}.adapter_stdout.json",
            f"{job_id}.adapter_stderr.txt",
        )
    }
    for pdf, job_id in ids.items():
        if job_id.casefold() in generated_files:
            raise PreflightError(
                f"job id collides with a batch capture file: {job_id!r} ({pdf.name})"
            )
    return ids


def _preflight_inputs(
    input_dir: Path,
    expected_count: int | None = None,
) -> tuple[
    list[Path],
    dict[Path, str],
    dict[Path, dict[str, Any]],
    tuple[tuple[str, str], ...],
]:
    if expected_count is not None and expected_count <= 0:
        raise PreflightError("--expected-count must be a positive integer")
    pdfs, member_signature = _scan_pdf_members(input_dir)
    if expected_count is not None and len(pdfs) != expected_count:
        raise PreflightError(
            f"--expected-count={expected_count} does not match PDF count {len(pdfs)}"
        )
    ids = _validate_job_ids(pdfs)
    fingerprints: dict[Path, dict[str, Any]] = {}
    by_sha: dict[str, Path] = {}
    for pdf in pdfs:
        fingerprint = _fingerprint_pdf(pdf)
        digest = str(fingerprint["input_sha256"])
        previous = by_sha.get(digest)
        if previous is not None:
            raise PreflightError(
                "duplicate PDF content (same SHA-256): "
                f"{previous.name} and {pdf.name} ({digest})"
            )
        by_sha[digest] = pdf
        fingerprints[pdf] = fingerprint
    return pdfs, ids, fingerprints, member_signature


def _prepare_output_root(output_root: Path) -> Path:
    """Create or validate a fresh, real, empty output directory."""

    output_root = _absolute_path(output_root)
    try:
        output_root.lstat()
    except FileNotFoundError:
        try:
            output_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # A concurrent creator wins only if the resulting path still
            # satisfies the same strict checks below.
            pass
        except OSError as exc:
            raise PreflightError(f"cannot create output root {output_root}: {exc}") from exc
    except OSError as exc:
        raise PreflightError(f"cannot inspect output root {output_root}: {exc}") from exc

    try:
        current = output_root.lstat()
    except OSError as exc:
        raise PreflightError(f"output root is unavailable: {output_root}: {exc}") from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise PreflightError(f"output root must be a real directory: {output_root}")
    try:
        next(output_root.iterdir())
    except StopIteration:
        return output_root
    except OSError as exc:
        raise PreflightError(f"cannot inspect output root {output_root}: {exc}") from exc
    raise PreflightError(f"output root must be empty: {output_root}")


def _validate_runtime_args(args: argparse.Namespace) -> None:
    """Validate numeric options for both CLI and direct/programmatic callers."""

    checks = (
        ("timeout_seconds", getattr(args, "timeout_seconds", None), 0, "greater than zero"),
        ("http_retries", getattr(args, "http_retries", None), -1, "non-negative"),
        ("cn_ocr_chunk_size", getattr(args, "cn_ocr_chunk_size", None), 0, "greater than zero"),
    )
    for name, value, lower_bound, description in checks:
        if isinstance(value, bool) or not isinstance(value, int) or value <= lower_bound:
            raise PreflightError(f"{name} must be {description}")
    expected_count = getattr(args, "expected_count", None)
    if expected_count is not None and (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count <= 0
    ):
        raise PreflightError("expected_count must be greater than zero")


def load_json(path: Path) -> dict[str, Any]:
    """Read one generated JSON artifact and require a top-level object."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read valid JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactError(
            f"JSON artifact must have an object root {path}, got {type(value).__name__}"
        )
    return value


def _optional_object(parent: dict[str, Any], key: str, *, artifact: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ArtifactError(
            f"{artifact}.{key} must be an object, got {type(value).__name__}"
        )
    return value


def _optional_list(
    parent: dict[str, Any],
    key: str,
    *,
    artifact: str,
    item_type: type | tuple[type, ...] | None = None,
) -> list[Any]:
    value = parent.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ArtifactError(
            f"{artifact}.{key} must be a list, got {type(value).__name__}"
        )
    if item_type is not None and any(not isinstance(item, item_type) for item in value):
        expected = (
            item_type.__name__
            if isinstance(item_type, type)
            else "/".join(item.__name__ for item in item_type)
        )
        raise ArtifactError(f"{artifact}.{key} contains a non-{expected} item")
    return value


def _optional_string_list(
    parent: dict[str, Any], key: str, *, artifact: str
) -> list[str]:
    return _optional_list(parent, key, artifact=artifact, item_type=str)


def _optional_integer_list(
    parent: dict[str, Any], key: str, *, artifact: str
) -> list[int]:
    values = _optional_list(parent, key, artifact=artifact)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ArtifactError(f"{artifact}.{key} must contain only JSON integers")
    return values


def _validate_artifact_shapes(
    metadata: dict[str, Any], status: dict[str, Any]
) -> None:
    """Validate nested containers and normalize warning/error lists."""

    _optional_object(status, "quality_signals", artifact="status")
    _optional_string_list(status, "warnings", artifact="status")
    # The adapter's failure contract permits structured error values. Keep the
    # container strict (a list) but textualize each JSON value deterministically
    # when it is rendered into a summary row.
    _optional_list(status, "errors", artifact="status")

    second_pass = _optional_object(metadata, "formula_second_pass", artifact="metadata")
    _optional_object(second_pass, "alignment_diagnostics", artifact="metadata.formula_second_pass")
    structural = _optional_object(metadata, "structural_quarantine_qc", artifact="metadata")
    _optional_object(metadata, "semantic_emphasis", artifact="metadata")
    exported_counts = structural.get("exported_structural_content_counts_by_kind")
    if exported_counts is not None and not isinstance(exported_counts, dict):
        raise ArtifactError(
            "metadata.structural_quarantine_qc."
            "exported_structural_content_counts_by_kind must be an object"
        )

    diagnostics = _optional_list(
        metadata,
        "formula_number_qc_diagnostics",
        artifact="metadata",
        item_type=dict,
    )
    for index, item in enumerate(diagnostics):
        reasons = item.get("reasons")
        if reasons is not None and (
            not isinstance(reasons, list)
            or any(not isinstance(reason, str) for reason in reasons)
        ):
            raise ArtifactError(
                f"metadata.formula_number_qc_diagnostics[{index}].reasons "
                "must be a list of strings"
            )
        safe_to_recover = item.get("safe_to_recover")
        if safe_to_recover is not None and not isinstance(safe_to_recover, bool):
            raise ArtifactError(
                f"metadata.formula_number_qc_diagnostics[{index}].safe_to_recover "
                "must be a JSON boolean"
            )
    _optional_integer_list(
        metadata,
        "formula_number_recovered_html_indexes",
        artifact="metadata",
    )


def parse_labeled_root(value: str) -> tuple[str | None, Path]:
    if "=" in value:
        label, path = value.split("=", 1)
        return label.strip() or None, Path(path)
    return None, Path(value)


def sample_source_arg(value: str, job_id: str) -> str:
    label, root = parse_labeled_root(value)
    sample_dir = root / job_id
    return f"{label}={sample_dir}" if label else str(sample_dir)


def summarize_success(
    pdf: Path,
    job_id: str,
    output_dir: Path,
    elapsed: float,
    *,
    input_size_bytes: int | None = None,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    # Keep all generated-artifact parsing inside an explicit boundary. The
    # batch runner catches ArtifactError (and unexpected shape exceptions) and
    # turns it into this PDF's failure row instead of aborting the corpus.
    metadata = load_json(output_dir / "metadata.json")
    status = load_json(output_dir / "status.json")
    _validate_artifact_shapes(metadata, status)
    second_pass = _optional_object(metadata, "formula_second_pass", artifact="metadata")
    alignment = _optional_object(
        second_pass,
        "alignment_diagnostics",
        artifact="metadata.formula_second_pass",
    )
    structural = _optional_object(
        metadata,
        "structural_quarantine_qc",
        artifact="metadata",
    )
    emphasis = _optional_object(metadata, "semantic_emphasis", artifact="metadata")
    number_diag = _optional_list(
        metadata,
        "formula_number_qc_diagnostics",
        artifact="metadata",
        item_type=dict,
    )
    recovered_numbers = _optional_integer_list(
        metadata,
        "formula_number_recovered_html_indexes",
        artifact="metadata",
    )
    status_ok = status.get("ok") is True
    raw_errors = status.get("errors") or []
    status_errors = (
        "; ".join(_textualize(item) for item in raw_errors)
        if isinstance(raw_errors, list)
        else _textualize(raw_errors)
    )
    return {
        "input_filename": pdf.name,
        "input_path": str(_absolute_path(pdf)),
        "job_id": job_id,
        "output_dir": str(_absolute_path(output_dir)),
        "input_size_bytes": input_size_bytes,
        "input_sha256": input_sha256,
        # Identity comparison is intentional: JSON strings such as \"true\"
        # must never satisfy the success contract.
        "ok": status_ok,
        "success_class": status.get("success_class"),
        "ocr_fallback_used": metadata.get("ocr_fallback_used"),
        "text_quality_gxx_count": metadata.get("text_quality_gxx_count"),
        "text_quality_gxx_density": metadata.get("text_quality_gxx_density"),
        "formula_placeholder_count": metadata.get("formula_placeholder_count"),
        "formula_count": metadata.get("formula_count"),
        "second_pass_attempted_count": second_pass.get("second_pass_attempted_count"),
        "second_pass_main_output_replaced_count": second_pass.get("replaced_count"),
        "second_pass_fallback_count": second_pass.get("fallback_count"),
        "formula_all_second_pass_attempted": alignment.get("all_formulas_attempted"),
        "formula_sequence_mismatch_count": alignment.get("sequence_mismatch_count"),
        "duplicate_equation_number_count": alignment.get("duplicate_equation_number_count"),
        "image_formula_not_converted_count": alignment.get("image_formula_not_converted_count"),
        "missing_formula_number_count": len(
            [
                item for item in number_diag
                if "display_formula_missing_equation_number" in (item.get("reasons") or [])
            ]
        ),
        "recovered_formula_number_count": len(recovered_numbers),
        "unresolved_formula_number_count": len(
            [
                item for item in number_diag
                if "display_formula_missing_equation_number" in (item.get("reasons") or [])
                and not item.get("safe_to_recover")
            ]
        ),
        "header_footer_footnote_candidate_count": structural.get("candidate_count"),
        "isolated_main_text_pollution_count": structural.get("isolated_main_text_pollution_count"),
        "exported_structural_content_count": structural.get("exported_structural_content_count"),
        "exported_structural_content_counts_by_kind": (
            structural.get("exported_structural_content_counts_by_kind") or {}
        ),
        "final_output_structural_residual_count": structural.get("final_output_residual_count"),
        "semantic_emphasis_detected_count": emphasis.get("detected_span_count"),
        "semantic_emphasis_html_count": emphasis.get("html_applied_span_count"),
        "semantic_emphasis_markdown_count": emphasis.get("markdown_applied_span_count"),
        "assembled_note_count": structural.get("assembled_note_count"),
        "note_reference_link_count": structural.get("note_reference_link_count"),
        "unresolved_note_reference_count": structural.get("unresolved_note_reference_count"),
        "unresolved_structural_note_count": structural.get("unresolved_structural_note_count"),
        "recovered_footnote_count": structural.get("recovered_footnote_count"),
        "unresolved_footnote_count": structural.get("unresolved_footnote_count"),
        "evidence_links": {
            "review_index": str(output_dir / "review_index.html"),
            "metadata": str(output_dir / "metadata.json"),
            "status": str(output_dir / "status.json"),
            "formula_second_pass": str(output_dir / "formula_second_pass" / "second_pass_summary.json"),
            "structural_content": str(output_dir / "structural_content.json"),
            "structural_regions": str(output_dir / "structural_regions.json"),
        },
        "table_count": metadata.get("table_count"),
        "image_refs_embedded": metadata.get("image_refs_embedded"),
        "markdown_image_ref_count": metadata.get("markdown_image_ref_count"),
        "warnings": _optional_string_list(status, "warnings", artifact="status"),
        "runtime_seconds": elapsed,
        "failure_reason": None
        if status_ok
        else (status_errors or "status.json.ok is not the JSON boolean true"),
    }


def summarize_failure(
    pdf: Path,
    job_id: str,
    output_dir: Path,
    elapsed: float,
    reason: str,
    timed_out: bool,
    *,
    input_size_bytes: int | None = None,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "input_filename": pdf.name,
        "input_path": str(_absolute_path(pdf)),
        "job_id": job_id,
        "output_dir": str(_absolute_path(output_dir)),
        "input_size_bytes": input_size_bytes,
        "input_sha256": input_sha256,
        "ok": False,
        "success_class": "timeout" if timed_out else "failure",
        "ocr_fallback_used": None,
        "text_quality_gxx_count": None,
        "text_quality_gxx_density": None,
        "formula_placeholder_count": None,
        "formula_count": None,
        "second_pass_attempted_count": None,
        "second_pass_main_output_replaced_count": None,
        "second_pass_fallback_count": None,
        "formula_all_second_pass_attempted": None,
        "formula_sequence_mismatch_count": None,
        "duplicate_equation_number_count": None,
        "image_formula_not_converted_count": None,
        "missing_formula_number_count": None,
        "recovered_formula_number_count": None,
        "unresolved_formula_number_count": None,
        "header_footer_footnote_candidate_count": None,
        "isolated_main_text_pollution_count": None,
        "exported_structural_content_count": None,
        "exported_structural_content_counts_by_kind": {},
        "final_output_structural_residual_count": None,
        "semantic_emphasis_detected_count": None,
        "semantic_emphasis_html_count": None,
        "semantic_emphasis_markdown_count": None,
        "assembled_note_count": None,
        "note_reference_link_count": None,
        "unresolved_note_reference_count": None,
        "unresolved_structural_note_count": None,
        "recovered_footnote_count": None,
        "unresolved_footnote_count": None,
        "evidence_links": {},
        "table_count": None,
        "image_refs_embedded": None,
        "markdown_image_ref_count": None,
        "warnings": [],
        "runtime_seconds": elapsed,
        "failure_reason": reason,
    }


def write_markdown_summary(output_root: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Docling Serve Full Directory Review",
        "",
        f"PDF count: {len(rows)}",
        f"Completed: {sum(1 for row in rows if row['ok'])}",
        f"Failed: {sum(1 for row in rows if not row['ok'])}",
        f"Timeouts: {sum(1 for row in rows if row['success_class'] == 'timeout')}",
        "",
        "| PDF | ok | class | OCR | /Gxx | density | formulas | placeholders | tables | embedded images | runtime | output | warnings/failure |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        warning_text = row["failure_reason"] or "; ".join(row.get("warnings") or [])
        lines.append(
            "| {pdf} | {ok} | {cls} | {ocr} | {gxx} | {density} | {formulas} | "
            "{placeholders} | {tables} | {images} | {runtime:.1f}s | {output} | {warning} |".format(
                pdf=_markdown_cell(row["input_filename"]),
                ok=_markdown_cell(row["ok"]),
                cls=_markdown_cell(row["success_class"]),
                ocr=_markdown_cell(row["ocr_fallback_used"]),
                gxx=_markdown_cell(row["text_quality_gxx_count"]),
                density=_markdown_cell(row["text_quality_gxx_density"]),
                formulas=_markdown_cell(row["formula_count"]),
                placeholders=_markdown_cell(row["formula_placeholder_count"]),
                tables=_markdown_cell(row["table_count"]),
                images=_markdown_cell(row["image_refs_embedded"]),
                runtime=float(row["runtime_seconds"] or 0.0),
                output=_markdown_cell(row["output_dir"]),
                warning=_markdown_cell(warning_text)[:500],
            )
        )
    _atomic_write_text(output_root / "run_summary.md", "\n".join(lines) + "\n")

    qc_lines = [
        "# All Test PDF QC Summary",
        "",
        "| PDF | formulas | second-pass attempts | main replacements | fallbacks | missing eq nums | recovered eq nums | unresolved eq nums | structure candidates | isolated pollution | recovered footnotes | unresolved footnotes | evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        evidence = (row.get("evidence_links") or {}).get("review_index") or row.get("output_dir")
        qc_lines.append(
            "| {pdf} | {formulas} | {attempts} | {replaced} | {fallbacks} | {missing} | "
            "{recovered} | {unresolved} | {struct} | {isolated} | {foot_recovered} | "
            "{foot_unresolved} | {evidence} |".format(
                pdf=_markdown_cell(row["input_filename"]),
                formulas=_markdown_cell(row.get("formula_count")),
                attempts=_markdown_cell(row.get("second_pass_attempted_count")),
                replaced=_markdown_cell(row.get("second_pass_main_output_replaced_count")),
                fallbacks=_markdown_cell(row.get("second_pass_fallback_count")),
                missing=_markdown_cell(row.get("missing_formula_number_count")),
                recovered=_markdown_cell(row.get("recovered_formula_number_count")),
                unresolved=_markdown_cell(row.get("unresolved_formula_number_count")),
                struct=_markdown_cell(row.get("header_footer_footnote_candidate_count")),
                isolated=_markdown_cell(row.get("isolated_main_text_pollution_count")),
                foot_recovered=_markdown_cell(row.get("recovered_footnote_count")),
                foot_unresolved=_markdown_cell(row.get("unresolved_footnote_count")),
                evidence=_markdown_cell(evidence),
            )
        )
    qc_lines.append("")
    qc_lines.extend(
        [
            "| PDF | all formulas attempted | sequence mismatches | duplicate eq nums | image formulas not converted |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        qc_lines.append(
            "| {pdf} | {all_attempted} | {mismatch} | {dupes} | {not_converted} |".format(
                pdf=_markdown_cell(row["input_filename"]),
                all_attempted=_markdown_cell(row.get("formula_all_second_pass_attempted")),
                mismatch=_markdown_cell(row.get("formula_sequence_mismatch_count")),
                dupes=_markdown_cell(row.get("duplicate_equation_number_count")),
                not_converted=_markdown_cell(row.get("image_formula_not_converted_count")),
            )
        )
    _atomic_write_text(
        output_root / "all_testpdf_qc_summary.md",
        "\n".join(qc_lines) + "\n",
    )


def write_manual_review_index(output_root: Path, rows: list[dict[str, Any]]) -> None:
    review_dir = output_root / "manual_review"
    review_dir.mkdir(parents=True, exist_ok=True)
    table_rows = []
    markdown_rows = []
    for row in rows:
        job_id = row["job_id"]
        prefix = f"../{job_id}"
        status_label = "OK" if row.get("ok") else "FAILED"
        output_dir = output_root / job_id
        input_path = Path(str(row.get("input_path") or "")).expanduser()
        source_link = "N/A"
        md_source_link = "N/A"
        if input_path:
            source_href = "file://" + quote(input_path.as_posix())
            source_text = html.escape(input_path.as_posix())
            source_link = (
                f'<a href="{source_href}">Source PDF</a>'
                f'<br><code>{source_text}</code>'
            )
            md_source_link = (
                f"[Source PDF]({source_href})<br>`{_markdown_cell(input_path.as_posix())}`"
            )
        html_link = (
            f'<a href="{prefix}/document.html">HTML</a>'
            if (output_dir / "document.html").exists()
            else "N/A"
        )
        md_link = (
            f'<a href="{prefix}/document.md">Markdown</a>'
            if (output_dir / "document.md").exists()
            else "N/A"
        )
        structural_content_link = (
            f'<a href="{prefix}/structural_content.json">Extracted structure</a>'
            if (output_dir / "structural_content.json").exists()
            else "Not generated"
        )
        structural_regions_link = (
            f'<a href="{prefix}/structural_regions.json">QC evidence</a>'
            if (output_dir / "structural_regions.json").exists()
            else "Not generated"
        )
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(row['input_filename'])}</td>"
            f"<td>{status_label}</td>"
            f"<td>{source_link}</td>"
            f"<td>{html_link}</td>"
            f"<td>{md_link}</td>"
            f"<td>{structural_content_link}</td>"
            f"<td>{structural_regions_link}</td>"
            "</tr>"
        )
        md_html_link = (
            f"[HTML]({job_id}/document.html)"
            if (output_dir / "document.html").exists()
            else "N/A"
        )
        md_document_link = (
            f"[Markdown]({job_id}/document.md)"
            if (output_dir / "document.md").exists()
            else "N/A"
        )
        md_structural_content_link = (
            f"[Extracted structure]({job_id}/structural_content.json)"
            if (output_dir / "structural_content.json").exists()
            else "Not generated"
        )
        md_structural_regions_link = (
            f"[QC evidence]({job_id}/structural_regions.json)"
            if (output_dir / "structural_regions.json").exists()
            else "Not generated"
        )
        markdown_rows.append(
            f"| {_markdown_cell(row['input_filename'])} | {_markdown_cell(status_label)} | "
            f"{md_source_link} | "
            f"{md_html_link} | {md_document_link} | "
            f"{md_structural_content_link} | {md_structural_regions_link} |"
        )
    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling manual review</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 90rem; padding: 0 1rem; color: #17212b; }
table { border-collapse: collapse; width: 100%%; }
th, td { border-bottom: 1px solid #cbd3da; padding: .65rem; text-align: left; }
th { background: #eef2f4; }
a { color: #075ea8; }
.summary { color: #52606d; }
</style>
</head>
<body>
<h1>Docling manual review</h1>
<p class="summary">Completed: %d / %d. Review rendered HTML first, then compare Markdown and structural evidence.</p>
<table>
<thead><tr><th>PDF</th><th>Status</th><th>Source PDF</th><th>Rendered</th><th>Markdown</th><th>Structural output</th><th>Evidence</th></tr></thead>
<tbody>%s</tbody>
</table>
</body>
</html>
""" % (
        sum(1 for row in rows if row.get("ok")),
        len(rows),
        "".join(table_rows),
    )
    _atomic_write_text(review_dir / "index.html", page)
    markdown = [
        "# Docling manual review",
        "",
        "| PDF | Status | Source PDF | Rendered | Markdown | Structural output | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *markdown_rows,
        "",
    ]
    _atomic_write_text(output_root / "MANUAL_REVIEW.md", "\n".join(markdown))


def _refresh_summaries(output_root: Path, rows: list[dict[str, Any]]) -> None:
    # Keep run_summary.json a list for compatibility with existing review
    # tooling. The other summaries are refreshed from that same row snapshot.
    _atomic_write_json(output_root / "run_summary.json", rows)
    write_markdown_summary(output_root, rows)
    write_manual_review_index(output_root, rows)


def _build_adapter_command(
    args: argparse.Namespace,
    pdf: Path,
    output_root: Path,
    job_id: str,
    *,
    input_sha256: str | None = None,
) -> list[str]:
    cmd = [
        str(args.python),
        str(args.adapter),
        "--serve-url",
        str(args.serve_url),
        "--input-file",
        str(pdf),
        "--output-root",
        str(output_root),
        "--job-id",
        job_id,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--http-retries",
        str(args.http_retries),
    ]
    if input_sha256 is not None:
        cmd.extend(["--expected-input-sha256", input_sha256])
    if args.formula_second_pass_policy != "off":
        cmd.extend(["--formula-second-pass-policy", args.formula_second_pass_policy])
        if args.formula_second_pass_route_b_root is not None:
            route_b_dir = args.formula_second_pass_route_b_root / job_id
            cmd.extend(["--formula-second-pass-route-b-dir", str(route_b_dir)])
        for value in args.formula_second_pass_review_candidate_root:
            cmd.extend(
                [
                    "--formula-second-pass-review-candidate-dir",
                    sample_source_arg(value, job_id),
                ]
            )
        for value in args.formula_second_pass_guarded_fallback_root:
            cmd.extend(
                [
                    "--formula-second-pass-guarded-fallback-dir",
                    sample_source_arg(value, job_id),
                ]
            )
        for eq_number in args.formula_second_pass_guarded_fallback_eq:
            cmd.extend(
                [
                    "--formula-second-pass-guarded-fallback-eq",
                    str(eq_number),
                ]
            )
    if args.cn_ocr_parity:
        cmd.append("--cn-ocr-parity")
        cmd.extend(["--cn-ocr-request-shape", args.cn_ocr_request_shape])
        cmd.extend(["--cn-ocr-chunk-size", str(args.cn_ocr_chunk_size)])
    return cmd


def _write_process_capture(path: Path, value: object) -> None:
    path.write_text(_textualize(value), encoding="utf-8")


def _failure_row_for_input(
    pdf: Path,
    job_id: str,
    output_dir: Path,
    fingerprint: dict[str, Any],
    elapsed: float,
    reason: str,
    timed_out: bool = False,
) -> dict[str, Any]:
    return summarize_failure(
        pdf,
        job_id,
        output_dir,
        elapsed,
        reason,
        timed_out,
        input_size_bytes=fingerprint.get("input_size_bytes"),
        input_sha256=fingerprint.get("input_sha256"),
    )


def _append_unstarted_failures(
    output_root: Path,
    rows: list[dict[str, Any]],
    pdfs: list[Path],
    job_ids: dict[Path, str],
    fingerprints: dict[Path, dict[str, Any]],
    start_index: int,
    reason: str,
) -> None:
    """Keep one summary row for every PDF in the locked initial roster."""

    for pdf in pdfs[start_index:]:
        job_id = job_ids[pdf]
        output_dir = output_root / job_id
        rows.append(
            _failure_row_for_input(
                pdf,
                job_id,
                output_dir,
                fingerprints[pdf],
                0.0,
                reason,
            )
        )
        _write_process_capture(
            output_root / f"{job_id}.adapter_stderr.txt",
            reason,
        )
        _write_process_capture(
            output_root / f"{job_id}.adapter_stdout.json",
            "",
        )
        _refresh_summaries(output_root, rows)


def _mark_batch_integrity_failure(rows: list[dict[str, Any]], reason: str) -> None:
    for row in rows:
        row["ok"] = False
        # Preserve a timeout classification: it remains useful to distinguish
        # the adapter timeout from a later corpus-integrity finding. Other
        # classes are downgraded to the aggregate failure class.
        if row.get("success_class") != "timeout":
            row["success_class"] = "failure"
        previous = _textualize(row.get("failure_reason"))
        # A row may already contain this reason (for example if a membership
        # check failed before the final rescan). Avoid growing duplicate
        # diagnostics on repeated integrity checks.
        if not previous:
            row["failure_reason"] = reason
        elif previous == reason or previous.endswith(f"; {reason}"):
            row["failure_reason"] = previous
        else:
            row["failure_reason"] = f"{previous}; {reason}"


def _run_batch_jobs(
    args: argparse.Namespace,
    output_root: Path,
    pdfs: list[Path],
    job_ids: dict[Path, str],
    fingerprints: dict[Path, dict[str, Any]],
    member_signature: tuple[tuple[str, str], ...],
) -> int:
    rows: list[dict[str, Any]] = []
    for index, pdf in enumerate(pdfs, start=1):
        job_id = job_ids[pdf]
        output_dir = output_root / job_id
        stdout_path = output_root / f"{job_id}.adapter_stdout.json"
        stderr_path = output_root / f"{job_id}.adapter_stderr.txt"
        expected_fingerprint = fingerprints[pdf]
        print(f"[{index}/{len(pdfs)}] {pdf.name}", flush=True)
        start = time.perf_counter()

        try:
            _current_pdfs, current_signature = _scan_pdf_members(args.input_dir)
        except (PreflightError, OSError) as exc:
            reason = f"corpus_membership_changed: {exc}"
            _append_unstarted_failures(
                output_root,
                rows,
                pdfs,
                job_ids,
                fingerprints,
                index - 1,
                reason,
            )
            break
        if current_signature != member_signature:
            reason = "corpus_membership_changed: direct-child PDF roster changed"
            _append_unstarted_failures(
                output_root,
                rows,
                pdfs,
                job_ids,
                fingerprints,
                index - 1,
                reason,
            )
            break

        # Corpus lock: no adapter call is allowed when an input changed after
        # the all-files preflight. The row still carries the original digest.
        try:
            current_fingerprint = _fingerprint_pdf(pdf)
        except (PreflightError, OSError) as exc:
            elapsed = time.perf_counter() - start
            reason = f"input_changed_after_preflight: {exc}"
            rows.append(
                _failure_row_for_input(
                    pdf, job_id, output_dir, expected_fingerprint, elapsed, reason
                )
            )
            _write_process_capture(stderr_path, reason)
            _write_process_capture(stdout_path, "")
            _refresh_summaries(output_root, rows)
            continue
        if current_fingerprint != expected_fingerprint:
            elapsed = time.perf_counter() - start
            reason = "input_changed_after_preflight: size/SHA-256 mismatch"
            rows.append(
                _failure_row_for_input(
                    pdf, job_id, output_dir, expected_fingerprint, elapsed, reason
                )
            )
            _write_process_capture(stderr_path, reason)
            _write_process_capture(stdout_path, "")
            _refresh_summaries(output_root, rows)
            continue

        cmd = _build_adapter_command(
            args,
            pdf,
            output_root,
            job_id,
            input_sha256=expected_fingerprint["input_sha256"],
        )
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=args.timeout_seconds + 30,
                check=False,
            )
            elapsed = time.perf_counter() - start
            stdout_text = _textualize(getattr(result, "stdout", None))
            stderr_text = _textualize(getattr(result, "stderr", None))
            _write_process_capture(stdout_path, stdout_text)
            _write_process_capture(stderr_path, stderr_text)
            if result.returncode == 0:
                try:
                    row = summarize_success(
                        pdf,
                        job_id,
                        output_dir,
                        elapsed,
                        input_size_bytes=expected_fingerprint["input_size_bytes"],
                        input_sha256=expected_fingerprint["input_sha256"],
                    )
                except Exception as exc:  # isolate malformed output to this PDF
                    row = _failure_row_for_input(
                        pdf,
                        job_id,
                        output_dir,
                        expected_fingerprint,
                        elapsed,
                        f"output summary error: {type(exc).__name__}: {exc}",
                    )
                rows.append(row)
            else:
                reason = (
                    stderr_text.strip()
                    or stdout_text.strip()
                    or f"exit {result.returncode}"
                )
                rows.append(
                    _failure_row_for_input(
                        pdf,
                        job_id,
                        output_dir,
                        expected_fingerprint,
                        elapsed,
                        reason,
                    )
                )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            stdout_text = _textualize(getattr(exc, "stdout", None))
            stderr_text = _textualize(getattr(exc, "stderr", None))
            _write_process_capture(stdout_path, stdout_text)
            _write_process_capture(stderr_path, stderr_text)
            detail = stderr_text.strip() or stdout_text.strip()
            reason = f"timeout after {args.timeout_seconds}s"
            if detail:
                reason = f"{reason}: {detail}"
            rows.append(
                _failure_row_for_input(
                    pdf,
                    job_id,
                    output_dir,
                    expected_fingerprint,
                    elapsed,
                    reason,
                    True,
                )
            )
        except OSError as exc:
            elapsed = time.perf_counter() - start
            reason = f"subprocess error: {exc}"
            _write_process_capture(stdout_path, "")
            _write_process_capture(stderr_path, reason)
            rows.append(
                _failure_row_for_input(
                    pdf,
                    job_id,
                    output_dir,
                    expected_fingerprint,
                    elapsed,
                    reason,
                )
            )

        _refresh_summaries(output_root, rows)

    integrity_reason: str | None = None
    try:
        _final_pdfs, final_signature = _scan_pdf_members(args.input_dir)
    except (PreflightError, OSError) as exc:
        integrity_reason = f"corpus_membership_changed: {exc}"
    else:
        if final_signature != member_signature:
            integrity_reason = (
                "corpus_membership_changed: direct-child PDF roster changed"
            )
        else:
            for locked_pdf in pdfs:
                try:
                    final_fingerprint = _fingerprint_pdf(locked_pdf)
                except (PreflightError, OSError) as exc:
                    integrity_reason = (
                        f"corpus_input_changed: {locked_pdf.name}: {exc}"
                    )
                    break
                if final_fingerprint != fingerprints[locked_pdf]:
                    integrity_reason = (
                        f"corpus_input_changed: {locked_pdf.name}: "
                        "size/SHA-256 mismatch"
                    )
                    break
    if integrity_reason is not None:
        if len(rows) < len(pdfs):
            _append_unstarted_failures(
                output_root,
                rows,
                pdfs,
                job_ids,
                fingerprints,
                len(rows),
                integrity_reason,
            )
        _mark_batch_integrity_failure(rows, integrity_reason)
        _refresh_summaries(output_root, rows)

    return 0 if len(rows) == len(pdfs) and all(row.get("ok") is True for row in rows) else 1


def run_batch(args: argparse.Namespace) -> int:
    """Run one fresh corpus and return 0 success, 1 conversion failure, 2 preflight."""

    try:
        _validate_runtime_args(args)
        pdfs, job_ids, fingerprints, member_signature = _preflight_inputs(
            args.input_dir,
            getattr(args, "expected_count", None),
        )
        output_root = _prepare_output_root(args.output_root)
        with OutputRootLock(output_root):
            # The lock serializes the final freshness check. A second caller
            # may have validated this root while an earlier run was finishing;
            # reject it once the lock is held instead of reusing a populated
            # output directory.
            output_root = _prepare_output_root(output_root)
            return _run_batch_jobs(
                args,
                output_root,
                pdfs,
                job_ids,
                fingerprints,
                member_signature,
            )
    except (PreflightError, OSError) as exc:
        print(f"batch review preflight failed: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    return run_batch(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
