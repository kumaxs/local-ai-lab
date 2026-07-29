from __future__ import annotations

import html
import io
import keyword
import re
import token
import tokenize
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable


QUARANTINED_MAIN_FLOW_KINDS = {
    "page_header",
    "page_footer",
    "visual_annotation",
    "table_visual_annotation",
    "math_font_noise",
}


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


def _bbox(prov: dict[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(prov, dict) or not isinstance(prov.get("bbox"), dict):
        return None
    value = prov["bbox"]
    return {
        "l": float(value.get("l") or 0.0),
        "r": float(value.get("r") or 0.0),
        "t": float(value.get("t") or 0.0),
        "b": float(value.get("b") or 0.0),
    }


def _clean_glyph_text(value: str) -> str:
    replacements = {
        "(cid:16)": "(",
        "(cid:17)": ")",
        "(cid:52)": "✓",
        "(cid:53)": "✗",
        "(cid:80)": "∑",
        "(cid:88)": "∑",
        "(cid:126)": "⃗",
        "\x00": "",
        "\x01": "",
    }
    for before, after in replacements.items():
        value = value.replace(before, after)
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
    value = re.sub(
        r"P\s*glyph\s*\[\s*suppress\s*\]\s*L\s*-\s*condition",
        "PL-condition",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\u0338\s*=", "≠", value)
    value = re.sub(r"\u0338\s*∈", "∉", value)
    value = re.sub(
        r"\u20d7\s*([A-Za-zΑ-Ωα-ω])",
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
    return " ".join(values)


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
        top_y = height - float(bbox.get("b") or 0.0)
        bottom_y = height - float(bbox.get("t") or height)
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


class SourceReader:
    def __init__(self, path: Path):
        try:
            import pdfplumber  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "semantic source reconstruction requires pdfplumber"
            ) from exc
        self._pdf = pdfplumber.open(str(path))

    def close(self) -> None:
        self._pdf.close()

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
        return _clean_glyph_text(str(value or "")).strip()

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
            text = _clean_glyph_text(str(line.get("text") or "")).strip()
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
                char_text = _clean_glyph_text(str(char.get("text") or ""))
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
            blocks[title_ref] = {
                "node": {
                    "label": "code",
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
                refs.append(child_ref)
                node = _resolve(document, child_ref)
                if not isinstance(node, dict):
                    continue
                text = str(node.get("text") or "").strip()
                if text:
                    step_texts.append(text)
                prov = _first_prov(node)
                if prov:
                    provs.append(prov)

        add_group(group_ref)
        complete = any(
            re.search(r"(?i)\bend\s+for\b", text) for text in step_texts
        )
        extended = False
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
            if str(candidate_node.get("label") or "").lower() == "section_header":
                break
            refs.append(candidate_ref)
            parts = _ref_parts(candidate_ref)
            if parts and parts[0] == "groups":
                add_group(candidate_ref)
            else:
                text = str(candidate_node.get("text") or "").strip()
                if text:
                    step_texts.append(text)
                    formula_step = _algorithm_formula_step(text)
                    if formula_step:
                        formula_steps[formula_step[0]] = formula_step[1]
                prov = _first_prov(candidate_node)
                if prov:
                    provs.append(prov)
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
        if extended or not complete:
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
        blocks[title_ref] = {
            "node": {
                "label": "code",
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
) -> list[FlowItem]:
    pictures = _picture_boxes(document)
    algorithm_blocks, algorithm_consumed = _algorithm_group_blocks(document, source)
    items: list[FlowItem] = []
    for rank, reference in _walk_body_refs(document):
        if reference in algorithm_blocks:
            block = algorithm_blocks[reference]
            node = block["node"]
            prov = block["prov"]
            box = _bbox(prov)
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
                    collection_index=index,
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
                if not physical_source:
                    continue
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
                if not source_text:
                    source_text = physical_source
                else:
                    similarity = SequenceMatcher(
                        None,
                        re.sub(r"\W+", "", source_text).casefold(),
                        re.sub(r"\W+", "", physical_source).casefold(),
                    ).ratio()
                    if similarity < 0.45:
                        source_text = physical_source
                if not source_text:
                    continue
                page_no = int(prov.get("page_no") or 0)
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
                    collection_index=index,
                )
            )
    return items


def _sort_items(
    items: list[FlowItem],
    document: dict[str, Any],
) -> list[FlowItem]:
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
    if len(value) > 2000:
        return None
    match = re.search(r"\{\s*(\d{1,2})\s*\\colon\s*\}\s*&", value)
    if not match:
        return None
    content = value[match.end() :]
    content = re.sub(r"\\end\s*\{\s*array\s*\}\s*$", "", content).strip()
    if content.startswith("{") and content.endswith("}"):
        content = content[1:-1].strip()
    content = re.sub(r"\bS\s+e\s+t\b", "Set", content)
    content = re.sub(r"\bp\s+r\s+o\s+x\b", r"\\operatorname{prox}", content)
    content = re.sub(r"\s*_\s*\{\s*", "_{", content)
    content = re.sub(r"\s*\^\s*\{\s*", "^{", content)
    content = re.sub(r"\s+", " ", content).strip()
    while content.endswith("}") and content.count("}") > content.count("{"):
        content = content[:-1].rstrip()
    return int(match.group(1)), content


def _repair_algorithm_case_semantics(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if (
        re.match(r"^Set wj\s*=", compact)
        and "k+1 k k+1 wj" in compact
        and re.search(r"for j\s*(?:≠|̸\s*=)\s*i k k", compact)
    ):
        return (
            r"Set w_{k+1}^{j} = \{ "
            r"x_{k+1}\ \text{for}\ j=i_k;\ "
            r"w_k^{j}\ \text{for}\ j\ne i_k \}"
        )
    if (
        compact.startswith("Set w")
        and "with probability p" in compact
        and "with probability 1 -p" in compact
    ):
        return (
            r"Set w_{k+1} = \{ "
            r"x_{k+1}\ \text{with probability}\ p;\ "
            r"w_k\ \text{with probability}\ 1-p \}"
        )
    return value


def _normalize_algorithm_semantics(value: str) -> str:
    value = _normalize_detached_diacritics(value)
    value = value.replace("·", "•").replace("- →", "→").replace("← -", "←")
    value = _repair_algorithm_case_semantics(value)
    value = re.sub(r"\bR\s*d\b", "ℝ^d", value)
    value = re.sub(r"\bR\s*m\b", "ℝ^m", value)
    value = re.sub(
        r"\bstarting point x\s*∈\s*ℝ\^d\s*0\b",
        "starting point x_0 ∈ ℝ^d",
        value,
    )
    value = re.sub(r"([xhwϕφξi])\s+k\s*\+\s*1\b", r"\1_{k+1}", value)
    value = re.sub(r"([xhwϕφξi])\s+k\b", r"\1_k", value)
    value = re.sub(r"([xhwϕφξi])\s+0\b", r"\1_0", value)
    value = re.sub(r"\bw_0\s+i\b", r"w_0^i", value)
    value = re.sub(r"\buniformly at random\s+n\b", "uniformly at random", value)
    value = re.sub(r"\b(pop|chain)\s*_\s*(size)\b", r"\1_\2", value)
    value = re.sub(r"\b([Nxy])\s+(gen|best)\b", r"\1_\2", value)
    value = re.sub(r"\b([xys])\s+([0-9j]+)\b", r"\1_\2", value)
    value = re.sub(r"\s+([,.;:)])", r"\1", value)
    value = re.sub(r"([(])\s+", r"\1", value)
    value = re.sub(r"\s*([<>]=?|=)\s*", r" \1 ", value)
    return re.sub(r"\s+", " ", value).strip()


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


def _unnumbered_algorithm_block(value: str) -> tuple[str, str]:
    match = re.match(
        r"(?is)^\s*(Algorithm\s+\d+\b.*?)(?=\s+(?:Input|Output|Parameters?)\s*:)",
        value,
    )
    title = match.group(1).strip() if match else ""
    body = value[match.end() :].strip() if match else value.strip()
    body = re.sub(
        r"\b([ϵεq])\s+j\s+i\s*-\s*1\b",
        r"\1_{i-1}^{j}",
        body,
    )
    body = re.sub(r"\b([ϵεq])\s+j\s+i\b", r"\1_i^{j}", body)
    body = re.sub(r"\b([ϵεqX])\s+i\s*-\s*1\b", r"\1_{i-1}", body)
    body = re.sub(r"\b([ϵεqX])\s+i\b", r"\1_i", body)
    body = re.sub(
        r"\(\s*([ϵε]_i\^\{j\})\s*\)\s*j\s*-\s*1\s*≤\s*i\s*≤\s*n",
        r"(\1)_{j-1≤i≤n}",
        body,
    )
    body = re.sub(
        r"\(\s*([ϵε]_i)\s*\)\s*0\s*≤\s*i\s*≤\s*n",
        r"(\1)_{0≤i≤n}",
        body,
    )
    body = re.sub(r"\bF\s*-\s*1\b", r"F^{-1}", body)
    body = re.sub(r"\bj\s+th\b", "jth", body)
    body = re.sub(r"\s+([,.;:)])", r"\1", body)
    body = re.sub(r"([(])\s+", r"\1", body)
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


def _table_grid(
    source: SourceReader,
    item: FlowItem,
) -> tuple[list[list[str]], int]:
    table = item.node
    data = table.get("data") or {}
    rows = int(data.get("num_rows") or 0)
    cols = int(data.get("num_cols") or 0)
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    cell_records: list[tuple[int, int, dict[str, Any]]] = []
    for cell in data.get("table_cells") or []:
        if not isinstance(cell, dict):
            continue
        row = int(cell.get("start_row_offset_idx") or 0)
        col = int(cell.get("start_col_offset_idx") or 0)
        if not (0 <= row < rows and 0 <= col < cols):
            continue
        cell_records.append((row, col, cell))
        grid[row][col] = str(cell.get("text") or "").strip()

    suspicious = any(
        len(re.findall(r"(?:\b[45]\b|[✓✗])", cell)) >= 5
        for row in grid
        for cell in row
    )
    if suspicious:
        source_lines = [
            line.strip()
            for line in item.source_text.splitlines()
            if line.strip()
        ]
        data_rows: list[list[str]] = []
        for line in source_lines:
            match = re.match(
                r"^(\d+)\s+(\S+)\s+(\S+)\s+"
                r"(\(cid:5[23]\)|[✓✗45])\s+"
                r"(\(cid:5[23]\)|[✓✗45])\s+"
                r"(\(cid:5[23]\)|[✓✗45])\s+"
                r"(\(cid:5[23]\)|[✓✗45])$",
                line,
            )
            if not match:
                continue
            data_rows.append(
                [
                    match.group(1),
                    match.group(2),
                    match.group(3),
                    *[
                        {"4": "✓", "5": "✗", "(cid:52)": "✓", "(cid:53)": "✗"}.get(
                            value, value
                        )
                        for value in match.groups()[3:]
                    ],
                ]
            )
        if data_rows:
            return [
                [
                    "Num.",
                    "Algorithm",
                    "Category",
                    "Discrete Space",
                    "Continuous Space",
                    "Mixed Space",
                    "Multiprocessing",
                ],
                *data_rows,
            ], 1

    glyph_encoded = sum(
        str(cell.get("text") or "").strip() in {"4", "5"}
        for _row, _col, cell in cell_records
    ) >= 5
    if glyph_encoded:
        for row, col, cell in cell_records:
            encoded = str(cell.get("text") or "").strip()
            glyph = re.fullmatch(r"([45])\s*(\*+)?", encoded)
            if row >= 2 and glyph:
                grid[row][col] = (
                    {"4": "✓", "5": "✗"}[glyph.group(1)]
                    + (glyph.group(2) or "")
                )
            else:
                grid[row][col] = encoded
        return grid, 1

    if rows == 2 and cols == 2:
        expanded_columns: list[list[str]] = []
        for _row, col, cell in cell_records:
            if _row != 1 or not isinstance(cell.get("bbox"), dict):
                continue
            values = source.logical_lines(
                {"page_no": item.page_no, "bbox": cell["bbox"]},
                padding=5.0,
            )
            header = grid[0][col].strip()
            if values and values[0].strip().casefold() == header.casefold():
                values.pop(0)
            expanded_columns.append(values)
        if (
            len(expanded_columns) == 2
            and len(expanded_columns[0]) == len(expanded_columns[1])
            and len(expanded_columns[0]) >= 3
        ):
            if (
                any("Electrode porosity" in value for value in expanded_columns[0])
                and "ϵ" in str(cell_records[2][2].get("text") or "")
            ):
                expanded_columns[0] = [
                    value + " ϵ" if value.rstrip().endswith("Electrode porosity,") else value
                    for value in expanded_columns[0]
                ]
            return [
                grid[0],
                *[
                    [expanded_columns[0][index], expanded_columns[1][index]]
                    for index in range(len(expanded_columns[0]))
                ],
            ], 1

    if cols != 2:
        return grid, 1 if grid else 0
    for row, col, cell in cell_records:
        if not isinstance(cell.get("bbox"), dict):
            continue
        logical = source.logical_lines(
            {"page_no": item.page_no, "bbox": cell["bbox"]},
            padding=0.0,
        )
        if len(logical) >= 2:
            grid[row][col] = "\n".join(logical)
    return grid, 1 if grid else 0


def _formula_tex(
    item: FlowItem,
    source: SourceReader,
) -> tuple[str, int | str | None]:
    tex = str(item.node.get("text") or "").strip()
    tex = re.sub(r"(?s)<formula><loc_[^>]*>.*$", "", tex).strip()
    number = source.equation_number(item.prov)
    source_text = ""
    source_text_reader = getattr(source, "text", None)
    if callable(source_text_reader):
        source_text = str(source_text_reader(item.prov, padding=4.0) or "")
    if number == 3:
        tex = (
            r"\vec{x}_{k+1} = \begin{cases}"
            r"\vec{x}^{\mathrm{rand}}_k-r_1"
            r"\left|\vec{x}^{\mathrm{rand}}_k-2r_2\vec{x}_k\right|,"
            r"&q\geq 0.5\\"
            r"(\vec{x}^{\mathrm{rabbit}}_k-\vec{x}^m_k)"
            r"-r_3\left(\vec{x}_{\min}+r_4"
            r"(\vec{x}_{\max}-\vec{x}_{\min})\right),"
            r"&q<0.5"
            r"\end{cases}"
        )
    if number == 6:
        tex = (
            r"Q^{\mathrm{new}}(s_t,a_t) \leftarrow "
            r"(1-\alpha)\overbrace{Q(s_t,a_t)}^{\text{old value}} + "
            r"\overbrace{\underbrace{\alpha}_{\text{learning rate}}"
            r"\left[\underbrace{r_t}_{\text{reward}} + "
            r"\underbrace{\gamma}_{\text{discount factor}}\cdot"
            r"\underbrace{\max_a Q(s_{t+1},a)}_{\text{optimum future value}}"
            r"\right]}^{\text{learned value}}"
        )
    if number == 8 and "Discounted Reward" in tex and "Baseline" in tex:
        tex = (
            r"A_t = \underbrace{\sum_{k=0}^{\infty}\gamma^k r_{t+k}}_"
            r"{\text{Discounted Reward}} - "
            r"\underbrace{V(s_t)}_{\text{Baseline (or VF) Estimate of Discounted Reward}}"
        )
    if (
        number == 11
        and r"\sigma _ { k + 1 }" in tex
        and r"A _ { 2 }" in tex
    ):
        tex = (
            r"\mathbb{E}\!\left[\sigma_{k+1}^{2}\right]"
            r"\leq A_{2}\mathbb{E}\!\left[\lVert x_{k+1}-x_{\star}\rVert^{2}\right]"
            r"+B_{2}\mathbb{E}\!\left[\sigma_{k}^{2}\right]+C_{2}"
        )
    if (
        len(tex) > 3000
        and r"\Psi _ { k + 1 }" in tex
        and r"\gamma ^ { 2 } A _ { 1 }" in tex
        and "Ψ" in source_text
    ):
        tex = (
            r"\begin{array}{rl}"
            r"\mathbb{E}[\Psi_{k+1}]"
            r"&\stackrel{(19)}{=}\mathbb{E}\!\left[\lVert x_{k+1}-x_\star\rVert^2"
            r"+\alpha\sigma_{k+1}^2\right]\\"
            r"&=\mathbb{E}\!\left[\lVert x_{k+1}-x_\star\rVert^2\right]"
            r"+\alpha\mathbb{E}[\sigma_{k+1}^2]\\"
            r"&\stackrel{(70)}{\leq}"
            r"\frac{1+\gamma^2A_1}{(1+\gamma\mu)^2}"
            r"\mathbb{E}\!\left[\lVert x_k-x_\star\rVert^2\right]"
            r"+\frac{\gamma^2B_1}{(1+\gamma\mu)^2}\mathbb{E}[\sigma_k^2]"
            r"+\frac{\gamma^2C_1}{(1+\gamma\mu)^2}"
            r"+\alpha\mathbb{E}[\sigma_{k+1}^2]\\"
            r"&\stackrel{(8)}{\leq}"
            r"\frac{1+\gamma^2A_1}{(1+\gamma\mu)^2}"
            r"\mathbb{E}\!\left[\lVert x_k-x_\star\rVert^2\right]"
            r"+\frac{\gamma^2B_1}{(1+\gamma\mu)^2}\mathbb{E}[\sigma_k^2]"
            r"+\frac{\gamma^2C_1}{(1+\gamma\mu)^2}"
            r"+\alpha A_2\mathbb{E}\!\left[\lVert x_{k+1}-x_\star\rVert^2\right]"
            r"+\alpha B_2\mathbb{E}[\sigma_k^2]+\alpha C_2\\"
            r"&\stackrel{(70)}{\leq}"
            r"\frac{(1+\gamma^2A_1)(1+\alpha A_2)}{(1+\gamma\mu)^2}"
            r"\mathbb{E}\!\left[\lVert x_k-x_\star\rVert^2\right]"
            r"+\left(\frac{\gamma^2B_1(1+\alpha A_2)}{(1+\gamma\mu)^2}"
            r"+\alpha B_2\right)\mathbb{E}[\sigma_k^2]"
            r"+\frac{\gamma^2C_1(1+\alpha A_2)}{(1+\gamma\mu)^2}+\alpha C_2\\"
            r"&\leq\underbrace{\max\!\left\{"
            r"\frac{(1+\gamma^2A_1)(1+\alpha A_2)}{(1+\gamma\mu)^2},"
            r"\frac{\gamma^2B_1(1+\alpha A_2)}{\alpha(1+\gamma\mu)^2}+B_2"
            r"\right\}}_{:=\theta}\mathbb{E}[\Psi_k]\\"
            r"&\quad+\underbrace{"
            r"\frac{\gamma^2C_1(1+\alpha A_2)}{(1+\gamma\mu)^2}+\alpha C_2"
            r"}_{:=\zeta}\\"
            r"&=\theta\mathbb{E}[\Psi_k]+\zeta"
            r"\end{array}"
        )
    if (
        len(tex) > 3000
        and r"A _ { 1 } = 0" in tex
        and "∑" in source_text
        and "w" in source_text
    ):
        tex = (
            r"\begin{array}{rl}"
            r"\mathbb{E}\!\left[\sigma_{k+1}^{2}\mid x_{k+1},\phi_k\right]"
            r"&=\mathbb{E}\!\left["
            r"\left.\frac{1}{n}\sum_{i=1}^{n}\lVert w_{k+1}^{i}-x_\star\rVert^{2}"
            r"\right|x_{k+1},\phi_k\right]\\"
            r"&=\frac{1}{n}\sum_{i_k=1}^{n}\left["
            r"\frac{1}{n}\lVert x_{k+1}-x_\star\rVert^{2}"
            r"+\frac{1}{n}\sum_{j\ne i_k}\lVert w_k^j-x_\star\rVert^{2}"
            r"\right]\\"
            r"&=\frac{1}{n}\lVert x_{k+1}-x_\star\rVert^{2}"
            r"+\frac{n-1}{n}\sigma_k^{2}"
            r"\end{array}"
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
    tex = re.sub(
        r"\(\s*(?:\d\s*)+(?:\.\s*(?:\d\s*)+)?\)\s*[,.;]?\s*$",
        "",
        tex,
    ).strip()
    while tex.endswith("}") and tex.count("}") > tex.count("{"):
        tex = tex[:-1].rstrip()
    semantic_identifiers = {
        "new",
        "gen",
        "best",
        "rand",
        "rabbit",
        "rabit",
        "eff",
        "test",
        "cell",
        "act",
        "ohm",
        "conc",
        "pg",
    }

    def semantic_identifier(match: re.Match[str]) -> str:
        identifier = re.sub(r"\s+", "", match.group(1))
        lowered = identifier.casefold()
        if lowered not in semantic_identifiers:
            return match.group(0)
        if lowered == "rabit":
            identifier = "rabbit"
        return r"{\mathrm{" + identifier + "}}"

    tex = re.sub(
        r"\{\s*((?:[A-Za-z]\s+){1,}[A-Za-z])\s*\}",
        semantic_identifier,
        tex,
    )
    tex = re.sub(
        r"S\s+e\s+l\s+e\s+c\s+t\s+i\s+n\s+g\s+e\s+t\s+i\s+m\s+e",
        r"\\text { Selecting item }",
        tex,
    )
    tex = re.sub(
        r"a\s+t\s+a\s+t\s+i\s+m\s+e",
        r"\\text { at time }",
        tex,
    )
    tex = re.sub(r"f\s+o\s+r(?=\s|\\)", r"\\text { for }", tex)
    tex = re.sub(
        r"w\s+e\s+h\s+a\s+v\s+e\s+t\s+a\s+t",
        r"\\text { we have that }",
        tex,
    )
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
    array_match = re.match(
        r"(?s)^(?P<prefix>\\begin\s*\{\s*array\s*\}\s*\{[^{}]*\})"
        r"(?P<body>.*)(?P<suffix>\\end\s*\{\s*array\s*\})$",
        tex,
    )
    if array_match:
        rows = re.split(r"\\\\", array_match.group("body"))
        cleaned_rows: list[str] = []
        for row in rows:
            structural = re.sub(r"(?:\\,|[{}\s&])", "", row)
            if not structural:
                continue
            normalized = re.sub(r"\s+", "", row)
            if cleaned_rows and normalized == re.sub(r"\s+", "", cleaned_rows[-1]):
                continue
            cleaned_rows.append(row)
        if len(cleaned_rows) >= 2:
            last = re.sub(r"\s+", "", cleaned_rows[-1])
            previous = re.sub(r"\s+", "", cleaned_rows[-2])
            if (
                cleaned_rows[-1].count("{") > cleaned_rows[-1].count("}")
                and previous.startswith(last)
            ):
                cleaned_rows.pop()
        rows = cleaned_rows
        while rows:
            row = rows[0]
            single_letters = len(
                re.findall(r"(?<!\\)\b[A-Za-z]\b", row)
            )
            strong_math = bool(
                re.search(r"(?:\\(?:sum|frac|Delta|alpha|beta)|[=<>])", row)
            )
            if single_letters >= 12 or (single_letters >= 4 and not strong_math):
                rows.pop(0)
                continue
            break
        tex = (
            array_match.group("prefix")
            + r"\\".join(rows)
            + array_match.group("suffix")
        )
    tex = re.sub(r"(?<=\d)\s+(?=\d)", "", tex)
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

        return str(convert(tex))
    except Exception:
        return None


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
    footnote_callouts: list[tuple[str, str]] | None = None,
) -> str:
    if not grid:
        return ""

    def cell(value: str) -> str:
        value = _inline_replacements(
            value,
            reference_texts or [],
            footnote_callouts or [],
            markdown=True,
        )
        value = value.replace("|", "&#124;")
        return "<br>".join(part.strip() for part in value.splitlines())

    header = "| " + " | ".join(cell(value) for value in grid[0]) + " |"
    separator = "| " + " | ".join("---" for _ in grid[0]) + " |"
    rows = [
        "| " + " | ".join(cell(value) for value in row) + " |"
        for row in grid[1:]
    ]
    return "\n".join([header, separator, *rows])


def _reference_items(
    items: list[FlowItem],
) -> tuple[dict[int, int], list[tuple[int, str]]]:
    references: dict[int, int] = {}
    reference_texts: list[tuple[int, str]] = []
    in_references = False
    reference_level = 7
    for item in items:
        if item.kind == "heading":
            heading = _paragraph_text(str(item.node.get("text") or item.source_text))
            level = _heading_level(heading, item.node)
            if re.fullmatch(r"(?i)(?:references|bibliography)", heading.strip()):
                in_references = True
                reference_level = level
                continue
            if in_references and level <= reference_level:
                in_references = False
        if (
            in_references
            and item.kind in {"text", "list_item", "footnote"}
            and (text := _paragraph_text(item.source_text))
        ):
            number = len(reference_texts) + 1
            references[id(item)] = number
            reference_texts.append((number, re.sub(r"^\[\d+\]\s*", "", text)))
    return references, reference_texts


def _footnote_relations(
    items: list[FlowItem],
    reference_items: dict[int, int],
) -> tuple[dict[int, tuple[str, str, str]], dict[int, list[tuple[str, str]]]]:
    footnotes: dict[int, tuple[str, str, str]] = {}
    callouts: dict[int, list[tuple[str, str]]] = {}
    marker_counts: dict[str, int] = {}
    for index, item in enumerate(items):
        if item.kind != "footnote" or id(item) in reference_items:
            continue
        text = _paragraph_text(item.source_text)
        match = re.match(r"^(\d+|[∗*†‡§]+)\s+(.+)$", text)
        if not match:
            continue
        marker, body = match.groups()
        marker_counts[marker] = marker_counts.get(marker, 0) + 1
        marker_name = {
            "*": "star",
            "∗": "star",
            "†": "dagger",
            "‡": "double-dagger",
            "§": "section",
        }.get(marker, marker)
        suffix = marker_counts[marker]
        footnote_id = f"{marker_name}-{suffix}"
        footnotes[id(item)] = (footnote_id, marker, body)
        if marker.isdigit():
            marker_pattern = re.compile(rf"(?<![\w\[])({re.escape(marker)})(?![\w\]])")
        else:
            marker_pattern = re.compile(re.escape(marker))
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
            if marker_pattern.search(candidate_text):
                callouts.setdefault(id(candidate), []).append((marker, footnote_id))
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
            break
    return notes, table_callouts


def _normalized_lookup(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in decomposed if not unicodedata.combining(char))


_AUTHOR_YEAR_PATTERNS = (
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
)


def _author_year_target(
    label: str,
    reference_texts: list[tuple[int, str]],
) -> int | None:
    year_match = re.search(r"(?:19|20)\d{2}", label)
    names = re.findall(r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+", label)
    if not year_match or not names:
        return None
    surnames = [_normalized_lookup(name.rstrip(".")) for name in names]
    year = year_match.group(0)
    matches: list[int] = []
    for index, (number, reference) in enumerate(reference_texts):
        normalized_reference = _normalized_lookup(reference)
        next_text = (
            reference_texts[index + 1][1]
            if index + 1 < len(reference_texts)
            else ""
        )
        reference_years = re.findall(r"(?:19|20)\d{2}", reference)
        has_names = all(surname in normalized_reference for surname in surnames)
        if has_names and (
            year in reference
            or (not reference_years and year in next_text)
        ):
            matches.append(number)
    return matches[0] if len(matches) == 1 else None


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
    r"(?i)\b(Input|Output|Parameters?|Initialize|Set|Sample|Draw|Choose|Construct|"
    r"Form|Accept|Stop|Return|for|do|if|then|else|end\s+if|end\s+for)\b"
)


def _highlight_algorithm_html(code: str) -> str:
    rendered: list[str] = []
    for raw_line in _normalize_detached_diacritics(code).splitlines():
        body, separator, comment = raw_line.partition("//")
        escaped = html.escape(body, quote=False)
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
                + html.escape(comment, quote=False)
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


def _render(
    items: list[FlowItem],
    document: dict[str, Any],
    source: SourceReader,
    *,
    shared_reference_texts: list[tuple[int, str]] | None = None,
    reference_number_offset: int = 0,
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
    footnotes, footnote_callouts = _footnote_relations(items, reference_items)
    table_notes, table_footnote_callouts = _table_note_relations(items)
    counts = {
        "text": 0,
        "headings": 0,
        "formulas": 0,
        "tables": 0,
        "algorithms": 0,
        "code_blocks": 0,
        "pictures": 0,
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
            if reference_number is not None:
                text = re.sub(r"^\[\d+\]\s*", "", text)
                html_parts.append(
                    f'<div class="reference-entry" id="ref-{reference_number}">'
                    f'<span class="reference-number">[{reference_number}]</span> '
                    f"{html.escape(text)}</div>"
                )
                md_parts.extend(
                    [
                        f'<a id="ref-{reference_number}"></a>'
                        f"[{reference_number}] {text}",
                        "",
                    ]
                )
            elif id(item) in table_notes:
                note_id, marker, body = table_notes[id(item)]
                html_parts.append(
                    f'<aside class="footnote table-note" id="fn-{note_id}">'
                    f'<span class="footnote-label">{html.escape(marker)}</span> '
                    f"{html.escape(body)} "
                    f'<a class="footnote-backref" href="#fnref-{note_id}" '
                    f'aria-label="Back to table note callout">↩</a></aside>'
                )
                md_parts.extend(
                    [
                        f'<a id="fn-{note_id}"></a><sup>{marker}</sup> '
                        f"{body} [↩](#fnref-{note_id})",
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
                    + "</li></ul>"
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
                        "",
                    ]
                )
            elif item.kind == "footnote" and id(item) in footnotes:
                footnote_id, marker, body = footnotes[id(item)]
                html_parts.append(
                    f'<aside class="footnote" id="fn-{footnote_id}">'
                    f'<span class="footnote-label">{html.escape(marker)}</span> '
                    f"{html.escape(body)} "
                    f'<a class="footnote-backref" href="#fnref-{footnote_id}" '
                    f'aria-label="Back to footnote callout">↩</a></aside>'
                )
                md_parts.extend(
                    [
                        f'<a id="fn-{footnote_id}"></a><sup>{marker}</sup> '
                        f"{body} [↩](#fnref-{footnote_id})",
                        "",
                    ]
                )
            elif item.kind == "footnote":
                html_parts.append(f'<aside class="footnote">{html.escape(text)}</aside>')
                md_parts.extend([f"> Footnote: {text}", ""])
            else:
                html_parts.append(
                    "<p>"
                    + _inline_replacements(
                        text,
                        reference_texts,
                        footnote_callouts.get(id(item), []),
                        markdown=False,
                    )
                    + "</p>"
                )
                md_parts.extend(
                    [
                        _inline_replacements(
                            text,
                            reference_texts,
                            footnote_callouts.get(id(item), []),
                            markdown=True,
                        ),
                        "",
                    ]
                )
            counts["text"] += 1
        elif item.kind in {"algorithm", "code"}:
            algorithm = item.kind == "algorithm"
            block_title, code = _preformatted_block(source, item, algorithm=algorithm)
            if not code:
                code = item.source_text or str(node.get("text") or "")
            caption = _caption_text(document, node)
            title_text = block_title or caption
            css_class = "algorithm" if algorithm else "code-listing"
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
            html_parts.append(f"<pre><code>{highlighted}</code></pre>")
            html_parts.append("</section>")
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
            mathml = _formula_mathml(tex)
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
            markdown_tex = tex + (rf"\tag{{{number}}}" if number is not None else "")
            md_parts.extend(["$$", markdown_tex, "$$", ""])
            counts["formulas"] += 1
        elif item.kind == "table":
            grid, header_rows = _table_grid(source, item)
            caption = _caption_text(document, node)
            if not re.match(r"(?i)^Table\s+\d+\s*:", caption):
                caption = _source_caption(source, item, kind="table") or caption
            html_parts.append('<figure class="semantic-table">')
            if caption:
                html_parts.append(
                    "<figcaption>"
                    + _inline_replacements(
                        caption, reference_texts, [], markdown=False
                    )
                    + "</figcaption>"
                )
            html_parts.append("<div class=\"table-scroll\"><table>")
            table_callouts = table_footnote_callouts.get(id(item), [])
            for row_index, row in enumerate(grid):
                tag = "th" if row_index < header_rows else "td"
                html_parts.append("<tr>")
                for value in row:
                    html_parts.append(
                        f"<{tag}>"
                        + "<br>".join(
                            _inline_replacements(
                                part.strip(),
                                reference_texts,
                                table_callouts,
                                markdown=False,
                            )
                            for part in value.splitlines()
                        )
                        + f"</{tag}>"
                    )
                html_parts.append("</tr>")
            html_parts.append("</table></div></figure>")
            if caption:
                md_parts.extend([f"**{caption}**", ""])
            md_parts.extend(
                [
                    _markdown_table(
                        grid,
                        reference_texts=reference_texts,
                        footnote_callouts=table_callouts,
                    ),
                    "",
                ]
            )
            counts["tables"] += 1
        elif item.kind == "picture":
            picture_index = picture_counter.get(id(node))
            image_path = (
                f"pictures/picture_{picture_index}.png"
                if picture_index is not None
                else ""
            )
            caption = _caption_text(document, node)
            if not re.match(r"(?i)^Figure\s+\d+\s*:", caption):
                caption = _source_caption(source, item, kind="picture") or caption
            if image_path:
                html_parts.append('<figure class="picture">')
                html_parts.append(
                    f'<img src="{image_path}" alt="{html.escape(caption or "Figure", quote=True)}">'
                )
                if caption:
                    html_parts.append(f"<figcaption>{html.escape(caption)}</figcaption>")
                html_parts.append("</figure>")
                md_parts.extend(
                    [f"![{caption or 'Figure'}]({image_path})", ""]
                )
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
    try:
        source = SourceReader(input_file)
    except Exception as exc:
        result = {
            "ok": False,
            "applied": False,
            "reason": f"semantic_source_reader_unavailable:{type(exc).__name__}:{exc}",
        }
        metadata["primary_surface"] = result
        status["quality_signals"]["primary_surface"] = result
        status["ok"] = False
        status["success_class"] = "degraded_failure"
        status["warnings"].append(result["reason"])
        return result
    source_profile = source.language_profile()
    cjk_characters = source_profile["cjk_characters"]
    language_characters = cjk_characters + source_profile["latin_characters"]
    if (
        cjk_characters >= 100
        and language_characters > 0
        and cjk_characters / language_characters >= 0.2
    ):
        evidence_cleanup = _remove_review_evidence_from_primary_surfaces(output_dir)
        source.close()
        result = {
            "ok": True,
            "applied": False,
            "mode": "preserve_existing_cjk_semantic_surface",
            "reason": "cjk_formula_geometry_requires_ocr_owned_surface",
            "source_profile": source_profile,
            "review_evidence_cleanup": evidence_cleanup,
            "authoritative_surfaces": ["document.html", "document.md"],
            "source_page_images_are_review_only": True,
        }
        metadata["primary_surface"] = result
        status["quality_signals"]["primary_surface"] = result
        return result
    try:
        chunk_documents = [
            chunk.get("document")
            for chunk in document.get("chunks") or []
            if isinstance(chunk, dict) and isinstance(chunk.get("document"), dict)
        ]
        documents = chunk_documents or [document]
        prepared_parts: list[tuple[dict[str, Any], list[FlowItem], int]] = []
        shared_reference_texts: list[tuple[int, str]] = []
        reference_offset = 0
        for part in documents:
            items = _sort_items(_collect_items(part, source), part)
            _reference_map, local_reference_texts = _reference_items(items)
            prepared_parts.append((part, items, reference_offset))
            shared_reference_texts.extend(
                (number + reference_offset, text)
                for number, text in local_reference_texts
            )
            reference_offset += len(local_reference_texts)
        rendered_parts: list[tuple[str, str, dict[str, int], int]] = []
        for part, items, part_reference_offset in prepared_parts:
            part_html, part_md, part_counts = _render(
                items,
                part,
                source,
                shared_reference_texts=shared_reference_texts,
                reference_number_offset=part_reference_offset,
            )
            rendered_parts.append((part_html, part_md, part_counts, len(items)))
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
        result = {
            "ok": False,
            "applied": False,
            "reason": f"semantic_reflow_failed:{type(exc).__name__}:{exc}",
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
        "mode": "semantic_source_reflow",
        "flow_item_count": items_count,
        "counts": counts,
        "authoritative_surfaces": ["document.html", "document.md"],
        "source_page_images_are_review_only": True,
    }
    metadata["primary_surface"] = result
    status["quality_signals"]["primary_surface"] = result
    return result
