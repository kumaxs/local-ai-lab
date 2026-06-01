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
DATA_IMAGE_RE = re.compile(r"data:image/[^\"')\s]+")
CN_OCR_LANG = ["zh-Hans", "zh-Hant", "en-US"]

START_COMMAND = (
    "UVICORN_WORKERS=1 DOCLING_DEVICE=cpu "
    "DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true "
    "DOCLING_SERVE_ALLOW_CUSTOM_OCR_CONFIG=true "
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
        "--cn-ocr-parity",
        action="store_true",
        help=(
            "On /Gxx failure, request OCRMac full-page OCR with "
            "zh-Hans/zh-Hant/en-US locales."
        ),
    )
    parser.add_argument(
        "--cn-ocr-request-shape",
        choices=["preset", "custom"],
        default="preset",
        help=(
            "preset uses ocr_preset=ocrmac plus ocr_lang; custom uses "
            "ocr_custom_config.kind=ocrmac and requires "
            "DOCLING_SERVE_ALLOW_CUSTOM_OCR_CONFIG=true."
        ),
    )
    parser.add_argument(
        "--cn-ocr-chunk-size",
        type=int,
        default=1,
        help="Page count per chunk when full-document CN OCR fallback gets 503/504.",
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
        "--http-retries",
        type=int,
        default=3,
        help="Retry transient Docling Serve HTTP 503/504 responses this many times.",
    )
    parser.add_argument(
        "--http-retry-sleep-seconds",
        type=float,
        default=10.0,
        help="Base sleep between transient HTTP retries; multiplied by attempt.",
    )
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


