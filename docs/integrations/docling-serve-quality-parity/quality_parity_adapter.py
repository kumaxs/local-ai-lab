#!/usr/bin/env python3
"""Minimal Docling Serve quality parity adapter boundary.

This script is the narrow n8n-callable boundary between Local AI Lab automation
and Docling Serve. Docling Serve remains the model execution backend; this
adapter owns only the quality policy and contract-output mapping.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GXX_RE = re.compile(r"/G[0-9A-Fa-f]{2}")

START_COMMAND = (
    "UVICORN_WORKERS=1 DOCLING_DEVICE=cpu "
    "DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true "
    "DOCLING_SERVE_ENG_KIND=local "
    "DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1 "
    "DOCLING_SERVE_ENG_LOC_SHARE_MODELS=true "
    "DOCLING_SERVE_ARTIFACTS_PATH=/Users/zeyuan/.cache/docling/models "
    "DOCLING_SERVE_LOAD_MODELS_AT_BOOT=true "
    "DOCLING_SERVE_OPTIONS_CACHE_SIZE=2 "
    ".runtime/docling-serve/.venv/bin/docling-serve run "
    "--host 127.0.0.1 --port 5001 "
    "--artifacts-path /Users/zeyuan/.cache/docling/models"
)

GRANITE_MLX_CODE_FORMULA_CONFIG: dict[str, Any] = {
    "engine_options": {"engine_type": "mlx"},
    "model_spec": {
        "name": "Granite-Docling-258M",
        "default_repo_id": "ibm-granite/granite-docling-258M",
        "revision": "main",
        "prompt": "",
        "response_format": "plaintext",
        "engine_overrides": {
            "mlx": {
                "repo_id": "ibm-granite/granite-docling-258M-mlx",
                "revision": None,
                "torch_dtype": None,
                "extra_config": {},
            },
            "transformers": {
                "repo_id": None,
                "revision": None,
                "torch_dtype": None,
                "extra_config": {
                    "transformers_model_type": "automodel-imagetexttotext",
                    "extra_generation_config": {"skip_special_tokens": False},
                },
            },
        },
        "api_overrides": {
            "api_ollama": {"params": {"model": "ibm/granite-docling:258m"}}
        },
        "trust_remote_code": False,
        "stop_strings": ["</doctag>", "<|end_of_text|>"],
        "max_new_tokens": 8192,
    },
    "scale": 2.0,
    "max_size": None,
    "extract_code": True,
    "extract_formulas": True,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-url", default="http://127.0.0.1:5001")
    parser.add_argument("--input-file", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--job-id",
        default=None,
        help="Stable job id used as the output directory name; intended for n8n.",
    )
    parser.add_argument("--sample-name", default=None)
    parser.add_argument("--page-start", type=int, default=None)
    parser.add_argument("--page-end", type=int, default=None)
    parser.add_argument("--gxx-count-threshold", type=int, default=50)
    parser.add_argument("--gxx-density-threshold", type=float, default=0.001)
    parser.add_argument(
        "--ocr-fallback-policy",
        choices=["gxx", "off"],
        default="gxx",
        help="Use gxx to retry with force_ocr=true when bad text-layer metrics fail.",
    )
    parser.add_argument(
        "--force-ocr-on-gxx",
        action="store_true",
        help="Backward-compatible alias for --ocr-fallback-policy=gxx.",
    )
    parser.add_argument(
        "--formula-policy",
        choices=["granite_mlx", "off"],
        default="granite_mlx",
        help="Use granite_mlx to request Granite-Docling-CodeFormula through MLX.",
    )
    parser.add_argument(
        "--enable-formula-mlx",
        action="store_true",
        help="Backward-compatible alias for --formula-policy=granite_mlx.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument(
        "--image-export-mode",
        choices=["embedded", "referenced", "placeholder"],
        default="embedded",
        help="Use embedded by default so document.html opens without sidecar images.",
    )
    return parser.parse_args()


def effective_formula_policy(args: argparse.Namespace) -> str:
    if args.enable_formula_mlx:
        return "granite_mlx"
    return args.formula_policy


def effective_ocr_fallback_policy(args: argparse.Namespace) -> str:
    if args.force_ocr_on_gxx:
        return "gxx"
    return args.ocr_fallback_policy


def page_range(args: argparse.Namespace) -> list[int] | None:
    if args.page_start is None and args.page_end is None:
        return None
    if args.page_start is None or args.page_end is None:
        raise ValueError("--page-start and --page-end must be provided together")
    if args.page_start < 1 or args.page_end < args.page_start:
        raise ValueError("page range must be 1-based and page-end >= page-start")
    return [args.page_start, args.page_end]


def post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def get_json(url: str, timeout: int = 10) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


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
        label = node.get("label") if isinstance(node, dict) else None
        if isinstance(label, str):
            counts[label] = counts.get(label, 0) + 1
    return counts


def extract_table_nodes(document_json: Any) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for node in iter_nodes(document_json):
        if isinstance(node, dict) and str(node.get("label", "")).lower() == "table":
            tables.append(node)
    return tables


def combined_text(document: dict[str, Any]) -> str:
    parts = [
        document.get("md_content") or "",
        document.get("html_content") or "",
        document.get("text_content") or "",
    ]
    if document.get("json_content") is not None:
        parts.append(json.dumps(document["json_content"], ensure_ascii=False))
    return "\n".join(parts)


def gxx_quality(document: dict[str, Any]) -> dict[str, Any]:
    text = combined_text(document)
    count = len(GXX_RE.findall(text))
    length = max(len(text), 1)
    return {
        "text_quality_gxx_count": count,
        "text_quality_gxx_density": count / length,
        "text_length": len(text),
    }


def formula_metrics(document: dict[str, Any]) -> dict[str, Any]:
    text = combined_text(document)
    document_json = document.get("json_content")
    labels = label_counts(document_json)
    formula_examples: list[str] = []
    for node in iter_nodes(document_json):
        if not isinstance(node, dict):
            continue
        if str(node.get("label", "")).lower() != "formula":
            continue
        formula_text = str(node.get("text") or "")
        if formula_text:
            formula_examples.append(formula_text[:240])
    return {
        "formula_count": labels.get("formula", 0),
        "formula_placeholder_count": text.count("Formula not decoded"),
        "formula_latex_like_count": len(
            re.findall(
                r"\\(?:frac|sum|int|alpha|beta|gamma|theta|math|mathbf|mathrm|sqrt|infty|cdot|left|right|sigma|mu)",
                text,
            )
        ),
        "formula_examples": formula_examples[:5],
    }


def html_ref_metrics(document: dict[str, Any]) -> dict[str, Any]:
    html = document.get("html_content") or ""
    markdown = document.get("md_content") or ""
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    md_images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)
    return {
        "html_len": len(html),
        "html_src_href_count": len(refs),
        "markdown_image_ref_count": len(md_images),
        "image_refs_embedded": html.count("data:image/") + markdown.count("data:image/"),
        "image_refs_external": len(
            [
                ref
                for ref in [*refs, *md_images]
                if ref and not ref.startswith(("data:", "http://", "https://", "#"))
            ]
        ),
    }


def broken_local_refs(output_dir: Path, document: dict[str, Any]) -> list[str]:
    html = document.get("html_content") or ""
    markdown = document.get("md_content") or ""
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    refs.extend(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown))
    broken: list[str] = []
    for ref in refs:
        if not ref or ref.startswith(("data:", "http://", "https://", "#", "mailto:")):
            continue
        if not (output_dir / ref).exists():
            broken.append(ref)
    return broken


def base_options(args: argparse.Namespace, *, force_ocr: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "from_formats": ["pdf"],
        "to_formats": ["md", "json", "html"],
        "image_export_mode": args.image_export_mode,
        "do_ocr": True,
        "force_ocr": force_ocr,
        "ocr_preset": "auto",
        "do_table_structure": True,
        "table_mode": "accurate",
        "table_cell_matching": True,
        "include_images": True,
        "images_scale": 2.0,
        "do_formula_enrichment": effective_formula_policy(args) == "granite_mlx",
        "do_code_enrichment": False,
    }
    selected_page_range = page_range(args)
    if selected_page_range:
        options["page_range"] = selected_page_range
    if effective_formula_policy(args) == "granite_mlx":
        options["code_formula_custom_config"] = GRANITE_MLX_CODE_FORMULA_CONFIG
    return options


def request_payload(args: argparse.Namespace, options: dict[str, Any]) -> dict[str, Any]:
    pdf_bytes = args.input_file.read_bytes()
    return {
        "sources": [
            {
                "kind": "file",
                "filename": args.input_file.name,
                "base64_string": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        ],
        "target": {"kind": "inbody"},
        "options": options,
    }


def write_contract_outputs(
    output_dir: Path,
    response: dict[str, Any],
    metadata: dict[str, Any],
    status: dict[str, Any],
) -> None:
    document = response.get("document") or {}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "document.md").write_text(
        document.get("md_content") or "", encoding="utf-8"
    )
    (output_dir / "document.html").write_text(
        document.get("html_content") or "", encoding="utf-8"
    )
    (output_dir / "document.json").write_text(
        json.dumps(document.get("json_content"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tables = extract_table_nodes(document.get("json_content"))
    tables_dir = output_dir / "tables"
    for index, table in enumerate(tables, start=1):
        tables_dir.mkdir(exist_ok=True)
        (tables_dir / f"table_{index}.json").write_text(
            json.dumps(table, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def summarize_response(
    name: str,
    response: dict[str, Any],
    wall_time: float,
    options: dict[str, Any],
    warnings: list[str],
    args: argparse.Namespace,
    output_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = response.get("document") or {}
    document_json = document.get("json_content")
    labels = label_counts(document_json)
    gxx = gxx_quality(document)
    formulas = formula_metrics(document)
    refs = html_ref_metrics(document)
    table_nodes = extract_table_nodes(document_json)
    broken_refs = broken_local_refs(output_dir, document)
    selected_formula_policy = effective_formula_policy(args)
    selected_ocr_policy = effective_ocr_fallback_policy(args)

    metadata = {
        "parser": "docling_serve",
        "conversion_policy": "quality_first_docling_serve_parity",
        "job_id": name,
        "sample_name": args.sample_name,
        "input_file": str(args.input_file),
        "output_dir": str(output_dir),
        "serve_url": args.serve_url.rstrip("/"),
        "serve_status": response.get("status"),
        "serve_processing_time": response.get("processing_time"),
        "client_wall_time": wall_time,
        "page_range": options.get("page_range"),
        "ocr_fallback_policy": selected_ocr_policy,
        "gxx_count_threshold": args.gxx_count_threshold,
        "gxx_density_threshold": args.gxx_density_threshold,
        "ocr_fallback_used": bool(options.get("force_ocr")),
        "formula_model": "granite_docling_mlx"
        if options.get("code_formula_custom_config")
        else None,
        "formula_policy": selected_formula_policy,
        "image_export_mode": options.get("image_export_mode"),
        "table_count": len(table_nodes),
        "asset_count": 0,
        "table_artifact_count": len(table_nodes),
        "broken_local_refs_count": len(broken_refs),
        "broken_local_refs": broken_refs[:20],
        "generated_outputs": [
            "document.md",
            "document.html",
            "document.json",
            "metadata.json",
            "status.json",
        ],
        "label_counts": labels,
        **gxx,
        **formulas,
        **refs,
    }
    if table_nodes:
        metadata["generated_outputs"].extend(
            [f"tables/table_{index}.json" for index in range(1, len(table_nodes) + 1)]
        )

    status = {
        "ok": response.get("status") == "success",
        "success_class": "success" if response.get("status") == "success" else "failure",
        "errors": response.get("errors") or [],
        "warnings": warnings,
        "output_dir": str(output_dir),
        "metadata_path": str(output_dir / "metadata.json"),
        "status_path": str(output_dir / "status.json"),
        "n8n_read_fields": {
            "status_ok": "status.json:ok",
            "success_class": "status.json:success_class",
            "warnings": "status.json:warnings",
            "text_quality": "status.json:quality_signals.text_quality_gxx_count",
            "ocr_fallback_used": "metadata.json:ocr_fallback_used",
            "formula_placeholders": "metadata.json:formula_placeholder_count",
            "table_count": "metadata.json:table_count",
            "generated_outputs": "metadata.json:generated_outputs",
        },
        "quality_signals": {
            **gxx,
            **formulas,
            "table_count": len(table_nodes),
            "table_artifact_count": len(table_nodes),
            "broken_local_refs_count": len(broken_refs),
            **refs,
        },
    }
    return metadata, status


def run_conversion(
    args: argparse.Namespace,
    name: str,
    *,
    force_ocr: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    options = base_options(args, force_ocr=force_ocr)
    payload = request_payload(args, options)
    start = time.perf_counter()
    response = post_json(
        f"{args.serve_url.rstrip('/')}/v1/convert/source",
        payload,
        timeout=args.timeout_seconds,
    )
    wall_time = time.perf_counter() - start
    warnings: list[str] = []
    if effective_formula_policy(args) == "granite_mlx":
        warnings.append("formula_enrichment_requested_granite_docling_mlx")
    if force_ocr:
        warnings.append("ocr_fallback_force_ocr_request")
    output_dir = args.output_root / name
    metadata, status = summarize_response(
        name, response, wall_time, options, warnings, args, output_dir
    )
    return response, metadata, status


def main() -> int:
    args = parse_args()
    try:
        selected_page_range = page_range(args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "blocked": str(exc)}, indent=2))
        return 2

    if not args.input_file.exists():
        print(
            json.dumps(
                {"ok": False, "blocked": f"input file not found: {args.input_file}"},
                indent=2,
            )
        )
        return 2

    name = args.job_id or args.sample_name or args.input_file.stem
    output_dir = args.output_root / name

    try:
        version = get_json(f"{args.serve_url.rstrip('/')}/version")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "blocked": f"Docling Serve is not reachable: {exc}",
                    "start_command": START_COMMAND,
                },
                indent=2,
            )
        )
        return 2

    response, metadata, status = run_conversion(args, name, force_ocr=False)
    metadata["docling_serve_version"] = version
    status["docling_serve_version"] = version

    gxx_count = metadata["text_quality_gxx_count"]
    gxx_density = metadata["text_quality_gxx_density"]
    should_fallback = (
        effective_ocr_fallback_policy(args) == "gxx"
        and gxx_count >= args.gxx_count_threshold
        and gxx_density >= args.gxx_density_threshold
    )

    if should_fallback:
        response, metadata, status = run_conversion(args, name, force_ocr=True)
        metadata["docling_serve_version"] = version
        status["docling_serve_version"] = version
        metadata["ocr_fallback_reason"] = "gxx_quality_failure"
        status["warnings"].insert(
            0,
            (
                "text_quality_failed_gxx; forced OCR fallback via "
                "Docling Serve force_ocr=true"
            ),
        )

    gaps = [
        "Serve response does not provide prior custom formula/source crop links.",
        "Serve response does not write standalone assets/ and tables/ unless post-processed.",
        "This adapter writes table_N.json from table nodes but does not reconstruct table HTML/Markdown artifacts.",
        "This adapter is a minimal n8n-callable boundary, not a product decision to make it the long-term service.",
    ]
    status["warnings"].extend(gaps)
    if gaps and status["ok"]:
        status["success_class"] = "degraded_success"
    metadata["known_gaps"] = gaps
    metadata["n8n_callable"] = True
    metadata["effective_page_range"] = selected_page_range

    write_contract_outputs(output_dir, response, metadata, status)
    summary = {
        "ok": status["ok"],
        "output_dir": str(output_dir),
        "metadata": metadata,
        "status": status,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
