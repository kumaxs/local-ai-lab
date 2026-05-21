#!/usr/bin/env python3
"""Detect intake file type and route PDFs to the legacy pdf_extract.py path."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect source type before PDF extraction."
    )
    parser.add_argument("input_file", help="Downloaded source file")
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


def read_head(path: Path, size: int = 512) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def detect_source_type(path: Path) -> str:
    head = read_head(path)
    stripped = head.lstrip()
    lowered = stripped[:128].lower()
    if stripped.startswith(b"%PDF"):
        return "pdf"
    if lowered.startswith((b"<!doctype", b"<html", b"<!doc")):
        return "html"
    return "unsupported"


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    return data.decode("utf-8", errors="replace")


def first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return html.unescape(match.group(1)).strip()


def extract_html_meta(path: Path) -> dict[str, Any]:
    text = read_text_lossy(path)
    title = first_match(r"<title[^>]*>(.*?)</title>", text)
    canonical = first_match(
        r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)[\"']", text
    ) or first_match(
        r"<link[^>]+href=[\"']([^\"']+)[\"'][^>]+rel=[\"']canonical[\"']", text
    )
    doi = first_match(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text)
    pii = first_match(r"(S\d{8,}[A-Z0-9]*\d*)", text)
    if canonical and not pii:
        pii = first_match(r"/pii/(S\d{8,}[A-Z0-9]*\d*)", canonical)

    return {
        "html_title": title,
        "canonical_url": canonical,
        "doi": doi,
        "pii": pii,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_markdown(path: Path, source_name: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([f"# Intake Status: {source_name}", "", *lines]).rstrip() + "\n"
    path.write_text(content, encoding="utf-8")


def run_pdf_extract(args: argparse.Namespace, source: Path) -> int:
    extractor = Path(__file__).resolve().parent / "pdf_extract.py"
    command = [
        sys.executable,
        str(extractor),
        str(source),
        "--txt",
        str(Path(args.txt)),
        "--md",
        str(Path(args.md)),
        "--meta",
        str(Path(args.meta)),
        "--layout",
        args.layout,
        "--two-column-header-ratio",
        str(args.two_column_header_ratio),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        return result.returncode

    meta_path = Path(args.meta)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("source_type", "pdf")
    meta.setdefault("source_status", "pdf_extracted")
    meta.setdefault("needs_pdf", False)
    write_json(meta_path, meta)
    return 0


def handle_html(source: Path, meta_path: Path, md_path: Path) -> int:
    html_meta = extract_html_meta(source)
    payload = {
        "source": str(source.resolve()),
        "source_type": "html",
        "source_status": "not_pdf",
        "needs_pdf": True,
        "fulltext_available": "unknown",
        "html_title": html_meta["html_title"],
        "canonical_url": html_meta["canonical_url"],
        "doi": html_meta["doi"],
        "pii": html_meta["pii"],
        "extraction_quality": "no_pdf",
        "quality_flags": [
            "downloaded_html_instead_of_pdf",
            "needs_manual_pdf_or_access_check",
        ],
        "warnings": [],
    }
    write_json(meta_path, payload)
    write_markdown(
        md_path,
        source.name,
        [
            "This file is HTML, not a PDF.",
            "",
            "PDF retrieval or access check is required.",
        ],
    )
    return 0


def handle_unsupported(source: Path, meta_path: Path, md_path: Path) -> int:
    payload = {
        "source": str(source.resolve()),
        "source_type": "unsupported",
        "source_status": "unsupported",
        "needs_pdf": True,
        "extraction_quality": "no_pdf",
        "quality_flags": [
            "unsupported_source_type",
            "needs_manual_pdf_or_access_check",
        ],
        "warnings": ["Input file is neither PDF nor recognized HTML."],
    }
    write_json(meta_path, payload)
    write_markdown(
        md_path,
        source.name,
        [
            "This file is not a recognized PDF.",
            "",
            "PDF retrieval or manual source inspection is required.",
        ],
    )
    return 0


def main() -> int:
    args = parse_args()
    source = Path(args.input_file).expanduser().resolve()
    meta_path = Path(args.meta).expanduser().resolve()
    md_path = Path(args.md).expanduser().resolve()

    if not source.exists():
        print(f"Input file does not exist: {source}", file=sys.stderr)
        return 2
    if not source.is_file():
        print(f"Input path is not a file: {source}", file=sys.stderr)
        return 2

    source_type = detect_source_type(source)
    if source_type == "pdf":
        return run_pdf_extract(args, source)
    if source_type == "html":
        return handle_html(source, meta_path, md_path)
    return handle_unsupported(source, meta_path, md_path)


if __name__ == "__main__":
    raise SystemExit(main())
