from __future__ import annotations

import base64
import copy
import html
import io
import keyword
import re
import token
import tokenize
from pathlib import Path
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable


QUARANTINED_MAIN_FLOW_KINDS = {
    "page_header",
    "page_footer",
    "visual_annotation",
    "table_visual_annotation",
    "math_font_noise",
}

_INLINE_MATH_MATH_FONT_HINTS = (
    "CMMI",
    "CMSY",
    "CMEX",
    "MSBM",
    "EUSM",
    "MATH",
)
_MATH_SYMBOL_CHARS = set("∑∏∫∈∏∞∇√·≤≥≠≈≡⊂⊃⊆⊇∩∪∧∨¬∀∃→←→↔")


def _strip_inline_math_control_text(value: str) -> str:
    value = _clean_glyph_text(value)
    return re.sub(
        r"[\uFFFE\uFFFF]",
        "",
        value,
    ).replace("\r", "").replace("\n", "")


def _normalize_formula_similarity_text(value: str) -> str:
    value = _strip_inline_math_control_text(value)
    value = value.replace("\u2212", "-")
    value = value.replace("−", "-")
    value = value.replace("—", "-")
    value = value.replace("\n", "")
    return re.sub(r"\s+", "", value)


def _repair_flattened_inline_math(value: str) -> str:
    """Return fallback text unchanged when no PDF glyph geometry is present.

    Inline script recovery is deliberately implemented by
    :func:`_inline_geometry_repair`, which pairs a unique fallback span with
    SourceReader pypdfium glyph evidence.  This compatibility helper no longer
    performs global token substitutions that could alter ordinary prose.
    """
    return value


def _repair_source_comparison_operators(value: str, source_value: str) -> str:
    """Repair only a locally proven combining-strike operator.

    A previous implementation copied the complete comparison-operator sequence
    from the PDF text into the Docling fallback.  That is unsafe: a paragraph
    can contain ``C > 0`` and ``n ≥ n₀`` while the two extraction paths emit a
    different ordering of operators.  Copying the sequence by position then
    silently changes the meaning of ordinary inequalities.  We now normalize
    only an actual U+0338 overlay, or a uniquely aligned ``=``/``∈`` whose
    source counterpart is the corresponding negated operator.
    """

    value = re.sub(r"\u0338\s*=", "≠", value)
    value = re.sub(r"\u0338\s*∈", "∉", value)
    source_value = re.sub(r"\u0338\s*=", "≠", source_value)
    source_value = re.sub(r"\u0338\s*∈", "∉", source_value)
    operator_pattern = re.compile(r"≠|∉|≤|≥|=|<|>|∈")
    source_matches = list(operator_pattern.finditer(source_value))
    value_matches = list(operator_pattern.finditer(value))
    if not source_matches or len(source_matches) != len(value_matches):
        return value

    source_operators = [match.group(0) for match in source_matches]
    value_operators = [match.group(0) for match in value_matches]
    differing = [
        index
        for index, (value_operator, source_operator) in enumerate(
            zip(value_operators, source_operators)
        )
        if value_operator != source_operator
    ]
    # A local overlay is the only source of a new negation.  Every other
    # comparison must already agree, and all non-operator text must match; this
    # prevents C>0/n≥n0 (or any other ordinary inequalities) from being zipped
    # into the wrong glyph.
    if not differing or any(
        source_operators[index] not in {"≠", "∉"}
        or value_operators[index] not in {"=", "∈"}
        for index in differing
    ):
        return value
    value_parts = operator_pattern.split(value)
    source_parts = operator_pattern.split(source_value)
    if len(value_parts) != len(source_parts):
        return value
    if any(
        _normalize_formula_similarity_text(value_part)
        != _normalize_formula_similarity_text(source_part)
        for value_part, source_part in zip(value_parts, source_parts)
    ):
        return value
    replacements = [
        (value_matches[index].start(), value_matches[index].end(), source_operators[index])
        for index in differing
    ]
    repaired = value
    for start, end, replacement in reversed(replacements):
        repaired = repaired[:start] + replacement + repaired[end:]
    return repaired


def _inline_geometry_compact_chars(
    runs: list[dict[str, Any]],
) -> tuple[str, list[int]]:
    compact: list[str] = []
    mapping: list[int] = []
    for index, run in enumerate(runs):
        text_value = _strip_inline_math_control_text(str(run.get("text") or ""))
        for character in text_value:
            if character.isspace():
                continue
            compact.append(character)
            mapping.append(index)
    return "".join(compact), mapping


