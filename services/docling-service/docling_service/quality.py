"""Quality policy helpers for real Docling conversions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONVERSION_POLICY = "quality_first"
GXX_TOKEN_RE = re.compile(r"/G[0-9A-Fa-f]{2}")
GXX_FAILURE_MIN_COUNT = 10
GXX_FAILURE_MIN_DENSITY = 0.002


@dataclass(frozen=True)
class TextQuality:
    """Small text-layer quality signal used to decide whether OCR is needed."""

    gxx_count: int
    gxx_density: float
    failed: bool


def measure_gxx_quality(*texts: str | None) -> TextQuality:
    """Count PDF CID fallback tokens such as /G21 and flag dense bad text."""
    combined = "\n".join(str(text) for text in texts if text)
    gxx_count = len(GXX_TOKEN_RE.findall(combined))
    text_length = len(combined)
    density = gxx_count / text_length if text_length else 0.0
    failed = gxx_count >= GXX_FAILURE_MIN_COUNT and density >= GXX_FAILURE_MIN_DENSITY
    return TextQuality(gxx_count=gxx_count, gxx_density=density, failed=failed)


def count_tables(document_dict: dict[str, Any] | None) -> int | None:
    """Count tables from Docling's document structure without inventing them."""
    if not isinstance(document_dict, dict):
        return None

    tables = document_dict.get("tables")
    if isinstance(tables, list):
        return len(tables)
    if isinstance(tables, dict):
        return len(tables)

    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("label") == "table" or str(value.get("self_ref", "")).startswith("#/tables/"):
                key = str(value.get("self_ref") or value.get("$ref") or id(value))
                seen.add(key)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document_dict)
    return len(seen)


def extract_table_dicts(document_dict: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return table dictionaries that contain real extracted table data."""
    if not isinstance(document_dict, dict):
        return []
    tables = document_dict.get("tables")
    if isinstance(tables, dict):
        table_values = list(tables.values())
    elif isinstance(tables, list):
        table_values = tables
    else:
        table_values = []

    extracted: list[dict[str, Any]] = []
    for table in table_values:
        if not isinstance(table, dict):
            continue
        data = table.get("data")
        if _has_table_data(data):
            extracted.append(table)
    return extracted


def _has_table_data(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("table_cells", "grid"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return True
    return any(isinstance(data.get(key), int) and data[key] > 0 for key in ("num_rows", "num_cols"))


def relative_output(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
