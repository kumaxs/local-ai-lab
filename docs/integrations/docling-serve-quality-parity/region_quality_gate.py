"""Bounded, generic region-level quality evidence for the delivery adapter.

The quality-parity adapter already has specialised formula and structural gates.
This module is deliberately independent of those producers: it consumes their
published diagnostics and emits a small, deterministic inventory of region
bindings.  It never recognises a paper by name, path, or hash and it does not
rewrite document surfaces.

``regions.json`` is the detailed bounded inventory.  ``quality_signals.json``
is a compact summary suitable for callers that do not need to load every
record.  The public :func:`evaluate_regions` function is also usable by a
stand-alone post-publish validator and by the production adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import stat
import statistics
import unicodedata
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 1000
REGION_STATUSES = ("verified_semantic", "visual_only", "unresolved")
STRUCTURAL_KINDS = ("table", "algorithm", "code", "formula", "inline_math")

# Diagnostics are data from a conversion service.  Keep every traversal and
# byte read bounded even when a malformed service response is presented to the
# stand-alone validator.
MAX_JSON_BYTES = 512 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_SOURCE_PDF_BYTES = 4 * 1024 * 1024 * 1024
MAX_DOCUMENT_NODES = 100_000
MAX_TABLE_CELLS = 65_536
MAX_RESIDUAL_SURFACES = 32
MAX_LIST_ITEMS = DEFAULT_MAX_RECORDS + 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SOURCE_REF_RE = re.compile(r"[^A-Za-z0-9_.:/#@+\-]+")
_TABLE_NUMBER_RE = re.compile(
    r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|\.\d+)(?![\w.])"
)
_TABLE_RANGE_RE = re.compile(r"(?:\d\s*(?:[-–—]|to)\s*\d)|(?:\d\s*\+/-\s*\d)", re.I)
_STRUCTURAL_BODY_TOKEN_RE = re.compile(
    r"\\[A-Za-z]+|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)*|"
    r"===|!==|==|!=|<=|>=|:=|->|<-|=>|\+\+|--|\*\*|//|&&|\|\||<<|>>|[^\s]"
)

_ASSET_PREFIXES = {
    "picture_ocr": ("pages/", "pictures/"),
    "header_footer": ("pages/",),
    "picture": ("pictures/",),
    "table": ("tables/",),
    "algorithm": ("algorithms/",),
    "code": ("code_blocks/",),
    "formula": ("formulas/",),
    "inline_math": ("inline_math/",),
}

# A single evaluation pins the output directory to the directory inode opened
# here.  Every asset read and sidecar write below then uses a dup of this fd;
# replacing/renaming the path during validation cannot redirect an operation
# into an attacker-controlled directory.  ContextVar keeps nested/concurrent
# callers from sharing a mutable process-global handle.
_ROOT_CONTEXT: ContextVar[tuple[str, int, int, int] | None] = ContextVar(
    "region_quality_root_context", default=None
)


def _root_context_for(root: Path) -> tuple[str, int, int, int] | None:
    context = _ROOT_CONTEXT.get()
    if context is None:
        return None
    root_key = os.path.abspath(os.fspath(root))
    return context if context[0] == root_key else None


def _open_root_dir(root: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.fspath(root), flags)
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISDIR(stat_result.st_mode):
            raise OSError("output_root_not_directory")
        return fd, stat_result
    except Exception:
        os.close(fd)
        raise


def _root_path_matches_context(root: Path, context: tuple[str, int, int, int]) -> bool:
    try:
        stat_result = os.stat(os.fspath(root), follow_symlinks=False)
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(stat_result.st_mode)
        and stat_result.st_dev == context[2]
        and stat_result.st_ino == context[3]
    )


def _text(value: Any, limit: int = 240) -> str:
    """Return bounded, single-line diagnostic text."""

    if value is None:
        return ""
    value = str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _source_ref(value: Any, limit: int = 180) -> str | None:
    value = _text(value, limit)
    if not value:
        return None
    # Keep refs readable while preventing them from becoming paths, comments,
    # or unbounded IDs in the sidecar.
    return _SAFE_SOURCE_REF_RE.sub("_", value)


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    return value if _SHA256_RE.fullmatch(value) else None


def _safe_page(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed <= 0:
        return None
    return parsed


def _safe_bbox(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        numbers = {
            key: float(value[key]) for key in ("l", "r", "t", "b")
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in numbers.values()):
        return None
    if numbers["r"] <= numbers["l"] or numbers["t"] == numbers["b"]:
        return None
    origin = str(value.get("coord_origin") or "TOPLEFT").upper()
    if origin not in {"TOPLEFT", "BOTTOMLEFT"}:
        return None
    return {
        "l": numbers["l"],
        "r": numbers["r"],
        "t": numbers["t"],
        "b": numbers["b"],
        "coord_origin": origin,
    }


def _first_bbox(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    prov = node.get("prov")
    if isinstance(prov, list):
        for item in prov:
            if isinstance(item, dict):
                bbox = _safe_bbox(item.get("bbox"))
                if bbox is not None:
                    return bbox
    return _safe_bbox(node.get("bbox"))


def _first_page(node: Any) -> int | None:
    if not isinstance(node, dict):
        return None
    prov = node.get("prov")
    if isinstance(prov, list):
        for item in prov:
            if isinstance(item, dict):
                page = _safe_page(item.get("page_no"))
                if page is not None:
                    return page
    return _safe_page(node.get("page_no"))


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    """Yield bounded dictionary nodes from an arbitrary JSON value."""

    stack: list[Any] = [value]
    yielded = 0
    while stack and yielded < MAX_DOCUMENT_NODES:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            yielded += 1
            values: list[Any] = []
            for index, value_item in enumerate(current.values()):
                if index >= MAX_DOCUMENT_NODES:
                    break
                values.append(value_item)
            stack.extend(reversed(values))
        elif isinstance(current, list):
            stack.extend(reversed(current[:MAX_DOCUMENT_NODES]))


def _document_traversal_exceeds_limit(value: Any) -> bool:
    stack: list[Any] = [value]
    dictionary_count = 0
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            dictionary_count += 1
            if dictionary_count > MAX_DOCUMENT_NODES or len(current) > MAX_DOCUMENT_NODES:
                return True
            if len(stack) + len(current) > MAX_DOCUMENT_NODES * 2:
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            if len(current) > MAX_DOCUMENT_NODES:
                return True
            if len(stack) + len(current) > MAX_DOCUMENT_NODES * 2:
                return True
            stack.extend(current)
    return False


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _bounded_values(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value[:MAX_LIST_ITEMS])
    if isinstance(value, set):
        if len(value) > MAX_LIST_ITEMS:
            return []
        return sorted(value, key=lambda item: str(item))
    return []


def _chunk_part_map(document_json: Any) -> dict[int, int]:
    """Map object identity to the adapter's deterministic chunk part index."""

    if not isinstance(document_json, dict):
        return {}
    raw_chunks = document_json.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        return {}
    entries: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for original, chunk in enumerate(raw_chunks[:MAX_LIST_ITEMS]):
        if not isinstance(chunk, dict) or not isinstance(chunk.get("document"), dict):
            continue
        page_range = chunk.get("page_range")
        start = _safe_page(page_range[0]) if isinstance(page_range, list) and page_range else None
        end = (
            _safe_page(page_range[1])
            if isinstance(page_range, list) and len(page_range) > 1
            else start
        )
        entries.append(
            ((start if start is not None else original + 1, end if end is not None else -1, original), chunk)
        )
    result: dict[int, int] = {}
    for part_index, (_key, chunk) in enumerate(sorted(entries, key=lambda item: item[0])):
        for node in _walk(chunk.get("document")):
            result[id(node)] = part_index
    return result


def _node_source_ref(node: dict[str, Any], part_index: int | None = None) -> str | None:
    raw = _text(node.get("self_ref"), 180)
    if not raw:
        return None
    explicit_part = node.get("_local_ai_lab_chunk_part_index")
    if isinstance(explicit_part, int) and not isinstance(explicit_part, bool):
        part_index = explicit_part
    if part_index is not None and not raw.startswith("chunk:"):
        return _source_ref(f"chunk:{part_index}:{raw}")
    return _source_ref(raw)


def _document_nodes(document_json: Any, labels: set[str] | None = None) -> list[dict[str, Any]]:
    labels = {value.casefold() for value in (labels or set())}
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    part_map = _chunk_part_map(document_json)
    for node in _walk(document_json):
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        label = _text(node.get("label"), 64).casefold()
        if labels and label not in labels:
            continue
        if not label:
            continue
        part_index = part_map.get(identity)
        raw_page_no = node.get("page_no")
        prov = node.get("prov")
        if isinstance(prov, list):
            for provenance in prov:
                if isinstance(provenance, dict) and "page_no" in provenance:
                    raw_page_no = provenance.get("page_no")
                    break
        records.append(
            {
                "label": label,
                "text": _text(node.get("text")),
                "page_no": _first_page(node),
                "raw_page_no": raw_page_no,
                "bbox": _first_bbox(node),
                "source_ref": _node_source_ref(node, part_index),
                "part_index": part_index,
                "node": node,
            }
        )
    return records


