#!/usr/bin/env python3
"""Run Docling Serve parity adapter over a directory of PDFs.

This is a review-output helper, not a production n8n integration. It calls the
repo-backed parity adapter once per PDF, continues after failures, and writes
top-level run summaries for manual inspection.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs"),
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--serve-url", default="http://127.0.0.1:5001")
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--http-retries", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    return parser.parse_args()


def safe_job_id(pdf: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", pdf.stem).strip("-")
    return stem or "document"


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def summarize_success(pdf: Path, job_id: str, output_dir: Path, elapsed: float) -> dict[str, Any]:
    metadata = load_json(output_dir / "metadata.json") or {}
    status = load_json(output_dir / "status.json") or {}
    signals = status.get("quality_signals") or {}
    return {
        "input_filename": pdf.name,
        "input_path": str(pdf),
        "job_id": job_id,
        "output_dir": str(output_dir),
        "ok": bool(status.get("ok")),
        "success_class": status.get("success_class"),
        "ocr_fallback_used": metadata.get("ocr_fallback_used"),
        "text_quality_gxx_count": metadata.get("text_quality_gxx_count"),
        "text_quality_gxx_density": metadata.get("text_quality_gxx_density"),
        "formula_placeholder_count": metadata.get("formula_placeholder_count"),
        "formula_count": metadata.get("formula_count"),
        "table_count": metadata.get("table_count"),
        "image_refs_embedded": metadata.get("image_refs_embedded"),
        "markdown_image_ref_count": metadata.get("markdown_image_ref_count"),
        "warnings": status.get("warnings") or [],
        "runtime_seconds": elapsed,
        "failure_reason": None if status.get("ok") else "; ".join(status.get("errors") or []),
    }


def summarize_failure(
    pdf: Path,
    job_id: str,
    output_dir: Path,
    elapsed: float,
    reason: str,
    timed_out: bool,
) -> dict[str, Any]:
    return {
        "input_filename": pdf.name,
        "input_path": str(pdf),
        "job_id": job_id,
        "output_dir": str(output_dir),
        "ok": False,
        "success_class": "timeout" if timed_out else "failure",
        "ocr_fallback_used": None,
        "text_quality_gxx_count": None,
        "text_quality_gxx_density": None,
        "formula_placeholder_count": None,
        "formula_count": None,
        "table_count": None,
        "image_refs_embedded": None,
        "markdown_image_ref_count": None,
        "warnings": [],
        "runtime_seconds": elapsed,
        "failure_reason": reason,
    }


def write_markdown_summary(output_root: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Docling Serve Full Directory Review",
        "",
        f"PDF count: {len(rows)}",
        f"Completed: {sum(1 for row in rows if row['ok'])}",
        f"Failed: {sum(1 for row in rows if not row['ok'])}",
        f"Timeouts: {sum(1 for row in rows if row['success_class'] == 'timeout')}",
        "",
        "| PDF | ok | class | OCR | /Gxx | density | formulas | placeholders | tables | embedded images | runtime | output | warnings/failure |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        warning_text = row["failure_reason"] or "; ".join(row.get("warnings") or [])
        lines.append(
            "| {pdf} | {ok} | {cls} | {ocr} | {gxx} | {density} | {formulas} | "
            "{placeholders} | {tables} | {images} | {runtime:.1f}s | {output} | {warning} |".format(
                pdf=row["input_filename"],
                ok=row["ok"],
                cls=row["success_class"],
                ocr=row["ocr_fallback_used"],
                gxx=row["text_quality_gxx_count"],
                density=row["text_quality_gxx_density"],
                formulas=row["formula_count"],
                placeholders=row["formula_placeholder_count"],
                tables=row["table_count"],
                images=row["image_refs_embedded"],
                runtime=float(row["runtime_seconds"] or 0.0),
                output=row["output_dir"],
                warning=str(warning_text).replace("|", "\\|")[:500],
            )
        )
    (output_root / "run_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    pdfs = sorted(args.input_dir.glob("*.pdf"))
    args.output_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for index, pdf in enumerate(pdfs, start=1):
        job_id = safe_job_id(pdf)
        output_dir = args.output_root / job_id
        stdout_path = args.output_root / f"{job_id}.adapter_stdout.json"
        stderr_path = args.output_root / f"{job_id}.adapter_stderr.txt"
        cmd = [
            args.python,
            str(args.adapter),
            "--serve-url",
            args.serve_url,
            "--input-file",
            str(pdf),
            "--output-root",
            str(args.output_root),
            "--job-id",
            job_id,
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--http-retries",
            str(args.http_retries),
        ]
        print(f"[{index}/{len(pdfs)}] {pdf.name}", flush=True)
        start = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=args.timeout_seconds + 30,
                check=False,
            )
            elapsed = time.perf_counter() - start
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
            if result.returncode == 0:
                rows.append(summarize_success(pdf, job_id, output_dir, elapsed))
            else:
                reason = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                rows.append(summarize_failure(pdf, job_id, output_dir, elapsed, reason, False))
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "", encoding="utf-8")
            rows.append(
                summarize_failure(
                    pdf,
                    job_id,
                    output_dir,
                    elapsed,
                    f"timeout after {args.timeout_seconds}s",
                    True,
                )
            )

        (args.output_root / "run_summary.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        write_markdown_summary(args.output_root, rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
