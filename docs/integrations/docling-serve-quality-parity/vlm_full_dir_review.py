#!/usr/bin/env python3
"""Run Docling VlmPipeline over a PDF directory for review comparison.

This is an evaluation helper only. It does not replace the Docling Serve
standard-pipeline adapter.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any


DEFAULT_INPUT_DIR = Path("/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline/test_pdfs")
DEFAULT_ARTIFACTS_PATH = Path("/Users/zeyuan/.cache/docling/models")
GRANITE_MLX_CACHE = DEFAULT_ARTIFACTS_PATH / "ibm-granite--granite-docling-258M-mlx"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--artifacts-path", type=Path, default=DEFAULT_ARTIFACTS_PATH)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--document-timeout", type=float, default=1500.0)
    parser.add_argument("--worker-pdf", type=Path, default=None)
    parser.add_argument("--worker-job-id", default=None)
    return parser.parse_args()


def safe_job_id(pdf: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", pdf.stem).strip("-")
    return stem or "document"


def iter_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_nodes(child)


def label_counts(document_json: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in iter_nodes(document_json):
        if isinstance(node, dict) and isinstance(node.get("label"), str):
            label = node["label"].lower()
            counts[label] = counts.get(label, 0) + 1
    return counts


def extract_label_nodes(document_json: Any, label: str) -> list[dict[str, Any]]:
    wanted = label.lower()
    return [
        node
        for node in iter_nodes(document_json)
        if isinstance(node, dict) and str(node.get("label", "")).lower() == wanted
    ]


def table_grid(table: dict[str, Any]) -> list[list[str]]:
    cells = ((table.get("data") or {}).get("table_cells") or [])
    max_row = 0
    max_col = 0
    for cell in cells:
        max_row = max(max_row, int(cell.get("end_row_offset_idx") or 0))
        max_col = max(max_col, int(cell.get("end_col_offset_idx") or 0))
    grid = [["" for _ in range(max_col)] for _ in range(max_row)]
    for cell in cells:
        row = int(cell.get("start_row_offset_idx") or 0)
        col = int(cell.get("start_col_offset_idx") or 0)
        if 0 <= row < max_row and 0 <= col < max_col:
            grid[row][col] = str(cell.get("text") or "")
    return grid


def write_table_artifacts(output_dir: Path, tables: list[dict[str, Any]]) -> int:
    tables_dir = output_dir / "tables"
    count = 0
    for index, table in enumerate(tables, start=1):
        tables_dir.mkdir(exist_ok=True)
        (tables_dir / f"table_{index}.json").write_text(
            json.dumps(table, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        grid = table_grid(table)
        if grid:
            with (tables_dir / f"table_{index}.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                csv.writer(handle).writerows(grid)
            rows = [
                "<tr>"
                + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row)
                + "</tr>"
                for row in grid
            ]
            (tables_dir / f"table_{index}.html").write_text(
                "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
                "<table>"
                + "\n".join(rows)
                + "</table></body></html>\n",
                encoding="utf-8",
            )
        count += 1
    return count


def write_page_images(doc: Any, output_dir: Path) -> int:
    pages = getattr(doc, "pages", None) or {}
    count = 0
    pages_dir = output_dir / "pages"
    for page_no, page in pages.items():
        image_ref = getattr(page, "image", None)
        pil_image = getattr(image_ref, "pil_image", None)
        if pil_image is None:
            continue
        pages_dir.mkdir(exist_ok=True)
        pil_image.save(pages_dir / f"page_{page_no}.png")
        count += 1
    return count


def write_review_index(output_dir: Path, metadata: dict[str, Any], status: dict[str, Any]) -> None:
    def links(pattern: str) -> str:
        items = sorted(output_dir.glob(pattern))
        return "".join(
            f'<li><a href="{html.escape(str(path.relative_to(output_dir)))}">'
            f'{html.escape(str(path.relative_to(output_dir)))}</a></li>'
            for path in items
            if path.is_file()
        )

    warnings = "".join(
        f"<li>{html.escape(str(warning))}</li>" for warning in status.get("warnings", [])
    )
    output_dir.joinpath("review_index.html").write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
        f"<h1>VLM review: {html.escape(str(metadata['job_id']))}</h1>"
        '<p><a href="document.html">document.html</a> | '
        '<a href="document.md">document.md</a> | '
        '<a href="document.json">document.json</a></p>'
        f"<h2>Warnings</h2><ul>{warnings}</ul>"
        f"<h2>Pages</h2><ul>{links('pages/page_*.png')}</ul>"
        f"<h2>Tables</h2><ul>{links('tables/table_*.*')}</ul>"
        "</body></html>\n",
        encoding="utf-8",
    )


def model_selection(artifacts_path: Path) -> tuple[str, list[str], bool]:
    warnings: list[str] = []
    granite_cache = artifacts_path / "ibm-granite--granite-docling-258M-mlx"
    if granite_cache.exists():
        return "granite_docling_mlx", warnings, True
    warnings.append(f"missing_required_local_mlx_model_cache:{granite_cache}")
    return "granite_docling_mlx", warnings, False


def run_worker(args: argparse.Namespace) -> int:
    assert args.worker_pdf is not None
    pdf = args.worker_pdf
    job_id = args.worker_job_id or safe_job_id(pdf)
    output_dir = args.output_root / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    model_used, warnings, model_available = model_selection(args.artifacts_path)
    start = time.perf_counter()

    if not model_available:
        status = {
            "ok": False,
            "success_class": "failure",
            "warnings": warnings,
            "errors": ["Required local MLX VLM model cache is missing; not downloading."],
        }
        metadata = {
            "parser": "docling_vlm_pipeline",
            "job_id": job_id,
            "input_file": str(pdf),
            "output_dir": str(output_dir),
            "model_used": model_used,
            "pages_processed": 0,
            "runtime_seconds": time.perf_counter() - start,
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return 1

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("DOCLING_ARTIFACTS_PATH", str(args.artifacts_path))

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
        from docling.datamodel.vlm_engine_options import MlxVlmEngineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.pipeline.vlm_pipeline import VlmPipeline
        from docling_core.types.doc import ImageRefMode

        pipeline_options = VlmPipelineOptions(
            artifacts_path=args.artifacts_path,
            document_timeout=args.document_timeout,
            images_scale=2.0,
            generate_page_images=True,
            generate_picture_images=True,
            vlm_options=VlmConvertOptions.from_preset(
                "granite_docling", engine_options=MlxVlmEngineOptions()
            ),
        )
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=VlmPipeline,
                    pipeline_options=pipeline_options,
                )
            }
        )
        result = converter.convert(pdf)
        doc = result.document
        artifacts_dir = output_dir / "artifacts"
        doc.save_as_markdown(
            output_dir / "document.md",
            artifacts_dir=artifacts_dir,
            image_mode=ImageRefMode.REFERENCED,
        )
        doc.save_as_html(
            output_dir / "document.html",
            artifacts_dir=artifacts_dir,
            image_mode=ImageRefMode.REFERENCED,
        )
        doc.save_as_json(
            output_dir / "document.json",
            artifacts_dir=artifacts_dir,
            image_mode=ImageRefMode.REFERENCED,
            indent=2,
        )
        document_json = doc.export_to_dict()
        labels = label_counts(document_json)
        tables = extract_label_nodes(document_json, "table")
        table_artifact_count = write_table_artifacts(output_dir, tables)
        page_image_count = write_page_images(doc, output_dir)
        runtime = time.perf_counter() - start
        metadata = {
            "parser": "docling_vlm_pipeline",
            "route": "B_evaluation_only",
            "job_id": job_id,
            "input_file": str(pdf),
            "output_dir": str(output_dir),
            "model_used": model_used,
            "pipeline_cls": "VlmPipeline",
            "pages_processed": len(getattr(doc, "pages", {}) or {}),
            "runtime_seconds": runtime,
            "label_counts": labels,
            "table_count": len(tables),
            "formula_count": labels.get("formula", 0),
            "picture_count": labels.get("picture", 0),
            "page_image_count": page_image_count,
            "table_artifact_count": table_artifact_count,
            "contains_html": (output_dir / "document.html").exists(),
            "contains_markdown": (output_dir / "document.md").exists(),
            "contains_json": (output_dir / "document.json").exists(),
        }
        status = {
            "ok": True,
            "success_class": "success",
            "warnings": warnings,
            "errors": [],
            "output_dir": str(output_dir),
        }
        write_review_index(output_dir, metadata, status)
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return 0
    except Exception as exc:
        runtime = time.perf_counter() - start
        status = {
            "ok": False,
            "success_class": "failure",
            "warnings": warnings,
            "errors": [f"{exc.__class__.__name__}: {exc}"],
            "traceback": traceback.format_exc(),
            "output_dir": str(output_dir),
        }
        metadata = {
            "parser": "docling_vlm_pipeline",
            "route": "B_evaluation_only",
            "job_id": job_id,
            "input_file": str(pdf),
            "output_dir": str(output_dir),
            "model_used": model_used,
            "pipeline_cls": "VlmPipeline",
            "pages_processed": 0,
            "runtime_seconds": runtime,
            "contains_html": (output_dir / "document.html").exists(),
            "contains_markdown": (output_dir / "document.md").exists(),
            "contains_json": (output_dir / "document.json").exists(),
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return 1


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def summarize_row(pdf: Path, job_id: str, output_dir: Path, elapsed: float) -> dict[str, Any]:
    metadata = load_json(output_dir / "metadata.json")
    status = load_json(output_dir / "status.json")
    labels = metadata.get("label_counts") or {}
    return {
        "input_filename": pdf.name,
        "input_path": str(pdf),
        "job_id": job_id,
        "output_dir": str(output_dir),
        "model_used": metadata.get("model_used"),
        "pages_processed": metadata.get("pages_processed"),
        "ok": bool(status.get("ok")),
        "success_class": status.get("success_class") or "failure",
        "runtime_seconds": metadata.get("runtime_seconds", elapsed),
        "warnings": status.get("warnings") or [],
        "failure_reason": "; ".join(status.get("errors") or []) or None,
        "contains_html": bool(metadata.get("contains_html")),
        "contains_markdown": bool(metadata.get("contains_markdown")),
        "contains_json": bool(metadata.get("contains_json")),
        "table_count": metadata.get("table_count") or labels.get("table", 0),
        "formula_count": metadata.get("formula_count") or labels.get("formula", 0),
        "picture_count": metadata.get("picture_count") or labels.get("picture", 0),
        "page_image_count": metadata.get("page_image_count", 0),
    }


def summarize_timeout(pdf: Path, job_id: str, output_dir: Path, elapsed: float) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "ok": False,
        "success_class": "timeout",
        "warnings": [],
        "errors": [f"timeout after {elapsed:.1f}s"],
        "output_dir": str(output_dir),
    }
    metadata = {
        "parser": "docling_vlm_pipeline",
        "route": "B_evaluation_only",
        "job_id": job_id,
        "input_file": str(pdf),
        "output_dir": str(output_dir),
        "model_used": "granite_docling_mlx",
        "pages_processed": 0,
        "runtime_seconds": elapsed,
        "contains_html": (output_dir / "document.html").exists(),
        "contains_markdown": (output_dir / "document.md").exists(),
        "contains_json": (output_dir / "document.json").exists(),
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summarize_row(pdf, job_id, output_dir, elapsed)


def write_markdown_summary(output_root: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Docling VLM Full Directory Review",
        "",
        "Route: B evaluation only. This does not replace Route A.",
        f"PDF count: {len(rows)}",
        f"Completed: {sum(1 for row in rows if row['ok'])}",
        f"Failed: {sum(1 for row in rows if not row['ok'])}",
        f"Timeouts: {sum(1 for row in rows if row['success_class'] == 'timeout')}",
        "",
        "| PDF | job_id | ok | class | model | pages | runtime | outputs | tables | formulas | images | output | warnings/failure |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        outputs = ",".join(
            name
            for name, present in [
                ("html", row["contains_html"]),
                ("md", row["contains_markdown"]),
                ("json", row["contains_json"]),
            ]
            if present
        )
        warnings = row["failure_reason"] or "; ".join(row.get("warnings") or [])
        lines.append(
            "| {pdf} | {job} | {ok} | {cls} | {model} | {pages} | {runtime:.1f}s | "
            "{outputs} | {tables} | {formulas} | {images} | {out} | {warnings} |".format(
                pdf=row["input_filename"],
                job=row["job_id"],
                ok=row["ok"],
                cls=row["success_class"],
                model=row["model_used"],
                pages=row["pages_processed"],
                runtime=float(row["runtime_seconds"] or 0.0),
                outputs=outputs or "none",
                tables=row["table_count"],
                formulas=row["formula_count"],
                images=row["page_image_count"],
                out=row["output_dir"],
                warnings=str(warnings).replace("|", "\\|")[:500],
            )
        )
    (output_root / "run_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_worker_python(python_bin: str) -> str | None:
    """Return a clear error when the selected worker cannot import Docling."""
    try:
        result = subprocess.run(
            [python_bin, "-c", "import docling"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{python_bin}: {exc}"
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "import docling failed"
    return f"{python_bin}: {detail}"


def run_batch(args: argparse.Namespace) -> int:
    args.output_root.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(args.input_dir.glob("*.pdf"))
    rows: list[dict[str, Any]] = []
    for index, pdf in enumerate(pdfs, start=1):
        job_id = safe_job_id(pdf)
        output_dir = args.output_root / job_id
        stdout_path = args.output_root / f"{job_id}.vlm_stdout.txt"
        stderr_path = args.output_root / f"{job_id}.vlm_stderr.txt"
        cmd = [
            args.python,
            str(Path(__file__).resolve()),
            "--output-root",
            str(args.output_root),
            "--artifacts-path",
            str(args.artifacts_path),
            "--document-timeout",
            str(args.document_timeout),
            "--worker-pdf",
            str(pdf),
            "--worker-job-id",
            job_id,
        ]
        print(f"[{index}/{len(pdfs)}] {pdf.name}", flush=True)
        start = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=args.timeout_seconds,
                check=False,
            )
            elapsed = time.perf_counter() - start
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
            rows.append(summarize_row(pdf, job_id, output_dir, elapsed))
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "", encoding="utf-8")
            rows.append(summarize_timeout(pdf, job_id, output_dir, elapsed))

        (args.output_root / "run_summary.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        write_markdown_summary(args.output_root, rows)
    return 0


def main() -> int:
    args = parse_args()
    if args.worker_pdf is not None:
        return run_worker(args)
    worker_error = validate_worker_python(args.python)
    if worker_error:
        print(f"Worker Python preflight failed: {worker_error}", file=sys.stderr)
        return 2
    return run_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())