def _safe_asset(output_dir: Path, value: Any) -> str | None:
    """Return a safe relative asset path only when the file is regular."""

    raw = _text(value, 300)
    if not raw:
        return None
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    try:
        fd = _open_relative(output_dir, relative.as_posix(), max_bytes=MAX_ASSET_BYTES)
        os.close(fd)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _open_relative(root: Path, relative: str, *, max_bytes: int) -> int:
    """Open a regular file below *root* without following symlinks.

    Traversing directory components through a dirfd and using ``O_NOFOLLOW``
    on every component avoids the common check-then-open race.  The returned
    descriptor is already size-checked and must be closed by the caller.
    """

    rel_path = Path(relative)
    if rel_path.is_absolute() or not rel_path.parts or ".." in rel_path.parts:
        raise ValueError("unsafe_relative_path")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    context = _root_context_for(root)
    if context is not None:
        # Duplicate the pinned descriptor so the caller owns the returned
        # traversal's root handle while the evaluation context remains open.
        root_fd = os.dup(context[1])
    else:
        root_fd = os.open(str(root), os.O_RDONLY | directory_flag | nofollow)
    current_fd = root_fd
    try:
        for part in rel_path.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow,
                dir_fd=current_fd,
            )
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        fd = os.open(
            rel_path.parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=current_fd,
        )
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size > max_bytes:
            os.close(fd)
            raise OSError("file_missing_or_too_large")
        return fd
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _read_bounded_fd(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining == 0:
        # A byte exactly at the limit is allowed; probe once to reject an
        # over-limit file without unbounded buffering.
        if os.read(fd, 1):
            raise OSError("file_too_large")
    return b"".join(chunks)


def _hash_relative_asset(output_dir: Path, value: Any) -> str | None:
    safe = _safe_asset(output_dir, value)
    if safe is None:
        return None
    try:
        fd = _open_relative(output_dir, safe, max_bytes=MAX_ASSET_BYTES)
        try:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError:
        return None


def _asset_path_matches_kind(kind: str, relative: str | None) -> bool:
    prefixes = _ASSET_PREFIXES.get(kind)
    if not prefixes:
        return True
    return bool(relative and any(relative.startswith(prefix) for prefix in prefixes))


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            return None, "json_file_missing_or_unsafe"
        fd = _open_relative(parent, path.name, max_bytes=MAX_JSON_BYTES)
        try:
            raw = _read_bounded_fd(fd, MAX_JSON_BYTES)
        finally:
            os.close(fd)
        return json.loads(raw.decode("utf-8")), None
    except FileNotFoundError:
        return None, "json_file_missing_or_unsafe"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"json_read_failed:{type(exc).__name__}"


def _atomic_json(path: Path, payload: Any) -> str | None:
    """Write a sidecar through the evaluation's pinned root directory fd.

    A path-based ``mkstemp``/``replace`` pair is vulnerable when the output
    directory is renamed and replaced between validation and publication.  The
    evaluator establishes ``_ROOT_CONTEXT`` once and all writes below use
    ``*at``-style dirfd operations against that inode.  Calls made outside an
    evaluation are rejected rather than silently falling back to a path race.
    """

    context = _root_context_for(path.parent)
    if context is None:
        return "sidecar_root_context_missing"
    if not _root_path_matches_context(path.parent, context):
        return "output_root_changed"
    root_fd: int | None = None
    try:
        root_fd = os.dup(context[1])
    except OSError as exc:
        return f"sidecar_write_failed:{type(exc).__name__}"
    target_name = path.name
    temp_name: str | None = None
    temp_fd: int | None = None
    try:
        try:
            existing = os.lstat(target_name, dir_fd=root_fd)
            if stat.S_ISLNK(existing.st_mode):
                return "sidecar_path_is_symlink"
        except FileNotFoundError:
            pass
        # Create the temporary inode directly below the pinned root.  Bounded
        # retries avoid unbounded work if a hostile directory is crowded.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for attempt in range(64):
            candidate_name = f".{target_name}.{os.getpid()}.{attempt}.tmp"
            try:
                temp_fd = os.open(candidate_name, flags, 0o600, dir_fd=root_fd)
                temp_name = candidate_name
                break
            except FileExistsError:
                continue
        if temp_fd is None or temp_name is None:
            return "sidecar_temp_name_exhausted"
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = None
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Re-check the destination entry immediately before replacement; a
        # newly-created symlink is never replaced by this writer.
        try:
            existing = os.lstat(target_name, dir_fd=root_fd)
            if stat.S_ISLNK(existing.st_mode):
                return "sidecar_path_is_symlink"
        except FileNotFoundError:
            pass
        os.replace(temp_name, target_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        temp_name = None
        # If the named root was swapped while writing, the fixed inode is still
        # safe, but the caller must not advertise the sidecar under the new
        # path.  Report failure so status.ok is forced false.
        if not _root_path_matches_context(path.parent, context):
            return "output_root_changed_after_write"
    except (OSError, TypeError, ValueError) as exc:
        return f"sidecar_write_failed:{type(exc).__name__}"
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except OSError:
                pass
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass
    return None


def _unique(values: Iterable[Any], limit: int = 8) -> list[str]:
    result: list[str] = []
    for value in values:
        value = _text(value, 140)
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def _truthy(value: Any) -> bool:
    if value is True or (isinstance(value, (int, float)) and value != 0):
        return True
    # Structural quarantine stores picture/table overlap as a bounded evidence
    # mapping, not a boolean.  Treat only non-empty containers as a positive
    # overlap signal; empty mappings/lists remain false.
    return isinstance(value, (dict, list, tuple, set)) and bool(value)


def _status_value(status: str, *, critical: bool, reasons: list[str]) -> tuple[str, bool]:
    if status not in REGION_STATUSES:
        status = "unresolved"
        reasons.append("invalid_region_status")
    return status, bool(critical and status == "unresolved")


def _record_id(kind: str, source_ref: str | None, page_no: int | None, index: Any) -> str:
    identity = source_ref or f"page={page_no or 0};index={index}"
    digest = _sha256_text(f"{kind}|{identity}")[:16]
    return f"{kind}:{digest}"


def _record(
    *,
    output_dir: Path,
    kind: str,
    index: Any,
    page_no: Any = None,
    bbox: Any = None,
    source_ref: Any = None,
    body_identity: Any = None,
    source_asset: Any = None,
    html_anchor: Any = None,
    markdown_anchor: Any = None,
    signals: dict[str, Any] | None = None,
    status: str,
    critical: bool,
    reasons: Iterable[Any] = (),
    text_preview: Any = None,
) -> dict[str, Any]:
    page = _safe_page(page_no)
    safe_box = _safe_bbox(bbox)
    raw_ref = _text(source_ref, 180)
    ref = _source_ref(raw_ref)
    asset = _safe_asset(output_dir, source_asset)
    normalized_reasons = _unique(reasons)
    if page is None and page_no is not None:
        normalized_reasons.append("invalid_page_no")
    if safe_box is None and bbox is not None:
        normalized_reasons.append("invalid_bbox")
    if source_asset and asset is None:
        normalized_reasons.append("unsafe_or_missing_source_asset")
    elif asset is not None and not _asset_path_matches_kind(kind, asset):
        normalized_reasons.append("source_asset_kind_mismatch")
    # Source refs are identifiers, never filesystem paths.  Keep a sanitized
    # value in the bounded record, but fail closed when a producer accidentally
    # exposes traversal or an absolute path as its mapping key.
    if raw_ref and (
        raw_ref.startswith(("/", "\\"))
        or raw_ref == ".."
        or raw_ref.startswith(("../", "..\\"))
        or "/../" in raw_ref
        or "\\..\\" in raw_ref
    ):
        normalized_reasons.append("unsafe_source_ref_mapping")
    if critical and normalized_reasons and status != "unresolved":
        status = "unresolved"
    status, _critical_unresolved = _status_value(
        status,
        critical=critical,
        reasons=normalized_reasons,
    )
    return {
        "id": _record_id(kind, ref, page, index),
        "kind": _text(kind, 48),
        "page_no": page,
        "bbox": safe_box,
        "source_ref": ref,
        "body_identity_sha256": _sha256(body_identity),
        "evidence": {
            "source_asset": asset,
            "html_anchor": _text(html_anchor, 180) or None,
            "markdown_anchor": _text(markdown_anchor, 180) or None,
        },
        "signals": signals or {},
        "status": status,
        # ``critical`` describes the policy for this region even when it is
        # currently verified.  Consumers can therefore distinguish a healthy
        # hard-gated region from advisory picture evidence without inferring
        # policy from the current status.
        "critical": bool(critical),
        "reasons": _unique(normalized_reasons),
        "text_preview": _text(text_preview, 180) or None,
    }


def _source_signals(status: dict[str, Any], metadata: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    quality = status.get("quality_signals") if isinstance(status, dict) else None
    if not isinstance(quality, dict):
        quality = {}
    metadata_quality = metadata.get("quality_signals") if isinstance(metadata, dict) else None
    if isinstance(metadata_quality, dict):
        merged = dict(metadata_quality)
        merged.update(quality)
        quality = merged
    source_visuals = quality.get("final_source_visuals")
    if not isinstance(source_visuals, dict):
        source_visuals = metadata.get("final_source_visuals") if isinstance(metadata, dict) else None
    return quality, source_visuals if isinstance(source_visuals, dict) else {}


def _candidate_map(source_visuals: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    payloads: list[Any] = [
        source_visuals.get(
            "structured_table_source_renderings"
            if kind == "table"
            else f"{kind}_source_renderings"
        )
    ]
    # Empty tables use the explicit visual fallback producer.  It has the same
    # source-ref/provenance contract, so merge it into the table candidate map
    # instead of declaring every empty semantic grid missing.
    if kind == "table":
        payloads.append(source_visuals.get("empty_table_visual_fallbacks"))
    result: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidate_lists: list[list[Any]] = []
        raw_candidates = payload.get("candidates")
        if isinstance(raw_candidates, list):
            candidate_lists.append(raw_candidates)
        # ``recover_algorithm_blocks_in_outputs`` publishes production records
        # under ``records`` and calls the crop ``source_image``.  Merge both
        # compatibility arrays when both exist; an empty ``candidates`` array
        # must not hide valid production records.
        raw_records = payload.get("records") if kind == "algorithm" else None
        if isinstance(raw_records, list):
            candidate_lists.append(raw_records)
        for candidates in candidate_lists:
            for item in candidates[:MAX_LIST_ITEMS]:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                if not candidate.get("image") and candidate.get("source_image"):
                    candidate["image"] = candidate.get("source_image")
                ref = _source_ref(candidate.get("source_ref"))
                if kind == "table" and ref in _ref_set(
                    source_visuals, "algorithm_source_expected_refs"
                ):
                    continue
                if kind == "table":
                    explicit_marker = " ".join(
                        _text(candidate.get(key), 80)
                        for key in ("kind", "label", "original_label")
                    ).casefold()
                    algorithm_like = bool(
                        candidate.get("algorithm_like") is True
                        or candidate.get("is_algorithm") is True
                        or "algorithm_like_table" in explicit_marker
                    )
                    if algorithm_like:
                        continue
                if ref:
                    existing = result.get(ref)
                    if existing is None:
                        result[ref] = candidate
                    else:
                        # Preserve the first producer's values and fill only
                        # absent fields from its compatibility twin.
                        for key, value in candidate.items():
                            if existing.get(key) in (None, "", [], {}):
                                existing[key] = value
    return result


def _ref_set(source_visuals: dict[str, Any], key: str) -> set[str]:
    values = source_visuals.get(key)
    if not isinstance(values, (list, tuple, set)):
        return set()
    bounded = _bounded_values(values)
    return {ref for value in bounded if (ref := _source_ref(value))}


def _ref_list(source_visuals: dict[str, Any], key: str) -> tuple[list[str], list[str]]:
    """Return bounded refs and duplicate refs without losing evidence order."""

    values = source_visuals.get(key)
    if not isinstance(values, (list, tuple, set)):
        return [], []
    bounded = _bounded_values(values)
    normalised = [ref for value in bounded if (ref := _source_ref(value))]
    if isinstance(values, set):
        normalised.sort()
    duplicates: list[str] = []
    seen: set[str] = set()
    for ref in normalised[:MAX_LIST_ITEMS]:
        if ref in seen and ref not in duplicates:
            duplicates.append(ref)
        seen.add(ref)
    return normalised[:MAX_LIST_ITEMS], duplicates[:32]


def _ref_items_invalid(value: Any) -> bool:
    """Reject non-string/empty reference entries instead of silently dropping them."""

    if not isinstance(value, (list, tuple, set)):
        return False
    for item in _bounded_values(value):
        if not isinstance(item, str) or not _source_ref(item):
            return True
    return False


def _candidate_duplicate_refs(source_visuals: dict[str, Any], kind: str) -> list[str]:
    payload_keys = [
        "structured_table_source_renderings" if kind == "table" else f"{kind}_source_renderings"
    ]
    if kind == "table":
        payload_keys.append("empty_table_visual_fallbacks")
    duplicates: list[str] = []
    for key in payload_keys:
        payload = source_visuals.get(key)
        if not isinstance(payload, dict):
            continue
        arrays = [payload.get("candidates")]
        if kind == "algorithm":
            arrays.append(payload.get("records"))
        for candidates in arrays:
            if not isinstance(candidates, list):
                continue
            # The same ref may legitimately appear once in each compatibility
            # array.  Detect duplicates within an array, not across aliases.
            seen: set[str] = set()
            for item in candidates[:MAX_LIST_ITEMS]:
                if not isinstance(item, dict):
                    continue
                ref = _source_ref(item.get("source_ref"))
                if not ref:
                    continue
                if kind == "table" and ref in _ref_set(
                    source_visuals, "algorithm_source_expected_refs"
                ):
                    continue
                if ref in seen and ref not in duplicates:
                    duplicates.append(ref)
                seen.add(ref)
    return duplicates[:32]


def _candidate_alias_conflict_refs(
    source_visuals: dict[str, Any], kind: str
) -> list[str]:
    """Reject contradictory evidence across compatibility arrays."""

    if kind != "algorithm":
        return []
    payload = source_visuals.get("algorithm_source_renderings")
    if not isinstance(payload, dict):
        return []
    candidates = payload.get("candidates")
    records = payload.get("records")
    if not isinstance(candidates, list) or not isinstance(records, list):
        return []

    def by_ref(items: list[Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in items[:MAX_LIST_ITEMS]:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            if not normalized.get("image") and normalized.get("source_image"):
                normalized["image"] = normalized.get("source_image")
            ref = _source_ref(normalized.get("source_ref"))
            if ref:
                result.setdefault(ref, normalized)
        return result

    left = by_ref(candidates)
    right = by_ref(records)
    conflicts: list[str] = []
    keys = (
        "image",
        "page_no",
        "bbox",
        "page_span",
        "page_bboxes",
        "table_index",
        "provenance_verified",
    )
    for ref in sorted(set(left) & set(right)):
        if any(
            left[ref].get(key) not in (None, "", [], {})
            and right[ref].get(key) not in (None, "", [], {})
            and left[ref].get(key) != right[ref].get(key)
            for key in keys
        ):
            conflicts.append(ref)
    return conflicts[:32]


def _structural_visible_body_text(value: Any, *, html_markup: bool = False) -> str:
    """Mirror the adapter's bounded visible-text normalization contract."""

    visible = str(value or "")[:100_000]
    if html_markup:
        visible = re.sub(
            r"<(?:script|style|template)\b.*?</(?:script|style|template)>",
            " ",
            visible,
            flags=re.I | re.S,
        )
        visible = re.sub(
            r"<sup\b(?=[^>]*(?:"
            r"\bclass\s*=\s*[\"'][^\"']*\bfootnote[^\"']*[\"']|"
            r"\bid\s*=\s*[\"'][^\"']*\bfnref-[^\"']*[\"']))[^>]*>",
            "",
            visible,
            flags=re.I | re.S,
        )
        visible = re.sub(r"<sub\b[^>]*>", "_", visible, flags=re.I)
        visible = re.sub(r"</sub\s*>", "", visible, flags=re.I)
        visible = re.sub(r"<sup\b[^>]*>", "^", visible, flags=re.I)
        visible = re.sub(r"</sup\s*>", "", visible, flags=re.I)
        visible = re.sub(r"<br\s*/?>", "\n", visible, flags=re.I)
        visible = re.sub(
            r"</(?:p|pre|li|tr|section|article|h[1-6])\s*>",
            "\n",
            visible,
            flags=re.I,
        )
        visible = re.sub(r"<[^>]+>", "", visible)
    visible = html.unescape(visible)
    visible = visible.translate(
        str.maketrans(
            {
                "\u00a0": " ",
                "−": "-",
                "–": "-",
                "—": "-",
                "（": "(",
                "）": ")",
            }
        )
    )
    return visible.replace("\r\n", "\n").replace("\r", "\n")


def _structural_body_identity(value: Any, *, html_markup: bool = False) -> str:
    visible = _structural_visible_body_text(value, html_markup=html_markup)
    visible = visible.replace(r"\(", "").replace(r"\)", "")
    identities: list[str] = []
    for line in visible.splitlines() or [visible]:
        tokens = _STRUCTURAL_BODY_TOKEN_RE.findall(line)
        if tokens:
            identities.append(" ".join(tokens))
    return "\n".join(identities)


def _algorithm_body_identity(value: Any) -> str:
    """Mirror the adapter's algorithm line-number/soft-wrap identity."""

    visible = _structural_visible_body_text(value)
    lines = [
        re.sub(r"^\s*(\d{1,3})\s*:\s*", r"\1 ", line)
        for line in (visible.splitlines() or [visible])
    ]
    return " ".join(_structural_body_identity("\n".join(lines)).splitlines())


def _algorithm_layout_visible_lines(lines: list[Any]) -> list[Any]:
    """Select the same visible layout body used by the publishing adapter."""

    if len(lines) < 3:
        return lines
    body_start = re.compile(
        r"(?i)^\s*(?:Require|Ensure|Input|Output|Parameters?|Initialize|"
        r"for\b.*\bdo\b|while\b|if\b|else\b|return\b|"
        r"end(?:\s+(?:for|while|if))?\b|\d+\s*[:.]|"
        r"[•\-\u2022]\s*(?:Sample|Update|Process|Train|Add|Modify|Set|Compute|Draw)\b|"
        r"Sample|Update|Process|Train|Add|Modify|Set|Compute|Draw)"
    )
    start_index: int | None = None
    for index, line in enumerate(lines[:MAX_LIST_ITEMS]):
        if isinstance(line, dict) and body_start.match(
            re.sub(r"\s+", " ", str(line.get("text") or "")).strip()
        ):
            start_index = index
            break
    visible = lines[:MAX_LIST_ITEMS] if start_index is None else lines[start_index:MAX_LIST_ITEMS]
    return [
        line
        for line in visible
        if not (
            isinstance(line, dict)
            and re.match(
                r"(?i)^Algorithm\s+\d+\b",
                re.sub(r"\s+", " ", str(line.get("text") or "")).strip(),
            )
        )
    ]


def _algorithm_layout_fragmented(lines: list[Any]) -> bool:
    text_lines = [
        re.sub(r"\s+", " ", str(line.get("text") or "")).strip()
        for line in lines[:MAX_LIST_ITEMS]
        if isinstance(line, dict)
        and re.sub(r"\s+", " ", str(line.get("text") or "")).strip()
    ]
    if len(text_lines) < 3:
        return True
    short_lines = [line for line in text_lines if len(line) <= 3]
    readable_lines = [line for line in text_lines if len(line) >= 12]
    return bool(
        (len(short_lines) >= 4 and len(short_lines) / max(1, len(text_lines)) > 0.28)
        or (len(short_lines) >= 6 and len(readable_lines) < 4)
        or (len({line for line in short_lines}) >= 5 and len(text_lines) >= 10)
    )


def _algorithm_expected_body_identity(record: dict[str, Any]) -> str:
    layout = record.get("layout")
    if isinstance(layout, dict):
        line_count = _strict_nonnegative_int(layout.get("line_count"))
        raw_lines = layout.get("lines")
        if line_count is not None and line_count >= 3 and isinstance(raw_lines, list):
            visible_lines = _algorithm_layout_visible_lines(raw_lines)
            if not _algorithm_layout_fragmented(visible_lines):
                body = "\n".join(
                    str(line.get("text") or "")
                    for line in visible_lines
                    if isinstance(line, dict) and str(line.get("text") or "")
                )
                if body:
                    return _algorithm_body_identity(body)
    return _algorithm_body_identity(record.get("text"))


def _inline_binding_identity(value: Any) -> str:
    """Canonicalize presentation-only math syntax for paragraph binding.

    Docling may render ``s_j`` as ``s j`` and ``α_{s_j}`` as ``α s j`` in the
    final text node.  Remove only whitespace and TeX subscript/grouping
    presentation; operators, relation signs, delimiters, punctuation, Unicode
    math symbols, letters, and digits all remain identity-bearing.
    """

    visible = unicodedata.normalize(
        "NFKC", _structural_visible_body_text(value)
    ).casefold()
    visible = (
        visible.replace(r"\(", "")
        .replace(r"\)", "")
        .replace(r"\[", "")
        .replace(r"\]", "")
    )
    return "".join(
        character
        for character in visible
        if not character.isspace() and character not in "_{}$"
    )


def _code_body_identity(value: Any) -> str:
    raw = str(value or "")
    matches = list(re.finditer(r"(?<![\w.])(\d{1,3})\s+", raw))
    selected: list[re.Match[str]] = []
    expected = 1
    for match in matches:
        if int(match.group(1)) == expected:
            selected.append(match)
            expected += 1
    if len(selected) >= 2:
        contents: list[str] = []
        for index, match in enumerate(selected):
            end = selected[index + 1].start() if index + 1 < len(selected) else len(raw)
            content = raw[match.end() : end].strip().replace("−", "-")
            content = re.sub(r"\.\s+(?=[A-Za-z_]\w*\s*\()", ".", content)
            contents.append(content)
        # The adapter feeds the numbered contents joined by ``\n`` into
        # ``_code_body_identity``; that contract flattens physical lines with
        # spaces after structural tokenisation.  Mirror it exactly here.
        return " ".join(_structural_body_identity("\n".join(contents)).splitlines())
    return " ".join(_structural_body_identity(raw).splitlines())


def _table_body_identity(node: dict[str, Any]) -> str:
    data = node.get("data") if isinstance(node, dict) else None
    raw_cells = data.get("table_cells") if isinstance(data, dict) else None
    if not isinstance(raw_cells, list):
        return ""
    cells = [cell for cell in raw_cells[:MAX_TABLE_CELLS] if isinstance(cell, dict)]
    rows = _strict_nonnegative_int(data.get("num_rows"))
    cols = _strict_nonnegative_int(data.get("num_cols"))
    rows = rows if rows is not None else 0
    cols = cols if cols is not None else 0
    has_coordinates = any(
        any(
            key in cell
            for key in (
                "start_row_offset_idx",
                "end_row_offset_idx",
                "start_col_offset_idx",
                "end_col_offset_idx",
            )
        )
        for cell in cells
    )
    if cells and not has_coordinates:
        if rows <= 0 and cols <= 0:
            rows, cols = 1, len(cells)
        elif rows <= 0 and cols > 0:
            rows = (len(cells) + cols - 1) // cols
        elif cols <= 0 and rows > 0:
            cols = (len(cells) + rows - 1) // rows
        if cols <= 0 or rows * cols < len(cells):
            return ""
        if rows * cols > MAX_TABLE_CELLS:
            return ""
        grid = [["" for _ in range(cols)] for _ in range(rows)]
        for index, cell in enumerate(cells):
            row, col = divmod(index, cols)
            grid[row][col] = str(cell.get("text") or "")
    else:
        for cell in cells:
            end_row = _strict_nonnegative_int(cell.get("end_row_offset_idx"))
            end_col = _strict_nonnegative_int(cell.get("end_col_offset_idx"))
            if end_row is not None:
                rows = max(rows, end_row)
            if end_col is not None:
                cols = max(cols, end_col)
        if rows <= 0 or cols <= 0 or rows * cols > MAX_TABLE_CELLS:
            return ""
        grid = [["" for _ in range(cols)] for _ in range(rows)]
        unplaced: list[dict[str, Any]] = []
        for cell in cells:
            row = _strict_nonnegative_int(cell.get("start_row_offset_idx"))
            col = _strict_nonnegative_int(cell.get("start_col_offset_idx"))
            if row is None or col is None:
                unplaced.append(cell)
            elif row < rows and col < cols:
                grid[row][col] = str(cell.get("text") or "")
        empty_slots = [
            (row, col)
            for row in range(rows)
            for col in range(cols)
            if not grid[row][col]
        ]
        for cell, (row, col) in zip(unplaced, empty_slots):
            grid[row][col] = str(cell.get("text") or "")

    def cell_identity(value: Any) -> str:
        visible = re.sub(r"\s+", " ", str(value or "")).strip()
        visible = re.sub(r"(?<=[A-Za-z])\s+(?=\d)", "", visible)
        visible = re.sub(r"(?<=\d)\s+(?=[A-Za-z])", "", visible)
        return _structural_body_identity(
            visible,
            html_markup=bool(re.search(r"</?[A-Za-z][^>]*>", visible)),
        )

    return json.dumps(
        [[cell_identity(cell) for cell in row] for row in grid],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _node_body_identity(kind: str, node: dict[str, Any]) -> str:
    if kind == "table":
        return _table_body_identity(node)
    if kind == "code":
        return _code_body_identity(node.get("text"))
    return _structural_body_identity(node.get("text"))


def _algorithm_page_span_reasons(candidate: dict[str, Any]) -> list[str]:
    """Keep cross-page algorithm records fail-closed until a join contract exists."""

    reasons: list[str] = []
    span = candidate.get("page_span")
    page_numbers: set[int] = set()
    if span is not None:
        if not isinstance(span, dict):
            reasons.append("algorithm_page_span_invalid")
        else:
            start = _safe_page(span.get("start_page"))
            end = _safe_page(span.get("end_page"))
            if span.get("start_page") is not None and start is None:
                reasons.append("algorithm_page_span_invalid")
            if span.get("end_page") is not None and end is None:
                reasons.append("algorithm_page_span_invalid")
            if start is not None:
                page_numbers.add(start)
            if end is not None:
                page_numbers.add(end)
            pages = span.get("pages")
            if pages is not None:
                if not isinstance(pages, list):
                    reasons.append("algorithm_page_span_invalid")
                else:
                    if len(pages) > MAX_LIST_ITEMS:
                        reasons.append("algorithm_page_span_too_many")
                    for value in pages[:MAX_LIST_ITEMS]:
                        page = _safe_page(value)
                        if page is None:
                            reasons.append("algorithm_page_span_invalid")
                        else:
                            page_numbers.add(page)
            if start is not None and end is not None and start != end:
                reasons.append("algorithm_cross_page_unsupported")
    page_bboxes = candidate.get("page_bboxes")
    if page_bboxes is not None:
        if not isinstance(page_bboxes, list):
            reasons.append("algorithm_page_bboxes_invalid")
        else:
            if len(page_bboxes) > MAX_LIST_ITEMS:
                reasons.append("algorithm_page_bboxes_too_many")
            for item in page_bboxes[:MAX_LIST_ITEMS]:
                if not isinstance(item, dict):
                    reasons.append("algorithm_page_bboxes_invalid")
                    continue
                page = _safe_page(item.get("page_no"))
                if page is None:
                    reasons.append("algorithm_page_bboxes_invalid")
                else:
                    page_numbers.add(page)
    if len(page_numbers) > 1:
        reasons.append("algorithm_cross_page_unsupported")
    return _unique(reasons)


def _body_identity_sha(kind: str, identity: str) -> str:
    return hashlib.sha256(
        (str(kind).casefold() + "\0" + str(identity)).encode("utf-8")
    ).hexdigest()


def _formula_raw_content_sha256(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    raw = raw.translate(
        str.maketrans({"−": "-", "–": "-", "—": "-", "（": "(", "）": ")"})
    )
    raw = re.sub(r"\s+", "", raw)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""


def _bbox_equal(left: Any, right: Any) -> bool:
    lhs = _safe_bbox(left)
    rhs = _safe_bbox(right)
    if lhs is None or rhs is None:
        return False
    return all(abs(float(lhs[key]) - float(rhs[key])) <= 1e-6 for key in ("l", "r", "t", "b")) and lhs.get("coord_origin") == rhs.get("coord_origin")


def _bbox_contains(outer: Any, inner: Any, *, tolerance: float = 1.0) -> bool:
    outer_box = _safe_bbox(outer)
    inner_box = _safe_bbox(inner)
    if outer_box is None or inner_box is None:
        return False
    if outer_box.get("coord_origin") != inner_box.get("coord_origin"):
        return False
    outer_low, outer_high = sorted((outer_box["t"], outer_box["b"]))
    inner_low, inner_high = sorted((inner_box["t"], inner_box["b"]))
    return bool(
        outer_box["l"] - tolerance <= inner_box["l"]
        and outer_box["r"] + tolerance >= inner_box["r"]
        and outer_low - tolerance <= inner_low
        and outer_high + tolerance >= inner_high
    )


def _bbox_union(values: Iterable[Any]) -> dict[str, Any] | None:
    boxes = [_safe_bbox(value) for value in values]
    safe_boxes = [box for box in boxes if box is not None]
    if not safe_boxes or len(safe_boxes) != len(boxes):
        return None
    origins = {str(box.get("coord_origin")) for box in safe_boxes}
    if len(origins) != 1:
        return None
    origin = origins.pop()
    result = {
        "l": min(float(box["l"]) for box in safe_boxes),
        "r": max(float(box["r"]) for box in safe_boxes),
        "coord_origin": origin,
    }
    if origin == "BOTTOMLEFT":
        result["t"] = max(float(box["t"]) for box in safe_boxes)
        result["b"] = min(float(box["b"]) for box in safe_boxes)
    else:
        result["t"] = min(float(box["t"]) for box in safe_boxes)
        result["b"] = max(float(box["b"]) for box in safe_boxes)
    return result


def _manifest_entries(metadata: dict[str, Any], kind: str, source_ref: str | None) -> list[dict[str, Any]]:
    manifest = metadata.get("structural_visual_provenance_manifest")
    if not isinstance(manifest, dict) or not source_ref:
        return []
    aliases = {
        "table": ("tables", "table"),
        "algorithm": ("algorithms", "algorithm"),
        "code": ("code", "codes"),
        "formula": ("formulas", "formula"),
        "inline_math": ("inline_math", "inline-math"),
    }
    entries: list[dict[str, Any]] = []
    for key in aliases.get(kind, (kind,)):
        raw = manifest.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw[:MAX_LIST_ITEMS]:
            if isinstance(item, dict) and _source_ref(item.get("source_ref")) == source_ref:
                entries.append(item)
    return entries[:MAX_LIST_ITEMS]


def _manifest_diagnostics(
    output_dir: Path,
    metadata: dict[str, Any],
    kind: str,
    source_ref: str,
    candidate: dict[str, Any],
    node_item: dict[str, Any] | None,
    nodes_by_key: dict[tuple[int | None, str], list[dict[str, Any]]],
    source_sha: str | None,
    semantic_record: dict[str, Any] | None = None,
) -> list[str]:
    """Validate the manifest's immutable path/hash/node binding when present."""

    manifest = metadata.get("structural_visual_provenance_manifest")
    if not isinstance(manifest, dict):
        return []
    aliases = {
        "table": ("tables", "table"),
        "algorithm": ("algorithms", "algorithm"),
        "code": ("code", "codes"),
        "formula": ("formulas", "formula"),
        "inline_math": ("inline_math", "inline-math"),
    }
    manifest_schema_reasons: list[str] = []
    for key in aliases.get(kind, (kind,)):
        raw = manifest.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            manifest_schema_reasons.append("structural_manifest_entries_invalid")
            continue
        if len(raw) > MAX_LIST_ITEMS:
            manifest_schema_reasons.append("structural_manifest_entries_too_many")
        if any(not isinstance(item, dict) for item in raw[:MAX_LIST_ITEMS]):
            manifest_schema_reasons.append("structural_manifest_entry_invalid")
    entries = _manifest_entries(metadata, kind, source_ref)
    if not entries:
        return _unique([*manifest_schema_reasons, "structural_manifest_entry_missing"])
    if len(entries) != 1:
        return _unique([*manifest_schema_reasons, "structural_manifest_entry_ambiguous"])
    entry = entries[0]
    reasons: list[str] = list(manifest_schema_reasons)
    declared_kind = _text(entry.get("kind"), 48).casefold()
    if declared_kind != kind:
        reasons.append("structural_manifest_kind_mismatch")
    if _source_ref(entry.get("source_ref")) != source_ref:
        reasons.append("structural_manifest_source_ref_mismatch")
    entry_self_ref = entry.get("self_ref")
    entry_part_index = entry.get("part_index")
    if "self_ref" not in entry or not isinstance(entry_self_ref, str):
        reasons.append("structural_manifest_self_ref_invalid")
    if "part_index" not in entry:
        reasons.append("structural_manifest_part_index_missing")
    if kind == "algorithm":
        # Algorithm records are sidecar semantic blocks, not Docling nodes.
        # The adapter intentionally binds their entry to the empty self-ref;
        # source_node_bindings below bind the contributing final nodes.
        if entry_self_ref != "":
            reasons.append("structural_manifest_self_ref_mismatch")
        if entry_part_index is not None:
            reasons.append("structural_manifest_part_index_mismatch")
    elif isinstance(node_item, dict):
        node = node_item.get("node") if isinstance(node_item.get("node"), dict) else {}
        if entry_self_ref != str(node.get("self_ref") or ""):
            reasons.append("structural_manifest_self_ref_mismatch")
        if entry_part_index != node_item.get("part_index"):
            reasons.append("structural_manifest_part_index_mismatch")
    asset = candidate.get("image") or candidate.get("path") or entry.get("asset_path")
    manifest_asset = _safe_asset(output_dir, entry.get("asset_path"))
    if manifest_asset is None:
        reasons.append("structural_manifest_asset_path_invalid")
    elif asset and _safe_asset(output_dir, asset) != manifest_asset:
        reasons.append("structural_manifest_asset_path_mismatch")
    declared_asset_sha = entry.get("asset_sha256")
    expected_asset_sha = _sha256(declared_asset_sha)
    actual_asset_sha = _hash_relative_asset(output_dir, manifest_asset)
    if expected_asset_sha is None:
        reasons.append("structural_manifest_asset_hash_invalid")
    elif actual_asset_sha != expected_asset_sha:
        reasons.append("structural_manifest_asset_hash_mismatch")
    page_no = _safe_page(entry.get("page_no"))
    if entry.get("page_no") is not None and page_no is None:
        reasons.append("structural_manifest_page_invalid")
    candidate_page = _safe_page(candidate.get("page_no")) if candidate.get("page_no") is not None else None
    if page_no is not None and candidate_page is not None and page_no != candidate_page:
        reasons.append("structural_manifest_page_mismatch")
    node_page = node_item.get("page_no") if isinstance(node_item, dict) else None
    if page_no is not None and node_page is not None and page_no != node_page:
        reasons.append("structural_manifest_final_node_page_mismatch")
    node_bbox = entry.get("node_bbox")
    candidate_bbox = candidate.get("bbox")
    if node_bbox is not None and candidate_bbox is not None and not _bbox_equal(node_bbox, candidate_bbox):
        reasons.append("structural_manifest_bbox_mismatch")
    if isinstance(node_item, dict) and node_item.get("bbox") is not None and node_bbox is not None and not _bbox_equal(node_bbox, node_item.get("bbox")):
        reasons.append("structural_manifest_final_node_bbox_mismatch")
    page_path = _text(entry.get("page_image_path"), 300)
    if page_path:
        safe_page_path = _safe_asset(output_dir, page_path)
        if safe_page_path is None:
            reasons.append("structural_manifest_page_asset_invalid")
        else:
            page_sha = _sha256(entry.get("page_image_sha256"))
            if entry.get("page_image_sha256") is not None and page_sha is None:
                reasons.append("structural_manifest_page_hash_invalid")
            elif page_sha and _hash_relative_asset(output_dir, safe_page_path) != page_sha:
                reasons.append("structural_manifest_page_hash_mismatch")
    for key in ("visual_pdf_sha256", "source_pdf_sha256"):
        declared = entry.get(key)
        if declared is not None:
            digest = _sha256(declared)
            if digest is None:
                reasons.append("structural_manifest_source_hash_invalid")
            elif source_sha and digest != source_sha:
                reasons.append("structural_manifest_source_hash_mismatch")
    body_hash = _sha256(entry.get("structural_body_identity_sha256"))
    if body_hash is None:
        reasons.append("structural_manifest_body_hash_invalid")
    if body_hash:
        if kind == "algorithm":
            if not isinstance(semantic_record, dict):
                reasons.append("algorithm_semantic_record_missing_or_ambiguous")
            elif body_hash != _body_identity_sha(
                "algorithm", _algorithm_expected_body_identity(semantic_record)
            ):
                reasons.append("structural_manifest_body_hash_mismatch")
        elif isinstance(node_item, dict):
            identity = _node_body_identity(
                kind,
                node_item.get("node")
                if isinstance(node_item.get("node"), dict)
                else {},
            )
            if body_hash != _body_identity_sha(kind, identity):
                reasons.append("structural_manifest_body_hash_mismatch")
    bindings = entry.get("source_node_bindings")
    binding_prefix = "algorithm" if kind == "algorithm" else "structural"
    if not isinstance(bindings, list) or not bindings:
        reasons.append(f"{binding_prefix}_source_node_bindings_missing")
    else:
        if len(bindings) > MAX_LIST_ITEMS:
            reasons.append(f"{binding_prefix}_source_node_bindings_too_many")
        seen_binding_keys: set[tuple[int | None, str]] = set()
        for binding in bindings[:MAX_LIST_ITEMS]:
            if not isinstance(binding, dict):
                reasons.append(f"{binding_prefix}_source_node_binding_invalid")
                continue
            self_ref = _text(binding.get("self_ref"), 180)
            binding_source_ref = _source_ref(binding.get("source_ref"))
            part_index_raw = binding.get("part_index")
            part_index = (
                part_index_raw
                if isinstance(part_index_raw, int) and not isinstance(part_index_raw, bool)
                else None
            )
            key = (part_index, self_ref)
            if (
                not self_ref
                or not binding_source_ref
                or "part_index" not in binding
                or key in seen_binding_keys
            ):
                reasons.append(f"{binding_prefix}_source_node_binding_not_bijective")
                continue
            seen_binding_keys.add(key)
            matching = nodes_by_key.get(key, [])
            if len(matching) != 1:
                reasons.append(f"{binding_prefix}_source_node_binding_missing")
                continue
            bound_item = matching[0]
            if binding_source_ref != bound_item.get("source_ref"):
                reasons.append(f"{binding_prefix}_source_node_ref_mismatch")
            page_no = _safe_page(binding.get("page_no"))
            if page_no is None or page_no != bound_item.get("page_no"):
                reasons.append(f"{binding_prefix}_source_node_page_mismatch")
            if not _bbox_equal(binding.get("bbox"), bound_item.get("bbox")):
                reasons.append(f"{binding_prefix}_source_node_bbox_mismatch")
            bound_hash = _sha256(binding.get("body_identity_sha256"))
            bound_node = bound_item.get("node") if isinstance(bound_item.get("node"), dict) else {}
            raw_identity_kind = binding.get("body_identity_kind")
            if kind == "algorithm":
                if raw_identity_kind not in {"node_text", "table_grid"}:
                    reasons.append("algorithm_source_node_body_identity_kind_invalid")
                elif raw_identity_kind == "table_grid" and (
                    bound_item.get("label") != "table"
                    or not _table_body_identity(bound_node)
                ):
                    reasons.append("algorithm_source_node_body_identity_kind_mismatch")
                elif raw_identity_kind == "node_text" and (
                    bound_item.get("label")
                    not in {"code", "formula", "list_item", "text", "section_header"}
                    or not _structural_body_identity(bound_node.get("text"))
                ):
                    reasons.append("algorithm_source_node_body_identity_kind_mismatch")
                bound_identity_kind = (
                    "table" if raw_identity_kind == "table_grid" else "algorithm"
                )
            else:
                if raw_identity_kind not in (None, ""):
                    reasons.append("structural_source_node_body_identity_kind_invalid")
                bound_identity_kind = kind
            bound_identity = _node_body_identity(bound_identity_kind, bound_node)
            hash_kind = "algorithm-source-node" if kind == "algorithm" else kind
            if bound_hash is None or bound_hash != _body_identity_sha(hash_kind, bound_identity):
                reasons.append(f"{binding_prefix}_source_node_body_hash_mismatch")
        if kind == "algorithm" and isinstance(semantic_record, dict):
            semantic_bindings = semantic_record.get("source_node_bindings")
            if not isinstance(semantic_bindings, list) or not semantic_bindings:
                reasons.append("algorithm_semantic_source_node_bindings_missing")
            else:
                if len(semantic_bindings) > MAX_LIST_ITEMS:
                    reasons.append("algorithm_semantic_source_node_bindings_too_many")

                def contributor_key(value: Any) -> tuple[str, str, int | None] | None:
                    if not isinstance(value, dict):
                        return None
                    contributor_ref = _source_ref(value.get("source_ref"))
                    contributor_self_ref = _text(value.get("self_ref"), 180)
                    contributor_part = value.get("part_index")
                    if (
                        not contributor_ref
                        or not contributor_self_ref
                        or "part_index" not in value
                        or (
                            contributor_part is not None
                            and (
                                not isinstance(contributor_part, int)
                                or isinstance(contributor_part, bool)
                                or contributor_part < 0
                            )
                        )
                    ):
                        return None
                    return contributor_ref, contributor_self_ref, contributor_part

                manifest_keys = [
                    key
                    for binding in bindings[:MAX_LIST_ITEMS]
                    if (key := contributor_key(binding)) is not None
                ]
                semantic_keys = [
                    key
                    for binding in semantic_bindings[:MAX_LIST_ITEMS]
                    if (key := contributor_key(binding)) is not None
                ]
                if (
                    len(manifest_keys) != len(bindings[:MAX_LIST_ITEMS])
                    or len(semantic_keys) != len(semantic_bindings[:MAX_LIST_ITEMS])
                    or len(set(manifest_keys)) != len(manifest_keys)
                    or len(set(semantic_keys)) != len(semantic_keys)
                    or set(manifest_keys) != set(semantic_keys)
                ):
                    reasons.append("algorithm_source_node_binding_set_mismatch")
                manifest_by_key = {
                    key: binding
                    for binding in bindings[:MAX_LIST_ITEMS]
                    if (key := contributor_key(binding)) is not None
                }
                semantic_by_key = {
                    key: binding
                    for binding in semantic_bindings[:MAX_LIST_ITEMS]
                    if (key := contributor_key(binding)) is not None
                }
                if any(
                    _safe_page(manifest_by_key[key].get("page_no"))
                    != _safe_page(semantic_by_key[key].get("page_no"))
                    or not _bbox_equal(
                        manifest_by_key[key].get("bbox"),
                        semantic_by_key[key].get("bbox"),
                    )
                    or manifest_by_key[key].get("body_identity_kind")
                    != semantic_by_key[key].get("body_identity_kind")
                    or _sha256(manifest_by_key[key].get("body_identity_sha256"))
                    != _sha256(semantic_by_key[key].get("body_identity_sha256"))
                    for key in set(manifest_by_key) & set(semantic_by_key)
                ):
                    reasons.append(
                        "algorithm_source_node_binding_evidence_mismatch"
                    )
                manifest_union = _bbox_union(
                    binding.get("bbox")
                    for binding in bindings[:MAX_LIST_ITEMS]
                    if isinstance(binding, dict)
                )
                if manifest_union is None or not _bbox_equal(
                    manifest_union, semantic_record.get("bbox")
                ):
                    reasons.append("algorithm_source_node_union_bbox_mismatch")
    return _unique(reasons, limit=80)


def _manifest_entry(metadata: dict[str, Any], kind: str, source_ref: str | None) -> dict[str, Any] | None:
    manifest = metadata.get("structural_visual_provenance_manifest")
    if not isinstance(manifest, dict) or not source_ref:
        return None
    aliases = {
        "table": ("tables", "table"),
        "algorithm": ("algorithms", "algorithm"),
        "code": ("code", "codes"),
        "formula": ("formulas", "formula"),
        "inline_math": ("inline_math", "inline-math"),
    }
    entries: list[Any] = []
    for key in aliases.get(kind, (kind,)):
        value = manifest.get(key)
        if isinstance(value, list):
            entries.extend(value[:MAX_LIST_ITEMS])
    if not entries:
        return None
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and _source_ref(item.get("source_ref")) == source_ref
    ]
    return matches[0] if len(matches) == 1 else None


def _nonnegative_int(value: Any) -> int | None:
    return _strict_nonnegative_int(value)


def _positive_index_set(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    bounded = _bounded_values(value)
    result: set[int] = set()
    for item in bounded:
        parsed = _safe_page(item)
        if parsed is not None:
            result.add(parsed)
    return result


def _table_numeric_signal(value: Any) -> int:
    """Count independent numeric tokens while ignoring legal uncertainty forms.

    A cell such as ``[1, 2]``, ``(3, 4)``, ``1–2`` or ``4 ± 0.2`` is one
    semantic value/range, not evidence that a source row was flattened.  The
    collapsed-row heuristic therefore opts out for these explicit forms.
    """

    text = _text(value, 500)
    if not text:
        return 0
    if "±" in text or "+/-" in text.lower() or _TABLE_RANGE_RE.search(text):
        return 0
    if re.search(r"[\[\(]\s*[^\]\)]*[\]\)]", text):
        return 0
    return len(_TABLE_NUMBER_RE.findall(text))


def _table_topology_diagnostics(document_json: Any) -> dict[str, dict[str, Any]]:
    """Independently flag semantic cells that collapse source row geometry.

    The source-visual producer can prove that a crop is bound to the right
    table while still accepting a wrong grid.  These checks use only the final
    document's cell offsets and bboxes: a one-row cell may not span another
    semantic row center, and repeated tall multi-number cells in one body row
    indicate that several visual rows were flattened into one semantic row.
    """

    diagnostics: dict[str, dict[str, Any]] = {}
    for ordinal, item in enumerate(_document_nodes(document_json, {"table"}), start=1):
        node = item.get("node") if isinstance(item.get("node"), dict) else {}
        source_ref = item.get("source_ref") or _source_ref(node.get("self_ref"))
        if not source_ref:
            source_ref = f"table:{ordinal}"
        data = node.get("data") if isinstance(node, dict) else None
        raw_cells = data.get("table_cells") if isinstance(data, dict) else None
        if not isinstance(raw_cells, list):
            raw_cells = []
        raw_cell_count = len(raw_cells)
        declared_rows = _strict_nonnegative_int(data.get("num_rows")) if isinstance(data, dict) else None
        declared_cols = _strict_nonnegative_int(data.get("num_cols")) if isinstance(data, dict) else None
        dimensions_exceed_limit = bool(
            declared_rows is not None
            and declared_cols is not None
            and declared_rows * declared_cols > MAX_TABLE_CELLS
        )

        cells: list[dict[str, Any]] = []
        invalid_cell_geometry_count = 0
        invalid_bounds_count = 0
        invalid_span_count = 0
        overlap_count = 0
        occupied: set[tuple[int, int]] = set()
        occupancy_work_count = 0
        geometry_work_limited = False
        for raw in raw_cells[:MAX_TABLE_CELLS]:
            if not isinstance(raw, dict):
                invalid_cell_geometry_count += 1
                continue
            bbox = _safe_bbox(raw.get("bbox"))
            start_row = _nonnegative_int(raw.get("start_row_offset_idx"))
            end_row = _nonnegative_int(raw.get("end_row_offset_idx"))
            start_col = _nonnegative_int(raw.get("start_col_offset_idx"))
            end_col = _nonnegative_int(raw.get("end_col_offset_idx"))
            row_span = _strict_nonnegative_int(raw.get("row_span"))
            col_span = _strict_nonnegative_int(raw.get("col_span"))
            if bbox is None or start_row is None or end_row is None or end_row <= start_row:
                invalid_cell_geometry_count += 1
                continue
            if start_col is None or end_col is None or end_col <= start_col:
                invalid_bounds_count += 1
                continue
            if declared_rows is not None and (end_row > declared_rows or start_row >= declared_rows):
                invalid_bounds_count += 1
            if declared_cols is not None and (end_col > declared_cols or start_col >= declared_cols):
                invalid_bounds_count += 1
            if row_span is not None and (row_span <= 0 or row_span != end_row - start_row):
                invalid_span_count += 1
            if col_span is not None and (col_span <= 0 or col_span != end_col - start_col):
                invalid_span_count += 1
            span_area = (end_row - start_row) * (end_col - start_col)
            if (
                span_area > MAX_TABLE_CELLS
                or occupancy_work_count + span_area > MAX_TABLE_CELLS
            ):
                geometry_work_limited = True
            else:
                occupancy_work_count += span_area
                for row_index in range(start_row, end_row):
                    for col_index in range(start_col, end_col):
                        cell_key = (row_index, col_index)
                        if cell_key in occupied:
                            overlap_count += 1
                        occupied.add(cell_key)
            low, high = sorted((float(bbox["t"]), float(bbox["b"])))
            cells.append(
                {
                    "start_row": start_row,
                    "end_row": end_row,
                    "height": high - low,
                    "low": low,
                    "high": high,
                    "center": (low + high) / 2.0,
                    "number_count": _table_numeric_signal(raw.get("text")),
                }
            )

        positive_heights = sorted(
            cell["height"] for cell in cells if cell["height"] > 0
        )
        baseline_height: float | None = None
        if positive_heights:
            lower_sample_size = max(1, (len(positive_heights) + 2) // 3)
            baseline_height = float(
                statistics.median(positive_heights[:lower_sample_size])
            )

        row_centers: dict[int, float] = {}
        if baseline_height and baseline_height > 0:
            for row_index in sorted({cell["start_row"] for cell in cells}):
                compact_centers = [
                    cell["center"]
                    for cell in cells
                    if cell["start_row"] == row_index
                    and cell["end_row"] == row_index + 1
                    and cell["height"] <= baseline_height * 1.6
                ]
                if compact_centers:
                    row_centers[row_index] = float(statistics.median(compact_centers))

        cross_row_cell_count = 0
        collapsed_rows: list[int] = []
        if baseline_height and baseline_height > 0:
            for cell in cells:
                if (
                    cell["end_row"] != cell["start_row"] + 1
                    or cell["height"] <= baseline_height * 1.6
                ):
                    continue
                inset = min(baseline_height * 0.2, cell["height"] * 0.2)
                if any(
                    row_index != cell["start_row"]
                    and cell["low"] + inset < center < cell["high"] - inset
                    for row_index, center in row_centers.items()
                ):
                    cross_row_cell_count += 1

            for row_index in sorted({cell["start_row"] for cell in cells if cell["start_row"] > 0}):
                repeated_counts: dict[int, int] = {}
                for cell in cells:
                    if (
                        cell["start_row"] != row_index
                        or cell["end_row"] != row_index + 1
                        or cell["height"] <= baseline_height * 1.6
                        or cell["number_count"] < 2
                    ):
                        continue
                    count = int(cell["number_count"])
                    repeated_counts[count] = repeated_counts.get(count, 0) + 1
                if any(count >= 2 for count in repeated_counts.values()):
                    collapsed_rows.append(row_index)

        reasons: list[str] = []
        if not raw_cells:
            reasons.append("table_cell_geometry_missing")
        if raw_cells and (invalid_cell_geometry_count or raw_cell_count > MAX_TABLE_CELLS):
            reasons.append("table_cell_geometry_invalid")
        if invalid_bounds_count:
            reasons.append("table_cell_bounds_invalid")
        if invalid_span_count:
            reasons.append("table_cell_span_invalid")
        if overlap_count:
            reasons.append("table_cell_overlap")
        if declared_rows is None or declared_cols is None:
            reasons.append("table_dimensions_missing")
        if dimensions_exceed_limit:
            reasons.append("table_dimensions_exceed_limit")
        if geometry_work_limited:
            reasons.append("table_geometry_work_limit")
        if (
            raw_cells
            and declared_rows is not None
            and declared_cols is not None
            and declared_rows * declared_cols <= MAX_TABLE_CELLS
        ):
            in_bounds_occupied = {
                (row_index, col_index)
                for row_index, col_index in occupied
                if 0 <= row_index < declared_rows
                and 0 <= col_index < declared_cols
            }
            if len(in_bounds_occupied) != declared_rows * declared_cols:
                reasons.append("table_cell_occupancy_incomplete")
        if cross_row_cell_count:
            reasons.append("table_cell_crosses_semantic_row_boundary")
        if collapsed_rows:
            reasons.append("table_row_likely_collapsed")
        diagnostics[source_ref] = {
            "geometry_checked": bool(cells and baseline_height),
            "cell_count": raw_cell_count,
            "valid_cell_geometry_count": len(cells),
            "invalid_cell_geometry_count": invalid_cell_geometry_count,
            "invalid_bounds_count": invalid_bounds_count,
            "invalid_span_count": invalid_span_count,
            "overlap_count": overlap_count,
            "declared_num_rows": declared_rows,
            "declared_num_cols": declared_cols,
            "dimensions_exceed_limit": dimensions_exceed_limit,
            "geometry_work_limited": geometry_work_limited,
            "occupancy_work_count": occupancy_work_count,
            "occupied_slot_count": len(occupied),
            "baseline_line_height": (
                round(baseline_height, 4) if baseline_height is not None else None
            ),
            "cross_row_cell_count": cross_row_cell_count,
            "collapsed_row_indexes": collapsed_rows[:32],
            "reasons": reasons,
        }
    return diagnostics


def _structural_region_records(
    output_dir: Path,
    kind: str,
    source_visuals: dict[str, Any],
    metadata: dict[str, Any],
    primary_counts: dict[str, Any],
    table_diagnostics: dict[str, dict[str, Any]] | None = None,
    document_json: Any = None,
    source_sha: str | None = None,
) -> list[dict[str, Any]]:
    expected_key = f"{kind}_source_expected_refs"
    _expected_values, expected_duplicates = _ref_list(source_visuals, expected_key)
    expected_refs = _ref_set(source_visuals, expected_key)
    candidates = _candidate_map(source_visuals, kind)
    expected_declared = expected_key in source_visuals
    if not expected_refs and not expected_declared:
        expected_refs = set(candidates)

    count_key = {
        "table": "tables",
        "algorithm": "algorithms",
        "code": "code_blocks",
        "formula": "formulas",
    }.get(kind)
    count_declared = bool(count_key and count_key in primary_counts)
    expected_count_value = primary_counts.get(count_key) if count_declared else None
    expected_count = (
        _strict_nonnegative_int(expected_count_value)
        if count_declared
        else None
    )
    global_reasons: list[str] = []
    if count_declared and expected_count is None:
        global_reasons.append("expected_region_count_invalid")
    if expected_duplicates:
        global_reasons.append("expected_region_refs_duplicate")
    if expected_count is not None and expected_count != len(expected_refs):
        global_reasons.append("expected_region_count_mismatch")
    if isinstance(source_visuals.get(expected_key), (list, tuple, set)) and len(source_visuals.get(expected_key)) > MAX_LIST_ITEMS:
        global_reasons.append("expected_region_refs_too_many")
    candidate_duplicates = _candidate_duplicate_refs(source_visuals, kind)
    if candidate_duplicates:
        global_reasons.append("candidate_source_refs_duplicate")
    if _candidate_alias_conflict_refs(source_visuals, kind):
        global_reasons.append("candidate_alias_evidence_conflict")
    if expected_declared and not isinstance(
        source_visuals.get(expected_key), (list, tuple, set)
    ):
        global_reasons.append("expected_region_refs_invalid")
    elif _ref_items_invalid(source_visuals.get(expected_key)):
        global_reasons.append("expected_region_ref_item_invalid")
    if not expected_declared and (expected_count or candidates):
        global_reasons.append("expected_region_refs_missing_declaration")
    candidate_payload_keys = [
        "structured_table_source_renderings"
        if kind == "table"
        else f"{kind}_source_renderings"
    ]
    if kind == "table":
        candidate_payload_keys.append("empty_table_visual_fallbacks")
    for payload_key in candidate_payload_keys:
        raw_payload = source_visuals.get(payload_key)
        if raw_payload is not None and not isinstance(raw_payload, dict):
            global_reasons.append("structural_renderings_invalid")
        array_keys = ["candidates"]
        if kind == "algorithm":
            array_keys.append("records")
        for array_key in array_keys:
            raw_candidates = (
                raw_payload.get(array_key)
                if isinstance(raw_payload, dict)
                else None
            )
            if isinstance(raw_candidates, list):
                if len(raw_candidates) > MAX_LIST_ITEMS:
                    global_reasons.append("structural_candidates_too_many")
                if any(not isinstance(item, dict) for item in raw_candidates[:MAX_LIST_ITEMS]):
                    global_reasons.append("structural_candidate_invalid")
                if any(
                    isinstance(item, dict)
                    and (
                        not isinstance(item.get("source_ref"), str)
                        or not _source_ref(item.get("source_ref"))
                    )
                    for item in raw_candidates[:MAX_LIST_ITEMS]
                ):
                    global_reasons.append("structural_candidate_ref_invalid")
            elif isinstance(raw_payload, dict) and array_key in raw_payload:
                global_reasons.append("structural_candidates_invalid")
    candidate_refs = set(candidates)
    if candidate_refs != expected_refs:
        global_reasons.append("candidate_source_ref_set_mismatch")
    node_items = _document_nodes(document_json, None) if isinstance(document_json, dict) else []
    nodes_by_ref: dict[str, list[dict[str, Any]]] = {}
    nodes_by_key: dict[tuple[int | None, str], list[dict[str, Any]]] = {}
    for node_item in node_items:
        node_ref = _source_ref(node_item.get("source_ref"))
        if node_ref:
            nodes_by_ref.setdefault(node_ref, []).append(node_item)
        node = node_item.get("node") if isinstance(node_item.get("node"), dict) else {}
        self_ref = _text(node.get("self_ref"), 180)
        if self_ref:
            nodes_by_key.setdefault((node_item.get("part_index"), self_ref), []).append(node_item)
    manifest = metadata.get("structural_visual_provenance_manifest")
    strict_manifest = isinstance(manifest, dict)
    manifest_aliases = {
        "table": ("tables", "table"),
        "algorithm": ("algorithms", "algorithm"),
        "code": ("code", "codes"),
    }
    manifest_items: list[dict[str, Any]] = []
    if strict_manifest:
        for manifest_key in manifest_aliases.get(kind, (kind,)):
            raw_entries = manifest.get(manifest_key)
            if raw_entries is None:
                continue
            if not isinstance(raw_entries, list):
                global_reasons.append("structural_manifest_entries_invalid")
                continue
            if len(raw_entries) > MAX_LIST_ITEMS:
                global_reasons.append("structural_manifest_entries_too_many")
            if any(not isinstance(item, dict) for item in raw_entries[:MAX_LIST_ITEMS]):
                global_reasons.append("structural_manifest_entry_invalid")
            manifest_items.extend(
                item for item in raw_entries[:MAX_LIST_ITEMS] if isinstance(item, dict)
            )
    manifest_refs = {
        ref
        for item in manifest_items
        if (ref := _source_ref(item.get("source_ref")))
    }
    algorithm_refs = _ref_set(source_visuals, "algorithm_source_expected_refs")
    if kind == "table":
        manifest_refs -= algorithm_refs
    if strict_manifest and manifest_refs != expected_refs:
        global_reasons.append("structural_manifest_ref_set_mismatch")

    algorithm_records_by_ref: dict[str, list[dict[str, Any]]] = {}
    if kind == "algorithm":
        algorithm_sidecar, sidecar_error = _read_json(
            output_dir / "algorithm_blocks.json"
        )
        if sidecar_error:
            if expected_refs or candidate_refs or manifest_items:
                global_reasons.append("algorithm_semantic_sidecar_missing_or_unsafe")
        elif not isinstance(algorithm_sidecar, list):
            global_reasons.append("algorithm_semantic_sidecar_invalid")
        else:
            if len(algorithm_sidecar) > MAX_LIST_ITEMS:
                global_reasons.append("algorithm_semantic_records_too_many")
            for record in algorithm_sidecar[:MAX_LIST_ITEMS]:
                if not isinstance(record, dict):
                    global_reasons.append("algorithm_semantic_record_invalid")
                    continue
                record_ref = _source_ref(record.get("source_ref"))
                if not record_ref:
                    global_reasons.append("algorithm_semantic_record_ref_invalid")
                    continue
                algorithm_records_by_ref.setdefault(record_ref, []).append(record)
            if any(
                len(records_for_ref) != 1
                for records_for_ref in algorithm_records_by_ref.values()
            ):
                global_reasons.append("algorithm_semantic_record_ref_duplicate")

    if kind == "table":
        relevant_nodes = [
            item
            for item in node_items
            if item.get("label") == "table"
            and item.get("source_ref") not in algorithm_refs
        ]
        current_kind_refs = {
            ref
            for item in relevant_nodes
            if (ref := _source_ref(item.get("source_ref")))
        }
    elif kind == "code":
        relevant_nodes = [
            item
            for item in node_items
            if item.get("label") in {"code", "code_block"}
            and item.get("source_ref") not in algorithm_refs
        ]
        current_kind_refs = {
            ref
            for item in relevant_nodes
            if (ref := _source_ref(item.get("source_ref")))
        }
    else:
        relevant_nodes = []
        current_kind_refs = set(algorithm_records_by_ref)
    if len(current_kind_refs) != len(relevant_nodes) and kind != "algorithm":
        global_reasons.append("final_document_node_ref_invalid_or_duplicate")
    if current_kind_refs != expected_refs:
        global_reasons.append("final_document_ref_set_mismatch")

    if expected_refs and not strict_manifest:
        global_reasons.append("structural_provenance_manifest_missing")
    records: list[dict[str, Any]] = []
    if not expected_refs and (
        candidate_refs
        or manifest_refs
        or current_kind_refs
        or (expected_count is not None and expected_count > 0)
        or global_reasons
    ):
        declaration_reason = (
            "expected_region_refs_empty_declaration"
            if expected_declared
            else "expected_region_refs_missing"
        )
        records.append(
            _record(
                output_dir=output_dir,
                kind=kind,
                index=0,
                status="unresolved",
                critical=True,
                reasons=[declaration_reason, *global_reasons],
                signals={
                    "source_ref_present": False,
                    "candidate_count": len(candidate_refs),
                    "manifest_count": len(manifest_refs),
                    "final_ref_count": len(current_kind_refs),
                },
            )
        )
        return records

    html_bound = _ref_set(source_visuals, f"{kind}_source_html_bound_refs")
    markdown_bound = _ref_set(source_visuals, f"{kind}_source_markdown_bound_refs")
    html_verified = _ref_set(source_visuals, f"{kind}_source_html_body_identity_verified_refs")
    markdown_verified = _ref_set(source_visuals, f"{kind}_source_markdown_body_identity_verified_refs")
    html_mismatch = _ref_set(source_visuals, f"{kind}_source_html_body_identity_mismatch_refs")
    markdown_mismatch = _ref_set(source_visuals, f"{kind}_source_markdown_body_identity_mismatch_refs")
    provenance_verified = _ref_set(source_visuals, f"{kind}_source_provenance_verified_refs")
    provenance_mismatch = _ref_set(source_visuals, f"{kind}_source_provenance_mismatch_refs")
    expected_body = _ref_set(source_visuals, f"{kind}_source_body_identity_expected_refs")
    empty_fallback_refs = (
        _ref_set(source_visuals, "table_empty_fallback_expected_refs")
        if kind == "table"
        else set()
    )
    exact_key = f"{kind}_source_exact_coverage"
    exact_coverage = source_visuals.get(exact_key)
    if not isinstance(exact_coverage, bool):
        exact_coverage = None
    structural_ref_keys = (
        f"{kind}_source_html_bound_refs",
        f"{kind}_source_markdown_bound_refs",
        f"{kind}_source_html_body_identity_verified_refs",
        f"{kind}_source_markdown_body_identity_verified_refs",
        f"{kind}_source_html_body_identity_mismatch_refs",
        f"{kind}_source_markdown_body_identity_mismatch_refs",
        f"{kind}_source_provenance_verified_refs",
        f"{kind}_source_provenance_mismatch_refs",
        f"{kind}_source_body_identity_expected_refs",
    )
    if kind == "table":
        structural_ref_keys = (
            *structural_ref_keys,
            "table_empty_fallback_expected_refs",
        )
    for key in structural_ref_keys:
        raw_values = source_visuals.get(key)
        if isinstance(raw_values, (list, tuple, set)) and len(raw_values) > MAX_LIST_ITEMS:
            global_reasons.append("structural_binding_refs_too_many")
        elif key in source_visuals and not isinstance(
            raw_values, (list, tuple, set)
        ):
            global_reasons.append("structural_binding_refs_invalid")
        elif _ref_items_invalid(raw_values):
            global_reasons.append("structural_binding_ref_item_invalid")
        elif _ref_list(source_visuals, key)[1]:
            global_reasons.append("structural_binding_refs_duplicate")
    expected_semantic_refs = expected_refs - empty_fallback_refs
    if empty_fallback_refs - expected_refs:
        global_reasons.append("unexpected_empty_table_fallback_refs")
    if html_bound - expected_refs or markdown_bound - expected_refs:
        global_reasons.append("unexpected_surface_binding_refs")
    if html_verified - expected_semantic_refs or markdown_verified - expected_semantic_refs:
        global_reasons.append("unexpected_body_identity_refs")
    if provenance_verified - expected_refs:
        global_reasons.append("unexpected_provenance_refs")
    if (
        html_mismatch
        | markdown_mismatch
        | provenance_mismatch
        | expected_body
    ) - expected_refs:
        global_reasons.append("unexpected_structural_diagnostic_refs")

    for ordinal, ref in enumerate(sorted(expected_refs), start=1):
        candidate = candidates.get(ref) or {}
        entry = _manifest_entry(metadata, kind, ref) or {}
        asset = candidate.get("image") or candidate.get("path") or entry.get("asset_path")
        page_no = candidate.get("page_no") or entry.get("page_no")
        bbox = candidate.get("bbox") or entry.get("node_bbox")
        body_identity = entry.get("structural_body_identity_sha256")
        reasons: list[str] = list(global_reasons)
        matching_nodes = nodes_by_ref.get(ref, [])
        node_item = matching_nodes[0] if len(matching_nodes) == 1 else None
        if not matching_nodes:
            reasons.append("final_document_node_missing")
        elif len(matching_nodes) > 1:
            reasons.append("final_document_node_ambiguous")
        if strict_manifest:
            reasons.extend(
                _manifest_diagnostics(
                    output_dir,
                    metadata,
                    kind,
                    ref,
                    candidate,
                    node_item,
                    nodes_by_key,
                    source_sha,
                    (
                        algorithm_records_by_ref.get(ref, [None])[0]
                        if len(algorithm_records_by_ref.get(ref, [])) == 1
                        else None
                    ),
                )
            )
        if strict_manifest and node_item is not None:
            candidate_page = _safe_page(page_no) if page_no is not None else None
            if candidate_page is not None and node_item.get("page_no") is not None and candidate_page != node_item.get("page_no"):
                reasons.append("final_document_page_mismatch")
            if bbox is not None and node_item.get("bbox") is not None and not _bbox_equal(bbox, node_item.get("bbox")):
                reasons.append("final_document_bbox_mismatch")
        candidate_asset_sha = _sha256(candidate.get("asset_sha256"))
        if candidate.get("asset_sha256") is not None:
            if candidate_asset_sha is None:
                reasons.append("source_candidate_asset_hash_invalid")
            elif _hash_relative_asset(output_dir, asset) != candidate_asset_sha:
                reasons.append("source_candidate_asset_hash_mismatch")
        topology: dict[str, Any] | None = None
        if kind == "table":
            is_empty_fallback = ref in empty_fallback_refs
            independent = (table_diagnostics or {}).get(ref) or {}
            topology = {
                "exact_coverage": exact_coverage,
                "body_identity_expected": ref in expected_body,
                "html_body_identity_verified": ref in html_verified,
                "markdown_body_identity_verified": ref in markdown_verified,
                "body_identity_mismatch": ref in (html_mismatch | markdown_mismatch),
                "empty_visual_fallback": is_empty_fallback,
                "independent_cell_geometry": {
                    key: independent.get(key)
                    for key in (
                        "geometry_checked",
                        "cell_count",
                        "valid_cell_geometry_count",
                        "invalid_cell_geometry_count",
                        "baseline_line_height",
                        "cross_row_cell_count",
                        "collapsed_row_indexes",
                    )
                },
            }
            if exact_coverage is not True:
                reasons.append("table_topology_unverified")
            if ref not in expected_body and not is_empty_fallback:
                reasons.append("table_body_identity_missing")
            if not is_empty_fallback:
                reasons.extend(independent.get("reasons") or [])
        if not candidate:
            reasons.append("source_candidate_missing")
        if not asset:
            reasons.append("source_asset_missing")
        if ref not in html_bound:
            reasons.append("html_occurrence_unbound")
        if ref not in markdown_bound:
            reasons.append("markdown_occurrence_unbound")
        if ref not in empty_fallback_refs and (
            ref not in html_verified or ref not in markdown_verified
        ):
            reasons.append("body_identity_unverified")
        if ref not in empty_fallback_refs and (ref in html_mismatch or ref in markdown_mismatch):
            reasons.append("body_identity_mismatch")
        if ref not in provenance_verified or ref in provenance_mismatch:
            reasons.append("source_provenance_unverified")
        candidate_reasons = candidate.get("provenance_reasons")
        if isinstance(candidate_reasons, list):
            reasons.extend(candidate_reasons)
        if kind == "algorithm":
            reasons.extend(_algorithm_page_span_reasons(candidate))
            semantic_algorithm_records = algorithm_records_by_ref.get(ref, [])
            if len(semantic_algorithm_records) == 1:
                reasons.extend(
                    _algorithm_page_span_reasons(semantic_algorithm_records[0])
                )
        signals: dict[str, Any] = {
            "source_candidate_present": bool(candidate),
            "source_asset_present": bool(asset),
            "html_bound": ref in html_bound,
            "markdown_bound": ref in markdown_bound,
            "html_body_identity_verified": ref in html_verified,
            "markdown_body_identity_verified": ref in markdown_verified,
            "provenance_verified": ref in provenance_verified and ref not in provenance_mismatch,
            "exact_coverage": exact_coverage,
        }
        if topology is not None:
            signals["table_topology"] = topology
        verified = not reasons
        record_status = "verified_semantic" if verified else "unresolved"
        records.append(
            _record(
                output_dir=output_dir,
                kind=kind,
                index=ordinal,
                page_no=page_no,
                bbox=bbox,
                source_ref=ref,
                body_identity=body_identity,
                source_asset=asset,
                html_anchor=ref if ref in html_bound else None,
                markdown_anchor=ref if ref in markdown_bound else None,
                signals=signals,
                status=record_status,
                critical=True,
                reasons=reasons,
                text_preview=candidate.get("caption") or candidate.get("text"),
            )
        )
    return records


def _formula_region_records(
    output_dir: Path,
    source_visuals: dict[str, Any],
    metadata: dict[str, Any],
    primary_counts: dict[str, Any],
    document_json: Any = None,
    source_sha: str | None = None,
) -> list[dict[str, Any]]:
    """Build index-bound records for display formulas.

    Formula evidence is intentionally index based in the adapter (unlike
    tables/algorithms/code, which use stable ``source_ref`` values).  A
    ``formula:index:N`` identifier keeps that contract explicit and prevents a
    crop with a matching count from being mistaken for the wrong occurrence.
    """

    raw_expected = source_visuals.get("formula_source_expected_indexes")
    expected: set[int] = set()
    expected_duplicates: set[int] = set()
    if isinstance(raw_expected, (list, tuple, set)):
        for value in _bounded_values(raw_expected):
            index = _safe_page(value)
            if index is None:
                continue
            if index in expected:
                expected_duplicates.add(index)
            expected.add(index)
    approved_drops: set[int] = set()
    approved_drop_reasons: dict[int, set[str]] = {}
    declared_drops = source_visuals.get("formula_source_dropped_artifacts")
    if isinstance(declared_drops, list):
        for artifact in declared_drops[:MAX_LIST_ITEMS]:
            if not isinstance(artifact, dict):
                continue
            index = _safe_page(
                artifact.get("raw_formula_index", artifact.get("index"))
            )
            if index is None:
                continue
            if str(artifact.get("reason") or ""):
                approved_drops.add(index)
                approved_drop_reasons.setdefault(index, set()).add(
                    str(artifact.get("reason"))
                )
    expected_count: int | None = None
    if "formulas" in primary_counts:
        expected_count = _strict_nonnegative_int(primary_counts.get("formulas"))
    global_reasons: list[str] = []
    if raw_expected is not None and not isinstance(raw_expected, (list, tuple, set)):
        global_reasons.append("formula_expected_indexes_invalid")
    if expected_duplicates:
        global_reasons.append("formula_expected_indexes_duplicate")
    if expected_count is None and "formulas" in primary_counts:
        global_reasons.append("formula_expected_count_invalid")
    if isinstance(raw_expected, (list, tuple, set)) and len(raw_expected) > MAX_LIST_ITEMS:
        global_reasons.append("formula_expected_indexes_too_many")
    if isinstance(raw_expected, (list, tuple, set)) and any(
        _safe_page(value) is None for value in _bounded_values(raw_expected)
    ):
        global_reasons.append("formula_expected_index_invalid")
    if (
        "formula_source_dropped_artifacts" in source_visuals
        and not isinstance(declared_drops, list)
    ):
        global_reasons.append("formula_dropped_artifacts_invalid")
    if isinstance(declared_drops, list) and len(declared_drops) > MAX_LIST_ITEMS:
        global_reasons.append("formula_dropped_artifacts_too_many")
    if isinstance(declared_drops, list) and any(
        not isinstance(artifact, dict)
        or _safe_page(
            artifact.get("raw_formula_index", artifact.get("index"))
        )
        is None
        or not str(artifact.get("reason") or "")
        for artifact in declared_drops[:MAX_LIST_ITEMS]
    ):
        global_reasons.append("formula_dropped_artifact_invalid")
    records: list[dict[str, Any]] = []
    if expected_count and not expected:
        return [
            _record(
                output_dir=output_dir,
                kind="formula",
                index=0,
                source_ref="formula:index:missing",
                status="unresolved",
                critical=True,
                reasons=["expected_formula_indexes_missing"],
                signals={"expected_count": expected_count},
            )
        ]
    semantic_expected = expected - approved_drops
    if expected_count is not None and expected_count != len(semantic_expected):
        count_mismatch = True
    else:
        count_mismatch = False

    payload = source_visuals.get("formula_source_renderings")
    candidates = payload.get("candidates") if isinstance(payload, dict) else []
    if payload is not None and not isinstance(payload, dict):
        global_reasons.append("formula_renderings_invalid")
    elif isinstance(payload, dict) and "candidates" in payload and not isinstance(candidates, list):
        global_reasons.append("formula_candidates_invalid")
    by_index: dict[int, dict[str, Any]] = {}
    candidate_duplicates: set[int] = set()
    if isinstance(candidates, list):
        if len(candidates) > MAX_LIST_ITEMS:
            global_reasons.append("formula_candidates_too_many")
        for candidate in candidates[:MAX_LIST_ITEMS]:
            if not isinstance(candidate, dict):
                continue
            index = _safe_page(candidate.get("formula_index"))
            if index is None:
                continue
            if index in by_index:
                candidate_duplicates.add(index)
            by_index[index] = candidate
        if any(
            not isinstance(candidate, dict)
            or _safe_page(candidate.get("formula_index")) is None
            for candidate in candidates[:MAX_LIST_ITEMS]
        ):
            global_reasons.append("formula_candidate_invalid")
    if candidate_duplicates:
        global_reasons.append("formula_candidate_indexes_duplicate")
    if set(by_index) != expected:
        global_reasons.append("formula_candidate_index_set_mismatch")

    html_indexes = _positive_index_set(source_visuals.get("formula_source_html_indexes"))
    markdown_indexes = _positive_index_set(
        source_visuals.get("formula_source_markdown_indexes")
    )
    missing_indexes = _positive_index_set(
        source_visuals.get("formula_source_missing_indexes")
    )
    unexpected_indexes = _positive_index_set(
        source_visuals.get("formula_source_unexpected_indexes")
    )
    duplicate_html = _positive_index_set(
        source_visuals.get("formula_source_duplicate_html_anchor_indexes")
    )
    duplicate_markdown = _positive_index_set(
        source_visuals.get("formula_source_duplicate_markdown_anchor_indexes")
    )
    appendix_indexes = _positive_index_set(
        source_visuals.get("formula_source_html_appendix_indexes")
    ) | _positive_index_set(
        source_visuals.get("formula_source_markdown_appendix_indexes")
    )
    formula_surface_index_keys = (
        "formula_source_html_indexes",
        "formula_source_markdown_indexes",
        "formula_source_missing_indexes",
        "formula_source_unexpected_indexes",
        "formula_source_duplicate_html_anchor_indexes",
        "formula_source_duplicate_markdown_anchor_indexes",
        "formula_source_html_appendix_indexes",
        "formula_source_markdown_appendix_indexes",
    )
    for key in formula_surface_index_keys:
        if key not in source_visuals:
            continue
        raw_indexes = source_visuals.get(key)
        if not isinstance(raw_indexes, (list, tuple, set)):
            global_reasons.append("formula_surface_indexes_invalid")
            continue
        if len(raw_indexes) > MAX_LIST_ITEMS:
            global_reasons.append("formula_surface_indexes_too_many")
        if any(
            _safe_page(value) is None for value in _bounded_values(raw_indexes)
        ):
            global_reasons.append("formula_surface_index_invalid")
        normalized_indexes = [
            index
            for value in _bounded_values(raw_indexes)
            if (index := _safe_page(value)) is not None
        ]
        if len(normalized_indexes) != len(set(normalized_indexes)):
            global_reasons.append("formula_surface_indexes_duplicate")
    crop_metrics = metadata.get("formula_crop_diagnostics") if isinstance(metadata, dict) else None
    bounded_crop_metrics: list[Any] = []
    diagnostic_indexes: set[int] = set()
    if crop_metrics is not None and not isinstance(crop_metrics, list):
        global_reasons.append("formula_crop_diagnostics_invalid")
    elif isinstance(crop_metrics, list):
        bounded_crop_metrics = crop_metrics[:MAX_LIST_ITEMS]
        if len(crop_metrics) > MAX_LIST_ITEMS:
            global_reasons.append("formula_crop_diagnostics_too_many")
        for item in crop_metrics[:MAX_LIST_ITEMS]:
            if not isinstance(item, dict):
                global_reasons.append("formula_crop_diagnostic_invalid")
                continue
            index_value = _safe_page(item.get("index"))
            if index_value is None:
                global_reasons.append("formula_crop_diagnostic_index_invalid")
                continue
            diagnostic_indexes.add(index_value)
    extra_diagnostic_indexes = diagnostic_indexes - expected
    if extra_diagnostic_indexes:
        global_reasons.append("formula_crop_diagnostic_extra_index")
    formula_nodes = _document_nodes(document_json, {"formula"}) if isinstance(document_json, dict) else []
    nodes_by_formula_index = {
        formula_index: formula_nodes[formula_index - 1]
        for formula_index in expected
        if formula_index <= len(formula_nodes)
    }
    if len(formula_nodes) != len(expected):
        global_reasons.append("formula_final_node_count_mismatch")
    if (html_indexes - semantic_expected) or (markdown_indexes - semantic_expected):
        global_reasons.append("formula_unexpected_surface_indexes")
    diagnostic_surface_indexes = (
        missing_indexes
        | unexpected_indexes
        | duplicate_html
        | duplicate_markdown
        | appendix_indexes
    )
    if diagnostic_surface_indexes - expected:
        global_reasons.append("formula_unexpected_diagnostic_indexes")
    if approved_drops - expected:
        global_reasons.append("formula_unexpected_dropped_artifact_indexes")
    if not expected and (
        by_index or diagnostic_indexes or formula_nodes or expected_count or global_reasons
    ):
        # Keep this explicit marker separate from the per-index loop: an empty
        # declaration with an extra candidate/diagnostic/node must not vanish
        # as a vacuous successful zero-count run.
        return [
            _record(
                output_dir=output_dir,
                kind="formula",
                index=0,
                source_ref="formula:index:empty-declaration",
                status="unresolved",
                critical=True,
                reasons=["formula_expected_indexes_empty_or_missing", *global_reasons],
                signals={"candidate_count": len(by_index), "diagnostic_count": len(diagnostic_indexes), "final_node_count": len(formula_nodes)},
            )
        ]
    for ordinal, index in enumerate(sorted(expected), start=1):
        candidate = by_index.get(index) or {}
        selected_image = candidate.get("selected_image")
        selected = _text(candidate.get("selected"), 40)
        reasons: list[str] = list(global_reasons)
        if count_mismatch:
            reasons.append("formula_expected_index_count_mismatch")
        is_approved_appendix = index in approved_drops and index in appendix_indexes
        if not is_approved_appendix and (index in missing_indexes or index not in html_indexes):
            reasons.append("formula_html_occurrence_unbound")
        if not is_approved_appendix and (
            index in missing_indexes or index not in markdown_indexes
        ):
            reasons.append("formula_markdown_occurrence_unbound")
        if index in duplicate_html:
            reasons.append("formula_html_occurrence_duplicate")
        if index in duplicate_markdown:
            reasons.append("formula_markdown_occurrence_duplicate")
        if index in unexpected_indexes:
            reasons.append("formula_occurrence_unexpected")
        if not candidate:
            reasons.append("formula_source_candidate_missing")
        evidence_image = selected_image or candidate.get("diagnostic_image")
        if not evidence_image:
            reasons.append("formula_source_crop_missing")
        if candidate and not _truthy(candidate.get("source_provenance_verified")):
            # A verified context crop is an allowed fallback only when the
            # producer explicitly selected it and verified its provenance.
            if (
                not is_approved_appendix
                and (
                    selected != "context"
                    or not _truthy(candidate.get("context_provenance_verified"))
                )
            ):
                reasons.append("formula_source_provenance_unverified")
        source_reasons = candidate.get("source_reasons")
        if isinstance(source_reasons, list):
            for source_reason in source_reasons:
                if (
                    is_approved_appendix
                    and str(source_reason) in approved_drop_reasons.get(index, set())
                ):
                    continue
                reasons.append(source_reason)
        diagnostic = None
        if isinstance(bounded_crop_metrics, list):
            matches = [item for item in bounded_crop_metrics if isinstance(item, dict) and _safe_page(item.get("index")) == index]
            if len(matches) == 1:
                diagnostic = matches[0]
            elif len(matches) > 1:
                reasons.append("formula_crop_diagnostic_ambiguous")
        if diagnostic is None and candidate and not is_approved_appendix:
            reasons.append("formula_crop_diagnostic_missing")
        if diagnostic is not None:
            selected_metric_key = "context" if selected == "context" else "source"
            metric = (
                diagnostic.get(selected_metric_key)
                if isinstance(diagnostic.get(selected_metric_key), dict)
                else None
            )
            if metric is None:
                reasons.append("formula_source_crop_diagnostic_missing")
            else:
                diag_path = _text(metric.get("path"), 300)
                selected_path = _text(selected_image or candidate.get("source_image"), 300)
                if diag_path and selected_path and diag_path != selected_path:
                    reasons.append("formula_source_crop_path_mismatch")
                diag_page = _safe_page(diagnostic.get("page_no"))
                crop_page = _safe_page(metric.get("page_no"))
                cand_page = _safe_page(candidate.get("page_no"))
                if diag_page is None or crop_page != diag_page or (cand_page is not None and cand_page != diag_page):
                    reasons.append("formula_source_crop_page_mismatch")
                if metric.get("bbox") is not None and candidate.get("bbox") is not None and not _bbox_equal(metric.get("bbox"), candidate.get("bbox")):
                    reasons.append("formula_source_crop_bbox_mismatch")
                diag_sha = _sha256(metric.get("source_pdf_sha256") or diagnostic.get("source_pdf_sha256"))
                if metric.get("source_pdf_sha256") is not None and diag_sha is None:
                    reasons.append("formula_source_crop_source_hash_invalid")
                elif source_sha and diag_sha and diag_sha != source_sha:
                    reasons.append("formula_source_crop_source_hash_mismatch")
                asset_sha = _sha256(metric.get("asset_sha256"))
                if metric.get("asset_sha256") is not None and asset_sha is None:
                    reasons.append("formula_source_crop_asset_hash_invalid")
                elif asset_sha and _hash_relative_asset(output_dir, selected_image or candidate.get("source_image")) != asset_sha:
                    reasons.append("formula_source_crop_asset_hash_mismatch")
                content_sha = _sha256(
                    metric.get("formula_content_identity_sha256")
                    or diagnostic.get("formula_content_identity_sha256")
                )
                raw_sha = _sha256(
                    metric.get("formula_raw_content_sha256")
                    or diagnostic.get("formula_raw_content_sha256")
                )
                if content_sha is None:
                    reasons.append("formula_content_identity_hash_invalid")
                if raw_sha is None:
                    reasons.append("formula_raw_content_hash_invalid")
        node_item = nodes_by_formula_index.get(index)
        if not is_approved_appendix and node_item is not None:
            node_page = node_item.get("page_no")
            cand_page = _safe_page(candidate.get("page_no"))
            if cand_page is not None and node_page is not None and cand_page != node_page:
                reasons.append("formula_final_node_page_mismatch")
            if candidate.get("bbox") is not None and node_item.get("bbox") is not None and not _bbox_equal(candidate.get("bbox"), node_item.get("bbox")):
                reasons.append("formula_final_node_bbox_mismatch")
            if diagnostic is not None:
                diagnostic_source = (
                    diagnostic.get("source")
                    if isinstance(diagnostic.get("source"), dict)
                    else {}
                )
                expected_raw_sha = _sha256(
                    diagnostic.get("formula_raw_content_sha256")
                    or diagnostic_source.get("formula_raw_content_sha256")
                )
                node = node_item.get("node") if isinstance(node_item.get("node"), dict) else {}
                if expected_raw_sha is None or _formula_raw_content_sha256(node.get("text")) != expected_raw_sha:
                    reasons.append("formula_final_node_body_mismatch")
        elif not is_approved_appendix:
            reasons.append("formula_final_node_missing")
        entry = _manifest_entry(metadata, "formula", f"formula:{index}") or {}
        body_identity = entry.get("structural_body_identity_sha256")
        asset = evidence_image or candidate.get("source_image")
        record_status = "verified_semantic" if not reasons else "unresolved"
        records.append(
            _record(
                output_dir=output_dir,
                kind="formula",
                index=ordinal,
                page_no=candidate.get("page_no") or entry.get("page_no"),
                bbox=candidate.get("bbox") or entry.get("node_bbox"),
                source_ref=f"formula:index:{index}",
                body_identity=body_identity,
                source_asset=asset,
                html_anchor=f"formula:index:{index}" if index in html_indexes else None,
                markdown_anchor=(
                    f"formula:index:{index}" if index in markdown_indexes else None
                ),
                signals={
                    "formula_index": index,
                    "html_bound": index in html_indexes,
                    "markdown_bound": index in markdown_indexes,
                    "source_crop_selected": bool(selected_image),
                    "source_crop_selection": selected or None,
                    "source_provenance_verified": bool(
                        candidate.get("source_provenance_verified")
                    ),
                    "context_provenance_verified": bool(
                        candidate.get("context_provenance_verified")
                    ),
                    "appendix_only": index in appendix_indexes,
                    "approved_dropped_formula": is_approved_appendix,
                },
                status=record_status,
                critical=True,
                reasons=reasons,
                text_preview=candidate.get("selection_reason"),
            )
        )
    if expected_count == 0 and semantic_expected:
        # The semantic count and the occurrence inventory disagree.  Keep the
        # records above for diagnosis and add one bounded critical marker.
        records.append(
            _record(
                output_dir=output_dir,
                kind="formula",
                index="count-mismatch",
                source_ref="formula:index:count-mismatch",
                status="unresolved",
                critical=True,
                reasons=["formula_expected_count_zero"],
                signals={"expected_indexes": sorted(expected)},
            )
        )
    return records


def _inline_math_records(
    output_dir: Path,
    quality: dict[str, Any],
    source_visuals: dict[str, Any],
    document_json: Any,
) -> list[dict[str, Any]]:
    primary = quality.get("primary_surface")
    if not isinstance(primary, dict):
        primary = {}
    regions = primary.get("inline_math_source_regions")
    regions_declared = "inline_math_source_regions" in primary
    regions_invalid = regions_declared and not isinstance(regions, list)
    if not isinstance(regions, list):
        regions = []
    by_anchor: dict[str, dict[str, Any]] = {}
    region_duplicates: set[str] = set()
    for item in regions[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        anchor = _text(item.get("anchor"), 180)
        if not anchor:
            continue
        if anchor in by_anchor:
            region_duplicates.add(anchor)
        by_anchor[anchor] = item
    expected = _ref_set(source_visuals, "inline_math_source_expected_anchors")
    expected_declared = "inline_math_source_expected_anchors" in source_visuals
    candidates = source_visuals.get("inline_math_source_renderings")
    candidate_by_anchor: dict[str, dict[str, Any]] = {}
    candidate_duplicates: set[str] = set()
    if isinstance(candidates, dict):
        candidate_items = candidates.get("candidates")
        for item in (
            candidate_items[:MAX_LIST_ITEMS]
            if isinstance(candidate_items, list)
            else []
        ):
            if isinstance(item, dict) and _text(item.get("anchor"), 180):
                anchor = _text(item.get("anchor"), 180)
                if anchor in candidate_by_anchor:
                    candidate_duplicates.add(anchor)
                candidate_by_anchor[anchor] = item
    html = _ref_set(source_visuals, "inline_math_source_html_anchors")
    markdown = _ref_set(source_visuals, "inline_math_source_markdown_anchors")
    missing_crop = _ref_set(source_visuals, "inline_math_source_missing_crop_anchors")
    missing_html = _ref_set(source_visuals, "inline_math_source_missing_html_anchors")
    missing_markdown = _ref_set(source_visuals, "inline_math_source_missing_markdown_anchors")
    duplicate_html = _ref_set(source_visuals, "inline_math_source_duplicate_html_anchors")
    duplicate_markdown = _ref_set(source_visuals, "inline_math_source_duplicate_markdown_anchors")
    _expected_values, expected_duplicates = _ref_list(
        source_visuals, "inline_math_source_expected_anchors"
    )
    global_reasons: list[str] = []
    inline_binding_keys = (
        "inline_math_source_html_anchors",
        "inline_math_source_markdown_anchors",
        "inline_math_source_missing_crop_anchors",
        "inline_math_source_missing_html_anchors",
        "inline_math_source_missing_markdown_anchors",
        "inline_math_source_duplicate_html_anchors",
        "inline_math_source_duplicate_markdown_anchors",
    )
    for key in inline_binding_keys:
        if key not in source_visuals:
            continue
        raw_anchors = source_visuals.get(key)
        if not isinstance(raw_anchors, (list, tuple, set)):
            global_reasons.append("inline_math_binding_refs_invalid")
            continue
        if len(raw_anchors) > MAX_LIST_ITEMS:
            global_reasons.append("inline_math_binding_refs_too_many")
        if _ref_items_invalid(raw_anchors):
            global_reasons.append("inline_math_binding_ref_item_invalid")
        elif _ref_list(source_visuals, key)[1]:
            global_reasons.append("inline_math_binding_refs_duplicate")
    if regions_invalid:
        global_reasons.append("inline_math_regions_invalid")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("anchor"), str)
        or not _source_ref(item.get("anchor"))
        for item in regions[:MAX_LIST_ITEMS]
    ):
        global_reasons.append("inline_math_region_anchor_invalid")
    if source_visuals.get("inline_math_source_expected_anchors") is not None and not isinstance(source_visuals.get("inline_math_source_expected_anchors"), (list, tuple, set)):
        global_reasons.append("inline_math_expected_anchors_invalid")
    elif _ref_items_invalid(source_visuals.get("inline_math_source_expected_anchors")):
        global_reasons.append("inline_math_expected_anchor_item_invalid")
    if expected_duplicates:
        global_reasons.append("inline_math_expected_anchors_duplicate")
    if region_duplicates or candidate_duplicates:
        global_reasons.append("inline_math_candidate_anchors_duplicate")
    if len(regions) > MAX_LIST_ITEMS:
        global_reasons.append("inline_math_regions_too_many")
    raw_inline_expected = source_visuals.get("inline_math_source_expected_anchors")
    if isinstance(raw_inline_expected, (list, tuple, set)) and len(raw_inline_expected) > MAX_LIST_ITEMS:
        global_reasons.append("inline_math_expected_anchors_too_many")
    raw_inline_candidates = candidates.get("candidates") if isinstance(candidates, dict) else None
    if isinstance(raw_inline_candidates, list) and len(raw_inline_candidates) > MAX_LIST_ITEMS:
        global_reasons.append("inline_math_candidates_too_many")
    if candidates is not None and not isinstance(candidates, dict):
        global_reasons.append("inline_math_renderings_invalid")
    elif isinstance(candidates, dict) and "candidates" in candidates and not isinstance(raw_inline_candidates, list):
        global_reasons.append("inline_math_candidates_invalid")
    if isinstance(raw_inline_candidates, list) and any(
        not isinstance(item, dict) for item in raw_inline_candidates[:MAX_LIST_ITEMS]
    ):
        global_reasons.append("inline_math_candidate_invalid")
    if isinstance(raw_inline_candidates, list) and any(
        not isinstance(item.get("anchor"), str) or not _source_ref(item.get("anchor"))
        for item in raw_inline_candidates[:MAX_LIST_ITEMS]
        if isinstance(item, dict)
    ):
        global_reasons.append("inline_math_candidate_anchor_invalid")
    if set(by_anchor) != expected:
        global_reasons.append("inline_math_region_anchor_set_mismatch")
    if set(candidate_by_anchor) != expected:
        global_reasons.append("inline_math_candidate_anchor_set_mismatch")
    if html != expected or markdown != expected:
        global_reasons.append("inline_math_surface_anchor_set_mismatch")
    if (
        missing_crop
        | missing_html
        | missing_markdown
        | duplicate_html
        | duplicate_markdown
    ) - expected:
        global_reasons.append("inline_math_unexpected_diagnostic_anchors")
    counts = primary.get("counts") if isinstance(primary.get("counts"), dict) else {}
    inline_count_declarations: list[tuple[str, Any]] = []
    if "inline_math_source_region_count" in primary:
        inline_count_declarations.append(
            (
                "inline_math_source_region_count",
                primary.get("inline_math_source_region_count"),
            )
        )
    for key in ("inline_math", "inline_math_regions", "inline_math_source_regions"):
        if key in counts:
            inline_count_declarations.append((f"counts.{key}", counts.get(key)))
    parsed_inline_counts = [
        _strict_nonnegative_int(value)
        for _name, value in inline_count_declarations
    ]
    parsed_inline_count = (
        parsed_inline_counts[0] if parsed_inline_counts else None
    )
    inline_count = parsed_inline_count if parsed_inline_count is not None else 0
    if inline_count_declarations and any(
        value is None for value in parsed_inline_counts
    ):
        global_reasons.append("inline_math_expected_count_invalid")
    valid_inline_counts = {
        value for value in parsed_inline_counts if value is not None
    }
    if len(valid_inline_counts) > 1:
        global_reasons.append("inline_math_expected_count_conflict")
    if not inline_count_declarations and (regions_declared or expected_declared):
        global_reasons.append("inline_math_expected_count_missing")
    if parsed_inline_count is not None and (
        parsed_inline_count != len(regions) or parsed_inline_count != len(expected)
    ):
        global_reasons.append("inline_math_expected_count_mismatch")
    document_nodes = _document_nodes(document_json, None)
    document_nodes_by_ref: dict[str, list[dict[str, Any]]] = {}
    for node_item in document_nodes:
        node_ref = _source_ref(node_item.get("source_ref"))
        if node_ref:
            document_nodes_by_ref.setdefault(node_ref, []).append(node_item)
    has_extra_occurrences = bool(
        by_anchor
        or candidate_by_anchor
        or regions
        or html
        or markdown
        or missing_crop
        or (isinstance(raw_inline_candidates, list) and raw_inline_candidates)
    )
    if not expected and (has_extra_occurrences or inline_count > 0 or global_reasons):
        declaration_reason = (
            "inline_math_expected_anchors_empty"
            if expected_declared
            else "inline_math_expected_anchors_missing"
        )
        return [
            _record(
                output_dir=output_dir,
                kind="inline_math",
                index=0,
                source_ref="inline_math:empty-or-missing-declaration",
                status="unresolved",
                critical=True,
                reasons=[declaration_reason, *global_reasons],
                signals={
                    "region_count": len(by_anchor),
                    "candidate_count": len(candidate_by_anchor),
                },
            )
        ]
    records: list[dict[str, Any]] = []
    for ordinal, anchor in enumerate(sorted(expected), start=1):
        region = by_anchor.get(anchor) or {}
        candidate = candidate_by_anchor.get(anchor) or {}
        reasons: list[str] = list(global_reasons)
        binding_mode = _text(region.get("binding_mode"), 40) or "inline"
        if binding_mode != "inline":
            reasons.append("inline_math_occurrence_not_bound")
        if bool(region.get("unresolved")):
            reasons.append("inline_math_source_unresolved")
        if anchor not in html or anchor in missing_html or anchor in duplicate_html:
            reasons.append("inline_math_html_anchor_unverified")
        if anchor not in markdown or anchor in missing_markdown or anchor in duplicate_markdown:
            reasons.append("inline_math_markdown_anchor_unverified")
        if anchor in missing_crop or not candidate:
            reasons.append("inline_math_source_crop_missing")
        asset = candidate.get("image") or candidate.get("path")
        if not asset:
            reasons.append("inline_math_source_asset_missing")
        region_page = _safe_page(region.get("page_no"))
        candidate_page = _safe_page(candidate.get("page_no"))
        if region_page is None or candidate_page is None or candidate_page != region_page:
            reasons.append("inline_math_candidate_page_mismatch")
        if not _bbox_equal(region.get("bbox"), candidate.get("bbox")):
            reasons.append("inline_math_candidate_bbox_mismatch")
        region_part = region.get("part_index")
        candidate_part = candidate.get("part_index")
        if (
            region_part is not None
            and candidate_part is not None
            and region_part != candidate_part
        ):
            reasons.append("inline_math_candidate_part_mismatch")
        candidate_source_page = candidate.get("source_page_no")
        if (
            candidate_source_page is not None
            and _safe_page(candidate_source_page) != region_page
        ):
            reasons.append("inline_math_candidate_source_page_mismatch")
        region_source_identity = _inline_binding_identity(region.get("source_text"))
        candidate_source_text = candidate.get("source_text")
        if candidate_source_text is None:
            reasons.append("inline_math_candidate_body_missing")
        else:
            candidate_source_identity = _inline_binding_identity(candidate_source_text)
            if (
                not region_source_identity
                or not candidate_source_identity
                or region_source_identity not in candidate_source_identity
            ):
                reasons.append("inline_math_candidate_body_mismatch")
        if bool(candidate.get("unresolved")):
            reasons.append("inline_math_candidate_unresolved")
        candidate_asset_sha = _sha256(candidate.get("asset_sha256"))
        if candidate.get("asset_sha256") is not None:
            if candidate_asset_sha is None:
                reasons.append("inline_math_source_asset_hash_invalid")
            elif _hash_relative_asset(output_dir, asset) != candidate_asset_sha:
                reasons.append("inline_math_source_asset_hash_mismatch")
        explicit_body_ref = _source_ref(region.get("source_ref") or candidate.get("source_ref"))
        if explicit_body_ref:
            body_nodes = document_nodes_by_ref.get(explicit_body_ref, [])
            if len(body_nodes) != 1:
                reasons.append("inline_math_final_node_binding_missing")
            else:
                body_node_item = body_nodes[0]
                declared_page = _safe_page(
                    region.get("page_no") or candidate.get("page_no")
                )
                declared_bbox = region.get("bbox") or candidate.get("bbox")
                if (
                    declared_page is not None
                    and body_node_item.get("page_no") != declared_page
                ):
                    reasons.append("inline_math_final_node_page_mismatch")
                if declared_bbox is not None and not _bbox_equal(
                    declared_bbox, body_node_item.get("bbox")
                ):
                    reasons.append("inline_math_final_node_bbox_mismatch")
                expected_body_sha = _sha256(
                    region.get("final_body_identity_sha256")
                    or candidate.get("final_body_identity_sha256")
                )
                node = (
                    body_node_item.get("node")
                    if isinstance(body_node_item.get("node"), dict)
                    else {}
                )
                if expected_body_sha is not None:
                    if _body_identity_sha("inline_math", _structural_body_identity(node.get("text"))) != expected_body_sha:
                        reasons.append("inline_math_final_node_body_mismatch")
                else:
                    source_text = _inline_binding_identity(
                        region.get("source_text") or candidate.get("source_text")
                    )
                    node_text = _inline_binding_identity(node.get("text"))
                    if not source_text or not node_text or source_text not in node_text:
                        reasons.append("inline_math_final_node_body_unverified")
        else:
            # Inline regions emitted by semantic_reflow normally carry a
            # paragraph anchor rather than a Docling self_ref.  Bind them to
            # exactly one final text/paragraph node by page, part, bbox, and
            # visible source text; ambiguous or absent mappings fail closed.
            source_text = _inline_binding_identity(
                region.get("source_text") or candidate.get("source_text")
            )
            region_page = _safe_page(region.get("page_no") or candidate.get("page_no"))
            region_bbox = region.get("bbox") or candidate.get("bbox")
            safe_region_bbox = _safe_bbox(region_bbox)
            part_index = region.get("part_index")
            paragraph_matches: list[dict[str, Any]] = []
            collection_index = _strict_nonnegative_int(
                region.get("collection_index", candidate.get("collection_index"))
            )
            candidate_nodes = document_nodes
            collection_bound = collection_index is not None
            if collection_index is not None:
                collection_refs = [f"#/texts/{collection_index}"]
                if isinstance(part_index, int) and not isinstance(part_index, bool):
                    collection_refs.insert(
                        0, f"chunk:{part_index}:#/texts/{collection_index}"
                    )
                candidate_nodes = [
                    node_item
                    for collection_ref in collection_refs
                    for node_item in document_nodes_by_ref.get(collection_ref, [])
                ]
            if region_page is not None and safe_region_bbox is not None and source_text:
                for node_item in candidate_nodes:
                    if node_item.get("label") not in {"text", "paragraph", "list_item"}:
                        continue
                    if node_item.get("page_no") != region_page:
                        continue
                    node_part_index = node_item.get("part_index")
                    if (
                        part_index is not None
                        and node_part_index != part_index
                        and not (part_index == 0 and node_part_index is None)
                    ):
                        continue
                    bbox_bound = (
                        _bbox_contains(node_item.get("bbox"), safe_region_bbox)
                        if collection_bound
                        else _bbox_equal(safe_region_bbox, node_item.get("bbox"))
                    )
                    if not bbox_bound:
                        continue
                    node_text = _inline_binding_identity(node_item.get("text"))
                    body_bound = bool(
                        node_text
                        and source_text in node_text
                    )
                    if body_bound:
                        paragraph_matches.append(node_item)
            if len(paragraph_matches) != 1:
                reasons.append("inline_math_final_node_binding_missing_or_ambiguous")
            else:
                expected_body_sha = _sha256(
                    region.get("final_body_identity_sha256")
                    or candidate.get("final_body_identity_sha256")
                )
                if expected_body_sha is not None:
                    node = paragraph_matches[0].get("node") if isinstance(paragraph_matches[0].get("node"), dict) else {}
                    if _body_identity_sha("inline_math", _structural_body_identity(node.get("text"))) != expected_body_sha:
                        reasons.append("inline_math_final_node_body_mismatch")
        signals = {
            "binding_mode": binding_mode,
            "html_bound": anchor in html and anchor not in duplicate_html,
            "markdown_bound": anchor in markdown and anchor not in duplicate_markdown,
            "crop_present": anchor not in missing_crop and bool(candidate),
            "source_unresolved": bool(region.get("unresolved")),
        }
        records.append(
            _record(
                output_dir=output_dir,
                kind="inline_math",
                index=ordinal,
                page_no=region.get("page_no") or candidate.get("page_no"),
                bbox=region.get("bbox") or candidate.get("bbox"),
                source_ref=anchor,
                source_asset=asset,
                html_anchor=anchor if anchor in html else None,
                markdown_anchor=anchor if anchor in markdown else None,
                signals=signals,
                status="verified_semantic" if not reasons else "unresolved",
                critical=True,
                reasons=reasons,
                text_preview=region.get("source_text"),
            )
        )
    return records


def _quarantine_records(output_dir: Path, quality: dict[str, Any]) -> list[dict[str, Any]]:
    structural = quality.get("structural_quarantine_qc")
    if not isinstance(structural, dict):
        return []
    if "candidates" not in structural:
        return []
    raw_candidates = structural.get("candidates")
    if not isinstance(raw_candidates, list):
        return [
            _record(
                output_dir=output_dir,
                kind="picture_ocr",
                index="schema",
                source_ref="picture_ocr:quarantine-schema",
                status="unresolved",
                critical=True,
                reasons=["quarantine_candidates_invalid"],
            )
        ]
    candidates_overflow = len(raw_candidates) > MAX_LIST_ITEMS
    invalid_count = sum(
        1 for item in raw_candidates[:MAX_LIST_ITEMS] if not isinstance(item, dict)
    )
    # Canonical sorting makes the representative record and merged evidence
    # independent of producer list order.  Non-dicts are counted separately so
    # malformed entries cannot disappear behind a valid duplicate.
    candidates = sorted(
        (item for item in raw_candidates[:MAX_LIST_ITEMS] if isinstance(item, dict)),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)[:4096],
    )
    records: list[dict[str, Any]] = []
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    by_fingerprint: dict[tuple[str, int | None, str, str], dict[str, Any]] = {}

    def classify(candidate: dict[str, Any]) -> tuple[str | None, bool, bool, int | None, str, str | None, tuple[Any, ...]]:
        picture_overlap = _truthy(candidate.get("picture_overlap"))
        label = _text(candidate.get("label"), 64).casefold().removeprefix("quarantined_")
        kind_hint = _text(candidate.get("kind"), 80).casefold()
        header_footer = label in {"page_header", "page_footer"} or "page_header" in kind_hint or "page_footer" in kind_hint
        picture_annotation = (
            label in {"visual_annotation", "picture", "picture_ocr"}
            or kind_hint in {"visual_annotation", "picture", "picture_ocr"}
            or "visual_annotation" in kind_hint
            or "picture_ocr" in kind_hint
        )
        if not picture_overlap and not header_footer and not picture_annotation:
            return None, picture_overlap, picture_annotation, None, "", None, ()
        kind = "picture_ocr" if picture_overlap or picture_annotation else "header_footer"
        page = _safe_page(candidate.get("page_no"))
        preview = _text(candidate.get("text") or candidate.get("text_preview"))
        action = _text(candidate.get("action"), 80)
        source_ref = _source_ref(candidate.get("source_ref"))
        fingerprint = (kind, page, preview, action)
        evidence_key = (
            page,
            json.dumps(_safe_bbox(candidate.get("bbox")), ensure_ascii=False, sort_keys=True),
            _text(candidate.get("source_asset") or candidate.get("image") or candidate.get("evidence"), 300),
            _text(candidate.get("picture_overlap"), 40),
            action,
            preview,
        )
        return kind, picture_overlap, picture_annotation, page, preview, source_ref, (fingerprint, evidence_key)

    def residual_evidence(candidate: dict[str, Any]) -> tuple[list[str], bool]:
        residual = candidate.get("final_output_residual_surfaces")
        malformed = residual is None or not isinstance(residual, list)
        values: list[str] = []
        if isinstance(residual, list):
            if len(residual) > MAX_RESIDUAL_SURFACES:
                malformed = True
            for value in residual[:MAX_RESIDUAL_SURFACES]:
                if not isinstance(value, str):
                    malformed = True
                    continue
                normalized = _text(value, 80)
                if normalized and normalized not in values:
                    values.append(normalized)
        return sorted(values), malformed

    for ordinal, candidate in enumerate(candidates, start=1):
        classified = classify(candidate)
        kind, picture_overlap, picture_annotation, page, preview, source_ref, keys = classified
        if kind is None:
            continue
        fingerprint, evidence_key = keys
        residuals, malformed_residuals = residual_evidence(candidate)
        raw_overlap = candidate.get("picture_overlap")
        overlap_schema_invalid = raw_overlap is not None and not isinstance(
            raw_overlap, (bool, int, float, dict, list, tuple, set)
        )
        action = _text(candidate.get("action"), 80)
        reasons: list[str] = []
        if malformed_residuals:
            reasons.append(
                "residual_surface_schema_invalid"
                if candidate.get("final_output_residual_surfaces") is not None
                else "residual_surface_schema_missing"
            )
        if overlap_schema_invalid:
            reasons.append("picture_overlap_schema_invalid")
        if picture_annotation and not picture_overlap:
            reasons.append("picture_overlap_unproven")
        if residuals:
            reasons.append("main_flow_residual")
        if action != "quarantine_from_main_text_flow":
            reasons.append("isolation_not_proven")
        identity = (kind, source_ref) if source_ref else ("fingerprint", repr(fingerprint))
        existing = by_identity.get(identity)
        if existing is None and not source_ref:
            existing = by_fingerprint.get(fingerprint)
        if existing is not None:
            merged = existing.setdefault("signals", {}).setdefault("residual_surfaces", [])
            for value in residuals:
                if value not in merged and len(merged) < MAX_RESIDUAL_SURFACES:
                    merged.append(value)
            merged.sort()
            existing_reasons = existing.setdefault("reasons", [])
            for reason in reasons:
                if reason not in existing_reasons:
                    existing_reasons.append(reason)
            core_existing = existing.setdefault("_quarantine_core", evidence_key)
            if core_existing != evidence_key:
                if "quarantine_duplicate_evidence_conflict" not in existing_reasons:
                    existing_reasons.append("quarantine_duplicate_evidence_conflict")
            existing["reasons"] = _unique(existing_reasons)
            if existing["reasons"]:
                existing["status"] = "unresolved"
            continue
        asset = candidate.get("source_asset") or candidate.get("image") or candidate.get("evidence")
        record = _record(
            output_dir=output_dir,
            kind=kind,
            index=ordinal,
            page_no=page,
            bbox=candidate.get("bbox"),
            source_ref=source_ref or f"{kind}:{ordinal}",
            source_asset=asset,
            signals={
                "picture_overlap": picture_overlap,
                "residual_surfaces": residuals,
                "quarantine_action": action or None,
            },
            status="verified_semantic" if not reasons else "unresolved",
            critical=True,
            reasons=reasons,
            text_preview=preview,
        )
        record["_quarantine_core"] = evidence_key
        records.append(record)
        if source_ref:
            by_identity[identity] = record
        else:
            by_fingerprint[fingerprint] = record
    if invalid_count:
        records.append(
            _record(
                output_dir=output_dir,
                kind="picture_ocr",
                index="schema-item",
                source_ref="picture_ocr:quarantine-schema-item",
                status="unresolved",
                critical=True,
                reasons=["quarantine_candidate_invalid"],
                signals={"invalid_candidate_count": invalid_count},
            )
        )
    if candidates_overflow:
        records.append(
            _record(
                output_dir=output_dir,
                kind="picture_ocr",
                index="candidate-limit",
                source_ref="picture_ocr:candidate-limit",
                status="unresolved",
                critical=True,
                reasons=["quarantine_candidate_limit_exceeded"],
                signals={"candidate_count": len(raw_candidates)},
            )
        )
    for record in records:
        record.pop("_quarantine_core", None)
    return records


def _bare_picture_records(output_dir: Path, document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pictures = _document_nodes(document_json, {"picture"})
    for index, item in enumerate(pictures[:MAX_LIST_ITEMS], start=1):
        source_asset = f"pictures/picture_{index}.png"
        asset_present = _safe_asset(output_dir, source_asset) is not None
        records.append(
            _record(
                output_dir=output_dir,
                kind="picture",
                index=index,
                page_no=item.get("raw_page_no", item.get("page_no")),
                bbox=item.get("bbox"),
                source_ref=item.get("source_ref") or f"picture:{index}",
                source_asset=source_asset,
                signals={
                    "machine_binding_expected": False,
                    "visual_evidence_present": asset_present,
                },
                status="visual_only" if asset_present else "unresolved",
                critical=False,
                reasons=(
                    ["bare_picture_visual_evidence"]
                    if asset_present
                    else ["picture_visual_evidence_missing"]
                ),
                text_preview=item.get("text"),
            )
        )
    if len(pictures) > MAX_LIST_ITEMS:
        records.append(
            _record(
                output_dir=output_dir,
                kind="picture",
                index="picture-limit",
                source_ref="picture:limit",
                status="unresolved",
                critical=True,
                reasons=["picture_record_limit_exceeded"],
            )
        )
    return records


def _hash_root_file(root: Path, relative: str, *, max_bytes: int) -> str | None:
    try:
        fd = _open_relative(root, relative, max_bytes=max_bytes)
        try:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError:
        return None


def _source_identity(
    root: Path,
    metadata: dict[str, Any],
    pdf_inventory: Any,
) -> tuple[str | None, list[str], dict[str, Any]]:
    """Cross-check declared source hashes and, when present, source.pdf bytes."""

    declared: list[tuple[str, str]] = []
    metadata_keys = (
        "visual_evidence_input_sha256",
        "input_sha256",
        "source_pdf_sha256",
        "source_sha256",
        "expected_source_pdf_sha256",
    )
    if isinstance(metadata, dict):
        for key in metadata_keys:
            if key not in metadata or metadata.get(key) in (None, ""):
                continue
            digest = _sha256(metadata.get(key))
            if digest is None:
                declared.append((key, ""))
            else:
                declared.append((key, digest))
        # ``conversion_input_sha256`` may identify a normalized/semantic PDF
        # rather than the immutable upload.  Keep it observable, but do not
        # mix it into the source identity quorum above.
        if metadata.get("conversion_input_sha256") not in (None, ""):
            conversion_digest = _sha256(metadata.get("conversion_input_sha256"))
            declared.append(("conversion_input_sha256", conversion_digest or ""))
        manifest = metadata.get("structural_visual_provenance_manifest")
        if isinstance(manifest, dict):
            for key in ("visual_pdf_sha256", "source_pdf_sha256"):
                if manifest.get(key) in (None, ""):
                    continue
                digest = _sha256(manifest.get(key))
                declared.append((f"manifest.{key}", digest or ""))
    if isinstance(pdf_inventory, dict):
        for key in (
            "source_pdf_sha256",
            "source_sha256",
            "actual_source_pdf_sha256",
            "expected_source_pdf_sha256",
            "input_sha256",
        ):
            if key not in pdf_inventory or pdf_inventory.get(key) in (None, ""):
                continue
            digest = _sha256(pdf_inventory.get(key))
            declared.append((f"pdf_inventory.{key}", digest or ""))
    reasons: list[str] = []
    invalid_names = [name for name, digest in declared if not digest]
    if invalid_names:
        reasons.append("source_hash_invalid")
    values = {
        digest
        for name, digest in declared
        if digest and name != "conversion_input_sha256"
    }
    if not values:
        # The immutable source bytes are not identity-bound by the conversion
        # output unless at least one metadata, inventory, or manifest digest is
        # declared.  A conversion-only digest is intentionally not sufficient.
        reasons.append("source_hash_declaration_missing")
    if len(values) > 1:
        reasons.append("source_hash_conflict")
    actual = _hash_root_file(root, "source.pdf", max_bytes=MAX_SOURCE_PDF_BYTES)
    if actual is None:
        reasons.append("source_pdf_missing_or_unsafe")
    elif values and actual not in values:
        reasons.append("source_pdf_actual_hash_mismatch")
    chosen = actual or (sorted(values)[0] if values else None)
    return chosen, _unique(reasons), {
        "declared_sources": [name for name, _digest in declared[:32]],
        "declared_hash_count": len(declared),
        "actual_source_pdf_sha256": actual,
        "source_hashes_consistent": not reasons,
    }


def _summary(records: list[dict[str, Any]], *, truncated: bool, total: int) -> dict[str, Any]:
    by_kind = {kind: sum(1 for record in records if record.get("kind") == kind) for kind in sorted({str(record.get("kind")) for record in records})}
    by_status = {status: sum(1 for record in records if record.get("status") == status) for status in REGION_STATUSES}
    critical = [record for record in records if record.get("critical") and record.get("status") == "unresolved"]
    return {
        "by_kind": by_kind,
        "by_status": by_status,
        "critical_unresolved_count": len(critical),
        "total_record_count": total,
        "truncated": truncated,
    }


def _evaluate_regions_impl(
    output_dir: Path | str,
    document_json: Any = None,
    metadata: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    *,
    pdf_inventory: dict[str, Any] | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    write_sidecars: bool = True,
) -> dict[str, Any]:
    """Evaluate final region evidence and optionally write both sidecars.

    The input dictionaries are mutated only to append the compact quality
    signal and the two generated-output names.  Document surfaces are never
    changed.  With ``write_sidecars=True``, valid caller-provided metadata and
    status are persisted too; this keeps their failure state consistent with
    partially published sidecars.  If dictionaries are omitted they are loaded
    from the output directory, which also supports a stand-alone validator.
    """

    root = Path(output_dir)
    loaded_metadata = metadata is None
    loaded_status = status is None
    caller_metadata_invalid = metadata is not None and not isinstance(metadata, dict)
    caller_status_invalid = status is not None and not isinstance(status, dict)
    metadata_read_ok = not loaded_metadata
    status_read_ok = not loaded_status
    # Preserve caller-owned dictionaries so production adapter state receives
    # the gate's ``status.ok``/``degraded_failure`` mutation immediately.  A
    # shallow copy here would make the returned sidecars look correct while
    # the in-memory release status remained successful.
    metadata = metadata if isinstance(metadata, dict) else {}
    status = status if isinstance(status, dict) else {}
    errors: list[str] = []
    if caller_metadata_invalid:
        errors.append("metadata_json_invalid")
    if caller_status_invalid:
        errors.append("status_json_invalid")
    root_context = _root_context_for(root)
    root_safe = root_context is not None and _root_path_matches_context(root, root_context)
    if not root_safe:
        errors.append("output_dir_missing_or_unsafe")
    if document_json is None:
        document_json, error = _read_json(root / "document.json")
        if error:
            errors.append(error)
    if loaded_metadata:
        value, error = _read_json(root / "metadata.json")
        metadata_read_ok = isinstance(value, dict) and error is None
        metadata = value if isinstance(value, dict) else {}
        if error:
            errors.append(error)
        elif not metadata_read_ok:
            errors.append("metadata_json_invalid")
    if loaded_status:
        value, error = _read_json(root / "status.json")
        status_read_ok = isinstance(value, dict) and error is None
        status = value if isinstance(value, dict) else {}
        if error:
            errors.append(error)
        elif not status_read_ok:
            errors.append("status_json_invalid")
    if not isinstance(document_json, dict):
        errors.append("document_json_missing_or_invalid")
        document_json = {}
    if not document_json:
        errors.append("document_json_empty")
    elif _document_traversal_exceeds_limit(document_json):
        errors.append("document_node_limit_exceeded")
    if isinstance(max_records, bool) or not isinstance(max_records, int):
        max_records = DEFAULT_MAX_RECORDS
    max_records = max(1, min(max_records, DEFAULT_MAX_RECORDS))

    if "quality_signals" in status and not isinstance(status.get("quality_signals"), dict):
        errors.append("status_quality_signals_invalid")
        if loaded_status:
            status_read_ok = False
    if "quality_signals" in metadata and not isinstance(metadata.get("quality_signals"), dict):
        errors.append("metadata_quality_signals_invalid")
        if loaded_metadata:
            metadata_read_ok = False
    quality, source_visuals = _source_signals(status, metadata)
    if pdf_inventory is None:
        stored_inventory = None
        inventory_declared = False
        for inventory_key in ("final_pdf_inventory", "pdf_structure_inventory"):
            if inventory_key in metadata:
                inventory_declared = True
                stored_inventory = metadata.get(inventory_key)
                if isinstance(stored_inventory, dict):
                    break
        if isinstance(stored_inventory, dict):
            pdf_inventory = stored_inventory
        elif inventory_declared:
            errors.append("pdf_inventory_invalid")
            pdf_inventory = {}
    elif not isinstance(pdf_inventory, dict):
        errors.append("pdf_inventory_invalid")
        pdf_inventory = {}
    source_sha, source_identity_reasons, source_identity_diagnostics = _source_identity(
        root, metadata, pdf_inventory
    )
    errors.extend(source_identity_reasons)
    primary = quality.get("primary_surface")
    if "primary_surface" in quality and not isinstance(primary, dict):
        errors.append("primary_surface_invalid")
    if isinstance(primary, dict) and "counts" in primary and not isinstance(
        primary.get("counts"), dict
    ):
        errors.append("primary_counts_invalid")
    primary_counts = primary.get("counts") if isinstance(primary, dict) else {}
    primary_counts = primary_counts if isinstance(primary_counts, dict) else {}
    records: list[dict[str, Any]] = []
    records.extend(_quarantine_records(root, quality))
    records.extend(_bare_picture_records(root, document_json))
    table_diagnostics = _table_topology_diagnostics(document_json)
    for kind in ("table", "algorithm", "code"):
        records.extend(
            _structural_region_records(
                root,
                kind,
                source_visuals,
                metadata,
                primary_counts,
                table_diagnostics=table_diagnostics if kind == "table" else None,
                document_json=document_json,
                source_sha=source_sha,
            )
        )
    records.extend(
        _formula_region_records(
            root,
            source_visuals,
            metadata,
            primary_counts,
            document_json=document_json,
            source_sha=source_sha,
        )
    )
    records.extend(_inline_math_records(root, quality, source_visuals, document_json))

    # Stable ordering ensures identical output for identical contract inputs.
    records.sort(key=lambda item: (str(item.get("kind")), item.get("page_no") or 0, str(item.get("id"))))
    total_record_count = len(records)
    truncated = total_record_count > max_records
    if truncated:
        errors.append("region_record_limit_exceeded")
    records = records[:max_records]
    summary = _summary(records, truncated=truncated, total=total_record_count)
    failure_reasons = _unique([*errors, *[
        reason
        for record in records
        if record.get("status") == "unresolved" and record.get("critical")
        for reason in (record.get("reasons") or [])
    ]], limit=80)
    critical_unresolved = int(summary.get("critical_unresolved_count") or 0)
    ok = not failure_reasons and critical_unresolved == 0
    signals = {
        "schema_version": SCHEMA_VERSION,
        "regions_path": "regions.json",
        "source_pdf_sha256": source_sha,
        "source_identity": source_identity_diagnostics,
        "max_records": max_records,
        "record_count": len(records),
        "total_record_count": total_record_count,
        "truncated": truncated,
        "summary": summary,
        "ok": ok,
        "failure_reasons": failure_reasons,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_pdf_sha256": source_sha,
        "source_identity": source_identity_diagnostics,
        "max_records": max_records,
        "record_count": len(records),
        "total_record_count": total_record_count,
        "truncated": truncated,
        "records": records,
        "summary": summary,
        "ok": ok,
        "failure_reasons": failure_reasons,
    }

    if isinstance(status, dict):
        quality_signals = status.get("quality_signals")
        if isinstance(quality_signals, dict):
            quality_signals["region_quality_gate"] = signals
        elif "quality_signals" not in status:
            status["quality_signals"] = {"region_quality_gate": signals}
        if not ok:
            status["ok"] = False
            status["success_class"] = "degraded_failure"
            warning = "region_quality_gate_failed:" + ",".join(failure_reasons[:8] or ["critical_unresolved_region"])
            warnings = status.setdefault("warnings", [])
            if not isinstance(warnings, list):
                warnings = []
                status["warnings"] = warnings
            if warning not in warnings:
                warnings.append(warning)
    if isinstance(metadata, dict):
        metadata["region_quality_gate"] = signals

    def mark_write_failure(reasons: Iterable[str]) -> None:
        nonlocal failure_reasons
        failure_reasons = _unique([*failure_reasons, *reasons], limit=80)
        payload["failure_reasons"] = failure_reasons
        payload["ok"] = False
        signals["failure_reasons"] = failure_reasons
        signals["ok"] = False
        if isinstance(status, dict):
            status["ok"] = False
            status["success_class"] = "degraded_failure"
            warning = "region_quality_gate_failed:" + ",".join(
                failure_reasons[:8] or ["sidecar_write_failed"]
            )
            warnings = status.setdefault("warnings", [])
            if not isinstance(warnings, list):
                warnings = []
                status["warnings"] = warnings
            if warning not in warnings:
                warnings.append(warning)

    written_payloads: dict[str, Any] = {}
    sidecar_names = ("regions.json", "quality_signals.json")
    if write_sidecars and root_safe:
        sidecar_payloads = {
            "regions.json": payload,
            "quality_signals.json": signals,
        }
        sidecar_errors: list[str] = []
        for name in sidecar_names:
            write_error = _atomic_json(root / name, sidecar_payloads[name])
            if write_error:
                sidecar_errors.append(f"{name}:{write_error}")
            else:
                written_payloads[name] = sidecar_payloads[name]
        if sidecar_errors:
            mark_write_failure(sidecar_errors)

        if isinstance(metadata, dict):
            generated = metadata.setdefault("generated_outputs", [])
            if not isinstance(generated, list):
                generated = []
                metadata["generated_outputs"] = generated
            for name in sidecar_names:
                if name in written_payloads and name not in generated:
                    generated.append(name)

        caller_metadata_state_valid = bool(
            not loaded_metadata
            and not caller_metadata_invalid
            and (
                "quality_signals" not in metadata
                or isinstance(metadata.get("quality_signals"), dict)
            )
        )
        caller_status_state_valid = bool(
            not loaded_status
            and not caller_status_invalid
            and (
                "quality_signals" not in status
                or isinstance(status.get("quality_signals"), dict)
            )
        )
        state_errors: list[str] = []
        for name, should_write, state_payload in (
            (
                "metadata.json",
                (loaded_metadata and metadata_read_ok) or caller_metadata_state_valid,
                metadata,
            ),
            (
                "status.json",
                (loaded_status and status_read_ok) or caller_status_state_valid,
                status,
            ),
        ):
            if not should_write:
                continue
            write_error = _atomic_json(root / name, state_payload)
            if write_error:
                state_errors.append(f"{name}:{write_error}")
            else:
                written_payloads[name] = state_payload
        if state_errors:
            mark_write_failure(state_errors)

        # If any target failed, rewrite every successfully written target with
        # the now-failed state.  A surviving regions.json must never claim that
        # the gate passed while its companion/state file failed to publish.
        if sidecar_errors or state_errors:
            rewrite_errors: list[str] = []
            for name, state_payload in written_payloads.items():
                write_error = _atomic_json(root / name, state_payload)
                if write_error:
                    rewrite_errors.append(f"{name}:failure_state_{write_error}")
            if rewrite_errors:
                mark_write_failure(rewrite_errors)
    return {
        **payload,
        "quality_signals": signals,
        "metadata": metadata,
        "status": status,
    }


def evaluate_regions(
    output_dir: Path | str,
    document_json: Any = None,
    metadata: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    *,
    pdf_inventory: dict[str, Any] | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    write_sidecars: bool = True,
) -> dict[str, Any]:
    """Pin *output_dir* for one evaluation and release it on every exit path."""

    root = Path(output_dir)
    root_fd: int | None = None
    token = None
    try:
        root_fd, root_stat = _open_root_dir(root)
    except OSError:
        # The implementation will emit the bounded unsafe-root failure and
        # avoid all path-based writes when no pinned descriptor is available.
        root_fd = None
    if root_fd is not None:
        try:
            token = _ROOT_CONTEXT.set(
                (
                    os.path.abspath(os.fspath(root)),
                    root_fd,
                    root_stat.st_dev,
                    root_stat.st_ino,
                )
            )
        except BaseException:
            os.close(root_fd)
            root_fd = None
            raise
    try:
        return _evaluate_regions_impl(
            root,
            document_json,
            metadata,
            status,
            pdf_inventory=pdf_inventory,
            max_records=max_records,
            write_sidecars=write_sidecars,
        )
    finally:
        try:
            if token is not None:
                _ROOT_CONTEXT.reset(token)
        finally:
            if root_fd is not None:
                try:
                    os.close(root_fd)
                except OSError:
                    pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = evaluate_regions(args.output_dir, max_records=args.max_records)
    print(json.dumps({key: result[key] for key in ("ok", "summary", "failure_reasons")}, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
