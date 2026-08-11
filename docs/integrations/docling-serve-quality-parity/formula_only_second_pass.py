#!/usr/bin/env python3
"""Formula-only second-pass prototype.

Keeps Route A (Docling Serve standard pipeline) as the document backbone.
Uses Route B (VlmPipeline) as a formula-candidate source for suspicious Route A
formula text. Replaces suspect formula nodes in document.json and document.md.

Matching strategy:
1. Convert all bbox coordinates to a common space (TOPLEFT, pixel scale).
   Route A bboxes use BOTTOMLEFT PDF-point coords (origin at bottom-left, y up).
   Route B bboxes use TOPLEFT pixel coords (origin at top-left, y down).
2. Match by equation number (Route A text -> extract "(N)").
3. Fall back to vertical-center proximity on same page (threshold 100 px).

This is a minimal prototype, not a production n8n integration.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import hashlib
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# Characters in Chinese CJK Unicode blocks (U+3400 to U+9FFF)
CJK_RE = re.compile(r"[\u3400-\u9fff]")
# Equation number in formula text: ( 3 ) or (3), spaces optional
EQ_NUM_RE = re.compile(r"\(\s*(\d+)\s*\)")
# Equation number split by OCR/LaTeX spacing, e.g. ( 1 6 ) for equation 16.
SPACED_EQ_NUM_RE = re.compile(r"\(\s*((?:\d\s+)+\d)\s*\)")
# Repeated "\\ a n d" pattern (at least 3 repeats = hallucination)
REPEATED_AND_RE = re.compile(r"(\\quad \\ \\ a n d ){3,}")
# Number-only formula text (just equation numbers, nothing else)
NUMBER_ONLY_RE = re.compile(r"^\s*(\(\s*[0-9]+\s*\)\s*)+\s*$")
# Suspicious repeated single characters like \ T \ T \ T (4+ repeats)
REPEATED_SINGLE_RE = re.compile(r"(\\ [a-zA-Z]\s*){4,}")
# Keep only non-semantic whitespace normalizations; avoid semantic token rewrites.
SPACED_OPERATOR_REPLACEMENTS = ()
# Source bbox area threshold (tiny = likely wrong detection)
MIN_BBOX_AREA = 50.0  # PDF points^2
# Route B uses ~2x pixel scale (1190x1684 for a PDF page 595x842)
PIXEL_SCALE = 2.0
RIGHT_COLUMN_X_PX = 650.0

# Preserve the historical 50/120 px vertical and 180/320 px horizontal
# tolerances in page-relative space.  The prior rounded values (.05/.12 and
# .18/.32) were substantially wider on a 1190x1684 Route-B page and could bind
# an equation tag to a neighboring display formula.
REFERENCE_ROUTE_B_PAGE_WIDTH_PX = 1190.0
REFERENCE_ROUTE_B_PAGE_HEIGHT_PX = 1684.0
RELATIVE_Y_CLOSE = 50.0 / REFERENCE_ROUTE_B_PAGE_HEIGHT_PX
RELATIVE_Y_NEAR = 120.0 / REFERENCE_ROUTE_B_PAGE_HEIGHT_PX
RELATIVE_X_CLOSE = 180.0 / REFERENCE_ROUTE_B_PAGE_WIDTH_PX
RELATIVE_X_FAR = 320.0 / REFERENCE_ROUTE_B_PAGE_WIDTH_PX
EXACT_EQUATION_DUPLICATE_MIN_SCORE_MARGIN = 20.0
ORPHAN_STAGING_MIN_AGE_SECONDS = 24 * 60 * 60
FORMULA_EVIDENCE_MIN_WIDTH_PX = 18
FORMULA_EVIDENCE_MIN_HEIGHT_PX = 18
CONTEXT_EVIDENCE_MIN_WIDTH_PX = 24
CONTEXT_EVIDENCE_MIN_HEIGHT_PX = 22
FULL_PAGE_EVIDENCE_MIN_WIDTH_PX = 64
FULL_PAGE_EVIDENCE_MIN_HEIGHT_PX = 64
FORMULA_EVIDENCE_MAX_ASPECT_RATIO = 28.0
FORMULA_EVIDENCE_MIN_ASPECT_RATIO = 0.03

INPUT_FILE_PATH_KEYS = (
    "input_file_reference",
    "input_file",
    "original_input_file",
    "conversion_input_file",
    "input_file_path",
)
INPUT_SHA_KEYS = ("input_sha256", "source.sha256", "source_sha256")
VISUAL_INPUT_FILE_PATH_KEYS = (
    "visual_evidence_input_file",
    "original_input_file",
)
VISUAL_INPUT_SHA_KEY = "visual_evidence_input_sha256"
FALLBACK_INPUT_PDF_NAMES = ("source.pdf", "input.pdf")


def _path_exists_without_following(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _route_local_path_error(
    route_dir: Path,
    candidate: Path,
    *,
    contract_entry: str,
) -> dict[str, Any] | None:
    """Return a fail-closed diagnostic for an existing route contract path."""

    route_absolute = route_dir.absolute()
    candidate_absolute = candidate.absolute()
    try:
        relative_parts = candidate_absolute.relative_to(route_absolute).parts
    except ValueError:
        return {
            "reason": "resolved_path_outside_route_root",
            "contract_entry": contract_entry,
            "path": str(candidate),
            "route_root": str(route_dir),
        }

    current = route_absolute
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            return {
                "reason": "symlink_not_allowed",
                "contract_entry": contract_entry,
                "path": str(candidate),
                "symlink_component": str(current),
                "route_root": str(route_dir),
            }

    try:
        route_resolved = route_dir.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return {
            "reason": "contract_path_unresolvable",
            "contract_entry": contract_entry,
            "path": str(candidate),
            "route_root": str(route_dir),
            "detail": str(exc),
        }
    if not candidate_resolved.is_relative_to(route_resolved):
        return {
            "reason": "resolved_path_outside_route_root",
            "contract_entry": contract_entry,
            "path": str(candidate),
            "resolved_path": str(candidate_resolved),
            "route_root": str(route_dir),
        }
    if not candidate.is_file():
        return {
            "reason": "contract_path_not_regular_file",
            "contract_entry": contract_entry,
            "path": str(candidate),
            "route_root": str(route_dir),
        }
    return None


def _secure_pdf_candidate_paths(
    route_dir: Path,
    metadata: dict[str, Any] | None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Return route-local, non-symlink PDF identity candidates.

    For a legacy contract, a persisted route-local ``source.pdf``/``input.pdf``
    is authoritative and must remain inside the route root.  Once metadata
    explicitly declares the original visual input and its checksum, only that
    visual input is authoritative: conversion/source siblings must not shadow
    it.  An explicit metadata PDF may be external, but it must still be a
    readable regular file and must not itself be a symlink.
    """

    fallback_candidates = [route_dir / name for name in FALLBACK_INPUT_PDF_NAMES]
    present_fallbacks = [
        path for path in fallback_candidates if _path_exists_without_following(path)
    ]
    visual_identity_contract = _visual_identity_contract_is_declared(metadata)
    raw_candidates: list[tuple[str, Path, bool]] = []

    def add_metadata_candidates(keys: tuple[str, ...], *, authoritative: bool) -> None:
        if not isinstance(metadata, dict):
            return
        for key in keys:
            raw_value = metadata.get(key)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            raw_path = Path(raw_value.strip())
            if raw_path.is_absolute():
                candidate_options = [raw_path]
            else:
                # Prefer a route-local relative reference; retain cwd-relative
                # compatibility for adapter metadata that records its original
                # invocation path.
                route_candidate = route_dir / raw_path
                candidate_options = (
                    [route_candidate]
                    if _path_exists_without_following(route_candidate)
                    else [Path.cwd() / raw_path]
                )
            for candidate in candidate_options:
                if _path_exists_without_following(candidate):
                    raw_candidates.append((key, candidate, authoritative))

    if visual_identity_contract:
        # A text-layer recovery/conversion sibling can differ byte-for-byte
        # from the submitted PDF used for visual review.  Once the adapter
        # declares that original visual source, never let the conversion path
        # or its checksum shadow it.
        add_metadata_candidates(VISUAL_INPUT_FILE_PATH_KEYS, authoritative=True)
    else:
        raw_candidates.extend((path.name, path, True) for path in present_fallbacks)
        add_metadata_candidates(
            INPUT_FILE_PATH_KEYS,
            authoritative=not present_fallbacks,
        )

    candidates: list[Path] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for contract_entry, candidate, use_for_identity in raw_candidates:
        candidate_absolute = candidate.absolute()
        route_absolute = route_dir.absolute()
        try:
            candidate_absolute.relative_to(route_absolute)
            is_route_local = True
        except ValueError:
            is_route_local = False
        if is_route_local:
            error = _route_local_path_error(
                route_dir,
                candidate,
                contract_entry=contract_entry,
            )
        elif candidate.is_symlink():
            error = {
                "reason": "symlink_not_allowed",
                "contract_entry": contract_entry,
                "path": str(candidate),
                "symlink_component": str(candidate),
                "route_root": str(route_dir),
            }
        elif not candidate.is_file():
            error = {
                "reason": "contract_path_not_regular_file",
                "contract_entry": contract_entry,
                "path": str(candidate),
                "route_root": str(route_dir),
            }
        else:
            try:
                candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                error = {
                    "reason": "contract_path_unresolvable",
                    "contract_entry": contract_entry,
                    "path": str(candidate),
                    "route_root": str(route_dir),
                    "detail": str(exc),
                }
            else:
                error = None
        if error is not None:
            errors.append(error)
            continue
        if not use_for_identity:
            continue
        try:
            resolved_key = str(candidate.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        candidates.append(candidate)
    return candidates, errors


def _validate_route_contract_security(
    route_dir: Path,
    route_label: str,
    *,
    apply_all: bool,
) -> dict[str, Any] | None:
    """Reject symlinked or escaping files before reading a route contract."""

    if route_dir.is_symlink():
        return {
            "ok": False,
            "error": "route_contract_path_security_violation",
            "route": route_label,
            "reason": "route_root_symlink_not_allowed",
            "route_root": str(route_dir),
        }

    names = ["document.json", "document.md"]
    if apply_all:
        names.extend(["status.json", "metadata.json"])
    for name in names:
        path = route_dir / name
        if not _path_exists_without_following(path):
            continue
        error = _route_local_path_error(route_dir, path, contract_entry=name)
        if error is not None:
            return {
                "ok": False,
                "error": "route_contract_path_security_violation",
                "route": route_label,
                **error,
            }

    if apply_all:
        metadata = load_json(route_dir / "metadata.json")
        _, pdf_errors = _secure_pdf_candidate_paths(route_dir, metadata)
        if pdf_errors:
            return {
                "ok": False,
                "error": "route_contract_path_security_violation",
                "route": route_label,
                **pdf_errors[0],
                "pdf_candidate_errors": pdf_errors,
            }
    return None


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _extract_input_sha256(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None

    def _from_path(data: Any, path: str) -> Any:
        current: Any = data
        for key in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    if _visual_identity_contract_is_declared(metadata):
        visual_sha = str(metadata.get(VISUAL_INPUT_SHA_KEY) or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", visual_sha):
            return visual_sha

    for key in INPUT_SHA_KEYS:
        literal_value = metadata.get(key)
        value = literal_value if isinstance(literal_value, str) else _from_path(metadata, key)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", normalized):
                return normalized
    return None


def _visual_identity_contract_is_declared(
    metadata: dict[str, Any] | None,
) -> bool:
    if not isinstance(metadata, dict):
        return False
    visual_sha = str(metadata.get(VISUAL_INPUT_SHA_KEY) or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", visual_sha):
        return False
    return any(
        isinstance(metadata.get(key), str) and bool(str(metadata.get(key)).strip())
        for key in VISUAL_INPUT_FILE_PATH_KEYS
    )


def _iter_pdf_candidate_paths(route_dir: Path, metadata: dict[str, Any] | None) -> list[Path]:
    candidates, _ = _secure_pdf_candidate_paths(route_dir, metadata)
    return candidates


def _sha256sum(path: Path) -> str | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        hasher = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as f:
            descriptor = None
            while chunk := f.read(8192):
                hasher.update(chunk)
        return hasher.hexdigest().lower()
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _has_pdf_header(path: Path) -> bool:
    """Return whether the production parser can open a non-empty PDF.

    ISO 32000 readers may accept a short preamble before the header, so retain
    the bounded signature scan.  A signature alone is not evidence of a PDF:
    the same parser used by the production adapter must open at least one page.
    """

    try:
        if not path.is_file():
            return False
        with path.open("rb") as source:
            if b"%PDF-" not in source.read(1024):
                return False
        try:
            import pypdfium2 as pdfium  # type: ignore
        except ImportError:
            # Developer workstations may use the adapter's PyMuPDF fallback;
            # the deployed/formal environment provides pypdfium2.
            import fitz  # type: ignore

            with fitz.open(str(path)) as document:
                if document.page_count < 1:
                    return False
                # Loading the first page catches damaged page trees that
                # merely advertise a positive count in the catalog.
                page = document.load_page(0)
                return bool(
                    math.isfinite(float(page.rect.width))
                    and math.isfinite(float(page.rect.height))
                    and page.rect.width > 0
                    and page.rect.height > 0
                )

        document = pdfium.PdfDocument(str(path))
        page = None
        try:
            if len(document) < 1:
                return False
            # Force PDFium to resolve and load the page rather than trusting a
            # catalog count from a corrupt trailer.
            page = document.get_page(0)
            width, height = page.get_size()
            return bool(
                math.isfinite(float(width))
                and math.isfinite(float(height))
                and width > 0
                and height > 0
            )
        finally:
            if page is not None:
                page.close()
            document.close()
    except Exception:
        return False


def _route_input_sha256(route_dir: Path) -> tuple[str | None, dict[str, Any]]:
    """Return SHA-256 for a route output's source pdf and diagnostic context."""
    metadata_path = route_dir / "metadata.json"
    if _path_exists_without_following(metadata_path):
        metadata_path_error = _route_local_path_error(
            route_dir,
            metadata_path,
            contract_entry="metadata.json",
        )
        if metadata_path_error is not None:
            return None, {
                "status": "contract_violation",
                "reason": "route_contract_path_security_violation",
                **metadata_path_error,
            }
    metadata = load_json(route_dir / "metadata.json")
    visual_identity_contract = _visual_identity_contract_is_declared(metadata)
    if not visual_identity_contract and isinstance(metadata, dict) and (
        metadata.get("input_file_reference_verified") is False
        or str(metadata.get("input_file_reference_mode") or "")
        in {"existing_mismatch", "mismatch"}
    ):
        return None, {
            "metadata_sha256": _extract_input_sha256(metadata),
            "candidate_path": str(metadata.get("input_file_reference") or "") or None,
            "candidate_sha256": None,
            "status": "metadata_mismatch",
            "reason": "input_file_reference_not_verified",
            "input_file_reference_mode": metadata.get("input_file_reference_mode"),
        }
    declared_sha = _extract_input_sha256(metadata)
    candidates, candidate_errors = _secure_pdf_candidate_paths(route_dir, metadata)
    if candidate_errors:
        return None, {
            "metadata_sha256": declared_sha,
            "candidate_path": candidate_errors[0].get("path"),
            "candidate_sha256": None,
            "status": "contract_violation",
            "reason": "route_pdf_candidate_security_violation",
            "candidate_errors": candidate_errors,
        }
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        if not _has_pdf_header(candidate):
            return None, {
                "metadata_sha256": declared_sha,
                "candidate_path": str(candidate),
                "candidate_sha256": None,
                "status": "invalid_pdf",
                "reason": "input_pdf_header_missing",
            }
        computed = _sha256sum(candidate)
        detail = {
            "metadata_sha256": declared_sha,
            "candidate_path": str(candidate),
            "candidate_sha256": computed,
            "identity_mode": (
                "visual_evidence_original_pdf"
                if visual_identity_contract
                else "legacy_input_pdf"
            ),
        }
        if computed is not None and declared_sha is None:
            detail["status"] = "unverified"
            detail["reason"] = "persisted_input_pdf_checksum_missing"
            return None, detail
        if declared_sha and computed and computed != declared_sha:
            detail["status"] = "metadata_mismatch"
            detail["reason"] = "input_pdf_checksum_mismatch"
            return None, detail
        if computed is not None:
            detail["status"] = "ok"
            detail["reason"] = "checksum_verified_from_file"
            return computed, detail

    return None, {
        "metadata_sha256": declared_sha,
        "candidate_paths": [str(path) for path in candidates],
        "candidate_path": None,
        "candidate_sha256": None,
        "status": "unverified",
        "reason": "no_readable_source_pdf_for_route",
    }


def iter_nodes(obj: Any):
    """Yield every dict node in the document tree."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_nodes(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from iter_nodes(x)


def _normalize_document_chunk_pages(document: dict[str, Any]) -> dict[str, Any]:
    """Copy a chunked document and map local provenance to physical pages."""

    raw_chunks = document.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        return document

    chunk_entries: list[tuple[tuple[int, int], int, dict[str, Any]]] = []
    for original_index, raw_chunk in enumerate(raw_chunks):
        if not isinstance(raw_chunk, dict) or not isinstance(
            raw_chunk.get("document"), dict
        ):
            continue
        page_range = raw_chunk.get("page_range")
        start = (
            _coerce_page_no(page_range[0])
            if isinstance(page_range, (list, tuple)) and page_range
            else None
        )
        end = (
            _coerce_page_no(page_range[1])
            if isinstance(page_range, (list, tuple)) and len(page_range) > 1
            else start
        )
        chunk_entries.append(
            (
                (
                    start if start is not None else original_index + 1,
                    end if end is not None else -1,
                ),
                original_index,
                raw_chunk,
            )
        )

    if not chunk_entries:
        return document

    normalized = copy.deepcopy(document)
    normalized_chunks: list[dict[str, Any]] = []
    for part_index, (_sort_key, _original_index, raw_chunk) in enumerate(
        sorted(chunk_entries, key=lambda item: (item[0], item[1]))
    ):
        chunk = copy.deepcopy(raw_chunk)
        part = chunk["document"]
        page_range = chunk.get("page_range")
        start = (
            _coerce_page_no(page_range[0])
            if isinstance(page_range, (list, tuple)) and page_range
            else None
        )
        end = (
            _coerce_page_no(page_range[1])
            if isinstance(page_range, (list, tuple)) and len(page_range) > 1
            else start
        )
        length = (
            end - start + 1
            if start is not None and end is not None and end >= start
            else None
        )

        raw_pages = part.get("pages")
        if isinstance(raw_pages, dict):
            page_items = list(raw_pages.items())
        elif isinstance(raw_pages, list):
            page_items = list(enumerate(raw_pages, start=1))
        else:
            page_items = []
        page_numbers = {
            page_no
            for raw_page_no, _record in page_items
            if (page_no := _coerce_page_no(raw_page_no)) is not None
        }
        provenance_page_numbers: set[int] = set()
        for current in list(iter_nodes(part)):
            provs = current.get("prov")
            if isinstance(provs, dict):
                provs = [provs]
            if not isinstance(provs, list):
                continue
            for prov in provs:
                if not isinstance(prov, dict):
                    continue
                page_no = _coerce_page_no(prov.get("page_no"))
                if page_no is not None:
                    provenance_page_numbers.add(page_no)
        numbering_evidence = page_numbers or provenance_page_numbers
        local_numbering = bool(
            start is not None
            and length is not None
            and numbering_evidence
            and all(1 <= page_no <= length for page_no in numbering_evidence)
            and (
                start == 1
                or 1 in numbering_evidence
                or any(page_no < start for page_no in numbering_evidence)
            )
        )

        def global_page_number(value: Any) -> int | None:
            page_no = _coerce_page_no(value)
            if page_no is None:
                return None
            if (
                local_numbering
                and start is not None
                and length is not None
                and 1 <= page_no <= length
            ):
                return start + page_no - 1
            if start is not None and end is not None and start <= page_no <= end:
                return page_no
            if start is not None and length is not None and 1 <= page_no <= length:
                return start + page_no - 1
            return page_no

        if page_items:
            normalized_page_map: dict[str, Any] = {}
            for raw_page_no, record in page_items:
                page_no = global_page_number(raw_page_no)
                if page_no is not None:
                    normalized_page_map[str(page_no)] = record
            part["pages"] = normalized_page_map

        for current in list(iter_nodes(part)):
            if any(key in current for key in ("label", "self_ref", "prov")):
                current["_local_ai_lab_chunk_part_index"] = part_index
            provs = current.get("prov")
            if isinstance(provs, dict):
                provs = [provs]
                current["prov"] = provs
            if not isinstance(provs, list):
                continue
            for prov in provs:
                if isinstance(prov, dict) and "page_no" in prov:
                    prov["page_no"] = global_page_number(prov.get("page_no"))
        normalized_chunks.append(chunk)

    normalized["chunks"] = normalized_chunks
    return normalized


def _document_page_sizes(doc: dict[str, Any]) -> dict[int, tuple[float, float]]:
    sizes: dict[int, tuple[float, float]] = {}
    ambiguous_pages: set[int] = set()
    documents = [doc]
    raw_chunks = doc.get("chunks")
    if isinstance(raw_chunks, list):
        documents.extend(
            chunk["document"]
            for chunk in raw_chunks
            if isinstance(chunk, dict) and isinstance(chunk.get("document"), dict)
        )
    for document in documents:
        pages = document.get("pages")
        if isinstance(pages, dict):
            page_items = pages.items()
        elif isinstance(pages, list):
            page_items = enumerate(pages, start=1)
        else:
            continue
        for raw_page_number, page in page_items:
            if not isinstance(page, dict) or not isinstance(page.get("size"), dict):
                continue
            page_number = _coerce_page_no(raw_page_number)
            try:
                width = float(page["size"].get("width") or 0.0)
                height = float(page["size"].get("height") or 0.0)
            except (TypeError, ValueError):
                continue
            if (
                page_number is None
                or width <= 0.0
                or height <= 0.0
                or not math.isfinite(width)
                or not math.isfinite(height)
            ):
                continue
            size = (width, height)
            if page_number in ambiguous_pages:
                continue
            if page_number in sizes and sizes[page_number] != size:
                sizes.pop(page_number, None)
                ambiguous_pages.add(page_number)
                continue
            sizes[page_number] = size
    return sizes


def _coerce_page_no(value: Any) -> int | None:
    """Return a strictly integral, positive page number or ``None``.

    Route payloads are external data.  In particular, ``page_no`` has been
    observed as ``"1"``, ``1.0`` and occasionally malformed strings.  Never
    let a malformed value escape into ``int(...)`` calls in the matching path:
    an untrusted page anchor simply loses geometry verification and is rejected
    by apply-all.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and value > 0:
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[1-9][0-9]*", text):
            return int(text)
    return None


def _coerce_page_order(value: Any, default: int = 0) -> int:
    """Coerce a non-negative reading-order value without raising."""
    if isinstance(value, bool):
        return default
    try:
        order = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return order if order >= 0 else default


def _bbox_values_are_finite(bbox: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        values = tuple(float(bbox[key]) for key in ("l", "r", "t", "b"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values  # type: ignore[return-value]


def _bbox_within_page(
    values: tuple[float, float, float, float],
    page_size: tuple[float, float] | None,
    coord_origin: str,
) -> bool:
    """Validate all bbox coordinates against a declared page extent."""
    if page_size is None:
        return False
    width, height = page_size
    if not (
        math.isfinite(width)
        and math.isfinite(height)
        and width > 0.0
        and height > 0.0
    ):
        return False
    l, r, t, b = values
    if not (r > l and _bbox_vertical_order_is_valid(values, coord_origin)):
        return False
    # A tiny tolerance allows a renderer's rounded edge to land one ULP past
    # the declared page while still rejecting genuinely out-of-bounds crops.
    tolerance = max(width, height) * 1e-6
    return (
        min(l, r) >= -tolerance
        and max(l, r) <= width + tolerance
        and min(t, b) >= -tolerance
        and max(t, b) <= height + tolerance
    )


def _bbox_vertical_order_is_valid(
    values: tuple[float, float, float, float],
    coord_origin: str,
) -> bool:
    """Validate top/bottom ordering in the bbox's declared coordinate space."""

    _, _, t, b = values
    if coord_origin == "TOPLEFT":
        return b > t
    if coord_origin == "BOTTOMLEFT":
        return t > b
    return False


def _normalized_bbox(
    bbox_norm: dict[str, float] | None,
    page_size: tuple[float, float] | None,
) -> dict[str, float] | None:
    if bbox_norm is None or page_size is None:
        return None
    width, height = page_size
    if width <= 0.0 or height <= 0.0:
        return None
    return {
        "l": bbox_norm["l"] / width,
        "r": bbox_norm["r"] / width,
        "t": bbox_norm["t"] / height,
        "b": bbox_norm["b"] / height,
    }


def extract_formulas(
    doc: dict[str, Any],
    *,
    target_page_sizes: dict[int, tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Return all formula nodes with normalized provenance info."""

    source_page_sizes = _document_page_sizes(doc)

    results: list[dict[str, Any]] = []
    ordered_nodes = [
        node
        for node in iter_nodes(doc)
        if isinstance(node, dict) and isinstance(node.get("text"), str)
    ]
    page_formula_orders: dict[int | None, int] = {}
    for reading_order, node in enumerate(ordered_nodes):
        if not isinstance(node, dict):
            continue
        label = str(node.get("label", "")).lower()
        if label != "formula":
            continue
        text = str(node.get("text", "") or "")
        prov = node.get("prov") or []
        if isinstance(prov, dict):
            prov = [prov]
        if not isinstance(prov, list):
            prov = []

        # Extract page number and bbox from prov.  Page numbers are external
        # payload, so normalize them once and never call int(...) on raw data.
        page_no: int | None = None
        bbox_raw: dict[str, Any] | None = None
        for p in prov:
            if isinstance(p, dict):
                if page_no is None:
                    page_no = _coerce_page_no(p.get("page_no"))
                if bbox_raw is None:
                    bbox_raw = p.get("bbox") or {}

        # Normalize bbox to TOPLEFT pixel scale:
        # Route A: BOTTOMLEFT PDF-point coords -> TOPLEFT at PIXEL_SCALE
        # Route B: TOPLEFT pixel coords -> keep as-is
        bbox_norm: dict[str, float] | None = None
        bbox_rel: dict[str, float] | None = None
        geometry_verified = False
        geometry_reason = "page_or_bbox_geometry_missing"
        if bbox_raw and isinstance(bbox_raw, dict):
            l_px = r_px = t_top_px = b_top_px = None
            coord_origin = str(bbox_raw.get("coord_origin") or "").upper()
            raw_values = _bbox_values_are_finite(bbox_raw)
            l, r, t, b = raw_values if raw_values is not None else (0.0, 0.0, 0.0, 0.0)

            source_size = source_page_sizes.get(page_no) if page_no is not None else None
            target_size = (
                target_page_sizes.get(page_no)
                if target_page_sizes is not None and page_no is not None
                else None
            )
            if raw_values is None:
                geometry_reason = "bbox_values_invalid"
            elif page_no is None:
                geometry_reason = "page_no_invalid"
            elif coord_origin == "BOTTOMLEFT" and source_size is not None:
                source_width, source_height = source_size
                if target_page_sizes is not None and target_size is None:
                    # A caller that asks for cross-route geometry must provide
                    # the corresponding target page.  Do not silently fall
                    # back to the historical fixed-DPI assumption.
                    l_px = r_px = t_top_px = b_top_px = None
                    geometry_reason = "target_page_size_missing"
                elif not _bbox_vertical_order_is_valid(raw_values, coord_origin):
                    l_px = r_px = t_top_px = b_top_px = None
                    geometry_reason = "bbox_vertical_order_invalid"
                elif not _bbox_within_page(raw_values, source_size, coord_origin):
                    l_px = r_px = t_top_px = b_top_px = None
                    geometry_reason = "bbox_out_of_source_page_bounds"
                else:
                    target_width, target_height = target_size or (
                        source_width * PIXEL_SCALE,
                        source_height * PIXEL_SCALE,
                    )
                    x_scale = target_width / source_width
                    y_scale = target_height / source_height
                    # Convert BOTTOMLEFT source units into the target's
                    # TOPLEFT coordinate space using declared page extents.
                    l_px = l * x_scale
                    r_px = r * x_scale
                    t_top_px = (source_height - t) * y_scale
                    b_top_px = (source_height - b) * y_scale
                    geometry_reason = "verified"
            elif coord_origin == "TOPLEFT":
                # Already TOPLEFT (pixel coords).  Unlike the historical
                # implementation, a declared page extent is mandatory: with
                # no page size we cannot distinguish a valid crop from one
                # outside the page.
                page_size = (
                    target_size if target_page_sizes is not None else source_size
                )
                if raw_values is None:
                    l_px = r_px = t_top_px = b_top_px = None
                    geometry_reason = "bbox_values_invalid"
                elif not _bbox_vertical_order_is_valid(raw_values, coord_origin):
                    l_px = r_px = t_top_px = b_top_px = None
                    geometry_reason = "bbox_vertical_order_invalid"
                elif not _bbox_within_page(raw_values, page_size, coord_origin):
                    l_px = r_px = t_top_px = b_top_px = None
                    geometry_reason = (
                        (
                            "target_page_size_missing"
                            if target_page_sizes is not None and target_size is None
                            else "page_size_missing"
                            if page_size is None
                            else "bbox_out_of_page_bounds"
                        )
                    )
                else:
                    l_px = l
                    r_px = r
                    t_top_px = t
                    b_top_px = b
                    geometry_reason = "verified"
            elif coord_origin:
                l_px = r_px = t_top_px = b_top_px = None
                geometry_reason = "coordinate_origin_unsupported"
            else:
                l_px = r_px = t_top_px = b_top_px = None
                geometry_reason = "coordinate_origin_missing"

            if raw_values is not None and page_no is not None and coord_origin == "BOTTOMLEFT" and source_size is None:
                l_px = r_px = t_top_px = b_top_px = None
                geometry_reason = "source_page_size_missing"

            if None not in (l_px, r_px, t_top_px, b_top_px) and geometry_reason == "verified":
                bbox_norm = {
                    "l": float(l_px),
                    "r": float(r_px),
                    "t": float(t_top_px),
                    "b": float(b_top_px),
                }
                page_size = target_size
                if page_size is None and source_size is not None:
                    page_size = (
                        source_size
                        if coord_origin == "TOPLEFT"
                        else (source_size[0] * PIXEL_SCALE, source_size[1] * PIXEL_SCALE)
                    )
                bbox_rel = _normalized_bbox(bbox_norm, page_size)
                geometry_verified = bbox_rel is not None
                if not geometry_verified:
                    geometry_reason = "page_size_missing"

        # Extract equation numbers from formula text. Use the trailing number as
        # the main anchor because display equations normally carry their number
        # at the right edge, and OCR may also mention other integers in the body.
        eq_numbers = _extract_eq_numbers_from_text(text)
        main_eq: int | None = eq_numbers[-1] if eq_numbers else None
        page_order = page_formula_orders.get(page_no, 0)
        page_formula_orders[page_no] = page_order + 1
        nearby_before = [
            str(ordered_nodes[index].get("text") or "")[:160]
            for index in range(max(0, reading_order - 2), reading_order)
            if str(ordered_nodes[index].get("label") or "").lower() != "formula"
        ]
        nearby_after = [
            str(ordered_nodes[index].get("text") or "")[:160]
            for index in range(reading_order + 1, min(len(ordered_nodes), reading_order + 3))
            if str(ordered_nodes[index].get("label") or "").lower() != "formula"
        ]
        nearby_text = nearby_before + nearby_after
        formula_no = len(results) + 1
        raw_part_index = node.get("_local_ai_lab_chunk_part_index")
        part_index = (
            raw_part_index
            if isinstance(raw_part_index, int) and not isinstance(raw_part_index, bool)
            else None
        )
        anchor_id = f"formula-{formula_no}-page-{page_no or 'unknown'}-order-{page_order}"
        if part_index is not None:
            anchor_id += f"-part-{part_index}"

        results.append({
            "formula_no": formula_no,
            "anchor_id": anchor_id,
            "part_index": part_index,
            "reading_order": reading_order,
            "page_order": page_order,
            "nearby_text": nearby_text,
            "nearby_before": nearby_before,
            "nearby_after": nearby_after,
            "text": text,
            "page_no": page_no,
            "bbox_norm": bbox_norm,  # TOPLEFT pixel space
            "bbox_rel": bbox_rel,  # TOPLEFT page-relative coordinates
            "geometry_verified": geometry_verified,
            "geometry_reason": geometry_reason,
            "bbox_raw": bbox_raw,     # original coords for reference
            "eq_numbers": eq_numbers,
            "main_eq": main_eq,
            "prov": prov,
            "node": node,
        })
    return results


def formula_vertical_center(bbox: dict[str, float]) -> float:
    """Center y in TOPLEFT space."""
    return (bbox["t"] + bbox["b"]) / 2


def formula_horizontal_center(bbox: dict[str, float]) -> float:
    """Center x in TOPLEFT space."""
    return (bbox["l"] + bbox["r"]) / 2


def _formula_bbox_summary(bbox: dict[str, float] | None) -> dict[str, float] | None:
    if bbox is None:
        return None
    return {
        "x_center": round(formula_horizontal_center(bbox), 2),
        "y_center": round(formula_vertical_center(bbox), 2),
        "width": round(abs(bbox["r"] - bbox["l"]), 2),
        "height": round(abs(bbox["b"] - bbox["t"]), 2),
    }


def _formula_geometry_verified(formula: dict[str, Any] | None) -> bool:
    """Return whether a formula carries an explicitly verified page bbox."""
    if not isinstance(formula, dict) or formula.get("geometry_verified") is not True:
        return False
    bbox = formula.get("bbox_norm")
    if not isinstance(bbox, dict):
        return False
    values = _bbox_values_are_finite(bbox)
    if values is None or not (
        values[1] > values[0]
        and _bbox_vertical_order_is_valid(values, "TOPLEFT")
    ):
        return False
    rel = formula.get("bbox_rel")
    if rel is not None:
        rel_values = _bbox_values_are_finite(rel) if isinstance(rel, dict) else None
        if rel_values is None:
            return False
        if not _bbox_vertical_order_is_valid(rel_values, "TOPLEFT"):
            return False
        if not (
            min(rel_values[0], rel_values[1], rel_values[2], rel_values[3]) >= -1e-6
            and max(rel_values[0], rel_values[1], rel_values[2], rel_values[3]) <= 1.000001
        ):
            return False
    return True


def _formula_matching_bbox(formula: dict[str, Any]) -> tuple[dict[str, float] | None, str]:
    """Prefer page-relative geometry while retaining legacy crop bbox support."""
    rel = formula.get("bbox_rel")
    if isinstance(rel, dict) and _bbox_values_are_finite(rel) is not None:
        return rel, "relative"
    bbox = formula.get("bbox_norm")
    if isinstance(bbox, dict) and _bbox_values_are_finite(bbox) is not None:
        return bbox, "pixel"
    return None, "missing"


def _visual_evidence_is_usable(
    path: Path,
    evidence_kind: str = "formula_crop",
) -> bool:
    """Require a decodable PNG/JPEG with dimensions and visible content."""

    try:
        with path.open("rb") as source:
            magic = source.read(12)
    except OSError:
        return False
    if not (
        magic.startswith(b"\x89PNG\r\n\x1a\n")
        or magic.startswith(b"\xff\xd8\xff")
    ):
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            if image.format not in {"PNG", "JPEG"}:
                return False
            minima = {
                "formula_crop": (
                    FORMULA_EVIDENCE_MIN_WIDTH_PX,
                    FORMULA_EVIDENCE_MIN_HEIGHT_PX,
                ),
                "formula_context": (
                    CONTEXT_EVIDENCE_MIN_WIDTH_PX,
                    CONTEXT_EVIDENCE_MIN_HEIGHT_PX,
                ),
                "full_page": (
                    FULL_PAGE_EVIDENCE_MIN_WIDTH_PX,
                    FULL_PAGE_EVIDENCE_MIN_HEIGHT_PX,
                ),
            }
            min_width, min_height = minima.get(
                evidence_kind,
                minima["formula_crop"],
            )
            if image.width < min_width or image.height < min_height:
                return False
            aspect = image.width / max(image.height, 1)
            if (
                aspect < FORMULA_EVIDENCE_MIN_ASPECT_RATIO
                or (
                    aspect > FORMULA_EVIDENCE_MAX_ASPECT_RATIO
                    and image.height < 40
                )
            ):
                return False
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            grayscale = background.convert("L")
            low, high = grayscale.getextrema()
            if high - low < 8:
                return False
            histogram = grayscale.histogram()
            total = max(1, grayscale.width * grayscale.height)
            non_dominant = total - max(histogram)
            return non_dominant >= max(4, int(total * 0.0002))
    except Exception:
        return False


def _formula_evidence_body_sha256(value: str) -> str | None:
    body = _markdown_identity_body(value)
    if not body:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _render_pdf_page_image(source_pdf: Path, page_no: int) -> Any | None:
    """Render one source page at the adapter's authoritative 2x scale."""

    if page_no <= 0:
        return None
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
            from PIL import Image

            with fitz.open(str(source_pdf)) as document:
                if page_no > document.page_count:
                    return None
                page = document.load_page(page_no - 1)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        except Exception:
            return None

    document = page = bitmap = None
    try:
        document = pdfium.PdfDocument(str(source_pdf))
        if page_no > len(document):
            return None
        page = document.get_page(page_no - 1)
        bitmap = page.render(scale=2.0)
        return bitmap.to_pil().convert("RGB").copy()
    except Exception:
        return None
    finally:
        for resource in (bitmap, page, document):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _verified_pdf_render_snapshot(
    source_pdf: Path,
    expected_sha256: str,
    destination_dir: Path,
) -> Path | None:
    """Copy one no-follow source fd to an immutable, checksum-bound snapshot."""

    source_descriptor: int | None = None
    snapshot_descriptor: int | None = None
    snapshot_path: Path | None = None
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        source_descriptor = os.open(
            source_pdf,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            return None
        snapshot_descriptor, snapshot_name = tempfile.mkstemp(
            prefix=".verified_visual_source_",
            suffix=".pdf",
            dir=destination_dir,
        )
        snapshot_path = Path(snapshot_name)
        hasher = hashlib.sha256()
        with (
            os.fdopen(source_descriptor, "rb") as source,
            os.fdopen(snapshot_descriptor, "wb") as snapshot,
        ):
            source_descriptor = None
            snapshot_descriptor = None
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
                snapshot.write(chunk)
            snapshot.flush()
            os.fsync(snapshot.fileno())
        if hasher.hexdigest().lower() != expected_sha256.lower():
            snapshot_path.unlink(missing_ok=True)
            return None
        if not _has_pdf_header(snapshot_path):
            snapshot_path.unlink(missing_ok=True)
            return None
        return snapshot_path
    except OSError:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        return None
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if snapshot_descriptor is not None:
            os.close(snapshot_descriptor)


def _authoritative_formula_crop(
    output_dir: Path,
    namespace: str,
    source_dir: Path,
    route_source_sha256: str | None,
    formula: dict[str, Any] | None,
    expected_formula_index: int | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Rerender a formula crop from the checksum-verified visual source PDF."""

    if (
        not isinstance(formula, dict)
        or formula.get("geometry_verified") is not True
        or not isinstance(expected_formula_index, int)
        or expected_formula_index <= 0
        or not isinstance(route_source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", route_source_sha256)
    ):
        return None, None
    page_no = _coerce_page_no(formula.get("page_no"))
    bbox_rel = formula.get("bbox_rel")
    values = _bbox_values_are_finite(bbox_rel) if isinstance(bbox_rel, dict) else None
    if (
        page_no is None
        or values is None
        or not _bbox_vertical_order_is_valid(values, "TOPLEFT")
        or min(values) < -1e-6
        or max(values) > 1.000001
    ):
        return None, None
    verified_sha, source_detail = _route_input_sha256(source_dir)
    source_path_value = source_detail.get("candidate_path")
    if (
        verified_sha != route_source_sha256
        or not isinstance(source_path_value, str)
        or not source_path_value
    ):
        return None, None
    source_pdf = Path(source_path_value)
    authoritative_dir = output_dir / "evidence" / namespace / "authoritative"
    render_snapshot = _verified_pdf_render_snapshot(
        source_pdf,
        route_source_sha256,
        authoritative_dir,
    )
    if render_snapshot is None:
        return None, None
    try:
        page_image = _render_pdf_page_image(render_snapshot, page_no)
    finally:
        render_snapshot.unlink(missing_ok=True)
    if page_image is None or page_image.width <= 0 or page_image.height <= 0:
        return None, None

    l, r, t, b = values
    padding = 2
    x0 = max(0, math.floor(min(l, r) * page_image.width) - padding)
    x1 = min(page_image.width, math.ceil(max(l, r) * page_image.width) + padding)
    y0 = max(0, math.floor(min(t, b) * page_image.height) - padding)
    y1 = min(page_image.height, math.ceil(max(t, b) * page_image.height) + padding)
    if x1 <= x0 or y1 <= y0:
        return None, None
    crop = page_image.crop((x0, y0, x1, y1)).convert("RGB")
    destination = authoritative_dir / f"formula_{expected_formula_index}_page_{page_no}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(temporary_descriptor, "wb") as target:
            temporary_descriptor = None
            crop.save(target, format="PNG")
            target.flush()
            os.fsync(target.fileno())
        temporary_path.replace(destination)
        temporary_path = None
    except (OSError, ValueError):
        return None, None
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if (
        _sha256sum(source_pdf) != route_source_sha256
        or not _visual_evidence_is_usable(destination, "formula_crop")
    ):
        destination.unlink(missing_ok=True)
        return None, None
    asset_sha256 = _sha256sum(destination)
    body_sha256 = _formula_evidence_body_sha256(str(formula.get("text") or ""))
    if asset_sha256 is None or body_sha256 is None:
        destination.unlink(missing_ok=True)
        return None, None
    provenance = {
        "method": "authoritative_visual_pdf_rerender",
        "asset_path": destination.relative_to(output_dir).as_posix(),
        "source_pdf_path": str(source_pdf),
        "source_pdf_sha256": route_source_sha256,
        "asset_sha256": asset_sha256,
        "page_no": page_no,
        "bbox_rel": {key: float(bbox_rel[key]) for key in ("l", "r", "t", "b")},
        "expected_formula_index": expected_formula_index,
        "formula_part_index": formula.get("part_index"),
        "formula_content_identity_sha256": body_sha256,
        "render_scale": 2.0,
        "page_image_size": {
            "width": page_image.width,
            "height": page_image.height,
        },
        "pixel_box": [x0, y0, x1, y1],
        "pixel_width": x1 - x0,
        "pixel_height": y1 - y0,
    }
    return destination.relative_to(output_dir).as_posix(), provenance


def _formula_asset_links(
    output_dir: Path,
    source_dir: Path,
    formula_no: int | None,
    page_no: int | None,
    *,
    route_source_sha256: str | None = None,
    formula: dict[str, Any] | None = None,
    expected_formula_index: int | None = None,
) -> dict[str, Any]:
    """Package diagnostics plus an authoritative PDF-rerendered formula crop."""
    links: dict[str, Any] = {
        "formula_crop": None,
        "route_formula_crop": None,
        "formula_context": None,
        "full_page": None,
        "source_review": None,
        "provenance": {},
    }
    namespace_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_dir.name).strip("-")
    namespace_hash = hashlib.sha256(str(source_dir.resolve()).encode("utf-8")).hexdigest()[:10]
    namespace = f"{namespace_label or 'source'}-{namespace_hash}"

    def package(source_path: Path, evidence_kind: str) -> str | None:
        source_error = _route_local_path_error(
            source_dir,
            source_path,
            contract_entry="formula_review_evidence",
        )
        if source_error is not None:
            return None
        if not _visual_evidence_is_usable(source_path, evidence_kind):
            return None
        try:
            source_relative = source_path.resolve(strict=True).relative_to(
                source_dir.resolve(strict=True)
            )
        except (OSError, RuntimeError, ValueError):
            return None
        destination = output_dir / "evidence" / namespace / source_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not destination.exists() or _sha256sum(destination) != _sha256sum(source_path):
                shutil.copy2(source_path, destination)
            if not _visual_evidence_is_usable(destination, evidence_kind):
                destination.unlink(missing_ok=True)
                return None
        except OSError:
            return None
        return destination.relative_to(output_dir).as_posix()

    authoritative_index = (
        expected_formula_index
        if isinstance(expected_formula_index, int)
        else formula_no
    )
    authoritative_link, authoritative_provenance = _authoritative_formula_crop(
        output_dir,
        namespace,
        source_dir,
        route_source_sha256,
        formula,
        authoritative_index,
    )
    links["formula_crop"] = authoritative_link
    if authoritative_provenance is not None:
        links["provenance"]["formula_crop"] = authoritative_provenance

    if formula_no is not None:
        crop = source_dir / "formulas" / f"formula_{formula_no}.png"
        context = source_dir / "formulas" / f"formula_{formula_no}_context.png"
        links["route_formula_crop"] = package(crop, "formula_crop")
        links["formula_context"] = package(context, "formula_context")
    if page_no is not None:
        page = source_dir / "pages" / f"page_{page_no}.png"
        links["full_page"] = package(page, "full_page")
    # A copied review_index.html would retain its own source-directory-relative
    # links and become misleading in a standalone sidecar.  Direct packaged
    # crops/pages above are the portable evidence contract.
    return links


def _bind_formula_evidence_audit(
    evidence: dict[str, Any],
    *,
    route_source_sha256: str | None,
    formula_page_no: int | None,
    formula_bbox: dict[str, float] | None,
    expected_formula_index: int | None,
    formula_part_index: int | None = None,
    formula: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind packaged evidence to the source and formula anchor it proves."""

    evidence["audit_binding"] = {
        "route_source_sha256": route_source_sha256,
        "formula_page_no": formula_page_no,
        "formula_bbox": formula_bbox,
        "expected_formula_index": expected_formula_index,
        "formula_part_index": formula_part_index,
        "formula_bbox_rel": (
            {
                key: float(formula["bbox_rel"][key])
                for key in ("l", "r", "t", "b")
            }
            if isinstance(formula, dict)
            and isinstance(formula.get("bbox_rel"), dict)
            and _bbox_values_are_finite(formula["bbox_rel"]) is not None
            else None
        ),
        "formula_content_identity_sha256": (
            _formula_evidence_body_sha256(str(formula.get("text") or ""))
            if isinstance(formula, dict)
            else None
        ),
    }
    return evidence


def _relative_link(from_dir: Path, target: Path) -> str:
    """Return a POSIX relative path suitable for an HTML href/src."""
    try:
        return target.resolve().relative_to(from_dir.resolve()).as_posix()
    except ValueError:
        return Path(os.path.relpath(target.resolve(), from_dir.resolve())).as_posix()


def _html_text(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _truncate_review_text(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated for review]..."


def is_suspicious(f: dict[str, Any]) -> list[str]:
    """Return list of suspicion reasons, empty if formula looks OK."""
    reasons: list[str] = []
    text = f["text"]

    if CJK_RE.search(text):
        reasons.append("contains_cjk")
    if NUMBER_ONLY_RE.match(text):
        reasons.append("number_only_missing_body")
    if REPEATED_AND_RE.search(text):
        reasons.append("repeated_and_hallucination")
    if REPEATED_SINGLE_RE.search(text):
        reasons.append("repeated_single_chars")

    # Repeated \frac hallucination: \frac { \sqrt { d } } { \sqrt { d } }
    # appearing 3+ times. This catches CN formula (5).
    _frac_pat = chr(92) + "frac { " + chr(92) + "sqrt { d } } { " + chr(92) + "sqrt { d } }"
    if text.count(_frac_pat) >= 3:
        reasons.append("repeated_frac_hallucination")
    # Geometry-based checks removed; CN formula (5) is caught by
    # repeated_frac_hallucination. CN formula (4) by number_only. CN formula (3)/(13)
    # by CJK. CN formula (16) by repeated_and. No geometry fallback needed.

    return reasons


def text_similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def _strip_trailing_display_equation_label(value: str) -> str:
    """Strip only a terminal display-equation label, never body parentheses."""

    body = value.rstrip()
    # ``\label``/``\nonumber`` are display metadata only when terminal.  Peel
    # them first so a preceding ``\tag`` or parenthesized equation number can
    # be recognized without touching identical syntax inside the formula body.
    body = re.sub(
        r"(?:\\(?:label|nonumber)\s*(?:\{[^{}]*\})?\s*)+$",
        "",
        body,
    ).rstrip()
    trailing_tag = re.compile(
        r"(?:\\(?:quad|qquad|,|;|!|:)\s*)?"
        r"\\tag\*?\s*\{[^{}]*\}\s*$"
    )
    trailing_parenthesized_number = re.compile(
        r"(?:\\(?:quad|qquad|,|;|!|:)\s*)?"
        r"\(\s*(?:\d\s*)+\)\s*$"
    )
    for pattern in (trailing_tag, trailing_parenthesized_number):
        match = pattern.search(body)
        if match is not None:
            return body[: match.start()].rstrip()
    return body


def _formula_body_numeric_tokens(value: str) -> tuple[str, ...]:
    body = _strip_trailing_display_equation_label(value)
    return tuple(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", body))


def formula_body_similarity(a: str, b: str) -> float:
    """Compare formula bodies without letting a shared equation tag dominate."""

    if _formula_body_numeric_tokens(a) != _formula_body_numeric_tokens(b):
        return 0.0

    def normalize(value: str) -> str:
        value = _strip_trailing_display_equation_label(value)
        value = re.sub(r"\\(?:quad|qquad|,|;|!|:)", "", value)
        return re.sub(r"\s+", "", value).casefold()

    return text_similarity(normalize(a), normalize(b))


def _neighborhood_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    return text_similarity(
        " ".join(a.get("nearby_text") or []),
        " ".join(b.get("nearby_text") or []),
    )


def route_b_match_rejection_reasons(
    score: float,
    formula_similarity: float,
    exact_eq_match: bool,
    score_margin: float | None,
    minimum_score: float,
    sim_threshold: float = 0.50,
    exact_eq_sim_threshold: float = 0.60,
) -> list[str]:
    """Return deterministic reasons for why a Route A/Route B pair should not be matched."""
    reasons: list[str] = []
    if score < minimum_score:
        reasons.append("score_too_low")
    if exact_eq_match:
        if formula_similarity < exact_eq_sim_threshold:
            reasons.append("formula_similarity_too_low_for_equation_match")
    else:
        if formula_similarity < sim_threshold:
            reasons.append("formula_similarity_too_low")
        if score_margin is not None and score_margin < 8:
            reasons.append("score_margin_too_small")
    return reasons


def _check_route_pdf_identity(route_a_dir: Path, route_b_dir: Path) -> tuple[bool, dict[str, Any]]:
    """Verify both routes point to the same source PDF when using apply-all matching."""
    a_sha, a_detail = _route_input_sha256(route_a_dir)
    b_sha, b_detail = _route_input_sha256(route_b_dir)
    diagnostics = {
        "route_a_source_sha256": a_sha,
        "route_b_source_sha256": b_sha,
        "route_a_source_sha256_detail": a_detail,
        "route_b_source_sha256_detail": b_detail,
    }

    if (
        a_detail.get("status") == "contract_violation"
        or b_detail.get("status") == "contract_violation"
    ):
        return False, {
            "ok": False,
            "error": "route_contract_path_security_violation",
            "route_b_source_identity_check": diagnostics,
        }

    if a_detail.get("status") == "metadata_mismatch" or b_detail.get("status") == "metadata_mismatch":
        return False, {
            "ok": False,
            "error": "route_b_identity_mismatch",
            "route_b_source_identity_check": diagnostics,
        }

    if a_sha is None or b_sha is None:
        return False, {
            "ok": False,
            "error": "route_b_identity_unverified",
            "route_b_source_identity_check": diagnostics,
        }
    if a_sha != b_sha:
        return False, {
            "ok": False,
            "error": "route_b_identity_mismatch",
            "route_b_source_identity_check": diagnostics,
        }
    return True, {"ok": True, "route_b_source_identity_check": diagnostics}


def _metadata_job_id(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("job_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _check_route_job_identity(
    route_a_dir: Path,
    route_b_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Require symmetric matching job IDs once either route declares one."""

    route_a_metadata = load_json(route_a_dir / "metadata.json")
    route_b_metadata = load_json(route_b_dir / "metadata.json")
    route_a_declares_job_id = (
        isinstance(route_a_metadata, dict) and "job_id" in route_a_metadata
    )
    route_b_declares_job_id = (
        isinstance(route_b_metadata, dict) and "job_id" in route_b_metadata
    )
    route_a_job_id = _metadata_job_id(route_a_metadata)
    route_b_job_id = _metadata_job_id(route_b_metadata)
    diagnostics = {
        "route_a_job_id": route_a_job_id,
        "route_b_job_id": route_b_job_id,
    }
    if (route_a_declares_job_id or route_b_declares_job_id) and (
        route_a_job_id is None
        or route_b_job_id is None
        or route_a_job_id != route_b_job_id
    ):
        return False, {
            "ok": False,
            "error": "route_contract_job_id_mismatch",
            "route_job_identity_check": diagnostics,
        }
    return True, {"ok": True, "route_job_identity_check": diagnostics}


def _anchor_match_score(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    a_page = _coerce_page_no(a.get("page_no"))
    b_page = _coerce_page_no(b.get("page_no"))
    if a_page is None or b_page is None:
        return float("-inf"), {"reasons": ["page_anchor_missing"]}
    if a_page != b_page:
        return float("-inf"), {"reasons": ["page_mismatch"]}
    score = 0.0
    reasons: list[str] = ["same_page"]
    aeq, beq = a.get("main_eq"), b.get("main_eq")
    if aeq is not None and beq is not None:
        if aeq == beq:
            score += 100
            reasons.append("equation_number")
        else:
            score -= 80
            reasons.append("equation_number_mismatch")
    abbox, a_geometry_space = _formula_matching_bbox(a)
    bbbox, b_geometry_space = _formula_matching_bbox(b)
    y_distance = x_distance = None
    y_within_match_limit: bool | None = None
    if abbox and bbbox:
        y_distance = abs(formula_vertical_center(abbox) - formula_vertical_center(bbbox))
        x_distance = abs(formula_horizontal_center(abbox) - formula_horizontal_center(bbbox))
        relative_geometry = a_geometry_space == b_geometry_space == "relative"
        y_close, y_near = (
            (RELATIVE_Y_CLOSE, RELATIVE_Y_NEAR)
            if relative_geometry
            else (50.0, 120.0)
        )
        x_close, x_far = (
            (RELATIVE_X_CLOSE, RELATIVE_X_FAR)
            if relative_geometry
            else (180.0, 320.0)
        )
        y_within_match_limit = y_distance <= y_near
        if y_distance <= y_close:
            score += 45
            reasons.append("bbox_y_close")
        elif y_distance <= y_near:
            score += 20
            reasons.append("bbox_y_near")
        else:
            score -= min(
                60,
                y_distance / (RELATIVE_Y_CLOSE if relative_geometry else 5.0),
            )
            reasons.append("bbox_y_far")
        if x_distance <= x_close:
            score += 15
            reasons.append("bbox_column")
        elif x_distance > x_far:
            score -= 30
            reasons.append("bbox_column_mismatch")
    elif abbox is None or bbbox is None:
        reasons.append("bbox_missing")
    order_distance = abs(
        _coerce_page_order(a.get("page_order"))
        - _coerce_page_order(b.get("page_order"))
    )
    score += max(0, 24 - (order_distance * 12))
    reasons.append(f"page_order_distance_{order_distance}")
    neighborhood_similarity = _neighborhood_similarity(a, b)
    score += neighborhood_similarity * 20
    formula_similarity = formula_body_similarity(
        str(a.get("text") or ""),
        str(b.get("text") or ""),
    )
    score += formula_similarity * 12
    return score, {
        "score": round(score, 3),
        "reasons": reasons,
        "y_distance": round(y_distance, 3) if y_distance is not None else None,
        "x_distance": round(x_distance, 3) if x_distance is not None else None,
        "geometry_space": (
            a_geometry_space if a_geometry_space == b_geometry_space else "mixed"
        ),
        "geometry_y_match_limit": (
            round(RELATIVE_Y_NEAR, 6)
            if a_geometry_space == b_geometry_space == "relative"
            else 120.0
        ),
        "geometry_y_within_match_limit": y_within_match_limit,
        "route_a_geometry_verified": _formula_geometry_verified(a),
        "route_b_geometry_verified": _formula_geometry_verified(b),
        "page_order_distance": order_distance,
        "neighborhood_similarity": round(neighborhood_similarity, 4),
        "formula_similarity": round(formula_similarity, 4),
    }


def formula_diagnostics(formula_text: str | None) -> dict[str, Any]:
    """Return lightweight review diagnostics for formula text."""
    text = formula_text or ""
    return {
        "char_count": len(text),
        "eq_numbers": _extract_eq_numbers_from_text(text),
        "frac_count": text.count(chr(92) + "frac"),
        "sqrt_count": text.count(chr(92) + "sqrt"),
        "sum_count": text.count(chr(92) + "sum"),
        "cjk_count": len(CJK_RE.findall(text)),
        "repeated_and_count": text.count(chr(92) + " \\ a n d"),
    }


def review_notes(entry: dict[str, Any]) -> list[str]:
    """Human-facing notes for formulas needing careful inspection."""
    notes: list[str] = []
    candidate_source = entry.get("candidate_source")
    if candidate_source == "guarded_fallback":
        notes.append("Guarded route-a-full fallback applied from reviewed equation allowlist.")
    if entry.get("status") != "replaced":
        notes.append("No Route B replacement was applied; Route A output is preserved.")
    if entry.get("right_column_likely"):
        notes.append("Right-column formula marker: inspect full-page evidence and any review-only fallback candidates.")
    candidate_diag = entry.get("candidate_diagnostics") or {}
    if candidate_diag.get("char_count", 0) > 400 or candidate_diag.get("frac_count", 0) >= 3:
        notes.append("Complex candidate: judge against the crop/page evidence before treating it as correct.")
    if entry.get("eq_number") is None:
        notes.append("No clean equation number was extracted from Route A text; markdown matching used content prefix/proximity.")
    if entry.get("review_candidate_attempts"):
        notes.append("Review-only candidate attempts are not written into document.json or document.md.")
    return notes


def needs_review_candidate_attempts(entry: dict[str, Any]) -> bool:
    """Limit fallback attempts to unresolved formulas and hard replacements."""
    if entry.get("status") != "replaced":
        return True
    candidate_diag = entry.get("candidate_diagnostics") or {}
    return bool(
        candidate_diag.get("char_count", 0) > 400
        or candidate_diag.get("frac_count", 0) >= 3
    )


def match_route_b_to_route_a(
    route_a_formulas: list[dict[str, Any]],
    route_b_formulas: list[dict[str, Any]],
    sim_threshold: float = 0.50,
    *,
    require_geometry: bool = True,
    exact_eq_sim_threshold: float | None = None,
) -> dict[int, dict[str, Any]]:
    """Match candidates monotonically using page, geometry, order, and text evidence.

    ``require_geometry`` defaults to the fail-closed policy.  The optional
    compatibility escape hatch is used only by the legacy low-level patch
    helper when a caller did not provide route page sizes; the production
    ``run_formula_second_pass(..., apply_all=True)`` path always enables it.
    """
    b_by_page: dict[int | None, list[dict[str, Any]]] = {}
    if exact_eq_sim_threshold is None:
        # The strict production path uses the conservative threshold.  The
        # legacy direct patch helper can still match old payloads that lack
        # any page geometry; those callers are not apply-all delivery output.
        exact_eq_sim_threshold = 0.60 if require_geometry else 0.35
    for bf in route_b_formulas:
        b_by_page.setdefault(_coerce_page_no(bf.get("page_no")), []).append(bf)
    for candidates in b_by_page.values():
        candidates.sort(key=lambda formula: _coerce_page_order(formula.get("page_order")))

    matches: dict[int, dict[str, Any]] = {}
    used_b_indices: set[int] = set()
    last_source_order_by_page: dict[int | None, int] = {}
    for i, af in enumerate(route_a_formulas):
        apage = _coerce_page_no(af.get("page_no"))
        minimum_source_order = last_source_order_by_page.get(apage, -1)
        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for candidate in b_by_page.get(apage, []):
            source_order = _coerce_page_order(candidate.get("page_order"))
            if id(candidate) in used_b_indices or source_order <= minimum_source_order:
                continue
            if (
                af.get("main_eq") is not None
                and candidate.get("main_eq") is not None
                and af.get("main_eq") != candidate.get("main_eq")
            ):
                continue
            score, evidence = _anchor_match_score(af, candidate)
            scored.append((score, candidate, evidence))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            continue
        best_score, best, evidence = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else None
        evidence["score_margin"] = (
            round(best_score - second_score, 3) if second_score is not None else None
        )
        exact_eq = af.get("main_eq") is not None and af.get("main_eq") == best.get("main_eq")
        exact_eq_candidates = [
            item
            for item in scored
            if af.get("main_eq") is not None
            and item[1].get("main_eq") == af.get("main_eq")
        ]
        evidence["exact_equation_candidate_count"] = len(exact_eq_candidates)
        if exact_eq and len(exact_eq_candidates) > 1:
            exact_eq_margin = exact_eq_candidates[0][0] - exact_eq_candidates[1][0]
            evidence["exact_equation_score_margin"] = round(exact_eq_margin, 3)
            # Reading-order adjacency contributes up to 12 points by itself;
            # that alone is not enough to disambiguate duplicate equation
            # labels.  Require an additional independent geometry/text margin.
            if exact_eq_margin < EXACT_EQUATION_DUPLICATE_MIN_SCORE_MARGIN:
                evidence["match_rejection_reasons"] = [
                    "exact_equation_candidate_ambiguous"
                ]
                continue
        if exact_eq and _formula_body_numeric_tokens(
            str(af.get("text") or "")
        ) != _formula_body_numeric_tokens(str(best.get("text") or "")):
            evidence["match_rejection_reasons"] = [
                "formula_body_numeric_tokens_mismatch"
            ]
            continue
        if require_geometry and (
            not _formula_geometry_verified(af)
            or not _formula_geometry_verified(best)
        ):
            evidence["match_rejection_reasons"] = ["geometry_unverified"]
            continue
        if evidence.get("geometry_y_within_match_limit") is False:
            evidence["match_rejection_reasons"] = ["bbox_y_distance_too_large"]
            continue
        if not require_geometry and not exact_eq and (
            af.get("bbox_norm") is None or best.get("bbox_norm") is None
        ):
            evidence["match_rejection_reasons"] = ["geometry_anchor_missing"]
            continue
        minimum_score = max(35.0, sim_threshold * 60)
        rejection_reasons = route_b_match_rejection_reasons(
            best_score,
            float(evidence.get("formula_similarity") or 0.0),
            exact_eq,
            evidence["score_margin"],
            minimum_score,
            sim_threshold=sim_threshold,
            exact_eq_sim_threshold=exact_eq_sim_threshold,
        )
        if rejection_reasons:
            evidence["match_rejection_reasons"] = rejection_reasons
            continue
        matched = dict(best)
        matched["anchor_match"] = evidence
        matches[i] = matched
        used_b_indices.add(id(best))
        last_source_order_by_page[apage] = _coerce_page_order(best.get("page_order"))

    return matches


def parse_review_candidate_arg(value: str) -> tuple[str, Path]:
    """Parse LABEL=PATH or PATH into a review-candidate source."""
    if "=" in value:
        label, path = value.split("=", 1)
        label = label.strip() or Path(path).name
        return label, Path(path)
    path = Path(value)
    return path.name, path


def load_review_candidate_sources(values: list[str]) -> list[dict[str, Any]]:
    """Load optional review-only formula candidate sources."""
    sources: list[dict[str, Any]] = []
    for value in values:
        label, source_dir = parse_review_candidate_arg(value)
        doc = load_json(source_dir / "document.json")
        if doc is None:
            sources.append({
                "label": label,
                "source_dir": source_dir,
                "formulas": [],
                "error": f"document.json not found: {source_dir}",
            })
            continue
        doc = _normalize_document_chunk_pages(doc)
        # A Route-B document declares the coordinate space used by its own
        # page images.  Pass that page map as the target so BOTTOMLEFT payloads
        # are normalized to the declared image extent rather than applying
        # the Route-A PDF-point fallback scale a second time.
        formulas = extract_formulas(doc, target_page_sizes=_document_page_sizes(doc))
        for formula_no, formula in enumerate(formulas, start=1):
            formula["formula_no"] = formula_no
        sources.append({
            "label": label,
            "source_dir": source_dir,
            "formulas": formulas,
            "error": None,
        })
    return sources


def find_source_formula_by_eq(
    sources: list[dict[str, Any]],
    page_no: int | None,
    eq_num: int | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find a unique source formula by page/equation identity.

    A guarded fallback is an explicit reviewed exception, so silently taking
    the first duplicate would be unsafe.  Ambiguous same-page equation tags
    (including duplicates across two supplied source directories) return
    ``None`` and are recorded by the caller instead.
    """
    if eq_num is None:
        return None
    candidates = find_source_formulas_by_eq(sources, page_no, eq_num)
    return candidates[0] if len(candidates) == 1 else None


def find_source_formulas_by_eq(
    sources: list[dict[str, Any]],
    page_no: int | None,
    eq_num: int | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return every source formula with the same page/equation identity."""
    if eq_num is None:
        return []
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for source in sources:
        if source.get("error"):
            continue
        for formula in source["formulas"]:
            if formula.get("page_no") == page_no and formula.get("main_eq") == eq_num:
                candidates.append((source, formula))
    return candidates


def find_review_candidate_attempts(
    entry: dict[str, Any],
    sources: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Find review-only candidates for unresolved or hard-to-judge formulas."""
    attempts: list[dict[str, Any]] = []
    eq_num = entry.get("eq_number")
    page_no = entry.get("page_no")
    bbox_summary = entry.get("route_a_bbox") or {}
    right_column = bool(entry.get("right_column_likely"))

    for source in sources:
        if source.get("error"):
            attempts.append({
                "source": source["label"],
                "status": "source_error",
                "message": source["error"],
            })
            continue

        candidates = []
        for formula in source["formulas"]:
            if formula.get("page_no") != page_no:
                continue
            match_reason: str | None = None
            if eq_num is not None and formula.get("main_eq") == eq_num:
                match_reason = "same_page_equation_number"
            elif right_column and formula.get("bbox_norm") and bbox_summary:
                source_bbox = formula.get("bbox_norm")
                y_dist = abs(formula_vertical_center(source_bbox) - float(bbox_summary.get("y_center", 0)))
                x_center = formula_horizontal_center(source_bbox)
                if x_center >= RIGHT_COLUMN_X_PX and y_dist < 80:
                    match_reason = "right_column_vertical_proximity"
            if not match_reason:
                continue
            text = formula.get("text", "")
            source_sha256, _source_sha256_detail = _route_input_sha256(
                Path(source["source_dir"])
            )
            evidence = _formula_asset_links(
                output_dir,
                source["source_dir"],
                formula.get("formula_no"),
                formula.get("page_no"),
                route_source_sha256=source_sha256,
                formula=formula,
                expected_formula_index=formula.get("formula_no"),
            )
            _bind_formula_evidence_audit(
                evidence,
                route_source_sha256=source_sha256,
                formula_page_no=formula.get("page_no"),
                formula_bbox=_formula_bbox_summary(formula.get("bbox_norm")),
                expected_formula_index=formula.get("formula_no"),
                formula_part_index=formula.get("part_index"),
                formula=formula,
            )
            candidates.append({
                "source": source["label"],
                "source_dir": str(source["source_dir"]),
                "formula_no": formula.get("formula_no"),
                "page_no": formula.get("page_no"),
                "eq_number": formula.get("main_eq"),
                "match_reason": match_reason,
                "bbox": _formula_bbox_summary(formula.get("bbox_norm")),
                "text": text,
                "diagnostics": formula_diagnostics(text),
                "evidence": evidence,
            })
        attempts.extend(candidates[:3])
    return attempts


def _patch_node_text(node: dict[str, Any], new_text: str) -> None:
    """Recursively patch formula node text in document tree."""
    if "text" in node:
        node["text"] = new_text
    for child in node.get("children", []) or []:
        _patch_node_text(child, new_text)


def _extract_eq_numbers_from_text(text: str) -> list[int]:
    numbers: list[int] = [int(m.group(1)) for m in EQ_NUM_RE.finditer(text)]
    for match in SPACED_EQ_NUM_RE.finditer(text):
        compact = re.sub(r"\s+", "", match.group(1))
        if compact:
            numbers.append(int(compact))
    deduped: list[int] = []
    for number in numbers:
        if number not in deduped:
            deduped.append(number)
    return deduped


def _infer_markdown_eq_number(entry: dict[str, Any]) -> int | None:
    """Return the equation number to preserve in patched markdown, if known."""
    eq_num = entry.get("eq_number")
    if isinstance(eq_num, int):
        return eq_num

    route_a_text = str(entry.get("route_a_text") or "")
    for match in SPACED_EQ_NUM_RE.finditer(route_a_text):
        compact = re.sub(r"\s+", "", match.group(1))
        if not compact:
            continue
        inferred = int(compact)
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int) or inferred == formula_no:
            return inferred
    return None


def _formula_text_with_eq_number(formula_text: str, eq_num: int | None) -> str:
    """Append an equation number when a replacement candidate omits it."""
    if eq_num is None or EQ_NUM_RE.search(formula_text) or SPACED_EQ_NUM_RE.search(formula_text):
        return formula_text
    return f"{formula_text} \\quad ( {eq_num} )"


def normalize_formula_candidate(formula_text: str) -> str:
    """Apply only minimal whitespace normalizations without changing token identity."""
    normalized = formula_text
    for pattern, replacement in SPACED_OPERATOR_REPLACEMENTS:
        normalized = pattern.sub(lambda _match, value=replacement: value, normalized)
    normalized = normalized.replace("\r", "")
    normalized = re.sub(r"[\t ]+", " ", normalized)
    normalized = normalized.replace("\n", " ")
    return normalized


def formula_hallucination_reasons(formula_text: str | None) -> list[str]:
    """Detect output patterns that are visibly unusable even when TeX is balanced."""
    text = (formula_text or "").strip()
    reasons: list[str] = []
    if not text or NUMBER_ONLY_RE.fullmatch(text):
        reasons.append("missing_formula_body")
    if len(text) > 1800:
        reasons.append("formula_unusually_long")
    if REPEATED_AND_RE.search(text):
        reasons.append("repeated_and_hallucination")
    if REPEATED_SINGLE_RE.search(text):
        reasons.append("repeated_single_chars")

    atoms = re.findall(
        r"\\(?:mathfrak|mathrm|mathbf|mathcal|text)\s*\{\s*[^{}]{1,16}\s*\}",
        text,
    )
    if atoms:
        most_common = max(atoms.count(atom) for atom in set(atoms))
        if most_common >= 8 and most_common / len(atoms) >= 0.45:
            reasons.append("repeated_tex_atom_hallucination")
    return reasons


def canonicalize_formula_output(
    formula_text: str | None,
    equation_number: int | None = None,
) -> tuple[str, list[str]]:
    """Normalize a final formula without inventing mathematical content."""
    text = normalize_formula_candidate(formula_text or "").strip()
    repairs: list[str] = []
    if equation_number is not None:
        tex_space = r"(?:\s|\\[,;:!]|\\\s)*"
        digits = tex_space.join(re.escape(char) for char in str(equation_number))
        number_pattern = re.compile(
            r"\(" + tex_space + digits + tex_space + r"\)"
        )
        trailing_arrays = list(
            re.finditer(
                r"(?s)\\begin\s*\{\s*array\s*\}.*?\\end\s*\{\s*array\s*\}",
                text,
            )
        )
        if trailing_arrays:
            array_match = trailing_arrays[-1]
            suffix = text[array_match.end() :]
            suffix_number = number_pattern.search(suffix)
            array_text = array_match.group(0)
            array_payload = re.sub(r"\\(?:begin|end)\s*\{[^}]+\}", "", array_text)
            array_payload = re.sub(r"[\s{}&\\,;:!]+", "", array_payload)
            if (
                suffix_number
                and not re.sub(
                    r"(?:\s|\\[,;:!]|\\\s)+",
                    "",
                    suffix[: suffix_number.start()],
                )
                and
                len(array_payload) <= 32
                and not re.search(r"[=<>]|\\(?:frac|sum|int|prod|lim)", array_text)
            ):
                prefix = re.sub(
                    r"(?:(?:\\quad)|(?:\\[,;:!])|(?:\\\s)|\s)+$",
                    "",
                    text[: array_match.start()],
                )
                text = prefix + " " + suffix[suffix_number.start() :]
                repairs.append("trimmed_low_information_trailing_array")
        matches = list(number_pattern.finditer(text))
        if matches:
            first = matches[0]
            tail = text[first.end() :]
            if formula_hallucination_reasons(tail):
                text = text[: first.end()].strip()
                repairs.append("trimmed_hallucinated_suffix")
            text, removed = number_pattern.subn("", text)
            if removed > 1:
                repairs.append("removed_duplicate_equation_numbers")
        text = re.sub(r"(?:(?:\\quad)|(?:\\[,;:!])|(?:\\\s))\s*$", "", text).strip()
        text = re.sub(r"(?:\\quad\s*)+$", "", text).strip()
        if text and not NUMBER_ONLY_RE.fullmatch(text):
            text = f"{text} \\quad ( {equation_number} )"
    return re.sub(r"[ \t]+", " ", text).strip(), repairs


def _find_markdown_block(md_text: str, formula_text: str, eq_num: int | None) -> str:
    """Find the most likely $$...$$ markdown block for a formula."""
    if not md_text:
        return ""
    blocks = _markdown_display_formula_spans(md_text)
    if eq_num is not None:
        for _, _, block in blocks:
            if eq_num in _extract_eq_numbers_from_text(block[2:-2]):
                return block

    prefix = formula_text[:30]
    if prefix:
        for _, _, block in blocks:
            if prefix in block[2:-2]:
                return block
    return ""


def _markdown_display_formula_spans(md_text: str) -> list[tuple[int, int, str]]:
    """Return ``$$...$$`` spans outside backtick and tilde code fences."""

    def fence_payload(line: str) -> str:
        # Strip Markdown container prefixes conservatively.  Fences nested in
        # blockquotes/lists are still code and must never expose their math.
        payload = line.lstrip(" \t")
        while payload:
            if payload.startswith(">"):
                payload = payload[1:].lstrip(" \t")
                continue
            list_marker = re.match(r"(?:[-+*]|[0-9]{1,9}[.)])[ \t]+", payload)
            if list_marker is not None:
                payload = payload[list_marker.end() :].lstrip(" \t")
                continue
            break
        return payload

    non_fenced_ranges: list[tuple[int, int]] = []
    outside_start = 0
    offset = 0
    fence_char: str | None = None
    fence_length = 0
    opener_re = re.compile(r"^(`{3,}|~{3,}).*$")

    for line in md_text.splitlines(keepends=True):
        line_start = offset
        line_end = line_start + len(line)
        line_body = fence_payload(line.rstrip("\r\n"))
        if fence_char is None:
            opener = opener_re.match(line_body)
            if opener is not None:
                if outside_start < line_start:
                    non_fenced_ranges.append((outside_start, line_start))
                marker = opener.group(1)
                fence_char = marker[0]
                fence_length = len(marker)
        else:
            closing_re = re.compile(
                "^" + re.escape(fence_char) + "{" + str(fence_length) + r",}[ \t]*$"
            )
            if closing_re.match(line_body):
                fence_char = None
                fence_length = 0
                outside_start = line_end
        offset = line_end

    if fence_char is None and outside_start < len(md_text):
        non_fenced_ranges.append((outside_start, len(md_text)))

    blocks: list[tuple[int, int, str]] = []
    for range_start, range_end in non_fenced_ranges:
        segment = md_text[range_start:range_end]
        for match in re.finditer(r"\$\$.*?\$\$", segment, re.DOTALL):
            start = range_start + match.start()
            end = range_start + match.end()
            blocks.append((start, end, md_text[start:end]))
    return blocks


def _markdown_display_formula_blocks(md_text: str) -> list[str]:
    """Return display-math blocks outside fenced code regions."""

    return [block for _, _, block in _markdown_display_formula_spans(md_text)]


def _markdown_identity_body(value: str) -> str:
    """Canonicalize harmless Markdown/TeX layout noise for identity checks."""
    body = _strip_trailing_display_equation_label(value)
    body = re.sub(r"\\(?:quad|qquad|,|;|!|:)", "", body)
    body = re.sub(r"\\(?:left|right)(?=\s|[().\[\]{}\\])", "", body)
    body = re.sub(r"\\(?:begin|end)\s*\{[^}]+\}", "", body)
    body = body.replace("&", "")
    return re.sub(r"\s+", "", body).casefold()


def _markdown_identity_similarity(a: str, b: str) -> float:
    return text_similarity(_markdown_identity_body(a), _markdown_identity_body(b))


def validate_markdown_formula_identity(
    md_text: str,
    formulas: list[dict[str, Any]],
    *,
    allow_missing_if_empty: bool = False,
) -> dict[str, Any]:
    """Validate a route markdown inventory against formula JSON identity.

    Count-only checks permit a swapped pair of same-count display blocks to
    pass and later make the wrong replacement at an anchor.  This validator
    requires every JSON formula to have exactly one matching display block by
    equation identity (when present) and normalized formula body.  Duplicate
    candidates are intentionally rejected as ambiguous.
    """
    if not md_text:
        if not formulas and allow_missing_if_empty:
            return {
                "ok": True,
                "status": "missing_empty_inventory",
                "formula_count": 0,
                "markdown_formula_count": 0,
            }
        return {
            "ok": False,
            "status": "missing",
            "formula_count": len(formulas),
            "markdown_formula_count": 0,
            "error": "markdown_missing",
        }

    blocks = _markdown_display_formula_blocks(md_text)
    diagnostics: dict[str, Any] = {
        "ok": False,
        "status": "mismatch",
        "formula_count": len(formulas),
        "markdown_formula_count": len(blocks),
        "unmatched_formula_numbers": [],
        "ambiguous_formula_numbers": [],
    }
    if len(blocks) != len(formulas):
        diagnostics["error"] = "inventory_mismatch"
        return diagnostics

    remaining = set(range(len(blocks)))
    for formula_index, formula in enumerate(formulas, start=1):
        formula_no = formula.get("formula_no")
        if not isinstance(formula_no, int):
            formula_no = formula_index
        expected_eq = formula.get("main_eq")
        expected_text = str(formula.get("text") or "")
        candidates: list[int] = []
        for block_index in sorted(remaining):
            block_body = blocks[block_index][2:-2]
            block_eqs = _extract_eq_numbers_from_text(block_body)
            # Some Docling Markdown writers omit equation tags even though
            # the JSON text retains them.  A present but conflicting tag is a
            # hard identity mismatch; an absent tag is adjudicated by body.
            if isinstance(expected_eq, int) and block_eqs and expected_eq not in block_eqs:
                continue
            if _markdown_identity_similarity(expected_text, block_body) < 0.999:
                continue
            candidates.append(block_index)
        if len(candidates) == 1:
            remaining.remove(candidates[0])
        elif len(candidates) > 1:
            diagnostics["ambiguous_formula_numbers"].append(formula_no)
        else:
            diagnostics["unmatched_formula_numbers"].append(formula_no)

    diagnostics["ok"] = not diagnostics["unmatched_formula_numbers"] and not diagnostics[
        "ambiguous_formula_numbers"
    ] and not remaining
    if diagnostics["ok"]:
        diagnostics["status"] = "verified"
    else:
        diagnostics["error"] = "identity_mismatch"
    return diagnostics


def patch_document_md(
    md_text: str,
    route_a_formulas: list[dict[str, Any]],
    replacement_log: list[dict[str, Any]],
) -> str:
    """Patch $$...$$ blocks in Route A document.md with Route B candidates.

    For each replacement in the log, find the corresponding $$...$$ block
    containing the matching equation number and replace its content.
    """
    if not md_text or not replacement_log:
        return md_text

    blocks = _markdown_display_formula_spans(md_text)
    edits: list[tuple[int, int, str]] = []
    used_block_indexes: set[int] = set()
    for entry in replacement_log:
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int) or not (0 < formula_no <= len(route_a_formulas)):
            entry["markdown_anchor_status"] = "anchor_missing"
            continue
        route_a_formula = route_a_formulas[formula_no - 1]
        expected_eq = route_a_formula.get("main_eq")
        candidate_indexes: list[int] = []
        if isinstance(expected_eq, int):
            candidate_indexes = [
                index
                for index, block in enumerate(blocks)
                if expected_eq in _extract_eq_numbers_from_text(block[2])
                and formula_body_similarity(
                    str(route_a_formula.get("text") or ""),
                    block[2][2:-2],
                )
                >= 0.999
                and index not in used_block_indexes
            ]
        if len(candidate_indexes) != 1:
            scored = sorted(
                (
                    formula_body_similarity(
                        str(route_a_formula.get("text") or ""),
                        block[2][2:-2],
                    ),
                    index,
                )
                for index, block in enumerate(blocks)
                if index not in used_block_indexes
            )
            scored.reverse()
            if scored and scored[0][0] >= 0.999:
                runner_up = scored[1][0] if len(scored) > 1 else 0.0
                if scored[0][0] - runner_up >= 0.08:
                    candidate_indexes = [scored[0][1]]
                else:
                    candidate_indexes = []
            else:
                candidate_indexes = []
        if len(candidate_indexes) != 1:
            entry["markdown_anchor_status"] = "anchor_missing"
            continue
        block_index = candidate_indexes[0]
        used_block_indexes.add(block_index)
        block = blocks[block_index]
        if entry.get("status") == "replaced":
            candidate = _formula_text_with_eq_number(
                normalize_formula_candidate(str(entry.get("route_b_candidate") or "")),
                _infer_markdown_eq_number(entry),
            )
            replacement = f"$${candidate}$$"
            entry["markdown_anchor_status"] = "replaced_at_anchor"
        else:
            reason = str(entry.get("fallback_reason") or entry.get("status") or "not_applied")
            replacement = (
                block[2]
                + f"\n<!-- formula-second-pass-fallback anchor={entry.get('anchor_id')} "
                f"reason={reason} -->"
            )
            entry["markdown_anchor_status"] = "fallback_at_anchor"
        edits.append((block[0], block[1], replacement))
    result = md_text
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def candidate_quality_gate(route_a_formula: dict[str, Any], candidate_text: str | None) -> tuple[bool, list[str]]:
    """Return whether a second-pass candidate is safe enough to enter main output."""
    reasons: list[str] = []
    text = (candidate_text or "").strip()
    if not text:
        return False, ["candidate_empty"]
    if NUMBER_ONLY_RE.match(text):
        reasons.append("candidate_number_only")
    if CJK_RE.search(text) and not CJK_RE.search(str(route_a_formula.get("text") or "")):
        reasons.append("candidate_introduces_cjk")
    if REPEATED_AND_RE.search(text):
        reasons.append("candidate_repeated_and_hallucination")
    if REPEATED_SINGLE_RE.search(text):
        reasons.append("candidate_repeated_single_chars")
    if len(text) > max(1200, len(str(route_a_formula.get("text") or "")) * 4):
        reasons.append("candidate_unusually_long")
    return not reasons, reasons


def validate_candidate_latex(candidate_text: str | None) -> tuple[bool, list[str]]:
    text = (candidate_text or "").strip()
    reasons: list[str] = []
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                reasons.append("unmatched_closing_brace")
                break
    if depth > 0:
        reasons.append("unclosed_brace")
    begins = re.findall(r"\\begin\s*\{\s*([^}]+)\s*\}", text)
    ends = re.findall(r"\\end\s*\{\s*([^}]+)\s*\}", text)
    if begins != ends:
        reasons.append("environment_mismatch")
    left_count = len(re.findall(r"\\left(?=\s|[\(\[\{\\])", text))
    # ``\right.`` is a valid invisible delimiter, commonly paired with
    # ``\left\{`` around an array of equations.
    right_count = len(re.findall(r"\\right(?=\s|[\)\]\}\\.])", text))
    if left_count != right_count:
        reasons.append("left_right_mismatch")
    if "&" in text and not re.search(
        r"\\begin\s*\{\s*(?:aligned|align|array|matrix|pmatrix|bmatrix|cases|split|gathered)\s*\}",
        text,
    ):
        reasons.append("bare_alignment_marker")
    return not reasons, reasons


def patch_document_json(
    route_a_doc: dict[str, Any],
    route_b_formulas: list[dict[str, Any]],
    guarded_fallback_sources: list[dict[str, Any]] | None = None,
    guarded_fallback_eqs: set[int] | None = None,
    apply_all: bool = False,
    route_a_target_page_sizes: dict[int, tuple[float, float]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Patch formula text in Route A document.json with Route B candidates.

    Returns (patched_doc, replacement_log).
    """
    route_a_doc = _normalize_document_chunk_pages(route_a_doc)
    route_a_formulas = extract_formulas(
        route_a_doc,
        target_page_sizes=route_a_target_page_sizes,
    )
    for formula_no, formula in enumerate(route_a_formulas, start=1):
        formula["formula_no"] = formula_no
    route_b_page_orders: dict[int | None, int] = {}
    for formula_no, formula in enumerate(route_b_formulas, start=1):
        formula["formula_no"] = formula_no
        page_no = _coerce_page_no(formula.get("page_no"))
        formula["page_no"] = page_no
        if formula.get("page_order") is None:
            formula["page_order"] = route_b_page_orders.get(page_no, 0)
        else:
            formula["page_order"] = _coerce_page_order(formula.get("page_order"))
        route_b_page_orders[page_no] = _coerce_page_order(formula.get("page_order")) + 1
    # The production runner always passes a page-size mapping, including an
    # explicitly empty mapping when Route-B pages are missing/malformed.  That
    # keeps production apply-all strict while preserving compatibility for
    # low-level legacy callers that omit the mapping entirely.
    strict_geometry = bool(apply_all and route_a_target_page_sizes is not None)
    matches = match_route_b_to_route_a(
        route_a_formulas,
        route_b_formulas,
        require_geometry=strict_geometry,
    )
    guarded_fallback_sources = guarded_fallback_sources or []
    guarded_fallback_eqs = guarded_fallback_eqs or set()

    log: list[dict[str, Any]] = []

    for i, af in enumerate(route_a_formulas):
        reasons = is_suspicious(af)
        if apply_all and not reasons:
            reasons = ["apply_all_candidate"]
        if not reasons:
            continue

        fallback_match = None
        fallback_guard_reason: str | None = None
        if af.get("main_eq") in guarded_fallback_eqs:
            fallback_candidates = find_source_formulas_by_eq(
                guarded_fallback_sources,
                af.get("page_no"),
                af.get("main_eq"),
            )
            if len(fallback_candidates) == 1:
                fallback_match = fallback_candidates[0]
                if strict_geometry and (
                    not _formula_geometry_verified(af)
                    or not _formula_geometry_verified(fallback_match[1])
                ):
                    fallback_match = None
                    fallback_guard_reason = "guarded_fallback_geometry_unverified"
            elif len(fallback_candidates) > 1:
                # Never select the first same-page equation from an ambiguous
                # review source.  Route B may still provide an independent,
                # geometry-verified candidate below.
                fallback_guard_reason = "guarded_fallback_ambiguous_same_page_equation"

        if fallback_match is not None:
            source, fallback_formula = fallback_match
            fallback_raw_text = str(fallback_formula.get("text") or "")
            fallback_text = normalize_formula_candidate(fallback_raw_text)
            fallback_audit = {
                "candidate_page_no": fallback_formula.get("page_no"),
                "candidate_bbox": _formula_bbox_summary(
                    fallback_formula.get("bbox_norm")
                ),
                "candidate_part_index": fallback_formula.get("part_index"),
            }
            candidate_ok, candidate_reasons = candidate_quality_gate(af, fallback_text)
            latex_ok, latex_reasons = validate_candidate_latex(fallback_text)
            if not fallback_text.strip():
                fallback_match = None
            elif not candidate_ok:
                log.append({
                    "index": i,
                    "formula_no": af.get("formula_no"),
                    "route_a_text": af["text"],
                    "page_no": af["page_no"],
                    "eq_number": af["main_eq"],
                    "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                    "reasons": reasons,
                    "route_b_candidate": fallback_text,
                    "route_b_formula_no": None,
                    "candidate_source": "guarded_fallback",
                    "candidate_source_label": source["label"],
                    "candidate_source_dir": str(source["source_dir"]),
                    "candidate_formula_no": fallback_formula.get("formula_no"),
                    **fallback_audit,
                    "candidate_diagnostics": formula_diagnostics(fallback_text),
                    "status": "fallback_candidate_failed_quality_gate",
                    "fallback_reason": ",".join(candidate_reasons),
                })
                continue
            elif not latex_ok:
                log.append({
                    "index": i,
                    "formula_no": af.get("formula_no"),
                    "route_a_text": af["text"],
                    "page_no": af["page_no"],
                    "eq_number": af["main_eq"],
                    "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                    "reasons": reasons,
                    "route_b_candidate": fallback_text,
                    "route_b_formula_no": None,
                    "candidate_source": "guarded_fallback",
                    "candidate_source_label": source["label"],
                    "candidate_source_dir": str(source["source_dir"]),
                    "candidate_formula_no": fallback_formula.get("formula_no"),
                    **fallback_audit,
                    "candidate_diagnostics": formula_diagnostics(fallback_text),
                    "status": "render_failed_latex",
                    "fallback_reason": ",".join(latex_reasons),
                })
                continue
            else:
                output_text = _formula_text_with_eq_number(fallback_text, af.get("main_eq"))
                _patch_node_text(af["node"], output_text)
                log.append({
                    "index": i,
                    "formula_no": af.get("formula_no"),
                    "route_a_text": af["text"],
                    "page_no": af["page_no"],
                    "eq_number": af["main_eq"],
                    "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                    "reasons": reasons,
                    "route_b_candidate": output_text,
                    "route_b_raw_candidate": fallback_raw_text,
                    "route_b_formula_no": None,
                    "candidate_source": "guarded_fallback",
                    "candidate_source_label": source["label"],
                    "candidate_source_dir": str(source["source_dir"]),
                    "candidate_formula_no": fallback_formula.get("formula_no"),
                    **fallback_audit,
                    "candidate_diagnostics": formula_diagnostics(fallback_text),
                    "status": "replaced",
                })
                continue

        if i not in matches:
            no_match_status = "suspicious_no_route_b_match"
            if fallback_guard_reason == "guarded_fallback_ambiguous_same_page_equation":
                no_match_status = "guarded_fallback_ambiguous"
            elif fallback_guard_reason:
                no_match_status = "guarded_fallback_geometry_unverified"
            log.append({
                "index": i,
                "formula_no": af.get("formula_no"),
                "route_a_text": af["text"],
                "page_no": af["page_no"],
                "eq_number": af["main_eq"],
                "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                "reasons": reasons,
                "route_b_candidate": None,
                "route_b_formula_no": None,
                "candidate_source": None,
                "candidate_source_label": None,
                "candidate_formula_no": None,
                "candidate_diagnostics": formula_diagnostics(None),
                "status": no_match_status,
                **({"fallback_reason": fallback_guard_reason} if fallback_guard_reason else {}),
            })
            continue

        bf = matches[i]
        route_b_text = normalize_formula_candidate(bf["text"])
        candidate_ok, candidate_reasons = candidate_quality_gate(af, route_b_text)
        latex_ok, latex_reasons = validate_candidate_latex(route_b_text)
        if not route_b_text.strip():
            log.append({
                "index": i,
                "formula_no": af.get("formula_no"),
                "route_a_text": af["text"],
                "page_no": af["page_no"],
                "eq_number": af["main_eq"],
                "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                "reasons": reasons,
                "route_b_candidate": None,
                "route_b_formula_no": bf.get("formula_no"),
                "candidate_source": "route_b",
                "candidate_source_label": "route_b",
                "candidate_formula_no": bf.get("formula_no"),
                "candidate_diagnostics": formula_diagnostics(None),
                "status": "route_b_also_empty",
                "fallback_reason": "candidate_empty",
            })
            continue
        if not candidate_ok:
            log.append({
                "index": i,
                "formula_no": af.get("formula_no"),
                "route_a_text": af["text"],
                "page_no": af["page_no"],
                "eq_number": af["main_eq"],
                "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                "reasons": reasons,
                "route_b_candidate": route_b_text,
                "route_b_formula_no": bf.get("formula_no"),
                "candidate_source": "route_b",
                "candidate_source_label": "route_b",
                "candidate_formula_no": bf.get("formula_no"),
                "candidate_diagnostics": formula_diagnostics(route_b_text),
                "status": "route_b_candidate_failed_quality_gate",
                "fallback_reason": ",".join(candidate_reasons),
            })
            continue
        if not latex_ok:
            log.append({
                "index": i,
                "formula_no": af.get("formula_no"),
                "route_a_text": af["text"],
                "page_no": af["page_no"],
                "eq_number": af["main_eq"],
                "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
                "reasons": reasons,
                "route_b_candidate": route_b_text,
                "route_b_formula_no": bf.get("formula_no"),
                "candidate_source": "route_b",
                "candidate_source_label": "route_b",
                "candidate_formula_no": bf.get("formula_no"),
                "candidate_diagnostics": formula_diagnostics(route_b_text),
                "status": "render_failed_latex",
                "fallback_reason": ",".join(latex_reasons),
            })
            continue

        output_text = _formula_text_with_eq_number(route_b_text, af.get("main_eq"))
        _patch_node_text(af["node"], output_text)
        log.append({
            "index": i,
            "formula_no": af.get("formula_no"),
            "route_a_text": af["text"],
            "page_no": af["page_no"],
            "eq_number": af["main_eq"],
            "route_a_bbox": _formula_bbox_summary(af.get("bbox_norm")),
            "reasons": reasons,
            "route_b_candidate": output_text,
            "route_b_raw_candidate": bf["text"],
            "route_b_formula_no": bf.get("formula_no"),
            "candidate_source": "route_b",
            "candidate_source_label": "route_b",
            "candidate_formula_no": bf.get("formula_no"),
            "candidate_diagnostics": formula_diagnostics(route_b_text),
            "status": "replaced",
        })

    for entry in log:
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int) or not (0 < formula_no <= len(route_a_formulas)):
            continue
        anchor = route_a_formulas[formula_no - 1]
        entry.update(
            {
                "anchor_id": anchor.get("anchor_id"),
                "anchor_part_index": anchor.get("part_index"),
                "anchor_reading_order": anchor.get("reading_order"),
                "anchor_page_order": anchor.get("page_order"),
                "anchor_nearby_text": anchor.get("nearby_text"),
                "anchor_nearby_before": anchor.get("nearby_before"),
                "anchor_nearby_after": anchor.get("nearby_after"),
            }
        )
        matched = matches.get(formula_no - 1)
        entry["anchor_match"] = (matched or {}).get("anchor_match")
        entry["route_b_part_index"] = (matched or {}).get("part_index")
        entry["route_b_page_no"] = (
            (matched or {}).get("page_no")
            if matched is not None
            else entry.get("page_no")
        )
        entry["route_b_bbox"] = _formula_bbox_summary(
            (matched or {}).get("bbox_norm")
        )
        if entry.get("status") != "replaced" and not entry.get("fallback_reason"):
            entry["fallback_reason"] = f"second_pass_not_applied:{entry.get('status')}"
        node = anchor["node"]
        node["local_ai_lab_formula_second_pass"] = {
            "anchor_id": anchor.get("anchor_id"),
            "anchor_part_index": anchor.get("part_index"),
            "status": entry.get("status"),
            "fallback_reason": entry.get("fallback_reason"),
            "candidate_source": entry.get("candidate_source_label"),
            "candidate_formula_no": entry.get("candidate_formula_no"),
            "anchor_match": entry.get("anchor_match"),
        }

    return route_a_doc, log


def add_review_evidence(
    replacement_log: list[dict[str, Any]],
    route_a_dir: Path,
    route_b_dir: Path,
    review_candidate_sources: list[dict[str, Any]],
    output_dir: Path,
    before_md: str,
    after_md: str,
    route_a_source_sha256: str | None = None,
    route_b_source_sha256: str | None = None,
    route_a_formulas: list[dict[str, Any]] | None = None,
    route_b_formulas: list[dict[str, Any]] | None = None,
) -> None:
    """Attach human-review evidence metadata to each replacement log entry."""
    if route_a_source_sha256 is None:
        route_a_source_sha256, _route_a_sha_detail = _route_input_sha256(route_a_dir)
    if route_b_source_sha256 is None:
        route_b_source_sha256, _route_b_sha_detail = _route_input_sha256(route_b_dir)
    route_a_by_number = {
        formula.get("formula_no"): formula
        for formula in route_a_formulas or []
        if isinstance(formula.get("formula_no"), int)
    }
    route_b_by_number = {
        formula.get("formula_no"): formula
        for formula in route_b_formulas or []
        if isinstance(formula.get("formula_no"), int)
    }

    def review_source_formula(entry: dict[str, Any]) -> dict[str, Any] | None:
        source_dir_value = entry.get("candidate_source_dir")
        formula_no_value = entry.get("candidate_formula_no")
        if not isinstance(source_dir_value, str) or not isinstance(formula_no_value, int):
            return None
        candidate_dir = Path(source_dir_value)
        for source in review_candidate_sources:
            try:
                same_source = Path(source["source_dir"]).resolve() == candidate_dir.resolve()
            except (KeyError, OSError, RuntimeError):
                continue
            if not same_source:
                continue
            for formula in source.get("formulas") or []:
                if formula.get("formula_no") == formula_no_value:
                    return formula
        return None

    for entry in replacement_log:
        route_a_text = entry.get("route_a_text", "")
        route_b_text = entry.get("route_b_candidate") or ""
        eq_num = entry.get("eq_number")
        page_no = entry.get("page_no")
        route_a_formula = route_a_by_number.get(entry.get("formula_no"))
        entry["route_a_evidence"] = _bind_formula_evidence_audit(
            _formula_asset_links(
                output_dir,
                route_a_dir,
                entry.get("formula_no"),
                page_no,
                route_source_sha256=route_a_source_sha256,
                formula=route_a_formula,
                expected_formula_index=entry.get("formula_no"),
            ),
            route_source_sha256=route_a_source_sha256,
            formula_page_no=page_no,
            formula_bbox=entry.get("route_a_bbox"),
            expected_formula_index=entry.get("formula_no"),
            formula_part_index=entry.get("anchor_part_index"),
            formula=route_a_formula,
        )
        route_b_page_no = entry.get("route_b_page_no")
        route_b_expected_formula_index = (
            entry.get("route_b_formula_no")
            if isinstance(entry.get("route_b_formula_no"), int)
            else entry.get("formula_no")
        )
        route_b_formula = route_b_by_number.get(entry.get("route_b_formula_no"))
        if route_b_formula is None:
            # A guarded fallback can intentionally have no accepted Route-B
            # formula node.  The Route-A anchor is already normalized into the
            # Route-B page coordinate space and identifies the visual region
            # that Route-B was expected to resolve.
            route_b_formula = route_a_formula
        entry["route_b_evidence"] = _bind_formula_evidence_audit(
            _formula_asset_links(
                output_dir,
                route_b_dir,
                entry.get("route_b_formula_no"),
                route_b_page_no,
                route_source_sha256=route_b_source_sha256,
                formula=route_b_formula,
                expected_formula_index=route_b_expected_formula_index,
            ),
            route_source_sha256=route_b_source_sha256,
            formula_page_no=route_b_page_no,
            formula_bbox=entry.get("route_b_bbox") or entry.get("route_a_bbox"),
            expected_formula_index=route_b_expected_formula_index,
            formula_part_index=(
                entry.get("route_b_part_index")
                if isinstance(entry.get("route_b_part_index"), int)
                else entry.get("anchor_part_index")
            ),
            formula=route_b_formula,
        )
        if entry.get("candidate_source") == "guarded_fallback" and entry.get("candidate_source_dir"):
            candidate_source_dir = Path(str(entry["candidate_source_dir"]))
            candidate_source_sha256, _candidate_sha_detail = _route_input_sha256(
                candidate_source_dir
            )
            candidate_formula = review_source_formula(entry)
            entry["replacement_evidence"] = _bind_formula_evidence_audit(
                _formula_asset_links(
                    output_dir,
                    candidate_source_dir,
                    entry.get("candidate_formula_no"),
                    entry.get("candidate_page_no"),
                    route_source_sha256=candidate_source_sha256,
                    formula=candidate_formula,
                    expected_formula_index=entry.get("candidate_formula_no"),
                ),
                route_source_sha256=candidate_source_sha256,
                formula_page_no=entry.get("candidate_page_no"),
                formula_bbox=entry.get("candidate_bbox"),
                expected_formula_index=entry.get("candidate_formula_no"),
                formula_part_index=entry.get("candidate_part_index"),
                formula=candidate_formula,
            )
        else:
            entry["replacement_evidence"] = entry["route_b_evidence"]
        bbox = entry.get("route_a_bbox") or {}
        entry["right_column_likely"] = bool(bbox.get("x_center", 0) >= RIGHT_COLUMN_X_PX)
        entry["route_a_diagnostics"] = formula_diagnostics(route_a_text)
        entry["markdown_before"] = _find_markdown_block(before_md, route_a_text, eq_num)
        after_probe = route_b_text if entry.get("status") == "replaced" else route_a_text
        entry["markdown_after"] = _find_markdown_block(after_md, after_probe, eq_num)
        if needs_review_candidate_attempts(entry):
            entry["review_candidate_attempts"] = find_review_candidate_attempts(
                entry,
                review_candidate_sources,
                output_dir,
            )
        else:
            entry["review_candidate_attempts"] = []
        route_a_evidence = entry.get("route_a_evidence") or {}
        entry["crop_only_without_formula"] = bool(
            entry.get("status") != "replaced"
            and (
                route_a_evidence.get("formula_crop")
                or route_a_evidence.get("formula_context")
            )
        )
        entry["review_notes"] = review_notes(entry)


def _packaged_evidence_is_available(
    evidence: dict[str, Any] | None,
    output_dir: Path,
) -> bool:
    """Return whether at least one packaged visual evidence file is usable."""

    if not isinstance(evidence, dict):
        return False
    # Route-produced PNGs remain useful diagnostics, but only a crop freshly
    # rerendered from the checksum-verified visual PDF proves the formula
    # page/bbox used for this replacement.
    relative = evidence.get("formula_crop")
    audit = evidence.get("audit_binding")
    provenance_root = evidence.get("provenance")
    provenance = (
        provenance_root.get("formula_crop")
        if isinstance(provenance_root, dict)
        else None
    )
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(audit, dict)
        or not isinstance(provenance, dict)
        or provenance.get("method") != "authoritative_visual_pdf_rerender"
        or provenance.get("asset_path") != relative
    ):
        return False
    path = output_dir / relative
    if _route_local_path_error(
        output_dir,
        path,
        contract_entry="packaged_formula_crop",
    ) is not None or not _visual_evidence_is_usable(path, "formula_crop"):
        return False
    asset_sha256 = str(provenance.get("asset_sha256") or "").lower()
    source_sha256 = str(provenance.get("source_pdf_sha256") or "").lower()
    body_sha256 = str(
        provenance.get("formula_content_identity_sha256") or ""
    ).lower()
    if not all(
        re.fullmatch(r"[0-9a-f]{64}", value)
        for value in (asset_sha256, source_sha256, body_sha256)
    ):
        return False
    if _sha256sum(path) != asset_sha256:
        return False
    if str(audit.get("route_source_sha256") or "").lower() != source_sha256:
        return False
    if audit.get("formula_page_no") != provenance.get("page_no"):
        return False
    if audit.get("expected_formula_index") != provenance.get(
        "expected_formula_index"
    ):
        return False
    if audit.get("formula_part_index") != provenance.get("formula_part_index"):
        return False
    if str(audit.get("formula_content_identity_sha256") or "").lower() != body_sha256:
        return False
    audit_bbox_rel = audit.get("formula_bbox_rel")
    provenance_bbox_rel = provenance.get("bbox_rel")
    audit_values = (
        _bbox_values_are_finite(audit_bbox_rel)
        if isinstance(audit_bbox_rel, dict)
        else None
    )
    provenance_values = (
        _bbox_values_are_finite(provenance_bbox_rel)
        if isinstance(provenance_bbox_rel, dict)
        else None
    )
    if (
        audit_values is None
        or provenance_values is None
        or any(abs(a - b) > 1e-9 for a, b in zip(audit_values, provenance_values))
    ):
        return False
    bbox_summary = audit.get("formula_bbox")
    try:
        if (
            not isinstance(bbox_summary, dict)
            or float(bbox_summary.get("width") or 0.0) <= 0.0
            or float(bbox_summary.get("height") or 0.0) <= 0.0
        ):
            return False
    except (TypeError, ValueError):
        return False
    source_path_value = provenance.get("source_pdf_path")
    if not isinstance(source_path_value, str) or not source_path_value:
        return False
    source_path = Path(source_path_value)
    if (
        source_path.is_symlink()
        or not source_path.is_file()
        or _sha256sum(source_path) != source_sha256
        or not _has_pdf_header(source_path)
    ):
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            if (
                image.width != int(provenance.get("pixel_width") or 0)
                or image.height != int(provenance.get("pixel_height") or 0)
            ):
                return False
    except Exception:
        return False
    return True


def _replacement_evidence_gaps(
    replacement_log: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for entry in replacement_log:
        if entry.get("status") != "replaced":
            continue
        missing_sides: list[str] = []
        if not _packaged_evidence_is_available(
            entry.get("route_a_evidence"), output_dir
        ):
            missing_sides.append("route_a")
        if not _packaged_evidence_is_available(
            entry.get("route_b_evidence"), output_dir
        ):
            missing_sides.append("route_b")
        if entry.get("candidate_source") == "guarded_fallback" and not (
            _packaged_evidence_is_available(
                entry.get("replacement_evidence"), output_dir
            )
        ):
            missing_sides.append("guarded_fallback")
        if missing_sides:
            gaps.append(
                {
                    "formula_no": entry.get("formula_no"),
                    "page_no": entry.get("page_no"),
                    "eq_number": entry.get("eq_number"),
                    "missing_evidence": missing_sides,
                }
            )
    return gaps


def _render_asset_link(label: str, href: str | None) -> str:
    if not href:
        return f"<span class=\"missing\">{html.escape(label)} missing</span>"
    return f"<a href=\"{html.escape(href)}\">{html.escape(label)}</a>"


def _math_body(text: str | None) -> str:
    """Return a MathJax-friendly body while preserving raw text elsewhere."""
    body = (text or "").strip()
    if body.startswith("$$") and body.endswith("$$"):
        body = body[2:-2].strip()
    return body


def _render_math(label: str, text: str | None) -> str:
    body = _math_body(text)
    if not body or body == "NO ROUTE B MATCH" or body == "No markdown block found":
        return f"<div class=\"math-render missing\">{html.escape(label)} unavailable</div>"
    return (
        f"<div class=\"math-render\" aria-label=\"{html.escape(label)}\">"
        f"\\[{html.escape(body)}\\]</div>"
    )


def _render_image(label: str, href: str | None) -> str:
    if not href:
        return f"<div class=\"asset missing\">{html.escape(label)} missing</div>"
    esc = html.escape(href)
    return (
        f"<figure class=\"asset\"><a href=\"{esc}\"><img src=\"{esc}\" "
        f"alt=\"{html.escape(label)}\"></a><figcaption>{html.escape(label)}</figcaption></figure>"
    )


def _render_diagnostics(diag: dict[str, Any] | None) -> str:
    if not diag:
        return "<span class=\"missing\">none</span>"
    items = [
        ("chars", diag.get("char_count")),
        ("eq", ", ".join(str(x) for x in diag.get("eq_numbers") or []) or "none"),
        ("frac", diag.get("frac_count")),
        ("sqrt", diag.get("sqrt_count")),
        ("sum", diag.get("sum_count")),
        ("cjk", diag.get("cjk_count")),
    ]
    return "".join(
        f"<span class=\"metric\"><strong>{html.escape(label)}</strong> {_html_text(value)}</span>"
        for label, value in items
    )


def _render_notes(notes: list[str]) -> str:
    if not notes:
        return "<p class=\"quiet\">No extra review notes.</p>"
    return "<ul class=\"notes\">" + "".join(f"<li>{_html_text(note)}</li>" for note in notes) + "</ul>"


def _render_candidate_attempts(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return "<p class=\"quiet\">No review-only fallback candidates found.</p>"
    rendered = []
    for attempt in attempts:
        if attempt.get("status") == "source_error":
            rendered.append(
                f"<div class=\"attempt\"><h4>{_html_text(attempt.get('source'))}</h4>"
                f"<p class=\"missing\">{_html_text(attempt.get('message'))}</p></div>"
            )
            continue
        ev = attempt.get("evidence") or {}
        rendered.append(f"""
<div class="attempt">
  <h4>{_html_text(attempt.get('source'))} formula {_html_text(attempt.get('formula_no'))}</h4>
  <dl class="meta compact">
    <div><dt>Match</dt><dd>{_html_text(attempt.get('match_reason'))}</dd></div>
    <div><dt>Equation</dt><dd>{_html_text(attempt.get('eq_number'))}</dd></div>
    <div><dt>BBox</dt><dd>{_html_text(attempt.get('bbox'))}</dd></div>
  </dl>
  <div class="diagnostics">{_render_diagnostics(attempt.get('diagnostics'))}</div>
  {_render_math('candidate attempt rendered math', attempt.get('text'))}
  <pre>{_html_text(_truncate_review_text(attempt.get('text') or ''))}</pre>
  <div class="links">
    {_render_asset_link('Candidate review index', ev.get('source_review'))}
    {_render_asset_link('Candidate full page', ev.get('full_page'))}
    {_render_asset_link('Candidate crop', ev.get('formula_crop'))}
    {_render_asset_link('Candidate context crop', ev.get('formula_context'))}
  </div>
  <div class="assets">
    {_render_image('Candidate formula crop', ev.get('formula_crop'))}
    {_render_image('Candidate context crop', ev.get('formula_context'))}
  </div>
</div>
""")
    return "".join(rendered)


def write_review_html(output_dir: Path, summary: dict[str, Any]) -> Path:
    """Write a human-reviewable HTML page for formula replacements."""
    rows = []
    for entry in summary.get("replacement_log", []):
        title = f"Formula {entry.get('formula_no')}"
        eq = entry.get("eq_number")
        if eq is not None:
            title += f" / equation ({eq})"
        route_a_ev = entry.get("route_a_evidence") or {}
        route_b_ev = entry.get("route_b_evidence") or {}
        replacement_ev = entry.get("replacement_evidence") or {}
        reasons = ", ".join(entry.get("reasons") or [])
        right_col = "yes" if entry.get("right_column_likely") else "no"
        candidate_label = entry.get("candidate_source_label") or entry.get("candidate_source") or "none"
        rows.append(f"""
<section class="formula-card" id="formula-{_html_text(entry.get('formula_no'))}">
  <header>
    <h2>{_html_text(title)}</h2>
    <div class="status {html.escape(str(entry.get('status', '')))}">{_html_text(entry.get('status'))}</div>
  </header>
  <dl class="meta">
    <div><dt>Page</dt><dd>{_html_text(entry.get('page_no'))}</dd></div>
    <div><dt>Reasons</dt><dd>{_html_text(reasons)}</dd></div>
    <div><dt>Route B formula</dt><dd>{_html_text(entry.get('route_b_formula_no'))}</dd></div>
    <div><dt>Replacement source</dt><dd>{_html_text(candidate_label)}</dd></div>
    <div><dt>Right column</dt><dd>{_html_text(right_col)}</dd></div>
    <div><dt>Route A bbox</dt><dd>{_html_text(entry.get('route_a_bbox'))}</dd></div>
  </dl>
  <h3>Review Notes</h3>
  {_render_notes(entry.get('review_notes') or [])}
  <div class="compare">
    <div>
      <h3>Route A Formula Text</h3>
      <div class="diagnostics">{_render_diagnostics(entry.get('route_a_diagnostics'))}</div>
      {_render_math('Route A rendered math', entry.get('route_a_text'))}
      <pre>{_html_text(_truncate_review_text(entry.get('route_a_text') or ''))}</pre>
    </div>
    <div>
      <h3>Replacement Candidate</h3>
      <div class="diagnostics">{_render_diagnostics(entry.get('candidate_diagnostics'))}</div>
      {_render_math('Replacement candidate rendered math', entry.get('route_b_candidate') or 'NO ROUTE B MATCH')}
      <pre>{_html_text(_truncate_review_text(entry.get('route_b_candidate') or 'NO ROUTE B MATCH'))}</pre>
    </div>
  </div>
  <div class="compare">
    <div>
      <h3>Before Markdown Snippet</h3>
      {_render_math('Before markdown rendered math', entry.get('markdown_before'))}
      <pre>{_html_text(entry.get('markdown_before') or 'No markdown block found')}</pre>
    </div>
    <div>
      <h3>After Markdown Snippet</h3>
      {_render_math('After markdown rendered math', entry.get('markdown_after'))}
      <pre>{_html_text(entry.get('markdown_after') or 'No markdown block found')}</pre>
    </div>
  </div>
  <h3>Evidence</h3>
  <div class="links">
    {_render_asset_link('Route A review index', route_a_ev.get('source_review'))}
    {_render_asset_link('Route A full page', route_a_ev.get('full_page'))}
    {_render_asset_link('Route A crop', route_a_ev.get('formula_crop'))}
    {_render_asset_link('Route A context crop', route_a_ev.get('formula_context'))}
    {_render_asset_link('Route B review index', route_b_ev.get('source_review'))}
    {_render_asset_link('Route B full page', route_b_ev.get('full_page'))}
    {_render_asset_link('Replacement source review index', replacement_ev.get('source_review'))}
    {_render_asset_link('Replacement source full page', replacement_ev.get('full_page'))}
  </div>
  <div class="assets">
    {_render_image('Route A formula crop', route_a_ev.get('formula_crop'))}
    {_render_image('Route A context crop', route_a_ev.get('formula_context'))}
    {_render_image('Route A full page', route_a_ev.get('full_page'))}
    {_render_image('Route B full page', route_b_ev.get('full_page'))}
  </div>
  <h3>Review-Only Candidate Attempts</h3>
  {_render_candidate_attempts(entry.get('review_candidate_attempts') or [])}
</section>
""")

    if not rows:
        rows.append("""
<section class="formula-card">
  <header><h2>No Suspicious Formulas</h2><div class="status clean">clean</div></header>
  <p>This run made no replacements. Route A document JSON and markdown were preserved.</p>
</section>
""")

    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Formula Second-Pass Review</title>
  <script>
    window.MathJax = {{
      tex: {{ inlineMath: [['\\\\(', '\\\\)']], displayMath: [['\\\\[', '\\\\]']] }},
      svg: {{ fontCache: 'global' }}
    }};
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #1f2933; background: #f7f8fa; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin: 0; font-size: 20px; }}
    h3 {{ margin: 18px 0 8px; font-size: 14px; text-transform: uppercase; color: #52606d; }}
    .summary, .formula-card {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 8px; padding: 18px; margin-bottom: 18px; }}
    .summary-grid, .meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
    .meta.compact {{ grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }}
    dt {{ color: #52606d; font-size: 12px; text-transform: uppercase; }}
    dd {{ margin: 4px 0 0; font-weight: 600; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    .status {{ border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 700; background: #e4e7eb; }}
    .status.replaced {{ background: #d8f3dc; color: #1b4332; }}
    .status.suspicious_no_route_b_match, .status.route_b_also_empty {{ background: #ffe8cc; color: #7c2d12; }}
    .status.clean {{ background: #e0f2fe; color: #0c4a6e; }}
    .compare {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f0f4f8; border: 1px solid #d9e2ec; border-radius: 6px; padding: 12px; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .math-render {{ overflow-x: auto; background: #ffffff; border: 1px solid #d9e2ec; border-radius: 6px; padding: 10px 12px; margin: 8px 0; min-height: 24px; }}
    .math-render.missing {{ color: #7b8794; background: #f0f4f8; font-size: 12px; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }}
    .links a, .missing {{ border: 1px solid #bcccdc; border-radius: 999px; padding: 5px 10px; font-size: 12px; text-decoration: none; color: #243b53; background: #fff; }}
    .missing {{ color: #7b8794; background: #f0f4f8; }}
    .assets {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; align-items: start; }}
    figure {{ margin: 0; }}
    .asset img {{ width: 100%; max-height: 480px; object-fit: contain; background: #fff; border: 1px solid #d9e2ec; border-radius: 6px; }}
    figcaption, .asset.missing {{ font-size: 12px; color: #52606d; margin-top: 6px; }}
    .diagnostics {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
    .metric {{ border: 1px solid #d9e2ec; border-radius: 999px; padding: 4px 8px; font-size: 12px; color: #334e68; background: #fff; }}
    .notes {{ margin: 0 0 8px 18px; padding: 0; color: #334e68; }}
    .quiet {{ color: #7b8794; }}
    .attempt {{ border: 1px dashed #bcccdc; border-radius: 8px; padding: 12px; margin-top: 10px; background: #fbfcfd; }}
    .attempt h4 {{ margin: 0 0 10px; font-size: 15px; }}
  </style>
</head>
<body>
  <main>
    <h1>Formula Second-Pass Review</h1>
    <section class="summary">
      <dl class="summary-grid">
        <div><dt>Route A formulas</dt><dd>{_html_text(summary.get('route_a_formula_count'))}</dd></div>
        <div><dt>Route B formulas</dt><dd>{_html_text(summary.get('route_b_formula_count'))}</dd></div>
        <div><dt>Suspicious</dt><dd>{_html_text(summary.get('suspicious_formula_count'))}</dd></div>
        <div><dt>Replaced</dt><dd>{_html_text(summary.get('replaced_count'))}</dd></div>
        <div><dt>No match</dt><dd>{_html_text(summary.get('no_match_count'))}</dd></div>
      </dl>
    </section>
    {''.join(rows)}
  </main>
</body>
</html>
"""
    path = output_dir / "review_index.html"
    path.write_text(html_text, encoding="utf-8")
    return path


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # Permission-denied and platform-specific probe errors are not proof
        # that a process is dead.  Preserve its staging directory.
        pass
    return True


def _cleanup_orphan_formula_staging_dirs(
    output_dir: Path,
) -> tuple[list[str], dict[str, Any] | None]:
    """Remove only provably abandoned staging entries for this output name."""

    parent = output_dir.parent
    if not parent.exists():
        return [], None
    if not parent.is_dir():
        return [], {
            "ok": False,
            "error": "formula_second_pass_orphan_staging_cleanup_failed",
            "output_dir": str(output_dir),
            "detail": "output parent is not a directory",
        }
    prefix = f".{output_dir.name}.formula_second_pass_staging_"
    removed: list[str] = []
    try:
        candidates = list(parent.iterdir())
    except OSError as exc:
        return [], {
            "ok": False,
            "error": "formula_second_pass_orphan_staging_cleanup_failed",
            "output_dir": str(output_dir),
            "detail": str(exc),
        }
    for candidate in candidates:
        if not candidate.name.startswith(prefix):
            continue
        suffix = candidate.name[len(prefix):]
        pid_match = re.match(r"([1-9][0-9]*)_", suffix)
        try:
            age_seconds = max(0.0, time.time() - candidate.lstat().st_mtime)
        except OSError as exc:
            return removed, {
                "ok": False,
                "error": "formula_second_pass_orphan_staging_cleanup_failed",
                "output_dir": str(output_dir),
                "staging_path": str(candidate),
                "detail": str(exc),
                "removed_staging_entries": removed,
            }
        if pid_match is not None:
            owner_pid = int(pid_match.group(1))
            if _process_is_alive(owner_pid):
                continue
        elif age_seconds < ORPHAN_STAGING_MIN_AGE_SECONDS:
            # Legacy staging names have no owner PID.  Only age can establish
            # that they are stale, so preserve recent entries conservatively.
            continue
        try:
            # Never follow a symlink bearing the staging prefix.  Unlink only
            # the directory entry; an external target remains untouched.
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            removed.append(candidate.name)
        except OSError as exc:
            return removed, {
                "ok": False,
                "error": "formula_second_pass_orphan_staging_cleanup_failed",
                "output_dir": str(output_dir),
                "staging_path": str(candidate),
                "detail": str(exc),
                "removed_staging_entries": removed,
            }
    return removed, None


def _formula_output_symlink_component(output_dir: Path) -> Path | None:
    """Return the first unsafe symlink in an output's lexical path."""

    output_absolute = output_dir.absolute()
    # macOS exposes /var and /tmp as stable aliases into /private.  They are
    # not user-controlled output escapes and appear in TemporaryDirectory
    # fixtures; do not reject every output merely because one system alias is
    # present in its ancestor chain.
    benign_system_aliases = {Path("/var"), Path("/tmp")}
    output_components = [*reversed(output_absolute.parents), output_absolute]
    return next(
        (
            path
            for path in output_components
            if path.is_symlink()
            and not (
                path in benign_system_aliases
                and path.resolve().is_relative_to(Path("/private"))
            )
        ),
        None,
    )


def _acquire_formula_output_lock(
    output_dir: Path,
) -> tuple[tuple[int, Path, os.stat_result] | None, dict[str, Any] | None]:
    """Acquire the sibling single-writer lock for one output directory."""

    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, {
            "ok": False,
            "error": "formula_second_pass_output_lock_failed",
            "output_dir": str(output_dir),
            "detail": str(exc),
        }
    lock_path = output_dir.parent / f".{output_dir.name}.formula_second_pass.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError:
        return None, {
            "ok": False,
            "error": "formula_second_pass_output_locked",
            "output_dir": str(output_dir),
            "lock_path": str(lock_path),
        }
    except OSError as exc:
        return None, {
            "ok": False,
            "error": "formula_second_pass_output_lock_failed",
            "output_dir": str(output_dir),
            "lock_path": str(lock_path),
            "detail": str(exc),
        }
    descriptor_stat: os.stat_result | None = None
    try:
        descriptor_stat = os.fstat(descriptor)
        token = f"pid={os.getpid()} nonce={os.urandom(16).hex()}\n".encode("ascii")
        os.write(descriptor, token)
        os.fsync(descriptor)
    except Exception as exc:
        if descriptor_stat is not None:
            _release_formula_output_lock((descriptor, lock_path, descriptor_stat))
        else:
            os.close(descriptor)
        return None, {
            "ok": False,
            "error": "formula_second_pass_output_lock_failed",
            "output_dir": str(output_dir),
            "lock_path": str(lock_path),
            "detail": str(exc),
        }
    assert descriptor_stat is not None
    return (descriptor, lock_path, descriptor_stat), None


def _release_formula_output_lock(
    lock: tuple[int, Path, os.stat_result],
) -> None:
    """Release only the same regular directory entry acquired by this run."""

    descriptor, lock_path, descriptor_stat = lock
    try:
        try:
            current_stat = lock_path.lstat()
        except OSError:
            current_stat = None
        if current_stat is not None and (
            current_stat.st_dev,
            current_stat.st_ino,
        ) == (descriptor_stat.st_dev, descriptor_stat.st_ino):
            try:
                lock_path.unlink()
            except OSError:
                pass
    finally:
        os.close(descriptor)


def run_formula_second_pass(
    route_a_dir: Path,
    route_b_dir: Path,
    output_dir: Path,
    review_candidate_args: list[str] | None = None,
    guarded_fallback_args: list[str] | None = None,
    guarded_fallback_eqs: set[int] | None = None,
    apply_all: bool = False,
) -> dict[str, Any]:
    """Run one formula pass while holding its per-output single-writer lock."""

    output_symlink_component = _formula_output_symlink_component(output_dir)
    if output_symlink_component is not None:
        return {
            "ok": False,
            "error": "formula_second_pass_output_symlink_not_allowed",
            "output_dir": str(output_dir),
            "symlink_component": str(output_symlink_component),
        }
    lock, lock_error = _acquire_formula_output_lock(output_dir)
    if lock_error is not None:
        return lock_error
    assert lock is not None
    try:
        return _run_formula_second_pass_with_lock_held(
            route_a_dir,
            route_b_dir,
            output_dir,
            review_candidate_args,
            guarded_fallback_args,
            guarded_fallback_eqs,
            apply_all,
        )
    finally:
        _release_formula_output_lock(lock)


def _run_formula_second_pass_with_lock_held(
    route_a_dir: Path,
    route_b_dir: Path,
    output_dir: Path,
    review_candidate_args: list[str] | None = None,
    guarded_fallback_args: list[str] | None = None,
    guarded_fallback_eqs: set[int] | None = None,
    apply_all: bool = False,
) -> dict[str, Any]:
    """Run the formula-only second pass on a single document."""
    if not route_a_dir.is_dir():
        return {
            "ok": False,
            "error": "route_a_dir_must_be_directory",
            "route_a_dir": str(route_a_dir),
        }
    if not route_b_dir.is_dir():
        return {
            "ok": False,
            "error": "route_b_dir_must_be_directory",
            "route_b_dir": str(route_b_dir),
        }
    for route_label, route_dir in (("route_a", route_a_dir), ("route_b", route_b_dir)):
        contract_error = _validate_route_contract_security(
            route_dir,
            route_label,
            apply_all=apply_all,
        )
        if contract_error is not None:
            return contract_error
    if apply_all and guarded_fallback_eqs:
        for value in guarded_fallback_args or []:
            fallback_label, fallback_dir = parse_review_candidate_arg(value)
            if not fallback_dir.is_dir():
                continue
            contract_error = _validate_route_contract_security(
                fallback_dir,
                f"guarded_fallback:{fallback_label}",
                apply_all=True,
            )
            if contract_error is not None:
                return contract_error
    route_a_resolved = route_a_dir.resolve()
    route_b_resolved = route_b_dir.resolve()
    output_resolved = output_dir.resolve()
    if route_a_resolved == route_b_resolved:
        return {
            "ok": False,
            "error": "formula_second_pass_input_routes_must_be_distinct",
            "route_a_dir": str(route_a_dir),
            "route_b_dir": str(route_b_dir),
        }
    if output_resolved in {route_a_resolved, route_b_resolved}:
        return {
            "ok": False,
            "error": "formula_second_pass_output_must_be_distinct_from_input_routes",
            "output_dir": str(output_dir),
        }
    output_symlink_component = _formula_output_symlink_component(output_dir)
    if output_symlink_component is not None:
        return {
            "ok": False,
            "error": "formula_second_pass_output_symlink_not_allowed",
            "output_dir": str(output_dir),
            "symlink_component": str(output_symlink_component),
        }
    if output_dir.exists() and not output_dir.is_dir():
        return {
            "ok": False,
            "error": "formula_second_pass_output_must_be_directory",
            "output_dir": str(output_dir),
        }
    removed_orphan_staging, cleanup_error = _cleanup_orphan_formula_staging_dirs(
        output_dir
    )
    if cleanup_error is not None:
        return cleanup_error
    if output_dir.exists():
        preexisting_entries = sorted(path.name for path in output_dir.iterdir())
        if preexisting_entries:
            return {
                "ok": False,
                "error": "formula_second_pass_output_dir_not_empty",
                "output_dir": str(output_dir),
                "preexisting_entries": preexisting_entries,
            }
    review_candidate_sources = load_review_candidate_sources(review_candidate_args or [])
    guarded_fallback_sources = load_review_candidate_sources(guarded_fallback_args or [])
    combined_review_sources = review_candidate_sources + guarded_fallback_sources

    route_a_doc = load_json(route_a_dir / "document.json")
    route_b_doc = load_json(route_b_dir / "document.json")

    if route_a_doc is None:
        return {"ok": False, "error": f"Route A document.json not found: {route_a_dir}"}
    if route_b_doc is None:
        return {"ok": False, "error": f"Route B document.json not found: {route_b_dir}"}

    route_a_doc = _normalize_document_chunk_pages(route_a_doc)
    route_b_doc = _normalize_document_chunk_pages(route_b_doc)

    route_a_formulas = extract_formulas(
        route_a_doc,
        target_page_sizes=_document_page_sizes(route_b_doc),
    )
    route_b_formulas = extract_formulas(
        route_b_doc,
        target_page_sizes=_document_page_sizes(route_b_doc),
    )
    route_a_formula_node_count = sum(
        1
        for node in iter_nodes(route_a_doc)
        if str(node.get("label") or "").lower() == "formula"
    )
    route_b_formula_node_count = sum(
        1
        for node in iter_nodes(route_b_doc)
        if str(node.get("label") or "").lower() == "formula"
    )

    suspicious_count = sum(1 for f in route_a_formulas if is_suspicious(f))
    route_b_markdown_identity_check: dict[str, Any] | None = None
    route_a_markdown_text: str | None = None

    if apply_all:
        route_a_status = load_json(route_a_dir / "status.json")
        route_b_status = load_json(route_b_dir / "status.json")
        if not isinstance(route_a_status, dict) or route_a_status.get("ok") is not True:
            return {
                "ok": False,
                "error": "route_a_status_not_successful",
                "route_a_status": route_a_status,
            }
        if not isinstance(route_b_status, dict) or route_b_status.get("ok") is not True:
            return {
                "ok": False,
                "error": "route_b_status_not_successful",
                "route_b_status": route_b_status,
            }
        job_identity_ok, job_identity_result = _check_route_job_identity(
            route_a_dir,
            route_b_dir,
        )
        if not job_identity_ok:
            return job_identity_result
        identity_ok, identity_result = _check_route_pdf_identity(route_a_dir, route_b_dir)
        if not identity_ok:
            return identity_result
        guarded_fallback_identity_checks: list[dict[str, Any]] = []
        if guarded_fallback_eqs:
            for source in guarded_fallback_sources:
                source_dir = Path(str(source.get("source_dir") or ""))
                source_status = load_json(source_dir / "status.json")
                if (
                    source.get("error")
                    or not isinstance(source_status, dict)
                    or source_status.get("ok") is not True
                ):
                    return {
                        "ok": False,
                        "error": "guarded_fallback_status_not_successful",
                        "guarded_fallback_source": str(source_dir),
                        "guarded_fallback_status": source_status,
                    }
                fallback_identity_ok, fallback_identity = _check_route_pdf_identity(
                    route_a_dir, source_dir
                )
                guarded_fallback_identity_checks.append(
                    {
                        "source_dir": str(source_dir),
                        **fallback_identity,
                    }
                )
                if not fallback_identity_ok:
                    return {
                        "ok": False,
                        "error": "guarded_fallback_identity_unverified",
                        "guarded_fallback_source": str(source_dir),
                        "guarded_fallback_identity_check": fallback_identity,
                    }
        if route_a_formula_node_count != len(route_a_formulas):
            return {
                "ok": False,
                "error": "route_a_formula_inventory_malformed",
                "route_a_formula_node_count": route_a_formula_node_count,
                "route_a_formula_count": len(route_a_formulas),
                "route_b_source_identity_check": identity_result,
            }
        if route_b_formula_node_count != len(route_b_formulas):
            return {
                "ok": False,
                "error": "route_b_formula_inventory_malformed",
                "route_b_formula_node_count": route_b_formula_node_count,
                "route_b_formula_count": len(route_b_formulas),
                "route_b_source_identity_check": identity_result,
            }
        if not route_a_formulas and route_b_formulas:
            return {
                "ok": False,
                "error": "route_a_formula_inventory_empty_route_b_nonempty",
                "route_a_formula_count": 0,
                "route_b_formula_count": len(route_b_formulas),
                "route_b_source_identity_check": identity_result,
            }
        route_a_markdown_path = route_a_dir / "document.md"
        if route_a_markdown_path.is_file():
            try:
                route_a_markdown_text = route_a_markdown_path.read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeError) as exc:
                return {
                    "ok": False,
                    "error": "route_a_markdown_unreadable",
                    "route_a_markdown_path": str(route_a_markdown_path),
                    "detail": str(exc),
                    "route_b_source_identity_check": identity_result,
                }
        else:
            route_a_markdown_text = ""
        route_a_markdown_formula_count = len(
            _markdown_display_formula_blocks(route_a_markdown_text)
        )
        if route_a_markdown_formula_count != len(route_a_formulas):
            return {
                "ok": False,
                "error": "route_a_markdown_formula_inventory_mismatch",
                "route_a_formula_count": len(route_a_formulas),
                "route_a_markdown_formula_count": route_a_markdown_formula_count,
                "route_b_source_identity_check": identity_result,
            }
        if route_a_formulas and not route_b_formulas:
            return {
                "ok": False,
                "error": "route_b_formula_inventory_empty_route_a_nonempty",
                "route_a_formula_count": len(route_a_formulas),
                "route_b_formula_count": 0,
                "route_b_source_identity_check": identity_result,
            }
        if not route_a_formulas and not route_b_formulas:
            return {
                "ok": False,
                "error": "route_formula_inventory_empty_no_visual_evidence",
                "route_a_formula_count": 0,
                "route_b_formula_count": 0,
                "route_b_source_identity_check": identity_result,
            }
        route_b_markdown_path = route_b_dir / "document.md"
        if route_b_markdown_path.is_file():
            try:
                route_b_markdown_text = route_b_markdown_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return {
                    "ok": False,
                    "error": "route_b_markdown_unreadable",
                    "route_b_markdown_path": str(route_b_markdown_path),
                    "detail": str(exc),
                    "route_b_source_identity_check": identity_result,
                }
            route_b_markdown_identity_check = validate_markdown_formula_identity(
                route_b_markdown_text,
                route_b_formulas,
                allow_missing_if_empty=False,
            )
            if not route_b_markdown_identity_check.get("ok"):
                markdown_error = route_b_markdown_identity_check.get("error")
                if markdown_error == "markdown_missing":
                    contract_error = "route_b_markdown_missing"
                elif markdown_error == "inventory_mismatch":
                    contract_error = "route_b_markdown_formula_inventory_mismatch"
                else:
                    contract_error = "route_b_markdown_formula_identity_mismatch"
                return {
                    "ok": False,
                    "error": contract_error,
                    "route_b_markdown_identity_check": route_b_markdown_identity_check,
                    "route_b_source_identity_check": identity_result,
                }
        else:
            # A formula-bearing Route B output without document.md is not a
            # complete apply-all contract.  An empty route may omit it, but
            # the decision is explicit and persisted in the summary.
            route_b_markdown_identity_check = validate_markdown_formula_identity(
                "",
                route_b_formulas,
                allow_missing_if_empty=True,
            )
            if not route_b_markdown_identity_check.get("ok"):
                return {
                    "ok": False,
                    "error": "route_b_markdown_missing",
                    "route_b_markdown_identity_check": route_b_markdown_identity_check,
                    "route_b_source_identity_check": identity_result,
                }
    else:
        job_identity_result = None
        identity_result = None
        guarded_fallback_identity_checks = []

    patched_doc, replacement_log = patch_document_json(
        route_a_doc,
        route_b_formulas,
        guarded_fallback_sources,
        guarded_fallback_eqs or set(),
        apply_all=apply_all,
        route_a_target_page_sizes=_document_page_sizes(route_b_doc),
    )

    replaced_count = sum(1 for e in replacement_log if e["status"] == "replaced")
    no_match_count = sum(
        1 for e in replacement_log
        if e["status"] in (
            "suspicious_no_route_b_match",
            "guarded_fallback_ambiguous",
            "guarded_fallback_geometry_unverified",
            "route_b_also_empty",
            "route_b_candidate_failed_quality_gate",
            "fallback_candidate_failed_quality_gate",
            "render_failed_latex",
        )
    )
    guarded_fallback_count = sum(
        1
        for entry in replacement_log
        if entry.get("status") == "replaced"
        and entry.get("candidate_source") == "guarded_fallback"
    )

    if apply_all and (
        len(replacement_log) != len(route_a_formulas)
        or no_match_count
        or replaced_count != len(route_a_formulas)
    ):
        return {
            "ok": False,
            "error": "route_b_formula_coverage_incomplete",
            "route_a_formula_count": len(route_a_formulas),
            "route_b_formula_count": len(route_b_formulas),
            "second_pass_attempted_count": len(replacement_log),
            "replaced_count": replaced_count,
            "no_match_count": no_match_count,
            "route_b_source_identity_check": (
                identity_result["route_b_source_identity_check"]
                if identity_result
                else None
            ),
            "replacement_log": replacement_log,
        }

    # Patch document.md
    md_path = route_a_dir / "document.md"
    if route_a_markdown_text is not None:
        md_text = route_a_markdown_text
    elif md_path.exists():
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {
                "ok": False,
                "error": "route_a_markdown_unreadable",
                "route_a_markdown_path": str(md_path),
                "detail": str(exc),
            }
    else:
        md_text = ""
    patched_md = patch_document_md(md_text, route_a_formulas, replacement_log)
    if apply_all:
        missing_markdown_anchors = [
            int(entry.get("formula_no"))
            for entry in replacement_log
            if entry.get("status") == "replaced"
            and entry.get("markdown_anchor_status") != "replaced_at_anchor"
            and isinstance(entry.get("formula_no"), int)
        ]
        if missing_markdown_anchors:
            return {
                "ok": False,
                "error": "route_a_markdown_formula_coverage_incomplete",
                "missing_formula_numbers": missing_markdown_anchors,
                "route_a_formula_count": len(route_a_formulas),
                "replacement_log": replacement_log,
            }

    summary = {
        "route_a_dir": str(route_a_dir),
        "route_b_dir": str(route_b_dir),
        "output_dir": str(output_dir),
        "route_job_identity_check": (
            job_identity_result["route_job_identity_check"]
            if job_identity_result
            else None
        ),
        "route_b_source_identity_check": identity_result["route_b_source_identity_check"] if identity_result else None,
        "route_b_markdown_identity_check": route_b_markdown_identity_check,
        "guarded_fallback_identity_checks": guarded_fallback_identity_checks,
        "route_a_formula_count": len(route_a_formulas),
        "route_b_formula_count": len(route_b_formulas),
        "suspicious_formula_count": suspicious_count,
        "second_pass_attempted_count": len(replacement_log),
        "replaced_count": replaced_count,
        "no_match_count": no_match_count,
        "fallback_count": guarded_fallback_count,
        "guarded_fallback_count": guarded_fallback_count,
        "crop_only_without_formula_count": sum(
            1 for entry in replacement_log if entry.get("crop_only_without_formula")
        ),
        "render_failed_latex_count": sum(
            1 for entry in replacement_log if entry.get("status") == "render_failed_latex"
        ),
        "apply_all": apply_all,
        "review_candidate_sources": [
            {
                "label": source["label"],
                "source_dir": str(source["source_dir"]),
                "formula_count": len(source["formulas"]),
                "error": source.get("error"),
            }
            for source in review_candidate_sources
        ],
        "guarded_fallback_sources": [
            {
                "label": source["label"],
                "source_dir": str(source["source_dir"]),
                "formula_count": len(source["formulas"]),
                "error": source.get("error"),
            }
            for source in guarded_fallback_sources
        ],
        "guarded_fallback_eqs": sorted(guarded_fallback_eqs or set()),
        "removed_orphan_staging_entries": removed_orphan_staging,
        "replacement_log": replacement_log,
        "ok": True,
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_preexisted = output_dir.exists()
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=(
                f".{output_dir.name}.formula_second_pass_staging_"
                f"{os.getpid()}_"
            ),
            dir=output_dir.parent,
        )
    )
    try:
        # Publish the complete sidecar directory atomically.  Evidence and
        # review-index failures must not leave document.json/document.md that
        # look consumable beside a missing summary.
        (staging_dir / "document.json").write_text(
            json.dumps(patched_doc, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (staging_dir / "document.md").write_text(patched_md, encoding="utf-8")
        add_review_evidence(
            replacement_log,
            route_a_dir,
            route_b_dir,
            combined_review_sources,
            staging_dir,
            md_text,
            patched_md,
            route_a_source_sha256=(
                identity_result["route_b_source_identity_check"].get(
                    "route_a_source_sha256"
                )
                if identity_result
                else None
            ),
            route_b_source_sha256=(
                identity_result["route_b_source_identity_check"].get(
                    "route_b_source_sha256"
                )
                if identity_result
                else None
            ),
            route_a_formulas=route_a_formulas,
            route_b_formulas=route_b_formulas,
        )
        if apply_all:
            evidence_gaps = _replacement_evidence_gaps(
                replacement_log,
                staging_dir,
            )
            if evidence_gaps:
                shutil.rmtree(staging_dir, ignore_errors=True)
                return {
                    "ok": False,
                    "error": "formula_second_pass_source_evidence_incomplete",
                    "output_dir": str(output_dir),
                    "route_a_formula_count": len(route_a_formulas),
                    "route_b_formula_count": len(route_b_formulas),
                    "evidence_gaps": evidence_gaps,
                    "replacement_log": replacement_log,
                }
        write_review_html(staging_dir, summary)
        summary["review_html_path"] = str(output_dir / "review_index.html")
        (staging_dir / "second_pass_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if output_preexisted:
            output_dir.rmdir()
        staging_dir.replace(output_dir)
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if output_preexisted and not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "ok": False,
            "error": "formula_second_pass_output_publication_failed",
            "error_type": exc.__class__.__name__,
            "detail": str(exc),
            "output_dir": str(output_dir),
            "route_a_formula_count": len(route_a_formulas),
            "route_b_formula_count": len(route_b_formulas),
            "replacement_log": replacement_log,
        }

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--route-a-dir",
        type=Path,
        required=True,
        help="Route A (Docling Serve standard pipeline) output directory.",
    )
    parser.add_argument(
        "--route-b-dir",
        type=Path,
        required=True,
        help="Route B (VlmPipeline evaluation) output directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for merged document.",
    )
    parser.add_argument(
        "--review-candidate-dir",
        action="append",
        default=[],
        help=(
            "Optional review-only formula candidate source as LABEL=DIR or DIR. "
            "Candidates are shown in review HTML but never patched into outputs."
        ),
    )
    parser.add_argument(
        "--guarded-fallback-dir",
        action="append",
        default=[],
        help=(
            "Optional guarded replacement source as LABEL=DIR or DIR. "
            "Only equations listed with --guarded-fallback-eq may use it."
        ),
    )
    parser.add_argument(
        "--guarded-fallback-eq",
        action="append",
        type=int,
        default=[],
        help="Reviewed equation number allowed to use guarded fallback replacement.",
    )
    parser.add_argument(
        "--apply-all",
        action="store_true",
        help="Attempt second-pass matching for every Route A formula, not only suspicious formulas.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_formula_second_pass(
        args.route_a_dir,
        args.route_b_dir,
        args.output_dir,
        args.review_candidate_dir,
        args.guarded_fallback_dir,
        set(args.guarded_fallback_eq),
        apply_all=args.apply_all,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
