#!/usr/bin/env python3
"""LEGACY: Batch smoke test for the old direct-PDF extraction path."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch test PDF extraction for all PDFs in a directory."
    )
    parser.add_argument("--input-dir", default="test_pdfs", help="PDF input directory")
    parser.add_argument(
        "--output-dir",
        "--out-dir",
        dest="output_dir",
        default="batch_outputs",
        help="Output directory",
    )
    parser.add_argument("--summary-json", default="summary.json", help="Summary JSON path")
    parser.add_argument("--summary-md", default="summary.md", help="Summary Markdown path")
    return parser.parse_args()


def file_size(path: Path) -> int | None:
    if not path.exists():
        return None
    return path.stat().st_size


def load_meta(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def run_one(pdf_path: Path, out_dir: Path, intake: Path) -> dict[str, Any]:
    out_base = out_dir / pdf_path.stem
    txt_path = out_base.with_suffix(".raw.txt")
    md_path = out_base.with_suffix(".extract.md")
    meta_path = out_base.with_suffix(".meta.json")

    command = [
        sys.executable,
        str(intake),
        str(pdf_path),
        "--txt",
        str(txt_path),
        "--md",
        str(md_path),
        "--meta",
        str(meta_path),
        "--layout",
        "auto",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    meta = load_meta(meta_path)
    warnings = meta.get("warnings", []) if meta else []

    return {
        "pdf": str(pdf_path),
        "exit_code": result.returncode,
        "source_type": meta.get("source_type") if meta else None,
        "source_status": meta.get("source_status") if meta else None,
        "needs_pdf": meta.get("needs_pdf") if meta else None,
        "extraction_quality": meta.get("extraction_quality") if meta else None,
        "layout_detected": meta.get("layout_detected") if meta else None,
        "layout_confidence": meta.get("layout_confidence") if meta else None,
        "needs_ocr": meta.get("needs_ocr") if meta else None,
        "total_text_chars": meta.get("total_text_chars") if meta else None,
        "warnings_count": len(warnings),
        "output_file_sizes": {
            "txt": file_size(txt_path),
            "md": file_size(md_path),
            "meta": file_size(meta_path),
        },
        "outputs": {
            "txt": str(txt_path),
            "md": str(md_path),
            "meta": str(meta_path),
        },
        "stderr": result.stderr.strip(),
    }


def make_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# PDF Extraction Batch Summary",
        "",
        "| PDF | Exit | Source | Needs PDF | Quality | Layout | Confidence | Needs OCR | Text Chars | Warnings | TXT | MD | META |",
        "| --- | ---: | --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        sizes = row["output_file_sizes"]
        lines.append(
            "| {pdf} | {exit_code} | {source_type} | {needs_pdf} | {quality} | "
            "{layout} | {confidence} | {needs_ocr} | {chars} | {warnings} | "
            "{txt} | {md} | {meta} |".format(
                pdf=Path(row["pdf"]).name,
                exit_code=row["exit_code"],
                source_type=row["source_type"],
                needs_pdf=row["needs_pdf"],
                quality=row["extraction_quality"],
                layout=row["layout_detected"],
                confidence=row["layout_confidence"],
                needs_ocr=row["needs_ocr"],
                chars=row["total_text_chars"],
                warnings=row["warnings_count"],
                txt=sizes["txt"],
                md=sizes["md"],
                meta=sizes["meta"],
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    input_dir = (project_root / args.input_dir).resolve()
    out_dir = (project_root / args.output_dir).resolve()
    summary_json = (project_root / args.summary_json).resolve()
    summary_md = (project_root / args.summary_md).resolve()
    intake = Path(__file__).resolve().parent / "intake_detect.py"

    if not input_dir.exists():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2

    pdfs = sorted(input_dir.glob("*.pdf"))
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [run_one(pdf, out_dir, intake) for pdf in pdfs]
    payload = {
        "input_dir": str(input_dir),
        "out_dir": str(out_dir),
        "pdf_count": len(pdfs),
        "results": rows,
    }

    summary_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_md.write_text(make_markdown(rows), encoding="utf-8")

    return 0 if all(row["exit_code"] == 0 for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
