#!/usr/bin/env python3
"""LEGACY: Minimal PDF text extraction for academic papers.

This script is kept for the current n8n/local-ai-python-worker runtime path.
Future ingestion should route through a Docling-ready parser layer instead of
treating direct PDF text extraction as the primary business path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import unicodedata
import sys
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader


for logger_name in ("pdfminer", "pdfplumber"):
    logging.getLogger(logger_name).setLevel(logging.ERROR)


MIN_TEXT_CHARS_PER_PAGE = 40
MIN_TOTAL_TEXT_CHARS = 200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract basic text and metadata from a paper PDF."
    )
    parser.add_argument("input_pdf", help="Source PDF path")
    parser.add_argument("--txt", required=True, help="Output raw text path")
    parser.add_argument("--md", required=True, help="Output Markdown path")
    parser.add_argument("--meta", required=True, help="Output metadata JSON path")
    parser.add_argument(
        "--layout",
        choices=("single", "two-column", "auto"),
        default="single",
        help="Text extraction layout mode. Default: single",
    )
    parser.add_argument(
        "--two-column-header-ratio",
        type=float,
        default=0.18,
        help="Top full-width header height ratio for two-column mode. Default: 0.18",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_metadata(metadata: Any) -> dict[str, Any]:
    if not metadata:
        return {}

    normalized: dict[str, Any] = {}
    for key, value in dict(metadata).items():
        clean_key = str(key).lstrip("/")
        if value is None:
            normalized[clean_key] = None
        else:
            normalized[clean_key] = str(value)
    return normalized


def get_pdf_info(path: Path) -> tuple[dict[str, Any], PdfReader | None, list[str]]:
    warnings: list[str] = []

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise RuntimeError(f"Failed to open PDF with pypdf: {exc}") from exc

    encrypted = bool(reader.is_encrypted)
    if encrypted:
        try:
            decrypt_result = reader.decrypt("")
        except Exception as exc:
            raise RuntimeError(f"PDF is encrypted and could not be decrypted: {exc}") from exc
        if decrypt_result == 0:
            raise RuntimeError("PDF is encrypted and could not be decrypted with an empty password.")
        warnings.append("PDF is encrypted but was readable after empty-password decrypt.")

    try:
        pages = len(reader.pages)
    except Exception as exc:
        raise RuntimeError(f"Failed to read PDF pages with pypdf: {exc}") from exc

    page_info: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        media_box = page.mediabox
        page_info.append(
            {
                "page": index,
                "width": float(media_box.width),
                "height": float(media_box.height),
            }
        )

    info = {
        "pages": pages,
        "encrypted": encrypted,
        "metadata": normalize_metadata(reader.metadata),
        "page_info": page_info,
    }
    return info, reader, warnings


def extract_page_text(page: Any, page_number: int, warnings: list[str]) -> str:
    try:
        return (page.extract_text() or "").strip()
    except Exception as exc:
        warnings.append(f"Page {page_number}: text extraction failed: {exc}")
        return ""


def clamp_bbox(
    bbox: tuple[float, float, float, float],
    parent_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x0, top, x1, bottom = bbox
    parent_x0, parent_top, parent_x1, parent_bottom = parent_bbox
    return (
        max(parent_x0, min(x0, parent_x1)),
        max(parent_top, min(top, parent_bottom)),
        max(parent_x0, min(x1, parent_x1)),
        max(parent_top, min(bottom, parent_bottom)),
    )


def is_valid_bbox(bbox: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = bbox
    return x0 < x1 and top < bottom


def extract_two_column_page_text(
    page: Any, page_number: int, header_ratio: float, warnings: list[str]
) -> str:
    parent_bbox = page.bbox
    page_x0, page_top, page_x1, page_bottom = parent_bbox
    page_width = page_x1 - page_x0
    page_height = page_bottom - page_top
    header_bottom = page_top + (page_height * header_ratio)
    midpoint = page_x0 + (page_width / 2)
    sections: list[str] = []

    for label, bbox in (
        ("header", (page_x0, page_top, page_x1, header_bottom)),
        ("left body", (page_x0, header_bottom, midpoint, page_bottom)),
        ("right body", (midpoint, header_bottom, page_x1, page_bottom)),
    ):
        crop_bbox = clamp_bbox(bbox, parent_bbox)
        if not is_valid_bbox(crop_bbox):
            warnings.append(
                f"Page {page_number} {label}: invalid crop bbox after clamp: {crop_bbox}"
            )
            return extract_page_text(page, page_number, warnings)
        try:
            section = page.crop(crop_bbox)
            sections.append((section.extract_text() or "").strip())
        except Exception as exc:
            warnings.append(
                f"Page {page_number} {label}: crop extraction failed, falling back to full page: {exc}"
            )
            return extract_page_text(page, page_number, warnings)

    return "\n\n".join(text for text in sections if text).strip()


def extract_text(
    path: Path, layout: str, two_column_header_ratio: float
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    page_texts: list[str] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                if layout == "two-column":
                    text = extract_two_column_page_text(
                        page, index, two_column_header_ratio, warnings
                    )
                else:
                    text = extract_page_text(page, index, warnings)
                page_texts.append(text)
    except Exception as exc:
        raise RuntimeError(f"Failed to extract text with pdfplumber: {exc}") from exc

    return page_texts, warnings


def detect_layout(path: Path, header_ratio: float) -> tuple[str, float, list[str]]:
    warnings: list[str] = []
    page_scores: list[float] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:3]:
                parent_x0, parent_top, parent_x1, parent_bottom = page.bbox
                page_width = parent_x1 - parent_x0
                page_height = parent_bottom - parent_top
                if page_width <= 0 or page_height <= 0:
                    continue

                body_top = parent_top + (page_height * header_ratio)
                midpoint = parent_x0 + (page_width / 2)
                gutter_half_width = page_width * 0.035
                words = page.extract_words() or []
                body_words = [
                    word
                    for word in words
                    if ((float(word["top"]) + float(word["bottom"])) / 2) >= body_top
                ]
                if len(body_words) < 40:
                    continue

                left = 0
                right = 0
                crossing_gutter = 0
                for word in body_words:
                    x0 = float(word["x0"])
                    x1 = float(word["x1"])
                    center = (x0 + x1) / 2
                    if center < midpoint:
                        left += 1
                    else:
                        right += 1
                    if x0 < midpoint + gutter_half_width and x1 > midpoint - gutter_half_width:
                        crossing_gutter += 1

                if not left or not right:
                    page_scores.append(0.0)
                    continue

                balance = min(left, right) / max(left, right)
                gutter_clear = 1 - (crossing_gutter / len(body_words))
                page_scores.append(max(0.0, min(1.0, (balance * 0.55) + (gutter_clear * 0.45))))
    except Exception as exc:
        warnings.append(f"Auto layout detection failed, using single layout: {exc}")
        return "single", 0.0, warnings

    confidence = max(page_scores) if page_scores else 0.0
    detected = "two-column" if confidence >= 0.62 else "single"
    return detected, round(confidence, 3), warnings


def make_raw_text(page_texts: list[str]) -> str:
    return "\n\n".join(text for text in page_texts if text).strip() + "\n"


def make_markdown(source: Path, page_texts: list[str]) -> str:
    lines = [
        f"# Extracted Text: {source.name}",
        "",
        "> This is an automated text extraction. Layout, figures, tables, equations, spacing, and reading order may be incomplete. Use the original PDF as the source of truth.",
        "",
    ]
    for index, text in enumerate(page_texts, start=1):
        lines.extend([f"## Page {index}", "", text or "_No extractable text found._", ""])
    return "\n".join(lines).rstrip() + "\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def needs_ocr(page_texts: list[str]) -> bool:
    if not page_texts:
        return True
    total_chars = sum(len(text) for text in page_texts)
    return (
        total_chars < MIN_TOTAL_TEXT_CHARS
        and all(len(text) < MIN_TEXT_CHARS_PER_PAGE for text in page_texts)
    )


def extraction_quality(
    layout_detected: str, total_text_chars: int, needs_ocr_value: bool
) -> tuple[str, list[str]]:
    if needs_ocr_value or total_text_chars == 0:
        return "no_text", ["needs_ocr", "no_extractable_text"]
    if layout_detected == "two-column":
        return (
            "rough",
            [
                "text_extraction_only",
                "possible_column_fragmentation",
                "spacing_may_be_incorrect",
                "figures_tables_not_extracted",
            ],
        )
    return (
        "basic",
        [
            "text_extraction_only",
            "figures_tables_not_extracted",
            "spacing_may_be_incorrect",
        ],
    )


def is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def is_private_or_replacement(char: str) -> bool:
    return unicodedata.category(char) == "Co" or char == "\ufffd"


def is_suspicious_symbol(char: str) -> bool:
    if char.isspace() or char.isalnum() or is_cjk(char):
        return False
    category = unicodedata.category(char)
    return (
        is_private_or_replacement(char)
        or category in {"So", "Sk", "Cn"}
        or ord(char) < 32
    )


def garbled_text_flags(text: str, total_text_chars: int, needs_ocr_value: bool) -> list[str]:
    if needs_ocr_value or total_text_chars == 0:
        return []

    visible_chars = [char for char in text if not char.isspace()]
    if len(visible_chars) < MIN_TOTAL_TEXT_CHARS:
        return []

    readable_chars = [
        char for char in visible_chars if char.isascii() and char.isalnum() or is_cjk(char)
    ]
    suspicious_chars = [char for char in visible_chars if is_suspicious_symbol(char)]
    private_chars = [char for char in visible_chars if is_private_or_replacement(char)]
    long_tokens = [token for token in text.split() if len(token) >= 28]
    suspicious_long_tokens = [
        token
        for token in long_tokens
        if (
            sum(1 for char in token if is_suspicious_symbol(char)) >= 2
            or sum(1 for char in token if char.isdigit()) >= 5
            or (
                sum(1 for char in token if char.isupper()) >= 8
                and sum(1 for char in token if char.isdigit() or not char.isalnum()) >= 4
            )
        )
    ]

    visible_count = len(visible_chars)
    cjk_count = sum(1 for char in visible_chars if is_cjk(char))
    readable_ratio = len(readable_chars) / visible_count
    suspicious_ratio = len(suspicious_chars) / visible_count
    private_ratio = len(private_chars) / visible_count
    suspicious_long_ratio = len(suspicious_long_tokens) / max(1, len(long_tokens))
    tokens = text.split()
    ascii_encoding_tokens = []
    for token in tokens:
        if len(token) < 12:
            continue
        cjk_in_token = sum(1 for char in token if is_cjk(char))
        ascii_alpha = sum(1 for char in token if char.isascii() and char.isalpha())
        digits = sum(1 for char in token if char.isdigit())
        punctuation = sum(
            1 for char in token if not char.isalnum() and not is_cjk(char)
        )
        uppercase = sum(1 for char in token if char.isascii() and char.isupper())
        if (
            cjk_in_token == 0
            and ascii_alpha >= 6
            and digits + punctuation >= 3
            and (uppercase >= 4 or punctuation / len(token) >= 0.18)
        ):
            ascii_encoding_tokens.append(token)

    cjk_ratio = cjk_count / visible_count
    ascii_encoding_token_ratio = len(ascii_encoding_tokens) / max(1, len(tokens))

    has_encoding_signal = (
        private_ratio >= 0.003
        or suspicious_ratio >= 0.08
        or (suspicious_ratio >= 0.035 and readable_ratio < 0.78)
        or (cjk_ratio >= 0.15 and ascii_encoding_token_ratio >= 0.05)
    )
    has_garbled_signal = (
        suspicious_long_tokens
        and (suspicious_long_ratio >= 0.25 or len(suspicious_long_tokens) >= 8)
    ) or readable_ratio < 0.55 or (
        cjk_ratio >= 0.15 and len(ascii_encoding_tokens) >= 20
    )

    if has_encoding_signal and has_garbled_signal:
        return ["possible_font_encoding_issue", "possible_garbled_text"]
    return []


def main() -> int:
    args = parse_args()
    source = Path(args.input_pdf).expanduser().resolve()
    txt_path = Path(args.txt).expanduser().resolve()
    md_path = Path(args.md).expanduser().resolve()
    meta_path = Path(args.meta).expanduser().resolve()

    if not 0 <= args.two_column_header_ratio <= 1:
        print("--two-column-header-ratio must be between 0 and 1.", file=sys.stderr)
        return 2

    if not source.exists():
        print(f"Input PDF does not exist: {source}", file=sys.stderr)
        return 2
    if not source.is_file():
        print(f"Input path is not a file: {source}", file=sys.stderr)
        return 2

    warnings: list[str] = []

    try:
        pdf_info, _reader, pypdf_warnings = get_pdf_info(source)
        warnings.extend(pypdf_warnings)
        layout_detected = args.layout
        layout_confidence = 1.0
        if args.layout == "auto":
            layout_detected, layout_confidence, detection_warnings = detect_layout(
                source, args.two_column_header_ratio
            )
            warnings.extend(detection_warnings)
        page_texts, extraction_warnings = extract_text(
            source, layout_detected, args.two_column_header_ratio
        )
        warnings.extend(extraction_warnings)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    raw_text = make_raw_text(page_texts)
    markdown = make_markdown(source, page_texts)
    total_text_chars = sum(len(text) for text in page_texts)
    needs_ocr_value = needs_ocr(page_texts)
    extraction_quality_value, quality_flags = extraction_quality(
        layout_detected, total_text_chars, needs_ocr_value
    )
    quality_flags.extend(
        flag
        for flag in garbled_text_flags(raw_text, total_text_chars, needs_ocr_value)
        if flag not in quality_flags
    )

    meta = {
        "source": str(source),
        "sha256": sha256_file(source),
        "layout_requested": args.layout,
        "layout_detected": layout_detected,
        "layout_confidence": layout_confidence,
        "pages": pdf_info["pages"],
        "encrypted": pdf_info["encrypted"],
        "metadata": pdf_info["metadata"],
        "page_info": pdf_info["page_info"],
        "total_text_chars": total_text_chars,
        "needs_ocr": needs_ocr_value,
        "extraction_quality": extraction_quality_value,
        "quality_flags": quality_flags,
        "text_extraction_only": True,
        "warnings": warnings,
    }

    try:
        write_text(txt_path, raw_text)
        write_text(md_path, markdown)
        write_json(meta_path, meta)
    except OSError as exc:
        print(f"Failed to write output: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
