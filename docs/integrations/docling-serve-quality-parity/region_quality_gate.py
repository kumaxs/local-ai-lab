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
import json
import math
import os
import re
import stat
import statistics
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 1000
REGION_STATUSES = ("verified_semantic", "visual_only", "unresolved")
STRUCTURAL_KINDS = ("table", "algorithm", "code", "formula", "inline_math")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SOURCE_REF_RE = re.compile(r"[^A-Za-z0-9_.:/#@+\-]+")
_TABLE_NUMBER_RE = re.compile(
    r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|\.\d+)(?![\w.])"
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
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    if isinstance(value, str) and value.strip() != str(parsed):
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
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _document_nodes(document_json: Any, labels: set[str] | None = None) -> list[dict[str, Any]]:
    labels = {value.casefold() for value in (labels or set())}
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
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
        records.append(
            {
                "label": label,
                "text": _text(node.get("text")),
                "page_no": _first_page(node),
                "bbox": _first_bbox(node),
                "source_ref": _source_ref(node.get("self_ref")),
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
    current = output_dir
    try:
        if output_dir.is_symlink():
            return None
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        candidate = current
        candidate.resolve(strict=False).relative_to(output_dir.resolve())
        if not candidate.is_file() or not stat.S_ISREG(os.lstat(candidate).st_mode):
            return None
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "json_file_missing_or_unsafe"
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"json_read_failed:{type(exc).__name__}"


def _atomic_json(path: Path, payload: Any) -> str | None:
    """Write a sidecar without following a pre-existing symlink."""

    if path.is_symlink():
        return "sidecar_path_is_symlink"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except Exception:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError) as exc:
        return f"sidecar_write_failed:{type(exc).__name__}"
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
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            continue
        for item in candidates:
            if not isinstance(item, dict):
                continue
            ref = _source_ref(item.get("source_ref"))
            if ref:
                result[ref] = item
    return result


def _ref_set(source_visuals: dict[str, Any], key: str) -> set[str]:
    values = source_visuals.get(key)
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {ref for value in values if (ref := _source_ref(value))}


def _manifest_entry(metadata: dict[str, Any], kind: str, source_ref: str | None) -> dict[str, Any] | None:
    manifest = metadata.get("structural_visual_provenance_manifest")
    if not isinstance(manifest, dict) or not source_ref:
        return None
    entries = manifest.get("tables" if kind == "table" else kind)
    if not isinstance(entries, list):
        return None
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and _source_ref(item.get("source_ref")) == source_ref
    ]
    return matches[0] if len(matches) == 1 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


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

        cells: list[dict[str, Any]] = []
        invalid_cell_geometry_count = 0
        for raw in raw_cells:
            if not isinstance(raw, dict):
                invalid_cell_geometry_count += 1
                continue
            bbox = _safe_bbox(raw.get("bbox"))
            start_row = _nonnegative_int(raw.get("start_row_offset_idx"))
            end_row = _nonnegative_int(raw.get("end_row_offset_idx"))
            if bbox is None or start_row is None or end_row is None or end_row <= start_row:
                invalid_cell_geometry_count += 1
                continue
            low, high = sorted((float(bbox["t"]), float(bbox["b"])))
            cells.append(
                {
                    "start_row": start_row,
                    "end_row": end_row,
                    "height": high - low,
                    "low": low,
                    "high": high,
                    "center": (low + high) / 2.0,
                    "number_count": len(_TABLE_NUMBER_RE.findall(_text(raw.get("text"), 500))),
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
        if raw_cells and invalid_cell_geometry_count:
            reasons.append("table_cell_geometry_invalid")
        if cross_row_cell_count:
            reasons.append("table_cell_crosses_semantic_row_boundary")
        if collapsed_rows:
            reasons.append("table_row_likely_collapsed")
        diagnostics[source_ref] = {
            "geometry_checked": bool(cells and baseline_height),
            "cell_count": len(raw_cells),
            "valid_cell_geometry_count": len(cells),
            "invalid_cell_geometry_count": invalid_cell_geometry_count,
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
) -> list[dict[str, Any]]:
    expected_key = f"{kind}_source_expected_refs"
    expected_refs = _ref_set(source_visuals, expected_key)
    candidates = _candidate_map(source_visuals, kind)
    if not expected_refs:
        expected_refs = set(candidates)

    count_key = {
        "table": "tables",
        "algorithm": "algorithms",
        "code": "code_blocks",
        "formula": "formulas",
    }.get(kind)
    expected_count = primary_counts.get(count_key) if count_key else None
    try:
        expected_count = int(expected_count or 0)
    except (TypeError, ValueError):
        expected_count = 0
    records: list[dict[str, Any]] = []
    if expected_count > 0 and not expected_refs:
        records.append(
            _record(
                output_dir=output_dir,
                kind=kind,
                index=0,
                status="unresolved",
                critical=True,
                reasons=["expected_region_refs_missing"],
                signals={"source_ref_present": False},
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
    payload = source_visuals.get(f"{kind}_source_renderings")
    records_payload = payload if isinstance(payload, dict) else {}

    for ordinal, ref in enumerate(sorted(expected_refs), start=1):
        candidate = candidates.get(ref) or {}
        entry = _manifest_entry(metadata, kind, ref) or {}
        asset = candidate.get("image") or candidate.get("path") or entry.get("asset_path")
        page_no = candidate.get("page_no") or entry.get("page_no")
        bbox = candidate.get("bbox") or entry.get("node_bbox")
        body_identity = entry.get("structural_body_identity_sha256")
        reasons: list[str] = []
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
) -> list[dict[str, Any]]:
    """Build index-bound records for display formulas.

    Formula evidence is intentionally index based in the adapter (unlike
    tables/algorithms/code, which use stable ``source_ref`` values).  A
    ``formula:index:N`` identifier keeps that contract explicit and prevents a
    crop with a matching count from being mistaken for the wrong occurrence.
    """

    raw_expected = source_visuals.get("formula_source_expected_indexes")
    expected: set[int] = set()
    if isinstance(raw_expected, (list, tuple, set)):
        for value in raw_expected:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if index > 0:
                expected.add(index)
    approved_drops: set[int] = set()
    approved_drop_reasons: dict[int, set[str]] = {}
    declared_drops = source_visuals.get("formula_source_dropped_artifacts")
    if isinstance(declared_drops, list):
        for artifact in declared_drops:
            if not isinstance(artifact, dict):
                continue
            try:
                index = int(artifact.get("raw_formula_index", artifact.get("index")))
            except (TypeError, ValueError):
                continue
            if index > 0 and str(artifact.get("reason") or ""):
                approved_drops.add(index)
                approved_drop_reasons.setdefault(index, set()).add(
                    str(artifact.get("reason"))
                )
    expected_count: int | None = None
    if "formulas" in primary_counts:
        try:
            expected_count = int(primary_counts.get("formulas") or 0)
        except (TypeError, ValueError):
            expected_count = 0
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
    by_index: dict[int, dict[str, Any]] = {}
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                index = int(candidate.get("formula_index"))
            except (TypeError, ValueError):
                continue
            if index > 0:
                by_index[index] = candidate

    html_indexes = {
        int(value)
        for value in (source_visuals.get("formula_source_html_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    markdown_indexes = {
        int(value)
        for value in (source_visuals.get("formula_source_markdown_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    missing_indexes = {
        int(value)
        for value in (source_visuals.get("formula_source_missing_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    unexpected_indexes = {
        int(value)
        for value in (source_visuals.get("formula_source_unexpected_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    duplicate_html = {
        int(value)
        for value in (
            source_visuals.get("formula_source_duplicate_html_anchor_indexes") or []
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    duplicate_markdown = {
        int(value)
        for value in (
            source_visuals.get("formula_source_duplicate_markdown_anchor_indexes")
            or []
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    appendix_indexes = {
        int(value)
        for value in (source_visuals.get("formula_source_html_appendix_indexes") or [])
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    } | {
        int(value)
        for value in (
            source_visuals.get("formula_source_markdown_appendix_indexes") or []
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }

    for ordinal, index in enumerate(sorted(expected), start=1):
        candidate = by_index.get(index) or {}
        selected_image = candidate.get("selected_image")
        selected = _text(candidate.get("selected"), 40)
        reasons: list[str] = []
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
) -> list[dict[str, Any]]:
    primary = quality.get("primary_surface")
    if not isinstance(primary, dict):
        primary = {}
    regions = primary.get("inline_math_source_regions")
    if not isinstance(regions, list):
        regions = []
    by_anchor = {
        _text(item.get("anchor"), 180): item
        for item in regions
        if isinstance(item, dict) and _text(item.get("anchor"), 180)
    }
    expected = _ref_set(source_visuals, "inline_math_source_expected_anchors")
    if not expected:
        expected = set(by_anchor)
    candidates = source_visuals.get("inline_math_source_renderings")
    candidate_by_anchor: dict[str, dict[str, Any]] = {}
    if isinstance(candidates, dict):
        for item in candidates.get("candidates") or []:
            if isinstance(item, dict) and _text(item.get("anchor"), 180):
                candidate_by_anchor[_text(item.get("anchor"), 180)] = item
    html = _ref_set(source_visuals, "inline_math_source_html_anchors")
    markdown = _ref_set(source_visuals, "inline_math_source_markdown_anchors")
    missing_crop = _ref_set(source_visuals, "inline_math_source_missing_crop_anchors")
    missing_html = _ref_set(source_visuals, "inline_math_source_missing_html_anchors")
    missing_markdown = _ref_set(source_visuals, "inline_math_source_missing_markdown_anchors")
    duplicate_html = _ref_set(source_visuals, "inline_math_source_duplicate_html_anchors")
    duplicate_markdown = _ref_set(source_visuals, "inline_math_source_duplicate_markdown_anchors")
    records: list[dict[str, Any]] = []
    for ordinal, anchor in enumerate(sorted(expected), start=1):
        region = by_anchor.get(anchor) or {}
        candidate = candidate_by_anchor.get(anchor) or {}
        reasons: list[str] = []
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
    records: list[dict[str, Any]] = []
    candidates = structural.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    seen: set[tuple[str, int | None, str]] = set()
    for ordinal, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        picture_overlap = _truthy(candidate.get("picture_overlap"))
        label = _text(candidate.get("label"), 64).casefold().removeprefix("quarantined_")
        kind_hint = _text(candidate.get("kind"), 80).casefold()
        header_footer = label in {"page_header", "page_footer"} or "page_header" in kind_hint or "page_footer" in kind_hint
        if not picture_overlap and not header_footer:
            continue
        kind = "picture_ocr" if picture_overlap else "header_footer"
        page = _safe_page(candidate.get("page_no"))
        preview = _text(candidate.get("text") or candidate.get("text_preview"))
        key = (kind, page, preview)
        if key in seen:
            continue
        seen.add(key)
        residual = candidate.get("final_output_residual_surfaces")
        malformed_residuals = residual is not None and not isinstance(residual, list)
        residuals = [
            _text(value, 80)
            for value in (residual if isinstance(residual, list) else [])
            if _text(value, 80)
        ]
        action = _text(candidate.get("action"), 80)
        reasons: list[str] = []
        if malformed_residuals:
            reasons.append("residual_surface_schema_invalid")
        if residuals:
            reasons.append("main_flow_residual")
        if action != "quarantine_from_main_text_flow":
            reasons.append("isolation_not_proven")
        record_status = "verified_semantic" if not reasons else "unresolved"
        # Quarantine diagnostics call this field ``evidence`` and point to the
        # source page crop; retain that path as the bounded asset proof rather
        # than manufacturing a picture filename that may not exist.
        asset = (
            candidate.get("source_asset")
            or candidate.get("image")
            or candidate.get("evidence")
        )
        records.append(
            _record(
                output_dir=output_dir,
                kind=kind,
                index=ordinal,
                page_no=page,
                bbox=candidate.get("bbox"),
                source_ref=candidate.get("source_ref") or f"{kind}:{ordinal}",
                source_asset=asset,
                signals={
                    "picture_overlap": picture_overlap,
                    "residual_surfaces": residuals,
                    "quarantine_action": action or None,
                },
                status=record_status,
                critical=True,
                reasons=reasons,
                text_preview=preview,
            )
        )
    return records


def _bare_picture_records(output_dir: Path, document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, item in enumerate(_document_nodes(document_json, {"picture"}), start=1):
        source_asset = f"pictures/picture_{index}.png"
        asset_present = _safe_asset(output_dir, source_asset) is not None
        records.append(
            _record(
                output_dir=output_dir,
                kind="picture",
                index=index,
                page_no=item.get("page_no"),
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
    return records


def _source_pdf_sha(metadata: dict[str, Any], pdf_inventory: Any) -> str | None:
    if isinstance(metadata, dict):
        for key in ("visual_evidence_input_sha256", "conversion_input_sha256", "input_sha256", "source_pdf_sha256"):
            value = _sha256(metadata.get(key))
            if value:
                return value
    if isinstance(pdf_inventory, dict):
        return _sha256(pdf_inventory.get("source_pdf_sha256"))
    return None


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
    """Evaluate final region evidence and optionally write both sidecars.

    The input dictionaries are mutated only to append the compact quality
    signal and the two generated-output names.  Document surfaces are never
    changed.  If dictionaries are omitted they are loaded from the output
    directory and the resulting metadata/status files are updated, which makes
    this function suitable as an independent post-publish validator.
    """

    root = Path(output_dir)
    loaded_metadata = metadata is None
    loaded_status = status is None
    # Preserve caller-owned dictionaries so production adapter state receives
    # the gate's ``status.ok``/``degraded_failure`` mutation immediately.  A
    # shallow copy here would make the returned sidecars look correct while
    # the in-memory release status remained successful.
    metadata = metadata if isinstance(metadata, dict) else {}
    status = status if isinstance(status, dict) else {}
    errors: list[str] = []
    try:
        root_safe = root.is_dir() and not root.is_symlink()
    except OSError:
        root_safe = False
    if not root_safe:
        errors.append("output_dir_missing_or_unsafe")
    if document_json is None:
        document_json, error = _read_json(root / "document.json")
        if error:
            errors.append(error)
    if loaded_metadata:
        value, error = _read_json(root / "metadata.json")
        metadata = value if isinstance(value, dict) else {}
        if error:
            errors.append(error)
    if loaded_status:
        value, error = _read_json(root / "status.json")
        status = value if isinstance(value, dict) else {}
        if error:
            errors.append(error)
    if not isinstance(document_json, dict):
        errors.append("document_json_missing_or_invalid")
        document_json = {}
    try:
        max_records = int(max_records)
    except (TypeError, ValueError):
        max_records = DEFAULT_MAX_RECORDS
    max_records = max(1, min(max_records, DEFAULT_MAX_RECORDS))

    quality, source_visuals = _source_signals(status, metadata)
    primary = quality.get("primary_surface")
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
            )
        )
    records.extend(_formula_region_records(root, source_visuals, metadata, primary_counts))
    records.extend(_inline_math_records(root, quality, source_visuals))

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
    source_sha = _source_pdf_sha(metadata, pdf_inventory)
    signals = {
        "schema_version": SCHEMA_VERSION,
        "regions_path": "regions.json",
        "source_pdf_sha256": source_sha,
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
        quality_signals = status.setdefault("quality_signals", {})
        if not isinstance(quality_signals, dict):
            quality_signals = {}
            status["quality_signals"] = quality_signals
        quality_signals["region_quality_gate"] = signals
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

        state_errors: list[str] = []
        for name, should_write, state_payload in (
            ("metadata.json", loaded_metadata, metadata),
            ("status.json", loaded_status, status),
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
