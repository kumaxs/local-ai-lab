#!/usr/bin/env python3
"""Run Docling Serve parity adapter over a directory of PDFs.

This is a review-output helper, not a production n8n integration. It calls the
repo-backed parity adapter once per PDF, continues after failures, and writes
top-level run summaries for manual inspection.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline/test_pdfs"),
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--serve-url", default="http://127.0.0.1:5001")
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--http-retries", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--formula-second-pass-policy",
        choices=["off", "review", "apply", "apply-all"],
        default="apply-all",
    )
    parser.add_argument(
        "--formula-second-pass-route-b-root",
        type=Path,
        default=None,
        help="Directory containing per-sample Route B/VLM outputs named by PDF stem.",
    )
    parser.add_argument(
        "--formula-second-pass-review-candidate-root",
        action="append",
        default=[],
        help=(
            "Optional per-sample review-only candidate root as LABEL=DIR or DIR; "
            "each sample uses <DIR>/<job-id>."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-guarded-fallback-root",
        action="append",
        default=[],
        help=(
            "Optional per-sample guarded fallback root as LABEL=DIR or DIR; "
            "each sample uses <DIR>/<job-id>."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-guarded-fallback-eq",
        action="append",
        type=int,
        default=[],
        help="Reviewed equation number allowed to use guarded fallback replacement.",
    )
    parser.add_argument(
        "--cn-ocr-parity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Forward CN OCRMac parity fallback options to each adapter invocation.",
    )
    parser.add_argument(
        "--cn-ocr-request-shape",
        choices=["preset", "custom"],
        default="preset",
    )
    parser.add_argument("--cn-ocr-chunk-size", type=int, default=1)
    return parser.parse_args()


def safe_job_id(pdf: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", pdf.stem).strip("-")
    return stem or "document"


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_labeled_root(value: str) -> tuple[str | None, Path]:
    if "=" in value:
        label, path = value.split("=", 1)
        return label.strip() or None, Path(path)
    return None, Path(value)


def sample_source_arg(value: str, job_id: str) -> str:
    label, root = parse_labeled_root(value)
    sample_dir = root / job_id
    return f"{label}={sample_dir}" if label else str(sample_dir)


def summarize_success(pdf: Path, job_id: str, output_dir: Path, elapsed: float) -> dict[str, Any]:
    metadata = load_json(output_dir / "metadata.json") or {}
    status = load_json(output_dir / "status.json") or {}
    signals = status.get("quality_signals") or {}
    second_pass = metadata.get("formula_second_pass") or {}
    alignment = second_pass.get("alignment_diagnostics") or {}
    structural = metadata.get("structural_quarantine_qc") or {}
    emphasis = metadata.get("semantic_emphasis") or {}
    number_diag = metadata.get("formula_number_qc_diagnostics") or []
    recovered_numbers = metadata.get("formula_number_recovered_html_indexes") or []
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
        "second_pass_attempted_count": second_pass.get("second_pass_attempted_count"),
        "second_pass_main_output_replaced_count": second_pass.get("replaced_count"),
        "second_pass_fallback_count": second_pass.get("fallback_count"),
        "formula_all_second_pass_attempted": alignment.get("all_formulas_attempted"),
        "formula_sequence_mismatch_count": alignment.get("sequence_mismatch_count"),
        "duplicate_equation_number_count": alignment.get("duplicate_equation_number_count"),
        "image_formula_not_converted_count": alignment.get("image_formula_not_converted_count"),
        "missing_formula_number_count": len(
            [
                item for item in number_diag
                if "display_formula_missing_equation_number" in (item.get("reasons") or [])
            ]
        ),
        "recovered_formula_number_count": len(recovered_numbers),
        "unresolved_formula_number_count": len(
            [
                item for item in number_diag
                if "display_formula_missing_equation_number" in (item.get("reasons") or [])
                and not item.get("safe_to_recover")
            ]
        ),
        "header_footer_footnote_candidate_count": structural.get("candidate_count"),
        "isolated_main_text_pollution_count": structural.get("isolated_main_text_pollution_count"),
        "exported_structural_content_count": structural.get("exported_structural_content_count"),
        "exported_structural_content_counts_by_kind": (
            structural.get("exported_structural_content_counts_by_kind") or {}
        ),
        "final_output_structural_residual_count": structural.get("final_output_residual_count"),
        "semantic_emphasis_detected_count": emphasis.get("detected_span_count"),
        "semantic_emphasis_html_count": emphasis.get("html_applied_span_count"),
        "semantic_emphasis_markdown_count": emphasis.get("markdown_applied_span_count"),
        "assembled_note_count": structural.get("assembled_note_count"),
        "note_reference_link_count": structural.get("note_reference_link_count"),
        "unresolved_note_reference_count": structural.get("unresolved_note_reference_count"),
        "unresolved_structural_note_count": structural.get("unresolved_structural_note_count"),
        "recovered_footnote_count": structural.get("recovered_footnote_count"),
        "unresolved_footnote_count": structural.get("unresolved_footnote_count"),
        "evidence_links": {
            "review_index": str(output_dir / "review_index.html"),
            "metadata": str(output_dir / "metadata.json"),
            "status": str(output_dir / "status.json"),
            "formula_second_pass": str(output_dir / "formula_second_pass" / "second_pass_summary.json"),
            "structural_content": str(output_dir / "structural_content.json"),
            "structural_regions": str(output_dir / "structural_regions.json"),
        },
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
        "second_pass_attempted_count": None,
        "second_pass_main_output_replaced_count": None,
        "second_pass_fallback_count": None,
        "formula_all_second_pass_attempted": None,
        "formula_sequence_mismatch_count": None,
        "duplicate_equation_number_count": None,
        "image_formula_not_converted_count": None,
        "missing_formula_number_count": None,
        "recovered_formula_number_count": None,
        "unresolved_formula_number_count": None,
        "header_footer_footnote_candidate_count": None,
        "isolated_main_text_pollution_count": None,
        "exported_structural_content_count": None,
        "exported_structural_content_counts_by_kind": {},
        "final_output_structural_residual_count": None,
        "semantic_emphasis_detected_count": None,
        "semantic_emphasis_html_count": None,
        "semantic_emphasis_markdown_count": None,
        "assembled_note_count": None,
        "note_reference_link_count": None,
        "unresolved_note_reference_count": None,
        "unresolved_structural_note_count": None,
        "recovered_footnote_count": None,
        "unresolved_footnote_count": None,
        "evidence_links": {},
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

    qc_lines = [
        "# All Test PDF QC Summary",
        "",
        "| PDF | formulas | second-pass attempts | main replacements | fallbacks | missing eq nums | recovered eq nums | unresolved eq nums | structure candidates | isolated pollution | recovered footnotes | unresolved footnotes | evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        evidence = (row.get("evidence_links") or {}).get("review_index") or row.get("output_dir")
        qc_lines.append(
            "| {pdf} | {formulas} | {attempts} | {replaced} | {fallbacks} | {missing} | "
            "{recovered} | {unresolved} | {struct} | {isolated} | {foot_recovered} | "
            "{foot_unresolved} | {evidence} |".format(
                pdf=row["input_filename"],
                formulas=row.get("formula_count"),
                attempts=row.get("second_pass_attempted_count"),
                replaced=row.get("second_pass_main_output_replaced_count"),
                fallbacks=row.get("second_pass_fallback_count"),
                missing=row.get("missing_formula_number_count"),
                recovered=row.get("recovered_formula_number_count"),
                unresolved=row.get("unresolved_formula_number_count"),
                struct=row.get("header_footer_footnote_candidate_count"),
                isolated=row.get("isolated_main_text_pollution_count"),
                foot_recovered=row.get("recovered_footnote_count"),
                foot_unresolved=row.get("unresolved_footnote_count"),
                evidence=str(evidence).replace("|", "\\|"),
            )
        )
    qc_lines.append("")
    qc_lines.extend(
        [
            "| PDF | all formulas attempted | sequence mismatches | duplicate eq nums | image formulas not converted |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        qc_lines.append(
            "| {pdf} | {all_attempted} | {mismatch} | {dupes} | {not_converted} |".format(
                pdf=row["input_filename"],
                all_attempted=row.get("formula_all_second_pass_attempted"),
                mismatch=row.get("formula_sequence_mismatch_count"),
                dupes=row.get("duplicate_equation_number_count"),
                not_converted=row.get("image_formula_not_converted_count"),
            )
        )
    (output_root / "all_testpdf_qc_summary.md").write_text(
        "\n".join(qc_lines) + "\n", encoding="utf-8"
    )


def write_manual_review_index(output_root: Path, rows: list[dict[str, Any]]) -> None:
    review_dir = output_root / "manual_review"
    review_dir.mkdir(parents=True, exist_ok=True)
    table_rows = []
    markdown_rows = []
    for row in rows:
        job_id = row["job_id"]
        prefix = f"../{job_id}"
        status_label = "OK" if row.get("ok") else "FAILED"
        output_dir = output_root / job_id
        input_path = Path(str(row.get("input_path") or "")).expanduser()
        source_link = "N/A"
        md_source_link = "N/A"
        if input_path:
            source_href = "file://" + quote(input_path.as_posix())
            source_text = html.escape(input_path.as_posix())
            source_link = (
                f'<a href="{source_href}">Source PDF</a>'
                f'<br><code>{source_text}</code>'
            )
            md_source_link = f"[Source PDF]({source_href})<br>`{input_path.as_posix()}`"
        html_link = (
            f'<a href="{prefix}/document.html">HTML</a>'
            if (output_dir / "document.html").exists()
            else "N/A"
        )
        md_link = (
            f'<a href="{prefix}/document.md">Markdown</a>'
            if (output_dir / "document.md").exists()
            else "N/A"
        )
        structural_content_link = (
            f'<a href="{prefix}/structural_content.json">Extracted structure</a>'
            if (output_dir / "structural_content.json").exists()
            else "Not generated"
        )
        structural_regions_link = (
            f'<a href="{prefix}/structural_regions.json">QC evidence</a>'
            if (output_dir / "structural_regions.json").exists()
            else "Not generated"
        )
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(row['input_filename'])}</td>"
            f"<td>{status_label}</td>"
            f"<td>{source_link}</td>"
            f"<td>{html_link}</td>"
            f"<td>{md_link}</td>"
            f"<td>{structural_content_link}</td>"
            f"<td>{structural_regions_link}</td>"
            "</tr>"
        )
        md_html_link = (
            f"[HTML]({job_id}/document.html)"
            if (output_dir / "document.html").exists()
            else "N/A"
        )
        md_document_link = (
            f"[Markdown]({job_id}/document.md)"
            if (output_dir / "document.md").exists()
            else "N/A"
        )
        md_structural_content_link = (
            f"[Extracted structure]({job_id}/structural_content.json)"
            if (output_dir / "structural_content.json").exists()
            else "Not generated"
        )
        md_structural_regions_link = (
            f"[QC evidence]({job_id}/structural_regions.json)"
            if (output_dir / "structural_regions.json").exists()
            else "Not generated"
        )
        markdown_rows.append(
            f"| {row['input_filename']} | {status_label} | "
            f"{md_source_link} | "
            f"{md_html_link} | {md_document_link} | "
            f"{md_structural_content_link} | {md_structural_regions_link} |"
        )
    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docling manual review</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 90rem; padding: 0 1rem; color: #17212b; }
table { border-collapse: collapse; width: 100%%; }
th, td { border-bottom: 1px solid #cbd3da; padding: .65rem; text-align: left; }
th { background: #eef2f4; }
a { color: #075ea8; }
.summary { color: #52606d; }
</style>
</head>
<body>
<h1>Docling manual review</h1>
<p class="summary">Completed: %d / %d. Review rendered HTML first, then compare Markdown and structural evidence.</p>
<table>
<thead><tr><th>PDF</th><th>Status</th><th>Source PDF</th><th>Rendered</th><th>Markdown</th><th>Structural output</th><th>Evidence</th></tr></thead>
<tbody>%s</tbody>
</table>
</body>
</html>
""" % (
        sum(1 for row in rows if row.get("ok")),
        len(rows),
        "".join(table_rows),
    )
    (review_dir / "index.html").write_text(page, encoding="utf-8")
    markdown = [
        "# Docling manual review",
        "",
        "| PDF | Status | Source PDF | Rendered | Markdown | Structural output | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *markdown_rows,
        "",
    ]
    (output_root / "MANUAL_REVIEW.md").write_text(
        "\n".join(markdown),
        encoding="utf-8",
    )


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
        if args.formula_second_pass_policy != "off":
            cmd.extend(["--formula-second-pass-policy", args.formula_second_pass_policy])
            if args.formula_second_pass_route_b_root is not None:
                route_b_dir = args.formula_second_pass_route_b_root / job_id
                cmd.extend(["--formula-second-pass-route-b-dir", str(route_b_dir)])
            for value in args.formula_second_pass_review_candidate_root:
                cmd.extend(
                    [
                        "--formula-second-pass-review-candidate-dir",
                        sample_source_arg(value, job_id),
                    ]
                )
            for value in args.formula_second_pass_guarded_fallback_root:
                cmd.extend(
                    [
                        "--formula-second-pass-guarded-fallback-dir",
                        sample_source_arg(value, job_id),
                    ]
                )
            for eq_number in args.formula_second_pass_guarded_fallback_eq:
                cmd.extend(
                    [
                        "--formula-second-pass-guarded-fallback-eq",
                        str(eq_number),
                    ]
                )
        if args.cn_ocr_parity:
            cmd.append("--cn-ocr-parity")
            cmd.extend(["--cn-ocr-request-shape", args.cn_ocr_request_shape])
            cmd.extend(["--cn-ocr-chunk-size", str(args.cn_ocr_chunk_size)])
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
        write_manual_review_index(args.output_root, rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