def is_transient_http_error(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in {503, 504}


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: int,
    retries: int,
    retry_sleep_seconds: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in {503, 504} or attempt >= retries:
                raise
            time.sleep(retry_sleep_seconds * (attempt + 1))
    raise RuntimeError("unreachable retry state")


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
    return DATA_IMAGE_RE.sub("data:image/<stripped>", "\n".join(parts))


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


def apply_cn_ocr_options(options: dict[str, Any], args: argparse.Namespace) -> None:
    options["force_ocr"] = True
    if args.cn_ocr_request_shape == "custom":
        options["ocr_preset"] = "auto"
        options.pop("ocr_lang", None)
        options["ocr_custom_config"] = {
            "kind": "ocrmac",
            "lang": CN_OCR_LANG,
            "recognition": "accurate",
            "framework": "vision",
        }
    else:
        options["ocr_preset"] = "ocrmac"
        options["ocr_lang"] = CN_OCR_LANG
        options.pop("ocr_custom_config", None)


def base_options(
    args: argparse.Namespace,
    *,
    force_ocr: bool,
    page_range_override: list[int] | None = None,
    cn_ocr_parity: bool = False,
) -> dict[str, Any]:
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
    selected_page_range = page_range_override or page_range(args)
    if selected_page_range:
        options["page_range"] = selected_page_range
    if cn_ocr_parity and force_ocr:
        apply_cn_ocr_options(options, args)
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


def page_count_from_document(document_json: Any) -> int | None:
    if not isinstance(document_json, dict):
        return None
    pages = document_json.get("pages")
    if isinstance(pages, dict):
        page_numbers: list[int] = []
        for key in pages:
            try:
                page_numbers.append(int(key))
            except (TypeError, ValueError):
                continue
        return max(page_numbers) if page_numbers else len(pages)
    if isinstance(pages, list):
        return len(pages)

    page_numbers: list[int] = []
    for node in iter_nodes(document_json):
        if not isinstance(node, dict):
            continue
        for prov in node.get("prov") or []:
            if isinstance(prov, dict) and isinstance(prov.get("page_no"), int):
                page_numbers.append(prov["page_no"])
    return max(page_numbers) if page_numbers else None


def chunk_ranges(page_count: int, chunk_size: int) -> list[list[int]]:
    if chunk_size < 1:
        raise ValueError("--cn-ocr-chunk-size must be >= 1")
    ranges: list[list[int]] = []
    for start in range(1, page_count + 1, chunk_size):
        ranges.append([start, min(start + chunk_size - 1, page_count)])
    return ranges


def merge_chunk_responses(chunk_responses: list[dict[str, Any]]) -> dict[str, Any]:
    md_parts: list[str] = []
    html_parts: list[str] = []
    json_chunks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    errors: list[Any] = []
    processing_time = 0.0
    status = "success"

    for item in chunk_responses:
        page_range_value = item.get("page_range")
        response = item.get("response") or {}
        document = response.get("document") or {}
        if response.get("status") != "success":
            status = response.get("status") or "failure"
        errors.extend(response.get("errors") or [])
        try:
            processing_time += float(response.get("processing_time") or 0.0)
        except (TypeError, ValueError):
            pass
        md_parts.append(
            f"\n\n<!-- docling-serve-cn-chunk pages {page_range_value} -->\n\n"
            + (document.get("md_content") or "")
        )
        html_parts.append(
            "<section class=\"docling-cn-chunk\" "
            f"data-page-range=\"{page_range_value}\">\n"
            f"<h2>Pages {page_range_value}</h2>\n"
            f"{document.get('html_content') or ''}\n</section>"
        )
        text_parts.append(document.get("text_content") or "")
        json_chunks.append(
            {
                "page_range": page_range_value,
                "document": document.get("json_content"),
                "status": response.get("status"),
                "errors": response.get("errors") or [],
            }
        )

    return {
        "status": status,
        "processing_time": processing_time,
        "errors": errors,
        "document": {
            "md_content": "\n".join(md_parts),
            "html_content": "\n".join(
                [
                    "<!doctype html><html><head><meta charset=\"utf-8\">",
                    "<title>Docling Serve CN OCR chunks</title>",
                    "</head><body>",
                    *html_parts,
                    "</body></html>",
                ]
            ),
            "text_content": "\n".join(text_parts),
            "json_content": {
                "schema_name": "local_ai_lab_docling_serve_chunked",
                "source": "docling_serve_cn_ocr_chunked",
                "chunks": json_chunks,
            },
        },
    }


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
        "ocr_backend": options.get("ocr_preset")
        or (options.get("ocr_custom_config") or {}).get("kind"),
        "ocr_lang": options.get("ocr_lang")
        or (options.get("ocr_custom_config") or {}).get("lang"),
        "ocr_custom_config_kind": (options.get("ocr_custom_config") or {}).get("kind"),
        "force_ocr": bool(options.get("force_ocr")),
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
    page_range_override: list[int] | None = None,
    cn_ocr_parity: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    options = base_options(
        args,
        force_ocr=force_ocr,
        page_range_override=page_range_override,
        cn_ocr_parity=cn_ocr_parity,
    )
    payload = request_payload(args, options)
    start = time.perf_counter()
    response = post_json(
        f"{args.serve_url.rstrip('/')}/v1/convert/source",
        payload,
        timeout=args.timeout_seconds,
        retries=args.http_retries,
        retry_sleep_seconds=args.http_retry_sleep_seconds,
    )
    wall_time = time.perf_counter() - start
    warnings: list[str] = []
    if effective_formula_policy(args) == "granite_mlx":
        warnings.append("formula_enrichment_requested_granite_docling_mlx")
    if force_ocr:
        warnings.append("ocr_fallback_force_ocr_request")
    if cn_ocr_parity and force_ocr:
        warnings.append(
            "ocr_fallback_cn_ocrmac_full_page_requested:"
            f"{args.cn_ocr_request_shape}:{','.join(CN_OCR_LANG)}"
        )
    output_dir = args.output_root / name
    metadata, status = summarize_response(
        name, response, wall_time, options, warnings, args, output_dir
    )
    return response, metadata, status


def run_cn_chunked_fallback(
    args: argparse.Namespace,
    name: str,
    *,
    page_count: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ranges = chunk_ranges(page_count, args.cn_ocr_chunk_size)
    chunk_responses: list[dict[str, Any]] = []
    failed_pages: list[int] = []
    chunk_failures: list[dict[str, Any]] = []
    start = time.perf_counter()

    for range_value in ranges:
        try:
            response, _, _ = run_conversion(
                args,
                name,
                force_ocr=True,
                page_range_override=range_value,
                cn_ocr_parity=True,
            )
            chunk_responses.append({"page_range": range_value, "response": response})
        except Exception as exc:
            failed_pages.extend(range(range_value[0], range_value[1] + 1))
            chunk_failures.append(
                {
                    "page_range": range_value,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                    "transient_http": is_transient_http_error(exc),
                }
            )

    merged = merge_chunk_responses(chunk_responses)
    if failed_pages:
        merged["status"] = "partial_success" if chunk_responses else "failure"
        merged["errors"] = list(merged.get("errors") or []) + chunk_failures

    output_dir = args.output_root / name
    warnings = [
        "text_quality_failed_gxx; full-document CN OCR fallback failed; "
        "attempted all-page chunked OCRMac fallback",
        "ocr_fallback_cn_chunked",
    ]
    if failed_pages:
        warnings.append(f"ocr_fallback_cn_chunked_failed_pages:{failed_pages}")
    options = base_options(args, force_ocr=True, cn_ocr_parity=True)
    metadata, status = summarize_response(
        name,
        merged,
        time.perf_counter() - start,
        options,
        warnings,
        args,
        output_dir,
    )
    covered_pages = [
        page for range_value in ranges for page in range(range_value[0], range_value[1] + 1)
    ]
    metadata["ocr_fallback_used"] = bool(chunk_responses)
    metadata["ocr_fallback_mode"] = "chunked"
    metadata["ocr_fallback_pages"] = covered_pages
    metadata["ocr_fallback_failed_pages"] = failed_pages
    metadata["ocr_fallback_chunk_ranges"] = ranges
    metadata["ocr_fallback_chunk_failures"] = chunk_failures
    status["quality_signals"]["ocr_fallback_mode"] = "chunked"
    status["quality_signals"]["ocr_fallback_pages"] = covered_pages
    status["quality_signals"]["ocr_fallback_failed_pages"] = failed_pages
    status["warnings"].extend(
        [
            "Serve response was merged from page/chunk OCR fallback responses.",
            "Chunked JSON preserves chunk documents under json_content.chunks.",
        ]
    )
    if failed_pages:
        status["ok"] = False
        status["success_class"] = "degraded_failure"
    return merged, metadata, status


def failure_response(message: str, error: BaseException | None = None) -> dict[str, Any]:
    errors: list[dict[str, Any]] = [{"message": message}]
    if error is not None:
        errors[0]["error_type"] = error.__class__.__name__
        errors[0]["error"] = str(error)
        if is_transient_http_error(error):
            errors[0]["http_code"] = getattr(error, "code", None)
    return {
        "status": "failure",
        "processing_time": 0.0,
        "errors": errors,
        "document": {
            "md_content": "",
            "html_content": (
                "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
                f"<body><h1>Conversion failed</h1><p>{message}</p></body></html>"
            ),
            "text_content": "",
            "json_content": {"error": message},
        },
    }


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
        first_pass_response = response
        try:
            response, metadata, status = run_conversion(
                args,
                name,
                force_ocr=True,
                cn_ocr_parity=args.cn_ocr_parity,
            )
        except Exception as exc:
            page_count = page_count_from_document(
                (first_pass_response.get("document") or {}).get("json_content")
            )
            if args.cn_ocr_parity and is_transient_http_error(exc) and page_count:
                response, metadata, status = run_cn_chunked_fallback(
                    args,
                    name,
                    page_count=page_count,
                )
                metadata["ocr_fallback_reason"] = "gxx_quality_failure"
                metadata["ocr_fallback_full_document_error"] = {
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                    "http_code": getattr(exc, "code", None),
                }
                status["warnings"].insert(
                    0,
                    (
                        "text_quality_failed_gxx; full-document CN OCRMac fallback "
                        "got transient Serve error; used chunked fallback"
                    ),
                )
            else:
                response = failure_response("required OCR fallback failed", exc)
                output_dir = args.output_root / name
                options = base_options(
                    args,
                    force_ocr=True,
                    cn_ocr_parity=args.cn_ocr_parity,
                )
                metadata, status = summarize_response(
                    name,
                    response,
                    0.0,
                    options,
                    [
                        "text_quality_failed_gxx; required OCR fallback failed",
                        (
                            "ocr_fallback_cn_ocrmac_full_page_requested"
                            if args.cn_ocr_parity
                            else "ocr_fallback_force_ocr_request"
                        ),
                    ],
                    args,
                    output_dir,
                )
                metadata["ocr_fallback_reason"] = "gxx_quality_failure"
                metadata["ocr_fallback_used"] = False
                metadata["ocr_fallback_mode"] = "failed"
                metadata["ocr_fallback_error"] = {
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                    "http_code": getattr(exc, "code", None),
                }
                status["ok"] = False
                status["success_class"] = "failure"
        else:
            metadata["ocr_fallback_reason"] = "gxx_quality_failure"
            metadata["ocr_fallback_mode"] = (
                "full_document_ocrmac" if args.cn_ocr_parity else "full_document"
            )
            metadata["ocr_fallback_pages"] = "all"
            status["warnings"].insert(
                0,
                (
                    "text_quality_failed_gxx; forced OCR fallback via "
                    + (
                        "Docling Serve OCRMac full-page request"
                        if args.cn_ocr_parity
                        else "Docling Serve force_ocr=true"
                    )
                ),
            )
        metadata["docling_serve_version"] = version
        status["docling_serve_version"] = version

        fallback_gxx_count = metadata["text_quality_gxx_count"]
        fallback_gxx_density = metadata["text_quality_gxx_density"]
        fallback_still_failed = (
            fallback_gxx_count >= args.gxx_count_threshold
            and fallback_gxx_density >= args.gxx_density_threshold
        )
        if fallback_still_failed and status["ok"]:
            status["ok"] = False
            status["success_class"] = "degraded_failure"
            status["warnings"].insert(
                0,
                (
                    "required OCR fallback completed but text quality still failed "
                    f"/Gxx thresholds: count={fallback_gxx_count}, "
                    f"density={fallback_gxx_density}"
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
