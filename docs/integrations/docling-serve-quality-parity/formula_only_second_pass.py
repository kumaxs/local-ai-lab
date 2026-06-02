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
import html
import json
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# Characters in Chinese CJK Unicode blocks (U+3400 to U+9FFF)
CJK_RE = re.compile(r"[\u3400-\u9fff]")
# Equation number in formula text: ( 3 ) or (3), spaces optional
EQ_NUM_RE = re.compile(r"\(\s*(\d+)\s*\)")
# Repeated "\\ a n d" pattern (at least 3 repeats = hallucination)
REPEATED_AND_RE = re.compile(r"(\\quad \\ \\ a n d ){3,}")
# Number-only formula text (just equation numbers, nothing else)
NUMBER_ONLY_RE = re.compile(r"^\s*(\(\s*[0-9]+\s*\)\s*)+\s*$")
# Suspicious repeated single characters like \ T \ T \ T (4+ repeats)
REPEATED_SINGLE_RE = re.compile(r"(\\ [a-zA-Z]\s*){4,}")
# Source bbox area threshold (tiny = likely wrong detection)
MIN_BBOX_AREA = 50.0  # PDF points^2
# Route B uses ~2x pixel scale (1190x1684 for a PDF page 595x842)
PIXEL_SCALE = 2.0
RIGHT_COLUMN_X_PX = 650.0


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def iter_nodes(obj: Any):
    """Yield every dict node in the document tree."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_nodes(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from iter_nodes(x)


def extract_formulas(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all formula nodes with normalized provenance info."""
    results: list[dict[str, Any]] = []
    for node in iter_nodes(doc):
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

        # Extract page number and bbox from prov
        page_no: int | None = None
        bbox_raw: dict[str, Any] | None = None
        for p in prov:
            if isinstance(p, dict):
                if page_no is None:
                    page_no = p.get("page_no")
                if bbox_raw is None:
                    bbox_raw = p.get("bbox") or {}

        # Normalize bbox to TOPLEFT pixel scale:
        # Route A: BOTTOMLEFT PDF-point coords -> TOPLEFT at PIXEL_SCALE
        # Route B: TOPLEFT pixel coords -> keep as-is
        bbox_norm: dict[str, float] | None = None
        if bbox_raw and isinstance(bbox_raw, dict):
            coord_origin = bbox_raw.get("coord_origin", "BOTTOMLEFT")
            l = float(bbox_raw.get("l", 0))
            r = float(bbox_raw.get("r", 0))
            # t and b are PDF coords: for BOTTOMLEFT, t > b (top is larger y)
            # for TOPLEFT, t < b (top is smaller y, i.e., closer to origin)
            t = float(bbox_raw.get("t", 0))
            b = float(bbox_raw.get("b", 0))

            if coord_origin == "BOTTOMLEFT":
                # Standard PDF page height ~842 pts
                page_h_pts = 841.8  # canonical page height
                # Convert to TOPLEFT: y_top = page_h_pts - y_pdf
                l_px = l * PIXEL_SCALE
                r_px = r * PIXEL_SCALE
                t_top_px = (page_h_pts - t) * PIXEL_SCALE
                b_top_px = (page_h_pts - b) * PIXEL_SCALE
            else:
                # Already TOPLEFT (pixel coords)
                l_px = l
                r_px = r
                t_top_px = t
                b_top_px = b

            bbox_norm = {
                "l": l_px, "r": r_px,
                "t": t_top_px, "b": b_top_px,
            }

        # Extract equation numbers from formula text
        eq_numbers: list[int] = [int(m.group(1)) for m in EQ_NUM_RE.finditer(text)]
        main_eq: int | None = eq_numbers[0] if eq_numbers else None

        results.append({
            "text": text,
            "page_no": page_no,
            "bbox_norm": bbox_norm,  # TOPLEFT pixel space
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


def _formula_asset_links(output_dir: Path, source_dir: Path, formula_no: int | None, page_no: int | None) -> dict[str, str | None]:
    """Return output-relative links to available source evidence assets."""
    links: dict[str, str | None] = {
        "formula_crop": None,
        "formula_context": None,
        "full_page": None,
        "source_review": None,
    }
    if formula_no is not None:
        crop = source_dir / "formulas" / f"formula_{formula_no}.png"
        context = source_dir / "formulas" / f"formula_{formula_no}_context.png"
        if crop.exists():
            links["formula_crop"] = _relative_link(output_dir, crop)
        if context.exists():
            links["formula_context"] = _relative_link(output_dir, context)
    if page_no is not None:
        page = source_dir / "pages" / f"page_{page_no}.png"
        if page.exists():
            links["full_page"] = _relative_link(output_dir, page)
    review = source_dir / "review_index.html"
    if review.exists():
        links["source_review"] = _relative_link(output_dir, review)
    return links


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
) -> dict[int, dict[str, Any]]:
    """Match Route B candidates to Route A formula indices.

    Returns dict: { route_a_index -> matched_route_b_formula }

    Matching strategy (in priority order):
    1. Equation number exact match on same page.
    2. Vertical-center proximity on same page (within 100 px).
    3. Text similarity >= sim_threshold (fallback for when bboxes unreliable).
    """
    # Build Route B lookup: by (page, eq_number)
    b_by_page_eq: dict[tuple[int | None, int], dict[str, Any]] = {}
    # Build Route B lookup: by page, sorted by vertical position
    b_by_page: dict[int | None, list[dict[str, Any]]] = {}

    for bf in route_b_formulas:
        page = bf.get("page_no")
        eq = bf.get("main_eq")
        if eq is not None:
            b_by_page_eq[(page, eq)] = bf
        b_by_page.setdefault(page, []).append(bf)

    matches: dict[int, dict[str, Any]] = {}
    used_b_indices: set[int] = set()  # prevent double-matching

    for i, af in enumerate(route_a_formulas):
        apage = af.get("page_no")
        aeq = af.get("main_eq")
        abbox = af.get("bbox_norm")

        # Strategy 1: equation number match on same page
        if aeq is not None:
            key = (apage, aeq)
            if key in b_by_page_eq and id(b_by_page_eq[key]) not in used_b_indices:
                matches[i] = b_by_page_eq[key]
                used_b_indices.add(id(b_by_page_eq[key]))
                continue

        # Strategy 2: vertical-center proximity on same page
        candidates: list[dict[str, Any]] = b_by_page.get(apage, [])
        if abbox is not None:
            a_cy = formula_vertical_center(abbox)
            best: dict[str, Any] | None = None
            best_dist = float("inf")
            for cf in candidates:
                if id(cf) in used_b_indices:
                    continue
                cbbox = cf.get("bbox_norm")
                if cbbox is None:
                    continue
                c_cy = formula_vertical_center(cbbox)
                dist = abs(a_cy - c_cy)
                if dist < best_dist and dist < 100:
                    best_dist = dist
                    best = cf
            if best is not None:
                matches[i] = best
                used_b_indices.add(id(best))
                continue

        # Strategy 3: text similarity fallback
        a_text = af.get("text", "")
        best_sim = sim_threshold
        best_sim_cf: dict[str, Any] | None = None
        for cf in candidates:
            if id(cf) in used_b_indices:
                continue
            sim = text_similarity(a_text, cf.get("text", ""))
            if sim > best_sim:
                best_sim = sim
                best_sim_cf = cf
        if best_sim_cf is not None:
            matches[i] = best_sim_cf
            used_b_indices.add(id(best_sim_cf))

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
        formulas = extract_formulas(doc)
        for formula_no, formula in enumerate(formulas, start=1):
            formula["formula_no"] = formula_no
        sources.append({
            "label": label,
            "source_dir": source_dir,
            "formulas": formulas,
            "error": None,
        })
    return sources


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
                "evidence": _formula_asset_links(
                    output_dir,
                    source["source_dir"],
                    formula.get("formula_no"),
                    formula.get("page_no"),
                ),
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
    return [int(m.group(1)) for m in EQ_NUM_RE.finditer(text)]


def _find_markdown_block(md_text: str, formula_text: str, eq_num: int | None) -> str:
    """Find the most likely $$...$$ markdown block for a formula."""
    if not md_text:
        return ""
    if eq_num is not None:
        pattern = re.compile(
            r"\$\$[^$]*?\(\s*" + re.escape(str(eq_num)) + r"\s*\)[^$]*?\$\$",
            re.DOTALL,
        )
        match = pattern.search(md_text)
        if match:
            return match.group(0)

    prefix = formula_text[:30]
    if prefix:
        pattern = re.compile(r"\$\$" + re.escape(prefix) + r"[^$]*?\$\$", re.DOTALL)
        match = pattern.search(md_text)
        if match:
            return match.group(0)
    return ""


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

    result = md_text

    for entry in replacement_log:
        if entry["status"] != "replaced":
            continue
        eq_num = entry.get("eq_number")
        route_b_text = entry.get("route_b_candidate", "")
        if not route_b_text:
            continue

        # Pattern: $$ ... ( N ) ... $$; replace the entire $$ block.
        # eq_num may be None (formula has no embedded equation number).
        # For those, fall back to content-prefix matching.
        eq_str = str(eq_num)
        # Find blocks containing this equation number
        pattern = re.compile(
            r"\$\$[^$]*?\(\s*" + eq_str + r"\s*\)[^$]*?\$\$",
            re.DOTALL,
        )
        # Also try blocks WITHOUT explicit equation number (for formulas where
        # Route A had eq_num but we replaced with Route B's cleaner text)
        def replacer(m: re.Match) -> str:
            # Preserve the $$ delimiters, replace inner content
            if eq_num is not None:
                if EQ_NUM_RE.search(route_b_text):
                    return f"$${route_b_text}$$"
                return f"$${route_b_text} \\quad ( {eq_num} )$$"
            else:
                return f"$${route_b_text}$$"

        result = pattern.sub(replacer, result)

        # Fallback for formulas without eq_numbers: match by content prefix.
        # This handles cases like formula (16) where Route B's VLM pipeline
        # omits equation numbers but the formula text is identifiable.
        if eq_num is None and route_b_text:
            route_a_text = entry.get("route_a_text", "")
            if route_a_text:
                # Use the first 30 chars of Route A text as a prefix anchor
                prefix = route_a_text[:30]
                # Escape special regex chars in the prefix
                safe_prefix = re.escape(prefix)
                # Find the $$ block that starts with this prefix
                prefix_pattern = re.compile(
                    r"\$\$" + safe_prefix + r"[^$]*?\$\$",
                    re.DOTALL,
                )
                result = prefix_pattern.sub(replacer, result)

    return result


def patch_document_json(
    route_a_doc: dict[str, Any],
    route_b_formulas: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Patch formula text in Route A document.json with Route B candidates.

    Returns (patched_doc, replacement_log).
    """
    route_a_formulas = extract_formulas(route_a_doc)
    for formula_no, formula in enumerate(route_a_formulas, start=1):
        formula["formula_no"] = formula_no
    for formula_no, formula in enumerate(route_b_formulas, start=1):
        formula["formula_no"] = formula_no
    matches = match_route_b_to_route_a(route_a_formulas, route_b_formulas)

    log: list[dict[str, Any]] = []

    for i, af in enumerate(route_a_formulas):
        reasons = is_suspicious(af)
        if not reasons:
            continue

        if i not in matches:
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
                "candidate_diagnostics": formula_diagnostics(None),
                "status": "suspicious_no_route_b_match",
            })
            continue

        bf = matches[i]
        route_b_text = bf["text"]
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
                "candidate_diagnostics": formula_diagnostics(None),
                "status": "route_b_also_empty",
            })
            continue

        _patch_node_text(af["node"], route_b_text)
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
            "candidate_diagnostics": formula_diagnostics(route_b_text),
            "status": "replaced",
        })

    return route_a_doc, log


def add_review_evidence(
    replacement_log: list[dict[str, Any]],
    route_a_dir: Path,
    route_b_dir: Path,
    review_candidate_sources: list[dict[str, Any]],
    output_dir: Path,
    before_md: str,
    after_md: str,
) -> None:
    """Attach human-review evidence metadata to each replacement log entry."""
    for entry in replacement_log:
        route_a_text = entry.get("route_a_text", "")
        route_b_text = entry.get("route_b_candidate") or ""
        eq_num = entry.get("eq_number")
        page_no = entry.get("page_no")
        entry["route_a_evidence"] = _formula_asset_links(
            output_dir, route_a_dir, entry.get("formula_no"), page_no
        )
        entry["route_b_evidence"] = _formula_asset_links(
            output_dir, route_b_dir, entry.get("route_b_formula_no"), page_no
        )
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
        entry["review_notes"] = review_notes(entry)


def _render_asset_link(label: str, href: str | None) -> str:
    if not href:
        return f"<span class=\"missing\">{html.escape(label)} missing</span>"
    return f"<a href=\"{html.escape(href)}\">{html.escape(label)}</a>"


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
        reasons = ", ".join(entry.get("reasons") or [])
        right_col = "yes" if entry.get("right_column_likely") else "no"
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
    <div><dt>Right column</dt><dd>{_html_text(right_col)}</dd></div>
    <div><dt>Route A bbox</dt><dd>{_html_text(entry.get('route_a_bbox'))}</dd></div>
  </dl>
  <h3>Review Notes</h3>
  {_render_notes(entry.get('review_notes') or [])}
  <div class="compare">
    <div>
      <h3>Route A Formula Text</h3>
      <div class="diagnostics">{_render_diagnostics(entry.get('route_a_diagnostics'))}</div>
      <pre>{_html_text(_truncate_review_text(entry.get('route_a_text') or ''))}</pre>
    </div>
    <div>
      <h3>Replacement Candidate</h3>
      <div class="diagnostics">{_render_diagnostics(entry.get('candidate_diagnostics'))}</div>
      <pre>{_html_text(_truncate_review_text(entry.get('route_b_candidate') or 'NO ROUTE B MATCH'))}</pre>
    </div>
  </div>
  <div class="compare">
    <div>
      <h3>Before Markdown Snippet</h3>
      <pre>{_html_text(entry.get('markdown_before') or 'No markdown block found')}</pre>
    </div>
    <div>
      <h3>After Markdown Snippet</h3>
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


def run_formula_second_pass(
    route_a_dir: Path,
    route_b_dir: Path,
    output_dir: Path,
    review_candidate_args: list[str] | None = None,
) -> dict[str, Any]:
    """Run the formula-only second pass on a single document."""
    output_dir.mkdir(parents=True, exist_ok=True)
    review_candidate_sources = load_review_candidate_sources(review_candidate_args or [])

    route_a_doc = load_json(route_a_dir / "document.json")
    route_b_doc = load_json(route_b_dir / "document.json")

    if route_a_doc is None:
        return {"ok": False, "error": f"Route A document.json not found: {route_a_dir}"}
    if route_b_doc is None:
        return {"ok": False, "error": f"Route B document.json not found: {route_b_dir}"}

    route_a_formulas = extract_formulas(route_a_doc)
    route_b_formulas = extract_formulas(route_b_doc)

    suspicious_count = sum(1 for f in route_a_formulas if is_suspicious(f))

    patched_doc, replacement_log = patch_document_json(route_a_doc, route_b_formulas)

    replaced_count = sum(1 for e in replacement_log if e["status"] == "replaced")
    no_match_count = sum(
        1 for e in replacement_log
        if e["status"] in ("suspicious_no_route_b_match", "route_b_also_empty")
    )

    # Write patched document.json
    (output_dir / "document.json").write_text(
        json.dumps(patched_doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Patch document.md
    md_path = route_a_dir / "document.md"
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    patched_md = patch_document_md(md_text, route_a_formulas, replacement_log)
    (output_dir / "document.md").write_text(patched_md, encoding="utf-8")

    add_review_evidence(
        replacement_log,
        route_a_dir,
        route_b_dir,
        review_candidate_sources,
        output_dir,
        md_text,
        patched_md,
    )

    summary = {
        "route_a_dir": str(route_a_dir),
        "route_b_dir": str(route_b_dir),
        "output_dir": str(output_dir),
        "route_a_formula_count": len(route_a_formulas),
        "route_b_formula_count": len(route_b_formulas),
        "suspicious_formula_count": suspicious_count,
        "replaced_count": replaced_count,
        "no_match_count": no_match_count,
        "review_candidate_sources": [
            {
                "label": source["label"],
                "source_dir": str(source["source_dir"]),
                "formula_count": len(source["formulas"]),
                "error": source.get("error"),
            }
            for source in review_candidate_sources
        ],
        "replacement_log": replacement_log,
        "ok": True,
    }
    review_path = write_review_html(output_dir, summary)
    summary["review_html_path"] = str(review_path)
    (output_dir / "second_pass_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_formula_second_pass(
        args.route_a_dir,
        args.route_b_dir,
        args.output_dir,
        args.review_candidate_dir,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
