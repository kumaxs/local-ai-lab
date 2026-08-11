from __future__ import annotations

"""Standalone PDF structure/formula inventory extractor.

This module intentionally does not import qpa adapter modules so it can be used as a
drop-in dependency from quality scripts and tests.
"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Optional, Sequence

KIND_ORDER = ("algorithm", "code", "table", "formula")
KIND_HEALTHY = "healthy"
KIND_UNKNOWN = "unknown"

ALGORITHM_HEADING_RE = re.compile(r"^\s*(?:Algorithm|算法)\s*\d+\s*", re.IGNORECASE)
ALGORITHM_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[:.)]\s+")
ALGORITHM_CONTROL_RE = re.compile(r"^\s*(?:if|elif|else|for|while|return|break|continue|try|except|finally|with|class|def)\b", re.IGNORECASE)
ALGORITHM_REQUIRE_KEYWORD_RE = re.compile(r"\b(?:Require|Requires|Ensure|Ensures)\b", re.IGNORECASE)
ALGORITHM_PROSE_TAIL_RE = re.compile(
    r"\b(?:is|are|was|were|this|these|that|using|used|proposed|propose|can|will|we|they|it)\b",
    re.IGNORECASE,
)

ASSIGNMENT_RE = re.compile(r"\b[\w\.\[\]']+\s*(?:=|:=|\+=|-=|\*=|/=|%=|->|<-|<<=|>>=)\s+")
CONTROL_KEYWORD_RE = re.compile(r"(?im)^\s*(?:if|elif|else|for|while|return|yield|break|continue|try|except|finally|class|def)\b")
INPUT_LABEL_ASSIGN_RE = re.compile(r"^\s*(Input|Label)\s*[:=]", re.IGNORECASE)
INPUT_LABEL_ONLY_RE = re.compile(r"^\s*(?:Input|Label)\s*:?\s*$", re.IGNORECASE)
CODE_MONO_FONT_RE = re.compile(r"(?i)(?:courier|mono|consola|consolas|menlo|monaco|source.?code|dejavu.?sans.?mono|terminal|monaco)")
INPUT_LABEL_MARKER_RE = re.compile(r"\[(?:CLS|MASK|SEP)\]|\\\[(?:MASK|CLS|SEP)\]|Input|Label", re.IGNORECASE)

TABLE_CAPTION_RE = re.compile(r"^\s*Table\s*\d+\s*[:.]")
TABLE_CAPTION_NOISE_RE = re.compile(
    r"^\s*Table\s*\d+\s*[:.]\s*(?:In this table|This table|Following table)\b",
    re.IGNORECASE,
)
TABLE_CN_PUNCT_SCAN_RE = re.compile(
    r"(?:^|(?<![\w\u4e00-\u9fff]))\s*表([&!#,+)\(])([^\n\r]+)",
    re.IGNORECASE,
)
TABLE_CN_FORBIDDEN_PREFIXES = ("所示", "如下", "见", "如", "展示")
EQ_NUMBER_RE = re.compile(r"\(\s*\d{1,4}(?:\.\d+)?\s*\)\s*$")
EQ_NUMBER_ANY_RE = re.compile(r"\(\s*\d{1,4}(?:\.\d+)?\s*\)")
MATH_OPERATOR_RE = re.compile(r"[+\-*/=<>≤≥≠≈∈∑∏∫√×÷→←⇒⇔∂∇]")
MATH_GREEK_RE = re.compile(r"[α-ωΑ-Ω]")
MATH_COMMAND_RE = re.compile(
    r"\\(?:frac|sum|int|sqrt|mathbf|mathrm|left|right|begin|end|lim|cdot|times|in|forall|exists|alpha|beta|gamma|delta|epsilon|pi|sigma|theta|phi|psi|omega|mathit|partial|nabla|neq|leq|geq|infty|cdots|quad)\b",
    re.IGNORECASE,
)
MATH_FONT_RE = re.compile(r"(?i)(?:math|symbol|cmsy|cmmib|cmmc|stix|xits|cambria math|latin modern math)")
MATH_SUBSUP_RE = re.compile(r"[_^]\s*\{[^}]+\}|[_^]\w")
FORMULA_HYPERPARAM_RE = re.compile(r"\([A-Za-z]+\s*=\s*\d+(?:\.\d+)?(?:\s*,\s*[A-Za-z]+\s*=\s*\d+(?:\.\d+)?)+\)")
FORMULA_NOISE_WORD_RE = re.compile(
    r"\b(?:a|an|the|and|that|this|with|for|from|were|was|we|they|their|there|are|is|isn't|was|were|when|where|which|then|into|using|used|compare|compared|compares|reported|report|learning|rate|parameters?|tokens?|models?|training)\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://", re.IGNORECASE)
UNKNOWN_TOKEN_RE = re.compile(r"\(cid:\d+\)|\ufffd")


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_bbox(raw_bbox: Any) -> Optional[dict[str, float]]:
    if not raw_bbox:
        return None
    if isinstance(raw_bbox, dict):
        values = [raw_bbox.get("l"), raw_bbox.get("r"), raw_bbox.get("t"), raw_bbox.get("b")]
    else:
        values = raw_bbox
    try:
        l, r, t, b = values[:4]
    except Exception:
        return None
    try:
        return {
            "l": float(l),
            "r": float(r),
            "t": float(t),
            "b": float(b),
            "coord_origin": "TOPLEFT",
        }
    except (TypeError, ValueError):
        return None


def _extract_node_bbox(node: dict[str, Any]) -> Optional[dict[str, float]]:
    provs = node.get("prov")
    if isinstance(provs, list):
        for prov in provs:
            if not isinstance(prov, dict):
                continue
            candidate = _coerce_bbox(prov.get("bbox"))
            if candidate:
                return candidate
    return _coerce_bbox(node.get("bbox"))


def _normalize_chunk_page_no(
    page_no: Optional[int],
    page_offset: Optional[int],
    chunk_page_count: Optional[int],
) -> Optional[int]:
    if page_no is None:
        return None
    if page_offset is None or page_offset <= 0:
        return page_no
    if page_offset <= page_no <= page_offset + (chunk_page_count or 0) - 1:
        return page_no
    if page_no <= (chunk_page_count or 0):
        return page_no + page_offset - 1
    if page_no < page_offset:
        return page_no + page_offset - 1
    return page_no


def _first_page_no(node: dict[str, Any]) -> Optional[int]:
    prov = node.get("prov")
    if isinstance(prov, list):
        for item in prov:
            if isinstance(item, dict):
                parsed = _safe_int(item.get("page_no"))
                if parsed is not None:
                    return parsed
    return _safe_int(node.get("page_no"))


def _extract_group_bbox(nodes: Sequence[dict[str, Any]]) -> Optional[dict[str, float]]:
    bounds = []
    for node in nodes:
        bbox = _extract_node_bbox(node)
        if isinstance(bbox, dict):
            bounds.append(bbox)
    if not bounds:
        return None
    return {
        "l": min(item["l"] for item in bounds),
        "r": max(item["r"] for item in bounds),
        "t": min(item["t"] for item in bounds),
        "b": max(item["b"] for item in bounds),
        "coord_origin": "TOPLEFT",
    }


def _record_fingerprint(record: dict[str, Any]) -> str:
    payload = [
        _safe_text(record.get("kind")),
        _safe_text(record.get("source")),
        "" if record.get("page_no") is None else str(record.get("page_no")),
        _safe_text(record.get("text"))[:256],
        _safe_text(_extract_node_bbox(record).get("l") if _extract_node_bbox(record) else ""),
        _safe_text(_extract_node_bbox(record).get("r") if _extract_node_bbox(record) else ""),
        _safe_text(",".join(str(item) for item in (record.get("line_indexes") or []))),
    ]
    return sha256("|".join(payload).encode("utf-8")).hexdigest()[:24]


def _line_sort_key(node: dict[str, Any]) -> tuple[int, float, float, int]:
    bbox = _extract_node_bbox(node) or {}
    return (
        int(_safe_int(node.get("page_no") or 0) or 0),
        _safe_float(bbox.get("t"), 0.0),
        _safe_float(bbox.get("l"), 0.0),
        int(_safe_int(node.get("line_no") or node.get("index") or 0)),
    )


def _normalize_records(records: Sequence[dict[str, Any]], fallback_source: str) -> list[dict[str, Any]]:
    out = []
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            continue
        text = _safe_text(raw.get("text")).strip()
        if not text:
            continue
        out.append(
            {
                "text": text,
                "label": _safe_text(raw.get("label") or fallback_source).lower(),
                "page_no": _first_page_no(raw),
                "prov": raw.get("prov"),
                "source": _safe_text(raw.get("source") or fallback_source),
                "index": int(_safe_int(raw.get("index")) or index),
                "raw": raw,
            }
        )
    return out


def _collect_nodes_from_document_json(document_json: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    texts = []
    tables = []
    formulas = []
    if not isinstance(document_json, dict):
        return texts, tables, formulas

    chunks = document_json.get("chunks")
    if isinstance(chunks, list) and chunks:
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            document = chunk.get("document") if isinstance(chunk.get("document"), dict) else {}
            if not isinstance(document, dict):
                continue
            page_offset = _safe_int((chunk.get("page_range") or [None, None])[0])
            raw_pages = document.get("pages")
            if isinstance(raw_pages, dict):
                chunk_page_count = len(raw_pages)
            elif isinstance(raw_pages, list):
                chunk_page_count = len(raw_pages)
            else:
                chunk_page_count = None
            for key, kind, target in (("texts", "text", texts), ("tables", "table", tables), ("formulas", "formula", formulas)):
                nodes = document.get(key)
                if not isinstance(nodes, list):
                    continue
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    node_copy = deepcopy(node)
                    page_no = _normalize_chunk_page_no(
                        _first_page_no(node_copy),
                        page_offset,
                        chunk_page_count,
                    )
                    payload = {
                        "text": _safe_text(node_copy.get("text")),
                        "label": kind,
                        "page_no": page_no,
                        "prov": deepcopy(node_copy.get("prov")),
                        "source": "document_json",
                        "index": len(target),
                        "raw": node_copy,
                    }
                    if isinstance(payload["prov"], list) and payload["prov"]:
                        normalized_prov = []
                        for item in payload["prov"]:
                            if not isinstance(item, dict):
                                continue
                            item["page_no"] = page_no
                            normalized_prov.append(item)
                        if normalized_prov:
                            payload["prov"] = normalized_prov
                    target.append(payload)
        return texts, tables, formulas

    for key, kind, target in (("texts", "text", texts), ("tables", "table", tables), ("formulas", "formula", formulas)):
        nodes = document_json.get(key)
        if not isinstance(nodes, list):
            continue
        target.extend(_normalize_records(nodes, "document_json"))
    return texts, tables, formulas


def _collect_nodes_from_source_records(
    source_evidence: Optional[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    texts = []
    tables = []
    formulas = []

    if not isinstance(source_evidence, dict):
        return texts, tables, formulas

    docs = source_evidence.get("document_json")
    if isinstance(docs, dict):
        a, b, c = _collect_nodes_from_document_json(docs)
        texts.extend(a)
        tables.extend(b)
        formulas.extend(c)

    for key in ("records", "text_nodes", "nodes"):
        values = source_evidence.get(key)
        if not isinstance(values, list):
            continue
        for node in values:
            if not isinstance(node, dict):
                continue
            label = _safe_text(node.get("label") or "text").lower()
            text = _safe_text(node.get("text")).strip()
            if not text:
                continue
            entry = {
                "text": text,
                "label": "table" if label == "table" else ("formula" if label == "formula" else "text"),
                "page_no": _first_page_no(node),
                "prov": node.get("prov"),
                "source": "source_records",
                "index": len(texts) + len(tables) + len(formulas),
                "raw": dict(node),
            }
            if entry["label"] == "table":
                tables.append(entry)
            elif entry["label"] == "formula":
                formulas.append(entry)
            else:
                texts.append(entry)
    return texts, tables, formulas


def _collect_source_nodes(
    source_evidence: Optional[dict[str, Any]],
    document_json: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    texts = []
    tables = []
    formulas = []
    if isinstance(document_json, dict):
        text_nodes, table_nodes, formula_nodes = _collect_nodes_from_document_json(document_json)
        texts.extend(text_nodes)
        tables.extend(table_nodes)
        formulas.extend(formula_nodes)
    if isinstance(source_evidence, dict):
        text_nodes, table_nodes, formula_nodes = _collect_nodes_from_source_records(source_evidence)
        texts.extend(text_nodes)
        tables.extend(table_nodes)
        formulas.extend(formula_nodes)

    all_nodes = texts + tables + formulas
    for index, node in enumerate(all_nodes):
        node["index"] = index
    return texts, tables, formulas


def _group_words_to_lines(words: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not words:
        return []
    heights = [_safe_float(word.get("size", 0.0)) for word in words if _safe_float(word.get("size", 0.0)) > 0.0]
    line_tol = max(2.0, (sorted(heights)[len(heights) // 2] * 0.85) if heights else 6.0)
    rows: list[list[dict[str, Any]]] = []
    words = sorted(words, key=lambda item: (_safe_float(item.get("top")), _safe_float(item.get("x0"))))
    current: list[dict[str, Any]] = []
    anchor_top: Optional[float] = None
    for word in words:
        if not _safe_text(word.get("text")).strip():
            continue
        top = _safe_float(word.get("top"))
        if anchor_top is None or abs(top - anchor_top) <= line_tol:
            current.append(word)
            if anchor_top is None:
                anchor_top = top
            continue
        if current:
            rows.append(current)
        current = [word]
        anchor_top = top
    if current:
        rows.append(current)
    return rows


def _group_chars_to_words(chars: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not chars:
        return []
    chars = [item for item in chars if _safe_text(item.get("text")).strip()]
    if not chars:
        return []
    chars = sorted(chars, key=lambda item: (_safe_float(item.get("x0")), _safe_float(item.get("top"))))
    sizes = [_safe_float(item.get("size", 0.0)) for item in chars if _safe_float(item.get("size", 0.0)) > 0.0]
    gap_tol = max(1.5, (sorted(sizes)[len(sizes) // 2] * 0.45) if sizes else 4.0)
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in chars:
        if not current:
            current.append(item)
            continue
        prev = current[-1]
        prev_x1 = _safe_float(prev.get("x1"), _safe_float(prev.get("x0")))
        curr_x0 = _safe_float(item.get("x0"), prev_x1)
        if curr_x0 - prev_x1 > gap_tol:
            out.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        out.append(current)
    return out


def _extract_lines_from_pdfplumber_page(page: Any) -> list[dict[str, Any]]:
    width = _safe_float(getattr(page, "width", 0.0), 0.0)
    height = _safe_float(getattr(page, "height", 0.0), 0.0)
    lines = []
    text_lines = []
    try:
        text_lines = list(page.extract_text_lines() or [])
    except Exception:
        text_lines = []

    if text_lines:
        for item in text_lines:
            text = _safe_text(item.get("text")).strip()
            if not text:
                continue
            line_chars = item.get("chars") or []
            if not isinstance(line_chars, list):
                line_chars = []
            words = _group_chars_to_words(line_chars)
            word_records = []
            for word in words:
                if not word:
                    continue
                word_records.append(
                    {
                        "text": "".join(_safe_text(ch.get("text")) for ch in word).strip(),
                        "x0": _safe_float(word[0].get("x0")),
                        "x1": _safe_float(word[-1].get("x1")),
                        "top": _safe_float(word[0].get("top")),
                        "bottom": _safe_float(word[-1].get("bottom")),
                        "size": _safe_float(word[0].get("size", 10.0), 10.0),
                        "fontname": _safe_text(word[0].get("fontname")),
                    }
                )
            l = _safe_float(item.get("x0"), 0.0)
            r = _safe_float(item.get("x1"), width)
            t = _safe_float(item.get("top"), 0.0)
            b = _safe_float(item.get("bottom"), t + 10.0)
            fonts: list[str] = []
            for ch in line_chars:
                font = _safe_text(ch.get("fontname"))
                if font and font not in fonts:
                    fonts.append(font)
            if not fonts and line_chars:
                for ch in line_chars:
                    font = _safe_text(ch.get("fontname"))
                    if font and font not in fonts:
                        fonts.append(font)
            if l >= r:
                if word_records:
                    l = min(_safe_float(word.get("x0") or width) for word in word_records)
                    r = max(_safe_float(word.get("x1") or 0.0) for word in word_records)
                else:
                    l = 0.0
                    r = max(width, 1.0)
            span_records = []
            for ch in line_chars:
                span_records.append(
                    {
                        "text": _safe_text(ch.get("text")),
                        "x0": _safe_float(ch.get("x0")),
                        "x1": _safe_float(ch.get("x1")),
                        "top": _safe_float(ch.get("top")),
                        "bottom": _safe_float(ch.get("bottom")),
                        "size": _safe_float(ch.get("size")),
                        "fontname": _safe_text(ch.get("fontname")),
                    }
                )
            lines.append(
                {
                    "text": text,
                    "bbox": {"l": l, "r": r, "t": t, "b": b, "coord_origin": "TOPLEFT"},
                    "fonts": fonts or ["unknown"],
                    "spans": span_records,
                    "words": word_records,
                    "width": max(width, 1.0),
                    "height": max(height, 1.0),
                    "words_width": max(1.0, max(1.0, r - l)),
                    "words_height": max(1.0, b - t),
                }
            )
        if lines:
            return lines

    raw_words = []
    for tol in (2, 3, 4, 5):
        try:
            raw_words = page.extract_words(x_tolerance=tol, y_tolerance=2, keep_blank_chars=False, use_text_flow=False, extra_attrs=["fontname", "size"])
        except TypeError:
            raw_words = page.extract_words(x_tolerance=tol, y_tolerance=2, keep_blank_chars=False, use_text_flow=False)
        except Exception:
            raw_words = []
        if not raw_words:
            continue
        filtered = [_safe_text(word.get("text")).strip() for word in raw_words if _safe_text(word.get("text")).strip()]
        if not filtered:
            continue
        if len(filtered) >= 5:
            raw_words = raw_words
            break

    if raw_words:
        for row in _group_words_to_lines(raw_words):
            row = sorted(row, key=lambda item: _safe_float(item.get("x0")))
            if not row:
                continue
            text = " ".join(_safe_text(item.get("text")).strip() for item in row).strip()
            if not text:
                continue
            l = min(_safe_float(item.get("x0")) for item in row)
            r = max(_safe_float(item.get("x1")) for item in row)
            t = min(_safe_float(item.get("top")) for item in row)
            b = max(_safe_float(item.get("bottom")) for item in row)
            fonts = []
            for item in row:
                font = _safe_text(item.get("fontname"))
                if font and font not in fonts:
                    fonts.append(font)
            spans = [
                {
                    "text": _safe_text(item.get("text")),
                    "x0": _safe_float(item.get("x0")),
                    "x1": _safe_float(item.get("x1")),
                    "top": _safe_float(item.get("top")),
                    "bottom": _safe_float(item.get("bottom")),
                    "size": _safe_float(item.get("size")),
                    "fontname": _safe_text(item.get("fontname")),
                }
                for item in row
            ]
            lines.append(
                {
                    "text": text,
                    "bbox": {"l": l, "r": r, "t": t, "b": b, "coord_origin": "TOPLEFT"},
                    "fonts": fonts,
                    "spans": spans,
                    "words": [
                        {
                            "text": _safe_text(item.get("text")).strip(),
                            "x0": _safe_float(item.get("x0")),
                            "x1": _safe_float(item.get("x1")),
                            "top": _safe_float(item.get("top")),
                            "bottom": _safe_float(item.get("bottom")),
                            "size": _safe_float(item.get("size")),
                            "fontname": _safe_text(item.get("fontname")),
                        }
                        for item in row
                        if _safe_text(item.get("text")).strip()
                    ],
                    "width": max(width, 1.0),
                    "height": max(height, 1.0),
                    "words_width": max(1.0, r - l),
                    "words_height": max(1.0, b - t),
                }
            )
        if lines:
            return lines

    try:
        chars = list(page.chars or [])
    except Exception:
        chars = []

    if chars:
        chars = [item for item in chars if _safe_text(item.get("text")).strip()]
        chars = sorted(chars, key=lambda item: (_safe_float(item.get("top")), _safe_float(item.get("x0"))))
        heights = [_safe_float(item.get("size", 0.0)) for item in chars if _safe_float(item.get("size", 0.0)) > 0.0]
        row_tol = max(2.0, (sorted(heights)[len(heights) // 2] * 0.85) if heights else 6.0)
        row: list[dict[str, Any]] = []
        anchor = None
        for char in chars:
            top = _safe_float(char.get("top"))
            if anchor is None or abs(top - anchor) <= row_tol:
                row.append(char)
                if anchor is None:
                    anchor = top
                continue
            if row:
                lines.extend(_lines_from_char_row(row, width, height))
            row = [char]
            anchor = top
        if row:
            lines.extend(_lines_from_char_row(row, width, height))
        if lines:
            return lines

    fallback_text = _safe_text(page.extract_text() or "")
    for idx, raw_line in enumerate(fallback_text.splitlines()):
        text = raw_line.strip()
        if not text:
            continue
        lines.append(
            {
                "text": text,
                "bbox": {"l": 0.0, "r": width, "t": idx * 12.0, "b": idx * 12.0 + 10.0, "coord_origin": "TOPLEFT"},
                "fonts": ["unknown"],
                "spans": (),
                "words": [],
                "width": width,
                "height": height,
                "words_width": max(1.0, width),
                "words_height": 10.0,
            }
        )
    return lines


def _lines_from_char_row(chars: list[dict[str, Any]], page_width: float, page_height: float) -> list[dict[str, Any]]:
    if not chars:
        return []
    chars = sorted(chars, key=lambda item: _safe_float(item.get("x0")))
    groups = _group_chars_to_words(chars)
    if not groups:
        return []
    text_parts = ["".join(_safe_text(ch.get("text")) for ch in group).strip() for group in groups]
    text = " ".join(part for part in text_parts if part).strip()
    if not text:
        return []
    l = min(_safe_float(item.get("x0")) for item in chars)
    r = max(_safe_float(item.get("x1")) for item in chars)
    t = min(_safe_float(item.get("top")) for item in chars)
    b = max(_safe_float(item.get("bottom")) for item in chars)
    fonts: list[str] = []
    for item in chars:
        font = _safe_text(item.get("fontname"))
        if font and font not in fonts:
            fonts.append(font)
    spans = [
        {
            "text": _safe_text(item.get("text")),
            "x0": _safe_float(item.get("x0")),
            "x1": _safe_float(item.get("x1")),
            "top": _safe_float(item.get("top")),
            "bottom": _safe_float(item.get("bottom")),
            "size": _safe_float(item.get("size")),
            "fontname": _safe_text(item.get("fontname")),
        }
        for item in chars
    ]
    return [
        {
            "text": text,
            "bbox": {"l": l, "r": r, "t": t, "b": b, "coord_origin": "TOPLEFT"},
            "fonts": fonts,
            "spans": spans,
            "words": [
                {
                    "text": _safe_text(item.get("text")).strip(),
                    "x0": _safe_float(item.get("x0")),
                    "x1": _safe_float(item.get("x1")),
                    "top": _safe_float(item.get("top")),
                    "bottom": _safe_float(item.get("bottom")),
                    "size": _safe_float(item.get("size")),
                    "fontname": _safe_text(item.get("fontname")),
                }
                for item in chars
                if _safe_text(item.get("text")).strip()
            ],
            "width": max(page_width, 1.0),
            "height": max(page_height, 1.0),
            "words_width": max(1.0, r - l),
            "words_height": max(1.0, b - t),
        }
    ]


def _extract_pdf_lines(path: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    try:
        import pdfplumber  # type: ignore[import]
    except Exception:  # pragma: no cover
        raise RuntimeError("pdfplumber_not_available")

    try:
        with pdfplumber.open(path) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                page_width = _safe_float(getattr(page, "width", 612.0), 612.0)
                page_height = _safe_float(getattr(page, "height", 792.0), 792.0)
                page_lines = _extract_lines_from_pdfplumber_page(page)
                for line_no, item in enumerate(page_lines):
                    text = _safe_text(item.get("text")).strip()
                    if not text:
                        continue
                    record = {
                        "text": text,
                        "label": "text",
                        "page_no": page_no,
                        "index": len(lines),
                        "line_no": line_no,
                        "bbox": item.get("bbox"),
                        "width": page_width,
                        "height": page_height,
                        "fonts": item.get("fonts", ("unknown",)),
                        "prov": [{"page_no": page_no, "bbox": item.get("bbox"), "coord_origin": "TOPLEFT"}],
                        "source": "pdf_lines",
                        "spans": item.get("spans", ()),
                        "words": item.get("words", ()),
                        "words_width": item.get("words_width", page_width),
                        "words_height": item.get("words_height", 12.0),
                    }
                    lines.append(record)
            return lines
    except Exception:
        raise RuntimeError("pdf_line_extraction_failed")


def _document_text_health(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "status": KIND_UNKNOWN,
        "reason": "input_file_not_found",
        "page_count": 0,
        "page_no_continuous": False,
        "pages": [],
    }
    if not path.exists() or not path.is_file():
        return result
    with path.open("rb") as handle:
        magic = handle.read(16)
    if b"%PDF-" not in magic:
        result["reason"] = "pdf_magic_missing"
        return result

    text_rows: list[tuple[int, str]] = []
    try:
        import pdfplumber  # type: ignore[import]

        with pdfplumber.open(path) as pdf:
            result["available"] = True
            result["reason"] = None
            result["page_count"] = len(pdf.pages)
            for page_no, page in enumerate(pdf.pages, start=1):
                text = _safe_text(page.extract_text() or "")
                text_rows.append((page_no, text))
                images = len(getattr(page, "images", ()) or [])
                reasons: list[str] = []
                if not text.strip():
                    reasons.append("image_only" if images else "empty_text")
                if not reasons and len(text.strip()) < 16:
                    reasons.append("short_text")
                if UNKNOWN_TOKEN_RE.search(text):
                    reasons.append("unknown_tokens")
                if "\ufffd" in text:
                    reasons.append("replacement_characters")
                unreadable_reasons = {
                    "image_only",
                    "empty_text",
                    "short_text",
                }
                result["pages"].append(
                    {
                        "page_no": page_no,
                        # CID/replacement markers make a candidate formula
                        # ambiguous, but do not make an otherwise readable
                        # text layer unavailable.  Keep that uncertainty in
                        # ``reasons`` so formula proof can fail closed without
                        # globally rejecting tables/code/algorithms on the
                        # same readable page.
                        "healthy": not any(
                            reason in unreadable_reasons for reason in reasons
                        ),
                        "text_chars": len(text),
                        "images": images,
                        "reasons": reasons,
                    }
                )
    except Exception:
        result["available"] = False
        result["reason"] = "text_layer_unavailable"
        result["status"] = KIND_UNKNOWN
        return result

    if result["page_count"] <= 0:
        result["status"] = KIND_UNKNOWN
        result["reason"] = "pdf_has_no_pages"
        result["page_no_continuous"] = False
        return result

    present = {page_no for page_no, _ in text_rows}
    expected = set(range(1, int(result["page_count"]) + 1))
    result["page_no_continuous"] = present == expected
    if not result["page_no_continuous"]:
        result["status"] = KIND_UNKNOWN
        result["reason"] = "page_range_non_continuous"
        return result

    for page in result["pages"]:
        if page.get("healthy"):
            continue
        if any(reason in {"image_only", "empty_text", "short_text"} for reason in page.get("reasons", ())):
            result["status"] = KIND_UNKNOWN
            reasons = page.get("reasons") or ["unknown"]
            result["reason"] = str(reasons[0])
            return result

    for page in result["pages"]:
        if any(reason in {"unknown_tokens", "replacement_characters"} for reason in page.get("reasons", ())):
            if not result["reason"]:
                result["reason"] = "unknown_tokens"

    result["status"] = KIND_HEALTHY
    return result


def _build_base_counts() -> dict[str, Any]:
    return {
        "high_confidence": 0,
        "ambiguous": 0,
        "records": [],
    }


def _add_to_counts(counts: dict[str, dict[str, Any]], records: Sequence[dict[str, Any]]) -> None:
    for record in records:
        kind = _safe_text(record.get("kind"))
        bucket = counts.setdefault(kind, _build_base_counts())
        if _safe_text(record.get("confidence")) == "high":
            bucket["high_confidence"] += 1
        else:
            bucket["ambiguous"] += 1
        bucket["records"].append(
            {
                "text": record.get("text"),
                "page_no": record.get("page_no"),
                "confidence": record.get("confidence"),
                "source": record.get("source"),
                "bbox": record.get("bbox"),
                "fingerprint": record.get("fingerprint"),
                "line_indexes": list(record.get("line_indexes") or []),
            }
        )


def _dedupe(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for record in records:
        if not isinstance(record, dict):
            continue
        fingerprint = _safe_text(record.get("fingerprint"))
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(record)
    return out


def _is_algorithm_heading_line(text: str) -> bool:
    match = ALGORITHM_HEADING_RE.match(_safe_text(text))
    if not match:
        return False
    tail = _safe_text(text[match.end() :]).strip()
    if not tail:
        return True
    if ALGORITHM_PROSE_TAIL_RE.search(tail):
        return False
    # cap title text length to avoid whole-paragraph captures.
    if len(tail) > 96:
        return False
    return True


def _is_input_or_label_only_line(text: str) -> bool:
    return bool(INPUT_LABEL_ONLY_RE.match(text))


def _classify_algorithm_records(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(nodes, key=_line_sort_key)
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(ordered):
        node = ordered[i]
        heading_text = _safe_text(node.get("text")).strip()
        if not _is_algorithm_heading_line(heading_text):
            i += 1
            continue
        page_no = node.get("page_no")
        if not isinstance(page_no, int):
            i += 1
            continue

        block = [node]
        j = i + 1
        while j < len(ordered):
            candidate = ordered[j]
            if candidate.get("page_no") != page_no:
                break
            candidate_text = _safe_text(candidate.get("text")).strip()
            if not candidate_text:
                break
            if _is_algorithm_heading_line(candidate_text):
                break
            if _is_input_or_label_only_line(candidate_text):
                break
            block.append(candidate)
            j += 1

        body_lines = [str(item.get("text", "")).strip() for item in block[1:]]
        if not body_lines:
            i = j
            continue

        numbered = 0
        control = 0
        requires = 0
        for line in body_lines:
            if ALGORITHM_NUMBERED_LINE_RE.search(line):
                numbered += 1
            if ALGORITHM_CONTROL_RE.search(line):
                control += 1
            if ALGORITHM_REQUIRE_KEYWORD_RE.search(line):
                requires += 1

        if not body_lines:
            i = j
            continue
        if requires == 0 and (numbered + control) < 3:
            i = j
            continue

        if requires > 0 and len(body_lines) < 2:
            i = j
            continue

        rec = {
            "kind": "algorithm",
            "confidence": "high",
            "text": "\n".join(_safe_text(item.get("text")).strip() for item in block),
            "page_no": page_no,
            "line_indexes": [int(item.get("index") or 0) for item in block],
            "indexes": [int(item.get("index") or 0) for item in block],
            "bbox": _extract_group_bbox(block),
            "nodes": block,
            "source": "pdf_lines",
        }
        rec["fingerprint"] = _record_fingerprint(rec)
        out.append(rec)
        i = j
    return out


def _line_code_signals(node: dict[str, Any]) -> tuple[int, int, int]:
    text = _safe_text(node.get("text"))
    assignment = 1 if ASSIGNMENT_RE.search(text) else 0
    control = 1 if CONTROL_KEYWORD_RE.search(text) else 0
    mono = 0
    fonts = node.get("fonts")
    if isinstance(fonts, str):
        fonts = [fonts]
    if isinstance(fonts, list):
        for item in fonts:
            if CODE_MONO_FONT_RE.search(_safe_text(item)):
                mono = 1
                break
    if mono == 0 and _safe_text(node.get("text")).startswith("    "):
        mono = 1
    return assignment, control, mono


def _collect_bert_code_block(
    ordered: Sequence[dict[str, Any]],
    start_pos: int,
    banned_indexes: set[int],
) -> tuple[list[dict[str, Any]], int]:
    if start_pos >= len(ordered):
        return [], start_pos

    block: list[dict[str, Any]] = []
    input_count = 0
    label_count = 0
    marker_count = 0
    page_no = ordered[start_pos].get("page_no")
    start_index = int(ordered[start_pos].get("index") or start_pos)
    window_end = min(len(ordered), start_pos + 16)
    seen_non_consecutive = 0

    j = start_pos
    while j < window_end:
        node = ordered[j]
        if node.get("page_no") != page_no:
            break
        idx = int(node.get("index") or j)
        if idx in banned_indexes:
            j += 1
            continue
        text = _safe_text(node.get("text")).strip()
        if not text or _is_algorithm_heading_line(text):
            j += 1
            continue
        if not INPUT_LABEL_ASSIGN_RE.match(text):
            seen_non_consecutive += 1
            if seen_non_consecutive >= 4:
                break
            j += 1
            continue
        name = INPUT_LABEL_ASSIGN_RE.match(text).group(1).lower()
        if name == "input":
            input_count += 1
        elif name == "label":
            label_count += 1
        block.append(node)
        if INPUT_LABEL_MARKER_RE.search(text):
            marker_count += 1
        j += 1
        seen_non_consecutive = 0

    if (
        block
        and any(item.get("index") == start_index for item in block)
        and len(block) >= 2
        and input_count >= 2
        and label_count >= 2
        and marker_count >= 2
    ):
        return block, j
    return [], start_pos


def _collect_generic_code_block(
    ordered: Sequence[dict[str, Any]],
    start_pos: int,
    banned_indexes: set[int],
) -> tuple[list[dict[str, Any]], int]:
    base_node = ordered[start_pos]
    page_no = base_node.get("page_no")
    block = [base_node]
    j = start_pos + 1
    while j < len(ordered):
        node = ordered[j]
        if node.get("page_no") != page_no:
            break
        idx = int(node.get("index") or j)
        if idx in banned_indexes:
            break
        text = _safe_text(node.get("text")).strip()
        if not text or _is_algorithm_heading_line(text):
            break
        if _is_input_or_label_only_line(text):
            break
        assignment, control, mono = _line_code_signals(node)
        if assignment + control + mono == 0:
            break
        block.append(node)
        j += 1
        if len(block) >= 10:
            break
    return block, j


def _classify_code_records(nodes: Sequence[dict[str, Any]], banned_indexes: set[int]) -> list[dict[str, Any]]:
    ordered = sorted(nodes, key=_line_sort_key)
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(ordered):
        node = ordered[i]
        idx = int(node.get("index") or i)
        if idx in banned_indexes:
            i += 1
            continue

        bert_block, j = _collect_bert_code_block(ordered, i, banned_indexes)
        if bert_block:
            indexes = [int(item.get("index") or 0) for item in bert_block]
            if any(index in used for index in indexes):
                i = j
                continue
            used.update(indexes)
            rec = {
                "kind": "code",
                "confidence": "high",
                "text": "\n".join(_safe_text(item.get("text")).strip() for item in bert_block),
                "page_no": node.get("page_no"),
                "line_indexes": indexes,
                "indexes": indexes,
                "bbox": _extract_group_bbox(bert_block),
                "nodes": bert_block,
                "source": "pdf_lines",
            }
            rec["fingerprint"] = _record_fingerprint(rec)
            out.append(rec)
            i = j
            continue

        text = _safe_text(node.get("text")).strip()
        if not text or _is_algorithm_heading_line(text) or INPUT_LABEL_ASSIGN_RE.search(text):
            i += 1
            continue

        assignment, control, mono = _line_code_signals(node)
        if assignment + control + mono < 2:
            i += 1
            continue

        block, j = _collect_generic_code_block(ordered, i, banned_indexes)
        block_indexes = [int(item.get("index") or 0) for item in block]
        if len(block) < 4:
            i = j
            continue
        if any(index in used for index in block_indexes):
            i = j
            continue

        signals = [_line_code_signals(item) for item in block]
        assignment_count = sum(a for a, _c, _m in signals)
        control_count = sum(c for _a, c, _m in signals)
        mono_count = sum(m for _a, _c, m in signals)
        code_like_lines = sum(1 for a, c, m in signals if a + c + m > 0)

        # conservative threshold: explicit code-like structure is required.
        if code_like_lines < 3:
            i = j
            continue
        if assignment_count < 3 and control_count < 2:
            i = j
            continue
        if assignment_count < 2 and control_count < 2 and mono_count < 2:
            i = j
            continue

        confidence = "high"
        if assignment_count >= 3:
            confidence = "high"
        elif control_count >= 2:
            confidence = "high"
        elif mono_count >= 2:
            confidence = "high"
        else:
            i = j
            continue

        rec = {
            "kind": "code",
            "confidence": confidence,
            "text": "\n".join(_safe_text(item.get("text")).strip() for item in block),
            "page_no": node.get("page_no"),
            "line_indexes": block_indexes,
            "indexes": block_indexes,
            "bbox": _extract_group_bbox(block),
            "nodes": block,
            "source": "pdf_lines",
        }
        rec["fingerprint"] = _record_fingerprint(rec)
        out.append(rec)
        used.update(block_indexes)
        i = j
    return out


def _line_columns_from_words(node: dict[str, Any]) -> list[float]:
    words = node.get("words")
    if isinstance(words, list) and words:
        words = [item for item in words if _safe_text(item.get("text")).strip()]
        if len(words) >= 2:
            words = sorted(words, key=lambda item: _safe_float(item.get("x0")))
            groups: list[list[dict[str, Any]]] = []
            prev = words[0]
            groups.append([prev])
            for item in words[1:]:
                gap = _safe_float(item.get("x0")) - _safe_float(prev.get("x1"))
                prev_size = max(_safe_float(prev.get("size", 8.0), 8.0), 6.0)
                if gap > max(1.8, prev_size * 0.55):
                    groups.append([item])
                else:
                    groups[-1].append(item)
                prev = item
            if len(groups) < 2:
                raw = [part for part in re.split(r"\s{2,}", _safe_text(node.get("text")).strip()) if part]
                if len(raw) >= 2:
                    return [float(i) for i in range(len(raw))]
                return []
            return [float(_safe_float(group[0].get("x0"))) for group in groups if group]

    fallback = [part for part in re.split(r"\s{2,}", _safe_text(node.get("text")).strip()) if part]
    if len(fallback) >= 2:
        return [float(i) for i, _ in enumerate(fallback)]
    return []


def _line_is_table_caption(text: str) -> bool:
    if TABLE_CAPTION_NOISE_RE.match(text):
        return False
    if TABLE_CAPTION_RE.match(text):
        return True
    for marker_match in TABLE_CN_PUNCT_SCAN_RE.finditer(text):
        _marker = marker_match.group(1)
        title = marker_match.group(2).strip()
        if not title:
            continue
        if not title.startswith(("(", "（")):
            if _marker != "(" and title:
                continue
        if any(title.startswith(prefix) for prefix in TABLE_CN_FORBIDDEN_PREFIXES):
            continue
        return True
    return False


def _classify_table_records(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(nodes, key=_line_sort_key)
    out: list[dict[str, Any]] = []
    for pos, node in enumerate(ordered):
        text = _safe_text(node.get("text")).strip()
        if not text:
            continue
        if not _line_is_table_caption(text):
            continue

        page_no = node.get("page_no")
        caption_index = int(node.get("index") or pos)
        rec = {
            "kind": "table",
            "confidence": "high",
            "text": text,
            "page_no": page_no,
            "line_indexes": [caption_index],
            "indexes": [caption_index],
            "bbox": _extract_node_bbox(node),
            "nodes": [node],
            "source": "pdf_lines",
        }
        rec["fingerprint"] = _record_fingerprint(rec)
        out.append(rec)
    return out


def _remove_trailing_eq_number(text: str) -> str:
    return _safe_text(EQ_NUMBER_RE.sub("", _safe_text(text)).rstrip())


def _formula_math_density(text: str) -> float:
    compact = re.sub(r"\s+", "", _safe_text(text))
    if not compact:
        return 0.0
    math_span = re.findall(r"[+\-*/=<>≤≥≠≈∈∑∏∫√×÷→←⇒⇔\._\^\\\(\)\{\}\d\[\]α-ωΑ-Ω]", compact)
    return float(len(math_span)) / float(len(compact))


def _formula_operator_classes(text: str) -> set[str]:
    classes: set[str] = set()
    compact = _safe_text(text)
    for match in MATH_OPERATOR_RE.finditer(compact):
        op = match.group(0)
        if op == "-":
            before = compact[match.start() - 1] if match.start() > 0 else ""
            after = compact[match.end()] if match.end() < len(compact) else ""
            if before and after and before.isalnum() and after.isalnum():
                continue
        if op in {"+", "-"}:
            classes.add("arith")
        elif op in {"=", "≈", "≠"}:
            classes.add("equal")
        elif op in {"<", ">", "≤", "≥"}:
            classes.add("relation")
        elif op in {"∈", "∑", "∏", "∂", "∇", "∫"}:
            classes.add("calculus")
        elif op in {"×", "÷"}:
            classes.add("scale")
        else:
            classes.add("misc")
    return classes


def _formula_text_signature(node: dict[str, Any]) -> tuple[bool, bool, bool]:
    text = _safe_text(node.get("text"))
    if not text:
        return False, False, False
    operator_classes = _formula_operator_classes(text)
    has_operator = bool(operator_classes)
    has_substantive_operator = bool(operator_classes - {"arith"})
    has_command = bool(MATH_COMMAND_RE.search(text))
    has_eq_trailing = bool(EQ_NUMBER_RE.search(text))
    has_formula_char = bool(re.search(r"[=<>≤≥≈∈∑∏∫√×÷\\]", text))
    has_math_font = _has_math_font(node) >= 0.40
    has_greek = bool(MATH_GREEK_RE.search(text))
    has_subsup = bool(MATH_SUBSUP_RE.search(text))
    has_hyperparam = bool(FORMULA_HYPERPARAM_RE.search(text))
    prose_count = len(FORMULA_NOISE_WORD_RE.findall(text))
    word_count = len(re.findall(r"[A-Za-z]{2,}", text))
    if re.search(r"^\w+\s*:", text):
        return False, False, False
    if has_hyperparam and not has_eq_trailing and not has_math_font:
        return False, False, False

    core_signal = has_operator or has_command or has_formula_char or has_math_font
    trimmed = _remove_trailing_eq_number(text)
    math_density = _formula_math_density(trimmed)
    operator_count = len(operator_classes)
    strong_signals = 0
    if has_command:
        strong_signals += 1
    if has_formula_char:
        strong_signals += 1
    if operator_count >= 2:
        strong_signals += 1
    if has_math_font:
        strong_signals += 1
    if has_greek:
        strong_signals += 1
    if has_subsup:
        strong_signals += 1

    if has_eq_trailing:
        if math_density < 0.06 and not has_command and not has_math_font and not has_substantive_operator:
            return False, False, False
        if len(trimmed) > 180 and prose_count >= 2:
            return False, False, False
        if (
            not has_substantive_operator
            and not has_command
            and not has_math_font
            and not has_greek
            and not has_subsup
            and strong_signals < 2
        ):
            return False, False, False
        if not core_signal and prose_count >= 1:
            return False, False, False
        return True, True, False

    if not core_signal:
        return False, False, False

    if has_math_font and (word_count <= 2 or has_greek or has_subsup):
        # math-font heavy formula fragments can be treated as plausible formulas
        pass
    elif strong_signals < 2:
        return False, False, False

    if prose_count >= 2 and word_count > 6:
        return False, False, False
    if word_count > 20:
        return False, False, False
    if math_density < 0.14 and strong_signals < 3:
        return False, False, False

    return True, False, strong_signals >= 2


def _has_math_font(node: dict[str, Any]) -> float:
    fonts = node.get("fonts")
    if isinstance(fonts, str):
        fonts = [fonts]
    elif isinstance(fonts, tuple):
        fonts = list(fonts)
    elif not isinstance(fonts, list):
        fonts = []
    spans = node.get("spans")
    if isinstance(spans, (list, tuple)):
        fonts = list(fonts)
        for span in spans:
            if not isinstance(span, dict):
                continue
            font = _safe_text(span.get("fontname"))
            if font:
                fonts.append(font)
    if not isinstance(fonts, list) or not fonts:
        return 0.0
    count = sum(1 for font in fonts if MATH_FONT_RE.search(_safe_text(font)))
    return float(count) / float(len(fonts))


def _line_is_display_like(
    node: dict[str, Any],
    prev_line: Optional[dict[str, Any]],
    next_line: Optional[dict[str, Any]],
    *,
    allow_column_formula: bool = False,
) -> bool:
    bbox = _extract_node_bbox(node)
    if not isinstance(bbox, dict):
        return False
    width = _safe_float(node.get("width") or node.get("page_width") or 612.0, 612.0)
    if width <= 0.0:
        return False
    line_width = max(1.0, bbox["r"] - bbox["l"])
    line_center = (bbox["l"] + bbox["r"]) * 0.5
    if line_width < width * 0.04 or line_width > width * 0.95:
        return False

    centered = abs(line_center - (width * 0.5)) <= max(10.0, width * 0.12)

    height = max(1.0, bbox["b"] - bbox["t"])
    prev_gap = 1e9
    if isinstance(prev_line, dict) and prev_line.get("page_no") == node.get("page_no"):
        prev_bbox = _extract_node_bbox(prev_line) or {}
        prev_gap = bbox["t"] - _safe_float(prev_bbox.get("b"), bbox["t"])
    next_gap = 1e9
    if isinstance(next_line, dict) and next_line.get("page_no") == node.get("page_no"):
        next_bbox = _extract_node_bbox(next_line) or {}
        next_gap = _safe_float(next_bbox.get("t"), bbox["b"]) - bbox["b"]
    isolated = (prev_gap > max(10.0, height * 1.4)) and (next_gap > max(10.0, height * 1.4))
    if centered or isolated:
        return True

    if not allow_column_formula:
        return False

    column_center = line_center / width
    if column_center < 0.35 or column_center > 0.65:
        if line_width > width * 0.14 and line_width < width * 0.82:
            return True
    return False


def _classify_formula_records(
    lines: Sequence[dict[str, Any]],
    *,
    unknown_formula_candidates: Optional[dict[str, bool]] = None,
) -> list[dict[str, Any]]:
    ordered = sorted(lines, key=_line_sort_key)
    out: list[dict[str, Any]] = []
    for pos, node in enumerate(ordered):
        text = _safe_text(node.get("text")).strip()
        if not text or len(text) > 5000:
            continue
        if URL_RE.search(text):
            continue

        prev_node = ordered[pos - 1] if pos > 0 else None
        next_node = ordered[pos + 1] if pos + 1 < len(ordered) else None
        text_chars = len(text)
        if text_chars > 512:
            continue

        is_formula_candidate, requires_eq, is_ambiguous = _formula_text_signature(node)
        if not is_formula_candidate:
            continue
        if not requires_eq and not is_ambiguous:
            continue
        if not requires_eq and text_chars >= 220:
            continue

        has_eq_trailing = bool(EQ_NUMBER_RE.search(text))
        formula_operator_classes = _formula_operator_classes(text)
        has_substantive_operator = bool(formula_operator_classes - {"arith"})
        has_math_font = _has_math_font(node) >= 0.40
        allow_column_formula = bool(
            has_eq_trailing and (has_substantive_operator or has_math_font or bool(MATH_COMMAND_RE.search(text)))
        )

        is_display_like = _line_is_display_like(
            node,
            prev_node,
            next_node,
            allow_column_formula=allow_column_formula,
        )
        if not is_display_like:
            continue

        confidence = "high" if requires_eq else "ambiguous"
        rec = {
            "kind": "formula",
            "confidence": confidence,
            "has_unknown_text": bool(UNKNOWN_TOKEN_RE.search(text)),
            "text": text,
            "page_no": node.get("page_no"),
            "line_indexes": [int(node.get("index") or pos)],
            "indexes": [int(node.get("index") or pos)],
            "bbox": _extract_node_bbox(node),
            "nodes": [node],
            "source": "pdf_lines",
        }
        rec["fingerprint"] = _record_fingerprint(rec)
        out.append(rec)
        if bool(UNKNOWN_TOKEN_RE.search(text)) and unknown_formula_candidates is not None:
            unknown_formula_candidates["value"] = True
    return out


def _text_health_has_unknown_tokens(text_health: dict[str, Any]) -> bool:
    for page in text_health.get("pages") or []:
        reasons = page.get("reasons") or []
        if "unknown_tokens" in reasons or "replacement_characters" in reasons:
            return True
    return False


def _text_health_has_unreadable_page(text_health: dict[str, Any]) -> bool:
    for page in text_health.get("pages") or []:
        for reason in page.get("reasons") or []:
            if reason in {"image_only", "empty_text", "short_text"}:
                return True
    return False


def _compute_proof_status(
    counts: dict[str, Any],
    text_health: dict[str, Any],
    has_unknown_formula_candidates: bool = False,
    has_extracted_lines: bool = True,
) -> dict[str, str]:
    proof = {kind: KIND_UNKNOWN for kind in KIND_ORDER}
    if not text_health.get("available") or not has_extracted_lines:
        return proof
    if not bool(text_health.get("page_no_continuous")):
        return proof
    if _text_health_has_unreadable_page(text_health):
        return proof
    for kind in KIND_ORDER:
        bucket = counts.get(kind) or {}
        if kind == "formula" and has_unknown_formula_candidates:
            proof[kind] = KIND_UNKNOWN
            continue
        if int(bucket.get("ambiguous") or 0) > 0:
            proof[kind] = KIND_UNKNOWN
            continue
        if int(bucket.get("high_confidence") or 0) > 0:
            proof[kind] = KIND_HEALTHY
            continue
        proof[kind] = KIND_HEALTHY
    return proof


def _document_records_counts(table_nodes: Sequence[dict[str, Any]], formula_nodes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts = {"table": _build_base_counts(), "formula": _build_base_counts()}
    for node in table_nodes:
        confidence = "high" if _safe_text(node.get("text")).strip() else "ambiguous"
        rec = {
            "kind": "table",
            "confidence": confidence,
            "text": _safe_text(node.get("text")),
            "page_no": node.get("page_no"),
            "line_indexes": [int(node.get("index") or 0)],
            "indexes": [int(node.get("index") or 0)],
            "bbox": _extract_node_bbox(node),
            "nodes": [node],
            "source": _safe_text(node.get("source", "document")),
            "fingerprint": _record_fingerprint(
                {
                    "kind": "table",
                    "text": node.get("text"),
                    "page_no": node.get("page_no"),
                    "source": node.get("source"),
                }
            ),
        }
        _add_to_counts(counts, [rec])
    for node in formula_nodes:
        confidence = "high" if _safe_text(node.get("text")).strip() else "ambiguous"
        rec = {
            "kind": "formula",
            "confidence": confidence,
            "text": _safe_text(node.get("text")),
            "page_no": node.get("page_no"),
            "line_indexes": [int(node.get("index") or 0)],
            "indexes": [int(node.get("index") or 0)],
            "bbox": _extract_node_bbox(node),
            "nodes": [node],
            "source": _safe_text(node.get("source", "document")),
            "fingerprint": _record_fingerprint(
                {
                    "kind": "formula",
                    "text": node.get("text"),
                    "page_no": node.get("page_no"),
                    "source": node.get("source"),
                }
            ),
        }
        _add_to_counts(counts, [rec])
    return counts


def pdf_structure_inventory(
    input_file: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "reason": "input_file_not_found",
        "source_pdf_sha256": None,
        "page_count": 0,
        "text_health": {},
        "counts": {kind: _build_base_counts() for kind in KIND_ORDER},
        "no_structure_proof": {kind: KIND_UNKNOWN for kind in KIND_ORDER},
    }

    path = Path(input_file)
    if not path.exists() or not path.is_file():
        return result

    try:
        result["source_pdf_sha256"] = _file_sha256(path)
    except OSError as exc:
        result["reason"] = f"hash_failed:{type(exc).__name__}"
        return result

    text_health = _document_text_health(path)
    result["text_health"] = text_health
    result["page_count"] = int(text_health.get("page_count") or 0)

    if not text_health.get("available"):
        result["reason"] = _safe_text(text_health.get("reason"))
        return result
    result["available"] = True

    try:
        text_nodes = _extract_pdf_lines(path)
    except Exception as exc:  # pragma: no cover
        result["reason"] = f"line_extraction_failed:{type(exc).__name__}"
        result["available"] = False
        result["no_structure_proof"] = {kind: KIND_UNKNOWN for kind in KIND_ORDER}
        return result
    has_extracted_lines = bool(text_nodes)

    algorithm_records = _classify_algorithm_records(text_nodes)
    algorithm_indexes = {
        index
        for record in algorithm_records
        for index in (record.get("line_indexes") or [])
        if isinstance(index, int)
    }
    code_records = _classify_code_records(text_nodes, algorithm_indexes)
    table_records = _classify_table_records(text_nodes)
    unknown_formula_candidates: dict[str, bool] = {"value": False}
    formula_records = _classify_formula_records(text_nodes, unknown_formula_candidates=unknown_formula_candidates)

    records = _dedupe([*algorithm_records, *code_records, *table_records, *formula_records])
    has_unknown_formula_candidates = any(
        bool(item.get("has_unknown_text")) for item in formula_records if item.get("kind") == "formula"
    )
    has_unknown_formula_candidates = has_unknown_formula_candidates or bool(unknown_formula_candidates.get("value"))
    counts = {kind: _build_base_counts() for kind in KIND_ORDER}
    _add_to_counts(counts, records)
    result["counts"] = counts

    result["no_structure_proof"] = _compute_proof_status(
        counts,
        text_health,
        has_unknown_formula_candidates=has_unknown_formula_candidates,
        has_extracted_lines=has_extracted_lines,
    )

    if not has_extracted_lines:
        if text_health.get("status") == KIND_HEALTHY and not _text_health_has_unreadable_page(text_health):
            result["reason"] = "line_extraction_no_lines"
        else:
            result["reason"] = _safe_text(text_health.get("reason"))
        return result

    if text_health.get("status") == KIND_HEALTHY and not _text_health_has_unreadable_page(text_health) and not any(
        record.get("confidence") == "ambiguous" for record in records
    ):
        result["reason"] = None
    else:
        result["reason"] = _safe_text(text_health.get("reason"))
    return result