def _inline_geometry_char_size(run: dict[str, Any]) -> float:
    try:
        return float(run.get("size") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _inline_geometry_char_center_y(run: dict[str, Any]) -> float:
    bbox = run.get("bbox")
    if not isinstance(bbox, dict):
        return 0.0
    return (
        float(bbox.get("t") or 0.0) + float(bbox.get("b") or 0.0)
    ) / 2.0


def _inline_geometry_bbox_intersects(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    """Return whether two PDF glyph boxes overlap in the same coordinate frame."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_l = float(left.get("l") or 0.0)
    left_r = float(left.get("r") or 0.0)
    left_t = float(left.get("t") or 0.0)
    left_b = float(left.get("b") or 0.0)
    right_l = float(right.get("l") or 0.0)
    right_r = float(right.get("r") or 0.0)
    right_t = float(right.get("t") or 0.0)
    right_b = float(right.get("b") or 0.0)
    return not (
        left_r < right_l
        or right_r < left_l
        or max(left_t, left_b) < min(right_t, right_b)
        or max(right_t, right_b) < min(left_t, left_b)
    )


def _inline_geometry_bbox_union(
    boxes: Iterable[dict[str, Any]],
) -> dict[str, float] | None:
    values = [box for box in boxes if isinstance(box, dict)]
    if not values:
        return None
    return {
        "l": min(float(box.get("l") or 0.0) for box in values),
        "r": max(float(box.get("r") or 0.0) for box in values),
        "t": max(float(box.get("t") or 0.0) for box in values),
        "b": min(float(box.get("b") or 0.0) for box in values),
        "coord_origin": str(values[0].get("coord_origin") or "BOTTOMLEFT"),
    }


def _inline_math_control_character(run: dict[str, Any]) -> bool:
    """Identify font-private CMEX/CID glyphs that cannot be semantically guessed."""
    font_name = str(
        run.get("fontname")
        or run.get("font_name")
        or run.get("font")
        or ""
    ).upper()
    text_value = str(run.get("text") or "")
    if not text_value:
        return False
    if "(CID:" in text_value.upper():
        return True
    if "CMEX" not in font_name:
        return False
    # CMEX control codes (for example U+0010/U+0011) are emitted for large
    # delimiters.  A CID marker is equally opaque even when pdfplumber keeps
    # it as printable text.  Do not classify ordinary CMMI/CMSY minus glyphs
    # as controls here; fraction bars are detected from page line objects.
    return any(ord(character) < 32 for character in text_value)


def _inline_geometry_preserve_private_text(value: str) -> str:
    """Keep unknown CID spellings for diagnostics instead of deleting them."""
    if re.search(r"\(cid:\d+\)", value, flags=re.IGNORECASE):
        return value.replace("\r", "").replace("\n", "")
    return _strip_inline_math_control_text(value)


def _inline_geometry_repair(
    fallback: str,
    runs: list[dict[str, Any]],
    *,
    math_font_evidence: Any = None,
    cluster_diagnostics: list[dict[str, Any]] | None = None,
    blocked_bboxes: list[dict[str, Any]] | None = None,
    allow_text_script_base: bool = False,
) -> tuple[str, set[str], set[str]]:
    """Recover inline scripts from local PDF glyph geometry.

    Docling's body fallback frequently flattens a TeX script into ordinary
    prose (``n -1 / 2``).  This routine deliberately does not contain a list
    of paper-specific tokens.  Instead it identifies a base glyph followed by
    a compact, spatially attached run of smaller glyphs, aligns that source
    span to the fallback with :class:`SequenceMatcher`, and rewrites only a
    unique alignment.  Ambiguous, cross-line, or incomplete alignments remain
    unchanged and are reported through ``unresolved_names`` so the caller can
    attach an exact PDF crop.
    """
    source_compact, source_mapping = _inline_geometry_compact_chars(runs)
    if not source_compact:
        return fallback, set(), set()

    def alignment_char(value: str) -> str:
        # Keep the alignment conservative: PDF minus variants are equivalent,
        # while all other symbols retain their identity.
        return {
            "−": "-",
            "—": "-",
            "–": "-",
        }.get(value, value)

    source_alignment = "".join(alignment_char(char) for char in source_compact)
    fallback_compact, fallback_mapping = _inline_geometry_compact_chars(
        [{"text": char} for char in fallback]
    )
    fallback_alignment = "".join(
        alignment_char(char) for char in fallback_compact
    )
    if not fallback_alignment:
        return fallback, set(), set()

    # Compact-stream alignment is intentionally explicit rather than relying
    # on paragraph word boundaries.  This handles extraction-only whitespace
    # and glued ``raten -1 / 2`` spans while preserving occurrence order.
    source_to_fallback: dict[int, int] = {}
    matcher = SequenceMatcher(
        None,
        source_alignment,
        fallback_alignment,
        autojunk=False,
    )
    for tag, source_start, source_end, fallback_start, fallback_end in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(source_end - source_start):
            source_to_fallback[source_start + offset] = fallback_start + offset

    positive_sizes = [
        _inline_geometry_char_size(run)
        for run in runs
        if _inline_geometry_char_size(run) > 0.0
    ]
    if not positive_sizes:
        return fallback, set(), set()
    document_median = median(positive_sizes)
    # Script glyphs can outnumber body glyphs in a tiny synthetic crop (or a
    # compact limit such as ``i=1``).  Establish the baseline from the upper
    # size mode instead of the raw median so a majority of 7--8pt scripts
    # cannot make 10--11pt bases look like scripts themselves.
    baseline_candidates = [
        size for size in positive_sizes if size >= document_median * 1.10
    ]
    document_baseline = median(baseline_candidates or positive_sizes)

    def run_bbox(index: int) -> dict[str, Any]:
        value = runs[index].get("bbox")
        return value if isinstance(value, dict) else {}

    def run_left(index: int) -> float:
        return float(run_bbox(index).get("l") or 0.0)

    def run_right(index: int) -> float:
        return float(run_bbox(index).get("r") or 0.0)

    def run_center_y(index: int) -> float:
        return _inline_geometry_char_center_y(runs[index])

    def local_baseline(index: int) -> float:
        center_y = run_center_y(index)
        nearby = []
        # A body line can contain a mixture of roman and italic math fonts;
        # use nearby full-size glyphs, excluding scripts and neighbouring
        # lines.  Fall back to the document baseline when the crop is tiny.
        for candidate_index, run in enumerate(runs):
            size = _inline_geometry_char_size(run)
            if size <= 0.0 or size < document_baseline * 0.90:
                continue
            if abs(run_center_y(candidate_index) - center_y) > max(8.0, size * 0.90):
                continue
            nearby.append(size)
        if nearby:
            return median(nearby)
        return max(document_baseline, _inline_geometry_char_size(runs[index]))

    def has_math_base(index: int) -> bool:
        if callable(math_font_evidence):
            try:
                if bool(math_font_evidence(runs[index])):
                    return True
            except Exception:
                pass
        if allow_text_script_base and re.fullmatch(
            r"[A-Za-z]",
            run_text(index),
        ):
            # Bold/roman variables in algorithms (for example ``x_out``)
            # are often not tagged with a math-font family.  The same strict
            # size/offset/x-chain checks below still have to prove the script.
            return True
        font_name = str(
            runs[index].get("fontname")
            or runs[index].get("font_name")
            or runs[index].get("font")
            or ""
        ).upper()
        return bool(font_name) and any(
            hint in font_name for hint in _INLINE_MATH_MATH_FONT_HINTS
        )

    def run_text(index: int) -> str:
        return _strip_inline_math_control_text(str(runs[index].get("text") or ""))

    # For each full-size math glyph, collect immediately following small glyphs
    # whose x-chain and vertical displacement prove a superscript/subscript.
    # Whitespace/zero-width PDF runs do not interrupt a local cluster, but a
    # normal-size glyph, line wrap, or large x-gap ends it.
    clusters: list[dict[str, Any]] = []
    for base_index, base_run in enumerate(runs):
        base_text = run_text(base_index)
        if not base_text or base_text.isspace():
            continue
        baseline = local_baseline(base_index)
        base_size = _inline_geometry_char_size(base_run)
        # Inline formula bases can themselves be set in a reduced math size
        # (e.g. BERT's ``e^{S·T_i}`` at 8pt beside 11pt prose).  Keep a lower
        # bound to exclude the 6pt script tier, then let the attached cluster
        # geometry prove whether the reduced base is legitimate.
        if base_size <= 0.0 or base_size < max(1.0, document_baseline * 0.65):
            continue
        if not has_math_base(base_index):
            continue
        base_y = run_center_y(base_index)
        minimum_delta = max(1.45, baseline * 0.18)
        attached: list[tuple[int, str]] = []
        roles: dict[str, list[int]] = {"sup": [], "sub": []}
        anchor_index = base_index
        look_index = base_index + 1
        while look_index < len(runs):
            text_value = run_text(look_index)
            if not text_value or text_value.isspace():
                look_index += 1
                continue
            size = _inline_geometry_char_size(runs[look_index])
            if size <= 0.0:
                look_index += 1
                continue
            candidate_baseline = local_baseline(look_index)
            if size > candidate_baseline * 0.88:
                break
            left_gap = run_left(look_index) - run_left(anchor_index)
            # Do not attach a small glyph from the next physical line or a
            # distant column.  Negative gaps are permitted for overlapping
            # italic glyph boxes, but a large backwards jump is a line wrap.
            if left_gap < -16.0 or left_gap > 16.0:
                break
            delta = run_center_y(look_index) - base_y
            role = "sup" if delta >= 0.0 else "sub"
            edge_gap = run_left(look_index) - run_right(anchor_index)
            # A vertical offset alone is not enough: punctuation or a glyph
            # from the next formula can sit far to the right and otherwise be
            # mistaken for a script (observed as `,^{1}`).  Keep every edge
            # in a tight local chain; small negative overlap covers italic
            # boxes while distant candidates remain unresolved crops.
            opposite_role_overlap = bool(attached) and role != attached[-1][1]
            base_edge_gap = run_left(look_index) - run_right(base_index)
            if edge_gap < -2.0 and not (
                opposite_role_overlap
                and -2.0 <= base_edge_gap <= max(2.5, size * 0.35)
            ):
                break
            if edge_gap > max(2.5, size * 0.35):
                break
            relaxed_vertical_ok = (
                size <= baseline * 0.80
                and abs(delta) >= 1.0
                and -2.0 <= edge_gap <= max(2.5, size * 0.30)
            )
            if abs(delta) < minimum_delta and not relaxed_vertical_ok:
                break
            attached.append((look_index, role))
            roles[role].append(look_index)
            anchor_index = look_index
            look_index += 1
        if not attached:
            continue
        # A reduced-size math base (for example an 8pt ``e`` embedded in an
        # 11pt body line) may itself be a limit/operator glyph.  Equal-size
        # neighbours in that reduced tier are not scripts; require one
        # strictly smaller attached glyph before accepting the cluster.  This
        # suppresses false inner repairs such as ``d i = 1`` while retaining
        # genuine nested notation ``e^{S\cdot T_i}`` and ``H_{K_i}``.
        if base_size < baseline * 0.88 and not any(
            _inline_geometry_char_size(runs[index]) < base_size * 0.98
            for index, _role in attached
        ):
            continue
        # A base with an attached cluster is itself evidence; retain the
        # cluster even when the fallback alignment later proves ambiguous.
        clusters.append(
            {
                "base_index": base_index,
                "scripts": attached,
                "roles": roles,
                "baseline": baseline,
            }
        )

    if not clusters:
        return fallback, set(), set()

    def cluster_bbox(cluster: dict[str, Any]) -> dict[str, float] | None:
        indexes = [
            int(cluster["base_index"]),
            *[index for index, _role in cluster["scripts"]],
        ]
        boxes = [
            runs[index].get("bbox")
            for index in indexes
            if isinstance(runs[index].get("bbox"), dict)
        ]
        if not boxes:
            return None
        return {
            "l": min(float(box.get("l") or 0.0) for box in boxes),
            "r": max(float(box.get("r") or 0.0) for box in boxes),
            "t": max(float(box.get("t") or 0.0) for box in boxes),
            "b": min(float(box.get("b") or 0.0) for box in boxes),
            "coord_origin": str(
                (boxes[0].get("coord_origin") or "BOTTOMLEFT")
            ),
        }

    blackboard = {
        "C": "ℂ",
        "N": "ℕ",
        "P": "ℙ",
        "Q": "ℚ",
        "R": "ℝ",
        "Z": "ℤ",
    }

    def source_positions_for_runs(run_indexes: Iterable[int]) -> list[int]:
        wanted = set(run_indexes)
        return [index for index, run_index in enumerate(source_mapping) if run_index in wanted]

    def compact_fallback_positions(source_positions: list[int]) -> list[int] | None:
        mapped = [source_to_fallback.get(position) for position in source_positions]
        if any(value is None for value in mapped):
            return None
        compact_positions = [int(value) for value in mapped if value is not None]
        if compact_positions != sorted(set(compact_positions)):
            return None
        if compact_positions[-1] - compact_positions[0] > max(24, len(source_positions) * 4):
            return None
        return compact_positions

    def render_script(
        role: str,
        value: str,
        run_indexes: list[int] | None = None,
    ) -> str:
        value = re.sub(r"\s+", "", value)
        if not value:
            return ""
        if run_indexes and len(run_indexes) > 1:
            nested_parts = [run_text(run_indexes[0]).strip()]
            previous_size = _inline_geometry_char_size(runs[run_indexes[0]])
            previous_index = run_indexes[0]
            for run_index in run_indexes[1:]:
                part = run_text(run_index).strip()
                if not part:
                    continue
                size = _inline_geometry_char_size(runs[run_index])
                nested_vertical = (
                    _inline_geometry_char_center_y(runs[run_index])
                    < _inline_geometry_char_center_y(runs[previous_index]) - 1.0
                )
                if (
                    previous_size > 0.0
                    and (
                        size < previous_size * 0.85
                        or nested_vertical
                    )
                ):
                    nested_parts.append(
                        "_" + (part if len(part) == 1 else "{" + part + "}")
                    )
                else:
                    nested_parts.append(part)
                previous_size = size or previous_size
                previous_index = run_index
            value = "".join(nested_parts)
        value = value.replace("−", "-").replace("—", "-").replace("–", "-")
        marker = "^" if role == "sup" else "_"
        return f"{marker}{value}" if len(value) == 1 else f"{marker}{{{value}}}"

    def cluster_name(
        base: str,
        superscript: str,
        subscript: str,
        base_index: int | None = None,
    ) -> str:
        superscript = superscript.replace("−", "-").replace("—", "-").replace("–", "-")
        subscript = subscript.replace("−", "-").replace("—", "-").replace("–", "-")
        role = (
            "dual"
            if superscript and subscript
            else "sup"
            if superscript
            else "sub"
            if subscript
            else "dual"
        )
        token = re.sub(r"[^A-Za-z0-9]+", "-", f"{superscript}{subscript}").strip("-")
        suffix = f"-run{base_index}" if base_index is not None else ""
        return f"geometry_script-{base or 'unknown'}-{role}-{token or 'empty'}{suffix}"

    replacements: list[tuple[int, int, str, str]] = []
    cluster_run_indexes: dict[str, set[int]] = {}
    unresolved_names: set[str] = set()
    blocked_names: set[str] = set()

    def has_substantial_math_span_overlap(
        candidate_box: dict[str, Any] | None,
        blocked_box: dict[str, Any] | None,
    ) -> bool:
        if not _inline_geometry_bbox_intersects(candidate_box, blocked_box):
            return False
        assert isinstance(candidate_box, dict)
        assert isinstance(blocked_box, dict)
        candidate_left = float(candidate_box.get("l") or 0.0)
        candidate_right = float(candidate_box.get("r") or 0.0)
        blocked_left = float(blocked_box.get("l") or 0.0)
        blocked_right = float(blocked_box.get("r") or 0.0)
        candidate_top = max(
            float(candidate_box.get("t") or 0.0),
            float(candidate_box.get("b") or 0.0),
        )
        candidate_bottom = min(
            float(candidate_box.get("t") or 0.0),
            float(candidate_box.get("b") or 0.0),
        )
        blocked_top = max(
            float(blocked_box.get("t") or 0.0),
            float(blocked_box.get("b") or 0.0),
        )
        blocked_bottom = min(
            float(blocked_box.get("t") or 0.0),
            float(blocked_box.get("b") or 0.0),
        )
        horizontal_overlap = min(candidate_right, blocked_right) - max(
            candidate_left, blocked_left
        )
        vertical_overlap = min(candidate_top, blocked_top) - max(
            candidate_bottom, blocked_bottom
        )
        # A one-point edge contact is common when a neighboring script is on
        # the adjacent text line.  Require a real vertical overlap so a visual
        # fraction crop cannot suppress an otherwise repairable C_b/P_n
        # cluster merely because its antialiased box touches it.
        return horizontal_overlap > 0.5 and vertical_overlap >= 2.0

    for cluster in clusters:
        base_index = int(cluster["base_index"])
        script_indexes = [index for index, _role in cluster["scripts"]]
        run_indexes = [base_index, *script_indexes]
        source_positions = source_positions_for_runs(run_indexes)
        if not source_positions:
            continue
        fallback_positions = compact_fallback_positions(source_positions)
        base_text = run_text(base_index)
        scripts_by_role: dict[str, str] = {"sup": "", "sub": ""}
        for role, role_indexes in cluster["roles"].items():
            scripts_by_role[role] = "".join(run_text(index) for index in role_indexes)
        superscript = scripts_by_role["sup"]
        subscript = scripts_by_role["sub"]
        name = cluster_name(base_text, superscript, subscript, base_index)
        cluster_run_indexes[name] = set(run_indexes)
        if cluster_diagnostics is not None:
            cluster_diagnostics.append(
                {
                    "name": name,
                    "bbox": cluster_bbox(cluster),
                    "source_text": "".join(
                        run_text(index) for index in run_indexes
                    ),
                    "base_index": base_index,
                    "script_indexes": script_indexes,
                    "resolved": False,
                }
            )
        cluster_box = cluster_bbox(cluster)
        if cluster_box and any(
            has_substantial_math_span_overlap(cluster_box, blocked_box)
            for blocked_box in (blocked_bboxes or [])
        ):
            # A fraction bar or a font-private delimiter proves that this
            # local span is a visual math object whose flattened text cannot
            # be repaired independently.  Keep the cluster diagnostic for
            # provenance, but let the encompassing control-span crop carry
            # the unresolved evidence rather than emitting a partial script.
            if cluster_diagnostics is not None:
                cluster_diagnostics[-1]["suppressed"] = True
                cluster_diagnostics[-1]["blocked_by_math_span"] = True
            blocked_names.add(name)
            continue
        if fallback_positions is None:
            base_source_positions = source_positions_for_runs([base_index])
            script_source_positions = source_positions_for_runs(script_indexes)
            mapped_base = [
                source_to_fallback[position]
                for position in base_source_positions
                if position in source_to_fallback
            ]
            mapped_scripts = [
                source_to_fallback[position]
                for position in script_source_positions
                if position in source_to_fallback
            ]
            missing_base_only = bool(
                base_source_positions
                and not mapped_base
                and script_source_positions
                and len(mapped_scripts) == len(script_source_positions)
            )
            missing_scripts_only = bool(
                allow_text_script_base
                and base_source_positions
                and len(mapped_base) == len(base_source_positions)
                and script_source_positions
                and not mapped_scripts
                and len(script_source_positions) <= 8
            )
            partial_positions = (
                mapped_scripts
                if missing_base_only
                else mapped_base
                if missing_scripts_only
                else []
            )
            partial_source_positions = (
                script_source_positions
                if missing_base_only
                else base_source_positions
                if missing_scripts_only
                else []
            )
            partial_positions = sorted(set(partial_positions))
            partial_source_positions = sorted(set(partial_source_positions))
            partial_safe = bool(
                partial_positions
                and len(partial_positions) == len(partial_source_positions)
                and partial_positions
                == list(range(partial_positions[0], partial_positions[-1] + 1))
                and "".join(
                    source_alignment[position]
                    for position in partial_source_positions
                )
                == "".join(
                    fallback_alignment[position]
                    for position in partial_positions
                )
            )
            if partial_safe and missing_base_only:
                token = "".join(
                    fallback_alignment[position]
                    for position in partial_positions
                )
                partial_safe = bool(
                    token
                    and sum(
                        1
                        for start in range(
                            0,
                            len(fallback_alignment) - len(token) + 1,
                        )
                        if fallback_alignment.startswith(token, start)
                    )
                    == 1
                )
            if partial_safe and missing_scripts_only:
                source_min = min(source_positions)
                source_max = max(source_positions)
                before = source_to_fallback.get(source_min - 1)
                after = source_to_fallback.get(source_max + 1)
                partial_safe = bool(
                    (before is not None and 0 < partial_positions[0] - before <= 3)
                    or (after is not None and 0 < after - partial_positions[-1] <= 3)
                )
            if partial_safe:
                fallback_start = fallback_mapping[partial_positions[0]]
                fallback_end = fallback_mapping[partial_positions[-1]] + 1
                font_name = str(
                    runs[base_index].get("fontname")
                    or runs[base_index].get("font_name")
                    or runs[base_index].get("font")
                    or ""
                ).upper()
                base_rendered = (
                    blackboard[base_text]
                    if "MSBM" in font_name and base_text in blackboard
                    else base_text
                )
                replacement = (
                    base_rendered
                    + render_script(
                        "sub",
                        subscript,
                        cluster["roles"]["sub"],
                    )
                    + render_script(
                        "sup",
                        superscript,
                        cluster["roles"]["sup"],
                    )
                )
                replacements.append(
                    (fallback_start, fallback_end, replacement, name)
                )
                continue
            unresolved_names.add(name)
            continue
        source_token = "".join(
            source_alignment[position] for position in source_positions
        )
        # A source token that occurs once but aligns to one of several
        # identical fallback tokens is not a safe semantic edit.  Keep the
        # visual evidence and report it instead of selecting by accident.
        source_occurrences = [
            start
            for start in range(0, len(source_alignment) - len(source_token) + 1)
            if source_alignment.startswith(source_token, start)
        ] if source_token else []
        fallback_occurrences = [
            start
            for start in range(0, len(fallback_alignment) - len(source_token) + 1)
            if fallback_alignment.startswith(source_token, start)
        ] if source_token else []
        if source_occurrences and len(source_occurrences) != len(fallback_occurrences):
            unresolved_names.add(name)
            continue
        fallback_start = fallback_mapping[fallback_positions[0]]
        fallback_end = fallback_mapping[fallback_positions[-1]] + 1
        if fallback_end <= fallback_start:
            unresolved_names.add(name)
            continue
        # Require a local compact match of the source base/script token.  The
        # SequenceMatcher map can be non-unique in repetitive prose; compare
        # the mapped fallback compact characters before accepting.
        fallback_token = "".join(
            fallback_alignment[position]
            for position in fallback_positions
        )
        if source_token != fallback_token:
            unresolved_names.add(name)
            continue
        base_rendered = base_text
        font_name = str(
            runs[base_index].get("fontname")
            or runs[base_index].get("font_name")
            or runs[base_index].get("font")
            or ""
        ).upper()
        if "MSBM" in font_name and base_text in blackboard:
            base_rendered = blackboard[base_text]
        replacement = (
            base_rendered
            + render_script("sub", subscript, cluster["roles"]["sub"])
            + render_script("sup", superscript, cluster["roles"]["sup"])
        )
        replacement_end = fallback_end
        # The source text layer keeps an em dash immediately after the script
        # (`n−1/2 — under`), while the fallback commonly glues an ASCII
        # hyphen to the next word (`n -1 / 2 -under`).  Rewrite only this
        # locally adjacent delimiter; never normalize dashes paragraph-wide.
        next_index = max(run_indexes) + 1
        while next_index < len(runs) and not run_text(next_index).strip():
            next_index += 1
        if next_index < len(runs) and run_text(next_index).strip() in {"—", "–"}:
            trailing_delimiter = re.match(
                r"\s*[-−]\s*(?=\w)",
                fallback[fallback_end:],
            )
            if trailing_delimiter:
                replacement += " — "
                replacement_end += trailing_delimiter.end()
        # Some PDF text layers retain an em-dash delimiter around an inline
        # formula while Docling's fallback drops it (``raten -1 / 2``).  Keep
        # the prose boundary visible without copying the punctuation itself.
        previous_index = base_index - 1
        while previous_index >= 0 and not run_text(previous_index).strip():
            previous_index -= 1
        previous_text = run_text(previous_index) if previous_index >= 0 else ""
        if (
            previous_text in {"—", "–"}
            and fallback_start > 0
            and fallback[fallback_start - 1].isalnum()
        ):
            replacement = " " + replacement
        if not replacement or replacement == fallback[fallback_start:fallback_end]:
            continue
        replacements.append((fallback_start, replacement_end, replacement, name))

    # Keep the largest cluster when nested candidates overlap (e.g. × limits
    # followed by X_i) and apply only non-overlapping edits.
    selected: list[tuple[int, int, str, str]] = []
    for replacement in sorted(
        replacements,
        key=lambda value: (-(value[1] - value[0]), value[0]),
    ):
        if any(
            replacement[0] < existing[1] and existing[0] < replacement[1]
            for existing in selected
        ):
            continue
        selected.append(replacement)
    selected_names = {candidate[3] for candidate in selected}
    selected_run_indexes = [
        cluster_run_indexes[name]
        for name in selected_names
        if name in cluster_run_indexes
    ]
    # Nested candidates are useful while deciding an edit, but once an outer
    # cluster owns every glyph in an inner candidate, emitting a second crop
    # would duplicate the same visual evidence.  Mark that occurrence as
    # suppressed and keep it out of unresolved_names; if the outer candidate
    # itself is unresolved no selected set exists and the inner crop remains
    # independently reviewable.
    suppressed_names: set[str] = set()
    for name, indexes in cluster_run_indexes.items():
        if name in selected_names:
            continue
        if any(indexes < selected_indexes for selected_indexes in selected_run_indexes):
            suppressed_names.add(name)
    if cluster_diagnostics is not None:
        for diagnostic in cluster_diagnostics:
            diagnostic_name = str(diagnostic.get("name") or "")
            if diagnostic_name in selected_names:
                diagnostic["resolved"] = True
            elif diagnostic_name in suppressed_names:
                diagnostic["suppressed"] = True
    repaired = fallback
    repaired_names: set[str] = set()
    for start, end, replacement, name in sorted(
        selected,
        key=lambda value: value[0],
        reverse=True,
    ):
        repaired = repaired[:start] + replacement + repaired[end:]
        repaired_names.add(name)
    # Any geometry cluster that did not yield an edit remains explicitly
    # unresolved.  The adapter uses this to retain an exact source crop.
    unresolved_names.update(
        cluster_name(
            run_text(int(cluster["base_index"])),
            "".join(
                run_text(index)
                for index in cluster["roles"]["sup"]
            ),
            "".join(
                run_text(index)
                for index in cluster["roles"]["sub"]
            ),
            int(cluster["base_index"]),
        )
        for cluster in clusters
        if not any(
            candidate[3]
            == cluster_name(
                run_text(int(cluster["base_index"])),
                "".join(run_text(index) for index in cluster["roles"]["sup"]),
                "".join(run_text(index) for index in cluster["roles"]["sub"]),
                int(cluster["base_index"]),
            )
            for candidate in selected
        )
        and cluster_name(
            run_text(int(cluster["base_index"])),
            "".join(run_text(index) for index in cluster["roles"]["sup"]),
            "".join(run_text(index) for index in cluster["roles"]["sub"]),
            int(cluster["base_index"]),
        )
        not in repaired_names
        and cluster_name(
            run_text(int(cluster["base_index"])),
            "".join(
                run_text(index)
                for index in cluster["roles"]["sup"]
            ),
            "".join(
                run_text(index)
                for index in cluster["roles"]["sub"]
            ),
            int(cluster["base_index"]),
        ) not in suppressed_names
        and cluster_name(
            run_text(int(cluster["base_index"])),
            "".join(
                run_text(index)
                for index in cluster["roles"]["sup"]
            ),
            "".join(
                run_text(index)
                for index in cluster["roles"]["sub"]
            ),
            int(cluster["base_index"]),
        ) not in blocked_names
    )
    return repaired, repaired_names, unresolved_names


def _ref_parts(reference: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"#/([^/]+)/(\d+)", reference)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _resolve(document: dict[str, Any], reference: str) -> dict[str, Any] | None:
    parts = _ref_parts(reference)
    if parts is None:
        return None
    collection_name, index = parts
    collection = document.get(collection_name)
    if not isinstance(collection, list) or not 0 <= index < len(collection):
        return None
    node = collection[index]
    return node if isinstance(node, dict) else None


def _first_prov(node: dict[str, Any]) -> dict[str, Any] | None:
    prov = node.get("prov")
    return prov[0] if isinstance(prov, list) and prov and isinstance(prov[0], dict) else None


def _bbox(prov: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(prov, dict) or not isinstance(prov.get("bbox"), dict):
        return None
    value = prov["bbox"]
    return {
        "l": float(value.get("l") or 0.0),
        "r": float(value.get("r") or 0.0),
        "t": float(value.get("t") or 0.0),
        "b": float(value.get("b") or 0.0),
        "coord_origin": str(value.get("coord_origin") or "BOTTOMLEFT"),
    }


def _clean_glyph_text(
    value: str,
    *,
    preserve_unknown_cid: bool = False,
) -> str:
    # CID values are font-private and vary across PDFs.  Do not map them to
    # semantic symbols without glyph evidence; the source crop remains the
    # authoritative visual for an unknown CID.
    replacements = {
        "\x00": "",
        "\x01": "",
    }
    for before, after in replacements.items():
        value = value.replace(before, after)
    if preserve_unknown_cid:
        # Keep the literal spelling in source-backed surfaces.  It is not a
        # semantic symbol, but deleting it would make an unresolved glyph look
        # like ordinary punctuation and hide the need for a source crop.
        return value
    return re.sub(r"\(cid:\d+\)", "", value)


_DETACHED_DIACRITICS = {
    "´": "\u0301",
    "ˇ": "\u030c",
    "˘": "\u0306",
    "¸": "\u0327",
    "¨": "\u0308",
    "˜": "\u0303",
    "ˆ": "\u0302",
    "˙": "\u0307",
}


def _normalize_detached_diacritics(value: str) -> str:
    """Attach PDF-extracted modifier glyphs to their intended base characters."""
    # Leave font-private PL/L glyph spellings untouched.  They are ambiguous
    # across encodings and should be reviewed against the source crop.
    value = re.sub(r"\u0338\s*=", "≠", value)
    value = re.sub(r"\u0338\s*∈", "∉", value)
    value = re.sub(
        r"(?<![A-Za-zΑ-Ωα-ω])\u20d7\s*([A-Za-zΑ-Ωα-ω])",
        lambda match: match.group(1) + "\u20d7",
        value,
    )
    for modifier, combining in _DETACHED_DIACRITICS.items():
        value = re.sub(
            re.escape(modifier) + r"\s*([A-Za-zÀ-ÖØ-öø-ÿ])",
            lambda match, mark=combining: match.group(1) + mark,
            value,
        )
    return unicodedata.normalize("NFC", value)


def _paragraph_text(value: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    merged = lines[0]
    for line in lines[1:]:
        if merged.endswith("-") and line and line[0].islower():
            merged += line
        else:
            merged += " " + line
    merged = re.sub(r"\s+([,.;:!?%)\]])", r"\1", merged)
    merged = re.sub(r"([(\[])\s+", r"\1", merged)
    return _normalize_detached_diacritics(merged.strip())


def _readability_metrics(value: str) -> dict[str, float]:
    """Score an extracted paragraph without assuming a paper-specific alphabet.

    ``SourceReader.text`` is useful when Docling's character spans are stale,
    but a broad PDF crop can also concatenate two columns into alternating
    one-character lines.  The old caller chose that crop whenever the two
    strings were dissimilar, which made otherwise readable body text harder to
    understand.  Keep this gate deliberately structural: it only looks at
    line lengths, printable characters, and opaque CID/control markers.
    """

    text = str(value or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact_lines = [re.sub(r"\s+", "", line) for line in lines]
    visible = re.sub(r"\s+", "", text)
    if not visible:
        return {
            "score": 0.0,
            "line_count": 0.0,
            "single_character_line_ratio": 0.0,
            "short_line_ratio": 0.0,
            "opaque_ratio": 0.0,
        }
    line_count = len(compact_lines)
    one_character = sum(
        1 for line in compact_lines if len(re.sub(r"[^\w\u3400-\u9fff]", "", line)) <= 1
    )
    short_lines = sum(
        1 for line in compact_lines if len(re.sub(r"[^\w\u3400-\u9fff]", "", line)) <= 3
    )
    singleton_ratio = one_character / max(line_count, 1)
    short_ratio = short_lines / max(line_count, 1)
    opaque_count = len(re.findall(r"\(cid:\d+\)|[\x00-\x08\x0b\x0c\x0e-\x1f]", text, re.I))
    opaque_ratio = opaque_count / max(len(visible), 1)
    # A high singleton ratio is a strong cross-column signal.  Do not reject
    # a legitimate short title or a list item on its own; the gate is applied
    # only when competing paragraph candidates are dissimilar.
    cross_column = float(
        (
            line_count >= 4
            and singleton_ratio >= 0.45
            and short_ratio >= 0.65
        )
        # Some PDF text layers interleave two columns as two-character runs
        # rather than single glyphs.  Those crops used to evade the singleton
        # detector and replace a complete Docling character span with dozens
        # of unreadable micro-lines.  A long run dominated by <=3-character
        # lines is the same structural failure mode.
        or (line_count >= 8 and short_ratio >= 0.75)
    )
    alphanumeric = len(re.findall(r"[\w\u3400-\u9fff]", text, re.UNICODE))
    printable_ratio = alphanumeric / max(len(visible), 1)
    score = (
        0.55 * min(1.0, printable_ratio)
        + 0.25 * (1.0 - min(1.0, singleton_ratio))
        + 0.20 * (1.0 - min(1.0, opaque_ratio * 3.0))
        - 0.30 * cross_column
    )
    return {
        "score": max(0.0, min(1.0, score)),
        "line_count": float(line_count),
        "single_character_line_ratio": singleton_ratio,
        "short_line_ratio": short_ratio,
        "opaque_ratio": opaque_ratio,
        "cross_column_suspect": cross_column,
    }


def _choose_readable_source_text(
    clean_slice: str,
    physical_source: str,
    *,
    similarity_threshold: float = 0.45,
) -> tuple[str, dict[str, Any]]:
    """Choose between Docling's clean span and a physical PDF crop.

    A low text similarity is not evidence that the physical crop is better:
    it can simply mean that the crop crossed a column boundary.  Replace a
    clean span only when the physical candidate is materially more readable,
    has no cross-column singleton-line signature, and clears an absolute
    quality floor.  Diagnostics are returned to the caller so a release can
    explain the decision without changing the visible prose.
    """

    clean = str(clean_slice or "").strip()
    physical = str(physical_source or "").strip()
    if not clean:
        metrics = _readability_metrics(physical)
        return physical, {
            "similarity": 1.0,
            "selected": "physical_source",
            "reason": "clean_slice_unavailable",
            "clean": _readability_metrics(clean),
            "physical": metrics,
        }
    if not physical:
        metrics = _readability_metrics(clean)
        return clean, {
            "similarity": 1.0,
            "selected": "clean_slice",
            "reason": "physical_source_unavailable",
            "clean": metrics,
            "physical": _readability_metrics(physical),
        }
    normalized_clean = re.sub(r"\W+", "", clean, flags=re.UNICODE).casefold()
    normalized_physical = re.sub(r"\W+", "", physical, flags=re.UNICODE).casefold()
    similarity = SequenceMatcher(None, normalized_clean, normalized_physical).ratio()
    clean_metrics = _readability_metrics(clean)
    physical_metrics = _readability_metrics(physical)
    diagnostic: dict[str, Any] = {
        "similarity": similarity,
        "selected": "clean_slice",
        "reason": "clean_slice_default",
        "clean": clean_metrics,
        "physical": physical_metrics,
    }
    short_special = bool(
        re.search(
            r"\(cid:\d+\)|\u0338|[\uFFFE\uFFFF]",
            clean,
            flags=re.IGNORECASE,
        )
    )
    # Short Docling spans are often genuine inline identifiers/operators.  A
    # larger physical crop naturally contains more fluent prose and can score
    # better even when it belongs to neighbouring text.  Require a meaningful
    # span length before allowing that replacement; CID/overlay spans retain
    # the stricter equivalence path below.
    short_clean = len(normalized_clean) < 12 or short_special
    special_physical_ok = False
    if short_special:
        cid_match = re.search(r"\(cid:\d+\)", clean, flags=re.IGNORECASE)
        overlay_match = re.search(r"\u0338|[\uFFFE\uFFFF]", clean)
        if cid_match:
            special_physical_ok = bool(
                cid_match.group(0).casefold() in physical.casefold()
                and len(normalized_physical) <= max(10, len(normalized_clean) * 3)
            )
        elif overlay_match:
            special_physical_ok = bool(
                any(symbol in physical for symbol in ("≠", "∉", "̸"))
                and len(normalized_physical) <= max(4, len(normalized_clean) * 3)
            )
    if short_clean and not special_physical_ok:
        diagnostic["reason"] = "short_clean_slice_preserved"
        return clean, diagnostic
    if similarity >= similarity_threshold:
        diagnostic["reason"] = "similarity_above_threshold"
        return clean, diagnostic
    quality_delta = physical_metrics["score"] - clean_metrics["score"]
    physical_usable = (
        physical_metrics["score"] >= 0.58
        and quality_delta >= 0.20
        and not physical_metrics.get("cross_column_suspect")
        and physical_metrics["single_character_line_ratio"] < 0.40
    )
    if physical_usable:
        diagnostic.update(
            selected="physical_source",
            reason="physical_source_materially_more_readable",
        )
        return physical, diagnostic
    if physical_metrics.get("cross_column_suspect"):
        diagnostic["reason"] = "physical_source_rejected_cross_column_singletons"
    elif quality_delta < 0.20:
        diagnostic["reason"] = "physical_source_not_materially_better"
    else:
        diagnostic["reason"] = "physical_source_below_quality_floor"
    return clean, diagnostic


def _quarantine_kind(node: dict[str, Any]) -> str | None:
    label = str(node.get("label") or "").lower()
    if label.startswith("quarantined_"):
        return label.removeprefix("quarantined_")
    qc = node.get("local_ai_lab_qc")
    if isinstance(qc, dict):
        quarantine = qc.get("structural_quarantine")
        if isinstance(quarantine, dict):
            return str(quarantine.get("kind") or "").lower() or None
    return None


def _caption_text(document: dict[str, Any], node: dict[str, Any]) -> str:
    values: list[str] = []
    for item in node.get("captions") or []:
        if not isinstance(item, dict):
            continue
        child = _resolve(document, str(item.get("$ref") or ""))
        text = str((child or {}).get("text") or "").strip()
        if text:
            values.append(text)
    return _paragraph_text(" ".join(values))


def _source_caption(
    source: SourceReader,
    item: FlowItem,
    *,
    kind: str,
) -> str:
    bbox = item.prov.get("bbox")
    if not isinstance(bbox, dict) or item.page_no <= 0:
        return ""
    width, height = source.page_size(item.page_no)
    origin = str(bbox.get("coord_origin") or "BOTTOMLEFT").upper()
    if origin == "TOPLEFT":
        # Docling TOPLEFT boxes use ``t`` for the smaller screen y and ``b``
        # for the larger one.  Convert each edge independently into the PDF
        # BOTTOMLEFT frame before probing caption bands.
        top_y = height - float(bbox.get("t") or 0.0)
        bottom_y = height - float(bbox.get("b") or height)
    else:
        top_y = float(bbox.get("t") or 0.0)
        bottom_y = float(bbox.get("b") or 0.0)
    regions = (
        [(top_y, min(height, top_y + 52.0)), (max(0.0, bottom_y - 62.0), bottom_y)]
        if kind == "table"
        else [(max(0.0, bottom_y - 62.0), bottom_y), (top_y, min(height, top_y + 52.0))]
    )
    label = "Table" if kind == "table" else "Figure"
    for lower, upper in regions:
        if upper <= lower:
            continue
        text = source.text(
            {
                "page_no": item.page_no,
                "bbox": {
                    "l": 0.0,
                    "r": width,
                    "t": upper,
                    "b": lower,
                    "coord_origin": "BOTTOMLEFT",
                },
            }
        )
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not re.match(rf"(?i)^{label}\s+\d+\s*:", line):
                continue
            caption_lines = [line]
            for continuation in lines[index + 1 :]:
                if re.match(
                    r"(?i)^(?:Table|Figure|Algorithm)\s+\d+\s*:",
                    continuation,
                ):
                    break
                caption_lines.append(continuation)
            return _paragraph_text("\n".join(caption_lines))
    return ""


_INLINE_MATH_HEAVY_HINTS = re.compile(
    r"(?i)(?:"
    r"\b[a-z]\s*∈\s*(?:r|ℝ)\b|"
    r"[a-z]\s*[−-]\s*1\s*/\s*2\b|"
    r"\b[a-z]\s*_\s*[a-z0-9]\b|"
    r"\b[a-z]\s*\^\s*\{?[a-z0-9]+|"
    r"\\(?:frac|sum|prod|left|right|vec|alpha|beta|gamma|delta|nabla|operatorname|mathbb|mathbf|mathcal)\b|"
    r"[∑∏√≤≥≠≈≡⊂⊃⊆⊇∩∪↔→←]"
    r")"
)

_INLINE_MATH_HEAVY_SHORT_HINTS = re.compile(
    r"(?i)(?:"
    r"\b[nN]\s*0\b|"
    r"\b[nN]\s*[<>]=?\s*0\b|"
    r"\b[a-zA-Z]\s*_\s*[a-z0-9]+\b|"
    r"\b[a-zA-Z]\s*\^\s*[a-z0-9]+\b|"
    r"\b[a-z]\s*:\s*[a-z]\b|"
    r"\d+[a-zA-Z]\b|"
    r"\b[nN]\s*>\s*0\b|"
    r"\b[nN]\s*<\s*0\b|"
    r"\b[a-zA-Z]\s*[<>]\s*[a-zA-Z0-9]+\b"
    r")"
)

_CJK_INLINE_MATH_HINT_RE = re.compile(
    r"(?ix)"
    r"(?:"
    r"\\(?:frac|sum|prod|int|left|right|vec|log|sin|cos|exp|lim|nabla|alpha|beta|gamma|delta|theta|lambda|pi|rho|tau|math[a-zA-Z]+)\b"
    r"|"
    r"[∑∏√≤≥≠≈≡⊂⊃⊆⊇∩∪↔→←↦]"
    r"|"
    # Arithmetic on a single-letter token (``n - 1``), not hyphenated model
    # names such as ``TCN-KT``.
    r"\b[A-Za-z](?![A-Za-z])\s*[-−]\s*(?:\d|[a-z]|[A-Z])"
    r"|"
    r"\b[A-Za-z]\s+\b[A-Za-z]\b"
    r"|"
    r"\b[A-Z](?![A-Za-z])\s+\d+\b"
    r"|"
    r"ER['\"′’]?\s*[xXdNlNT]"
    r"|"
    r"R['\"′’]?\s*x"
    r"|"
    r"\b[A-Za-z](?![A-Za-z])\s*[_^]\s*[A-Za-z0-9]"
    r"|"
    r"\b[A-Z](?![A-Za-z])\s+\d+\b"
    r"|"
    r"\b[nm](?![A-Za-z])\s*[-−]\s*1/2\b"
    r")"
)


def _source_math_font_evidence(char: dict[str, Any]) -> bool:
    font_name = str(
        char.get("fontname")
        or char.get("font_name")
        or char.get("font")
        or ""
    ).upper()
    if any(hint in font_name for hint in _INLINE_MATH_MATH_FONT_HINTS):
        return True
    symbol = str(char.get("text") or "")
    normalized_symbol = symbol.strip().lower()
    return (
        any(sym in symbol for sym in _MATH_SYMBOL_CHARS)
        or normalized_symbol.startswith("cid:")
        or "(cid:" in normalized_symbol
    )


def _cjk_inline_math_source_evidence(
    node_text: str,
    chars: list[dict[str, Any]],
) -> bool:
    """Conservatively confirm math intent from local glyph evidence."""
    if "(cid:" in node_text:
        return True
    compact = [
        {
            "text": str(char.get("text") or ""),
            "bbox": char.get("bbox") if isinstance(char.get("bbox"), dict) else {},
            "size": _inline_geometry_char_size(char),
        }
        for char in chars
        if str(char.get("text") or "").strip()
    ]
    if not compact:
        return False
    if any(_source_math_font_evidence(char) for char in chars):
        return True
    for char in compact:
        if re.search(r"[∑∏√≤≥≠≈≡⊂⊃⊆∪↔→←↦]", char["text"]):
            return True
    if _has_script_like_layout(compact):
        return True
    # Two ASCII glyphs next to a digit are not enough evidence: model names
    # (`TCN-KT`, `GKT 10`) and batch-size prose are common in CJK papers.
    # Without a math font, explicit operator, CID, or script-like geometry,
    # leave the node untouched and do not manufacture an appendix crop.
    return False


def _has_script_like_layout(chars: list[dict[str, Any]]) -> bool:
    """Detect a local sub/superscript-like layout pattern from glyph boxes."""
    normalized = [
        (
            float((char.get("bbox") or {}).get("l") or 0.0),
            float((char.get("bbox") or {}).get("r") or 0.0),
            float((char.get("bbox") or {}).get("t") or 0.0),
            float((char.get("bbox") or {}).get("b") or 0.0),
            float((char.get("bbox") or {}).get("r") or 0.0)
            - float((char.get("bbox") or {}).get("l") or 0.0),
            char.get("size") if isinstance(char.get("size"), (float, int)) else 0.0,
            char.get("text", ""),
        )
        for char in chars
        if isinstance(char.get("bbox"), dict)
        and (char.get("text") or "").strip()
    ]
    if len(normalized) < 2:
        return False
    normalized.sort(key=lambda value: value[0])
    baseline_sizes = [size for *_, size, _text in normalized if size > 0.0]
    if not baseline_sizes:
        return False
    baseline = median(baseline_sizes)
    for current, nxt in zip(normalized, normalized[1:]):
        current_left, current_right, current_t, current_b, current_w, current_size, current_text = current
        next_left, next_right, next_t, next_b, next_w, next_size, next_text = nxt
        if not current_w or not next_w:
            continue
        if (
            next_left - current_left <= 0
            or next_left - current_right > max(current_w, next_w) * 1.8
        ):
            continue
        current_center = (current_t + current_b) / 2.0
        next_center = (next_t + next_b) / 2.0
        if not re.search(r"[A-Za-z\d]", current_text + next_text):
            continue
        delta = abs(next_center - current_center)
        if (
            (current_size <= baseline * 0.88 and delta >= baseline * 0.16)
            or (next_size <= baseline * 0.88 and delta >= baseline * 0.16)
            or (next_size <= baseline * 0.86 and delta >= baseline * 0.12)
        ):
            return True
    return False


def _looks_math_heavy(value: str) -> bool:
    preserved = _strip_inline_math_control_text(value)
    if not preserved:
        return False
    compact = _normalize_formula_similarity_text(value)
    if not compact:
        return False
    if _INLINE_MATH_HEAVY_SHORT_HINTS.search(preserved):
        return True
    if _INLINE_MATH_HEAVY_HINTS.search(preserved):
        return True
    if len(re.sub(r"\W+", "", compact)) < 4:
        return False
    if sum(ch in _MATH_SYMBOL_CHARS for ch in preserved) >= 2:
        return True
    if re.search(r"\b\w\s*[_^]\s*\w", preserved):
        return True
    return False


def _inline_math_anchor_id(
    *,
    page_no: int,
    collection: str,
    index: int,
    offset: int,
    part_index: int = 0,
    bbox: dict[str, float],
) -> str:
    origin = str(bbox.get("coord_origin") or "BOTTOMLEFT").upper()
    vertical_extent = abs(float(bbox.get("t") or 0.0) - float(bbox.get("b") or 0.0))
    vertical_position = float(bbox.get("t") or 0.0)
    return (
        f"inline-math-chunk{part_index}-{collection}-{index}-{offset}-"
        f"p{int(page_no)}"
        f"-x{int(bbox['l'])}-y{int(vertical_position)}-"
        f"w{int(abs(float(bbox['r']) - float(bbox['l'])))}-"
        f"h{int(vertical_extent)}"
        + ("-tl" if origin == "TOPLEFT" else "")
    )


class SourceReader:
    def __init__(self, path: Path):
        try:
            import pdfplumber  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "semantic source reconstruction requires pdfplumber"
            ) from exc
        self._pdf = pdfplumber.open(str(path))
        self._pypdf = None
        self._pypdf_page_count: int | None = None
        self._pypdfium_char_cache: dict[int, list[dict[str, Any]]] = {}
        self._math_aware_diagnostics: dict[
            tuple[int, float, float, float, float], dict[str, Any]
        ] = {}
        try:
            import pypdfium2  # type: ignore

            self._pypdf = pypdfium2.PdfDocument(str(path))
            self._pypdf_page_count = int(len(self._pypdf))
        except Exception:
            self._pypdf = None
            self._pypdf_page_count = None

    def close(self) -> None:
        self._pdf.close()
        if self._pypdf is not None:
            try:
                self._pypdf.close()
            except Exception:
                pass
        self._pypdfium_char_cache.clear()
        self._math_aware_diagnostics.clear()

    @staticmethod
    def _math_diagnostic_key(prov: dict[str, Any]) -> tuple[int, float, float, float, float]:
        bbox = prov.get("bbox") if isinstance(prov, dict) else None
        if not isinstance(bbox, dict):
            bbox = {}
        return (
            int(prov.get("page_no") or 0) if isinstance(prov, dict) else 0,
            round(float(bbox.get("l") or 0.0), 2),
            round(float(bbox.get("r") or 0.0), 2),
            round(float(bbox.get("t") or 0.0), 2),
            round(float(bbox.get("b") or 0.0), 2),
        )

    @staticmethod
    def _normalize_bbox_for_math_order(value: dict[str, Any]) -> tuple[float, float, float, float]:
        origin = str(value.get("coord_origin") or "BOTTOMLEFT").upper()
        page_height = float(value.get("page_height") or 0.0)
        top_value = float(value.get("t") or 0.0)
        bottom_value = float(value.get("b") or 0.0)
        if origin == "TOPLEFT":
            if page_height <= 0.0:
                return (
                    float(value.get("l") or 0.0),
                    float(value.get("r") or 0.0),
                    top_value,
                    bottom_value,
                )
            # Convert top-left (distance from top) to the pypdfium bottom-left
            # coordinate convention before comparing vertical intervals.
            top_value, bottom_value = (
                page_height - min(top_value, bottom_value),
                page_height - max(top_value, bottom_value),
            )
        return (
            float(value.get("l") or 0.0),
            float(value.get("r") or 0.0),
            top_value,
            bottom_value,
        )

    def _math_font_evidence(self, char: dict[str, Any] | Any) -> bool:
        if not isinstance(char, dict):
            return False
        font_name = str(
            char.get("fontname")
            or char.get("font_name")
            or char.get("font")
            or ""
        ).upper()
        if any(hint in font_name for hint in _INLINE_MATH_MATH_FONT_HINTS):
            return True
        symbol = str(char.get("text") or char.get("char") or "")
        return any(sym in symbol for sym in _MATH_SYMBOL_CHARS)

    def _pypdfium_characters(
        self,
        page_no: int,
        bbox: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self._pypdf is None:
            return []
        if not self._pypdf_page_count or not (1 <= page_no <= self._pypdf_page_count):
            return []
        if page_no not in self._pypdfium_char_cache:
            self._pypdfium_char_cache[page_no] = self._extract_pypdfium_page_chars(page_no)
        chars = self._pypdfium_char_cache.get(page_no, [])
        if bbox is None:
            return chars
        page_height = 0.0
        if str((bbox or {}).get("coord_origin") or "BOTTOMLEFT").upper() == "TOPLEFT":
            try:
                _page_width, page_height = self.page_size(page_no)
            except Exception:
                page_height = 0.0
            if page_height <= 0.0:
                return []
        return [
            item
            for item in chars
            if self._intersects(item.get("bbox"), bbox, page_height=page_height)
        ]

    def _inline_math_span_evidence(
        self,
        prov: dict[str, Any],
        runs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Find fraction/control spans that must remain source-crop authoritative.

        A pypdfium text run does not expose the horizontal rule drawn by a
        fraction.  pdfplumber does expose that rule as a page ``line`` object,
        while CMEX private delimiter codes remain visible in the glyph stream.
        We use both signals and expand each seed to the nearby glyphs so a
        crop contains the complete numerator/denominator and paired delimiters
        rather than a single ``P``/``j`` cluster.
        """
        page_no = int(prov.get("page_no") or 0)
        bbox = prov.get("bbox")
        if not page_no or not isinstance(bbox, dict):
            return []
        try:
            page = self._pdf.pages[page_no - 1]
            page_height = float(page.height or 0.0)
        except Exception:
            return []
        if page_height <= 0.0:
            return []
        origin = str(bbox.get("coord_origin") or "BOTTOMLEFT").upper()
        raw_t = float(bbox.get("t") or 0.0)
        raw_b = float(bbox.get("b") or 0.0)
        if origin == "TOPLEFT":
            probe_top = min(raw_t, raw_b)
            probe_bottom = max(raw_t, raw_b)
        else:
            probe_top = page_height - max(raw_t, raw_b)
            probe_bottom = page_height - min(raw_t, raw_b)
        probe_left = float(bbox.get("l") or 0.0)
        probe_right = float(bbox.get("r") or 0.0)

        def overlap_xy(left: float, right: float, top: float, bottom: float) -> bool:
            return not (
                right < probe_left
                or probe_right < left
                or bottom < probe_top
                or probe_bottom < top
            )

        positive_sizes = [
            _inline_geometry_char_size(run)
            for run in runs
            if _inline_geometry_char_size(run) > 0.0
        ]
        if positive_sizes:
            # Use the upper half as the baseline so a fraction with several
            # reduced numerator/denominator glyphs does not make its own
            # script tier look full-size (or weaken the small-script gate).
            ordered_sizes = sorted(positive_sizes)
            typical_size = median(ordered_sizes[len(ordered_sizes) // 2 :])
        else:
            typical_size = 8.0
        spans: list[dict[str, Any]] = []

        # Fraction rules are vector line objects, not glyphs.  Ignore tiny
        # decoration rules and require a meaningful horizontal extent inside
        # the provenance box.
        try:
            page_lines = list(page.lines or [])
        except Exception:
            page_lines = []
        for line_index, line in enumerate(page_lines):
            try:
                x0 = float(line.get("x0") or 0.0)
                x1 = float(line.get("x1") or 0.0)
                y0 = float(line.get("top") or 0.0)
                y1 = float(line.get("bottom") or y0)
            except (TypeError, ValueError):
                continue
            left, right = sorted((x0, x1))
            top, bottom = sorted((y0, y1))
            if right - left < max(6.0, typical_size * 0.65):
                continue
            # A fraction bar is horizontal.  ``page.lines`` normally has
            # identical top/bottom; allow a small stroke thickness for PDFs
            # that report a non-zero line height.
            if bottom - top > max(1.0, typical_size * 0.12):
                continue
            if not overlap_xy(left, right, top, bottom):
                continue
            line_y_bottom_left = page_height - (top + bottom) / 2.0
            # Include the numerator/denominator and delimiter pair, but do
            # not absorb an entire prose line that happens to share the
            # fraction's baseline.
            expand_x = max(24.0, typical_size * 2.8)
            # Keep the fraction's nearest numerator/denominator tiers.  The
            # previous 2.2× radius could pull an adjacent subscript (for
            # example C_b/P_n) into an otherwise tight dQ/dP crop.  A 1.5×
            # radius plus nearest-layer selection is sufficient for the
            # observed fraction scripts while rejecting neighbouring prose.
            expand_y = max(6.0, typical_size * 1.5)
            candidate_runs: list[dict[str, Any]] = []
            for run in runs:
                run_box = run.get("bbox")
                if not isinstance(run_box, dict):
                    continue
                run_left = float(run_box.get("l") or 0.0)
                run_right = float(run_box.get("r") or 0.0)
                run_top = float(run_box.get("t") or 0.0)
                run_bottom = float(run_box.get("b") or 0.0)
                run_center = (run_top + run_bottom) / 2.0
                run_text = str(run.get("text") or "").strip()
                run_is_math = self._math_font_evidence(run)
                run_is_operator = run_text in {
                    "=", "−", "-", "/", "(", ")", "[", "]", ",", ":"
                }
                local_overlap = (
                    run_right >= left - 8.0 and run_left <= right + 8.0
                )
                nearby_math = (
                    run_right >= left - 20.0
                    and run_left <= right + 20.0
                    and (run_is_math or run_is_operator)
                )
                if (
                    (local_overlap or nearby_math)
                    and abs(run_center - line_y_bottom_left) <= expand_y
                ):
                    candidate_runs.append(run)
            local_sizes = [
                _inline_geometry_char_size(run)
                for run in candidate_runs
                if _inline_geometry_char_size(run) > 0.0
            ]
            local_math_sizes = [
                _inline_geometry_char_size(run)
                for run in candidate_runs
                if _inline_geometry_char_size(run) > 0.0
                and (
                    self._math_font_evidence(run)
                    or _inline_geometry_char_size(run) <= typical_size * 0.92
                )
            ]
            vertical_size = (
                median(local_math_sizes)
                if local_math_sizes
                else median(local_sizes)
                if local_sizes
                else typical_size
            )
            expand_y = max(6.0, vertical_size * 1.35)
            candidate_runs = [
                run
                for run in candidate_runs
                if abs(
                    _inline_geometry_char_center_y(run) - line_y_bottom_left
                ) <= expand_y
            ]
            # A true fraction rule has a math-like glyph on both sides of the
            # line (numerator above and denominator below in bottom-left PDF
            # coordinates).  Overbars/underlines only have glyphs on one side;
            # rejecting them avoids manufacturing unresolved fraction crops
            # for ordinary notation such as C̄_b or a vector accent.
            def side_evidence(run: dict[str, Any], *, above: bool) -> bool:
                run_box = run.get("bbox")
                if not isinstance(run_box, dict):
                    return False
                run_left = float(run_box.get("l") or 0.0)
                run_right = float(run_box.get("r") or 0.0)
                if run_right < left - 3.0 or run_left > right + 3.0:
                    return False
                run_center = _inline_geometry_char_center_y(run)
                delta = run_center - line_y_bottom_left
                if above and delta <= 1.0:
                    return False
                if not above and delta >= -1.0:
                    return False
                run_text = str(run.get("text") or "").strip()
                run_size = _inline_geometry_char_size(run)
                run_is_small = run_size > 0.0 and run_size <= typical_size * 0.92
                return bool(
                    self._math_font_evidence(run)
                    or run_is_small
                    or run_text in {"=", "−", "-", "/", "(", ")", ",", ":"}
                )

            def nearest_side_runs(*, above: bool) -> list[dict[str, Any]]:
                side = [
                    run
                    for run in candidate_runs
                    if (
                        _inline_geometry_char_center_y(run) - line_y_bottom_left > 1.0
                        if above
                        else _inline_geometry_char_center_y(run) - line_y_bottom_left < -1.0
                    )
                ]
                if not side:
                    return []
                reduced = [
                    run
                    for run in side
                    if 0.0 < _inline_geometry_char_size(run) <= typical_size * 0.92
                ]
                # Prefer the nearest reduced script tier when one exists. A
                # full-size neighbouring glyph can still belong to the same
                # fraction (P e S / j), so add only close context around that
                # tier; distant C_b/P_n scripts remain outside the crop.
                pool = reduced or side
                nearest = min(
                    abs(_inline_geometry_char_center_y(run) - line_y_bottom_left)
                    for run in pool
                )
                layer_tolerance = max(1.0, typical_size * 0.22)
                selected = [
                    run
                    for run in pool
                    if abs(
                        abs(_inline_geometry_char_center_y(run) - line_y_bottom_left)
                        - nearest
                    )
                    <= layer_tolerance
                ]
                selected_centers = [
                    _inline_geometry_char_center_y(run) for run in selected
                ]
                context_tolerance = max(2.0, typical_size * 0.70)
                for run in side:
                    if run in selected:
                        continue
                    center = _inline_geometry_char_center_y(run)
                    if any(
                        abs(center - selected_center) <= context_tolerance
                        for selected_center in selected_centers
                    ):
                        selected.append(run)
                return selected

            selected_runs = [
                run
                for run in candidate_runs
                if abs(_inline_geometry_char_center_y(run) - line_y_bottom_left) <= 1.0
            ]
            selected_runs.extend(nearest_side_runs(above=True))
            selected_runs.extend(nearest_side_runs(above=False))

            if not any(side_evidence(run, above=True) for run in selected_runs):
                continue
            if not any(side_evidence(run, above=False) for run in selected_runs):
                continue
            # In these inline text crops a drawn overbar typically sits over
            # two full-size prose/math glyphs.  Fraction numerators and
            # denominators carry the reduced script tier on both sides; use
            # that size evidence to reject accent bars without excluding the
            # observed dQ/dP, e−α/4, and BERT eS·Ti/PjeS·Tj fractions.
            if not any(
                side_evidence(run, above=True)
                and 0.0 < _inline_geometry_char_size(run) <= typical_size * 0.92
                for run in selected_runs
            ):
                continue
            if not any(
                side_evidence(run, above=False)
                and 0.0 < _inline_geometry_char_size(run) <= typical_size * 0.92
                for run in selected_runs
            ):
                continue
            line_box = {
                "l": left,
                "r": right,
                "t": line_y_bottom_left + max(0.5, typical_size * 0.06),
                "b": line_y_bottom_left - max(0.5, typical_size * 0.06),
                "coord_origin": "BOTTOMLEFT",
            }
            span_box = _inline_geometry_bbox_union(
                [line_box]
                + [
                    run.get("bbox")
                    for run in selected_runs
                    if isinstance(run.get("bbox"), dict)
                ]
            )
            if span_box is None:
                continue
            # Keep two geometries for a fraction occurrence.  ``bbox`` is the
            # visual source crop and may include a small amount of surrounding
            # context (for example the preceding ``P_n`` or a line-ending
            # comma) so a reviewer can orient themselves.  That crop must not
            # also become the repair suppression region: a neighboring script
            # can touch its edge even though it is not part of the fraction.
            # Restrict the repair block to glyphs that actually overlap the
            # horizontal rule, plus the rule itself.  The source crop remains
            # unchanged and authoritative for the unresolved fraction.
            repair_runs = [
                run
                for run in selected_runs
                if isinstance(run.get("bbox"), dict)
                and float(run.get("bbox", {}).get("r") or 0.0) >= left - 0.5
                and float(run.get("bbox", {}).get("l") or 0.0) <= right + 0.5
            ]
            repair_box = _inline_geometry_bbox_union(
                [line_box]
                + [
                    run.get("bbox")
                    for run in repair_runs
                    if isinstance(run.get("bbox"), dict)
                ]
            ) or line_box
            spans.append(
                {
                    "reason": "fraction_rule",
                    "index": line_index,
                    "bbox": span_box,
                    "repair_bbox": repair_box,
                    "source_text": "".join(
                        _inline_geometry_preserve_private_text(str(run.get("text") or ""))
                        for run in selected_runs
                    ),
                }
            )

        # CMEX control glyphs denote large delimiters and are not semantically
        # recoverable from the private code.  Build one tight span per control
        # occurrence, retaining nearby numerator/denominator glyphs.
        for run_index, run in enumerate(runs):
            if not _inline_math_control_character(run):
                continue
            run_box = run.get("bbox")
            if not isinstance(run_box, dict):
                continue
            run_left = float(run_box.get("l") or 0.0)
            run_right = float(run_box.get("r") or 0.0)
            run_center = _inline_geometry_char_center_y(run)
            run_size = _inline_geometry_char_size(run) or typical_size
            expand_x = max(12.0, run_size * 1.5)
            expand_y = max(10.0, run_size * 1.3)
            selected_runs = []
            for candidate in runs:
                candidate_box = candidate.get("bbox")
                if not isinstance(candidate_box, dict):
                    continue
                candidate_left = float(candidate_box.get("l") or 0.0)
                candidate_right = float(candidate_box.get("r") or 0.0)
                candidate_center = _inline_geometry_char_center_y(candidate)
                if (
                    candidate_right >= run_left - expand_x
                    and candidate_left <= run_right + expand_x
                    and abs(candidate_center - run_center) <= expand_y
                ):
                    selected_runs.append(candidate)
            span_box = _inline_geometry_bbox_union(
                [run_box]
                + [
                    candidate.get("bbox")
                    for candidate in selected_runs
                    if isinstance(candidate.get("bbox"), dict)
                ]
            )
            if span_box is None:
                continue
            spans.append(
                {
                    "reason": "cmex_control",
                    "index": run_index,
                    "bbox": span_box,
                    "source_text": "".join(
                        _inline_geometry_preserve_private_text(str(candidate.get("text") or ""))
                        for candidate in selected_runs
                    ),
                }
            )

        # Merge only overlapping spans from the same continuous math object;
        # independent controls on one paragraph remain occurrence-specific.
        merged: list[dict[str, Any]] = []
        for span in sorted(spans, key=lambda value: (
            float((value.get("bbox") or {}).get("l") or 0.0),
            float((value.get("bbox") or {}).get("b") or 0.0),
        )):
            prior_reason = str(merged[-1].get("reason") or "") if merged else ""
            span_reason = str(span.get("reason") or "")
            # Merge overlapping rules from one fraction object, but keep
            # independent CMEX delimiter occurrences occurrence-specific.
            merge_allowed = prior_reason == span_reason == "cmex_control"
            if merge_allowed:
                prior_box = merged[-1].get("bbox") or {}
                current_box = span.get("bbox") or {}
                prior_width = max(
                    0.0,
                    float(prior_box.get("r") or 0.0)
                    - float(prior_box.get("l") or 0.0),
                )
                current_width = max(
                    0.0,
                    float(current_box.get("r") or 0.0)
                    - float(current_box.get("l") or 0.0),
                )
                overlap_width = max(
                    0.0,
                    min(
                        float(prior_box.get("r") or 0.0),
                        float(current_box.get("r") or 0.0),
                    )
                    - max(
                        float(prior_box.get("l") or 0.0),
                        float(current_box.get("l") or 0.0),
                    ),
                )
                merge_allowed = overlap_width >= max(
                    1.0, min(prior_width, current_width) * 0.5
                )
            if (
                merged
                and merge_allowed
                and _inline_geometry_bbox_intersects(
                    merged[-1].get("bbox"), span.get("bbox")
                )
            ):
                prior = merged[-1]
                prior["bbox"] = _inline_geometry_bbox_union(
                    [prior.get("bbox") or {}, span.get("bbox") or {}]
                )
                prior["source_text"] = (
                    str(prior.get("source_text") or "")
                    + str(span.get("source_text") or "")
                )
                prior["reason"] = "+".join(
                    sorted(
                        set(
                            str(prior.get("reason") or "").split("+")
                            + [str(span.get("reason") or "")]
                        )
                    )
                )
                continue
            merged.append(dict(span))
        for occurrence, span in enumerate(merged):
            bbox_value = span.get("bbox") or {}
            span["name"] = (
                f"geometry_math_span-{span.get('reason') or 'unknown'}-"
                f"p{page_no}-occ{occurrence}-"
                f"x{int(float(bbox_value.get('l') or 0.0))}"
            )
        return merged

    def _extract_pypdfium_page_chars(self, page_no: int) -> list[dict[str, Any]]:
        if self._pypdf is None:
            return []
        result: list[dict[str, Any]] = []
        page = self._pypdf[page_no - 1]
        textpage = None
        try:
            textpage = page.get_textpage()
        except Exception:
            return []
        try:
            count = int(textpage.count_chars())
        except Exception:
            return []
        try:
            for index in range(count):
                try:
                    char_text = textpage.get_text_range(index, 1)
                    char_text = "" if char_text is None else str(char_text)
                except Exception:
                    continue
                if isinstance(char_text, bytes):
                    char_text = char_text.decode("utf-8", "ignore")
                char_text = _inline_geometry_preserve_private_text(char_text)
                if not char_text:
                    continue
                try:
                    info = textpage.get_charbox(index)
                except Exception:
                    info = None
                textobj = None
                try:
                    textobj = textpage.get_textobj(index)
                except Exception:
                    pass
                record = dict(
                    {
                        "text": char_text,
                        "bbox": self._extract_char_bbox(info, page_no, index),
                    }
                )
                if textobj is not None:
                    try:
                        font = textobj.get_font()
                    except Exception:
                        font = None
                    if font is not None:
                        try:
                            base_name = font.get_base_name()
                        except Exception:
                            base_name = ""
                        else:
                            record["fontname"] = str(base_name or "").upper()
                    try:
                        record["size"] = textobj.get_font_size()
                    except Exception:
                        pass
                if isinstance(info, dict):
                    for key in ("fontname", "fontname_id", "size", "x", "y", "top", "bottom"):
                        if key in info:
                            record[key] = info[key]
                if isinstance(info, (list, tuple)) and len(info) >= 4:
                    text_x, text_bottom, text_r, text_t = info[:4]
                    record["x"] = float(text_x or 0.0)
                    record["y"] = float(text_bottom or 0.0)
                    record["bbox"] = {
                        "l": float(info[0] or 0.0),
                        "r": float(info[2] or 0.0),
                        "t": float(info[3] or 0.0),
                        "b": float(text_bottom or 0.0),
                    }
                if not record.get("fontname"):
                    record["fontname"] = (
                        str(info.get("fontname") or info.get("font_name") or "")
                        if isinstance(info, dict)
                        else ""
                    )
                if isinstance(info, dict):
                    record["size"] = info.get("size") or info.get("font_size")
                result.append(record)
        finally:
            try:
                textpage.close()
            except Exception:
                pass
        return result

    def inline_math_evidence(self, prov: dict[str, Any]) -> bool:
        diagnostic = self._math_aware_diagnostics.get(self._math_diagnostic_key(prov))
        if isinstance(diagnostic, dict):
            return bool(diagnostic.get("anchor"))
        page_no = int(prov.get("page_no") or 0)
        bbox = prov.get("bbox")
        if not page_no or not isinstance(bbox, dict):
            return False
        chars = self._pypdfium_characters(page_no, bbox)
        if not chars:
            return False
        explicit_math_symbol = re.compile(
            r"\\(?:frac|sum|prod|int|left|right|vec|math[a-zA-Z]+|nabla|operatorname|sqrt|sin|cos|log|lim|exp)\b|"
            r"[∑∏∫√≤≥≠≈≡⊂⊃⊆⊇∩∪↔→←]"
        )
        math_font_glyphs = 0
        for char in chars:
            if self._math_font_evidence(char):
                math_font_glyphs += 1
            if explicit_math_symbol.search(str(char.get("text") or "")):
                return True
        # Without a text-fallback comparison we cannot prove a script loss;
        # explicit overlay glyphs are still reliable source evidence.  Avoid
        # treating every paragraph containing two math-font letters as an
        # inline widget.
        return math_font_glyphs >= 2 and any(
            str(char.get("text") or "") in {"̸", "≠", "∉"}
            for char in chars
        )

    @staticmethod
    def _extract_char_bbox(
        info: dict[str, Any] | tuple[Any, ...] | None,
        page_no: int,
        index: int,
    ) -> dict[str, float]:
        if isinstance(info, dict) and isinstance(info.get("bbox"), dict):
            return {
                "l": float(info["bbox"].get("l") or 0.0),
                "r": float(info["bbox"].get("r") or 0.0),
                "t": float(info["bbox"].get("t") or 0.0),
                "b": float(info["bbox"].get("b") or 0.0),
            }
        if isinstance(info, (list, tuple)) and len(info) >= 4:
            return {
                "l": float(info[0] or 0.0),
                "r": float(info[2] or 0.0),
                "t": float(info[3] or 0.0),
                "b": float(info[1] or 0.0),
            }
        return {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0}

    def math_aware_text(
        self,
        prov: dict[str, Any],
        fallback: str,
        *,
        similarity_threshold: float = 0.55,
        allow_text_script_base: bool = False,
    ) -> str:
        page_no = int(prov.get("page_no") or 0)
        bbox = prov.get("bbox")
        if not page_no or not isinstance(bbox, dict):
            return fallback
        runs = self._pypdfium_characters(page_no, bbox)
        if not runs:
            return fallback
        if (
            not allow_text_script_base
            and not any(self._math_font_evidence(char) for char in runs)
        ):
            return fallback
        runs = [
            {
                **run,
                "text": _inline_geometry_preserve_private_text(str(run.get("text") or "")),
            }
            for run in runs
            if str(run.get("text") or "") != ""
        ]
        if not runs:
            return fallback

        widths = [
            max(1.0, float((run.get("bbox") or {}).get("r") or 0.0)
                    - float((run.get("bbox") or {}).get("l") or 0.0))
            for run in runs
            if str((run.get("text") or "").strip())
            and isinstance(run.get("bbox"), dict)
        ]
        average_width = median(widths) if widths else 4.5
        average_width = max(average_width, 1.0)

        candidate_parts: list[str] = []
        previous_bbox: dict[str, float] | None = None
        previous_text = ""
        for run in runs:
            text_value = str(run.get("text") or "")
            if not text_value:
                continue
            if text_value.isspace():
                candidate_parts.append(text_value)
                if isinstance(run.get("bbox"), dict):
                    previous_bbox = run.get("bbox")
                previous_text = text_value
                continue

            if previous_text and previous_text.strip() and previous_bbox:
                current_bbox = run.get("bbox")
                if isinstance(current_bbox, dict):
                    gap = float(current_bbox.get("l") or 0.0) - float(previous_bbox.get("r") or 0.0)
                    is_wrap = gap < -average_width * 0.4
                    if is_wrap:
                        candidate_parts.append(" ")

            candidate_parts.append(text_value)
            if isinstance(run.get("bbox"), dict):
                previous_bbox = run.get("bbox")
            previous_text = text_value

        candidate = "".join(candidate_parts)
        candidate = re.sub(r"[ \t]+", " ", candidate).strip()
        if not candidate:
            return fallback
        # Fraction rules and CMEX private delimiters mark a continuous visual
        # math span.  Partial script edits inside that span would leave the
        # numerator/denominator or paired delimiter unreadable, so retain the
        # fallback text and attach one source crop per occurrence instead.
        span_evidence = self._inline_math_span_evidence(prov, runs)
        span_diagnostics = [
            {
                "name": str(span.get("name") or "geometry_math_span-unknown"),
                "bbox": span.get("bbox"),
                "repair_bbox": span.get("repair_bbox") or span.get("bbox"),
                "source_text": str(span.get("source_text") or ""),
                "reason": str(span.get("reason") or "math_span"),
                "resolved": False,
            }
            for span in span_evidence
            if isinstance(span.get("bbox"), dict)
        ]
        blocked_bboxes = [
            item.get("repair_bbox") or item["bbox"]
            for item in span_diagnostics
            if isinstance(item.get("repair_bbox") or item.get("bbox"), dict)
        ]
        fallback_normalized = _normalize_formula_similarity_text(fallback)
        if fallback_normalized:
            if SequenceMatcher(
                None,
                _normalize_formula_similarity_text(candidate),
                fallback_normalized,
            ).ratio() < similarity_threshold:
                if span_diagnostics:
                    self._math_aware_diagnostics[self._math_diagnostic_key(prov)] = {
                        "repaired_names": [],
                        "unresolved_names": sorted(
                            str(item["name"]) for item in span_diagnostics
                        ),
                        "unresolved_clusters": span_diagnostics,
                        "math_span_regions": span_diagnostics,
                        "anchor": True,
                    }
                return fallback
        cluster_diagnostics: list[dict[str, Any]] = []
        repaired, repaired_names, unresolved_names = _inline_geometry_repair(
            fallback,
            runs,
            math_font_evidence=self._math_font_evidence,
            cluster_diagnostics=cluster_diagnostics,
            blocked_bboxes=blocked_bboxes,
            allow_text_script_base=allow_text_script_base,
        )
        repaired = _repair_source_comparison_operators(repaired, candidate)
        if span_diagnostics:
            cluster_diagnostics.extend(span_diagnostics)
            unresolved_names.update(
                str(item["name"]) for item in span_diagnostics
            )
        # Isolated italic S in a comma-delimited notation list is a known
        # low-confidence overbar case.  We deliberately do not invent the bar;
        # retain a source visual anchor instead.
        if re.search(r"(?:^|[,;])\s*S\s*(?:[,;])", fallback):
            if any(
                str(run.get("text") or "") == "S"
                and "CMMI" in str(run.get("fontname") or "").upper()
                for run in runs
            ):
                unresolved_names.add("possible_overbar_S")
        diagnostic = {
            "repaired_names": sorted(repaired_names),
            "unresolved_names": sorted(unresolved_names),
            "unresolved_clusters": [
                item
                for item in cluster_diagnostics
                if not item.get("resolved")
                and not item.get("suppressed")
                and item.get("bbox")
            ],
            "math_span_regions": span_diagnostics,
            "anchor": bool(repaired_names or unresolved_names),
        }
        self._math_aware_diagnostics[self._math_diagnostic_key(prov)] = diagnostic
        return repaired

    def _intersects(
        self,
        bbox: dict[str, float] | None,
        probe: dict[str, Any] | None,
        *,
        page_height: float = 0.0,
    ) -> bool:
        if not isinstance(bbox, dict):
            return True
        if not isinstance(probe, dict):
            return True
        left, right, top, bottom = (
            float(bbox.get("l") or 0.0),
            float(bbox.get("r") or 0.0),
            float(bbox.get("t") or 0.0),
            float(bbox.get("b") or 0.0),
        )
        probe_left, probe_right, probe_top, probe_bottom = SourceReader._normalize_bbox_for_math_order(
            {
                "l": probe.get("l"),
                "r": probe.get("r"),
                "t": probe.get("t"),
                "b": probe.get("b"),
                "coord_origin": probe.get("coord_origin"),
                "page_height": page_height,
            }
        )
        return not (
            right < probe_left
            or probe_right < left
            or top < probe_bottom
            or probe_top < bottom
        )

    def language_profile(self, *, page_limit: int = 3) -> dict[str, int]:
        text = "\n".join(
            str(page.extract_text() or "")
            for page in self._pdf.pages[:page_limit]
        )
        return {
            "characters": len(text),
            "cjk_characters": len(re.findall(r"[\u3400-\u9fff]", text)),
            "latin_characters": len(re.findall(r"[A-Za-z]", text)),
        }

    def page_size(self, page_no: int) -> tuple[float, float]:
        page = self._pdf.pages[page_no - 1]
        return float(page.width), float(page.height)

    def _crop_box(
        self,
        page_no: int,
        bbox: dict[str, Any],
        *,
        padding: float = 0.0,
    ) -> tuple[float, float, float, float]:
        page = self._pdf.pages[page_no - 1]
        origin = str(bbox.get("coord_origin") or "BOTTOMLEFT").upper()
        left = float(bbox.get("l") or 0.0)
        right = float(bbox.get("r") or 0.0)
        if origin == "TOPLEFT":
            top = float(bbox.get("t") or 0.0)
            bottom = float(bbox.get("b") or 0.0)
        else:
            top = float(page.height) - float(bbox.get("t") or page.height)
            bottom = float(page.height) - float(bbox.get("b") or 0.0)
        return (
            max(0.0, min(left, right) - padding),
            max(0.0, min(top, bottom) - padding),
            min(float(page.width), max(left, right) + padding),
            min(float(page.height), max(top, bottom) + padding),
        )

    def text(
        self,
        prov: dict[str, Any],
        *,
        layout: bool = False,
        padding: float = 0.0,
    ) -> str:
        page_no = int(prov.get("page_no") or 0)
        bbox = prov.get("bbox")
        if not page_no or not isinstance(bbox, dict):
            return ""
        crop = self._pdf.pages[page_no - 1].crop(
            self._crop_box(page_no, bbox, padding=padding),
            strict=False,
        )
        value = crop.extract_text(
            layout=layout,
            x_tolerance=1,
            y_tolerance=3,
        )
        return _clean_glyph_text(
            str(value or ""),
            preserve_unknown_cid=True,
        ).strip()

    def lines(
        self,
        prov: dict[str, Any],
        *,
        padding: float = 0.0,
    ) -> list[dict[str, Any]]:
        page_no = int(prov.get("page_no") or 0)
        bbox = prov.get("bbox")
        if not page_no or not isinstance(bbox, dict):
            return []
        crop = self._pdf.pages[page_no - 1].crop(
            self._crop_box(page_no, bbox, padding=padding),
            strict=False,
        )
        result = crop.extract_text_lines(
            strip=True,
            return_chars=True,
            x_tolerance=1,
            y_tolerance=3,
        )
        lines: list[dict[str, Any]] = []
        for line in result:
            text = _clean_glyph_text(
                str(line.get("text") or ""),
                preserve_unknown_cid=True,
            ).strip()
            if not text:
                continue
            lines.append(
                {
                    "text": text,
                    "x0": float(line.get("x0") or 0.0),
                    "x1": float(line.get("x1") or 0.0),
                    "top": float(line.get("top") or 0.0),
                    "bottom": float(line.get("bottom") or 0.0),
                    "chars": line.get("chars") or [],
                }
            )
        return lines

    def logical_lines(
        self,
        prov: dict[str, Any],
        *,
        padding: float = 4.0,
    ) -> list[str]:
        physical = self.lines(prov, padding=padding)
        groups: list[list[dict[str, Any]]] = []
        for line in physical:
            if groups and float(line["top"]) - float(groups[-1][0]["top"]) < 5.5:
                groups[-1].append(line)
            else:
                groups.append([line])
        result: list[str] = []
        for group in groups:
            if len(group) == 1:
                result.append(str(group[0]["text"]).strip())
                continue
            chars: list[dict[str, Any]] = []
            seen: set[tuple[float, float, str]] = set()
            for line in group:
                for char in line.get("chars") or []:
                    if not isinstance(char, dict):
                        continue
                    key = (
                        float(char.get("x0") or 0.0),
                        float(char.get("top") or 0.0),
                        str(char.get("text") or ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    chars.append(char)
            chars.sort(
                key=lambda char: (
                    float(char.get("x0") or 0.0),
                    float(char.get("top") or 0.0),
                )
            )
            value = ""
            previous_x1: float | None = None
            for char in chars:
                char_text = _clean_glyph_text(
                    str(char.get("text") or ""),
                    preserve_unknown_cid=True,
                )
                if not char_text:
                    continue
                x0 = float(char.get("x0") or 0.0)
                if previous_x1 is not None and x0 - previous_x1 > 1.8:
                    value += " "
                value += char_text
                previous_x1 = float(char.get("x1") or x0)
            result.append(value.strip())
        return [value.replace("ﬁ", "fi") for value in result if value.strip()]

    def cell_text(
        self,
        page_no: int,
        cell_bbox: dict[str, Any],
    ) -> str:
        prov = {"page_no": page_no, "bbox": cell_bbox}
        return self.text(prov, layout=False)

    def equation_number(self, prov: dict[str, Any]) -> int | str | None:
        page_no = int(prov.get("page_no") or 0)
        bbox = prov.get("bbox")
        if not page_no or not isinstance(bbox, dict):
            return None
        width, _height = self.page_size(page_no)
        expanded = dict(bbox)
        expanded["r"] = width - 45.0
        matches: list[str] = []
        for line in self.lines({"page_no": page_no, "bbox": expanded}, padding=0.0):
            right_chars = [
                char
                for char in line.get("chars") or []
                if isinstance(char, dict)
                and float(char.get("x0") or 0.0) >= width * 0.84
            ]
            right_chars.sort(
                key=lambda char: (
                    float(char.get("x0") or 0.0),
                    float(char.get("top") or 0.0),
                )
            )
            right_text = "".join(
                _clean_glyph_text(str(char.get("text") or ""))
                for char in right_chars
            )
            label_match = re.fullmatch(
                r"\s*\(\s*((?:\d\s*)+(?:\.\s*(?:\d\s*)+)?)\)\s*",
                right_text,
            )
            if label_match:
                matches.append(label_match.group(1))
        if not matches:
            return None
        normalized = re.sub(r"\s+", "", matches[-1])
        return normalized if "." in normalized else int(normalized)


@dataclass
class FlowItem:
    kind: str
    node: dict[str, Any]
    rank: float
    page_no: int
    bbox: dict[str, float]
    prov: dict[str, Any]
    source_text: str = ""
    collection_index: int | None = None
    inline_math_repaired: bool = False
    inline_math_source_anchor: str | None = None
    inline_math_unresolved_regions: list[dict[str, Any]] = field(default_factory=list)
    source_readability_diagnostic: dict[str, Any] = field(default_factory=dict)


def _walk_body_refs(document: dict[str, Any]) -> Iterable[tuple[float, str]]:
    body = document.get("body")
    if not isinstance(body, dict):
        return
    rank = 0.0

    def walk(reference: str, parent_rank: float) -> Iterable[tuple[float, str]]:
        node = _resolve(document, reference)
        if node is None:
            return
        parts = _ref_parts(reference)
        if parts and parts[0] == "groups":
            children = node.get("children") or []
            for offset, child in enumerate(children, start=1):
                if isinstance(child, dict) and child.get("$ref"):
                    yield from walk(
                        str(child["$ref"]),
                        parent_rank + offset / max(len(children) + 1, 2),
                    )
            return
        yield parent_rank, reference

    for child in body.get("children") or []:
        if not isinstance(child, dict) or not child.get("$ref"):
            continue
        rank += 1.0
        yield from walk(str(child["$ref"]), rank)


def _picture_boxes(document: dict[str, Any]) -> dict[int, list[dict[str, float]]]:
    result: dict[int, list[dict[str, float]]] = {}
    for picture in document.get("pictures") or []:
        if not isinstance(picture, dict):
            continue
        prov = _first_prov(picture)
        box = _bbox(prov)
        page_no = int((prov or {}).get("page_no") or 0)
        if box and page_no:
            result.setdefault(page_no, []).append(box)
    return result


def _label_node_ordinals(value: Any, label: str) -> dict[int, int]:
    """Return stable object-id ordinals using the serialized document order."""

    ordinals: dict[int, int] = {}

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            if str(current.get("label") or "").casefold() == label.casefold():
                ordinals.setdefault(id(current), len(ordinals))
            for child in current.values():
                walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(value)
    return ordinals


_ALGORITHM_HEADER_RE = re.compile(r"(?i)^Algorithm\s+\d+\b")


def _table_cell_lines_equivalent(declared: str, logical: list[Any]) -> bool:
    """Return true when the declared value matches logical lines by display."""

    normalized_declared = re.sub(r"\s+", "", str(declared or ""))
    if not normalized_declared:
        return False
    normalized_logical = re.sub(
        r"\s+",
        "",
        "".join(str(line) for line in logical if str(line).strip()),
    )
    return normalized_declared == normalized_logical


def _primary_surface_count_from_document(value: Any) -> dict[str, int]:
    """Infer final-surface element counts from normalized document structure."""

    counts = {
        "text": 0,
        "headings": 0,
        "formulas": 0,
        "tables": 0,
        "algorithms": 0,
        "code_blocks": 0,
        "pictures": 0,
        "inline_math_repairs": 0,
    }

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            label = str(current.get("label") or "").strip().lower()
            if label in {"text", "paragraph", "list_item"} or "footnote" in label:
                provs = [
                    value
                    for value in (current.get("prov") or [])
                    if isinstance(value, dict) and _bbox(value)
                ]
                counts["text"] += max(len(provs), 1)
            elif label in {"section_header", "heading"}:
                counts["headings"] += 1
            elif label == "formula":
                counts["formulas"] += 1
            elif label == "table":
                counts["tables"] += 1
            elif label == "code":
                if _ALGORITHM_HEADER_RE.search(str(current.get("text") or "")):
                    counts["algorithms"] += 1
                else:
                    counts["code_blocks"] += 1
            elif label == "picture":
                counts["pictures"] += 1
            for child in current.values():
                walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(value)
    return counts


def _positive_chunk_page_number(value: Any) -> int | None:
    """Return a strict positive page number without accepting booleans."""

    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or str(value).strip() != str(parsed):
        return None
    return parsed


def _document_parts_with_global_pages(
    document: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[int | None, dict[str, Any]]]]:
    """Copy chunked documents and translate local provenance to PDF pages.

    Docling's merged chunk contract permits a part covering pages ``[3, 3]``
    to store that page as local page ``1``.  SourceReader always indexes the
    original, complete PDF, so consuming the local value directly reads page
    one and can silently attach text or math from the wrong page.  Normalize
    both page-map keys and every node provenance before semantic collection.

    The chunk marker is also retained on copied nodes.  Structural source refs
    use it to disambiguate repeated refs such as ``#/tables/0`` across parts.
    Non-chunked documents are returned unchanged for compatibility.
    """

    raw_chunks = document.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        return document, [(None, document)]

    chunk_entries: list[tuple[tuple[int, int], int, dict[str, Any]]] = []
    for original_index, raw_chunk in enumerate(raw_chunks):
        if not isinstance(raw_chunk, dict) or not isinstance(
            raw_chunk.get("document"), dict
        ):
            continue
        page_range = raw_chunk.get("page_range")
        start = (
            _positive_chunk_page_number(page_range[0])
            if isinstance(page_range, list) and page_range
            else None
        )
        end = (
            _positive_chunk_page_number(page_range[1])
            if isinstance(page_range, list) and len(page_range) > 1
            else start
        )
        sort_key = (
            start if start is not None else original_index + 1,
            end if end is not None else -1,
        )
        chunk_entries.append((sort_key, original_index, raw_chunk))

    normalized_root = copy.deepcopy(document)
    normalized_chunks: list[dict[str, Any]] = []
    normalized_parts: list[tuple[int | None, dict[str, Any]]] = []

    for part_index, (_sort_key, _original_index, raw_chunk) in enumerate(
        sorted(chunk_entries, key=lambda item: (item[0], item[1]))
    ):
        chunk = copy.deepcopy(raw_chunk)
        part = chunk["document"]
        page_range = chunk.get("page_range")
        start = (
            _positive_chunk_page_number(page_range[0])
            if isinstance(page_range, list) and page_range
            else None
        )
        end = (
            _positive_chunk_page_number(page_range[1])
            if isinstance(page_range, list) and len(page_range) > 1
            else start
        )
        length = (
            end - start + 1
            if start is not None and end is not None and end >= start
            else None
        )

        raw_pages = part.get("pages")
        page_items: list[tuple[Any, Any]] = []
        if isinstance(raw_pages, dict):
            page_items = list(raw_pages.items())
        elif isinstance(raw_pages, list):
            page_items = list(enumerate(raw_pages, start=1))
        page_numbers = {
            page_no
            for raw_page_no, _record in page_items
            if (page_no := _positive_chunk_page_number(raw_page_no)) is not None
        }

        provenance_page_numbers: set[int] = set()

        def collect_provenance(current: Any) -> None:
            if isinstance(current, dict):
                provs = current.get("prov")
                if isinstance(provs, list):
                    for prov in provs:
                        if not isinstance(prov, dict):
                            continue
                        page_no = _positive_chunk_page_number(prov.get("page_no"))
                        if page_no is not None:
                            provenance_page_numbers.add(page_no)
                for child in current.values():
                    collect_provenance(child)
            elif isinstance(current, list):
                for child in current:
                    collect_provenance(child)

        collect_provenance(part)
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
            page_no = _positive_chunk_page_number(value)
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

        def rewrite_provenance(current: Any) -> None:
            if isinstance(current, dict):
                if any(key in current for key in ("label", "self_ref", "prov")):
                    current["_local_ai_lab_chunk_part_index"] = part_index
                provs = current.get("prov")
                if isinstance(provs, list):
                    for prov in provs:
                        if not isinstance(prov, dict) or "page_no" not in prov:
                            continue
                        prov["page_no"] = global_page_number(prov.get("page_no"))
                for key, child in current.items():
                    if key != "_local_ai_lab_chunk_part_index":
                        rewrite_provenance(child)
            elif isinstance(current, list):
                for child in current:
                    rewrite_provenance(child)

        rewrite_provenance(part)
        normalized_chunks.append(chunk)
        normalized_parts.append((part_index, part))

    if not normalized_parts:
        return document, [(None, document)]
    normalized_root["chunks"] = normalized_chunks
    return normalized_root, normalized_parts


def _short_text_inside_picture(
    text: str,
    page_no: int,
    bbox: dict[str, float],
    pictures: dict[int, list[dict[str, float]]],
) -> bool:
    cx = (bbox["l"] + bbox["r"]) / 2
    cy = (bbox["t"] + bbox["b"]) / 2
    for picture in pictures.get(page_no, []):
        if (
            picture["l"] - 12 <= cx <= picture["r"] + 12
            and picture["b"] - 12 <= cy <= picture["t"] + 12
        ):
            return True
    return False


_ALGORITHM_SECTION_HEADER_HINT_RE = re.compile(
    r"(?i)^(?:"
    r"(?:Input|Output|Parameters?|Require|Ensure|Procedure|Initialization|Initialize|"
    r"for\s+|if|else|end\s+if|end\s+for|Stop|Return|Set|Sample|Draw|Choose|Construct|"
    r"Initialize|Appendix|Remark)\b|"
    r"(?:Algorithm|Input|Output)\s*:|"
    r"\d+\s*:\s+"
    r").*$"
)


def _looks_like_algorithm_section_header(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    return bool(_ALGORITHM_SECTION_HEADER_HINT_RE.search(_normalize_detached_diacritics(text)))


def _extract_algorithm_step_number(value: str) -> int | None:
    match = re.match(r"^\s*(\d{1,3})\s*[:.)]\s*", str(value or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _has_contiguous_step_sequence(values: list[int]) -> bool:
    if not values:
        return False
    normalized = sorted(set(values))
    return normalized == list(range(normalized[0], normalized[0] + len(normalized)))


def _is_algorithm_step_line(value: str) -> bool:
    if _extract_algorithm_step_number(value) is not None:
        return True
    return _looks_like_algorithm_section_header(value)


def _algorithm_group_blocks(
    document: dict[str, Any],
    source: SourceReader,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    body = document.get("body")
    children = body.get("children") if isinstance(body, dict) else None
    if not isinstance(children, list):
        return {}, set()
    blocks: dict[str, dict[str, Any]] = {}
    consumed: set[str] = set()
    for position, child in enumerate(children):
        if not isinstance(child, dict) or not child.get("$ref"):
            continue
        title_ref = str(child["$ref"])
        title_node = _resolve(document, title_ref)
        title = str((title_node or {}).get("text") or "").strip()
        if not re.match(r"(?i)^Algorithm\s+\d+\b", title):
            continue
        if position + 1 >= len(children):
            continue
        group_ref = str((children[position + 1] or {}).get("$ref") or "")
        following_node = _resolve(document, group_ref)
        if str((following_node or {}).get("label") or "").lower() == "code":
            title_prov = _first_prov(title_node or {})
            code_prov = _first_prov(following_node or {})
            valid_provs = [
                prov
                for prov in (title_prov, code_prov)
                if prov and int(prov.get("page_no") or 0) > 0 and _bbox(prov)
            ]
            if not valid_provs:
                continue
            page_no = int(valid_provs[0].get("page_no") or 0)
            same_page = [
                prov
                for prov in valid_provs
                if int(prov.get("page_no") or 0) == page_no
            ]
            boxes = [_bbox(prov) for prov in same_page]
            boxes = [box for box in boxes if box]
            union = {
                "l": min(box["l"] for box in boxes),
                "r": max(box["r"] for box in boxes),
                "t": max(box["t"] for box in boxes),
                "b": min(box["b"] for box in boxes),
                "coord_origin": str(
                    ((same_page[0].get("bbox") or {}).get("coord_origin"))
                    or "BOTTOMLEFT"
                ),
            }
            union_prov = {"page_no": page_no, "bbox": union}
            stable_source_ref = str(
                (title_node or {}).get("self_ref") or title_ref
            ).strip()
            blocks[title_ref] = {
                "node": {
                    "label": "code",
                    "self_ref": stable_source_ref,
                    "_local_ai_lab_chunk_part_index": (
                        (title_node or {}).get(
                            "_local_ai_lab_chunk_part_index"
                        )
                    ),
                    "text": (
                        title
                        + " "
                        + str((following_node or {}).get("text") or "").strip()
                    ).strip(),
                    "prov": [union_prov],
                },
                "prov": union_prov,
            }
            consumed.update({title_ref, group_ref})
            continue
        group_parts = _ref_parts(group_ref)
        if not group_parts or group_parts[0] != "groups":
            continue
        group_node = _resolve(document, group_ref)
        if not isinstance(group_node, dict):
            continue
        refs = [title_ref, group_ref]
        step_texts: list[str] = []
        formula_steps: dict[int, str] = {}
        provs: list[dict[str, Any]] = []
        title_prov = _first_prov(title_node or {})
        if title_prov:
            provs.append(title_prov)

        def add_group(reference: str) -> None:
            group = _resolve(document, reference)
            if not isinstance(group, dict):
                return
            for group_child in group.get("children") or []:
                if not isinstance(group_child, dict) or not group_child.get("$ref"):
                    continue
                child_ref = str(group_child["$ref"])
                node = _resolve(document, child_ref)
                if not isinstance(node, dict):
                    continue
                text = str(node.get("text") or "").strip()
                if not _is_algorithm_step_line(text):
                    continue
                refs.append(child_ref)
                if text:
                    step_texts.append(text)
                    formula_step = _algorithm_formula_step(text)
                    if formula_step:
                        formula_steps[formula_step[0]] = formula_step[1]
                prov = _first_prov(node)
                if prov:
                    provs.append(prov)

        add_group(group_ref)
        complete = any(
            re.search(r"(?i)\bend\s+for\b", text) for text in step_texts
        )
        extended = False
        observed_step_numbers: list[int] = []
        for step_text in step_texts:
            step_number = _extract_algorithm_step_number(step_text)
            if step_number is not None:
                observed_step_numbers.append(step_number)
        scan_position = position + 2
        while not complete and scan_position < len(children):
            extended = True
            candidate = children[scan_position]
            candidate_ref = str(
                candidate.get("$ref") if isinstance(candidate, dict) else ""
            )
            candidate_node = _resolve(document, candidate_ref)
            if not isinstance(candidate_node, dict):
                break
            parts = _ref_parts(candidate_ref)
            if parts and parts[0] == "groups":
                before_len = len(refs)
                add_group(candidate_ref)
                if len(refs) == before_len:
                    break
                refs.append(candidate_ref)
            elif str((candidate_node.get("label") or "")).lower() == "formula":
                refs.append(candidate_ref)
                prov = _first_prov(candidate_node)
                if prov:
                    provs.append(prov)
            else:
                text = str(candidate_node.get("text") or "").strip()
                step_number = _extract_algorithm_step_number(text)
                if observed_step_numbers and step_number is None:
                    break
                if not _is_algorithm_step_line(text):
                    break
                if text:
                    step_texts.append(text)
                    formula_step = _algorithm_formula_step(text)
                    if formula_step:
                        formula_steps[formula_step[0]] = formula_step[1]
                    if step_number is not None:
                        observed_step_numbers.append(step_number)
                prov = _first_prov(candidate_node)
                if prov:
                    provs.append(prov)
                refs.append(candidate_ref)
            complete = any(
                re.search(r"(?i)\bend\s+for\b", text) for text in step_texts
            )
            scan_position += 1
        valid_provs = [
            prov for prov in provs if int(prov.get("page_no") or 0) > 0 and _bbox(prov)
        ]
        if not valid_provs:
            continue
        page_no = int(valid_provs[0].get("page_no") or 0)
        same_page = [
            prov for prov in valid_provs if int(prov.get("page_no") or 0) == page_no
        ]
        boxes = [_bbox(prov) for prov in same_page]
        boxes = [box for box in boxes if box]
        union = {
            "l": min(box["l"] for box in boxes),
            "r": max(box["r"] for box in boxes),
            "t": max(box["t"] for box in boxes),
            "b": min(box["b"] for box in boxes),
            "coord_origin": str(
                ((same_page[0].get("bbox") or {}).get("coord_origin"))
                or "BOTTOMLEFT"
            ),
        }
        union_prov = {"page_no": page_no, "bbox": union}
        semantic_text = title + " " + " ".join(step_texts)
        # Some Docling exports split mathematically dense algorithm steps into
        # standalone formula nodes after the initial list group. Re-read the
        # complete source rectangle in that case so those numbered steps are
        # recovered as visible text rather than raw LaTeX fragments.
        needs_source_fallback = False
        if observed_step_numbers:
            needs_source_fallback = not _has_contiguous_step_sequence(
                observed_step_numbers
            )
        elif extended:
            needs_source_fallback = True
        if needs_source_fallback:
            source_text = source.text(union_prov)
            if source_text:
                source_title, source_steps = _numbered_algorithm_lines(source_text)
                if source_steps:
                    semantic_text = (source_title or title) + " " + " ".join(
                        f"{number}: {formula_steps.get(number, content)}"
                        for number, content in source_steps
                    )
                else:
                    semantic_text = source_text
        stable_source_ref = str(
            (title_node or {}).get("self_ref") or title_ref
        ).strip()
        blocks[title_ref] = {
            "node": {
                "label": "code",
                "self_ref": stable_source_ref,
                "_local_ai_lab_chunk_part_index": (
                    (title_node or {}).get("_local_ai_lab_chunk_part_index")
                ),
                "text": semantic_text,
                "prov": [union_prov],
            },
            "prov": union_prov,
        }
        consumed.update(refs)
    return blocks, consumed


def _collect_items(
    document: dict[str, Any],
    source: SourceReader,
    *,
    dropped_formula_artifacts: list[dict[str, Any]] | None = None,
    formula_offset: int = 0,
    inline_math_anchor_part: int = 0,
) -> list[FlowItem]:
    pictures = _picture_boxes(document)
    formula_ordinals = _label_node_ordinals(document, "formula")
    algorithm_blocks, algorithm_consumed = _algorithm_group_blocks(document, source)
    if dropped_formula_artifacts is None:
        dropped_formula_artifacts = []
    items: list[FlowItem] = []
    for rank, reference in _walk_body_refs(document):
        if reference in algorithm_blocks:
            block = algorithm_blocks[reference]
            node = block["node"]
            prov = block["prov"]
            box = _bbox(prov)
            reference_parts = _ref_parts(reference)
            if box:
                items.append(
                    FlowItem(
                        kind="algorithm",
                        node=node,
                        rank=rank,
                        page_no=int(prov.get("page_no") or 0),
                        bbox=box,
                        prov=prov,
                        source_text=source.text(prov),
                        collection_index=(
                            reference_parts[1] if reference_parts else None
                        ),
                    )
                )
            continue
        if reference in algorithm_consumed:
            continue
        parts = _ref_parts(reference)
        node = _resolve(document, reference)
        if parts is None or node is None:
            continue
        collection, index = parts
        label = str(node.get("label") or "").lower()
        quarantine_kind = _quarantine_kind(node)
        if quarantine_kind in QUARANTINED_MAIN_FLOW_KINDS:
            continue
        if str(node.get("content_layer") or "").lower() == "furniture":
            continue

        if collection == "pictures":
            formula_child: dict[str, Any] | None = None
            for child in node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                candidate = _resolve(document, str(child.get("$ref") or ""))
                if str((candidate or {}).get("label") or "").lower() == "formula":
                    formula_child = candidate
                    break
            image_size = (
                ((node.get("image") or {}).get("size") or {})
                if isinstance(node.get("image"), dict)
                else {}
            )
            if (
                formula_child is None
                and not node.get("captions")
                and float(image_size.get("width") or 0.0) <= 64.0
                and float(image_size.get("height") or 0.0) <= 64.0
            ):
                continue
            effective = formula_child or node
            prov = _first_prov(effective) or _first_prov(node)
            box = _bbox(prov)
            if not box or not prov:
                continue
            items.append(
                FlowItem(
                    kind="formula" if formula_child else "picture",
                    node=effective,
                    rank=rank,
                    page_no=int(prov.get("page_no") or 0),
                    bbox=box,
                    prov=prov,
                    collection_index=(
                        formula_ordinals.get(id(effective), index)
                        if formula_child
                        else index
                    ),
                )
            )
            continue

        if collection == "tables":
            prov = _first_prov(node)
            box = _bbox(prov)
            if not box or not prov:
                continue
            table_text = source.text(prov)
            kind = "algorithm" if re.search(r"(?i)\bAlgorithm\s+\d+\b", table_text) else "table"
            items.append(
                FlowItem(
                    kind=kind,
                    node=node,
                    rank=rank,
                    page_no=int(prov.get("page_no") or 0),
                    bbox=box,
                    prov=prov,
                    source_text=table_text,
                    collection_index=index,
                )
            )
            continue

        if collection != "texts":
            continue
        if label in {"caption", "page_header", "page_footer"}:
            continue
        if label in {"text", "paragraph", "list_item", "footnote"} or "footnote" in label:
            provs = [
                value
                for value in (node.get("prov") or [])
                if isinstance(value, dict) and _bbox(value)
            ]
            for offset, prov in enumerate(provs):
                box = _bbox(prov)
                assert box is not None
                physical_source = source.text(prov)
                charspan = prov.get("charspan")
                node_text = str(node.get("text") or "")
                source_text = ""
                if (
                    isinstance(charspan, list)
                    and len(charspan) == 2
                    and all(isinstance(value, int) for value in charspan)
                ):
                    start, end = charspan
                    source_text = node_text[start:end].strip()
                source_text, readability_diagnostic = _choose_readable_source_text(
                    source_text,
                    physical_source,
                )
                if not source_text:
                    # A valid Docling span can survive a missing/empty PDF
                    # text layer.  Only discard the item once both sources are
                    # unavailable; otherwise the clean slice remains the
                    # authoritative machine surface.
                    continue
                pre_math_source_text = source_text
                page_no = int(prov.get("page_no") or 0)
                math_aware_text = getattr(source, "math_aware_text", None)
                if callable(math_aware_text):
                    source_text = math_aware_text(prov, source_text)
                if not source_text:
                    continue
                inline_math_repaired = source_text != pre_math_source_text
                math_diagnostics = getattr(source, "_math_aware_diagnostics", {})
                if not isinstance(math_diagnostics, dict):
                    math_diagnostics = {}
                diagnostic_key_builder = getattr(
                    source,
                    "_math_diagnostic_key",
                    lambda _prov: None,
                )
                math_diagnostic = math_diagnostics.get(
                    diagnostic_key_builder(prov),
                    {},
                )
                if not isinstance(math_diagnostic, dict):
                    math_diagnostic = {}
                inline_math_unresolved_regions = [
                    item
                    for item in (math_diagnostic.get("unresolved_clusters") or [])
                    if isinstance(item, dict)
                ]
                unknown_cid = bool(
                    re.search(r"\(cid:\d+\)", source_text, flags=re.IGNORECASE)
                    or re.search(r"\(cid:\d+\)", physical_source, flags=re.IGNORECASE)
                )
                if unknown_cid:
                    # Keep the unresolved spelling in the body text and add a
                    # source-backed crop marker.  The adapter renders this as
                    # an open review disclosure rather than silently dropping
                    # the unknown glyph.
                    cid_bbox = dict(box)
                    inline_math_unresolved_regions.append(
                        {
                            "bbox": cid_bbox,
                            "source_text": source_text,
                            "reason": "unknown_cid_requires_source_crop",
                        }
                    )
                inline_math_evidence = False
                inline_math_detector = getattr(source, "inline_math_evidence", None)
                inline_math_detector_available = bool(
                    callable(inline_math_detector)
                    and getattr(source, "_pypdf", None) is not None
                )
                if inline_math_detector_available:
                    try:
                        inline_math_evidence = bool(inline_math_detector(prov))
                    except Exception:
                        inline_math_evidence = False
                inline_math_source_anchor: str | None = None
                # Repaired-only inline math is machine-readable and does not
                # need a paragraph-sized crop.  Keep an anchor only for a
                # genuinely unresolved cluster (including an unknown CID).
                if inline_math_unresolved_regions:
                    inline_math_source_anchor = _inline_math_anchor_id(
                        page_no=page_no,
                        collection=collection,
                        index=index,
                        offset=offset,
                        part_index=inline_math_anchor_part,
                        bbox=box,
                    )
                if _short_text_inside_picture(source_text, page_no, box, pictures):
                    continue
                if (box["r"] - box["l"]) < 18 or (box["t"] - box["b"]) < 3:
                    continue
                items.append(
                    FlowItem(
                        kind="list_item" if label == "list_item" else (
                            "footnote" if "footnote" in label else "text"
                        ),
                        node=node,
                        rank=rank + offset / max(len(provs) + 1, 2),
                        page_no=page_no,
                        bbox=box,
                        prov=prov,
                        source_text=source_text,
                        collection_index=index,
                        inline_math_repaired=inline_math_repaired,
                        inline_math_source_anchor=inline_math_source_anchor,
                        inline_math_unresolved_regions=inline_math_unresolved_regions,
                        source_readability_diagnostic=readability_diagnostic,
                    )
                )
            continue
        prov = _first_prov(node)
        box = _bbox(prov)
        if not box or not prov:
            continue
        kind = {
            "title": "title",
            "section_header": "heading",
            "formula": "formula",
            "code": "algorithm"
            if re.search(r"(?i)^Algorithm\s+\d+\b", str(node.get("text") or ""))
            else "code",
        }.get(label)
        standalone_equation_number = (
            re.fullmatch(
                r"\(\s*((?:\d\s*)+(?:\.\s*(?:\d\s*)+)?)\s*\)",
                str(node.get("text") or "").strip(),
            )
            if kind == "formula"
            else None
        )
        formula_index = formula_ordinals.get(id(node))
        if formula_index is None:
            formula_index = -1
        if standalone_equation_number:
            if dropped_formula_artifacts is not None:
                dropped_formula_artifacts.append(
                    {
                        "raw_formula_index": (
                            formula_offset + formula_index + 1
                            if formula_index >= 0
                            else formula_offset + len(dropped_formula_artifacts) + 1
                        ),
                        "text": str(node.get("text") or ""),
                        "bbox": box,
                        "page_no": int(prov.get("page_no") or 0),
                        "reason": "standalone_equation_number",
                    }
                )
            number = re.sub(r"\s+", "", standalone_equation_number.group(1))
            center = (box["t"] + box["b"]) / 2.0
            for previous in reversed(items):
                if previous.kind != "formula" or previous.page_no != int(
                    prov.get("page_no") or 0
                ):
                    continue
                if previous.bbox["b"] - 2.0 <= center <= previous.bbox["t"] + 2.0:
                    previous.node["_semantic_equation_number"] = number
                    break
            continue
        formula_text = str(node.get("text") or "")
        orphan_script_fragment = bool(
            kind == "formula"
            and re.search(r"[_^]", formula_text)
            and not re.sub(r"[\s{}_^*.,+\-−/0-9\\]", "", formula_text)
        )
        compact_formula_text = re.sub(r"\s+", "", formula_text)
        orphan_punctuation_fragment = bool(
            kind == "formula"
            and compact_formula_text
            and re.fullmatch(r"[\-−–—=,.;:|/\\()\[\]{}]+", compact_formula_text)
        )
        source_mismatch_fragment = False
        overlapping_formula_parent = False
        if (
            kind == "formula"
            and (box["r"] - box["l"]) < 6
            and (
                orphan_script_fragment
                or (
                    len(compact_formula_text) == 1
                    and re.fullmatch(
                        r"[A-Za-z0-9\-−–—=,.;:|/\\()\[\]{}]",
                        compact_formula_text,
                    )
                )
            )
        ):
            try:
                physical_formula_text = re.sub(r"\s+", "", source.text(prov))
            except Exception:
                physical_formula_text = ""
            source_mismatch_fragment = bool(
                physical_formula_text
                and physical_formula_text.casefold()
                != compact_formula_text.casefold()
            )
            if orphan_script_fragment:
                page_no = int(prov.get("page_no") or 0)
                for previous in reversed(items):
                    if previous.kind != "formula" or previous.page_no != page_no:
                        continue
                    horizontal_overlap = max(
                        0.0,
                        min(float(previous.bbox["r"]), float(box["r"]))
                        - max(float(previous.bbox["l"]), float(box["l"])),
                    )
                    vertical_overlap = max(
                        0.0,
                        min(float(previous.bbox["t"]), float(box["t"]))
                        - max(float(previous.bbox["b"]), float(box["b"])),
                    )
                    if horizontal_overlap > 0.5 and vertical_overlap > 0.5:
                        overlapping_formula_parent = True
                        break
        if (
            kind == "formula"
            # Discard only a near-zero-width orphan whose syntax or source
            # glyph proves it is a detached layout fragment. A legitimate
            # short formula such as ``x``, ``i``, or ``∫`` remains visible
            # when the source glyph agrees (or no source text is available).
            and (box["r"] - box["l"]) < 6
            and (
                orphan_script_fragment
                and source_mismatch_fragment
                or orphan_punctuation_fragment
                or source_mismatch_fragment
            )
        ):
            if dropped_formula_artifacts is not None:
                dropped_formula_artifacts.append(
                    {
                        "raw_formula_index": (
                            formula_offset + formula_index + 1
                            if formula_index >= 0
                            else formula_offset + len(dropped_formula_artifacts) + 1
                        ),
                        "text": str(node.get("text") or ""),
                        "bbox": box,
                        "page_no": int(prov.get("page_no") or 0),
                        # An overlapping script may belong to the neighboring
                        # formula even when a tiny physical crop does not
                        # recover it.  Do not classify that ambiguity as an
                        # allowed layout artifact: the final delivery gate
                        # must fail closed until it can be merged or proved
                        # absent from the submitted PDF.
                        "reason": (
                            "unmerged_formula_script"
                            if orphan_script_fragment
                            and overlapping_formula_parent
                            else "compact_formula_fragment"
                        ),
                    }
                )
            continue
        if kind:
            items.append(
                FlowItem(
                    kind=kind,
                    node=node,
                    rank=rank,
                    page_no=int(prov.get("page_no") or 0),
                    bbox=box,
                    prov=prov,
                    source_text=source.text(prov) if kind in {"code", "algorithm"} else "",
                    collection_index=(
                        formula_ordinals.get(id(node), index)
                        if kind == "formula"
                        else index
                    ),
                )
            )
    return items


def _sort_items(
    items: list[FlowItem],
    document: dict[str, Any],
) -> list[FlowItem]:
    edge_heading_pages: dict[str, set[int]] = {}
    edge_heading_ids: dict[str, set[int]] = {}
    for item in items:
        if item.kind != "heading":
            continue
        text = re.sub(
            r"\s+",
            " ",
            str(item.node.get("text") or item.source_text),
        ).strip()
        if not text or len(text) > 160 or re.match(r"^\d+(?:\.\d+)*\b", text):
            continue
        page_record = (document.get("pages") or {}).get(str(item.page_no)) or {}
        page_height = float(
            ((page_record.get("size") or {}).get("height")) or 792.0
        )
        origin = str(
            ((item.prov.get("bbox") or {}).get("coord_origin")) or "BOTTOMLEFT"
        ).upper()
        low = min(item.bbox["t"], item.bbox["b"])
        high = max(item.bbox["t"], item.bbox["b"])
        if origin == "TOPLEFT":
            top_distance = low
            bottom_distance = page_height - high
        else:
            top_distance = page_height - high
            bottom_distance = low
        if min(top_distance, bottom_distance) > page_height * 0.1:
            continue
        key = _normalized_lookup(text)
        edge_heading_pages.setdefault(key, set()).add(item.page_no)
        edge_heading_ids.setdefault(key, set()).add(id(item))
    repeated_edge_heading_ids = {
        item_id
        for key, pages in edge_heading_pages.items()
        if len(pages) >= 2
        for item_id in edge_heading_ids[key]
    }
    if repeated_edge_heading_ids:
        items = [
            item
            for item in items
            if id(item) not in repeated_edge_heading_ids
        ]

    by_page: dict[int, list[FlowItem]] = {}
    for item in items:
        by_page.setdefault(item.page_no, []).append(item)
    result: list[FlowItem] = []
    for page_no in sorted(by_page):
        page_items = by_page[page_no]
        page_record = (document.get("pages") or {}).get(str(page_no)) or {}
        page_width = float(((page_record.get("size") or {}).get("width")) or 612.0)
        body_widths = [
            item.bbox["r"] - item.bbox["l"]
            for item in page_items
            if item.kind in {"text", "heading", "list_item", "code", "algorithm"}
        ]
        single_column = bool(body_widths) and median(body_widths) >= page_width * 0.55
        if single_column:
            page_items.sort(key=lambda item: (-item.bbox["t"], item.bbox["l"], item.rank))
        else:
            page_items.sort(key=lambda item: item.rank)
        result.extend(page_items)
    return result


def _line_content_x(line: dict[str, Any], prefix_pattern: re.Pattern[str]) -> tuple[str, float]:
    text = str(line["text"])
    match = prefix_pattern.match(text)
    if not match:
        return text, float(line["x0"])
    chars = [char for char in line.get("chars") or [] if isinstance(char, dict)]
    consumed = len(match.group(0))
    visible_count = 0
    content_x = float(line["x0"])
    for char in chars:
        char_text = _clean_glyph_text(str(char.get("text") or ""))
        visible_count += len(char_text)
        if visible_count >= consumed:
            content_x = float(char.get("x1") or char.get("x0") or content_x)
            break
    return text[match.end() :].lstrip(), content_x


def _algorithm_semantic_text(node: dict[str, Any]) -> str:
    text = str(node.get("text") or "").strip()
    if text:
        return text
    cells = (node.get("data") or {}).get("table_cells") or []
    return " ".join(
        str(cell.get("text") or "").strip()
        for cell in cells
        if isinstance(cell, dict) and str(cell.get("text") or "").strip()
    )


def _algorithm_formula_step(value: str) -> tuple[int, str] | None:
    """Do not synthesize algorithm formula content from paper signatures.

    Formula nodes are retained as source-backed visual material.  Rewriting a
    flattened fragment into a guessed ``prox``/script expression can silently
    change an unseen algorithm, so the parser intentionally declines to
    provide a semantic replacement.
    """
    return None


def _repair_algorithm_case_semantics(value: str) -> str:
    return value


def _normalize_algorithm_semantics(value: str) -> str:
    # Preserve unseen algorithm prose/variables verbatim apart from collapsing
    # extraction whitespace.  Math scripts and case assignments require PDF
    # source evidence, not token-specific substitutions.
    return re.sub(r"\s+", " ", str(value)).strip()


def _numbered_algorithm_lines(value: str) -> tuple[str, list[tuple[int, str]]]:
    matches = list(re.finditer(r"(?<!\d)(\d{1,2}):\s*", value))
    if not matches:
        return value.strip(), []
    title = value[: matches[0].start()].strip()
    lines: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        lines.append(
            (
                int(match.group(1)),
                _normalize_algorithm_semantics(value[match.end() : end]),
            )
        )
    return title, lines


def _numbered_code_lines(value: str) -> list[tuple[int, str]]:
    candidates = list(
        re.finditer(r"(?<![\w.])(\d{1,3})\s+", value)
    )
    selected: list[re.Match[str]] = []
    expected = 1
    for match in candidates:
        if int(match.group(1)) == expected:
            selected.append(match)
            expected += 1
    if len(selected) < 2:
        return []
    result: list[tuple[int, str]] = []
    for index, match in enumerate(selected):
        end = selected[index + 1].start() if index + 1 < len(selected) else len(value)
        content = value[match.end() : end].strip()
        content = content.replace("−", "-")
        content = re.sub(r"\.\s+(?=[A-Za-z_]\w*\s*\()", ".", content)
        result.append((int(match.group(1)), content))
    return result


def _algorithm_table_block(
    item: FlowItem,
    source: SourceReader | None = None,
) -> tuple[str, str] | None:
    data = item.node.get("data") or {}
    rows = int(data.get("num_rows") or 0)
    cells = [
        cell
        for cell in data.get("table_cells") or []
        if isinstance(cell, dict)
    ]
    if rows < 3 or not cells:
        return None
    by_row: dict[int, list[dict[str, Any]]] = {}
    for cell in cells:
        row = int(cell.get("start_row_offset_idx") or 0)
        by_row.setdefault(row, []).append(cell)
    for values in by_row.values():
        values.sort(key=lambda cell: int(cell.get("start_col_offset_idx") or 0))

    cell_text_cache: dict[int, str] = {}

    def cell_text(cell: dict[str, Any]) -> str:
        cached = cell_text_cache.get(id(cell))
        if cached is not None:
            return cached
        value = str(cell.get("text") or "").strip()
        bbox = cell.get("bbox")
        if value and source is not None and isinstance(bbox, dict):
            try:
                repair_bbox = dict(bbox)
                repair_bbox["l"] = float(repair_bbox.get("l") or 0.0) - 2.0
                repair_bbox["r"] = float(repair_bbox.get("r") or 0.0) + 2.0
                if str(repair_bbox.get("coord_origin") or "BOTTOMLEFT").upper() == "TOPLEFT":
                    repair_bbox["t"] = float(repair_bbox.get("t") or 0.0) - 2.0
                    repair_bbox["b"] = float(repair_bbox.get("b") or 0.0) + 2.0
                else:
                    repair_bbox["t"] = float(repair_bbox.get("t") or 0.0) + 2.0
                    repair_bbox["b"] = float(repair_bbox.get("b") or 0.0) - 2.0
                value = source.math_aware_text(
                    {"page_no": item.page_no, "bbox": repair_bbox},
                    value,
                    allow_text_script_base=True,
                )
            except Exception:
                pass
        cell_text_cache[id(cell)] = value
        return value

    def row_text(row: int, *, minimum_column: int = 0) -> str:
        return " ".join(
            cell_text(cell)
            for cell in by_row.get(row, [])
            if int(cell.get("start_col_offset_idx") or 0) >= minimum_column
            and cell_text(cell)
        ).strip()

    title = row_text(0)
    if not re.match(r"(?i)^Algorithm\s+\d+\b", title):
        return None
    header = row_text(1)
    header = re.sub(r"\s+(?=Ensure\s*:)", "\n", header, flags=re.IGNORECASE)
    header_lines = [
        _normalize_algorithm_semantics(line)
        for line in header.splitlines()
        if line.strip()
    ]

    source_map: dict[int, str] = {}
    if source is not None:
        try:
            physical_text = "\n".join(
                str(line.get("text") or "")
                for line in source.lines(item.prov, padding=1.0)
            )
        except Exception:
            physical_text = ""
        _physical_title, physical_lines = _numbered_algorithm_lines(
            physical_text
        )
        source_map.update(
            {
                number: _normalize_algorithm_semantics(content)
                for number, content in physical_lines
                if content
            }
        )
    _source_title, source_lines = _numbered_algorithm_lines(item.source_text)
    for number, content in source_lines:
        if content:
            source_map.setdefault(
                number,
                _normalize_algorithm_semantics(content),
            )
    records: list[tuple[int, str, float]] = []
    for row in range(2, rows):
        row_cells = by_row.get(row, [])
        prefix_cells = [
            cell
            for cell in row_cells
            if int(cell.get("start_col_offset_idx") or 0) == 0
        ]
        content_cells = [
            cell
            for cell in row_cells
            if int(cell.get("start_col_offset_idx") or 0) > 0
        ]
        prefixes = re.findall(
            r"(?<!\d)(\d{1,2})\s*:",
            " ".join(str(cell.get("text") or "") for cell in prefix_cells),
        )
        if not prefixes:
            continue
        content = " ".join(
            cell_text(cell)
            for cell in content_cells
            if cell_text(cell)
        )
        content_x = min(
            (
                float(((cell.get("bbox") or {}).get("l")) or 0.0)
                for cell in content_cells
                if isinstance(cell.get("bbox"), dict)
            ),
            default=0.0,
        )
        numbers = [int(value) for value in prefixes]
        if len(numbers) == 1:
            value = content or source_map.get(numbers[0], "")
            records.append(
                (
                    numbers[0],
                    _normalize_algorithm_semantics(value),
                    content_x,
                )
            )
            continue
        for number in numbers:
            value = source_map.get(number, "")
            if value:
                records.append(
                    (
                        number,
                        _normalize_algorithm_semantics(value),
                        content_x,
                    )
                )
    if not records:
        return None
    base_x = min(value[2] for value in records)
    positive = sorted(
        {
            round(value[2] - base_x, 1)
            for value in records
            if value[2] - base_x >= 3.0
        }
    )
    indent_unit = min(positive) if positive else 12.0
    rendered = list(header_lines)
    for number, content, content_x in records:
        level = max(0, round((content_x - base_x) / max(indent_unit, 1.0)))
        rendered.append(f"{number:<4}{'    ' * level}{content}".rstrip())
    return title, "\n".join(rendered)


def _unnumbered_algorithm_block(value: str) -> tuple[str, str]:
    match = re.match(
        r"(?is)^\s*(Algorithm\s+\d+\b.*?)(?=\s+(?:Input|Output|Parameters?)\s*:)",
        value,
    )
    title = match.group(1).strip() if match else ""
    body = value[match.end() :].strip() if match else value.strip()
    body = re.sub(
        r"\s+(?=(?:[A-Za-z]\s*←|(?<!end\s)for\b[^.]{0,80}\bdo\b|Draw\b|"
        r"(?<!end\s)if\b|Accept\b|(?<!and\s)Stop\b|else\b|"
        r"end\s+(?:if|for)\b))",
        "\n",
        body,
        flags=re.IGNORECASE,
    )
    level = 0
    rendered: list[str] = []
    for raw_line in body.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        lowered = line.casefold()
        if lowered.startswith(("end if", "end for", "else")):
            level = max(0, level - 1)
        rendered.append("    " * level + line)
        if lowered.startswith("else"):
            level += 1
        elif re.match(r"(?i)^(?:for|if)\b", line):
            level += 1
    return title, "\n".join(rendered)


def _preformatted_block(
    source: SourceReader,
    item: FlowItem,
    *,
    algorithm: bool,
) -> tuple[str, str]:
    lines = source.lines(item.prov, padding=1.0)
    if algorithm:
        table_block = _algorithm_table_block(item, source)
        if table_block is not None:
            return table_block
        semantic_title, semantic_lines = _numbered_algorithm_lines(
            _algorithm_semantic_text(item.node)
        )
        if semantic_lines:
            source_positions: dict[int, float] = {}
            for line in lines:
                match = re.match(r"^\s*(\d{1,2})\s*:\s*", str(line["text"]))
                if not match:
                    continue
                _content, content_x = _line_content_x(
                    line,
                    re.compile(r"^\s*\d{1,2}\s*:\s*"),
                )
                source_positions[int(match.group(1))] = content_x
            known_positions = list(source_positions.values())
            base_x = min(known_positions) if known_positions else 0.0
            positive = sorted(
                {
                    round(position - base_x, 1)
                    for position in known_positions
                    if position - base_x >= 3.0
                }
            )
            indent_unit = min(positive) if positive else 12.0
            rendered = []
            for number, content in semantic_lines:
                position = source_positions.get(number, base_x)
                level = max(0, round((position - base_x) / max(indent_unit, 1.0)))
                rendered.append(f"{number:<4}{'    ' * level}{content}".rstrip())
            return semantic_title, "\n".join(rendered)
        unnumbered_title, unnumbered_block = _unnumbered_algorithm_block(
            _algorithm_semantic_text(item.node)
        )
        if unnumbered_block and re.search(
            r"(?im)^\s*(?:Input|Output|Parameters?|for\b|if\b)",
            unnumbered_block,
        ):
            return unnumbered_title, unnumbered_block
    else:
        semantic_lines = _numbered_code_lines(str(item.node.get("text") or ""))
        if semantic_lines:
            source_positions: dict[int, float] = {}
            prefix_pattern = re.compile(r"^\s*\d{1,3}\s+")
            for line in lines:
                match = re.match(r"^\s*(\d{1,3})\s+", str(line["text"]))
                if not match:
                    continue
                _content, content_x = _line_content_x(line, prefix_pattern)
                source_positions[int(match.group(1))] = content_x
            known_positions = list(source_positions.values())
            base_x = min(known_positions) if known_positions else 0.0
            rendered = []
            previous_number = 0
            for number, content in semantic_lines:
                if number - previous_number > 1:
                    rendered.append("")
                previous_number = number
                delta = source_positions.get(number, base_x) - base_x
                level = 0 if delta < 7.0 else max(1, round(delta / 18.0))
                rendered.append(f"{'    ' * level}{content}".rstrip())
            return "", "\n".join(rendered).strip()
    if not lines:
        return "", ""
    title = ""
    if algorithm and re.match(r"(?i)^Algorithm\s+\d+\b", lines[0]["text"]):
        title = lines.pop(0)["text"]
    prefix_pattern = re.compile(r"^\s*(\d+\s*:?\s*)")
    parsed: list[tuple[str, str, float]] = []
    content_positions: list[float] = []
    for line in lines:
        raw = str(line["text"]).rstrip()
        match = prefix_pattern.match(raw)
        prefix = match.group(1).strip() if match else ""
        content, content_x = _line_content_x(line, prefix_pattern)
        if not algorithm:
            prefix = ""
        if content or prefix:
            parsed.append((prefix, content, content_x))
            if content:
                content_positions.append(content_x)
    if not parsed:
        return title, ""
    base_x = min(content_positions) if content_positions else min(value[2] for value in parsed)
    deltas = sorted(
        {
            round(max(0.0, value[2] - base_x), 1)
            for value in parsed
            if value[1]
        }
    )
    positive = [value for value in deltas if value >= 3.0]
    indent_unit = min(positive) if positive else 12.0
    rendered: list[str] = []
    for prefix, content, content_x in parsed:
        level = max(0, round((content_x - base_x) / max(indent_unit, 1.0)))
        indent = "    " * level
        if algorithm:
            rendered.append(f"{prefix:<4}{indent}{content}".rstrip())
        else:
            rendered.append(f"{indent}{content}".rstrip())
    return title, "\n".join(rendered).strip()


def source_algorithm_block(
    node: dict[str, Any],
    pdf_path: Path,
) -> tuple[str, str] | None:
    """Rebuild one algorithm body from the same source geometry as reflow.

    The quality adapter independently inventories and verifies algorithm
    surfaces.  Letting it derive the expected body from flattened PDF text
    while semantic reflow uses table cells, character geometry and script
    repair can make two individually source-backed views disagree.  This
    narrow bridge keeps both paths on the same reconstruction without reading
    the generated HTML or Markdown back into the source contract.
    """

    if not isinstance(node, dict) or not pdf_path.is_file():
        return None
    prov = _first_prov(node)
    resolved_bbox = _bbox(prov)
    if not isinstance(prov, dict) or not isinstance(resolved_bbox, dict):
        return None
    try:
        page_no = int(prov.get("page_no") or 0)
    except (TypeError, ValueError):
        return None
    if page_no <= 0:
        return None
    source = SourceReader(pdf_path)
    try:
        title, body = _preformatted_block(
            source,
            FlowItem(
                kind="algorithm",
                node=node,
                rank=0.0,
                page_no=page_no,
                bbox=resolved_bbox,
                prov=prov,
                source_text=_algorithm_semantic_text(node),
            ),
            algorithm=True,
        )
    finally:
        source.close()
    body = body.strip()
    if not body:
        return None
    return title.strip(), body


def _table_cell_span(
    cell: dict[str, Any],
    *,
    rows: int,
    cols: int,
) -> tuple[int, int, int, int, int, int]:
    """Return ``(row, col, row_end, col_end, rowspan, colspan)``.

    Docling's ``end_*_offset_idx`` values are exclusive (a cell at row 0 with
    ``end_row_offset_idx == 1`` occupies only the first row).  Older payloads
    omit the end offsets, so retain a one-cell fallback and clamp malformed
    spans to the declared table dimensions.
    """

    def integer(name: str, default: int) -> int:
        try:
            return int(cell.get(name) if cell.get(name) is not None else default)
        except (TypeError, ValueError):
            return default

    row = max(0, integer("start_row_offset_idx", 0))
    col = max(0, integer("start_col_offset_idx", 0))
    row_end = integer("end_row_offset_idx", row + 1)
    col_end = integer("end_col_offset_idx", col + 1)
    if row_end <= row:
        row_end = row + max(1, integer("row_span", 1))
    if col_end <= col:
        col_end = col + max(1, integer("col_span", 1))
    row_end = min(max(row + 1, row_end), max(rows, row + 1))
    col_end = min(max(col + 1, col_end), max(cols, col + 1))
    return row, col, row_end, col_end, row_end - row, col_end - col


def _table_cell_layout(
    source: SourceReader,
    item: FlowItem,
) -> tuple[list[list[str]], int, list[dict[str, Any]]]:
    """Build the declared table grid and lossless HTML cell placements.

    The grid is kept rectangular for Markdown and legacy callers.  Placement
    records carry the original row/column spans so HTML can preserve merged
    headers/cells instead of repeating or dropping their value.
    """

    table = item.node
    data = table.get("data") or {}
    try:
        rows = max(0, int(data.get("num_rows") or 0))
    except (TypeError, ValueError):
        rows = 0
    try:
        cols = max(0, int(data.get("num_cols") or 0))
    except (TypeError, ValueError):
        cols = 0
    raw_cells = [cell for cell in data.get("table_cells") or [] if isinstance(cell, dict)]
    def raw_extent(cell: dict[str, Any], start_name: str, end_name: str, span_name: str) -> int:
        try:
            start = max(0, int(cell.get(start_name) or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            end = int(cell.get(end_name)) if cell.get(end_name) is not None else start + 1
        except (TypeError, ValueError):
            end = start + 1
        if end <= start:
            try:
                end = start + max(1, int(cell.get(span_name) or 1))
            except (TypeError, ValueError):
                end = start + 1
        return end

    if rows <= 0:
        rows = max(
            (raw_extent(cell, "start_row_offset_idx", "end_row_offset_idx", "row_span") for cell in raw_cells),
            default=0,
        )
    if cols <= 0:
        cols = max(
            (raw_extent(cell, "start_col_offset_idx", "end_col_offset_idx", "col_span") for cell in raw_cells),
            default=0,
        )
    if not rows or not cols:
        return [], 0, []

    grid = [["" for _ in range(cols)] for _ in range(rows)]
    occupied: set[tuple[int, int]] = set()
    placements: list[dict[str, Any]] = []
    for cell in raw_cells:
        row, col, row_end, col_end, rowspan, colspan = _table_cell_span(
            cell,
            rows=rows,
            cols=cols,
        )
        if row >= rows or col >= cols:
            continue
        row_end = min(row_end, rows)
        col_end = min(col_end, cols)
        rowspan = max(1, row_end - row)
        colspan = max(1, col_end - col)
        # A malformed export can overlap cells.  Preserve the first declared
        # placement and leave the later one in the diagnostic layout instead
        # of emitting invalid HTML with duplicate coverage.
        covered = {
            (covered_row, covered_col)
            for covered_row in range(row, row_end)
            for covered_col in range(col, col_end)
        }
        if occupied.intersection(covered):
            continue
        occupied.update(covered)
        value = str(cell.get("text") or "").strip()
        grid[row][col] = value
        def flag(name: str) -> bool:
            value = cell.get(name)
            if isinstance(value, bool):
                return value
            return str(value or "").strip().casefold() in {
                "1",
                "true",
                "yes",
            }

        column_header = flag("column_header")
        row_header = flag("row_header")
        row_section = flag("row_section")
        header_role = (
            "col"
            if column_header
            else "row"
            if row_header
            else "rowgroup"
            if row_section
            else ""
        )
        placements.append(
            {
                "row": row,
                "col": col,
                "rowspan": rowspan,
                "colspan": colspan,
                "text": value,
                "cell": cell,
                "column_header": column_header,
                "row_header": row_header,
                "row_section": row_section,
                "header_role": header_role,
            }
        )

    # Keep multiline cell text readable, but never reinterpret a 2x2 table as
    # a collapsed pair of columns merely because both cells contain the same
    # number of physical lines.  Such a heuristic loses legitimate multiline
    # headers and cannot prove a semantic row boundary.
    logical_lines = getattr(source, "logical_lines", None)
    if callable(logical_lines):
        for placement in placements:
            cell = placement.get("cell")
            bbox = cell.get("bbox") if isinstance(cell, dict) else None
            if not isinstance(bbox, dict):
                continue
            try:
                logical = logical_lines(
                    {"page_no": item.page_no, "bbox": bbox},
                    padding=0.0,
                )
            except Exception:
                logical = []
            if len(logical) >= 2 and _table_cell_lines_equivalent(
                placement["text"],
                logical,
            ):
                value = "\n".join(
                    str(line).strip() for line in logical if str(line).strip()
                )
                if value:
                    placement["text"] = value
                    grid[placement["row"]][placement["col"]] = value

    placements.sort(key=lambda value: (value["row"], value["col"]))
    has_explicit_header_roles = any(
        placement.get("column_header")
        or placement.get("row_header")
        or placement.get("row_section")
        for placement in placements
    )
    flagged_header_end = max(
        (
            int(placement["row"]) + int(placement.get("rowspan") or 1)
            for placement in placements
            if placement.get("column_header")
        ),
        default=0,
    )
    # A payload with no role flags uses the historical first-row header
    # convention.  Once Docling supplies explicit roles, use those roles as
    # the semantic authority; unflagged cells remain body cells even when a
    # row-header appears later in the table.
    header_rows = (
        flagged_header_end
        if has_explicit_header_roles
        else 1
        if grid
        else 0
    )
    table["_semantic_table_layout"] = placements
    table["_semantic_table_has_merged_cells"] = any(
        placement["rowspan"] > 1 or placement["colspan"] > 1
        for placement in placements
    )
    table["_semantic_table_has_header_roles"] = any(
        placement.get("header_role") for placement in placements
    )
    table["_semantic_table_markdown_degraded"] = bool(
        table["_semantic_table_has_merged_cells"]
        or table["_semantic_table_has_header_roles"]
    )
    return grid, header_rows, placements


def _table_grid(
    source: SourceReader,
    item: FlowItem,
) -> tuple[list[list[str]], int]:
    """Compatibility wrapper returning the rectangular semantic grid."""

    grid, header_rows, _placements = _table_cell_layout(source, item)
    return grid, header_rows


_DOCLING_FORMULA_PAYLOAD_RE = re.compile(
    r"<formula>(?:<loc_\d+>)+",
    flags=re.I,
)


def _formula_source_text(value: str) -> str:
    """Remove Docling location markup without discarding a richer TeX payload.

    Some Serve responses concatenate a short OCR prefix and a second, complete
    formula after ``<formula><loc_...>``.  Blindly truncating at that marker
    loses later array rows.  Prefer the post-marker payload only when it is
    demonstrably structured TeX and contains the normalized prefix; otherwise
    keep the prefix and treat the suffix as opaque converter metadata.
    """

    text = str(value or "").strip()
    marker = _DOCLING_FORMULA_PAYLOAD_RE.search(text)
    if marker is None:
        return text
    prefix = text[: marker.start()].strip()
    payload = _DOCLING_FORMULA_PAYLOAD_RE.sub("", text[marker.start() :], count=1).strip()
    # Alignment ``&`` and grouping braces can appear only in the richer array
    # copy.  Ignore them for the containment proof while retaining every byte
    # in the selected payload itself.
    compact_prefix = re.sub(r"[\s&{}]+", "", prefix)
    compact_payload = re.sub(r"[\s&{}]+", "", payload)
    structured_payload = bool(
        re.search(
            r"\\begin\s*\{\s*(?:array|aligned|align|cases|matrix|pmatrix|bmatrix)\s*\}",
            payload,
        )
        and re.search(r"(?:=|\\(?:leq|geq|Longrightarrow|Rightarrow|frac|sum|prod))", payload)
    )
    if (
        structured_payload
        and compact_prefix
        and compact_prefix in compact_payload
        and len(compact_payload) > len(compact_prefix)
    ):
        return payload
    return prefix


def _formula_tex(
    item: FlowItem,
    source: SourceReader,
) -> tuple[str, int | str | None]:
    tex = _formula_source_text(str(item.node.get("text") or ""))
    begin_array_count = len(
        re.findall(r"\\begin\s*\{\s*array\s*\}", tex)
    )
    end_array_matches = list(
        re.finditer(r"\\end\s*\{\s*array\s*\}?", tex)
    )
    # Converter output occasionally appends one duplicate ``\end{array}``
    # after the equation label.  Remove only excess closing environments.
    for match in reversed(end_array_matches[begin_array_count:]):
        tex = tex[: match.start()] + tex[match.end() :]
    number = item.node.get("_semantic_equation_number") or source.equation_number(
        item.prov
    )
    tex = re.sub(
        r"\\tag\*?\s*\{\s*\(\s*(?:\d\s*)+(?:\.\s*(?:\d\s*)+)?\)\s*\}",
        "",
        tex,
    ).strip()
    if number is not None:
        digits = r"\s*".join(
            r"\." if digit == "." else re.escape(digit)
            for digit in str(number)
        )
        label_pattern = re.compile(rf"(?:\\quad\s*)?\(\s*{digits}\s*\)")
        label_matches = list(label_pattern.finditer(tex))
        if label_matches:
            label = label_matches[-1]
            tex = (tex[: label.start()] + tex[label.end() :]).strip()
    if number is None:
        # Without independent source evidence, only an explicit display-space
        # command proves that a trailing parenthesized number is an equation
        # label.  A bare body such as ``f(1)`` or ``x+(2)`` is mathematics and
        # must never be stripped merely because it appears at the end.
        tex = re.sub(
            r"(?:\\quad|\\qquad)\s*\(\s*(?:[A-Za-z]\s*\.\s*)?"
            r"(?:\d\s*)+(?:\.\s*(?:\d\s*)+)*\)\s*[,.;]?\s*$",
            "",
            tex,
        ).strip()
    # A spaced equation label can be parsed as the argument of a formatting
    # command (for example ``\mathbf ( 1 )``).  After removing that label the
    # wrapper is syntactically dangling and changes no mathematical body;
    # retaining it makes otherwise valid TeX fail MathML conversion.
    tex = re.sub(
        r"(?:\\(?:mathbf|mathrm|mathit|mathsf|mathtt|mathcal|mathbb)\s*)+$",
        "",
        tex,
    ).rstrip()
    tex = re.sub(r"(?:\\(?:quad|qquad|,|;|!|:)\s*)+$", "", tex).rstrip()
    while tex.endswith("}") and tex.count("}") > tex.count("{"):
        tex = tex[:-1].rstrip()
    tex = re.sub(
        r"\\end\s*\{\s*array\s*\}?\s*$",
        r"\\end{array}",
        tex,
    )
    tex = re.sub(
        r"(\\(?:leq|geq|approx|sim|equiv)|=)\s*\}\s*&",
        r"\1 &",
        tex,
    )
    tex = re.sub(r"(?:\s*&)+\s*$", "", tex)
    if _formula_mathml(tex) is None:
        candidate = re.sub(r"\\(?:left|right)\s*", "", tex)
        brace_delta = candidate.count("{") - candidate.count("}")
        if brace_delta > 0:
            candidate += "}" * brace_delta
        if _formula_mathml(candidate) is not None:
            tex = candidate
    if _formula_mathml(tex) is None:
        candidate = re.sub(
            r"\\begin\s*\{\s*array\s*\}\s*\{[^{}]*\}",
            "",
            tex,
        )
        candidate = re.sub(r"\\end\s*\{\s*array\s*\}", "", candidate)
        candidate = candidate.replace(r"\\", r"\quad ").replace("&", "")
        candidate = re.sub(r"\\(?:left|right)\s*", "", candidate)
        while candidate.endswith("}") and candidate.count("}") > candidate.count("{"):
            candidate = candidate[:-1].rstrip()
        brace_delta = candidate.count("{") - candidate.count("}")
        if brace_delta > 0:
            candidate += "}" * brace_delta
        if _formula_mathml(candidate) is not None:
            tex = candidate
    return tex, number


def _formula_mathml(tex: str) -> str | None:
    try:
        from latex2mathml.converter import convert  # type: ignore

        conversion_tex = tex
        if (
            not re.match(r"\s*\\begin\s*\{\s*array\s*\}", conversion_tex)
            and re.search(
                r"(?s)(?:"
                r"(?:^|\\\\)\s*&|"
                r"(?:^|\\\\)(?:(?!\\\\).){0,500}&\s*"
                r"(?:=|\\(?:leq|geq|approx|sim|equiv|to|leftarrow|rightarrow|"
                r"stackrel|overset|underset))"
                r")",
                conversion_tex,
            )
        ):
            conversion_tex = (
                r"\begin{array}{rl}"
                + conversion_tex
                + r"\end{array}"
            )
        mathml = str(convert(conversion_tex))
        mathml = re.sub(
            r"&(?!#(?:x[0-9A-Fa-f]+|\d+);|amp;|lt;|gt;|quot;|apos;)",
            "&amp;",
            mathml,
        )
        mathml = re.sub(
            r"(<(?:mi|mo|mtext)\b[^>]*>)<(?=</(?:mi|mo|mtext)>)",
            r"\1&lt;",
            mathml,
        )
        mathml = mathml.replace("<mi>&amp;</mi>", "")
        ET.fromstring(mathml)
        return mathml
    except Exception:
        return None


def _materialize_picture_assets(
    output_dir: Path,
    documents: list[dict[str, Any]],
) -> dict[str, int]:
    pictures_dir = output_dir / "pictures"
    written = 0
    skipped = 0
    counter = 0
    extensions = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    for document in documents:
        for node in document.get("pictures") or []:
            if not isinstance(node, dict):
                continue
            counter += 1
            image = node.get("image")
            uri = str((image or {}).get("uri") or "") if isinstance(image, dict) else ""
            match = re.match(
                r"^data:(image/(?:png|jpeg|webp|gif));base64,(.+)$",
                uri,
                flags=re.DOTALL,
            )
            if not match:
                skipped += 1
                continue
            try:
                payload = base64.b64decode(match.group(2), validate=False)
            except Exception:
                skipped += 1
                continue
            if not payload:
                skipped += 1
                continue
            extension = extensions[match.group(1)]
            relative_path = f"pictures/picture_{counter}.{extension}"
            pictures_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / relative_path).write_bytes(payload)
            node["_semantic_picture_path"] = relative_path
            written += 1
    return {"written": written, "skipped": skipped}


_LEGACY_SECOND_PASS_FORMULA_RE = re.compile(
    r'(?s)<div class="docling-formula-second-pass(?: [^"]*)?"'
    r'[^>]*data-formula-index="(?P<number>[^"]+)"[^>]*>'
    r'(?P<body>.*?</pre>)\s*</div>'
)

def _formula_without_trailing_number(tex: str, number: str) -> str:
    tex = tex.strip()
    digits = r"\s*".join(
        r"\." if character == "." else re.escape(character)
        for character in number
    )
    matches = list(
        re.finditer(
            rf"\(\s*{digits}\s*\)\s*[,.;]?\s*$",
            tex,
        )
    )
    if matches:
        tex = tex[: matches[-1].start()].rstrip()
    return re.sub(
        r"(?:(?:\\quad|\\qquad|\\,|\\;|\\!)(?:\s*\\\s*)?|\s)+$",
        "",
        tex,
    ).strip()


_FORMULA_TRAILING_LABEL_RE = re.compile(
    r"(?s)(.*?)(?:\\quad|\\qquad|\\,|\\;|\\!|\s)*"
    r"\(\s*(?P<label>(?:[A-Za-z](?:\s*\.\s*\d+)+|\d+(?:\s*\.\s*\d+)*))\s*\)"
    r"\s*[,.;]?\s*$"
)


def _split_formula_trailing_label(tex: str) -> tuple[str, str]:
    """Split a display label from TeX while keeping its raw ordinal separate."""

    text = str(tex or "").strip()
    match = _FORMULA_TRAILING_LABEL_RE.fullmatch(text)
    if match is None:
        return text, ""
    label = re.sub(r"\s+", "", str(match.group("label") or ""))
    body = str(match.group(1) or "").rstrip()
    return body, label


def _semantic_formula_html(
    tex: str,
    number: str,
    formula_index: str,
) -> tuple[str, bool]:
    mathml = _formula_mathml(tex)
    number_text = html.escape(number, quote=True)
    number_html = (
        f'<span class="equation-number">({number_text})</span>'
        if number_text
        else ""
    )
    number_attribute = (
        f' data-equation="{number_text}"' if number_text else ""
    )
    index_text = html.escape(formula_index, quote=True)
    escaped_tex = html.escape(tex)
    if mathml:
        formula_body = f'<span class="formula-math">{mathml}</span>'
    else:
        formula_body = (
            '<span class="formula-math formula-tex-fallback">'
            f"<code>{escaped_tex}</code></span>"
        )
    return (
        f'<div class="formula" data-formula-index="{index_text}"{number_attribute}>'
        f"{formula_body}{number_html}"
        f"<details><summary>LaTeX</summary><code>{escaped_tex}</code></details>"
        f"<!-- source-formula-anchor:{index_text} -->"
        "</div>",
        mathml is not None,
    )


def _normalize_legacy_formula_surfaces(output_dir: Path) -> dict[str, Any]:
    html_path = output_dir / "document.html"
    markdown_path = output_dir / "document.md"
    if not html_path.exists() or not markdown_path.exists():
        raise RuntimeError("legacy formula surfaces are incomplete")
    html_text = html_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")
    matches = list(_LEGACY_SECOND_PASS_FORMULA_RE.finditer(html_text))
    if not matches:
        return {
            "applied": False,
            "formula_count": 0,
            "mathml_count": 0,
            "tex_fallback_count": 0,
            "markdown_formula_count": 0,
        }

    records: list[dict[str, str]] = []
    replacements: list[tuple[int, int, str]] = []
    mathml_count = 0
    raw_identifiers = [
        html.unescape(match.group("number")).strip() for match in matches
    ]
    used_raw_ordinals = {
        value for value in raw_identifiers if re.fullmatch(r"\d+", value)
    }
    next_raw_ordinal = 1
    for _position, (match, raw_identifier) in enumerate(
        zip(matches, raw_identifiers),
        start=1,
    ):
        # Numeric legacy identifiers are already raw formula ordinals and are
        # retained for compatibility. Decimal/alphanumeric identifiers are
        # equation labels; assign a stable one-based ordinal independent of
        # that label so `(2.1)` and `(A.15)` cannot collide with source refs.
        if re.fullmatch(r"\d+", raw_identifier):
            raw_ordinal = raw_identifier
        else:
            while str(next_raw_ordinal) in used_raw_ordinals:
                next_raw_ordinal += 1
            raw_ordinal = str(next_raw_ordinal)
            used_raw_ordinals.add(raw_ordinal)
            next_raw_ordinal += 1
        tex_versions = re.findall(
            r'(?s)<pre class="docling-formula-tex(?: [^"]*)?">(.*?)</pre>',
            match.group("body"),
        )
        if not tex_versions:
            raise RuntimeError(f"legacy formula {raw_ordinal} has no TeX node")
        raw_tex = html.unescape(tex_versions[0]).strip()
        _raw_body, inferred_equation_number = _split_formula_trailing_label(raw_tex)
        source_tex = _formula_without_trailing_number(raw_tex, raw_identifier)
        if source_tex == raw_tex:
            source_tex, fallback_equation_number = _split_formula_trailing_label(
                source_tex
            )
            inferred_equation_number = (
                inferred_equation_number or fallback_equation_number
            )
        if not source_tex:
            raise RuntimeError(f"legacy formula {raw_ordinal} has no semantic TeX")
        # The recognized document formula is authoritative.  Never select a
        # paper-specific replacement based on title, filename, or equation
        # count; formal releases must generalize to unseen papers.
        tex = source_tex
        equation_number = inferred_equation_number
        replacement, has_mathml = _semantic_formula_html(
            tex,
            equation_number,
            raw_ordinal,
        )
        mathml_count += int(has_mathml)
        records.append(
            {
                "number": raw_ordinal,
                "raw_identifier": raw_identifier,
                "equation_number": equation_number,
                "tex": tex,
                "source_tex": source_tex,
            }
        )
        replacements.append((match.start(), match.end(), replacement))

    markdown_matches = list(re.finditer(r"(?s)\$\$\s*(.*?)\s*\$\$", markdown_text))
    if len(markdown_matches) != len(records):
        raise RuntimeError(
            "legacy HTML/Markdown formula count mismatch:"
            f"{len(records)}!={len(markdown_matches)}"
        )
    markdown_replacements: list[tuple[int, int, str]] = []
    for match, record in zip(markdown_matches, records):
        raw_markdown_tex = html.unescape(match.group(1))
        _markdown_body, markdown_equation_number = _split_formula_trailing_label(
            raw_markdown_tex
        )
        markdown_tex = _formula_without_trailing_number(
            raw_markdown_tex,
            record["raw_identifier"],
        )
        if markdown_tex == raw_markdown_tex:
            markdown_tex, fallback_equation_number = _split_formula_trailing_label(
                markdown_tex
            )
            markdown_equation_number = (
                markdown_equation_number or fallback_equation_number
            )
        if re.sub(r"\s+", "", markdown_tex) != re.sub(
            r"\s+",
            "",
            record["source_tex"],
        ):
            raise RuntimeError(
                "legacy HTML/Markdown formula content mismatch:"
                + record["number"]
            )
        if markdown_equation_number != record["equation_number"]:
            raise RuntimeError(
                "legacy HTML/Markdown formula equation label mismatch:"
                + record["number"]
            )
        markdown_tag = (
            rf"\tag{{{markdown_equation_number}}}"
            if markdown_equation_number
            else ""
        )
        markdown_replacements.append(
            (
                match.start(),
                match.end(),
                "$$\n"
                + record["tex"]
                + markdown_tag
                + "\n$$\n"
                + f"<!-- source-formula-anchor:{record['number']} -->",
            )
        )

    for start, end, replacement in reversed(replacements):
        html_text = html_text[:start] + replacement + html_text[end:]
    for start, end, replacement in reversed(markdown_replacements):
        markdown_text = (
            markdown_text[:start] + replacement + markdown_text[end:]
        )

    html_text = re.sub(
        r'(?s)\s*<style id="docling-formula-second-pass-style">.*?</style>',
        "",
        html_text,
    )
    html_text = re.sub(
        r'(?s)\s*<script id="docling-formula-second-pass-mathjax">.*?</script>',
        "",
        html_text,
    )
    html_text = re.sub(
        r'(?s)\s*<script[^>]+src="https://cdn\.jsdelivr\.net/npm/'
        r'mathjax@3/es5/tex-svg\.js"[^>]*></script>',
        "",
        html_text,
    )
    semantic_style = """
<style id="docling-semantic-formula-style">
.formula{position:relative;display:flex;align-items:center;justify-content:center;
gap:1rem;margin:1.25rem 0;padding:.75rem 4.5rem .5rem 1rem;overflow-x:auto}
.formula math{font-size:1.14em;font-family:"STIX Two Math","Cambria Math",
"Noto Sans Math",math}.equation-number{position:absolute;right:1rem}
.formula details{font:12px/1.4 ui-monospace,monospace;color:#596273}
.formula-tex-fallback{font-family:"STIX Two Math","Cambria Math",
"Noto Sans Math",math}
@media(max-width:700px){.formula{padding-right:3.5rem;font-size:.9em}}
</style>
"""
    if "</head>" not in html_text:
        raise RuntimeError("legacy HTML head is missing")
    html_text = html_text.replace("</head>", semantic_style + "</head>", 1)
    html_path.write_text(html_text, encoding="utf-8")
    markdown_path.write_text(markdown_text.rstrip() + "\n", encoding="utf-8")
    return {
        "applied": True,
        "formula_count": len(records),
        "mathml_count": mathml_count,
        "tex_fallback_count": len(records) - mathml_count,
        "markdown_formula_count": len(markdown_matches),
        "equation_numbers": [
            record["equation_number"]
            for record in records
            if record["equation_number"]
        ],
        "source_verified_formula_corrections": [],
        "external_mathjax_removed": True,
    }


def _cjk_inline_math_hint(value: str) -> bool:
    """Recognize conservative inline-math-like mixing in CJK body text."""
    if not re.search(r"[\u3400-\u9fff]", value):
        return False
    if len(re.findall(r"[A-Za-z]", value)) < 1:
        return False
    if "(cid:" in value:
        return True
    return bool(_CJK_INLINE_MATH_HINT_RE.search(value))


_CJK_INLINE_MATH_STRONG_HINT_RE = re.compile(
    r"(?ix)(?:"
    r"\(cid:\d+\)"
    r"|\\(?:frac|sum|prod|int|left|right|vec|log|sin|cos|exp|lim|nabla|alpha|beta|gamma|delta|theta|lambda|pi|rho|tau|math[a-zA-Z]+)\b"
    r"|[∑∏√≤≥≠≈≡⊂⊃⊆⊇∩∪↔→←↦]"
    r"|\b[A-Za-z]\s*[_^]\s*[A-Za-z0-9]"
    r"|\b[A-Za-z]\s*[-−]\s*(?:\d|[a-z]|[A-Z])"
    r"|\b[A-Za-z]\s+\d+"
    r"|\b[A-Z]\s+[a-z]\b"
    r"|\b[nm]\s*[-−]\s*1/2\b"
    r"|ER['\"′’]?\s*[xXdNlNT]"
    r"|R['\"′’]?\s*x"
    r")"
)


def _cjk_inline_math_hint_is_strong(value: str) -> bool:
    """Return true only for explicit/script-like CJK inline notation.

    A CJK paragraph can contain ordinary English words (titles, venue names,
    classification codes) without any mathematical meaning.  Such weak
    mixed-language hints must not manufacture a source crop.  Operators,
    TeX/CID tokens, script geometry spellings, and an uppercase/lowercase
    single-letter pair are strong enough to retain as a review appendix when
    the PDF text layer itself is incomplete.
    """
    return bool(_CJK_INLINE_MATH_STRONG_HINT_RE.search(value))


def _cjk_inline_math_tight_bbox(
    bbox: dict[str, Any],
    chars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Tighten a CJK source crop around local math-like glyph evidence.

    If pypdfium exposes font/symbol evidence, union only those glyph boxes;
    otherwise retain the node provenance box (the crop remains authoritative
    and the appendix records that the text layer was incomplete).
    """
    candidates = [
        char
        for char in chars
        if isinstance(char, dict)
        and isinstance(char.get("bbox"), dict)
        and (
            _source_math_font_evidence(char)
            or re.search(r"[A-Za-z0-9∑∏√≤≥≠≈≡⊂⊃⊆⊇∩∪↔→←↦]", str(char.get("text") or ""))
        )
    ]
    boxes = [char.get("bbox") for char in candidates if isinstance(char.get("bbox"), dict)]
    if not boxes:
        return dict(bbox)
    result = dict(bbox)
    result.update(
        {
            "l": min(float(box.get("l") or 0.0) for box in boxes),
            "r": max(float(box.get("r") or 0.0) for box in boxes),
            "t": max(float(box.get("t") or 0.0) for box in boxes),
            "b": min(float(box.get("b") or 0.0) for box in boxes),
        }
    )
    return result


def _collect_cjk_inline_math_source_regions(
    output_dir: Path,
    document: dict[str, Any],
    source: SourceReader,
) -> dict[str, Any]:
    """Attach source anchors to CJK body nodes with local PDF math evidence.

    CJK papers keep the original body HTML/Markdown because the OCR surface is
    not safe to reflow.  We therefore do not rewrite the body.  For a uniquely
    identifiable text node, however, we can still expose the exact PDF crop
    next to the node so a reviewer can read symbols such as ``ER^d``. Nodes
    without local Latin/math glyph evidence are ignored rather than generating
    a document-wide collection of generic widgets.
    """
    html_path = output_dir / "document.html"
    markdown_path = output_dir / "document.md"
    html_text = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    markdown_text = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    # Strip prior inline review widgets/markers before binding this rebuild.
    # The CJK path preserves existing body HTML/Markdown, so leaving old
    # comments in place would make each retry duplicate an occurrence anchor.
    html_text = re.sub(
        r'<details\b[^>]*class="[^"]*\bdocling-inline-math-source\b[^"]*"[^>]*>.*?</details>',
        "",
        html_text,
        flags=re.S | re.I,
    )
    html_text = re.sub(
        r"\s*<!--\s*source-inline-math-anchor:[^\s>]+\s*-->",
        "",
        html_text,
        flags=re.I,
    )
    markdown_text = re.sub(
        r"\n?<!--\s*source-inline-math-visual:[^\s>]+\s*-->.*?</details>\s*",
        "\n",
        markdown_text,
        flags=re.S | re.I,
    )
    markdown_text = re.sub(
        r"\s*<!--\s*source-inline-math-anchor:[^\s>]+\s*-->",
        "",
        markdown_text,
        flags=re.I,
    )
    # Rebuilds may run repeatedly against the same output directory (for
    # example a release retry).  Replace, rather than stack, the appendix so
    # every occurrence marker remains unique for the final adapter pass.
    html_text = re.sub(
        r'(?s)<section\b[^>]*class="[^"]*\bdocling-inline-math-source-appendix\b[^"]*"[^>]*>.*?</section>',
        "",
        html_text,
        flags=re.I,
    )
    markdown_text = re.sub(
        r"(?s)\n## Inline math source review appendix\n.*?(?=\n## |\Z)",
        "\n",
        markdown_text,
    )
    regions: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    binding_diagnostics: list[dict[str, Any]] = []
    html_markers: list[tuple[str, str]] = []
    markdown_markers: list[tuple[str, str]] = []
    appendix_markers: list[tuple[str, int, int]] = []
    chunk_source = document.get("chunks")
    documents: list[tuple[int, dict[str, Any]]] = []
    if isinstance(chunk_source, list) and chunk_source:
        chunk_entries: list[tuple[tuple[int, int], int, dict[str, Any]]] = []
        for chunk_index, chunk in enumerate(chunk_source):
            if not isinstance(chunk, dict):
                continue
            chunk_document = chunk.get("document")
            if not isinstance(chunk_document, dict):
                continue
            page_range = chunk.get("page_range")
            if (
                isinstance(page_range, list)
                and len(page_range) >= 1
                and all(isinstance(value, int) for value in page_range[:2])
            ):
                chunk_key = (page_range[0], page_range[1] if len(page_range) > 1 else -1)
            else:
                chunk_key = (chunk_index, -1)
            chunk_entries.append((chunk_key, chunk_index, chunk_document))
        documents = [
            chunk_document
            for (_, _chunk_index, chunk_document) in sorted(
                chunk_entries,
                key=lambda item: item[0],
            )
        ]
    else:
        documents = [document]
    next_collection_index = 0
    for part_index, part in enumerate(documents):
        if not isinstance(part, dict):
            continue
        for node in part.get("texts") or []:
            collection_index = next_collection_index
            next_collection_index += 1
            if not isinstance(node, dict):
                continue
            if str(node.get("label") or "").lower() not in {
                "text",
                "paragraph",
                "list_item",
            }:
                continue
            node_text = str(node.get("text") or "")
            if not _cjk_inline_math_hint(node_text):
                continue
            provs = [value for value in (node.get("prov") or []) if isinstance(value, dict)]
            if len(provs) != 1 or not isinstance(provs[0].get("bbox"), dict):
                missing.append(
                    {
                        "anchor": None,
                        "collection_index": collection_index,
                        "part_index": part_index,
                        "reason": "cjk_inline_body_node_provenance_not_unique",
                    }
                )
                continue
            prov = provs[0]
            page_no = int(prov.get("page_no") or 0)
            bbox = _bbox(prov)
            if not page_no or bbox is None:
                missing.append(
                    {
                        "anchor": None,
                        "collection_index": collection_index,
                        "part_index": part_index,
                        "page_no": page_no,
                        "reason": "cjk_inline_body_node_bbox_unavailable",
                    }
                )
                continue
            chars = source._pypdfium_characters(page_no, prov.get("bbox"))
            source_evidence = _cjk_inline_math_source_evidence(node_text, chars)
            strong_hint = _cjk_inline_math_hint_is_strong(node_text)
            # A weak mixed-language hint without source glyph evidence is
            # ordinary prose (e.g. a venue title) and is not a candidate.
            # Explicit operators/scripts/CIDs remain reviewable even when the
            # PDF text layer is incomplete: retain a bbox appendix crop and
            # mark the machine surface degraded rather than hard-failing.
            if not source_evidence and not strong_hint:
                continue
            anchor = _inline_math_anchor_id(
                page_no=page_no,
                collection="texts",
                index=collection_index,
                offset=0,
                part_index=part_index,
                bbox=_cjk_inline_math_tight_bbox(bbox, chars),
            )
            marker = f"<!-- source-inline-math-anchor:{anchor} -->"
            html_node_text = html.escape(node_text, quote=False)
            binding_mode = "inline"
            if (
                not source_evidence
                or html_text.count(html_node_text) != 1
                or markdown_text.count(node_text) != 1
            ):
                # Do not inject a marker into the middle of an ambiguous
                # paragraph.  Add one occurrence marker to a review appendix
                # at the end of both semantic surfaces; the adapter already
                # replaces that marker with the exact crop in HTML/Markdown.
                binding_mode = "appendix"
                appendix_markers.append((marker, collection_index, part_index))
                binding_diagnostics.append(
                    {
                        "anchor": anchor,
                        "collection_index": collection_index,
                        "part_index": part_index,
                        "page_no": page_no,
                        "reason": (
                            "cjk_inline_body_node_math_glyph_evidence_missing"
                            if not source_evidence
                            else "cjk_inline_body_node_not_uniquely_bindable"
                        ),
                    }
                )
            else:
                html_markers.append((html_node_text, marker))
                markdown_markers.append((node_text, marker))
            regions.append(
                {
                    "anchor": anchor,
                    "page_no": page_no,
                    "bbox": _cjk_inline_math_tight_bbox(bbox, chars),
                    "source_text": _paragraph_text(source.text(prov)),
                    "collection_index": collection_index,
                    "rank": float(collection_index),
                    "part_index": part_index,
                    "binding_mode": binding_mode,
                }
            )
    for node_text, marker in html_markers:
        html_text = html_text.replace(node_text, node_text + " " + marker, 1)
    for node_text, marker in markdown_markers:
        markdown_text = markdown_text.replace(node_text, node_text + " " + marker, 1)
    if appendix_markers:
        html_appendix = (
            '<section class="docling-inline-math-source-appendix">'
            "<h2>Inline math source review appendix</h2>"
            + "".join(
                f'<div data-inline-math-source-occurrence="{collection_index}-{part_index}">'
                f"{marker}</div>"
                for marker, collection_index, part_index in appendix_markers
            )
            + "</section>"
        )
        body_close = re.search(r"</body\s*>", html_text, flags=re.I)
        if body_close is None:
            # An appendix outside the document body is invalid HTML and is
            # not reliably discoverable by the adapter.  Keep the occurrence
            # diagnostics, but surface a hard insertion failure instead of
            # silently appending after ``</html>``.
            for marker, collection_index, part_index in appendix_markers:
                missing.append(
                    {
                        "anchor": marker,
                        "collection_index": collection_index,
                        "part_index": part_index,
                        "reason": "cjk_inline_math_appendix_insertion_failed",
                    }
                )
            binding_diagnostics.append(
                {
                    "reason": "cjk_inline_math_appendix_insertion_failed",
                    "appendix_anchor_count": len(appendix_markers),
                }
            )
        else:
            html_text = (
                html_text[: body_close.start()]
                + "\n"
                + html_appendix
                + "\n"
                + html_text[body_close.start() :]
            )
        markdown_text += (
            "\n\n## Inline math source review appendix\n\n"
            + "\n".join(marker for marker, _collection_index, _part_index in appendix_markers)
            + "\n"
        )
    if html_path.exists():
        html_path.write_text(html_text, encoding="utf-8")
    if markdown_path.exists():
        markdown_path.write_text(markdown_text, encoding="utf-8")
    return {
        "regions": regions,
        "missing": missing,
        "binding_diagnostics": binding_diagnostics,
        "html_anchor_count": len(html_markers),
        "markdown_anchor_count": len(markdown_markers),
        "appendix_anchor_count": len(appendix_markers),
    }


def _remove_review_evidence_from_primary_surfaces(
    output_dir: Path,
) -> dict[str, int]:
    counts = {
        "html_appendices_removed": 0,
        "html_formula_source_links_removed": 0,
        "markdown_appendices_removed": 0,
    }
    html_path = output_dir / "document.html"
    if html_path.exists():
        html_text = html_path.read_text(encoding="utf-8")
        for css_class in (
            "docling-table-source-evidence-appendix",
            "docling-formula-source-evidence-appendix",
        ):
            html_text, removed = re.subn(
                rf'(?s)<section class="{css_class}">.*?</section>',
                "",
                html_text,
            )
            counts["html_appendices_removed"] += removed
        html_text, removed = re.subn(
            r'(?s)<div class="docling-formula-source">.*?</div>',
            "",
            html_text,
        )
        counts["html_formula_source_links_removed"] += removed
        html_path.write_text(html_text, encoding="utf-8")

    markdown_path = output_dir / "document.md"
    if markdown_path.exists():
        markdown_text = markdown_path.read_text(encoding="utf-8")
        markdown_text, removed = re.subn(
            r"(?s)\n## Original table renderings\s*\n.*$",
            "\n",
            markdown_text,
        )
        counts["markdown_appendices_removed"] += removed
        markdown_path.write_text(markdown_text.rstrip() + "\n", encoding="utf-8")
    return counts


def _heading_level(text: str, node: dict[str, Any]) -> int:
    match = re.match(r"^\s*(\d+(?:\.\d+)*)\b", text)
    if match:
        return min(6, match.group(1).count(".") + 2)
    return min(6, max(1, int(node.get("level") or 1)))


def _markdown_table(
    grid: list[list[str]],
    reference_texts: list[tuple[int, str]] | None = None,
    footnote_callouts: dict[tuple[int, int], list[tuple[str, str]]] | None = None,
) -> str:
    if not grid:
        return ""

    def cell(value: str, row: int, column: int) -> str:
        value = _inline_replacements(
            value,
            reference_texts or [],
            (footnote_callouts or {}).get((row, column), []),
            markdown=True,
        )
        value = value.replace("|", "&#124;")
        return "<br>".join(part.strip() for part in value.splitlines())

    header = (
        "| "
        + " | ".join(
            cell(value, 0, column) for column, value in enumerate(grid[0])
        )
        + " |"
    )
    separator = "| " + " | ".join("---" for _ in grid[0]) + " |"
    rows = [
        "| "
        + " | ".join(
            cell(value, row_index, column)
            for column, value in enumerate(row)
        )
        + " |"
        for row_index, row in enumerate(grid[1:], start=1)
    ]
    return "\n".join([header, separator, *rows])


def _reference_items(
    items: list[FlowItem],
) -> tuple[dict[int, int], list[tuple[int, str]]]:
    references: dict[int, int] = {}
    reference_texts: list[tuple[int, str]] = []
    in_references = False
    reference_level = 7
    last_reference_item: FlowItem | None = None
    for index, item in enumerate(items):
        if item.kind == "heading":
            heading = _paragraph_text(str(item.node.get("text") or item.source_text))
            level = _heading_level(heading, item.node)
            if re.fullmatch(r"(?i)(?:references|bibliography)", heading.strip()):
                in_references = True
                reference_level = level
                continue
            if in_references and level <= reference_level:
                following_candidates = [
                    candidate
                    for candidate in items[index + 1 : index + 5]
                    if _paragraph_text(candidate.source_text)
                ]
                following = (
                    following_candidates[0]
                    if following_candidates
                    else None
                )
                following_text = (
                    _paragraph_text(following.source_text)
                    if following is not None
                    else ""
                )
                following_window_text = " ".join(
                    _paragraph_text(candidate.source_text)
                    for candidate in following_candidates
                )
                page_header_between_references = bool(
                    following is not None
                    and following.kind in {"text", "list_item", "footnote"}
                    and re.search(r"(?:19|20)\d{2}", following_window_text)
                    and re.search(
                        r"^[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+",
                        following_text,
                    )
                    and len(heading) <= 120
                    and not re.match(
                        r"(?i)^(?:appendix|supplement|acknowledg|"
                        r"author contributions?|limitations?)\b",
                        heading,
                    )
                )
                if page_header_between_references:
                    item.kind = "reference_page_header"
                    continue
                in_references = False
        if (
            in_references
            and item.kind in {"text", "list_item", "footnote"}
            and (text := _paragraph_text(item.source_text))
        ):
            if (
                last_reference_item is not None
                and not re.search(r"[.!?]\s*$", last_reference_item.source_text)
                and re.match(r"^[a-zà-öø-ÿ]", text)
            ):
                last_reference_item.source_text = (
                    last_reference_item.source_text.rstrip()
                    + " "
                    + text.lstrip()
                )
                previous_number, previous_text = reference_texts[-1]
                reference_texts[-1] = (
                    previous_number,
                    previous_text.rstrip() + " " + text.lstrip(),
                )
                item.kind = "reference_continuation"
                continue
            number = len(reference_texts) + 1
            references[id(item)] = number
            reference_texts.append((number, re.sub(r"^\[\d+\]\s*", "", text)))
            last_reference_item = item
    return references, reference_texts


_STAR_FOOTNOTE_MARKERS = "*∗⋆★✶⁎"


def _canonical_footnote_marker(marker: str) -> str:
    if marker and all(char in _STAR_FOOTNOTE_MARKERS for char in marker):
        return "*" * len(marker)
    return marker


def _footnote_marker_pattern(marker: str) -> re.Pattern[str]:
    canonical = _canonical_footnote_marker(marker)
    if canonical and set(canonical) == {"*"}:
        return re.compile(
            rf"[{re.escape(_STAR_FOOTNOTE_MARKERS)}]{{{len(canonical)}}}"
        )
    if marker.isdigit():
        return re.compile(rf"(?<![\w\[])({re.escape(marker)})(?![\w\]])")
    return re.compile(re.escape(marker))


def _footnote_relations(
    items: list[FlowItem],
    reference_items: dict[int, int],
    document: dict[str, Any] | None = None,
) -> tuple[dict[int, tuple[str, str, str]], dict[int, list[tuple[str, str]]]]:
    footnotes: dict[int, tuple[str, str, str]] = {}
    callouts: dict[int, list[tuple[str, str]]] = {}
    marker_counts: dict[str, int] = {}
    for index, item in enumerate(items):
        if item.kind != "footnote" or id(item) in reference_items:
            continue
        text = _paragraph_text(item.source_text)
        match = re.match(
            rf"^(\d+|[{re.escape(_STAR_FOOTNOTE_MARKERS)}†‡§]+)\s+(.+)$",
            text,
        )
        if not match:
            continue
        marker, body = match.groups()
        canonical_marker = _canonical_footnote_marker(marker)
        marker_counts[canonical_marker] = marker_counts.get(canonical_marker, 0) + 1
        marker_name = {
            "*": "star",
            "†": "dagger",
            "‡": "double-dagger",
            "§": "section",
        }.get(canonical_marker, canonical_marker)
        suffix = marker_counts[canonical_marker]
        footnote_id = f"{marker_name}-{suffix}"
        footnotes[id(item)] = (footnote_id, marker, body)
        marker_pattern = _footnote_marker_pattern(marker)
        linked = False
        for candidate in reversed(items[max(0, index - 24) : index]):
            if candidate.kind not in {"title", "heading", "text", "list_item"}:
                continue
            candidate_text = _paragraph_text(
                candidate.source_text or str(candidate.node.get("text") or "")
            )
            if (
                candidate.kind == "heading"
                and marker.isdigit()
                and re.match(rf"^{re.escape(marker)}(?:\.|\s|$)", candidate_text)
            ):
                continue
            marker_match = marker_pattern.search(candidate_text)
            if marker_match:
                callouts.setdefault(id(candidate), []).append(
                    (marker_match.group(0), footnote_id)
                )
                linked = True
                break
        if linked or document is None:
            continue
        for candidate in items[index + 1 : min(len(items), index + 8)]:
            candidate_text = ""
            if candidate.kind == "table":
                candidate_text = _caption_text(document, candidate.node)
            elif candidate.kind in {"title", "heading", "text", "list_item"}:
                candidate_text = _paragraph_text(
                    candidate.source_text or str(candidate.node.get("text") or "")
                )
            marker_match = marker_pattern.search(candidate_text)
            if marker_match:
                callouts.setdefault(id(candidate), []).append(
                    (marker_match.group(0), footnote_id)
                )
                break
    return footnotes, callouts


def _table_note_relations(
    items: list[FlowItem],
) -> tuple[
    dict[int, tuple[str, str, str]],
    dict[int, list[tuple[str, str]]],
]:
    notes: dict[int, tuple[str, str, str]] = {}
    table_callouts: dict[int, list[tuple[str, str]]] = {}
    counter = 0
    for index, item in enumerate(items):
        if item.kind != "table":
            continue
        for candidate in items[index + 1 : min(len(items), index + 5)]:
            text = _paragraph_text(candidate.source_text)
            if not text:
                continue
            match = re.match(r"^([∗*†‡§]+)\s+(.+)$", text)
            if not match or candidate.kind not in {"text", "footnote"}:
                break
            marker, body = match.groups()
            counter += 1
            note_id = f"table-{counter}"
            notes[id(candidate)] = (note_id, marker, body)
            callout_marker = "*" * len(marker) if set(marker) == {"∗"} else marker
            table_callouts.setdefault(id(item), []).append(
                (callout_marker, note_id)
            )
    return notes, table_callouts


def _normalized_lookup(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(
        char
        for char in decomposed
        if not unicodedata.combining(char) and char.isalnum()
    )


_AUTHOR_YEAR_PATTERNS = (
    re.compile(
        r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+\s+(?:and|&)\s+"
        r"(?:[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+\s+){1,2}"
        r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+\s+"
        r"(?:\[(?:19|20)\d{2}[a-z]?\]|\((?:19|20)\d{2}[a-z]?\)|"
        r",?\s*(?:19|20)\d{2}[a-z]?)"
    ),
    re.compile(
        r"\b(?:[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+)"
        r"(?:\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+|\s+et al\.)?"
        r"\s+\[(?:19|20)\d{2}[a-z]?\]"
    ),
    re.compile(
        r"\b(?:[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+)"
        r"(?:\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+|\s+et al\.)?"
        r"\s+\((?:19|20)\d{2}[a-z]?\)"
    ),
    re.compile(
        r"\((?:[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+)"
        r"(?:\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+|\s+et al\.)?"
        r",\s*(?:19|20)\d{2}[a-z]?\)"
    ),
    re.compile(
        r"\b(?:[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+)"
        r"(?:\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+|\s+et al\.)?"
        r",\s*(?:19|20)\d{2}[a-z]?"
    ),
    re.compile(
        r"\b(?:[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+)"
        r"(?:\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+|\s+et al\.)?"
        r"\.?\s+(?:19|20)\d{2}[a-z]?"
    ),
)

_SQUARE_AUTHOR_YEAR_SEGMENT_RE = re.compile(
    r"(?P<label>[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+"
    r"(?:\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+|\s+et al\.)?"
    r"\s+(?:19|20)\d{2}[a-z]?)"
)


def _reference_author_segment(reference: str) -> str:
    for match in re.finditer(r"\.", reference):
        preceding = re.search(
            r"([^\W\d_]+(?:['’.-][^\W\d_]+)*)\s*$",
            reference[: match.start()],
        )
        token = (
            re.sub(r"[\W\d_]", "", preceding.group(1))
            if preceding
            else ""
        )
        if len(token) == 1 and token.isupper():
            continue
        return reference[: match.start()]
    return reference


def _reference_first_author_raw(reference: str) -> str:
    et_al = re.match(
        r"\s*([^\W\d_]+(?:['’.-][^\W\d_]+)*)\s+et al\.",
        reference,
        flags=re.IGNORECASE,
    )
    if et_al:
        return et_al.group(1)
    first_author = re.split(
        r",|\band\b|;",
        _reference_author_segment(reference),
        maxsplit=1,
    )[0]
    names = re.findall(r"[^\W\d_]+(?:['’.-][^\W\d_]+)*", first_author)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    non_initials = [
        name
        for name in names
        if len(re.sub(r"[\W\d_]", "", name)) > 1
    ]
    return (non_initials or names)[-1]


def _reference_first_author_surname(reference: str) -> str:
    return _normalized_lookup(_reference_first_author_raw(reference))


def _author_year_target(
    label: str,
    reference_texts: list[tuple[int, str]],
) -> int | None:
    year_match = re.search(
        r"((?:19|20)\d{2})([a-z]?)",
        label,
        flags=re.IGNORECASE,
    )
    names = re.findall(r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+", label)
    if not year_match or not names:
        return None
    surnames = [_normalized_lookup(name.rstrip(".")) for name in names]
    year = year_match.group(1)
    year_suffix = year_match.group(2).casefold()
    year_token = f"{year}{year_suffix}"
    citation_uses_et_al = bool(re.search(r"\bet al\.", label, flags=re.IGNORECASE))
    citation_primary_raw = unicodedata.normalize(
        "NFC",
        names[0].rstrip("."),
    ).casefold()
    matches: list[tuple[int, bool, bool]] = []
    for index, (number, reference) in enumerate(reference_texts):
        normalized_reference = _normalized_lookup(reference)
        first_author_raw = _reference_first_author_raw(reference)
        first_author_surname = _reference_first_author_surname(reference)
        next_text = (
            reference_texts[index + 1][1]
            if index + 1 < len(reference_texts)
            else ""
        )
        reference_year_tokens = [
            value.casefold()
            for value in re.findall(
                r"(?:19|20)\d{2}[a-z]?",
                reference,
                flags=re.IGNORECASE,
            )
        ]
        has_names = (
            (
                not first_author_surname
                or surnames[0] == first_author_surname
                or (
                    first_author_surname.endswith(surnames[0])
                    and len(first_author_surname) - len(surnames[0]) <= 16
                )
            )
            and all(
                surname in normalized_reference
                for surname in surnames[1:]
            )
        )
        supports_et_al = (
            bool(
                re.match(
                    r"\s*[^\W\d_]+(?:['’.-][^\W\d_]+)*\s+et al\.",
                    reference,
                    flags=re.IGNORECASE,
                )
            )
            or _reference_author_segment(reference).count(",") >= 2
        )
        exact_primary_spelling = (
            unicodedata.normalize("NFC", first_author_raw).casefold()
            == citation_primary_raw
        )
        has_year = (
            year_token in reference_year_tokens
            if year_suffix
            else any(value.startswith(year) for value in reference_year_tokens)
        )
        if has_names and (
            has_year
            or (
                not reference_year_tokens
                and re.search(
                    rf"\b{re.escape(year_token)}\b",
                    next_text,
                    flags=re.IGNORECASE,
                )
            )
        ):
            matches.append((number, supports_et_al, exact_primary_spelling))
    if citation_uses_et_al and any(match[1] for match in matches):
        matches = [match for match in matches if match[1]]
    if len(matches) > 1 and any(match[2] for match in matches):
        matches = [match for match in matches if match[2]]
    return matches[0][0] if len(matches) == 1 else None


def _inline_replacements(
    text: str,
    reference_texts: list[tuple[int, str]],
    footnote_callouts: list[tuple[str, str]],
    *,
    markdown: bool,
) -> str:
    replacements: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []

    def available(start: int, end: int) -> bool:
        return not any(start < other_end and end > other_start for other_start, other_end in occupied)

    for marker, footnote_id in footnote_callouts:
        pattern = (
            re.compile(rf"(?<![\w\[])({re.escape(marker)})(?![\w\]])")
            if marker.isdigit()
            else re.compile(re.escape(marker))
        )
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        match = matches[-1]
        visible = html.escape(marker, quote=False)
        replacement = (
            f'<sup id="fnref-{footnote_id}"><a href="#fn-{footnote_id}">{visible}</a></sup>'
        )
        replacements.append((match.start(), match.end(), replacement))
        occupied.append((match.start(), match.end()))

    reference_count = len(reference_texts)
    for match in re.finditer(r"\[([0-9][0-9,\-–\s]*)\]", text):
        numbers = [int(value) for value in re.findall(r"\d+", match.group(1))]
        if (
            not numbers
            or any(number < 1 or number > reference_count for number in numbers)
            or not available(match.start(), match.end())
        ):
            continue
        if markdown:
            linked = re.sub(
                r"\d+",
                lambda number: f'[{number.group(0)}](#ref-{number.group(0)})',
                match.group(1),
            )
            replacement = f"[{linked}]"
        else:
            linked = re.sub(
                r"\d+",
                lambda number: (
                    f'<a class="citation" href="#ref-{number.group(0)}">'
                    f"{number.group(0)}</a>"
                ),
                html.escape(match.group(1), quote=False),
            )
            replacement = f"[{linked}]"
        replacements.append((match.start(), match.end(), replacement))
        occupied.append((match.start(), match.end()))

    for group in re.finditer(r"\[([^\[\]\n]{3,500})\]", text):
        if not available(group.start(), group.end()):
            continue
        parts = re.split(r"(;\s*)", group.group(1))
        linked = 0
        rendered_parts: list[str] = []
        for part in parts:
            if part.startswith(";"):
                rendered_parts.append(part)
                continue
            leading = part[: len(part) - len(part.lstrip())]
            trailing = part[len(part.rstrip()) :]
            core = part.strip()
            segment = _SQUARE_AUTHOR_YEAR_SEGMENT_RE.fullmatch(core)
            number = (
                _author_year_target(segment.group("label"), reference_texts)
                if segment
                else None
            )
            if number is None:
                rendered_core = (
                    core if markdown else html.escape(core, quote=False)
                )
            else:
                visible = (
                    core if markdown else html.escape(core, quote=False)
                )
                rendered_core = (
                    f"[{visible}](#ref-{number})"
                    if markdown
                    else (
                        f'<a class="citation" href="#ref-{number}">'
                        f"{visible}</a>"
                    )
                )
                linked += 1
            rendered_parts.append(leading + rendered_core + trailing)
        if not linked:
            continue
        replacement = "[" + "".join(rendered_parts) + "]"
        replacements.append((group.start(), group.end(), replacement))
        occupied.append((group.start(), group.end()))

    for pattern in _AUTHOR_YEAR_PATTERNS:
        for match in pattern.finditer(text):
            if not available(match.start(), match.end()):
                continue
            number = _author_year_target(match.group(0), reference_texts)
            if number is None:
                continue
            visible = (
                html.escape(match.group(0), quote=False)
                if not markdown
                else match.group(0)
            )
            replacement = (
                f"[{visible}](#ref-{number})"
                if markdown
                else f'<a class="citation" href="#ref-{number}">{visible}</a>'
            )
            replacements.append((match.start(), match.end(), replacement))
            occupied.append((match.start(), match.end()))

    replacements.sort(key=lambda value: value[0])
    output: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        if start < cursor:
            continue
        raw = text[cursor:start]
        output.append(raw if markdown else html.escape(raw, quote=False))
        output.append(replacement)
        cursor = end
    tail = text[cursor:]
    output.append(tail if markdown else html.escape(tail, quote=False))
    return "".join(output)


_ALGORITHM_KEYWORDS = re.compile(
    r"(?i)\b(Require|Ensure|Input|Output|Parameters?|Initialize|Set|Sample|Draw|Choose|Construct|"
    r"Form|Accept|Stop|Return|for|do|if|then|else|end\s+if|end\s+for)\b"
)


def _highlight_algorithm_html(code: str) -> str:
    def escape_with_math_fonts(value: str) -> str:
        clusters: list[str] = []
        for character in value:
            if unicodedata.combining(character) and clusters:
                clusters[-1] += character
            else:
                clusters.append(character)
        rendered_clusters: list[str] = []
        for cluster in clusters:
            visible = html.escape(cluster, quote=False)
            if any(ord(character) > 127 for character in cluster):
                rendered_clusters.append(
                    '<span class="alg-symbol" '
                    'style="font-family:&quot;STIX Two Math&quot;,&quot;Cambria Math&quot;,'
                    '&quot;Apple Symbols&quot;,&quot;Noto Sans Math&quot;,math,serif">'
                    + visible
                    + "</span>"
                )
            else:
                rendered_clusters.append(visible)
        return "".join(rendered_clusters)

    rendered: list[str] = []
    for raw_line in _normalize_detached_diacritics(code).splitlines():
        body, separator, comment = raw_line.partition("//")
        escaped = escape_with_math_fonts(body)
        escaped = re.sub(
            r'(<span class="alg-symbol"[^>]*>[A-Za-z]⃗</span>)'
            r"_([A-Za-z0-9_]+)\^([A-Za-z0-9_]+)",
            r"\1<sub>\2</sub><sup>\3</sup>",
            escaped,
        )
        escaped = re.sub(
            r'(<span class="alg-symbol"[^>]*>[A-Za-z]⃗</span>)'
            r"_([A-Za-z0-9_]+)",
            r"\1<sub>\2</sub>",
            escaped,
        )
        escaped = re.sub(
            r"\b([A-Za-z])_([A-Za-z0-9]+)",
            r"\1<sub>\2</sub>",
            escaped,
        )
        escaped = re.sub(
            r"\b([A-Za-z])\^\((.*?)\)",
            r"\1<sup>(\2)</sup>",
            escaped,
        )
        escaped = re.sub(
            r'(<span class="alg-symbol"[^>]*>ℝ</span>)\^\((.*?)\)',
            r"\1<sup>(\2)</sup>",
            escaped,
        )
        escaped = re.sub(
            r"^(\s*)(\d+)(\s+)",
            r'\1<span class="line-number">\2</span>\3',
            escaped,
        )
        escaped = _ALGORITHM_KEYWORDS.sub(
            r'<strong class="alg-keyword">\1</strong>',
            escaped,
        )
        if separator:
            escaped += (
                '<em class="alg-comment">//'
                + escape_with_math_fonts(comment)
                + "</em>"
            )
        rendered.append(escaped)
    return "\n".join(rendered)


def _highlight_python_html(code: str) -> str:
    code = _normalize_detached_diacritics(code)
    lines = code.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def absolute(position: tuple[int, int]) -> int:
        row, column = position
        return offsets[min(max(row - 1, 0), len(offsets) - 1)] + column

    classes = {
        token.COMMENT: "code-comment",
        token.STRING: "code-string",
        token.NUMBER: "code-number",
    }
    output: list[str] = []
    cursor = 0
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        for current in tokens:
            if current.type in {
                token.ENDMARKER,
                token.ENCODING,
                token.INDENT,
                token.DEDENT,
            }:
                continue
            start = absolute(current.start)
            end = absolute(current.end)
            if start < cursor:
                continue
            output.append(html.escape(code[cursor:start], quote=False))
            css_class = classes.get(current.type)
            if current.type == token.NAME and keyword.iskeyword(current.string):
                css_class = "code-keyword"
            visible = html.escape(current.string, quote=False)
            output.append(
                f'<span class="{css_class}">{visible}</span>'
                if css_class
                else visible
            )
            cursor = end
    except (IndentationError, tokenize.TokenError):
        return html.escape(code, quote=False)
    output.append(html.escape(code[cursor:], quote=False))
    return "".join(output)


def _inline_math_anchor_comment(anchor: str | None) -> str:
    return f"<!-- source-inline-math-anchor:{anchor} -->" if anchor else ""


def _inline_math_source_region_records(
    item: FlowItem,
    *,
    part_index: int,
) -> list[dict[str, Any]]:
    """Return source crops only for unresolved inline spans.

    Repaired-only paragraphs remain a clean machine surface and should not
    acquire a broad, paragraph-sized review image.  When an unresolved
    diagnostic has no tight geometry, fall back to the item's provenance box
    explicitly and mark that degradation for the adapter/UI.
    """

    if not item.inline_math_source_anchor or not item.inline_math_unresolved_regions:
        return []
    records: list[dict[str, Any]] = []
    for cluster_index, cluster in enumerate(
        item.inline_math_unresolved_regions,
        start=1,
    ):
        if not isinstance(cluster, dict):
            continue
        cluster_anchor = (
            f"{item.inline_math_source_anchor}-cluster{cluster_index}"
        )
        cluster["anchor"] = cluster_anchor
        cluster_bbox = cluster.get("bbox")
        try:
            bbox_is_usable = (
                isinstance(cluster_bbox, dict)
                and abs(
                    float(cluster_bbox.get("r") or 0.0)
                    - float(cluster_bbox.get("l") or 0.0)
                ) > 0.0
                and abs(
                    float(cluster_bbox.get("t") or 0.0)
                    - float(cluster_bbox.get("b") or 0.0)
                ) > 0.0
            )
        except (TypeError, ValueError):
            bbox_is_usable = False
        fallback_whole_paragraph = not bbox_is_usable
        if fallback_whole_paragraph:
            cluster_bbox = dict(item.bbox)
        records.append(
            {
                "anchor": cluster_anchor,
                "page_no": item.page_no,
                "bbox": cluster_bbox,
                "repair_bbox": cluster.get("repair_bbox"),
                "source_text": str(cluster.get("source_text") or item.source_text),
                "collection_index": item.collection_index,
                "rank": item.rank,
                "part_index": part_index,
                "unresolved": True,
                "reason": str(
                    cluster.get("reason")
                    or (
                        "inline_math_unresolved_fallback_whole_paragraph"
                        if fallback_whole_paragraph
                        else "inline_math_geometry_unresolved"
                    )
                ),
                "fallback_whole_paragraph": fallback_whole_paragraph,
            }
        )
    return records


def _structure_block_source_ref(item: FlowItem) -> str:
    """Return a stable identity token for a rendered structure block.

    Docling nodes normally carry ``self_ref`` (for example ``#/tables/2``),
    which is the only identity that survives a reordering of the body flow.
    A few synthesized algorithm blocks do not retain that field, so retain the
    original collection index on ``FlowItem`` and use a deterministic
    kind/index fallback.  Keep the token single-line and HTML-comment safe;
    the HTML caller still performs attribute escaping.
    """

    node = item.node if isinstance(item.node, dict) else {}
    raw_ref = str(node.get("self_ref") or "").strip()
    if raw_ref:
        # A malformed external self_ref must not break a Markdown comment (or
        # inject a second HTML comment).  Ordinary Docling refs are unchanged.
        source_ref = (
            raw_ref.replace("\r", " ").replace("\n", " ").replace("--", "- -")
        )
    else:
        kind = {
            "algorithm": "algorithm",
            "code": "code",
            "table": "table",
        }.get(item.kind, str(item.kind or "structure"))
        index = item.collection_index if item.collection_index is not None else 0
        source_ref = f"{kind}:{int(index)}"
    part_index = node.get("_local_ai_lab_chunk_part_index")
    if isinstance(part_index, int) and not isinstance(part_index, bool):
        return f"chunk:{part_index}:{source_ref}"
    return source_ref


def _render(
    items: list[FlowItem],
    document: dict[str, Any],
    source: SourceReader,
    *,
    shared_reference_texts: list[tuple[int, str]] | None = None,
    reference_number_offset: int = 0,
    formula_index_offset: int = 0,
) -> tuple[str, str, dict[str, int]]:
    title = str(document.get("name") or "Converted paper")
    html_parts: list[str] = []
    md_parts: list[str] = []
    reference_items, local_reference_texts = _reference_items(items)
    if reference_number_offset:
        reference_items = {
            item_id: number + reference_number_offset
            for item_id, number in reference_items.items()
        }
        local_reference_texts = [
            (number + reference_number_offset, text)
            for number, text in local_reference_texts
        ]
    reference_texts = shared_reference_texts or local_reference_texts
    footnotes, footnote_callouts = _footnote_relations(
        items,
        reference_items,
        document,
    )
    table_notes, table_footnote_callouts = _table_note_relations(items)
    linked_footnote_ids = {
        footnote_id
        for values in footnote_callouts.values()
        for _marker, footnote_id in values
    }
    linked_table_note_ids = {
        footnote_id
        for values in table_footnote_callouts.values()
        for _marker, footnote_id in values
    }
    counts = {
        "text": 0,
        "headings": 0,
        "formulas": 0,
        "tables": 0,
        "algorithms": 0,
        "code_blocks": 0,
        "pictures": 0,
        "inline_math_repairs": 0,
    }
    picture_counter = {
        id(node): index
        for index, node in enumerate(document.get("pictures") or [], start=1)
        if isinstance(node, dict)
    }
    for item in items:
        node = item.node
        if item.kind == "title":
            text = _paragraph_text(str(node.get("text") or title))
            html_parts.append(
                "<h1>"
                + _inline_replacements(
                    text,
                    reference_texts,
                    footnote_callouts.get(id(item), []),
                    markdown=False,
                )
                + "</h1>"
            )
            md_parts.extend(
                [
                    "# "
                    + _inline_replacements(
                        text,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=True,
                    ),
                    "",
                ]
            )
        elif item.kind == "heading":
            text = _paragraph_text(str(node.get("text") or ""))
            level = _heading_level(text, node)
            html_parts.append(
                f"<h{level}>"
                + _inline_replacements(
                    text,
                    reference_texts,
                    footnote_callouts.get(id(item), []),
                    markdown=False,
                )
                + f"</h{level}>"
            )
            md_parts.extend(
                [
                    f"{'#' * level} "
                    + _inline_replacements(
                        text,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=True,
                    ),
                    "",
                ]
            )
            counts["headings"] += 1
        elif item.kind in {"text", "list_item", "footnote"}:
            text = _paragraph_text(item.source_text)
            if not text:
                continue
            reference_number = reference_items.get(id(item))
            unresolved_anchor_comments = "".join(
                _inline_math_anchor_comment(str(region.get("anchor") or ""))
                for region in item.inline_math_unresolved_regions
                if region.get("anchor")
            )
            anchor_comments = unresolved_anchor_comments
            if reference_number is not None:
                text = re.sub(r"^\[\d+\]\s*", "", text)
                html_parts.append(
                    f'<div class="reference-entry" id="ref-{reference_number}">'
                    f'<span class="reference-number">[{reference_number}]</span> '
                    f"{html.escape(text)}</div>{anchor_comments}"
                )
                md_parts.extend(
                    [
                        f'<a id="ref-{reference_number}"></a>'
                        f"[{reference_number}] {text}"
                        + (f"\n{anchor_comments}" if anchor_comments else ""),
                        "",
                    ]
                )
            elif id(item) in table_notes:
                note_id, marker, body = table_notes[id(item)]
                backref_html = (
                    f' <a class="footnote-backref" href="#fnref-{note_id}" '
                    f'aria-label="Back to table note callout">↩</a>'
                    if note_id in linked_table_note_ids
                    else ""
                )
                backref_md = (
                    f" [↩](#fnref-{note_id})"
                    if note_id in linked_table_note_ids
                    else ""
                )
                linked_body_html = _inline_replacements(
                    body,
                    reference_texts,
                    [],
                    markdown=False,
                )
                linked_body_md = _inline_replacements(
                    body,
                    reference_texts,
                    [],
                    markdown=True,
                )
                html_parts.append(
                    f'<aside class="footnote table-note" id="fn-{note_id}">'
                    f'<span class="footnote-label">{html.escape(marker)}</span> '
                    f"{linked_body_html}{backref_html}</aside>"
                    + anchor_comments
                )
                md_parts.extend(
                    [
                        f'<a id="fn-{note_id}"></a><sup>{marker}</sup> '
                        f"{linked_body_md}{backref_md}"
                        + (f"\n{anchor_comments}" if anchor_comments else ""),
                        "",
                    ]
                )
            elif item.kind == "list_item":
                html_parts.append(
                    "<ul><li>"
                    + _inline_replacements(
                        text,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=False,
                    )
                    + "</li></ul>" + anchor_comments
                )
                md_parts.extend(
                    [
                        "- "
                        + _inline_replacements(
                            text,
                            reference_texts,
                            footnote_callouts.get(id(item), []),
                            markdown=True,
                        ),
                        anchor_comments if anchor_comments else "",
                        "",
                    ]
                )
            elif item.kind == "footnote" and id(item) in footnotes:
                footnote_id, marker, body = footnotes[id(item)]
                backref_html = (
                    f' <a class="footnote-backref" href="#fnref-{footnote_id}" '
                    f'aria-label="Back to footnote callout">↩</a>'
                    if footnote_id in linked_footnote_ids
                    else ""
                )
                backref_md = (
                    f" [↩](#fnref-{footnote_id})"
                    if footnote_id in linked_footnote_ids
                    else ""
                )
                linked_body_html = _inline_replacements(
                    body,
                    reference_texts,
                    [],
                    markdown=False,
                )
                linked_body_md = _inline_replacements(
                    body,
                    reference_texts,
                    [],
                    markdown=True,
                )
                html_parts.append(
                    f'<aside class="footnote" id="fn-{footnote_id}">'
                    f'<span class="footnote-label">{html.escape(marker)}</span> '
                    f"{linked_body_html}{backref_html}</aside>"
                    + anchor_comments
                )
                md_parts.extend(
                    [
                        f'<a id="fn-{footnote_id}"></a><sup>{marker}</sup> '
                        f"{linked_body_md}{backref_md}"
                        + (f"\n{anchor_comments}" if anchor_comments else ""),
                        "",
                    ]
                )
            elif item.kind == "footnote":
                html_parts.append(
                    '<aside class="footnote">'
                    + _inline_replacements(
                        text,
                        reference_texts,
                        [],
                        markdown=False,
                    )
                    + "</aside>" + anchor_comments
                )
                md_parts.extend(
                    [
                        "> Footnote: "
                        + _inline_replacements(
                            text,
                            reference_texts,
                            [],
                            markdown=True,
                        ),
                        anchor_comments if anchor_comments else "",
                        "",
                    ]
                )
            else:
                html_parts.append(
                    "<p>"
                    + _inline_replacements(
                        text,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=False,
                    )
                    + "</p>" + anchor_comments
                )
                md_parts.extend(
                    [
                        _inline_replacements(
                            text,
                            reference_texts,
                            footnote_callouts.get(id(item), []),
                            markdown=True,
                        ),
                        anchor_comments if anchor_comments else "",
                        "",
                    ]
                )
            counts["text"] += 1
            counts["inline_math_repairs"] += int(item.inline_math_repaired)
        elif item.kind in {"algorithm", "code"}:
            algorithm = item.kind == "algorithm"
            block_title, code = _preformatted_block(source, item, algorithm=algorithm)
            if not code:
                code = item.source_text or str(node.get("text") or "")
            caption = _caption_text(document, node)
            title_text = block_title or caption
            css_class = "algorithm" if algorithm else "code-listing"
            source_ref = _structure_block_source_ref(item)
            escaped_source_ref = html.escape(source_ref, quote=True)
            html_parts.append(f'<section class="{css_class}">')
            if title_text:
                html_parts.append(
                    f'<div class="{css_class}-title">{html.escape(title_text)}</div>'
                )
            highlighted = (
                _highlight_algorithm_html(code)
                if algorithm
                else _highlight_python_html(code)
            )
            html_parts.append(
                f'<pre data-source-ref="{escaped_source_ref}"><code>'
                f"{highlighted}</code></pre>"
            )
            html_parts.append("</section>")
            md_parts.append(f"<!-- source-ref:{source_ref} -->")
            md_parts.append(f"<!-- source-{item.kind}-ref:{source_ref} -->")
            if title_text:
                md_parts.extend([f"**{title_text}**", ""])
            if algorithm:
                md_parts.extend(
                    [
                        '<pre class="algorithm"><code>'
                        + _highlight_algorithm_html(code)
                        + "</code></pre>",
                        "",
                    ]
                )
            else:
                md_parts.extend(["```python", code, "```", ""])
            counts["algorithms" if algorithm else "code_blocks"] += 1
        elif item.kind == "formula":
            tex, number = _formula_tex(item, source)
            # Preserve the exact source-reconstructed body used to render the
            # authoritative surfaces.  The converter JSON may contain a
            # malformed array wrapper or may have absorbed a right-margin
            # equation label into a formatting command.  Final occurrence
            # identity must compare against this PDF-grounded body, while the
            # crop manifest continues to bind the immutable raw node.
            node["_local_ai_lab_semantic_formula_tex"] = tex
            mathml = _formula_mathml(tex)
            source_formula_index = formula_index_offset + item.collection_index + 1
            number_html = (
                f'<span class="equation-number">({number})</span>'
                if number is not None
                else ""
            )
            if mathml:
                html_parts.append(
                    f'<div class="formula" data-equation="{number or ""}">'
                    f'<span class="formula-math">{mathml}</span>{number_html}'
                    f'<details><summary>LaTeX</summary><code>{html.escape(tex)}</code></details>'
                    "</div>"
                )
            else:
                html_parts.append(
                    f'<div class="formula formula-tex-fallback"><code>'
                    f"{html.escape(tex)}</code>{number_html}</div>"
                )
            html_parts.append(
                f"<!-- source-formula-anchor:{source_formula_index} -->"
            )
            markdown_tex = tex + (rf"\tag{{{number}}}" if number is not None else "")
            md_parts.extend(
                [
                    "$$",
                    markdown_tex,
                    "$$",
                    f"<!-- source-formula-anchor:{source_formula_index} -->",
                    "",
                ]
            )
            counts["formulas"] += 1
        elif item.kind == "table":
            grid, header_rows, placements = _table_cell_layout(source, item)
            caption = _caption_text(document, node)
            if not re.match(r"(?i)^Table\s+\d+\s*:", caption):
                caption = _source_caption(source, item, kind="table") or caption
            source_ref = _structure_block_source_ref(item)
            escaped_source_ref = html.escape(source_ref, quote=True)
            html_parts.append('<figure class="semantic-table">')
            if caption:
                html_parts.append(
                    "<figcaption>"
                    + _inline_replacements(
                        caption,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=False,
                    )
                    + "</figcaption>"
                )
            html_parts.append(
                '<div class="table-scroll">'
                f'<table data-source-ref="{escaped_source_ref}">'
            )
            table_callouts = table_footnote_callouts.get(id(item), [])
            table_cell_callouts: dict[
                tuple[int, int], list[tuple[str, str]]
            ] = {}
            for marker, note_id in table_callouts:
                variants = (
                    ("*", "∗")
                    if marker in {"*", "∗"}
                    else (marker,)
                )
                assigned = False
                for row_index, row in enumerate(grid):
                    for column, value in enumerate(row):
                        matched = next(
                            (variant for variant in variants if variant in value),
                            None,
                        )
                        if matched is None:
                            continue
                        table_cell_callouts.setdefault(
                            (row_index, column),
                            [],
                        ).append((matched, note_id))
                        assigned = True
                        break
                    if assigned:
                        break
            placements_by_row: dict[int, list[dict[str, Any]]] = {}
            occupied_cells: set[tuple[int, int]] = set()
            for placement in placements:
                placement_row = int(placement["row"])
                placement_col = int(placement["col"])
                placements_by_row.setdefault(placement_row, []).append(placement)
                for covered_row in range(
                    placement_row,
                    placement_row + int(placement.get("rowspan") or 1),
                ):
                    for covered_col in range(
                        placement_col,
                        placement_col + int(placement.get("colspan") or 1),
                    ):
                        occupied_cells.add((covered_row, covered_col))
            for row_index, row in enumerate(grid):
                row_placements = sorted(
                    placements_by_row.get(row_index, []),
                    key=lambda value: int(value["col"]),
                )
                if not row_placements and row and all(
                    (row_index, column) in occupied_cells
                    for column in range(len(row))
                ):
                    # The row is fully covered by a rowspan declared on a
                    # previous row; emitting placeholder cells would defeat
                    # the merge semantics.
                    continue
                placements_at_column = {
                    int(placement["col"]): placement
                    for placement in row_placements
                }
                html_parts.append("<tr>")
                column = 0
                while column < len(row):
                    placement = placements_at_column.get(column)
                    if placement is None:
                        if (row_index, column) in occupied_cells:
                            column += 1
                            continue
                        # Sparse merged-header payloads omit explicit blank
                        # cells.  Emit the uncovered grid slot so a leading or
                        # interior blank cannot shift every following header
                        # left in the HTML machine surface.
                        placement = {
                            "row": row_index,
                            "col": column,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": row[column],
                            "header_role": "",
                        }
                    value = str(placement.get("text") or "")
                    header_role = str(placement.get("header_role") or "")
                    if header_role == "col":
                        tag = "th"
                        scope_attribute = ' scope="col"'
                    elif header_role == "row":
                        tag = "th"
                        scope_attribute = ' scope="row"'
                    elif header_role == "rowgroup":
                        tag = "th"
                        scope_attribute = ' scope="rowgroup"'
                    elif (
                        not item.node.get("_semantic_table_has_header_roles")
                        and row_index < header_rows
                    ):
                        # Legacy payloads omit semantic flags.  Preserve the
                        # historical first-row header surface in that case.
                        tag = "th"
                        scope_attribute = ""
                    else:
                        tag = "td"
                        scope_attribute = ""
                    span_attributes = scope_attribute
                    if int(placement.get("rowspan") or 1) > 1:
                        span_attributes += f' rowspan="{int(placement["rowspan"])}"'
                    if int(placement.get("colspan") or 1) > 1:
                        span_attributes += f' colspan="{int(placement["colspan"])}"'
                    html_parts.append(
                        f"<{tag}{span_attributes}>"
                        + "<br>".join(
                            _inline_replacements(
                                part.strip(),
                                reference_texts,
                                table_cell_callouts.get(
                                    (row_index, column),
                                    [],
                                ),
                                markdown=False,
                            )
                            for part in value.splitlines()
                        )
                        + f"</{tag}>"
                    )
                    column += max(1, int(placement.get("colspan") or 1))
                html_parts.append("</tr>")
            html_parts.append("</table></div></figure>")
            md_parts.append(f"<!-- source-ref:{source_ref} -->")
            md_parts.append(f"<!-- source-table-ref:{source_ref} -->")
            if item.node.get("_semantic_table_markdown_degraded"):
                degradation_reasons: list[str] = []
                if item.node.get("_semantic_table_has_merged_cells"):
                    degradation_reasons.append(
                        "merged table cells require the exact source-table image"
                    )
                if item.node.get("_semantic_table_has_header_roles"):
                    degradation_reasons.append(
                        "HTML header scope is not representable in Markdown"
                    )
                if not degradation_reasons:
                    degradation_reasons.append(
                        "the exact source-table image is required for faithful reading"
                    )
                md_parts.append(
                    "<!-- machine-surface: degraded; "
                    + "; ".join(degradation_reasons)
                    + " -->"
                )
            if caption:
                md_parts.extend([f"**{caption}**", ""])
            md_parts.extend(
                [
                    _markdown_table(
                        grid,
                        reference_texts=reference_texts,
                        footnote_callouts=table_cell_callouts,
                    ),
                    "",
                ]
            )
            counts["tables"] += 1
        elif item.kind == "picture":
            picture_index = picture_counter.get(id(node))
            image_path = str(node.get("_semantic_picture_path") or "")
            if not image_path and picture_index is not None:
                image_path = f"pictures/picture_{picture_index}.png"
            caption = _caption_text(document, node)
            if not re.match(r"(?i)^Figure\s+\d+\s*:", caption):
                caption = _source_caption(source, item, kind="picture") or caption
            if image_path:
                html_parts.append('<figure class="picture">')
                html_parts.append(
                    f'<img src="{image_path}" alt="{html.escape(caption or "Figure", quote=True)}">'
                )
                if caption:
                    html_parts.append(
                        "<figcaption>"
                        + _inline_replacements(
                            caption,
                            reference_texts,
                            footnote_callouts.get(id(item), []),
                            markdown=False,
                        )
                        + "</figcaption>"
                    )
                html_parts.append("</figure>")
                markdown_caption = (
                    _inline_replacements(
                        caption,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=True,
                    )
                    if caption
                    else "Figure"
                )
                md_parts.append(
                    f"![{caption or 'Figure'}]({image_path})"
                )
                if caption:
                    md_parts.append(f"*{markdown_caption}*")
                md_parts.append("")
                counts["pictures"] += 1

    style = """
body{max-width:980px;margin:0 auto;padding:2rem 2.5rem;color:#172033;
font:17px/1.58 Georgia,"STIX Two Text","Times New Roman","Noto Serif",serif;
background:#fff}
h1,h2,h3,h4,h5,h6{font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.25;
margin:1.55em 0 .65em}p{margin:.65em 0;text-align:justify}
.formula{position:relative;display:flex;align-items:center;justify-content:center;
gap:1rem;margin:1.25rem 0;padding:.75rem 4.5rem .5rem 1rem;overflow-x:auto}
.formula math{font-size:1.14em;font-family:"STIX Two Math","Cambria Math",
"Noto Sans Math",math}.equation-number{position:absolute;right:1rem}
.formula details{font:12px/1.4 ui-monospace,monospace;color:#596273}
.algorithm,.code-listing{margin:1.3rem 0;border:1px solid #aeb7c4;background:#fafafa}
.algorithm-title,.code-listing-title{padding:.45rem .7rem;border-bottom:1px solid #aeb7c4;
font-weight:700}.algorithm pre,.code-listing pre{margin:0;padding:.8rem 1rem;overflow:auto;
font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,"STIX Two Math",
"Noto Sans Math",monospace;white-space:pre}
.alg-keyword,.code-keyword{font-weight:700;color:#7c2d12}.alg-comment,.code-comment{
font-style:italic;color:#477052}.code-string{color:#9a3412}.code-number{color:#1d4ed8}
.line-number{color:#64748b;font-weight:600}
.semantic-table{margin:1.4rem 0}.semantic-table figcaption{text-align:center;
font-weight:700;margin-bottom:.5rem}.table-scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9em}th,td{border:1px solid #8f99a8;
padding:.35rem .48rem;vertical-align:top}th{background:#eef2f6}
.picture{text-align:center;margin:1.4rem auto}.picture img{max-width:100%;height:auto}
.picture figcaption{margin-top:.45rem}.citation{text-decoration:none;color:#2457a6}
.citation:hover{text-decoration:underline}.reference-entry{padding-left:2.9rem;
text-indent:-2.9rem;margin:.5rem 0}.reference-number{font-variant-numeric:tabular-nums}
.footnote{font-size:.86em;color:#3f4857;border-top:1px solid #c9cfd8;
padding-top:.4rem}.footnote-label{font-weight:700}.footnote-backref{text-decoration:none}
@media(max-width:700px){body{padding:1rem}.formula{padding-right:3.5rem;font-size:.9em}}
"""
    html_document = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{style}</style></head><body>"
        + "\n".join(html_parts)
        + "</body></html>\n"
    )
    return html_document, "\n".join(md_parts).rstrip() + "\n", counts


def rebuild_semantic_surfaces(
    output_dir: Path,
    document: dict[str, Any],
    input_file: Path,
    metadata: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(status.get("quality_signals"), dict):
        status["quality_signals"] = {}
    if not isinstance(status.get("warnings"), list):
        status["warnings"] = []
    inline_math_source_regions: list[dict[str, Any]] = []
    source_readability_diagnostics: list[dict[str, Any]] = []
    try:
        source = SourceReader(input_file)
        dropped_formula_artifacts: list[dict[str, Any]] = []
    except Exception as exc:
        result = {
            "ok": False,
            "applied": False,
            "reason": f"semantic_source_reader_unavailable:{type(exc).__name__}:{exc}",
            "dropped_formula_artifacts": [],
            "inline_math_source_regions": inline_math_source_regions,
            "inline_math_source_region_count": 0,
        }
        metadata["primary_surface"] = result
        status["quality_signals"]["primary_surface"] = result
        status["ok"] = False
        status["success_class"] = "degraded_failure"
        status["warnings"].append(result["reason"])
        return result
    source_profile = source.language_profile()
    normalized_document, normalized_parts = _document_parts_with_global_pages(
        document
    )
    primary_counts = _primary_surface_count_from_document(normalized_document)
    cjk_semantic_fallback: dict[str, Any] | None = None

    def preserve_cjk_surfaces(
        *,
        reason: str,
        evidence_cleanup: dict[str, Any],
        cjk_inline_source: dict[str, Any],
    ) -> dict[str, Any]:
        """Record the legacy CJK surface only when semantic reflow is unavailable."""

        cjk_regions = list(cjk_inline_source.get("regions") or [])
        cjk_missing = list(cjk_inline_source.get("missing") or [])
        cjk_binding_diagnostics = list(
            cjk_inline_source.get("binding_diagnostics") or []
        )
        metadata["cjk_inline_math_source_binding_diagnostics"] = (
            cjk_binding_diagnostics
        )
        if cjk_binding_diagnostics:
            status["warnings"].append(
                "cjk_inline_math_source_appendix_bindings:"
                f"{len(cjk_binding_diagnostics)}"
            )
        if cjk_missing:
            missing_reasons = sorted(
                {
                    str(item.get("reason"))
                    for item in cjk_missing
                    if isinstance(item, dict)
                }
            )
            status["ok"] = False
            status["success_class"] = "degraded_failure"
            status["warnings"].append(
                "cjk_inline_math_source_missing:"
                f"{len(cjk_missing)}"
                + (
                    f":{','.join(missing_reasons)}"
                    if missing_reasons
                    else ""
                )
            )
        result = {
            "ok": not cjk_missing,
            "applied": False,
            "machine_surface_ok": False,
            "mode": "preserve_existing_cjk_body_source_visual_authoritative",
            "reason": reason,
            "source_profile": source_profile,
            "dropped_formula_artifacts": [],
            "review_evidence_cleanup": evidence_cleanup,
            "authoritative_surfaces": ["document.html", "document.md"],
            "source_formula_visuals_authoritative": True,
            "counts": primary_counts,
            "inline_math_source_regions": cjk_regions,
            "inline_math_source_region_count": len(cjk_regions),
            "inline_math_source_missing": cjk_missing,
            "inline_math_source_binding_diagnostics": cjk_binding_diagnostics,
            "inline_math_source_appendix_anchor_count": cjk_inline_source.get(
                "appendix_anchor_count", 0
            ),
        }
        metadata["primary_surface"] = result
        status["quality_signals"]["primary_surface"] = result
        if status.get("ok"):
            status["success_class"] = "degraded_success"
        status["warnings"].append(reason)
        return result

    cjk_characters = source_profile["cjk_characters"]
    language_characters = cjk_characters + source_profile["latin_characters"]
    if (
        cjk_characters >= 100
        and language_characters > 0
        and cjk_characters / language_characters >= 0.2
    ):
        evidence_cleanup = _remove_review_evidence_from_primary_surfaces(output_dir)
        cjk_inline_source = _collect_cjk_inline_math_source_regions(
            output_dir,
            normalized_document,
            source,
        )
        source.close()
        try:
            formula_normalization = _normalize_legacy_formula_surfaces(output_dir)
        except Exception as exc:
            fallback_reason = (
                "cjk_machine_formula_normalization_unavailable:"
                f"{type(exc).__name__}:{exc}"
            )
            cjk_semantic_fallback = {
                "reason": fallback_reason,
                "review_evidence_cleanup": evidence_cleanup,
                "inline_source": cjk_inline_source,
            }
            # A legacy CJK HTML surface may omit display formulas or contain
            # number-only placeholders.  Prefer the same source-backed
            # semantic reconstruction used for every other document; it
            # produces stable table/formula refs and explicit dropped-formula
            # records.  Preserve the legacy surface only when that stronger
            # reconstruction itself is unavailable.
            inline_math_source_regions = []
            source_readability_diagnostics = []
            dropped_formula_artifacts = []
            metadata.pop("cjk_inline_math_source_binding_diagnostics", None)
            try:
                source = SourceReader(input_file)
            except Exception as source_exc:
                return preserve_cjk_surfaces(
                    reason=(
                        fallback_reason
                        + ":semantic_fallback_source_unavailable:"
                        + f"{type(source_exc).__name__}:{source_exc}"
                    ),
                    evidence_cleanup=evidence_cleanup,
                    cjk_inline_source=cjk_inline_source,
                )
        else:
            inline_math_source_regions = list(cjk_inline_source.get("regions") or [])
            inline_math_source_missing = list(cjk_inline_source.get("missing") or [])
            cjk_binding_diagnostics = list(
                cjk_inline_source.get("binding_diagnostics") or []
            )
            metadata["cjk_inline_math_source_binding_diagnostics"] = (
                cjk_binding_diagnostics
            )
            if cjk_binding_diagnostics:
                status["warnings"].append(
                    "cjk_inline_math_source_appendix_bindings:"
                    f"{len(cjk_binding_diagnostics)}"
                )
            if inline_math_source_missing:
                missing_reasons = sorted(
                    {
                        str(item.get("reason"))
                        for item in inline_math_source_missing
                        if isinstance(item, dict)
                    }
                )
                status["ok"] = False
                status["success_class"] = "degraded_failure"
                status["warnings"].append(
                    "cjk_inline_math_source_missing:"
                    f"{len(inline_math_source_missing)}"
                    + (
                        f":{','.join(missing_reasons)}"
                        if missing_reasons
                        else ""
                    )
                )
            normalized = bool(formula_normalization["applied"])
            result = {
                "ok": not inline_math_source_missing,
                "applied": normalized,
                "machine_surface_ok": not bool(cjk_binding_diagnostics),
                "mode": (
                    "preserve_existing_cjk_body_with_semantic_formulas"
                    if normalized
                    else "preserve_existing_cjk_semantic_surface"
                ),
                "reason": "cjk_formula_geometry_requires_ocr_owned_surface",
                "source_profile": source_profile,
                "dropped_formula_artifacts": [],
                "review_evidence_cleanup": evidence_cleanup,
                "formula_normalization": formula_normalization,
                "authoritative_surfaces": ["document.html", "document.md"],
                "source_formula_visuals_authoritative": True,
                "counts": primary_counts,
                "inline_math_source_regions": inline_math_source_regions,
                "inline_math_source_region_count": len(inline_math_source_regions),
                "inline_math_source_missing": inline_math_source_missing,
                "inline_math_source_binding_diagnostics": cjk_binding_diagnostics,
                "inline_math_source_appendix_anchor_count": cjk_inline_source.get(
                    "appendix_anchor_count", 0
                ),
            }
            metadata["primary_surface"] = result
            status["quality_signals"]["primary_surface"] = result
            if cjk_binding_diagnostics and status.get("ok"):
                status["success_class"] = "degraded_success"
            return result
    try:
        documents = [part for _part_index, part in normalized_parts]
        picture_assets = _materialize_picture_assets(output_dir, documents)
        prepared_parts: list[
            tuple[dict[str, Any], list[FlowItem], int, int]
        ] = []
        shared_reference_texts: list[tuple[int, str]] = []
        reference_offset = 0
        formula_offset = 0
        for normalized_part_index, part in normalized_parts:
            part_index = (
                normalized_part_index
                if normalized_part_index is not None
                else 0
            )
            items = _sort_items(
                _collect_items(
                    part,
                    source,
                    dropped_formula_artifacts=dropped_formula_artifacts,
                    formula_offset=formula_offset,
                    inline_math_anchor_part=part_index,
                ),
                part,
            )
            _reference_map, local_reference_texts = _reference_items(items)
            for item in items:
                if item.source_readability_diagnostic:
                    source_readability_diagnostics.append(
                        {
                            "page_no": item.page_no,
                            "collection_index": item.collection_index,
                            "rank": item.rank,
                            **item.source_readability_diagnostic,
                        }
                    )
                inline_math_source_regions.extend(
                    _inline_math_source_region_records(
                        item,
                        part_index=part_index,
                    )
                )
            prepared_parts.append((part, items, reference_offset, formula_offset))
            shared_reference_texts.extend(
                (number + reference_offset, text)
                for number, text in local_reference_texts
            )
            reference_offset += len(local_reference_texts)
            formula_offset += len(_label_node_ordinals(part, "formula"))
        rendered_parts: list[tuple[str, str, dict[str, int], int]] = []
        for part, items, part_reference_offset, part_formula_offset in prepared_parts:
            part_html, part_md, part_counts = _render(
                items,
                part,
                source,
                shared_reference_texts=shared_reference_texts,
                reference_number_offset=part_reference_offset,
                formula_index_offset=part_formula_offset,
            )
            rendered_parts.append((part_html, part_md, part_counts, len(items)))
        semantic_formula_tex_by_index: dict[int, str] = {}
        for _part, items, _part_reference_offset, part_formula_offset in prepared_parts:
            for item in items:
                if item.kind != "formula" or item.collection_index is None:
                    continue
                tex = str(
                    item.node.get("_local_ai_lab_semantic_formula_tex") or ""
                ).strip()
                if tex:
                    semantic_formula_tex_by_index[
                        part_formula_offset + item.collection_index + 1
                    ] = tex
        total_items = sum(value[3] for value in rendered_parts)
        if total_items == 0:
            raise RuntimeError("semantic source reconstruction produced no flow items")
        first_html = rendered_parts[0][0]
        head, _separator, _body = first_html.partition("<body>")
        bodies = []
        for part_html, _part_md, _counts, _count in rendered_parts:
            _prefix, separator, body = part_html.partition("<body>")
            if separator:
                body = body.rsplit("</body>", 1)[0]
            bodies.append(body)
        document_html = head + "<body>" + "\n".join(bodies) + "</body></html>\n"
        document_md = "\n".join(value[1].rstrip() for value in rendered_parts) + "\n"
        counts = {
            key: sum(value[2].get(key, 0) for value in rendered_parts)
            for key in rendered_parts[0][2]
        }
        items_count = total_items
    except Exception as exc:
        if cjk_semantic_fallback is not None:
            return preserve_cjk_surfaces(
                reason=(
                    str(cjk_semantic_fallback["reason"])
                    + ":semantic_fallback_failed:"
                    + f"{type(exc).__name__}:{exc}"
                ),
                evidence_cleanup=dict(
                    cjk_semantic_fallback["review_evidence_cleanup"]
                ),
                cjk_inline_source=dict(cjk_semantic_fallback["inline_source"]),
            )
        result = {
            "ok": False,
            "applied": False,
            "reason": f"semantic_reflow_failed:{type(exc).__name__}:{exc}",
            "dropped_formula_artifacts": dropped_formula_artifacts,
            "inline_math_source_regions": inline_math_source_regions,
            "inline_math_source_region_count": len(inline_math_source_regions),
            "source_readability_diagnostics": source_readability_diagnostics,
        }
        metadata["primary_surface"] = result
        status["quality_signals"]["primary_surface"] = result
        status["ok"] = False
        status["success_class"] = "degraded_failure"
        status["warnings"].append(result["reason"])
        return result
    finally:
        source.close()
    (output_dir / "document.html").write_text(document_html, encoding="utf-8")
    (output_dir / "document.md").write_text(document_md, encoding="utf-8")
    result = {
        "ok": True,
        "applied": True,
        "mode": (
            "cjk_semantic_source_reflow_fallback"
            if cjk_semantic_fallback is not None
            else "semantic_source_reflow"
        ),
        "flow_item_count": items_count,
        "counts": counts,
        "semantic_formula_tex_by_index": semantic_formula_tex_by_index,
        "dropped_formula_artifacts": dropped_formula_artifacts,
        "inline_math_source_regions": inline_math_source_regions,
        "inline_math_source_region_count": len(inline_math_source_regions),
        "source_readability_diagnostics": source_readability_diagnostics,
        "authoritative_surfaces": ["document.html", "document.md"],
        "source_visuals_authoritative": True,
        "picture_assets": picture_assets,
    }
    if cjk_semantic_fallback is not None:
        fallback_reason = str(cjk_semantic_fallback["reason"])
        result.update(
            {
                "machine_surface_ok": True,
                "source_profile": source_profile,
                "legacy_cjk_formula_normalization_failure": fallback_reason,
                "review_evidence_cleanup": dict(
                    cjk_semantic_fallback["review_evidence_cleanup"]
                ),
            }
        )
        status["warnings"].append(
            "cjk_semantic_source_reflow_fallback:" + fallback_reason
        )
        if status.get("ok"):
            status["success_class"] = "degraded_success"
    metadata["primary_surface"] = result
    status["quality_signals"]["primary_surface"] = result
    return result
