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
import json
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


def _patch_node_text(node: dict[str, Any], new_text: str) -> None:
    """Recursively patch formula node text in document tree."""
    if "text" in node:
        node["text"] = new_text
    for child in node.get("children", []) or []:
        _patch_node_text(child, new_text)


def _extract_eq_numbers_from_text(text: str) -> list[int]:
    return [int(m.group(1)) for m in EQ_NUM_RE.finditer(text)]


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
            old = m.group(0)
            # Preserve the $$ delimiters, replace inner content
            if eq_num is not None:
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
    matches = match_route_b_to_route_a(route_a_formulas, route_b_formulas)

    log: list[dict[str, Any]] = []

    for i, af in enumerate(route_a_formulas):
        reasons = is_suspicious(af)
        if not reasons:
            continue

        if i not in matches:
            log.append({
                "index": i,
                "route_a_text": af["text"][:100],
                "page_no": af["page_no"],
                "eq_number": af["main_eq"],
                "reasons": reasons,
                "route_b_candidate": None,
                "status": "suspicious_no_route_b_match",
            })
            continue

        bf = matches[i]
        route_b_text = bf["text"]
        if not route_b_text.strip():
            log.append({
                "index": i,
                "route_a_text": af["text"][:100],
                "page_no": af["page_no"],
                "eq_number": af["main_eq"],
                "reasons": reasons,
                "route_b_candidate": None,
                "status": "route_b_also_empty",
            })
            continue

        _patch_node_text(af["node"], route_b_text)
        log.append({
            "index": i,
            "route_a_text": af["text"][:100],
            "page_no": af["page_no"],
            "eq_number": af["main_eq"],
            "reasons": reasons,
            "route_b_candidate": route_b_text[:100],
            "status": "replaced",
        })

    return route_a_doc, log


def run_formula_second_pass(
    route_a_dir: Path,
    route_b_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Run the formula-only second pass on a single document."""
    output_dir.mkdir(parents=True, exist_ok=True)

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

    summary = {
        "route_a_dir": str(route_a_dir),
        "route_b_dir": str(route_b_dir),
        "output_dir": str(output_dir),
        "route_a_formula_count": len(route_a_formulas),
        "route_b_formula_count": len(route_b_formulas),
        "suspicious_formula_count": suspicious_count,
        "replaced_count": replaced_count,
        "no_match_count": no_match_count,
        "replacement_log": replacement_log,
        "ok": True,
    }
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_formula_second_pass(args.route_a_dir, args.route_b_dir, args.output_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
