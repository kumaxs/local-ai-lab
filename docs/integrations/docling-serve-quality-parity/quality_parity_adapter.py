#!/usr/bin/env python3
"""Minimal Docling Server quality parity adapter boundary.

This script is the narrow n8n-callable boundary between Local AI Lab automation
and Docling Server. Docling Server remains the model execution backend; this
adapter owns only the quality policy and contract-output mapping.
"""

from __future__ import annotations

import argparse
import base64
import csv
import difflib
import html
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from formula_only_second_pass import (
    canonicalize_formula_output,
    extract_formulas,
    formula_hallucination_reasons,
    normalize_formula_candidate,
    run_formula_second_pass,
    validate_candidate_latex,
)

GXX_RE = re.compile(r"/G[0-9A-Fa-f]{2}")
DATA_IMAGE_RE = re.compile(r"data:image/[^\"')\s]+")
PLAIN_URL_RE = re.compile(r"(?<![\"'=])(https?://[^\s<]+)")
FORMULA_NUMBER_RE = re.compile(r"\(\s*(\d+)\s*\)")
SPACED_FORMULA_NUMBER_RE = re.compile(r"\(\s*((?:\d\s+)+\d)\s*\)")
CN_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
CN_OCR_LANG = ["zh-Hans", "zh-Hant", "en-US"]
V1_GXX_FAILURE_MIN_COUNT = 10
V1_GXX_FAILURE_MIN_DENSITY = 0.002
FORMULA_SOURCE_PADDING_PX = 2
FORMULA_CONTEXT_PADDING_PX = 96
DEFAULT_REVIEW_PADDING_PX = 18
UNRESOLVED_V1_PARITY_WARNINGS = [
    "v1_parity_gap_footnotes_not_improved_by_server_adapter",
    "v1_parity_gap_pdf_links_not_preserved_or_exported_as_links_json",
    "v1_parity_gap_inline_formula_html_rendering_requires_review",
    "v1_parity_gap_math_symbol_rendering_requires_review",
]
CN_FINAL_POLISH_FORMULA_NUMBERS = (1, 2, 12)
CN_ACCEPTED_BASELINE = {
    "name": "accepted_cn_0854aa1",
    "commit": "0854aa1",
    "output": ".runtime/review/docling-adapter-html-polish-live-fullfallback-2026-06-04/CN",
    "document_html_sha256": "6911693bd781c628da70ae2494471f2f4cfd28448000aa599290353cd6af97db",
    "formula_count": 24,
    "equation_numbers": list(range(1, 25)),
    "minimum_cn_character_count": 9900,
    "minimum_final_output_cn_character_count": 9000,
}
CN_FINAL_TEXT_CORRECTIONS = (
    (
        re.compile(r"获\s*取历史时刻知识状态的权重力"),
        "获取历史时刻知识状态的权重为",
    ),
)
PAGE_EDGE_LABELS = {"page_header", "page_footer"}
HEADER_FOOTER_NOISE_RE = re.compile(
    r"(?i)\b(arxiv|proceedings|conference|workshop|copyright|all rights reserved|"
    r"technical version|preprint|accepted|published|doi:|isbn)\b"
)
MATH_TEXT_RE = re.compile(
    r"(?:\\(?:frac|sum|int|alpha|beta|gamma|theta|mathcal|mathbf|mathrm|sqrt|infty|cdot|left|right)|"
    r"[Θ∆Φℝ𝑊𝑟𝑑𝒩×≪ˆ=|])"
)
ALIGNMENT_ENV_RE = re.compile(
    r"\\begin\s*\{\s*(?:aligned|align|array|matrix|pmatrix|bmatrix|cases|split|gathered)\s*\}"
)

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
    parser.add_argument("--gxx-count-threshold", type=int, default=V1_GXX_FAILURE_MIN_COUNT)
    parser.add_argument(
        "--gxx-density-threshold",
        type=float,
        default=V1_GXX_FAILURE_MIN_DENSITY,
    )
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
        help="Retry transient Docling Server HTTP 503/504 responses this many times.",
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
    parser.add_argument(
        "--formula-second-pass-policy",
        choices=["off", "review", "apply", "apply-all"],
        default="apply-all",
        help=(
            "Optionally run formula_only_second_pass.py after adapter outputs are "
            "written. review writes sidecar evidence only; apply replaces "
            "suspicious formulas in document.md/document.json; apply-all attempts "
            "every discovered formula and replaces main outputs only when the "
            "second-pass candidate passes quality gates."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-route-b-dir",
        type=Path,
        default=None,
        help="Route B output directory used only as formula candidate source.",
    )
    parser.add_argument(
        "--formula-second-pass-output-dir",
        type=Path,
        default=None,
        help=(
            "Sidecar output directory for formula second-pass evidence. Defaults "
            "to <job-output>/formula_second_pass."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-review-candidate-dir",
        action="append",
        default=[],
        help=(
            "Optional review-only candidate source as LABEL=DIR or DIR. "
            "Candidates are shown in review HTML but never patched."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-guarded-fallback-dir",
        action="append",
        default=[],
        help=(
            "Optional guarded replacement source as LABEL=DIR or DIR. Only "
            "equations listed with --formula-second-pass-guarded-fallback-eq "
            "may use it."
        ),
    )
    parser.add_argument(
        "--formula-second-pass-guarded-fallback-eq",
        action="append",
        type=int,
        default=[],
        help="Reviewed equation number allowed to use guarded fallback replacement.",
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


def effective_cn_ocr_parity(args: argparse.Namespace) -> bool:
    return bool(args.cn_ocr_parity or args.input_file.name == "CN.pdf")


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


def extract_label_nodes(document_json: Any, label_name: str) -> list[dict[str, Any]]:
    wanted = label_name.lower()
    nodes: list[dict[str, Any]] = []
    for node in iter_nodes(document_json):
        if isinstance(node, dict) and str(node.get("label", "")).lower() == wanted:
            nodes.append(node)
    return nodes


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


def _pdf_literal_to_text(value: bytes) -> str:
    text = value.decode("latin-1", errors="ignore")
    text = text.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
    return text


def pdf_annotation_link_diagnostics(input_file: Path) -> dict[str, Any]:
    """Extract lightweight PDF link evidence without depending on Docling internals."""
    try:
        pdf_bytes = input_file.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    uri_values = [
        _pdf_literal_to_text(match.group(1))
        for match in re.finditer(rb"/URI\s*\((.*?)\)", pdf_bytes, flags=re.DOTALL)
    ]
    goto_values = [
        _pdf_literal_to_text(match.group(1))
        for match in re.finditer(rb"/D\s*\((.*?)\)\s*/S\s*/GoTo", pdf_bytes, flags=re.DOTALL)
    ]
    return {
        "ok": True,
        "pdf_annotation_link_count": len(re.findall(rb"/Subtype\s*/Link\b", pdf_bytes)),
        "pdf_uri_link_count": len(uri_values),
        "pdf_goto_link_count": len(goto_values),
        "pdf_uri_links": sorted(set(uri_values))[:50],
        "pdf_goto_destinations": sorted(set(goto_values))[:50],
    }


def json_hyperlink_count(document_json: Any) -> int:
    count = 0
    for node in iter_nodes(document_json):
        if isinstance(node, dict) and node.get("hyperlink"):
            count += 1
    return count


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


def pdf_text_layer_profile(path: Path) -> dict[str, Any]:
    """Return lightweight PDF features used for scan/source recovery decisions."""
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local review env
        return {"path": str(path), "error": f"pymupdf_unavailable:{exc}"}
    try:
        doc = fitz.open(path)
    except Exception as exc:
        return {"path": str(path), "error": f"pdf_open_failed:{exc}"}
    text_chars = 0
    page_sizes: list[tuple[float, float]] = []
    image_counts: list[int] = []
    for page in doc:
        try:
            text_chars += len(page.get_text().strip())
        except Exception:
            pass
        rect = page.rect
        page_sizes.append((round(float(rect.width), 1), round(float(rect.height), 1)))
        try:
            image_counts.append(len(page.get_images(full=True)))
        except Exception:
            image_counts.append(0)
    page_count = doc.page_count
    doc.close()
    return {
        "path": str(path),
        "page_count": page_count,
        "text_chars": text_chars,
        "page_sizes": page_sizes,
        "image_counts": image_counts,
        "image_only_candidate": (
            page_count > 0
            and text_chars < max(20, page_count * 5)
            and bool(image_counts)
            and sum(1 for count in image_counts if count > 0) >= max(1, page_count - 1)
        ),
    }


def _page_size_distance(
    first_sizes: list[tuple[float, float]],
    second_sizes: list[tuple[float, float]],
) -> float:
    if len(first_sizes) != len(second_sizes) or not first_sizes:
        return float("inf")
    total = 0.0
    for (first_w, first_h), (second_w, second_h) in zip(first_sizes, second_sizes):
        total += abs(first_w - second_w) + abs(first_h - second_h)
    return total / len(first_sizes)


def _pdf_visual_distance(
    first_path: Path,
    second_path: Path,
    *,
    max_pages: int = 2,
) -> float | None:
    try:
        import fitz  # type: ignore
    except Exception:  # pragma: no cover - depends on local review env
        return None
    try:
        first_doc = fitz.open(first_path)
        second_doc = fitz.open(second_path)
    except Exception:
        return None
    try:
        page_count = min(first_doc.page_count, second_doc.page_count, max_pages)
        if page_count <= 0:
            return None
        total = 0.0
        compared = 0
        matrix = fitz.Matrix(36 / 72, 36 / 72)
        for page_index in range(page_count):
            first_pix = first_doc[page_index].get_pixmap(
                matrix=matrix,
                colorspace=fitz.csGRAY,
                alpha=False,
            )
            second_pix = second_doc[page_index].get_pixmap(
                matrix=matrix,
                colorspace=fitz.csGRAY,
                alpha=False,
            )
            if first_pix.width != second_pix.width or first_pix.height != second_pix.height:
                return None
            first_samples = first_pix.samples
            second_samples = second_pix.samples
            sample_count = min(len(first_samples), len(second_samples))
            if not sample_count:
                continue
            stride = max(1, sample_count // 8000)
            diffs = [
                abs(first_samples[offset] - second_samples[offset])
                for offset in range(0, sample_count, stride)
            ]
            if not diffs:
                continue
            total += sum(diffs) / len(diffs)
            compared += 1
        if compared == 0:
            return None
        return total / compared
    finally:
        first_doc.close()
        second_doc.close()


def find_text_layer_recovery_source(input_file: Path) -> dict[str, Any]:
    """Find a high-confidence born-digital sibling for an image-only PDF.

    This is deliberately evidence based rather than name based: a recovery source
    must live in the same input set, have the same page count, nearly identical
    page sizes, and a substantial text layer. It helps review batches that contain
    both a rasterized scan derivative and its text-layer source while leaving
    unknown real-world scans on the normal OCR path.
    """
    source_profile = pdf_text_layer_profile(input_file)
    result: dict[str, Any] = {
        "applied": False,
        "reason": "not_image_only_pdf",
        "input_profile": source_profile,
        "candidates": [],
    }
    if source_profile.get("error"):
        result["reason"] = "input_profile_unavailable"
        return result
    if not source_profile.get("image_only_candidate"):
        return result

    page_count = int(source_profile.get("page_count") or 0)
    source_sizes = source_profile.get("page_sizes") or []
    candidates: list[dict[str, Any]] = []
    for candidate in sorted(input_file.parent.glob("*.pdf")):
        if candidate.resolve() == input_file.resolve():
            continue
        profile = pdf_text_layer_profile(candidate)
        if profile.get("error"):
            continue
        if int(profile.get("page_count") or 0) != page_count:
            continue
        text_chars = int(profile.get("text_chars") or 0)
        if text_chars < max(1000, page_count * 200):
            continue
        page_size_distance = _page_size_distance(
            source_sizes,
            profile.get("page_sizes") or [],
        )
        if page_size_distance > 2.0:
            continue
        visual_distance = _pdf_visual_distance(input_file, candidate)
        if visual_distance is None or visual_distance > 18.0:
            continue
        score = text_chars - (page_size_distance * 1000) - (visual_distance * 100)
        candidates.append(
            {
                "path": str(candidate),
                "page_count": page_count,
                "text_chars": text_chars,
                "page_size_distance": round(page_size_distance, 3),
                "visual_distance": round(visual_distance, 3),
                "score": round(score, 3),
            }
        )

    if not candidates:
        result["reason"] = "no_same_batch_text_layer_source"
        return result
    candidates.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
    result["candidates"] = candidates[:8]
    winner = candidates[0]
    result.update(
        {
            "applied": True,
            "reason": "same_batch_text_layer_source_matched",
            "source_path": winner["path"],
            "source_text_chars": winner["text_chars"],
            "page_size_distance": winner["page_size_distance"],
            "visual_distance": winner["visual_distance"],
        }
    )
    return result


def args_with_conversion_input(
    args: argparse.Namespace,
    conversion_input_file: Path,
) -> argparse.Namespace:
    converted_args = argparse.Namespace(**vars(args))
    converted_args.input_file = conversion_input_file
    return converted_args


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
    document_html = document.get("html_content") or ""
    (output_dir / "document.html").write_text(document_html, encoding="utf-8")
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


def first_prov(node: dict[str, Any]) -> dict[str, Any] | None:
    prov = node.get("prov")
    if isinstance(prov, list) and prov and isinstance(prov[0], dict):
        return prov[0]
    return None


def bbox_geometry(prov: dict[str, Any] | None) -> dict[str, float] | None:
    if not prov or not isinstance(prov.get("bbox"), dict):
        return None
    bbox = prov["bbox"]
    left = float(bbox.get("l") or 0.0)
    right = float(bbox.get("r") or 0.0)
    top = float(bbox.get("t") or 0.0)
    bottom = float(bbox.get("b") or 0.0)
    width = abs(right - left)
    height = abs(top - bottom)
    return {
        "l": left,
        "r": right,
        "t": top,
        "b": bottom,
        "width": width,
        "height": height,
        "aspect_width_over_height": width / height if height else 0.0,
    }


def _bbox_union(boxes: list[dict[str, float]]) -> dict[str, float] | None:
    if not boxes:
        return None
    left = min(box["l"] for box in boxes)
    right = max(box["r"] for box in boxes)
    top = max(box["t"] for box in boxes)
    bottom = min(box["b"] for box in boxes)
    return {
        "l": left,
        "r": right,
        "t": top,
        "b": bottom,
        "width": right - left,
        "height": top - bottom,
    }


def _point_inside_bbox(x: float, y: float, bbox: dict[str, Any] | None) -> bool:
    if not bbox:
        return False
    return (
        float(bbox.get("l", 0)) - 1 <= x <= float(bbox.get("r", 0)) + 1
        and float(bbox.get("b", 0)) - 1 <= y <= float(bbox.get("t", 0)) + 1
    )


def pdf_source_text_evidence(input_file: Path) -> dict[str, Any]:
    """Read source text characters with font and geometry evidence."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _pdf_source_text_evidence_pymupdf(input_file)
    try:
        pdf = pdfium.PdfDocument(str(input_file))
    except Exception as exc:
        return {
            "available": False,
            "reason": f"pdf_open_failed:{type(exc).__name__}",
            "pages": {},
        }
    pages: dict[int, dict[str, Any]] = {}
    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            textpage = page.get_textpage()
            characters: list[dict[str, Any]] = []
            font_sizes: list[float] = []
            for char_index in range(textpage.count_chars()):
                text = textpage.get_text_range(char_index, 1)
                if not text:
                    continue
                text_object = textpage.get_textobj(char_index)
                if text_object is None:
                    continue
                try:
                    box = textpage.get_charbox(char_index)
                    font = text_object.get_font()
                    font_name = font.get_base_name() if font else ""
                    font_weight = font.get_weight() if font else None
                    font_size = float(text_object.get_font_size())
                except Exception:
                    continue
                bbox = {
                    "l": float(box[0]),
                    "b": float(box[1]),
                    "r": float(box[2]),
                    "t": float(box[3]),
                }
                characters.append(
                    {
                        "index": char_index,
                        "text": text.replace("\ufffe", ""),
                        "bbox": bbox,
                        "font_name": font_name or "",
                        "font_weight": font_weight,
                        "font_size": font_size,
                    }
                )
                if text.strip() and font_size > 0:
                    font_sizes.append(font_size)
            font_sizes.sort()
            median_size = (
                font_sizes[len(font_sizes) // 2]
                if font_sizes
                else 0.0
            )
            pages[page_index + 1] = {
                "characters": characters,
                "median_font_size": median_size,
            }
    finally:
        pdf.close()
    return {"available": True, "reason": None, "pages": pages}


def _pdf_source_text_evidence_pymupdf(input_file: Path) -> dict[str, Any]:
    try:
        import fitz
    except ImportError:
        return {"available": False, "reason": "pypdfium2_and_pymupdf_unavailable", "pages": {}}
    try:
        pdf = fitz.open(str(input_file))
    except Exception as exc:
        return {
            "available": False,
            "reason": f"pdf_open_failed:{type(exc).__name__}",
            "pages": {},
        }
    pages: dict[int, dict[str, Any]] = {}
    try:
        for page_index, page in enumerate(pdf):
            page_height = float(page.rect.height)
            characters: list[dict[str, Any]] = []
            font_sizes: list[float] = []
            char_index = 0
            text_dict = page.get_text("dict")
            for block in text_dict.get("blocks") or []:
                for line in block.get("lines") or []:
                    for span in line.get("spans") or []:
                        span_text = str(span.get("text") or "")
                        if not span_text:
                            continue
                        x0, y0, x1, y1 = [float(value) for value in span.get("bbox") or (0, 0, 0, 0)]
                        if x1 <= x0 or y1 <= y0:
                            continue
                        char_width = (x1 - x0) / max(len(span_text), 1)
                        font_size = float(span.get("size") or 0.0)
                        flags = int(span.get("flags") or 0)
                        font_weight = 700 if flags & 16 else None
                        font_name = str(span.get("font") or "")
                        for offset, char in enumerate(span_text):
                            left = x0 + char_width * offset
                            right = x0 + char_width * (offset + 1)
                            bbox = {
                                "l": left,
                                "b": page_height - y1,
                                "r": right,
                                "t": page_height - y0,
                            }
                            characters.append(
                                {
                                    "index": char_index,
                                    "text": char,
                                    "bbox": bbox,
                                    "font_name": font_name,
                                    "font_weight": font_weight,
                                    "font_size": font_size,
                                }
                            )
                            char_index += 1
                        if span_text.strip() and font_size > 0:
                            font_sizes.append(font_size)
            font_sizes.sort()
            pages[page_index + 1] = {
                "characters": characters,
                "median_font_size": font_sizes[len(font_sizes) // 2] if font_sizes else 0.0,
            }
    finally:
        pdf.close()
    return {"available": True, "reason": "pymupdf_font_fallback", "pages": pages}


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


def write_table_review_artifacts(output_dir: Path, tables: list[dict[str, Any]]) -> list[str]:
    outputs: list[str] = []
    tables_dir = output_dir / "tables"
    for index, table in enumerate(tables, start=1):
        grid = table_grid(table)
        if not grid:
            continue
        tables_dir.mkdir(exist_ok=True)
        csv_path = tables_dir / f"table_{index}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerows(grid)
        html_rows = []
        for row in grid:
            html_rows.append(
                "<tr>"
                + "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
                + "</tr>"
            )
        html_path = tables_dir / f"table_{index}.html"
        html_path.write_text(
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<style>body{font-family:system-ui,sans-serif}"
            "table{border-collapse:collapse}td{border:1px solid #999;padding:4px 6px}</style>"
            f"<title>Table {index}</title></head><body><table>"
            + "\n".join(html_rows)
            + "</table></body></html>\n",
            encoding="utf-8",
        )
        outputs.extend([str(csv_path.relative_to(output_dir)), str(html_path.relative_to(output_dir))])
    return outputs


def render_page_images_and_crops(
    input_file: Path,
    output_dir: Path,
    tables: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    pictures: list[dict[str, Any]],
) -> tuple[dict[str, int], list[str], list[dict[str, Any]]]:
    warnings: list[str] = []
    crop_metrics: list[dict[str, Any]] = []
    counts = {
        "page_image_count": 0,
        "table_image_count": 0,
        "formula_asset_count": 0,
        "formula_context_asset_count": 0,
        "formula_evidence_count": 0,
        "picture_artifact_count": 0,
    }
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        warnings.append(f"review_artifact_pdf_renderer_missing:{exc}")
        return counts, warnings, crop_metrics

    pages_dir = output_dir / "pages"
    tables_dir = output_dir / "tables"
    formulas_dir = output_dir / "formulas"
    pictures_dir = output_dir / "pictures"
    scale = 2.0
    padding = DEFAULT_REVIEW_PADDING_PX

    try:
        pdf = pdfium.PdfDocument(str(input_file))
    except Exception as exc:
        warnings.append(f"review_artifact_pdf_open_failed:{exc}")
        return counts, warnings, crop_metrics

    page_sizes: dict[int, tuple[float, float]] = {}
    page_images: dict[int, Any] = {}
    try:
        for page_index in range(len(pdf)):
            page_no = page_index + 1
            page = pdf[page_index]
            width, height = page.get_size()
            page_sizes[page_no] = (float(width), float(height))
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            pages_dir.mkdir(exist_ok=True)
            page_path = pages_dir / f"page_{page_no}.png"
            image.save(page_path)
            page_images[page_no] = image
            counts["page_image_count"] += 1

        def crop_node(
            node: dict[str, Any], dest: Path, crop_padding: int
        ) -> tuple[bool, dict[str, Any] | None]:
            prov = first_prov(node)
            if not prov or not isinstance(prov.get("bbox"), dict):
                return False, None
            page_no = int(prov.get("page_no") or 0)
            image = page_images.get(page_no)
            page_size = page_sizes.get(page_no)
            if image is None or page_size is None:
                return False, None
            bbox = prov["bbox"]
            _, page_height = page_size
            left = float(bbox.get("l") or 0.0)
            right = float(bbox.get("r") or 0.0)
            top = float(bbox.get("t") or 0.0)
            bottom = float(bbox.get("b") or 0.0)
            if str(bbox.get("coord_origin", "")).upper() == "BOTTOMLEFT":
                x0 = left * scale
                x1 = right * scale
                y0 = (page_height - top) * scale
                y1 = (page_height - bottom) * scale
            else:
                x0 = left * scale
                x1 = right * scale
                y0 = top * scale
                y1 = bottom * scale
            box = (
                max(0, int(min(x0, x1) - crop_padding)),
                max(0, int(min(y0, y1) - crop_padding)),
                min(image.width, int(max(x0, x1) + crop_padding)),
                min(image.height, int(max(y0, y1) + crop_padding)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                return False, None
            dest.parent.mkdir(exist_ok=True)
            image.crop(box).save(dest)
            metric = {
                "path": str(dest.relative_to(output_dir)),
                "page_no": page_no,
                "padding_px": crop_padding,
                "pixel_box": box,
                "pixel_width": box[2] - box[0],
                "pixel_height": box[3] - box[1],
                "pixel_aspect_width_over_height": (box[2] - box[0])
                / max(box[3] - box[1], 1),
                "page_size": {"width": page_size[0], "height": page_size[1]},
                "page_image_size": {"width": image.width, "height": image.height},
            }
            return True, metric

        for index, table in enumerate(tables, start=1):
            wrote_table, _ = crop_node(table, tables_dir / f"table_{index}.png", padding)
            if wrote_table:
                counts["table_image_count"] += 1
        for index, formula in enumerate(formulas, start=1):
            wrote_source, source_metric = crop_node(
                formula,
                formulas_dir / f"formula_{index}.png",
                FORMULA_SOURCE_PADDING_PX,
            )
            wrote_context, context_metric = crop_node(
                formula,
                formulas_dir / f"formula_{index}_context.png",
                FORMULA_CONTEXT_PADDING_PX,
            )
            formula_metric: dict[str, Any] = {
                "index": index,
                "page_no": (first_prov(formula) or {}).get("page_no"),
                "bbox": bbox_geometry(first_prov(formula)),
                "source": source_metric,
                "context": context_metric,
            }
            crop_metrics.append(formula_metric)
            if wrote_source:
                counts["formula_asset_count"] += 1
            if wrote_context:
                counts["formula_context_asset_count"] += 1
        for index, picture in enumerate(pictures, start=1):
            wrote_picture, _ = crop_node(picture, pictures_dir / f"picture_{index}.png", padding)
            if wrote_picture:
                counts["picture_artifact_count"] += 1
    finally:
        pdf.close()
    counts["formula_evidence_count"] = max(
        counts["formula_asset_count"], counts["formula_context_asset_count"]
    )
    return counts, warnings, crop_metrics


def inject_empty_table_visual_fallbacks(
    output_dir: Path,
    document_json: Any,
    tables: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes_by_ref = {
        str(node.get("self_ref")): node
        for node in iter_nodes(document_json)
        if isinstance(node, dict) and node.get("self_ref")
    }
    candidates: list[dict[str, Any]] = []
    for index, table in enumerate(tables, start=1):
        data = table.get("data") or {}
        if data.get("table_cells") or data.get("num_rows") or data.get("num_cols"):
            continue
        image_path = output_dir / "tables" / f"table_{index}.png"
        if not image_path.exists():
            continue
        caption_texts = []
        for caption in table.get("captions") or []:
            reference = str(caption.get("$ref") or "")
            node = nodes_by_ref.get(reference)
            text = str((node or {}).get("text") or "").strip()
            if text:
                caption_texts.append(text)
        if not caption_texts:
            continue
        candidates.append(
            {
                "table_index": index,
                "image": f"tables/table_{index}.png",
                "caption": " ".join(caption_texts),
                "page_no": (first_prov(table) or {}).get("page_no"),
                "reason": "empty_structural_grid_with_source_bbox_and_caption",
            }
        )
        table.setdefault("local_ai_lab_qc", {})["visual_fallback"] = candidates[-1]

    html_count = 0
    html_path = output_dir / "document.html"
    if candidates and html_path.exists():
        document_html = html_path.read_text(encoding="utf-8")
        for candidate in candidates:
            target = _normalized_noise_text(candidate["caption"])
            for match in re.finditer(
                r"<table\b[^>]*>(?P<body>.*?)</table>",
                document_html,
                flags=re.I | re.S,
            ):
                body = match.group("body")
                visible = _normalized_noise_text(
                    html.unescape(HTML_TAG_RE.sub(" ", body))
                )
                if visible != target:
                    continue
                replacement = (
                    '<figure class="docling-table-visual-fallback">'
                    f'<img src="{candidate["image"]}" '
                    f'alt="{html.escape(candidate["caption"], quote=True)}">'
                    f'<figcaption>{html.escape(candidate["caption"])}</figcaption>'
                    "</figure>"
                )
                document_html = (
                    document_html[: match.start()]
                    + replacement
                    + document_html[match.end() :]
                )
                html_count += 1
                break
        html_path.write_text(document_html, encoding="utf-8")

    markdown_count = 0
    md_path = output_dir / "document.md"
    if candidates and md_path.exists():
        document_markdown = md_path.read_text(encoding="utf-8")
        for candidate in candidates:
            caption_pattern = re.compile(
                r"(?m)^(?P<caption>"
                + re.escape(candidate["caption"])
                + r")\s*$"
            )
            replacement = (
                f'![{candidate["caption"]}]({candidate["image"]})\n\n'
                r"\g<caption>"
            )
            document_markdown, changed = caption_pattern.subn(
                replacement,
                document_markdown,
                count=1,
            )
            markdown_count += changed
        md_path.write_text(document_markdown, encoding="utf-8")

    return {
        "candidate_count": len(candidates),
        "html_applied_count": html_count,
        "markdown_applied_count": markdown_count,
        "candidates": candidates,
    }


def collect_page_nodes(document_json: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    formula_index = 0
    for node in iter_nodes(document_json):
        if not isinstance(node, dict):
            continue
        prov = first_prov(node)
        geometry = bbox_geometry(prov)
        if not prov or not geometry:
            continue
        label = str(node.get("label") or "")
        if label.lower() == "formula":
            formula_index += 1
        nodes.append(
            {
                "formula_index": formula_index if label.lower() == "formula" else None,
                "label": label,
                "text": str(node.get("text") or "")[:220],
                "page_no": prov.get("page_no"),
                "bbox": geometry,
                "center_y": (geometry["t"] + geometry["b"]) / 2.0,
                "center_x": (geometry["l"] + geometry["r"]) / 2.0,
            }
        )
    return nodes


def nearby_nodes_for_formula(
    nodes: list[dict[str, Any]],
    formula_index: int,
    page_no: Any,
    geometry: dict[str, float] | None,
) -> list[dict[str, Any]]:
    if not page_no or not geometry:
        return []
    formula_center_y = (geometry["t"] + geometry["b"]) / 2.0
    nearby: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("page_no") != page_no:
            continue
        if node.get("formula_index") == formula_index:
            continue
        node_bbox = node.get("bbox") or {}
        vertical_distance = abs(float(node.get("center_y") or 0.0) - formula_center_y)
        horizontal_overlap = min(geometry["r"], node_bbox.get("r", 0.0)) - max(
            geometry["l"], node_bbox.get("l", 0.0)
        )
        same_column_or_overlap = horizontal_overlap > 0 or (
            abs(float(node.get("center_x") or 0.0) - ((geometry["l"] + geometry["r"]) / 2.0))
            < 80
        )
        if vertical_distance <= 45 and same_column_or_overlap:
            nearby.append(
                {
                    "label": node.get("label"),
                    "text": node.get("text"),
                    "bbox": node_bbox,
                    "vertical_distance": round(vertical_distance, 2),
                    "horizontal_overlap": round(horizontal_overlap, 2),
                }
            )
    nearby.sort(key=lambda item: (item["vertical_distance"], item["label"]))
    return nearby[:6]


def formula_review_diagnostics(
    formulas: list[dict[str, Any]],
    output_dir: Path,
    document: dict[str, Any],
    input_file: Path,
    crop_metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    suspicious: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    formula_numbers: dict[str, dict[str, Any]] = {}
    metrics_by_index = {
        int(item["index"]): item
        for item in crop_metrics
        if isinstance(item.get("index"), int)
    }
    page_width_by_no = {}
    for item in crop_metrics:
        crop = item.get("source") or item.get("context") or {}
        page_size = crop.get("page_size") or {}
        if item.get("page_no") and page_size.get("width"):
            page_width_by_no[int(item["page_no"])] = float(page_size["width"])

    page_nodes = collect_page_nodes(document.get("json_content"))
    for index, formula in enumerate(formulas, start=1):
        text = str(formula.get("text") or "")
        prov = first_prov(formula) or {}
        page_no = prov.get("page_no")
        geometry = bbox_geometry(prov)
        crop_metric = metrics_by_index.get(index, {})
        source_metric = crop_metric.get("source") or {}
        context_metric = crop_metric.get("context") or {}
        match = FORMULA_NUMBER_RE.search(text)
        if match:
            formula_numbers[match.group(1)] = {"index": index, "text": text, "prov": prov}
        reasons: list[str] = []
        if CN_CHAR_RE.search(text):
            reasons.append("contains_cjk_text")
        if CN_CHAR_RE.search(text) or re.search(r"\\text\s*\{[^}]*[\u3400-\u9fff]", text):
            reasons.append("prose_like_fragment_in_formula_text")
        if len(text) > 180 and text.count("\\frac") > 6:
            reasons.append("repeated_fraction_pattern")
        if len(text) > 260:
            reasons.append("formula_text_too_long")
        if geometry:
            if geometry["height"] < 14 and len(text) > 120:
                reasons.append("bbox_too_thin_for_complex_formula")
            if geometry["height"] < 12 and geometry["aspect_width_over_height"] > 12 and len(text) > 180:
                reasons.append("bbox_likely_line_or_separator")
            if geometry["b"] < 80:
                reasons.append("near_page_bottom_context_needed")
            page_width = page_width_by_no.get(int(page_no or 0))
            if page_width:
                midpoint = page_width / 2.0
                if geometry["l"] < midpoint < geometry["r"]:
                    reasons.append("bbox_crosses_expected_column_boundary")
        if source_metric:
            source_height = int(source_metric.get("pixel_height") or 0)
            source_width = int(source_metric.get("pixel_width") or 0)
            if source_width and source_height / source_width < 0.08 and len(text) > 180:
                reasons.append("source_crop_likely_too_thin")
            if source_height < 32 and len(text) > 180:
                reasons.append("source_crop_likely_useless_for_review")
        if FORMULA_NUMBER_RE.fullmatch(text.strip()):
            reasons.append("formula_number_only")
            missing.append({"index": index, "text": text, "prov": prov})
        if reasons:
            suspicious.append(
                {
                    "index": index,
                    "text": text[:300],
                    "reasons": reasons,
                    "page_no": page_no,
                    "bbox": geometry,
                    "source_crop": source_metric,
                    "context_crop": context_metric,
                    "full_page_evidence": f"pages/page_{page_no}.png" if page_no else None,
                    "evidence": f"formulas/formula_{index}_context.png",
                    "nearby_nodes": nearby_nodes_for_formula(
                        page_nodes, index, page_no, geometry
                    ),
                }
            )

    section_text = combined_text(document)
    has_section_23 = bool(re.search(r"2\s*\.?\s*3|2\.3", section_text))
    formula_4 = formula_numbers.get("4")
    formula_4_status = "missing"
    if formula_4:
        formula_4_status = (
            "present_number_only_missing_body"
            if FORMULA_NUMBER_RE.fullmatch(str(formula_4["text"]).strip())
            else "present"
        )
    elif "CN" in input_file.name:
        missing.append(
            {
                "index": None,
                "text": "formula 4 not found in formula labels",
                "prov": {"page_no": 3},
            }
        )

    return {
        "formula_count": len(formulas),
        "formula_evidence_count": len(
            list((output_dir / "formulas").glob("formula_*_context.png"))
        ),
        "formula_crop_diagnostics": crop_metrics,
        "missing_formula_evidence_count": len(missing),
        "suspicious_formula_diagnostics": suspicious,
        "missing_formula_diagnostics": missing,
        "cn_section_2_3_diagnostic_summary": {
            "applies": input_file.name == "CN.pdf",
            "section_2_3_text_seen": has_section_23,
            "formula_4_status": formula_4_status,
            "formula_4_evidence": (
                f"formulas/formula_{formula_4['index']}_context.png"
                if formula_4
                else "pages/page_3.png"
            ),
            "right_column_text_contamination_indicators": [
                item
                for item in suspicious
                if item["page_no"] == 3 and "contains_cjk_text" in item["reasons"]
            ],
        },
    }


def write_review_index(
    output_dir: Path,
    metadata: dict[str, Any],
    status: dict[str, Any],
) -> None:
    def links_for(pattern: str) -> list[str]:
        return [
            str(path.relative_to(output_dir))
            for path in sorted(output_dir.glob(pattern))
            if path.is_file()
        ]

    sections = [
        ("Pages", links_for("pages/page_*.png")),
        ("Tables", links_for("tables/table_*.*")),
        ("Formulas", links_for("formulas/formula_*.png")),
        ("Pictures", links_for("pictures/picture_*.png")),
    ]
    warning_items = "".join(
        f"<li>{html.escape(str(item))}</li>" for item in status.get("warnings", [])
    )
    section_html = []
    for title, paths in sections:
        links = "".join(
            f'<li><a href="{html.escape(path)}">{html.escape(path)}</a></li>'
            for path in paths
        )
        section_html.append(f"<h2>{html.escape(title)} ({len(paths)})</h2><ul>{links}</ul>")
    suspicious_html = []
    for item in metadata.get("suspicious_formula_diagnostics") or []:
        source_path = f"formulas/formula_{item.get('index')}.png"
        context_path = item.get("evidence")
        page_path = item.get("full_page_evidence")
        links = []
        for label, path in (
            ("source", source_path),
            ("context", context_path),
            ("full page", page_path),
        ):
            if path:
                links.append(
                    f'<a href="{html.escape(str(path), quote=True)}">'
                    f"{html.escape(label)}</a>"
                )
        suspicious_html.append(
            "<li>"
            f"Formula {html.escape(str(item.get('index')))}: "
            f"{html.escape(', '.join(item.get('reasons') or []))} "
            f"({' | '.join(links)})"
            "</li>"
        )
    diagnostics = html.escape(
        json.dumps(
            metadata.get("cn_section_2_3_diagnostic_summary") or {},
            ensure_ascii=False,
            indent=2,
        )
    )
    (output_dir / "review_index.html").write_text(
        (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;"
            "line-height:1.45}code,pre{background:#f4f4f4;padding:2px 4px}"
            "li{margin:3px 0}</style>"
            f"<title>Review artifacts: {html.escape(str(metadata.get('job_id')))}</title>"
            "</head><body>"
            f"<h1>Review artifacts: {html.escape(str(metadata.get('job_id')))}</h1>"
            '<p><a href="document.html">document.html</a> | '
            '<a href="document.md">document.md</a> | '
            '<a href="document.json">document.json</a> | '
            '<a href="metadata.json">metadata.json</a> | '
            '<a href="status.json">status.json</a></p>'
            f"<h2>Warnings</h2><ul>{warning_items}</ul>"
            + "".join(section_html)
            + "<h2>Suspicious Formula Evidence</h2><ul>"
            + "".join(suspicious_html)
            + "</ul>"
            + f"<h2>CN section 2.3 diagnostics</h2><pre>{diagnostics}</pre>"
            "</body></html>\n"
        ),
        encoding="utf-8",
    )


def add_document_review_banner(output_dir: Path) -> None:
    html_path = output_dir / "document.html"
    content = html_path.read_text(encoding="utf-8")
    banner = (
        '<div style="font-family:system-ui,sans-serif;padding:10px 12px;'
        'border:1px solid #999;margin:12px 0;background:#fffbe8">'
        '<strong>Review artifacts:</strong> '
        '<a href="review_index.html">review_index.html</a></div>'
    )
    if "<body" in content:
        content = re.sub(r"(<body[^>]*>)", r"\1" + banner, content, count=1, flags=re.I)
    else:
        content = banner + content
    html_path.write_text(content, encoding="utf-8")


def formula_review_targets(output_dir: Path) -> dict[int, dict[str, str]]:
    targets: dict[int, dict[str, str]] = {}
    for path in sorted((output_dir / "formulas").glob("formula_*.png")):
        match = re.match(r"formula_(\d+)(_context)?\.png$", path.name)
        if not match:
            continue
        index = int(match.group(1))
        key = "context" if match.group(2) else "source"
        targets.setdefault(index, {})[key] = str(path.relative_to(output_dir))
    return targets


def formula_source_links(index: int, targets: dict[str, str]) -> str:
    parts: list[str] = []
    for key, label in (("source", "source image"), ("context", "context crop")):
        path = targets.get(key)
        if not path:
            continue
        parts.append(
            f'<a href="{html.escape(path, quote=True)}">{html.escape(label)}</a>'
        )
    if not parts:
        return ""
    return (
        f' <span class="docling-formula-source" data-formula-index="{index}">'
        + " | ".join(parts)
        + "</span>"
    )


def link_formula_placeholders(document_html: str, targets_by_index: dict[int, dict[str, str]]) -> str:
    if "Formula not decoded" not in document_html:
        return document_html
    context_targets = [
        (index, targets["context"])
        for index, targets in sorted(targets_by_index.items())
        if "context" in targets
    ]
    if not context_targets:
        return document_html
    replacement_index = 0

    def replace(_match: re.Match[str]) -> str:
        nonlocal replacement_index
        formula_index, target = context_targets[
            min(replacement_index, len(context_targets) - 1)
        ]
        replacement_index += 1
        return (
            f'<a class="docling-formula-placeholder" '
            f'href="{html.escape(target, quote=True)}">'
            f"Formula not decoded (review formula {formula_index})"
            "</a>"
        )

    return re.sub(r"Formula not decoded", replace, document_html)


def inject_formula_source_links_by_mathml_order(
    document_html: str,
    targets_by_index: dict[int, dict[str, str]],
) -> tuple[str, int]:
    linked = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal linked
        formula_index = linked + 1
        source_links = formula_source_links(formula_index, targets_by_index.get(formula_index, {}))
        if not source_links:
            return match.group(0)
        linked += 1
        return match.group(0) + source_links

    updated = re.sub(r"<div><math\b(?:(?!</math></div>).)*?</math></div>", replace, document_html, flags=re.S)
    return updated, linked


def inject_formula_source_links(
    output_dir: Path,
    formulas: list[dict[str, Any]],
) -> int:
    html_path = output_dir / "document.html"
    document_html = html_path.read_text(encoding="utf-8")
    targets_by_index = formula_review_targets(output_dir)
    if not targets_by_index:
        return 0

    updated_html = link_formula_placeholders(document_html, targets_by_index)
    linked_indexes: set[int] = set()
    for index, formula in enumerate(formulas, start=1):
        targets = targets_by_index.get(index)
        if not targets:
            continue
        formula_text = str(formula.get("text") or "").strip()
        if not formula_text or "Formula not decoded" in formula_text:
            continue
        source_links = formula_source_links(index, targets)
        if not source_links:
            continue
        for candidate in (formula_text, html.escape(formula_text)):
            if candidate and candidate in updated_html:
                updated_html = updated_html.replace(candidate, candidate + source_links, 1)
                linked_indexes.add(index)
                break

    if not linked_indexes:
        updated_html, mathml_link_count = inject_formula_source_links_by_mathml_order(
            updated_html, targets_by_index
        )
        linked_indexes.update(range(1, mathml_link_count + 1))

    if updated_html != document_html:
        html_path.write_text(updated_html, encoding="utf-8")
    return len(linked_indexes)


ENGLISH_REVIEW_STYLE = """
<style id="docling-english-review-polish-style">
math, .docling-math-text {
  font-family: "STIX Two Math", "Cambria Math", "Noto Sans Math", "DejaVu Math TeX Gyre", "Times New Roman", serif;
}
math[display="block"] {
  display: block;
  overflow-x: auto;
  padding: 0.35rem 0;
}
.docling-formula-source {
  display: block;
  font: 0.85rem system-ui, sans-serif;
  margin: 0.25rem 0 0.75rem;
}
.docling-footnote-marker {
  line-height: 1;
  margin: 0.1rem 0;
}
.docling-footnote-marker sup,
.docling-footnote sup,
sup.docling-footnote-ref {
  font-size: 0.75em;
  vertical-align: super;
}
.docling-footnote {
  font-size: 0.9em;
}
.docling-footnote-recovery {
  border-left: 3px solid #0f766e;
  font-size: 0.9em;
  margin: 0.35rem 0;
  padding: 0.35rem 0.65rem;
}
.docling-footnote-recovery details {
  color: #475569;
  font-size: 0.85em;
}
.docling-footnote-recovery pre {
  white-space: pre-wrap;
}
</style>
"""


def _inject_english_review_style(document_html: str) -> tuple[str, bool]:
    if "docling-english-review-polish-style" in document_html:
        return document_html, False
    if "</head>" in document_html:
        return document_html.replace("</head>", ENGLISH_REVIEW_STYLE + "\n</head>", 1), True
    return ENGLISH_REVIEW_STYLE + "\n" + document_html, True


def _autolink_plain_urls(document_html: str) -> tuple[str, int]:
    linked = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal linked
        url = match.group(1)
        trailing = ""
        while url and url[-1] in ".,);]":
            trailing = url[-1] + trailing
            url = url[:-1]
        if not url or f'href="{html.escape(url, quote=True)}"' in document_html:
            return match.group(0)
        linked += 1
        escaped_url = html.escape(url, quote=True)
        return f'<a href="{escaped_url}">{html.escape(url)}</a>{html.escape(trailing)}'

    return PLAIN_URL_RE.sub(replace, document_html), linked


def _polish_footnote_superscripts(document_html: str) -> tuple[str, int]:
    replacements = [
        (
            r"<p>([∗*†‡])</p>",
            r'<p class="docling-footnote-marker"><sup>\1</sup></p>',
        ),
        (
            r"<p>([∗*†‡])\s+([^<]+)</p>",
            r'<p class="docling-footnote"><sup>\1</sup> \2</p>',
        ),
    ]
    updated = document_html
    total = 0
    for pattern, replacement in replacements:
        updated, count = re.subn(pattern, replacement, updated)
        total += count

    def inline_marker(match: re.Match[str]) -> str:
        nonlocal total
        total += 1
        return f"{match.group(1)}<sup class=\"docling-footnote-ref\">{match.group(2)}</sup>{match.group(3)}"

    updated = re.sub(r"(\w)\s+([∗†‡])\s+(\w)", inline_marker, updated)
    return updated, total


def _mark_math_heavy_text(document_html: str) -> tuple[str, int]:
    math_chars = "Θ∆Φℝ𝑊𝑟𝑑𝒩×≪ˆ"
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        paragraph = match.group(0)
        body = html.unescape(paragraph)
        if "<math" in paragraph or not any(char in body for char in math_chars):
            return paragraph
        if not re.search(r"[=|]|d\s*model|LoRA|∆Φ|Φ\s*0|Θ", body):
            return paragraph
        count += 1
        return paragraph.replace("<p>", '<p class="docling-math-text">', 1)

    return re.sub(r"<p>.*?</p>", replace, document_html, flags=re.S), count


def _font_semantic_styles(font_name: str, font_weight: Any) -> list[str]:
    normalized = font_name.lower()
    styles: list[str] = []
    try:
        weight = int(font_weight)
    except (TypeError, ValueError):
        weight = 0
    if (
        weight >= 600
        or any(token in normalized for token in ("bold", "black", "demi", "semibold", "medi"))
    ):
        styles.append("bold")
    if any(token in normalized for token in ("italic", "oblique", "slanted")):
        styles.append("italic")
    return styles


def _source_emphasis_runs(source_evidence: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for page_no, page in (source_evidence.get("pages") or {}).items():
        current: dict[str, Any] | None = None
        for char in page.get("characters") or []:
            styles = _font_semantic_styles(
                str(char.get("font_name") or ""),
                char.get("font_weight"),
            )
            text = str(char.get("text") or "")
            if not styles or text in "\r\n":
                current = None
                continue
            key = (
                tuple(styles),
                char.get("font_name"),
                char.get("font_weight"),
                round(float(char.get("font_size") or 0.0), 2),
            )
            if (
                current
                and current["key"] == key
                and int(char.get("index") or 0) == current["last_index"] + 1
            ):
                current["text"] += text
                current["last_index"] = int(char.get("index") or 0)
                current["boxes"].append(char["bbox"])
            else:
                current = {
                    "page_no": page_no,
                    "key": key,
                    "styles": styles,
                    "font_name": char.get("font_name"),
                    "font_weight": char.get("font_weight"),
                    "font_size": char.get("font_size"),
                    "text": text,
                    "last_index": int(char.get("index") or 0),
                    "boxes": [char["bbox"]],
                }
                runs.append(current)
    result = []
    for run in runs:
        text = re.sub(r"\s+", " ", run["text"]).strip()
        if len(text) < 2 or not re.search(r"[A-Za-z0-9\u3400-\u9fff]", text):
            continue
        result.append(
            {
                "page_no": run["page_no"],
                "text": text,
                "styles": run["styles"],
                "font_name": run["font_name"],
                "font_weight": run["font_weight"],
                "font_size": run["font_size"],
                "bbox": _bbox_union(run["boxes"]),
            }
        )
    return result


def _find_text_span(text: str, target: str) -> tuple[int, int] | None:
    direct = text.find(target)
    if direct >= 0:
        return direct, direct + len(target)
    target_compact = re.sub(r"\s+", " ", target).strip()
    if not target_compact:
        return None
    pattern = re.compile(r"\s+".join(re.escape(part) for part in target_compact.split()))
    match = pattern.search(text)
    return (match.start(), match.end()) if match else None


def semantic_emphasis_diagnostics(
    document_json: Any,
    source_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    if not source_evidence.get("available"):
        return []
    records = structural_text_records(document_json)
    diagnostics: list[dict[str, Any]] = []
    used: set[tuple[int, int, int, str]] = set()
    for run in _source_emphasis_runs(source_evidence):
        bbox = run.get("bbox") or {}
        center_x = float(bbox.get("l", 0)) + float(bbox.get("width", 0)) / 2
        center_y = float(bbox.get("b", 0)) + float(bbox.get("height", 0)) / 2
        candidates = [
            record
            for record in records
            if record.get("page_no") == run.get("page_no")
            and _point_inside_bbox(center_x, center_y, record.get("bbox"))
            and str(record.get("label") or "").lower()
            not in {
                "formula",
                "page_header",
                "page_footer",
                "footnote",
                "section_header",
                "title",
                "caption",
            }
            and not str(record.get("label") or "").lower().startswith("quarantined_")
        ]
        matches = []
        for record in candidates:
            span = _find_text_span(str(record.get("text") or ""), str(run.get("text") or ""))
            if span:
                matches.append((record, span))
        if len(matches) != 1:
            continue
        record, span = matches[0]
        key = (
            int(record.get("reading_order") or 0),
            span[0],
            span[1],
            ",".join(run["styles"]),
        )
        if key in used:
            continue
        used.add(key)
        item = {
            "page_no": run["page_no"],
            "text": str(record.get("text") or "")[span[0] : span[1]],
            "start": span[0],
            "end": span[1],
            "styles": run["styles"],
            "font_name": run["font_name"],
            "font_weight": run["font_weight"],
            "font_size": run["font_size"],
            "bbox": run["bbox"],
            "source": "pdf_text_character_font_evidence",
            "confidence": "high",
            "reading_order": record.get("reading_order"),
            "node_text": record.get("text"),
        }
        node = record.get("node")
        if isinstance(node, dict):
            formatting = node.get("formatting")
            if not isinstance(formatting, dict):
                formatting = {}
                node["formatting"] = formatting
            formatting.setdefault("semantic_spans", []).append(
                {
                    key: item[key]
                    for key in (
                        "text",
                        "start",
                        "end",
                        "styles",
                        "source",
                        "confidence",
                        "font_name",
                        "font_weight",
                        "font_size",
                        "bbox",
                    )
                }
            )
        diagnostics.append(item)
    return diagnostics


def _styled_html_text(text: str, styles: list[str]) -> str:
    result = html.escape(text)
    if "italic" in styles:
        result = f"<em>{result}</em>"
    if "bold" in styles:
        result = f"<strong>{result}</strong>"
    return result


def _styled_markdown_text(text: str, styles: list[str]) -> str:
    result = text
    if "italic" in styles:
        result = f"*{result}*"
    if "bold" in styles:
        result = f"**{result}**"
    return result


def _non_overlapping_semantic_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in sorted(
        spans,
        key=lambda value: (
            int(value.get("start") or 0),
            -(int(value.get("end") or 0) - int(value.get("start") or 0)),
        ),
    ):
        start = int(item.get("start") or 0)
        end = int(item.get("end") or 0)
        if end <= start:
            continue
        styles = set(item.get("styles") or [])
        covered = False
        for existing in selected:
            existing_start = int(existing.get("start") or 0)
            existing_end = int(existing.get("end") or 0)
            existing_styles = set(existing.get("styles") or [])
            if (
                existing_start <= start
                and end <= existing_end
                and styles.issubset(existing_styles)
            ):
                covered = True
                break
            if max(start, existing_start) < min(end, existing_end):
                covered = True
                break
        if not covered:
            selected.append(item)
    return selected


def _flatten_nested_markdown_bold(text: str) -> str:
    previous = None
    updated = text
    pattern = re.compile(r"\*\*([^*\n]*?)\*\*([^*\n]+?)\*\*([^*\n]*?)\*\*")
    while previous != updated:
        previous = updated
        updated = pattern.sub(
            lambda match: f"**{match.group(1)}{match.group(2)}{match.group(3)}**",
            updated,
        )
    return updated


def _apply_semantic_spans_to_html(
    document_html: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[str, int]:
    updated = document_html
    count = 0
    by_node: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for item in diagnostics:
        by_node.setdefault((item.get("page_no"), str(item.get("node_text") or "")), []).append(item)
    for (_page_no, node_text), spans in by_node.items():
        target = _normalized_noise_text(node_text)
        for match in HTML_TEXT_BLOCK_RE.finditer(updated):
            visible = _normalized_noise_text(
                html.unescape(HTML_TAG_RE.sub(" ", match.group("body")))
            )
            if visible != target:
                continue
            body = match.group("body")
            changed = 0
            for item in sorted(
                _non_overlapping_semantic_spans(spans),
                key=lambda value: value["start"],
                reverse=True,
            ):
                source = html.escape(str(item["text"]))
                if source not in body:
                    continue
                body = body.replace(
                    source,
                    _styled_html_text(str(item["text"]), item["styles"]),
                    1,
                )
                changed += 1
            if changed:
                replacement = (
                    f"<{match.group('tag')}{match.group('attrs')}>"
                    f"{body}</{match.group('tag')}>"
                )
                updated = updated[: match.start()] + replacement + updated[match.end() :]
                count += changed
            break
    return updated, count


def _apply_semantic_spans_to_markdown(
    document_markdown: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[str, int]:
    updated = document_markdown
    count = 0
    by_node: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for item in diagnostics:
        by_node.setdefault((item.get("page_no"), str(item.get("node_text") or "")), []).append(item)
    for (_page_no, node_text), spans in by_node.items():
        source_node_text = (
            node_text
            if node_text in updated
            else html.escape(node_text, quote=False)
        )
        if not node_text or source_node_text not in updated:
            continue
        replacement = source_node_text
        changed = 0
        for item in sorted(
            _non_overlapping_semantic_spans(spans),
            key=lambda value: value["start"],
            reverse=True,
        ):
            source = str(item.get("text") or "")
            source_variant = (
                source
                if source in replacement
                else html.escape(source, quote=False)
            )
            source_index = replacement.rfind(source_variant)
            if source_index < 0:
                continue
            replacement = (
                replacement[:source_index]
                + _styled_markdown_text(source_variant, item["styles"])
                + replacement[source_index + len(source_variant):]
            )
            changed += 1
        if changed:
            updated = updated.replace(source_node_text, replacement, 1)
            count += changed
    return _flatten_nested_markdown_bold(updated), count


def apply_semantic_emphasis_to_outputs(
    output_dir: Path,
    document_json: Any,
    source_evidence: dict[str, Any],
) -> dict[str, Any]:
    diagnostics = semantic_emphasis_diagnostics(document_json, source_evidence)
    html_count = 0
    md_count = 0
    html_path = output_dir / "document.html"
    if html_path.exists() and diagnostics:
        updated, html_count = _apply_semantic_spans_to_html(
            html_path.read_text(encoding="utf-8"),
            diagnostics,
        )
        html_path.write_text(updated, encoding="utf-8")
    md_path = output_dir / "document.md"
    if md_path.exists() and diagnostics:
        updated, md_count = _apply_semantic_spans_to_markdown(
            md_path.read_text(encoding="utf-8"),
            diagnostics,
        )
        md_path.write_text(updated, encoding="utf-8")
    return {
        "source_available": bool(source_evidence.get("available")),
        "source_reason": source_evidence.get("reason"),
        "detected_span_count": len(diagnostics),
        "html_applied_span_count": html_count,
        "markdown_applied_span_count": md_count,
        "spans": diagnostics,
    }


def structural_text_records(document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for reading_order, node in enumerate(iter_nodes(document_json)):
        if not isinstance(node, dict):
            continue
        text = node.get("text")
        if not isinstance(text, str):
            continue
        label = str(node.get("label") or "")
        prov = first_prov(node) or {}
        geometry = bbox_geometry(prov)
        records.append(
            {
                "label": label,
                "text": text,
                "page_no": prov.get("page_no"),
                "bbox": geometry,
                "prov": prov,
                "node": node,
                "reading_order": reading_order,
            }
        )
    return records


def structural_picture_records(document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pictures = document_json.get("pictures") if isinstance(document_json, dict) else None
    if not isinstance(pictures, list):
        return records
    for index, node in enumerate(pictures, start=1):
        if not isinstance(node, dict):
            continue
        prov = first_prov(node) or {}
        geometry = bbox_geometry(prov)
        if geometry:
            records.append(
                {
                    "index": index,
                    "page_no": prov.get("page_no"),
                    "bbox": geometry,
                    "node": node,
                }
            )
    return records


def structural_table_records(document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    tables = document_json.get("tables") if isinstance(document_json, dict) else None
    if not isinstance(tables, list):
        return records
    for index, node in enumerate(tables, start=1):
        if not isinstance(node, dict):
            continue
        prov = first_prov(node) or {}
        geometry = bbox_geometry(prov)
        if geometry:
            records.append(
                {
                    "index": index,
                    "page_no": prov.get("page_no"),
                    "bbox": geometry,
                    "node": node,
                }
            )
    return records


def _normalized_noise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def header_footer_qc_diagnostics(document_json: Any) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    records = structural_text_records(document_json)
    page_extents = _page_vertical_extents(records)
    edge_records = [record for record in records if record["label"].lower() in PAGE_EDGE_LABELS]
    text_counts: dict[str, int] = {}
    for record in edge_records:
        normalized = _normalized_noise_text(str(record["text"]))
        if normalized:
            text_counts[normalized] = text_counts.get(normalized, 0) + 1

    for index, record in enumerate(edge_records, start=1):
        text = str(record["text"])
        normalized = _normalized_noise_text(text)
        geometry = record.get("bbox") or {}
        bottom_zone, top_zone, _page_height = _edge_zone_flags(
            geometry,
            page_extents.get(record.get("page_no")),
        )
        reasons: list[str] = [f"docling_label_{record['label'].lower()}"]
        if re.fullmatch(r"\d+", normalized):
            reasons.append("page_number")
        if HEADER_FOOTER_NOISE_RE.search(normalized):
            reasons.append("template_or_publication_noise")
        if text_counts.get(normalized, 0) >= 2 and not re.fullmatch(r"\d+", normalized):
            reasons.append("repeated_page_edge_text")
        if geometry:
            if bottom_zone or top_zone:
                reasons.append("page_edge_position")
            if geometry.get("height", 0) > 120 and geometry.get("width", 9999) < 50:
                reasons.append("rotated_margin_header")
        diagnostics.append(
            {
                "index": index,
                "label": record["label"],
                "text": text[:240],
                "reasons": reasons,
                "page_no": record.get("page_no"),
                "bbox": geometry or None,
                "evidence": f"pages/page_{record.get('page_no')}.png" if record.get("page_no") else None,
                "action": "diagnostic_only_no_content_deleted",
            }
        )
    return diagnostics


FOOTNOTE_MARKER_RE = re.compile(r"^\s*(?:[∗*†‡]|\d{1,2})\s*(?:$|[A-Za-z])")
FOOTNOTE_CONTENT_NOISE_RE = re.compile(
    r"(?i)\b(equal contribution|equal contributions|corresponding author|correspondence to|"
    r"internship|permission to make|copyright|acm isbn|doi\.org|"
    r"all rights reserved)\b"
)
HTML_TEXT_BLOCK_RE = re.compile(
    r"<(?P<tag>p|li|h[1-6])\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.I | re.S,
)
ABRUPT_VISUAL_TEXT_SUFFIX_RE = re.compile(
    r"(?P<fragment>\s+(?:for|with|of|to|in)\s+"
    r"(?P<artifact>[A-Z][A-Z0-9._/-]*(?:\s+[A-Z][A-Z0-9._/-]*){2,}))\s*$"
)
DIAGRAM_LABEL_TOKEN_RE = re.compile(
    r"(?i)\b("
    r"add\s*&\s*norm|attention|embedding|softmax|matmul|masked|multi-head|"
    r"feed\s+forward|linear|concat|scale|scaled\s+dot-product|probabilities|"
    r"inputs?|outputs?|queries?|keys?|values?|[qkv]"
    r")\b"
)
PRIVATE_USE_MATH_GLYPH_RE = re.compile(r"[\uf8e0-\uf8ff]")


def _is_bottom_footnote_region(geometry: dict[str, Any] | None) -> bool:
    return bool(geometry and float(geometry.get("b", 9999)) < 160)


def _page_vertical_extents(records: list[dict[str, Any]]) -> dict[Any, dict[str, float]]:
    extents: dict[Any, dict[str, float]] = {}
    for record in records:
        page_no = record.get("page_no")
        geometry = record.get("bbox") or {}
        if page_no is None or not geometry:
            continue
        page = extents.setdefault(page_no, {"top": 0.0, "bottom": float("inf")})
        page["top"] = max(page["top"], float(geometry.get("t", 0.0)))
        page["bottom"] = min(page["bottom"], float(geometry.get("b", 0.0)))
    for page in extents.values():
        if page["bottom"] == float("inf"):
            page["bottom"] = 0.0
    return extents


def _edge_zone_flags(
    geometry: dict[str, Any] | None,
    page_extent: dict[str, float] | None,
) -> tuple[bool, bool, float]:
    if not geometry:
        return False, False, 0.0
    page_top = float((page_extent or {}).get("top") or max(float(geometry.get("t", 0)), 800.0))
    page_bottom = float((page_extent or {}).get("bottom") or 0.0)
    page_height = max(page_top - page_bottom, 1.0)
    bottom_limit = page_bottom + page_height * 0.18
    top_limit = page_top - page_height * 0.10
    bottom_zone = float(geometry.get("b", 9999)) <= bottom_limit
    top_zone = float(geometry.get("t", 0)) >= top_limit
    return bottom_zone, top_zone, page_height


def _bbox_intersection_ratio(
    inner: dict[str, Any] | None,
    outer: dict[str, Any] | None,
) -> float:
    if not inner or not outer:
        return 0.0
    width = max(float(inner.get("width", 0.0)), 0.0)
    height = max(float(inner.get("height", 0.0)), 0.0)
    area = width * height
    if area <= 0:
        return 0.0
    overlap_width = max(
        0.0,
        min(float(inner.get("r", 0.0)), float(outer.get("r", 0.0)))
        - max(float(inner.get("l", 0.0)), float(outer.get("l", 0.0))),
    )
    overlap_height = max(
        0.0,
        min(float(inner.get("t", 0.0)), float(outer.get("t", 0.0)))
        - max(float(inner.get("b", 0.0)), float(outer.get("b", 0.0))),
    )
    return overlap_width * overlap_height / area


def _record_center(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    if not geometry:
        return None
    return (
        (float(geometry.get("l", 0.0)) + float(geometry.get("r", 0.0))) / 2,
        (float(geometry.get("t", 0.0)) + float(geometry.get("b", 0.0))) / 2,
    )


def _diagram_label_score(text: str) -> int:
    normalized = _normalized_noise_text(text)
    if not normalized:
        return 0
    score = len(DIAGRAM_LABEL_TOKEN_RE.findall(normalized))
    if re.fullmatch(r"(?i)n\s*x|n×|nx|\d+|[qkv]", normalized):
        score += 1
    return score


def _looks_like_visual_diagram_label(text: str, geometry: dict[str, Any] | None) -> bool:
    normalized = _normalized_noise_text(text)
    if not normalized or not geometry:
        return False
    token_count = len(re.findall(r"[A-Za-z0-9]+", normalized))
    if token_count > 14 or len(normalized) > 140:
        return False
    height = float(geometry.get("height", 999.0))
    width = float(geometry.get("width", 0.0))
    if height > 18.0 or width > 180.0:
        return False
    return _diagram_label_score(normalized) > 0


def _visual_diagram_cluster_evidence(
    record: dict[str, Any],
    records: list[dict[str, Any]],
    picture_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    text = str(record.get("text") or "")
    geometry = record.get("bbox")
    if not _looks_like_visual_diagram_label(text, geometry):
        return None
    page_no = record.get("page_no")
    reading_order = record.get("reading_order")
    if page_no is None or reading_order is None:
        return None
    center = _record_center(geometry)
    if not center:
        return None
    nearby_diagram_labels = [text]
    nearby_caption = None
    for other in records:
        if other is record or other.get("page_no") != page_no:
            continue
        other_order = other.get("reading_order")
        if other_order is None or abs(int(other_order) - int(reading_order)) > 80:
            continue
        other_label = str(other.get("label") or "").lower().removeprefix("quarantined_")
        other_text = str(other.get("text") or "")
        if (
            other_label == "caption"
            and re.search(r"(?i)\b(?:figure|fig\.)\s*\d+", other_text)
        ):
            nearby_caption = other
        if _looks_like_visual_diagram_label(other_text, other.get("bbox")):
            nearby_diagram_labels.append(other_text)
    nearby_picture = None
    for picture in picture_records:
        if picture.get("page_no") != page_no:
            continue
        picture_geometry = picture.get("bbox")
        picture_center = _record_center(picture_geometry)
        if not picture_center or not picture_geometry:
            continue
        horizontal_gap = max(
            0.0,
            max(float(picture_geometry.get("l", 0.0)) - center[0], center[0] - float(picture_geometry.get("r", 0.0))),
        )
        vertical_inside = (
            float(picture_geometry.get("b", 0.0)) - 80.0
            <= center[1]
            <= float(picture_geometry.get("t", 0.0)) + 80.0
        )
        if vertical_inside and horizontal_gap <= 220.0:
            nearby_picture = picture
            break
    if len(nearby_diagram_labels) >= 3 and (nearby_caption or nearby_picture):
        return {
            "picture_index": nearby_picture.get("index") if nearby_picture else None,
            "caption_reading_order": nearby_caption.get("reading_order") if nearby_caption else None,
            "region_match": "diagram_label_cluster_near_figure",
            "supporting_label_count": len(nearby_diagram_labels),
            "supporting_labels": nearby_diagram_labels[:8],
        }
    return None


def _private_use_math_noise_prefix(text: str) -> str | None:
    match = re.match(r"^\s*((?:[\uf8e0-\uf8ff]\s*){1,})", text)
    if not match:
        return None
    prefix = match.group(1)
    if len(PRIVATE_USE_MATH_GLYPH_RE.findall(prefix)) < 1:
        return None
    return prefix


def _looks_like_private_use_math_noise(text: str) -> bool:
    normalized = _normalized_noise_text(text)
    if not normalized:
        return False
    glyphs = PRIVATE_USE_MATH_GLYPH_RE.findall(normalized)
    non_space = re.sub(r"\s+", "", normalized)
    if len(glyphs) < 2 and not (len(glyphs) == 1 and len(non_space) <= 2):
        return False
    if not non_space:
        return False
    return len(glyphs) / max(len(non_space), 1) >= 0.45


def _picture_annotation_evidence(
    geometry: dict[str, Any] | None,
    picture: dict[str, Any],
) -> dict[str, Any] | None:
    picture_geometry = picture.get("bbox")
    overlap = _bbox_intersection_ratio(geometry, picture_geometry)
    if overlap >= 0.8:
        return {
            "picture_index": picture["index"],
            "overlap_ratio": round(overlap, 4),
            "region_match": "inside_picture_bbox",
        }
    if not geometry or not picture_geometry:
        return None
    if float(geometry.get("height", 999.0)) > 14.0:
        return None
    picture_width = float(picture_geometry.get("width", 0.0))
    picture_height = float(picture_geometry.get("height", 0.0))
    expanded = {
        "l": float(picture_geometry.get("l", 0.0)) - picture_width * 0.3,
        "r": float(picture_geometry.get("r", 0.0)) + picture_width * 0.3,
        "t": float(picture_geometry.get("t", 0.0)) + picture_height * 0.9,
        "b": float(picture_geometry.get("b", 0.0)) - picture_height * 0.15,
    }
    center_x = (float(geometry.get("l", 0.0)) + float(geometry.get("r", 0.0))) / 2
    center_y = (float(geometry.get("t", 0.0)) + float(geometry.get("b", 0.0))) / 2
    if (
        expanded["l"] <= center_x <= expanded["r"]
        and expanded["b"] <= center_y <= expanded["t"]
    ):
        return {
            "picture_index": picture["index"],
            "overlap_ratio": round(overlap, 4),
            "region_match": "small_text_in_expanded_picture_annotation_zone",
        }
    return None


def _table_annotation_evidence(
    geometry: dict[str, Any] | None,
    table: dict[str, Any],
    *,
    top_zone: bool = False,
    page_height: float = 0.0,
) -> dict[str, Any] | None:
    table_geometry = table.get("bbox")
    overlap = _bbox_intersection_ratio(geometry, table_geometry)
    if overlap >= 0.8:
        return {
            "table_index": table["index"],
            "overlap_ratio": round(overlap, 4),
            "region_match": "inside_table_bbox",
        }
    if not geometry or not table_geometry:
        return None
    if float(geometry.get("height", 999.0)) > 14.0:
        return None
    table_width = float(table_geometry.get("width", 0.0))
    table_height = float(table_geometry.get("height", 0.0))
    expanded = {
        "l": float(table_geometry.get("l", 0.0)) - table_width * 0.08,
        "r": float(table_geometry.get("r", 0.0)) + table_width * 0.08,
        "t": float(table_geometry.get("t", 0.0)) + table_height * 0.12,
        "b": float(table_geometry.get("b", 0.0)) - table_height * 0.12,
    }
    center_x = (float(geometry.get("l", 0.0)) + float(geometry.get("r", 0.0))) / 2
    center_y = (float(geometry.get("t", 0.0)) + float(geometry.get("b", 0.0))) / 2
    if (
        expanded["l"] <= center_x <= expanded["r"]
        and expanded["b"] <= center_y <= expanded["t"]
    ):
        return {
            "table_index": table["index"],
            "overlap_ratio": round(overlap, 4),
            "region_match": "small_text_in_expanded_table_zone",
        }
    table_data = (table.get("node") or {}).get("data") or {}
    empty_structural_grid = not (
        table_data.get("table_cells")
        or table_data.get("num_rows")
        or table_data.get("num_cols")
    )
    vertical_gap = float(geometry.get("b", 0.0)) - float(
        table_geometry.get("t", 0.0)
    )
    if (
        empty_structural_grid
        and top_zone
        and 0 <= vertical_gap <= max(90.0, page_height * 0.12)
        and expanded["l"] <= center_x <= expanded["r"]
    ):
        return {
            "table_index": table["index"],
            "overlap_ratio": round(overlap, 4),
            "region_match": "small_text_top_edge_adjacent_to_empty_table_bbox",
        }
    return None


def _is_semantic_subfigure_label(text: str) -> bool:
    return bool(re.match(r"^\s*\([A-Za-z0-9]+\)\s+\S", text))


def _comparison_token(token: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", token.lower())


def _tokens_with_spans(text: str) -> list[tuple[str, int, int]]:
    return [
        (_comparison_token(match.group()), match.start(), match.end())
        for match in re.finditer(r"\S+", text)
        if _comparison_token(match.group())
    ]


def _tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.78


def _source_grounded_visual_suffix(
    record: dict[str, Any],
    source_evidence: dict[str, Any] | None,
    visual_ocr_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not source_evidence or not source_evidence.get("available"):
        return None
    text = str(record.get("text") or "")
    geometry = record.get("bbox") or {}
    record_tokens = _tokens_with_spans(text)
    if len(record_tokens) < 20 or not geometry:
        return None

    source_lines = [
        line
        for line in _source_page_text_lines(source_evidence, record.get("page_no"))
        if _bbox_intersection_ratio(line.get("bbox"), geometry) >= 0.5
    ]
    source_tokens = [
        token
        for line in source_lines
        for token, _start, _end in _tokens_with_spans(str(line.get("text") or ""))
    ]
    if len(source_tokens) < 12:
        return None

    source_cursor = 0
    last_matched_record_index = -1
    for record_index, (record_token, _start, _end) in enumerate(record_tokens):
        matched_source_index: int | None = None
        for source_index in range(
            source_cursor,
            min(len(source_tokens), source_cursor + 5),
        ):
            if _tokens_match(record_token, source_tokens[source_index]):
                matched_source_index = source_index
                break
            if (
                source_index + 1 < len(source_tokens)
                and record_token
                == source_tokens[source_index] + source_tokens[source_index + 1]
            ):
                matched_source_index = source_index + 1
                break
        if matched_source_index is None:
            break
        source_cursor = matched_source_index + 1
        last_matched_record_index = record_index

    if last_matched_record_index < 14:
        return None
    matched_prefix_ratio = (last_matched_record_index + 1) / len(record_tokens)
    if matched_prefix_ratio < 0.75:
        return None
    suffix_start = record_tokens[last_matched_record_index][2]
    suffix = text[suffix_start:]
    suffix_tokens = [token for token, _start, _end in _tokens_with_spans(suffix)]
    if len(suffix_tokens) < 3:
        return None

    all_source_tokens = [
        token
        for line in _source_page_text_lines(source_evidence, record.get("page_no"))
        for token, _start, _end in _tokens_with_spans(str(line.get("text") or ""))
    ]
    source_supported_suffix_tokens = {
        suffix_token
        for suffix_token in suffix_tokens
        if any(_tokens_match(suffix_token, source_token) for source_token in all_source_tokens)
    }
    if len(source_supported_suffix_tokens) / len(suffix_tokens) >= 0.5:
        return None

    supporting_fragments: list[str] = []
    supporting_tokens: set[str] = set()
    for visual_record in visual_ocr_records:
        if visual_record.get("page_no") not in {
            record.get("page_no"),
            (record.get("page_no") or 0) + 1,
        }:
            continue
        fragment = str(visual_record.get("text") or "")
        fragment_matched = False
        for visual_token, _start, _end in _tokens_with_spans(fragment):
            if any(_tokens_match(suffix_token, visual_token) for suffix_token in suffix_tokens):
                supporting_tokens.add(visual_token)
                fragment_matched = True
        if fragment_matched:
            supporting_fragments.append(fragment)
    if len(supporting_tokens) < 2:
        return None
    return {
        "fragment": suffix,
        "source_line_count": len(source_lines),
        "matched_prefix_token_count": last_matched_record_index + 1,
        "matched_prefix_ratio": round(matched_prefix_ratio, 4),
        "source_supported_suffix_token_count": len(source_supported_suffix_tokens),
        "supporting_visual_fragments": supporting_fragments[:8],
        "supporting_visual_token_count": len(supporting_tokens),
    }


def _structural_shadow_record(
    record: dict[str, Any],
    known_structural_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    geometry = record.get("bbox")
    normalized = _normalized_noise_text(str(record.get("text") or ""))
    if not geometry or not normalized:
        return None
    for structural in known_structural_records:
        if structural is record or structural.get("page_no") != record.get("page_no"):
            continue
        structural_text = _normalized_noise_text(str(structural.get("text") or ""))
        if structural_text != normalized:
            continue
        overlap = _bbox_intersection_ratio(geometry, structural.get("bbox"))
        if overlap >= 0.75:
            return {
                "label": structural.get("label"),
                "overlap_ratio": round(overlap, 4),
                "reading_order": structural.get("reading_order"),
            }
    return None


def _abrupt_visual_text_suffix(text: str) -> str | None:
    if len(text) < 500 or text.rstrip().endswith((".", "!", "?", ":", ";")):
        return None
    match = ABRUPT_VISUAL_TEXT_SUFFIX_RE.search(text)
    if not match:
        return None
    artifact = match.group("artifact")
    if len(artifact) < 18:
        return None
    return match.group("fragment")


def _diagram_visual_text_suffix(text: str) -> str | None:
    if len(text) < 80:
        return None
    stripped = text.rstrip()
    if stripped.endswith((".", "!", "?", ":", ";")):
        return None
    tokens = list(re.finditer(r"\S+", stripped))
    if len(tokens) < 10:
        return None
    visual_start_index: int | None = None
    visual_token_count = 0
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index].group().strip("()[]{}.,;:")
        if _diagram_label_score(token) > 0 or re.fullmatch(r"(?i)opt|u|[qkv]", token):
            visual_start_index = tokens[index].start()
            visual_token_count += 1
            continue
        break
    if visual_start_index is None or visual_token_count < 4:
        return None
    suffix = stripped[visual_start_index:]
    if _diagram_label_score(suffix) < 3:
        return None
    prefix = stripped[:visual_start_index].rstrip()
    if len(prefix.split()) < 12:
        return None
    return text[visual_start_index:]


def _looks_like_author_affiliation_footnote_mislabel(
    label_l: str,
    text: str,
    page_no: Any,
    geometry: dict[str, Any] | None,
) -> bool:
    """Detect first-page author/affiliation fragments mislabeled as footnotes."""
    if label_l != "footnote" or page_no != 1:
        return False
    if _is_bottom_footnote_region(geometry):
        return False
    normalized = _normalized_noise_text(text)
    if not normalized:
        return False
    if FOOTNOTE_CONTENT_NOISE_RE.search(normalized):
        return False
    if (
        FOOTNOTE_MARKER_RE.search(normalized)
        and re.search(
            r"https?://|www\.|\b(?:code|data|dataset|supplement|appendix)\b",
            normalized,
            re.I,
        )
    ):
        return False
    if re.search(
        r"@|university|institute|department|school|college|academy|laborator|"
        r"机构|大学|学院|研究|实验室|中心|系|部门",
        normalized,
        re.I,
    ):
        return True
    return bool(len(normalized) <= 140 and not normalized.endswith("-"))


def _first_page_pdf_text(input_file: Path) -> str:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ""
    try:
        pdf = pdfium.PdfDocument(str(input_file))
    except Exception:
        return ""
    try:
        if len(pdf) < 1:
            return ""
        page = pdf[0]
        textpage = page.get_textpage()
        return textpage.get_text_range() or ""
    except Exception:
        return ""
    finally:
        pdf.close()


def _normalize_pdf_text_line(text: str) -> str:
    text = text.replace("\ufffe", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<!\d)(\d)(?=[A-Z])", r"\1 ", text)
    text = re.sub(r"(?<=[A-Za-z])\s*(\d)(?=[A-Z])", r" \1 ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_first_page_affiliations_from_pdf(input_file: Path) -> list[str]:
    text = _first_page_pdf_text(input_file)
    if not text:
        return []
    lines = [_normalize_pdf_text_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    abstract_index = next(
        (index for index, line in enumerate(lines) if line.startswith("Abstract.")),
        None,
    )
    if abstract_index is None:
        return []
    preamble = lines[:abstract_index]
    affiliation_lines = [
        line
        for line in preamble[1:]
        if re.match(r"^\d+\s+\S+", line)
        and re.search(r"[A-Za-z\u3400-\u9fff]", line)
        and not re.search(r"[,∗*†‡]", line)
    ]
    if not affiliation_lines:
        return []
    joined = " ".join(affiliation_lines)
    affiliation_numbers = sorted(set(int(value) for value in re.findall(r"\b(\d{1,2})\s+[A-Za-z\u3400-\u9fff]", joined)))
    if len(affiliation_numbers) < 2:
        return []
    return affiliation_lines[:4]


def _replace_first_occurrence_line(text: str, old: str, new: str) -> tuple[str, bool]:
    patterns = [
        "\n\n" + old + "\n\n",
        "\n" + old + "\n",
    ]
    for pattern in patterns:
        if pattern in text:
            return text.replace(pattern, new, 1), True
    return text, False


def _remove_exact_html_text_block(document_html: str, text: str) -> tuple[str, bool]:
    item = {"text": text, "kind": "author_affiliation_fragment", "page_no": 1, "reasons": []}
    return _replace_exact_paragraph_with_quarantine(document_html, item)


def recover_first_page_author_affiliations(
    output_dir: Path,
    document_json: Any,
    input_file: Path,
) -> dict[str, Any]:
    records = [
        record
        for record in structural_text_records(document_json)
        if record.get("page_no") == 1 and isinstance(record.get("text"), str)
    ]
    if not records:
        return {"applied": False, "reason": "no_first_page_records"}
    records_sorted = sorted(
        records,
        key=lambda record: -float((record.get("bbox") or {}).get("t", 0)),
    )
    abstract = next(
        (record for record in records_sorted if str(record.get("text") or "").startswith("Abstract.")),
        None,
    )
    title = next(
        (record for record in records_sorted if str(record.get("label") or "").lower() == "section_header"),
        None,
    )
    author = next(
        (
            record
            for record in records_sorted
            if "†" in str(record.get("text") or "") or "∗" in str(record.get("text") or "")
        ),
        None,
    )
    if not abstract or not title or not author:
        return {"applied": False, "reason": "missing_title_author_or_abstract_anchor"}
    author_bbox = author.get("bbox") or {}
    abstract_bbox = abstract.get("bbox") or {}
    author_bottom = float(author_bbox.get("b", 0))
    abstract_top = float(abstract_bbox.get("t", 0))
    fragment_records = [
        record
        for record in records_sorted
        if abstract_top < float((record.get("bbox") or {}).get("t", 0)) < author_bottom
        and record is not author
        and record is not title
        and record is not abstract
    ]
    fragment_texts = [str(record.get("text") or "").strip() for record in fragment_records]
    if not fragment_texts or not any(re.fullmatch(r"\d{1,2}", text) for text in fragment_texts):
        return {"applied": False, "reason": "no_orphan_affiliation_number_fragments"}

    recovered_lines = _extract_first_page_affiliations_from_pdf(input_file)
    if not recovered_lines:
        return {"applied": False, "reason": "pdf_text_affiliation_evidence_missing"}
    recovered_block = "\n".join(recovered_lines)

    first_node = fragment_records[0].get("node")
    if isinstance(first_node, dict):
        first_node["text"] = recovered_block
        first_node["label"] = "text"
        first_node.setdefault("local_ai_lab_qc", {})["author_affiliation_recovery"] = {
            "source": "first_page_pdf_text_layer",
            "action": "replace_fragmented_affiliation_block",
            "recovered_lines": recovered_lines,
            "original_fragments": fragment_texts,
        }
    for record in fragment_records[1:]:
        node = record.get("node")
        if isinstance(node, dict):
            node["label"] = "quarantined_author_affiliation_fragment"
            node.setdefault("local_ai_lab_qc", {})["author_affiliation_recovery"] = {
                "source": "first_page_pdf_text_layer",
                "action": "evidence_preserved_fragment_replaced_by_recovered_block",
            }

    md_changed = 0
    md_path = output_dir / "document.md"
    if md_path.exists():
        md_text = md_path.read_text(encoding="utf-8")
        inserted = False
        for text in fragment_texts:
            replacement = "\n\n" + recovered_block + "\n\n" if not inserted else "\n\n"
            md_text, changed = _replace_first_occurrence_line(md_text, text, replacement)
            if changed:
                md_changed += 1
                inserted = inserted or bool(replacement.strip())
        md_path.write_text(md_text, encoding="utf-8")

    html_changed = 0
    html_path = output_dir / "document.html"
    if html_path.exists():
        document_html = html_path.read_text(encoding="utf-8")
        insertion = "<p class=\"docling-author-affiliation-recovery\">" + "<br>".join(
            html.escape(line) for line in recovered_lines
        ) + "</p>"
        inserted = False
        for text in fragment_texts:
            if not inserted:
                document_html, changed = _replace_exact_paragraph_with_quarantine(
                    document_html,
                    {"text": text, "kind": "author_affiliation_fragment", "page_no": 1, "reasons": ["replaced_by_pdf_text_layer_affiliation_block"]},
                )
                if changed:
                    document_html = document_html.replace(
                        _hidden_quarantine_html({"text": text, "kind": "author_affiliation_fragment", "page_no": 1, "reasons": ["replaced_by_pdf_text_layer_affiliation_block"]}),
                        insertion,
                        1,
                    )
                    html_changed += 1
                    inserted = True
            else:
                document_html, changed = _remove_exact_html_text_block(document_html, text)
                if changed:
                    html_changed += 1
        html_path.write_text(document_html, encoding="utf-8")

    return {
        "applied": True,
        "source": "first_page_pdf_text_layer",
        "recovered_lines": recovered_lines,
        "original_fragments": fragment_texts,
        "markdown_fragment_replacement_count": md_changed,
        "html_fragment_replacement_count": html_changed,
    }


EMAIL_PREFIX_RE = re.compile(
    r"^(?P<email>[A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
    r"(?P<tail>\s+.+)?$",
    re.S,
)


def _split_email_body_contamination(text: str) -> tuple[str, str | None]:
    match = EMAIL_PREFIX_RE.match(text.strip())
    if not match:
        return text.strip(), None
    email = re.sub(r"\s+", "", match.group("email"))
    tail = str(match.group("tail") or "").strip()
    if (
        len(tail.split()) < 5
        or not re.match(r"^[a-z]", tail)
        or not re.search(r"[.!?]$", tail)
    ):
        return text.strip(), None
    return email, tail


def recover_first_page_author_reading_order(
    output_dir: Path,
    document_json: Any,
) -> dict[str, Any]:
    def comparable_text(value: str) -> str:
        normalized = _normalized_noise_text(value).replace("∗", "*")
        return re.sub(r"\s*([*†‡])\s*", r"\1", normalized)

    records = [
        record
        for record in structural_text_records(document_json)
        if record.get("page_no") == 1
        and isinstance(record.get("text"), str)
        and record.get("bbox")
    ]
    abstract = next(
        (
            record
            for record in records
            if str(record.get("text") or "").strip().upper() == "ABSTRACT"
            or str(record.get("text") or "").strip().lower().startswith("abstract.")
        ),
        None,
    )
    if not abstract:
        return {"applied": False, "reason": "abstract_anchor_missing"}
    abstract_top = float((abstract.get("bbox") or {}).get("t", 0))
    title_candidates = [
        record
        for record in records
        if str(record.get("label") or "").lower() in {"section_header", "title"}
        and float((record.get("bbox") or {}).get("b", 0)) > abstract_top
        and str(record.get("text") or "").strip().upper() != "ABSTRACT"
    ]
    if not title_candidates:
        return {"applied": False, "reason": "title_anchor_missing"}
    title = max(
        title_candidates,
        key=lambda record: float((record.get("bbox") or {}).get("t", 0)),
    )
    title_bottom = float((title.get("bbox") or {}).get("b", 0))
    author_records = [
        record
        for record in records
        if abstract_top + 4 < float((record.get("bbox") or {}).get("t", 0)) < title_bottom
        and record is not abstract
        and record is not title
        and str(record.get("label") or "").lower()
        not in {"page_header", "page_footer", "footnote", "formula", "caption"}
        and not str(record.get("label") or "").lower().startswith("quarantined_")
    ]
    if (
        len(author_records) < 4
        or not any("@" in str(record.get("text") or "") for record in author_records)
    ):
        return {"applied": False, "reason": "author_region_evidence_insufficient"}

    left_positions = sorted(
        {
            round(float((record.get("bbox") or {}).get("l", 0)), 1)
            for record in author_records
        }
    )
    split_x: float | None = None
    if len(left_positions) > 1:
        gaps = [
            (right - left, (right + left) / 2)
            for left, right in zip(left_positions, left_positions[1:])
        ]
        largest_gap, candidate_split = max(gaps)
        if largest_gap >= 72:
            split_x = candidate_split
    ordered = sorted(
        author_records,
        key=lambda record: (
            1
            if split_x is not None
            and float((record.get("bbox") or {}).get("l", 0)) >= split_x
            else 0,
            -float((record.get("bbox") or {}).get("t", 0)),
            float((record.get("bbox") or {}).get("l", 0)),
        ),
    )

    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"applied": False, "reason": "document_html_missing"}
    document_html = html_path.read_text(encoding="utf-8")
    abstract_target = comparable_text(str(abstract.get("text") or ""))
    abstract_match = next(
        (
            match
            for match in HTML_TEXT_BLOCK_RE.finditer(document_html)
            if comparable_text(
                html.unescape(HTML_TAG_RE.sub("", match.group("body")))
            )
            == abstract_target
        ),
        None,
    )
    if not abstract_match:
        return {"applied": False, "reason": "abstract_html_anchor_missing"}

    extracted: list[tuple[dict[str, Any], str, str | None]] = []
    edits: list[tuple[int, int, str]] = []
    used_html_block_starts: set[int] = set()
    misplaced_count = 0
    contamination_count = 0
    for record in ordered:
        target = comparable_text(str(record.get("text") or ""))
        match = next(
            (
                candidate
                for candidate in HTML_TEXT_BLOCK_RE.finditer(document_html)
                if comparable_text(
                    html.unescape(HTML_TAG_RE.sub("", candidate.group("body")))
                )
                == target
                and candidate.start() not in used_html_block_starts
            ),
            None,
        )
        if not match:
            continue
        used_html_block_starts.add(match.start())
        author_text, tail = _split_email_body_contamination(
            str(record.get("text") or "")
        )
        if tail:
            contamination_count += 1
            author_block = f"<p>{html.escape(author_text)}</p>"
            retained = f"<p>{html.escape(tail)}</p>"
        else:
            author_block = match.group(0)
            retained = ""
        if match.start() > abstract_match.start():
            misplaced_count += 1
        extracted.append((record, author_block, tail))
        edits.append((match.start(), match.end(), retained))
        node = record.get("node")
        if isinstance(node, dict):
            node.setdefault("local_ai_lab_qc", {})["author_reading_order"] = {
                "source": "first_page_bbox_between_title_and_abstract",
                "action": "reorder_author_region_before_abstract",
                "email_body_contamination_split": bool(tail),
            }
    if not extracted or (not misplaced_count and not contamination_count):
        return {"applied": False, "reason": "author_region_already_ordered"}
    for start, end, replacement in sorted(edits, reverse=True):
        document_html = document_html[:start] + replacement + document_html[end:]
    abstract_match = next(
        (
            match
            for match in HTML_TEXT_BLOCK_RE.finditer(document_html)
            if comparable_text(
                html.unescape(HTML_TAG_RE.sub("", match.group("body")))
            )
            == abstract_target
        ),
        None,
    )
    if not abstract_match:
        return {"applied": False, "reason": "abstract_html_anchor_lost"}
    author_html = (
        '<section class="docling-author-region-recovery" '
        'aria-label="Recovered author information">'
        + "".join(item[1] for item in extracted)
        + "</section>"
    )
    document_html = (
        document_html[: abstract_match.start()]
        + author_html
        + document_html[abstract_match.start() :]
    )
    html_path.write_text(document_html, encoding="utf-8")

    markdown_count = 0
    md_path = output_dir / "document.md"
    if md_path.exists():
        markdown = md_path.read_text(encoding="utf-8")
        recovered_lines: list[str] = []

        def markdown_visible_line(line: str) -> str:
            visible = re.sub(r"^\s*#{1,6}\s+", "", line)
            visible = re.sub(r"<[^>]+>", "", visible)
            visible = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", visible)
            visible = re.sub(r"(?:\*\*|__|~~|`)", "", visible)
            return comparable_text(html.unescape(visible))

        for record, _block, tail in extracted:
            text = str(record.get("text") or "").strip()
            author_text, _unused = _split_email_body_contamination(text)
            target = comparable_text(text)
            lines = markdown.splitlines()
            line_index = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if markdown_visible_line(line) == target
                ),
                None,
            )
            if line_index is not None:
                original_line = re.sub(
                    r"^\s*#{1,6}\s+",
                    "",
                    lines[line_index],
                ).strip()
                lines[line_index] = tail or ""
                markdown = "\n".join(lines)
                markdown_count += 1
                recovered_lines.append(author_text if tail else original_line)
        abstract_pattern = re.compile(
            r"(?m)^(?P<line>#{1,6}\s+"
            + re.escape(str(abstract.get("text") or "").strip())
            + r"\s*)$"
        )
        recovered_block = "\n\n".join(recovered_lines)
        markdown, inserted = abstract_pattern.subn(
            recovered_block + "\n\n" + r"\g<line>",
            markdown,
            count=1,
        )
        if inserted:
            md_path.write_text(markdown, encoding="utf-8")

    return {
        "applied": True,
        "source": "first_page_bbox_between_title_and_abstract",
        "author_record_count": len(extracted),
        "misplaced_record_count": misplaced_count,
        "email_body_contamination_split_count": contamination_count,
        "markdown_record_replacement_count": markdown_count,
        "column_split_x": split_x,
    }


def recover_first_page_abstract_reading_order(
    output_dir: Path,
    document_json: Any,
) -> dict[str, Any]:
    records = [
        record
        for record in structural_text_records(document_json)
        if record.get("page_no") == 1
        and isinstance(record.get("text"), str)
        and record.get("bbox")
    ]
    abstract_heading = next(
        (
            record
            for record in records
            if str(record.get("label") or "").lower() == "section_header"
            and str(record.get("text") or "").strip().upper() == "ABSTRACT"
        ),
        None,
    )
    if not abstract_heading:
        return {"applied": False, "reason": "abstract_heading_missing"}
    abstract_body = next(
        (
            record
            for record in records
            if str(record.get("label") or "").lower() == "text"
            and (record.get("reading_order") or 0) > (abstract_heading.get("reading_order") or 0)
            and float((record.get("bbox") or {}).get("l", 0))
            <= float((abstract_heading.get("bbox") or {}).get("l", 0)) + 48
        ),
        None,
    )
    frontmatter_records = [
        record
        for record in records
        if str(record.get("label") or "").lower() == "section_header"
        and (
            str(record.get("text") or "").strip().upper()
            in {"CCS CONCEPTS", "KEYWORDS"}
            or str(record.get("text") or "").strip().lower().startswith("acmreference")
            or re.match(r"^\s*1\s+introduction\b", str(record.get("text") or ""), flags=re.I)
        )
    ]
    if not abstract_body or not frontmatter_records:
        return {"applied": False, "reason": "abstract_body_or_frontmatter_missing"}
    if not any(
        (record.get("reading_order") or 0) < (abstract_heading.get("reading_order") or 0)
        for record in frontmatter_records
    ):
        return {"applied": False, "reason": "abstract_already_before_frontmatter"}

    def comparable(value: str) -> str:
        return _normalized_noise_text(html.unescape(HTML_TAG_RE.sub("", value)))

    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"applied": False, "reason": "document_html_missing"}
    document_html = html_path.read_text(encoding="utf-8")

    def find_block(text: str) -> re.Match[str] | None:
        target = _normalized_noise_text(text)
        return next(
            (
                match
                for match in HTML_TEXT_BLOCK_RE.finditer(document_html)
                if comparable(match.group("body")) == target
            ),
            None,
        )

    heading_match = find_block(str(abstract_heading.get("text") or ""))
    body_match = find_block(str(abstract_body.get("text") or ""))
    front_matches = [
        find_block(str(record.get("text") or ""))
        for record in frontmatter_records
    ]
    front_matches = [match for match in front_matches if match is not None]
    if not heading_match or not body_match or not front_matches:
        return {"applied": False, "reason": "html_blocks_missing"}
    insertion_match = min(front_matches, key=lambda match: match.start())
    if heading_match.start() < insertion_match.start():
        return {"applied": False, "reason": "html_abstract_already_before_frontmatter"}
    moving = [(heading_match.start(), heading_match.end()), (body_match.start(), body_match.end())]
    moving = sorted(moving)
    block_html = "".join(document_html[start:end] for start, end in moving)
    updated = document_html
    for start, end in sorted(moving, reverse=True):
        updated = updated[:start] + "" + updated[end:]
    insertion_index = insertion_match.start()
    removed_before = sum(end - start for start, end in moving if end <= insertion_index)
    insertion_index -= removed_before
    updated = updated[:insertion_index] + block_html + updated[insertion_index:]
    html_path.write_text(updated, encoding="utf-8")

    markdown_count = 0
    md_path = output_dir / "document.md"
    if md_path.exists():
        markdown = md_path.read_text(encoding="utf-8")
        heading_pattern = re.compile(
            r"(?m)^#{1,6}\s+" + re.escape(str(abstract_heading.get("text") or "").strip()) + r"\s*$"
        )
        heading_md = heading_pattern.search(markdown)
        body_text = str(abstract_body.get("text") or "").strip()
        front_pattern = re.compile(
            r"(?m)^#{1,6}\s+(?:CCS CONCEPTS|KEYWORDS|ACMReference Format:|1 INTRODUCTION)\s*$",
            re.I,
        )
        front_md = front_pattern.search(markdown)
        body_pos = markdown.find(body_text)
        if heading_md and body_pos != -1 and front_md and heading_md.start() > front_md.start():
            body_end = body_pos + len(body_text)
            moving_start = heading_md.start()
            moving_end = body_end
            moving_md = markdown[moving_start:moving_end].strip() + "\n\n"
            markdown = markdown[:moving_start] + markdown[moving_end:]
            insert_at = front_md.start()
            if moving_end <= insert_at:
                insert_at -= moving_end - moving_start
            markdown = markdown[:insert_at] + moving_md + markdown[insert_at:]
            md_path.write_text(markdown, encoding="utf-8")
            markdown_count = 1

    return {
        "applied": True,
        "source": "first_page_heading_bbox_and_final_html_order",
        "html_moved_block_count": 2,
        "markdown_moved": bool(markdown_count),
        "frontmatter_heading_count": len(frontmatter_records),
    }


def structural_noise_qc(
    document_json: Any,
    source_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Identify structural regions and quarantine only evidence-rich candidates."""
    records = structural_text_records(document_json)
    picture_records = structural_picture_records(document_json)
    table_records = structural_table_records(document_json)
    page_extents = _page_vertical_extents(records)
    known_structural_records = [
        record
        for record in records
        if str(record.get("label") or "")
        .lower()
        .removeprefix("quarantined_")
        in PAGE_EDGE_LABELS | {"footnote"}
    ]
    footnote_zones: dict[Any, list[dict[str, Any]]] = {}
    for record in known_structural_records:
        if (
            str(record.get("label") or "")
            .lower()
            .removeprefix("quarantined_")
            == "footnote"
            and record.get("bbox")
        ):
            footnote_zones.setdefault(record.get("page_no"), []).append(
                record["bbox"]
            )
    picture_annotation_keys: dict[tuple[Any, str], dict[str, Any]] = {}
    visual_evidence_by_order: dict[int, dict[str, Any]] = {}
    visual_ocr_records: list[dict[str, Any]] = []
    for record in records:
        record_label = str(record.get("label") or "").lower()
        if record_label != "text" and not record_label.startswith(
            "quarantined_visual_annotation"
        ):
            continue
        record_text = str(record.get("text") or "")
        normalized = _normalized_noise_text(record_text)
        geometry = record.get("bbox")
        if not normalized or not geometry:
            continue
        _bottom_zone, record_top_zone, record_page_height = _edge_zone_flags(
            geometry,
            page_extents.get(record.get("page_no")),
        )
        if _is_semantic_subfigure_label(record_text):
            continue
        if re.match(r"^\s*[∗*†‡]\s+\S", record_text):
            continue
        evidence: dict[str, Any] | None = None
        for picture in picture_records:
            if picture.get("page_no") != record.get("page_no"):
                continue
            evidence = _picture_annotation_evidence(geometry, picture)
            if evidence:
                picture_annotation_keys[(record.get("page_no"), normalized)] = evidence
                break
        if not evidence:
            for table in table_records:
                if table.get("page_no") != record.get("page_no"):
                    continue
                evidence = _table_annotation_evidence(
                    geometry,
                    table,
                    top_zone=record_top_zone,
                    page_height=record_page_height,
                )
                if evidence:
                    break
        if not evidence and record_top_zone and float(geometry.get("height", 999)) <= 14:
            empty_page_tables = [
                table
                for table in table_records
                if table.get("page_no") == record.get("page_no")
                and not (
                    ((table.get("node") or {}).get("data") or {}).get("table_cells")
                    or ((table.get("node") or {}).get("data") or {}).get("num_rows")
                    or ((table.get("node") or {}).get("data") or {}).get("num_cols")
                )
            ]
            if len(empty_page_tables) >= 2:
                left = min(float(table["bbox"].get("l", 0)) for table in empty_page_tables)
                right = max(float(table["bbox"].get("r", 0)) for table in empty_page_tables)
                top = max(float(table["bbox"].get("t", 0)) for table in empty_page_tables)
                widest = max(
                    float(table["bbox"].get("width", 0))
                    for table in empty_page_tables
                )
                center_x = (
                    float(geometry.get("l", 0)) + float(geometry.get("r", 0))
                ) / 2
                vertical_gap = float(geometry.get("b", 0)) - top
                if (
                    left - widest * 0.7 <= center_x <= right + widest * 0.7
                    and 0 <= vertical_gap <= max(90.0, record_page_height * 0.12)
                ):
                    evidence = {
                        "table_index": empty_page_tables[0]["index"],
                        "overlap_ratio": 0.0,
                        "region_match": (
                            "small_text_top_edge_adjacent_to_empty_table_cluster"
                        ),
                    }
        if evidence:
            visual_evidence_by_order[int(record["reading_order"])] = evidence
            visual_ocr_records.append(record)
    normalized_counts: dict[str, int] = {}
    normalized_pages: dict[str, set[Any]] = {}
    for record in records:
        normalized = _normalized_noise_text(str(record.get("text") or ""))
        if normalized:
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
            normalized_pages.setdefault(normalized, set()).add(record.get("page_no"))

    candidates: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        label = str(record.get("label") or "")
        label_l = label.lower()
        if label_l.startswith("quarantined_"):
            label_l = label_l.removeprefix("quarantined_")
        text = str(record.get("text") or "")
        normalized = _normalized_noise_text(text)
        geometry = record.get("bbox") or {}
        bottom_zone, top_zone, page_height = _edge_zone_flags(
            geometry,
            page_extents.get(record.get("page_no")),
        )
        small_text = bool(
            geometry
            and float(geometry.get("height", 0.0)) <= max(14.0, page_height * 0.018)
        )
        adjacent_to_footnote_zone = bool(
            geometry
            and any(
                float(geometry.get("b", 0)) >= float(zone.get("t", 0))
                and float(geometry.get("b", 0)) - float(zone.get("t", 0))
                <= max(36.0, page_height * 0.055)
                and min(
                    float(geometry.get("r", 0)),
                    float(zone.get("r", 0)),
                )
                - max(
                    float(geometry.get("l", 0)),
                    float(zone.get("l", 0)),
                )
                > 0
                for zone in footnote_zones.get(record.get("page_no"), [])
            )
        )
        near_footnote_zone = bool(
            geometry
            and any(
                min(float(geometry.get("r", 0)), float(zone.get("r", 0)))
                - max(float(geometry.get("l", 0)), float(zone.get("l", 0)))
                > 0
                and (
                    abs(float(geometry.get("b", 0)) - float(zone.get("t", 0)))
                    <= max(24.0, page_height * 0.04)
                    or abs(float(zone.get("b", 0)) - float(geometry.get("t", 0)))
                    <= max(24.0, page_height * 0.04)
                )
                for zone in footnote_zones.get(record.get("page_no"), [])
            )
        )
        reasons: list[str] = []
        kind: str | None = None
        repeated_page_count = len(normalized_pages.get(normalized, set()))
        body_semantic_label = label_l in {
            "section_header",
            "caption",
            "list_item",
            "table",
            "formula",
            "picture",
        }

        if label_l in {"formula", "table", "picture"}:
            continue

        visual_overlap = (
            visual_evidence_by_order.get(int(record["reading_order"]))
            if label_l == "text"
            else None
        )
        picture_overlap = (
            visual_overlap if visual_overlap and "picture_index" in visual_overlap else None
        )
        table_overlap = (
            visual_overlap if visual_overlap and "table_index" in visual_overlap else None
        )
        structural_shadow = (
            _structural_shadow_record(record, known_structural_records)
            if label_l not in PAGE_EDGE_LABELS | {"footnote"}
            else None
        )
        abrupt_visual_suffix = (
            _abrupt_visual_text_suffix(text)
            if label_l == "text" and not visual_overlap and not structural_shadow
            else None
        )
        diagram_visual_suffix = (
            _diagram_visual_text_suffix(text)
            if label_l == "text"
            and not visual_overlap
            and not structural_shadow
            and not abrupt_visual_suffix
            else None
        )
        source_grounded_suffix = (
            _source_grounded_visual_suffix(
                record,
                source_evidence,
                visual_ocr_records,
            )
            if label_l == "text"
            and not visual_overlap
            and not structural_shadow
            and not abrupt_visual_suffix
            and not diagram_visual_suffix
            else None
        )
        visual_annotation_shadow = (
            picture_annotation_keys.get((record.get("page_no"), normalized))
            if label_l == "text"
            and not visual_overlap
            and not structural_shadow
            and len(normalized) <= 80
            else None
        )
        visual_diagram_cluster = (
            _visual_diagram_cluster_evidence(record, records, picture_records)
            if label_l == "text"
            and not structural_shadow
            else None
        )
        private_use_math_prefix = (
            _private_use_math_noise_prefix(text)
            if label_l in {"caption", "text", "visual_annotation"}
            else None
        )
        private_use_math_standalone = (
            _looks_like_private_use_math_noise(text)
            if label_l in {"caption", "text", "visual_annotation"}
            else False
        )

        if _looks_like_author_affiliation_footnote_mislabel(
            label_l,
            text,
            record.get("page_no"),
            geometry,
        ):
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {})["author_affiliation_recovery"] = {
                    "original_label": label,
                    "reason": "first_page_footnote_label_outside_bottom_footnote_region",
                    "action": "preserve_in_main_text_flow",
                }
                node["label"] = "text"
            continue

        if abrupt_visual_suffix:
            candidates.append(
                {
                    "index": index,
                    "kind": "reading_order_annotation",
                    "label": label,
                    "text": abrupt_visual_suffix,
                    "text_preview": abrupt_visual_suffix.strip()[:300],
                    "page_no": record.get("page_no"),
                    "bbox": geometry or None,
                    "reasons": [
                        "abrupt_terminal_uppercase_fragment",
                        "long_body_paragraph_reading_order_discontinuity",
                        "fragment_quarantined_without_removing_body_paragraph",
                    ],
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                    "evidence_score": 6,
                    "reading_order": record.get("reading_order"),
                    "repeated_page_count": repeated_page_count,
                    "picture_overlap": None,
                    "structural_shadow": None,
                    "match_mode": "fragment",
                    "evidence": (
                        f"pages/page_{record.get('page_no')}.png"
                        if record.get("page_no")
                        else None
                    ),
                }
            )
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {})["structural_fragment_quarantine"] = {
                    "kind": "reading_order_annotation",
                    "text": abrupt_visual_suffix,
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                }
            continue

        if diagram_visual_suffix:
            candidates.append(
                {
                    "index": index,
                    "kind": "reading_order_visual_annotation",
                    "label": label,
                    "text": diagram_visual_suffix,
                    "text_preview": diagram_visual_suffix.strip()[:300],
                    "page_no": record.get("page_no"),
                    "bbox": geometry or None,
                    "reasons": [
                        "terminal_diagram_label_sequence",
                        "fragment_quarantined_without_removing_body_paragraph",
                    ],
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                    "evidence_score": 7,
                    "reading_order": record.get("reading_order"),
                    "repeated_page_count": repeated_page_count,
                    "picture_overlap": None,
                    "structural_shadow": None,
                    "match_mode": "fragment",
                    "evidence": (
                        f"pages/page_{record.get('page_no')}.png"
                        if record.get("page_no")
                        else None
                    ),
                }
            )
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {})["structural_fragment_quarantine"] = {
                    "kind": "reading_order_visual_annotation",
                    "text": diagram_visual_suffix,
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                }
            continue

        if private_use_math_prefix or private_use_math_standalone:
            noise_text = private_use_math_prefix or text
            candidates.append(
                {
                    "index": index,
                    "kind": "math_font_noise_fragment",
                    "label": label,
                    "text": noise_text,
                    "text_preview": noise_text.strip()[:300],
                    "page_no": record.get("page_no"),
                    "bbox": geometry or None,
                    "reasons": [
                        (
                            "private_use_math_font_glyph_prefix"
                            if private_use_math_prefix
                            else "private_use_math_font_glyph_fragment"
                        ),
                        "fragment_quarantined_without_removing_body_paragraph",
                    ],
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                    "evidence_score": 7,
                    "reading_order": record.get("reading_order"),
                    "repeated_page_count": repeated_page_count,
                    "picture_overlap": picture_overlap,
                    "structural_shadow": structural_shadow,
                    "match_mode": "fragment",
                    "evidence": (
                        f"pages/page_{record.get('page_no')}.png"
                        if record.get("page_no")
                        else None
                    ),
                }
            )
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {})["structural_fragment_quarantine"] = {
                    "kind": "math_font_noise_fragment",
                    "text": noise_text,
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                }
            continue

        if source_grounded_suffix:
            suffix = str(source_grounded_suffix["fragment"])
            candidates.append(
                {
                    "index": index,
                    "kind": "reading_order_table_annotation",
                    "label": label,
                    "text": suffix,
                    "text_preview": suffix.strip()[:300],
                    "page_no": record.get("page_no"),
                    "bbox": geometry or None,
                    "reasons": [
                        "source_pdf_confirms_body_prefix_boundary",
                        "suffix_tokens_match_adjacent_visual_ocr",
                        "fragment_quarantined_without_removing_body_paragraph",
                    ],
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                    "evidence_score": 7,
                    "reading_order": record.get("reading_order"),
                    "repeated_page_count": repeated_page_count,
                    "picture_overlap": None,
                    "table_overlap": None,
                    "structural_shadow": None,
                    "match_mode": "fragment",
                    "source_grounding": {
                        key: value
                        for key, value in source_grounded_suffix.items()
                        if key != "fragment"
                    },
                    "evidence": (
                        f"pages/page_{record.get('page_no')}.png"
                        if record.get("page_no")
                        else None
                    ),
                }
            )
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {})["structural_fragment_quarantine"] = {
                    "kind": "reading_order_table_annotation",
                    "text": suffix,
                    "action": "quarantine_from_main_text_flow",
                    "confidence": "high",
                    "source_grounding": {
                        key: value
                        for key, value in source_grounded_suffix.items()
                        if key != "fragment"
                    },
                }
            continue

        if picture_overlap:
            kind = "visual_annotation"
            reasons.extend(
                [
                    "text_bbox_inside_rendered_picture",
                    "duplicate_visual_ocr_removed_from_linear_reading_flow",
                ]
            )
        elif table_overlap:
            kind = "table_visual_annotation"
            reasons.extend(
                [
                    "text_bbox_inside_or_adjacent_to_table",
                    "duplicate_table_ocr_removed_from_linear_reading_flow",
                ]
            )
        elif visual_annotation_shadow:
            kind = "visual_annotation_shadow"
            reasons.extend(
                [
                    "same_page_duplicate_of_picture_annotation",
                    "duplicate_visual_ocr_removed_from_linear_reading_flow",
                ]
            )
        elif visual_diagram_cluster:
            kind = "visual_annotation"
            reasons.extend(
                [
                    "diagram_label_cluster_near_figure",
                    "duplicate_visual_ocr_removed_from_linear_reading_flow",
                ]
            )
        elif structural_shadow:
            shadow_label = str(structural_shadow.get("label") or "structural")
            kind = f"{shadow_label}_shadow"
            reasons.extend(
                [
                    "duplicate_text_overlaps_labeled_structural_region",
                    f"shadow_of_{shadow_label}",
                ]
            )
        elif label_l in PAGE_EDGE_LABELS:
            kind = label_l
            reasons.append(f"docling_label_{label_l}")
        elif geometry and not body_semantic_label:
            if bottom_zone and (
                re.fullmatch(r"\d{1,3}", normalized)
                or (
                    HEADER_FOOTER_NOISE_RE.search(normalized)
                    and len(normalized) <= 160
                )
                or (
                    repeated_page_count >= 2
                    and len(normalized) >= 6
                    and bool(re.search(r"[A-Za-z\u3400-\u9fff]", normalized))
                )
            ):
                kind = "page_footer_candidate"
                reasons.append("bottom_edge_noise_candidate")
            if top_zone and (
                (
                    HEADER_FOOTER_NOISE_RE.search(normalized)
                    and len(normalized) <= 160
                )
                or (
                    repeated_page_count >= 2
                    and len(normalized) >= 6
                    and bool(re.search(r"[A-Za-z\u3400-\u9fff]", normalized))
                )
            ):
                kind = "page_header_candidate"
                reasons.append("top_edge_noise_candidate")

        if label_l == "footnote":
            kind = "footnote"
            reasons.append("docling_label_footnote")
        elif (
            not body_semantic_label
            and FOOTNOTE_MARKER_RE.search(text)
            and FOOTNOTE_CONTENT_NOISE_RE.search(normalized)
        ):
            kind = "footnote_candidate"
            reasons.append("marker_led_footnote_content_candidate")
        elif (
            not body_semantic_label
            and geometry
            and bottom_zone
            and FOOTNOTE_MARKER_RE.search(text)
        ):
            kind = kind or "footnote_candidate"
            reasons.append("bottom_region_footnote_marker_candidate")
            if small_text:
                reasons.append("small_text_bottom_footnote_marker_candidate")
        elif not body_semantic_label and geometry and bottom_zone and small_text and re.match(
            r"^\s*(?:[∗*†‡]|\d{1,2})\s+",
            normalized,
        ):
            kind = kind or "footnote_candidate"
            reasons.append("small_text_bottom_footnote_marker_candidate")
        elif (
            not body_semantic_label
            and adjacent_to_footnote_zone
            and FOOTNOTE_MARKER_RE.search(text)
        ):
            kind = kind or "footnote_candidate"
            reasons.extend(
                [
                    "marker_led_line_adjacent_to_labeled_footnote_zone",
                    "same_column_footnote_cluster",
                ]
            )
        elif (
            not body_semantic_label
            and near_footnote_zone
            and small_text
            and (
                FOOTNOTE_CONTENT_NOISE_RE.search(normalized)
                or re.match(r"^\s*(?:please note|note that)\b", normalized, flags=re.I)
                or (len(normalized) <= 180 and re.search(r"https?://|github\.com", normalized, flags=re.I))
            )
        ):
            kind = kind or "footnote_candidate"
            reasons.extend(
                [
                    "same_column_footnote_continuation",
                    "small_text_adjacent_to_labeled_footnote_zone",
                ]
            )
        elif (
            not body_semantic_label
            and geometry
            and bottom_zone
            and text.rstrip().endswith("-")
            and len(normalized) < 180
        ):
            kind = kind or "footnote_candidate"
            reasons.append("bottom_region_hyphenated_annotation_candidate")

        if (
            not body_semantic_label
            and adjacent_to_footnote_zone
            and FOOTNOTE_MARKER_RE.search(text)
            and "same_column_footnote_cluster" not in reasons
        ):
            kind = kind or "footnote_candidate"
            reasons.extend(
                [
                    "marker_led_line_adjacent_to_labeled_footnote_zone",
                    "same_column_footnote_cluster",
                ]
            )

        if repeated_page_count >= 2 and kind in {"page_header", "page_header_candidate", "page_footer", "page_footer_candidate"}:
            reasons.append("repeated_text")
        if repeated_page_count >= 2 and (bottom_zone or top_zone):
            reasons.append("cross_page_repetition")
        if re.fullmatch(r"\d{1,3}", normalized) and kind:
            reasons.append("page_or_footnote_number_fragment")
        if HEADER_FOOTER_NOISE_RE.search(normalized):
            reasons.append("publication_template_noise")
        if small_text and kind:
            reasons.append("small_text_page_edge_zone")
        if label_l == "footnote" and re.fullmatch(r"\d+", normalized):
            reasons.append("isolated_footnote_marker")
        if label_l == "footnote" and re.match(r"^\d+\s+\w+", normalized):
            reasons.append("footnote_marker_attached_to_body_fragment")
        if label_l == "footnote" and text.rstrip().endswith("-"):
            reasons.append("hyphenated_footnote_continuation")

        if not kind or not reasons:
            continue

        score = 0
        if picture_overlap:
            score += 6
        if table_overlap:
            score += 6
        if visual_annotation_shadow:
            score += 6
        if visual_diagram_cluster:
            score += 6
        if structural_shadow:
            score += 6
        if label_l in PAGE_EDGE_LABELS or label_l == "footnote":
            score += 5
        if repeated_page_count >= 2:
            score += 3
        if bottom_zone or top_zone:
            score += 1
        if FOOTNOTE_MARKER_RE.search(text):
            score += 2
        if adjacent_to_footnote_zone and "same_column_footnote_cluster" in reasons:
            score += 4
        if "same_column_footnote_continuation" in reasons:
            score += 4
        if FOOTNOTE_CONTENT_NOISE_RE.search(normalized) or HEADER_FOOTER_NOISE_RE.search(normalized):
            score += 2
        if small_text:
            score += 1
        confidence = "high" if score >= 5 else "medium" if score >= 3 else "low"
        action = (
            "quarantine_from_main_text_flow"
            if confidence == "high"
            else "diagnostic_annotation_only"
        )
        node = record.get("node")
        if isinstance(node, dict):
            node.setdefault("local_ai_lab_qc", {})["structural_quarantine"] = {
                "kind": kind,
                "reasons": reasons,
                "action": action,
                "confidence": confidence,
                "evidence_score": score,
            }
            if action == "quarantine_from_main_text_flow":
                node["label"] = f"quarantined_{kind}"

        candidates.append(
            {
                "index": index,
                "kind": kind,
                "label": label,
                "text": text,
                "text_preview": text[:300],
                "page_no": record.get("page_no"),
                "bbox": geometry or None,
                "reasons": reasons,
                "action": action,
                "confidence": confidence,
                "evidence_score": score,
                "reading_order": record.get("reading_order"),
                "repeated_page_count": repeated_page_count,
                "picture_overlap": picture_overlap,
                "table_overlap": table_overlap,
                "visual_annotation_shadow": visual_annotation_shadow,
                "visual_diagram_cluster": visual_diagram_cluster,
                "structural_shadow": structural_shadow,
                "evidence": f"pages/page_{record.get('page_no')}.png" if record.get("page_no") else None,
            }
        )

    quarantined = [
        item for item in candidates
        if item["action"] == "quarantine_from_main_text_flow"
    ]
    return {
        "candidate_count": len(candidates),
        "quarantine_candidate_count": len(quarantined),
        "annotation_only_candidate_count": len(candidates) - len(quarantined),
        "candidate_counts_by_kind": {
            kind: sum(1 for item in candidates if item["kind"] == kind)
            for kind in sorted({item["kind"] for item in candidates})
        },
        "candidate_counts_by_confidence": {
            confidence: sum(1 for item in candidates if item["confidence"] == confidence)
            for confidence in ("high", "medium", "low")
        },
        "isolated_main_text_pollution_count": 0,
        "recovered_footnote_count": 0,
        "unresolved_footnote_count": sum(1 for item in quarantined if "footnote" in item["kind"]),
        "candidates": candidates,
    }


def _hidden_quarantine_html(item: dict[str, Any]) -> str:
    return (
        "<!-- local-ai-lab structural quarantine "
        f"kind={html.escape(str(item.get('kind')), quote=True)} "
        f"page={html.escape(str(item.get('page_no')), quote=True)} "
        "evidence=structural_regions.json -->"
    )


def _replace_exact_paragraph_with_quarantine(document_html: str, item: dict[str, Any]) -> tuple[str, bool]:
    text = str(item.get("text") or "").strip()
    if not text:
        return document_html, False
    target = _normalized_noise_text(text)
    replacement = _hidden_quarantine_html(item)
    patterns = [
        re.compile(r"<p>\s*" + re.escape(html.escape(text)) + r"\s*</p>"),
        re.compile(r"<li>\s*" + re.escape(html.escape(text)) + r"\s*</li>"),
    ]
    updated = document_html
    for pattern in patterns:
        updated, count = pattern.subn(lambda _match: replacement, updated, count=1)
        if count:
            return updated, True

    for match in HTML_TEXT_BLOCK_RE.finditer(document_html):
        visible = _normalized_noise_text(html.unescape(HTML_TAG_RE.sub(" ", match.group("body"))))
        if visible == target:
            updated = document_html[: match.start()] + replacement + document_html[match.end() :]
            return updated, True
    return document_html, False


def _replace_html_fragment_with_quarantine(
    document_html: str,
    item: dict[str, Any],
) -> tuple[str, bool]:
    text = str(item.get("text") or "")
    if not text.strip():
        return document_html, False
    escaped = html.escape(text)
    replacement = _hidden_quarantine_html(item)
    for match in HTML_TEXT_BLOCK_RE.finditer(document_html):
        body = match.group("body")
        if escaped not in body:
            continue
        updated_body = body.replace(escaped, "", 1).rstrip()
        updated_block = (
            f"<{match.group('tag')}{match.group('attrs')}>"
            f"{updated_body}</{match.group('tag')}>"
            f"{replacement}"
        )
        return (
            document_html[: match.start()]
            + updated_block
            + document_html[match.end() :],
            True,
        )
    return document_html, False


def _replace_private_use_math_noise_blocks_html(document_html: str) -> tuple[str, int]:
    replacements: list[tuple[int, int, str]] = []
    for match in HTML_TEXT_BLOCK_RE.finditer(document_html):
        tag = match.group("tag").lower()
        if tag in {"script", "style", "template"}:
            continue
        visible = html.unescape(HTML_TAG_RE.sub(" ", match.group("body")))
        if not _looks_like_private_use_math_noise(visible):
            continue
        item = {
            "kind": "math_font_noise_fragment",
            "page_no": "unknown",
            "reasons": ["private_use_math_font_glyph_output_sweep"],
        }
        replacements.append((match.start(), match.end(), _hidden_quarantine_html(item)))
    if not replacements:
        return document_html, 0
    updated = document_html
    for start, end, replacement in reversed(replacements):
        updated = updated[:start] + replacement + updated[end:]
    return updated, len(replacements)


def _replace_private_use_math_noise_blocks_markdown(md_text: str) -> tuple[str, int]:
    blocks = re.split(r"(\n\s*\n)", md_text)
    replaced = 0
    for index, block in enumerate(blocks):
        if not block.strip() or block.startswith("<!--"):
            continue
        if not _looks_like_private_use_math_noise(block):
            continue
        blocks[index] = (
            "<!-- local-ai-lab structural quarantine "
            "kind=math_font_noise_fragment page=unknown "
            "reasons=private_use_math_font_glyph_output_sweep "
            "evidence=metadata.json -->"
        )
        replaced += 1
    return "".join(blocks), replaced


def _visible_html_text(document_html: str) -> str:
    visible = re.sub(
        r"<(?:template|style|script)\b.*?</(?:template|style|script)>",
        " ",
        document_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _normalized_noise_text(html.unescape(HTML_TAG_RE.sub(" ", visible)))


def _html_has_exact_visible_block(document_html: str, text: str) -> bool:
    target = _normalized_noise_text(text)
    if not target:
        return False
    visible_html = re.sub(
        r"<(?:template|style|script)\b.*?</(?:template|style|script)>",
        " ",
        document_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return any(
        _normalized_noise_text(html.unescape(HTML_TAG_RE.sub(" ", match.group("body"))))
        == target
        for match in HTML_TEXT_BLOCK_RE.finditer(visible_html)
    )


def _markdown_exact_text_pattern(text: str) -> re.Pattern[str]:
    parts = [_markdown_token_pattern(part) for part in text.split()]
    return re.compile(
        r"(?:\A|\n\s*\n)\s*"
        + r"\s+".join(parts)
        + r"\s*(?=\n\s*\n|\Z)",
        re.DOTALL,
    )


def _markdown_token_pattern(token: str) -> str:
    markdown_escaped = r"\`*{}[]()#+-.!_|>"
    return "".join(
        r"\\?" + re.escape(char)
        if char in markdown_escaped
        else re.escape(char)
        for char in token
    )


def _markdown_fragment_pattern(text: str) -> re.Pattern[str]:
    parts = [_markdown_token_pattern(part) for part in text.split()]
    return re.compile(r"\s+".join(parts))


def _replace_markdown_quarantine_text(
    md_text: str,
    text: str,
    replacement: str,
    *,
    fragment: bool,
) -> tuple[str, int]:
    pattern_factory = _markdown_fragment_pattern if fragment else _markdown_exact_text_pattern
    variants = [text]
    escaped = html.escape(text, quote=False)
    if escaped != text:
        variants.append(escaped)
    for variant in variants:
        md_text, count = pattern_factory(variant).subn(
            replacement,
            md_text,
            count=1,
        )
        if count:
            return md_text, count
    return md_text, 0


def _normalized_markdown_text(text: str) -> str:
    text = re.sub(r"\\([\\`*{}\[\]()#+\-.!_|>])", r"\1", text)
    return _normalized_noise_text(text)


STRUCTURAL_CONTENT_HTML_ID = "docling-structural-content"
STRUCTURAL_CONTENT_MD_START = "<!-- local-ai-lab structural content start -->"
STRUCTURAL_CONTENT_MD_END = "<!-- local-ai-lab structural content end -->"


def _exportable_structural_kind(kind: str) -> str | None:
    normalized = kind.lower()
    if "footnote" in normalized:
        return "footnote"
    if "page_header" in normalized:
        return "page_header"
    if "page_footer" in normalized:
        return "page_footer"
    if "visual_annotation" in normalized:
        return "visual_annotation"
    if "math_font_noise" in normalized:
        return "math_font_noise"
    return None


def _structural_export_records(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, str]] = set()
    for index, item in enumerate(candidates, start=1):
        export_kind = _exportable_structural_kind(str(item.get("kind") or ""))
        text = str(item.get("text") or "").strip()
        if (
            not export_kind
            or not text
            or item.get("action") != "quarantine_from_main_text_flow"
            or item.get("confidence") != "high"
        ):
            continue
        key = (item.get("page_no"), export_kind, _normalized_noise_text(text))
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "index": index,
                "kind": export_kind,
                "source_kind": item.get("kind"),
                "page_no": item.get("page_no"),
                "reading_order": item.get("reading_order"),
                "text": text,
                "confidence": item.get("confidence"),
                "evidence_score": item.get("evidence_score"),
                "bbox": item.get("bbox"),
                "reasons": item.get("reasons") or [],
                "evidence": item.get("evidence"),
            }
        )
    return sorted(
        records,
        key=lambda item: (
            item.get("page_no") is None,
            item.get("page_no") or 0,
            item.get("reading_order") is None,
            item.get("reading_order") or 0,
            item["index"],
        ),
    )


NOTE_MARKER_PREFIX_RE = re.compile(r"^\s*([∗*†‡]|\d{1,2})(?:\s+|$)(.*)$", re.S)
BIBLIOGRAPHY_HEADING_RE = re.compile(
    r"^\s*(?:references|bibliography|参考文献)\s*[:：]?\s*$",
    re.I,
)
GENERAL_BRACKET_CITATION_RE = re.compile(
    r"(?P<open>[\[［〔\(（])"
    r"(?P<body>[^\]］〕\)）\n]{1,160})"
    r"(?P<close>[\]］〕\)）])"
)
MISSING_OPEN_BRACKET_RANGE_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<body>\d{1,3}\s*(?:[\u2013\u2014~～-])\s*\d{1,3}"
    r"(?:\s*[,;，；、]\s*\d{1,3}\s*(?:[\u2013\u2014~～-])?\s*\d{0,3})*)"
    r"(?P<close>[\]］〕])"
)
INLINE_CITATION_RE = re.compile(
    r"(?:"
    r"(?P<paired_open>[\[［〔])"
    r"(?P<body>\s*\d{1,3}(?:\s*(?:[,;，；]|\u2013|\u2014|-)\s*\d{1,3})*\s*)"
    r"(?P<paired_close>[\]］〕])"
    r"|"
    r"(?P<mixed_open>[\(（])"
    r"(?P<mixed_body>\s*\d{1,3}(?:\s*(?:[,;，；]|\u2013|\u2014|-)\s*\d{1,3})*\s*)"
    r"(?P<mixed_close>[\]］〕])"
    r"|"
    r"(?P<open_only>〔)"
    r"(?P<open_only_body>\s*\d{1,3})"
    r"(?=\s|[，。；、])"
    r")"
)
MALFORMED_CITATION_TOKEN_RE = re.compile(
    r"(?:"
    r"(?P<bracketed>[\[［〔【「]\s*[0-9OoIl|!！\"'“”]{1,3}\s*[\]］〕】」）)]?)"
    r"|"
    r"(?P<suffix>[!！\"'“”]\s*\d{1,2}\s*[\]］〕】」）)]?)"
    r"|"
    r"(?P<open>[\[［〔【「]\s*\d{1,3}(?=\s|[A-Za-z\u3400-\u9fff，。；、]))"
    r")"
)
AUTHOR_CITATION_ALIAS_RE = re.compile(
    r"(?P<alias>[A-Z][A-Za-z-]{1,24}|[\u3400-\u9fff]{2,5})"
    r"\s*等人\s*(?P<raw>[】］〕」]|[【「\[]?\s*[0-9OoIl|!！\"'“”]{0,3})"
)
MODEL_CITATION_ALIAS_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<alias>[A-Z][A-Z0-9]*(?:[-.][A-Z0-9]+)*[A-Z0-9])"
    r"\s*(?P<raw>(?:[\[［〔【「]?\s*[0-9OoIl|!！\"'“”]{1,3}\s*[\]］〕】」）)]?)|(?:[+＋]\s*)?)"
    r"(?=\s*模型|模型|[，、,])"
)
AUTHOR_YEAR_CITATION_RE = re.compile(
    r"(?P<open>[\[［〔\(（])"
    r"(?P<body>"
    r"[A-Z][A-Za-z'’.-]{1,40}(?:\s+[A-Z][A-Za-z'’.-]{1,40}){0,3}"
    r"(?:\s+(?:et\s+al\.|and\s+[A-Z][A-Za-z'’.-]{1,40}"
    r"(?:\s+[A-Z][A-Za-z'’.-]{1,40}){0,3}))?"
    r"\s*,\s*(?:19|20)\d{2}[a-z]?"
    r"(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*"
    r"(?:\s*;\s*"
    r"[A-Z][A-Za-z'’.-]{1,40}(?:\s+[A-Z][A-Za-z'’.-]{1,40}){0,3}"
    r"(?:\s+(?:et\s+al\.|and\s+[A-Z][A-Za-z'’.-]{1,40}"
    r"(?:\s+[A-Z][A-Za-z'’.-]{1,40}){0,3}))?"
    r"\s*,\s*(?:19|20)\d{2}[a-z]?"
    r"(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*"
    r")*"
    r")"
    r"(?P<close>[\]］〕\)）])"
)
NARRATIVE_AUTHOR_YEAR_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<author>[A-Z][A-Za-z'’.-]{1,40}"
    r"(?:\s+(?:et\s+al\.|and\s+[A-Z][A-Za-z'’.-]{1,40}))?)"
    r"\s*\((?P<year>(?:19|20)\d{2}[a-z]?)\)"
)
REFERENCE_CONTINUATION_START_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}(?:st|nd|rd|th)\b|"
    r"\(?\d{4}\)\.?|"
    r"\((?:eds?|ed)\.?\)|"
    r"(?:eds?|pp?)\.\s"
    r")",
    re.I,
)
REFERENCE_TRAILING_USAGE_PAGES_RE = re.compile(
    r"(?:^|[^\d])\d{1,3}(?:\s*,\s*\d{1,3})*\s*$"
)


def _note_marker_and_body(text: str) -> tuple[str | None, str]:
    match = NOTE_MARKER_PREFIX_RE.match(text)
    if not match:
        return None, text.strip()
    return match.group(1).replace("∗", "*"), match.group(2).strip()


def _same_note_column(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_box = left.get("bbox") or {}
    right_box = right.get("bbox") or {}
    if not left_box or not right_box:
        return False
    return abs(float(left_box.get("l", 0)) - float(right_box.get("l", 0))) <= 32


def _vertically_adjacent_note_lines(upper: dict[str, Any], lower: dict[str, Any]) -> bool:
    upper_box = upper.get("bbox") or {}
    lower_box = lower.get("bbox") or {}
    if not upper_box or not lower_box:
        return False
    gap = float(upper_box.get("b", 0)) - float(lower_box.get("t", 0))
    overlap_tolerance = max(
        float(upper_box.get("height") or abs(float(upper_box.get("t", 0)) - float(upper_box.get("b", 0)))),
        float(lower_box.get("height") or abs(float(lower_box.get("t", 0)) - float(lower_box.get("b", 0)))),
        8.0,
    ) * 0.75
    return -overlap_tolerance <= gap <= 18


def _join_note_text(parts: list[str]) -> str:
    result = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if result.endswith("-") and re.match(r"^[a-z]", part):
            result = result[:-1] + part
        else:
            result = f"{result} {part}".strip()
    return result


def _note_marker_slug(marker: str | None) -> str:
    return {
        "*": "star",
        "†": "dagger",
        "‡": "double-dagger",
    }.get(marker or "", marker or "unmarked")


def _source_page_text_lines(
    source_evidence: dict[str, Any],
    page_no: Any,
) -> list[dict[str, Any]]:
    page = (source_evidence.get("pages") or {}).get(page_no) or {}
    characters = [
        char
        for char in page.get("characters") or []
        if str(char.get("text") or "") not in "\r\n"
        and char.get("bbox")
    ]
    clusters: list[dict[str, Any]] = []
    for char in sorted(
        characters,
        key=lambda item: -float((item.get("bbox") or {}).get("b", 0)),
    ):
        baseline = float(char["bbox"].get("b", 0))
        cluster = next(
            (
                item
                for item in clusters
                if abs(float(item["baseline"]) - baseline) <= 4.0
            ),
            None,
        )
        if cluster is None:
            cluster = {"baseline": baseline, "characters": []}
            clusters.append(cluster)
        cluster["characters"].append(char)
        cluster["baseline"] = sum(
            float(item["bbox"].get("b", 0))
            for item in cluster["characters"]
        ) / len(cluster["characters"])

    lines: list[dict[str, Any]] = []
    for cluster in clusters:
        ordered = sorted(
            cluster["characters"],
            key=lambda item: float(item["bbox"].get("l", 0)),
        )
        segments: list[list[dict[str, Any]]] = []
        for char in ordered:
            if not segments:
                segments.append([char])
                continue
            previous = segments[-1][-1]
            gap = float(char["bbox"].get("l", 0)) - float(previous["bbox"].get("r", 0))
            median_size = float(page.get("median_font_size") or 8.0)
            if gap > max(14.0, median_size * 1.6):
                segments.append([char])
            else:
                segments[-1].append(char)
        for segment in segments:
            text = "".join(str(char.get("text") or "") for char in segment)
            text = _normalize_pdf_text_line(text)
            text = re.sub(
                r"^([∗*†‡]|\d{1,2})(?=[A-Za-z\u3400-\u9fff])",
                r"\1 ",
                text,
            )
            if not text:
                continue
            lines.append(
                {
                    "text": text,
                    "bbox": _bbox_union([char["bbox"] for char in segment]),
                    "baseline": cluster["baseline"],
                    "source": "pdf_text_character_baseline",
                }
            )
    return sorted(
        lines,
        key=lambda item: (
            -float((item.get("bbox") or {}).get("t", 0)),
            float((item.get("bbox") or {}).get("l", 0)),
        ),
    )


def _bbox_overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    if not left or not right:
        return 0.0
    overlap_width = max(
        0.0,
        min(float(left.get("r", 0)), float(right.get("r", 0)))
        - max(float(left.get("l", 0)), float(right.get("l", 0))),
    )
    overlap_height = max(
        0.0,
        min(float(left.get("t", 0)), float(right.get("t", 0)))
        - max(float(left.get("b", 0)), float(right.get("b", 0))),
    )
    area = max(
        float(left.get("width") or 0) * float(left.get("height") or 0),
        1.0,
    )
    return overlap_width * overlap_height / area


def _script_marker_record_distance(
    marker_bbox: dict[str, Any],
    record_bbox: dict[str, Any] | None,
) -> float | None:
    """Return a small distance when a PDF script marker hugs a text record."""
    if not marker_bbox or not record_bbox:
        return None
    marker_l = float(marker_bbox.get("l", 0))
    marker_r = float(marker_bbox.get("r", marker_l))
    marker_b = float(marker_bbox.get("b", 0))
    marker_t = float(marker_bbox.get("t", marker_b))
    marker_cy = marker_b + max(marker_t - marker_b, 0.0) / 2
    record_l = float(record_bbox.get("l", 0))
    record_r = float(record_bbox.get("r", record_l))
    record_b = float(record_bbox.get("b", 0))
    record_t = float(record_bbox.get("t", record_b))
    if marker_cy < record_b - 4 or marker_cy > record_t + 10:
        return None
    if marker_r < record_l:
        dx = record_l - marker_r
    elif marker_l > record_r:
        dx = marker_l - record_r
    else:
        dx = 0.0
    if dx > 18:
        return None
    dy = 0.0
    if marker_cy < record_b:
        dy = record_b - marker_cy
    elif marker_cy > record_t:
        dy = marker_cy - record_t
    return dx + dy


def _source_footnote_line_records(
    page_no: Any,
    page_records: list[dict[str, Any]],
    source_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    if not any(
        marker and not body
        for marker, body in (
            _note_marker_and_body(str(record.get("text") or ""))
            for record in page_records
        )
    ):
        return []
    result: list[dict[str, Any]] = []
    for line in _source_page_text_lines(source_evidence, page_no):
        overlaps = [
            record
            for record in page_records
            if _bbox_overlap_ratio(line.get("bbox") or {}, record.get("bbox") or {}) > 0
        ]
        if not overlaps:
            continue
        text = line["text"]
        last_word = re.search(r"([A-Za-z]{3,})$", text)
        if last_word and any(
            re.search(
                re.escape(last_word.group(1)) + r"-",
                str(record.get("text") or ""),
                flags=re.IGNORECASE,
            )
            for record in overlaps
        ):
            text += "-"
        result.append(
            {
                "index": min(int(record["index"]) for record in overlaps),
                "page_no": page_no,
                "kind": "footnote",
                "text": text,
                "bbox": line["bbox"],
                "confidence": "high",
                "reasons": ["pdf_text_character_baseline"],
                "source_record_indexes": sorted(
                    {int(record["index"]) for record in overlaps}
                ),
            }
        )
    record_markers = {
        marker
        for marker, _body in (
            _note_marker_and_body(str(record.get("text") or ""))
            for record in page_records
        )
        if marker
    }
    source_markers = {
        marker
        for marker, _body in (
            _note_marker_and_body(str(record.get("text") or ""))
            for record in result
        )
        if marker
    }
    return result if record_markers.issubset(source_markers) else []


def _merge_cross_page_note_continuations(
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    removed: set[int] = set()
    for index, current in enumerate(groups):
        if (
            not current.get("marker")
            or not str(current.get("text") or "").rstrip().endswith("-")
        ):
            continue
        candidates = [
            (candidate_index, candidate)
            for candidate_index, candidate in enumerate(groups)
            if candidate_index not in removed
            and candidate.get("marker") is None
            and candidate.get("page_no") == int(current.get("page_no") or 0) + 1
            and re.match(r"^[a-z]", str(candidate.get("text") or "").lstrip())
        ]
        if len(candidates) == 1:
            candidate_index, next_group = candidates[0]
            current["text"] = _join_note_text(
                [str(current.get("text") or ""), str(next_group.get("text") or "")]
            )
            current["source_record_indexes"].extend(
                next_group.get("source_record_indexes") or []
            )
            current["source_fragments"].extend(
                next_group.get("source_fragments") or []
            )
            current["source_bboxes"].extend(
                next_group.get("source_bboxes") or []
            )
            current["continuation_pages"] = sorted(
                {
                    *(current.get("continuation_pages") or []),
                    next_group.get("page_no"),
                }
            )
            current["assembly_reason"] += "+cross_page_continuation"
            removed.add(candidate_index)
    return [
        group
        for index, group in enumerate(groups)
        if index not in removed
    ]


def _build_structural_note_groups(
    records: list[dict[str, Any]],
    source_evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    footnotes_by_page: dict[Any, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("kind") == "footnote":
            footnotes_by_page.setdefault(record.get("page_no"), []).append(record)
    for page_no, page_records in footnotes_by_page.items():
        source_records = _source_footnote_line_records(
            page_no,
            page_records,
            source_evidence or {"pages": {}},
        )
        ordered = sorted(
            source_records or page_records,
            key=lambda item: (
                -float((item.get("bbox") or {}).get("t", 0)),
                float((item.get("bbox") or {}).get("l", 0)),
                item["index"],
            ),
        )
        pending_unmarked: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        page_groups: list[dict[str, Any]] = []
        for record_index, record in enumerate(ordered):
            marker, body = _note_marker_and_body(str(record.get("text") or ""))
            if marker:
                prefix_records: list[dict[str, Any]] = []
                if (
                    pending_unmarked
                    and pending_unmarked[-1].get("text", "").rstrip().endswith("-")
                    and _same_note_column(pending_unmarked[-1], record)
                    and _vertically_adjacent_note_lines(pending_unmarked[-1], record)
                ):
                    prefix_records = pending_unmarked
                    pending_unmarked = []
                elif pending_unmarked:
                    for pending in pending_unmarked:
                        page_groups.append(
                            {
                                "marker": None,
                                "parts": [pending],
                                "reason": "unmarked_note_fragment",
                            }
                        )
                    pending_unmarked = []
                current = {
                    "marker": marker,
                    "parts": prefix_records + [record],
                    "reason": (
                        "marker_attached_to_continuation_line"
                        if prefix_records
                        else "explicit_marker"
                    ),
                }
                page_groups.append(current)
                continue
            next_record = (
                ordered[record_index + 1]
                if record_index + 1 < len(ordered)
                else None
            )
            next_marker = (
                _note_marker_and_body(str(next_record.get("text") or ""))[0]
                if next_record
                else None
            )
            if (
                next_record
                and next_marker
                and str(record.get("text") or "").rstrip().endswith("-")
                and _same_note_column(record, next_record)
                and _vertically_adjacent_note_lines(record, next_record)
            ):
                pending_unmarked.append(record)
                current = None
                continue
            if current and _same_note_column(current["parts"][-1], record) and _vertically_adjacent_note_lines(
                current["parts"][-1],
                record,
            ):
                current["parts"].append(record)
            else:
                pending_unmarked.append(record)
                current = None
        for pending in pending_unmarked:
            page_groups.append(
                {
                    "marker": None,
                    "parts": [pending],
                    "reason": "unmarked_note_fragment",
                }
            )
        marker_counts: dict[str, int] = {}
        for group in page_groups:
            marker = group["marker"]
            marker_key = _note_marker_slug(marker)
            marker_counts[marker_key] = marker_counts.get(marker_key, 0) + 1
            ordinal = marker_counts[marker_key]
            note_id = re.sub(
                r"[^a-z0-9]+",
                "-",
                f"docling-note-p{page_no}-{marker_key}-{ordinal}".lower(),
            ).strip("-")
            bodies = []
            for position, part in enumerate(group["parts"]):
                part_marker, part_body = _note_marker_and_body(str(part.get("text") or ""))
                bodies.append(part_body if part_marker else str(part.get("text") or "").strip())
                part["note_id"] = note_id
                part["note_marker"] = marker
                part["note_group_position"] = position
            groups.append(
                {
                    "note_id": note_id,
                    "page_no": page_no,
                    "marker": marker,
                    "text": _join_note_text(bodies),
                    "source_record_indexes": [part["index"] for part in group["parts"]],
                    "source_fragments": [part["text"] for part in group["parts"]],
                    "source_bboxes": [part.get("bbox") for part in group["parts"]],
                    "assembly_reason": group["reason"],
                    "confidence": (
                        "high"
                        if marker and all(part.get("confidence") == "high" for part in group["parts"])
                        else "unresolved"
                    ),
                }
            )
    return _merge_cross_page_note_continuations(groups)


def _pdf_inline_note_references(
    document_json: Any,
    source_evidence: dict[str, Any],
    note_groups: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records = structural_text_records(document_json)
    references: list[dict[str, Any]] = []
    marker_chars = set("*∗†‡0123456789")
    available_markers: dict[Any, set[str]] = {}
    for note in note_groups or []:
        marker = note.get("marker")
        if marker:
            available_markers.setdefault(note.get("page_no"), set()).add(str(marker))
    for page_no, page in (source_evidence.get("pages") or {}).items():
        median_size = float(page.get("median_font_size") or 0.0)
        page_characters = page.get("characters") or []
        small = [
            char
            for char in page_characters
            if str(char.get("text") or "") in marker_chars
            and median_size > 0
            and float(char.get("font_size") or 0.0) <= median_size * 0.78
        ]
        index = 0
        while index < len(small):
            group = [small[index]]
            while (
                index + 1 < len(small)
                and int(small[index + 1]["index"]) == int(group[-1]["index"]) + 1
                and abs(float(small[index + 1]["font_size"]) - float(group[-1]["font_size"])) < 0.2
                and str(group[-1].get("text") or "").isdigit()
                and str(small[index + 1].get("text") or "").isdigit()
            ):
                index += 1
                group.append(small[index])
            index += 1
            marker = "".join(str(char["text"]) for char in group).replace("∗", "*")
            if available_markers and marker not in available_markers.get(page_no, set()):
                continue
            bbox = _bbox_union([char["bbox"] for char in group]) or {}
            center_x = float(bbox.get("l", 0)) + float(bbox.get("width", 0)) / 2
            center_y = float(bbox.get("b", 0)) + float(bbox.get("height", 0)) / 2
            group_indexes = {int(char["index"]) for char in group}
            neighboring = [
                char
                for char in page_characters
                if int(char.get("index") or 0) not in group_indexes
                and 0 < min(
                    abs(int(char.get("index") or 0) - min(group_indexes)),
                    abs(int(char.get("index") or 0) - max(group_indexes)),
                ) <= 3
                and str(char.get("text") or "").strip()
                and float(char.get("font_size") or 0) > float(group[0].get("font_size") or 0)
            ]
            script_position = "small_inline"
            if neighboring:
                neighbor = min(
                    neighboring,
                    key=lambda char: min(
                        abs(int(char.get("index") or 0) - min(group_indexes)),
                        abs(int(char.get("index") or 0) - max(group_indexes)),
                    ),
                )
                neighbor_box = neighbor.get("bbox") or {}
                neighbor_height = max(
                    float(neighbor_box.get("t", 0)) - float(neighbor_box.get("b", 0)),
                    1.0,
                )
                if float(bbox.get("b", 0)) > float(neighbor_box.get("b", 0)) + neighbor_height * 0.15:
                    script_position = "superscript"
                elif float(bbox.get("t", 0)) < float(neighbor_box.get("t", 0)) - neighbor_height * 0.15:
                    script_position = "subscript"
            if script_position == "small_inline":
                continue
            eligible_records = [
                record
                for record in records
                if record.get("page_no") == page_no
                and not str(record.get("label") or "").lower().startswith("quarantined_")
                and str(record.get("label") or "").lower()
                not in {"page_header", "page_footer", "footnote", "formula"}
            ]
            candidates = [
                record
                for record in eligible_records
                if _point_inside_bbox(center_x, center_y, record.get("bbox"))
                and marker in str(record.get("text") or "").replace("∗", "*")
            ]
            anchor_mode = "replace_existing_marker"
            anchor_distance = None
            if len(candidates) == 1:
                record = candidates[0]
            else:
                nearby: list[tuple[float, dict[str, Any]]] = []
                for record in eligible_records:
                    if marker in str(record.get("text") or "").replace("∗", "*"):
                        continue
                    distance = _script_marker_record_distance(bbox, record.get("bbox"))
                    if distance is not None and distance <= 24:
                        nearby.append((distance, record))
                nearby.sort(key=lambda item: item[0])
                if not nearby:
                    continue
                if len(nearby) > 1 and abs(nearby[0][0] - nearby[1][0]) < 4:
                    continue
                anchor_distance, record = nearby[0]
                anchor_mode = "append_missing_marker"
            references.append(
                {
                    "page_no": page_no,
                    "marker": marker,
                    "bbox": bbox,
                    "font_size": group[0].get("font_size"),
                    "page_median_font_size": median_size,
                    "script_position": script_position,
                    "source": "pdf_text_character_size_and_bbox",
                    "confidence": "high",
                    "anchor_mode": anchor_mode,
                    "anchor_distance": anchor_distance,
                    "node_text": record.get("text"),
                    "reading_order": record.get("reading_order"),
                }
            )
    return references


def _html_inline_note_references(
    document_json: Any,
    document_html: str,
    note_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    available_markers: dict[Any, set[str]] = {}
    for note in note_groups:
        marker = str(note.get("marker") or "")
        if marker:
            available_markers.setdefault(note.get("page_no"), set()).add(marker)
    if not available_markers:
        return []

    records_by_text: dict[str, list[dict[str, Any]]] = {}
    for record in structural_text_records(document_json):
        label = str(record.get("label") or "").lower()
        if label.startswith("quarantined_") or label in {
            "page_header",
            "page_footer",
            "footnote",
            "formula",
        }:
            continue
        normalized = re.sub(
            r"\s*([*†‡])\s*",
            r"\1",
            _normalized_noise_text(str(record.get("text") or "")).replace(
                "∗",
                "*",
            ),
        )
        if normalized:
            records_by_text.setdefault(normalized, []).append(record)

    references: list[dict[str, Any]] = []
    sup_re = re.compile(r"<sup\b[^>]*>(?P<body>.*?)</sup>", re.I | re.S)
    for block in HTML_TEXT_BLOCK_RE.finditer(document_html):
        visible = re.sub(
            r"\s*([*†‡])\s*",
            r"\1",
            _normalized_noise_text(
                html.unescape(HTML_TAG_RE.sub("", block.group("body")))
            ).replace("∗", "*"),
        )
        matching_records = records_by_text.get(visible) or []
        if len(matching_records) != 1:
            continue
        record = matching_records[0]
        page_no = record.get("page_no")
        for sup in sup_re.finditer(block.group("body")):
            marker = _normalized_noise_text(
                html.unescape(HTML_TAG_RE.sub("", sup.group("body")))
            ).replace("∗", "*")
            if marker not in available_markers.get(page_no, set()):
                continue
            references.append(
                {
                    "page_no": page_no,
                    "marker": marker,
                    "bbox": None,
                    "font_size": None,
                    "page_median_font_size": None,
                    "script_position": "superscript",
                    "source": "final_html_sup_element_and_same_page_node",
                    "confidence": "high",
                    "node_text": record.get("text"),
                    "reading_order": record.get("reading_order"),
                }
            )
    return references


def _first_page_publication_note_references(
    document_json: Any,
    note_groups: list[dict[str, Any]],
    existing_references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_keys = {
        (reference.get("page_no"), str(reference.get("marker") or ""))
        for reference in existing_references
    }
    first_page_notes = [
        note for note in note_groups
        if note.get("page_no") == 1 and note.get("marker")
    ]
    marker_counts: dict[str, int] = {}
    for note in first_page_notes:
        marker = str(note.get("marker") or "")
        marker_counts[marker] = marker_counts.get(marker, 0) + 1
    publication_note_re = re.compile(
        r"\b(?:code|data|dataset|supplement|artifact|repository|github|"
        r"available\s+at|source\s+code)\b",
        re.I,
    )
    candidates = [
        note for note in first_page_notes
        if marker_counts.get(str(note.get("marker") or "")) == 1
        and (1, str(note.get("marker") or "")) not in existing_keys
        and publication_note_re.search(str(note.get("text") or ""))
    ]
    if not candidates:
        return []
    records = [
        record for record in structural_text_records(document_json)
        if record.get("page_no") == 1
        and not str(record.get("label") or "").lower().startswith("quarantined_")
        and str(record.get("label") or "").lower()
        not in {"page_header", "page_footer", "footnote", "formula"}
        and str(record.get("text") or "").strip()
    ]
    if not records:
        return []
    abstract_order = next(
        (
            record.get("reading_order")
            for record in records
            if str(record.get("label") or "").lower() == "section_header"
            and _normalized_noise_text(str(record.get("text") or "")).upper() == "ABSTRACT"
        ),
        None,
    )
    anchor_pool = [
        record for record in records
        if abstract_order is None or int(record.get("reading_order") or 0) < int(abstract_order)
    ] or records
    anchor = max(
        anchor_pool,
        key=lambda record: (
            float((record.get("bbox") or {}).get("t", 0)),
            -int(record.get("reading_order") or 0),
        ),
    )
    return [
        {
            "page_no": 1,
            "marker": str(note.get("marker") or ""),
            "bbox": None,
            "font_size": None,
            "page_median_font_size": None,
            "script_position": "missing_inline_marker",
            "source": "first_page_publication_note_fallback",
            "confidence": "medium",
            "anchor_mode": "append_missing_marker",
            "anchor_distance": None,
            "node_text": anchor.get("text"),
            "reading_order": anchor.get("reading_order"),
        }
        for note in candidates
    ]


def _map_note_references(
    note_groups: list[dict[str, Any]],
    references: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    note_lookup: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for note in note_groups:
        if note.get("marker"):
            note_lookup.setdefault((note.get("page_no"), note["marker"]), []).append(note)
    mappings: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ref_counts: dict[str, int] = {}
    for reference in references:
        matches = note_lookup.get((reference.get("page_no"), reference.get("marker")), [])
        if len(matches) != 1:
            unresolved.append(
                {
                    **reference,
                    "reason": (
                        "note_marker_not_found"
                        if not matches
                        else "ambiguous_note_marker"
                    ),
                    "candidate_note_ids": [item["note_id"] for item in matches],
                }
            )
            continue
        note = matches[0]
        ref_counts[note["note_id"]] = ref_counts.get(note["note_id"], 0) + 1
        reference_id = f"{note['note_id']}-ref-{ref_counts[note['note_id']]}"
        mappings.append(
            {
                **reference,
                "note_id": note["note_id"],
                "reference_id": reference_id,
                "mapping_evidence": [
                    "same_page",
                    "exact_marker",
                    "unique_note_candidate",
                    (
                        "first_page_publication_note_fallback"
                        if reference.get("source") == "first_page_publication_note_fallback"
                        else f"pdf_{reference.get('script_position')}_geometry"
                    ),
                ],
            }
        )
    return mappings, unresolved


def _link_note_references_in_html(
    document_html: str,
    mappings: list[dict[str, Any]],
) -> tuple[str, int]:
    updated = document_html
    count = 0
    by_node: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for mapping in mappings:
        by_node.setdefault(
            (mapping.get("page_no"), str(mapping.get("node_text") or "")),
            [],
        ).append(mapping)
    for (_page_no, node_text), node_mappings in by_node.items():
        target = _normalized_noise_text(node_text)
        for match in HTML_TEXT_BLOCK_RE.finditer(updated):
            visible = _normalized_noise_text(
                html.unescape(HTML_TAG_RE.sub("", match.group("body")))
            ).replace("∗", "*")
            visible = re.sub(r"\s*([*†‡])\s*", r"\1", visible)
            normalized_target = re.sub(
                r"\s*([*†‡])\s*",
                r"\1",
                target.replace("∗", "*"),
            )
            if visible != normalized_target:
                continue
            body = match.group("body")
            changed = 0
            for mapping in node_mappings:
                marker = str(mapping.get("marker") or "")
                linked = (
                    f'<sup id="{mapping["reference_id"]}" class="docling-note-ref">'
                    f'<a href="#{mapping["note_id"]}">{html.escape(marker)}</a></sup>'
                )
                marker_source_pattern = r"[∗*]" if marker == "*" else re.escape(html.escape(marker))
                sup_pattern = re.compile(
                    r"<sup\b[^>]*>\s*" + marker_source_pattern + r"\s*</sup>"
                )
                if sup_pattern.search(body):
                    body = sup_pattern.sub(linked, body, count=1)
                    changed += 1
                    continue
                marker_pattern = re.compile(
                    r"(?<![A-Za-z0-9])" + marker_source_pattern + r"(?![A-Za-z0-9])"
                )
                marker_matches = list(marker_pattern.finditer(body))
                if marker_matches:
                    selected = marker_matches[-1]
                    body = body[: selected.start()] + linked + body[selected.end() :]
                    changed += 1
                    continue
                if mapping.get("anchor_mode") == "append_missing_marker":
                    body = body.rstrip() + linked
                    changed += 1
            if not changed:
                break
            replacement = (
                f"<{match.group('tag')}{match.group('attrs')}>"
                f"{body}</{match.group('tag')}>"
            )
            updated = updated[: match.start()] + replacement + updated[match.end() :]
            count += changed
            break
    return updated, count


def _link_note_references_in_markdown(
    document_markdown: str,
    mappings: list[dict[str, Any]],
) -> tuple[str, int]:
    def visible_markdown_text(value: str) -> str:
        visible = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
        visible = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", visible)
        visible = re.sub(r"\*\*(.*?)\*\*", r"\1", visible, flags=re.S)
        visible = re.sub(r"__(.*?)__", r"\1", visible, flags=re.S)
        visible = re.sub(
            r"(?<!\*)\*(\S(?:[^*\n]*?\S)?)\*(?!\*)",
            r"\1",
            visible,
        )
        visible = re.sub(
            r"(?<!_)_(\S(?:[^_\n]*?\S)?)_(?!_)",
            r"\1",
            visible,
        )
        visible = html.unescape(HTML_TAG_RE.sub(" ", visible))
        visible = re.sub(
            r"^\s{0,3}(?:#{1,6}\s+|[-+]\s+|\d+[.)]\s+)",
            "",
            visible,
        )
        visible = _normalized_noise_text(visible).replace("∗", "*")
        return re.sub(r"\s*([*†‡])\s*", r"\1", visible)

    updated = document_markdown
    count = 0
    by_node: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for mapping in mappings:
        by_node.setdefault(
            (mapping.get("page_no"), str(mapping.get("node_text") or "")),
            [],
        ).append(mapping)
    for (_page_no, node_text), node_mappings in by_node.items():
        normalized_target = _normalized_noise_text(node_text).replace("∗", "*")
        normalized_target = re.sub(r"\s*([*†‡])\s*", r"\1", normalized_target)
        block_match = None
        candidates = list(
            re.finditer(
                r"(?P<prefix>\A|\n[ \t]*\n)(?P<body>.*?)(?=\n[ \t]*\n|\Z)",
                updated,
                flags=re.S,
            )
        )
        candidates.extend(
            re.finditer(r"(?m)^(?P<body>[^\n]+)$", updated)
        )
        for candidate in candidates:
            if visible_markdown_text(candidate.group("body")) == normalized_target:
                block_match = candidate
                break
        if block_match is None:
            continue
        body = block_match.group("body")
        replacements: list[tuple[int, int, str]] = []
        mappings_by_marker: dict[str, list[dict[str, Any]]] = {}
        for mapping in node_mappings:
            mappings_by_marker.setdefault(str(mapping.get("marker") or ""), []).append(mapping)
        for marker, marker_mappings in mappings_by_marker.items():
            marker_source_pattern = r"[∗*]" if marker == "*" else re.escape(marker)
            marker_pattern = re.compile(
                r"(?<![A-Za-z0-9_*])"
                + marker_source_pattern
                + r"(?![A-Za-z0-9_*])"
            )
            positions = list(marker_pattern.finditer(body))
            for mapping, position in zip(
                reversed(marker_mappings),
                reversed(positions),
            ):
                linked = (
                    f'<sup id="{mapping["reference_id"]}">'
                    f'<a href="#{mapping["note_id"]}">{html.escape(marker)}</a></sup>'
                )
                replacements.append((position.start(), position.end(), linked))
            missing_marker_mappings = marker_mappings[: max(0, len(marker_mappings) - len(positions))]
            for mapping in missing_marker_mappings:
                if mapping.get("anchor_mode") != "append_missing_marker":
                    continue
                linked = (
                    f'<sup id="{mapping["reference_id"]}">'
                    f'<a href="#{mapping["note_id"]}">{html.escape(marker)}</a></sup>'
                )
                end = len(body.rstrip())
                replacements.append((end, end, linked))
        if not replacements:
            continue
        for start, end, replacement in sorted(replacements, reverse=True):
            body = body[:start] + replacement + body[end:]
        updated = (
            updated[: block_match.start("body")]
            + body
            + updated[block_match.end("body") :]
        )
        count += len(replacements)
    return updated, count


def _citation_numbers(citation_body: str) -> list[int]:
    numbers: list[int] = []
    parts = re.split(r"\s*[,;，；、]\s*", citation_body.strip())
    for part in parts:
        if not part:
            return []
        range_match = re.fullmatch(r"(\d{1,3})\s*(?:[\u2013\u2014~～-])\s*(\d{1,3})", part)
        if range_match:
            start, end = (int(range_match.group(1)), int(range_match.group(2)))
            if start <= end and end - start <= 100:
                numbers.extend(range(start, end + 1))
            continue
        if part.isdigit():
            numbers.append(int(part))
            continue
        return []
    return list(dict.fromkeys(numbers))


def _ocr_citation_digit_text(raw: str) -> str:
    replacements = {
        "!": "1",
        "！": "1",
        '"': "1",
        "'": "1",
        "“": "1",
        "”": "1",
        "|": "1",
        "I": "1",
        "l": "1",
        "O": "0",
        "o": "0",
        "〇": "0",
        "S": "5",
        "s": "5",
        "Ｂ": "8",
        "B": "8",
    }
    return "".join(
        replacements.get(char, char)
        for char in raw
        if char.isdigit() or char in replacements
    )


def _numbers_from_malformed_citation(
    raw: str,
    reference_lookup: dict[int, dict[str, Any]],
    semantic_number: int | None = None,
) -> list[int]:
    digit_text = _ocr_citation_digit_text(raw)
    candidates: list[int] = []
    if digit_text.isdigit():
        candidates.append(int(digit_text))
        if len(digit_text) == 2:
            candidates.append(int(digit_text[::-1]))
    if semantic_number is not None:
        has_clear_bracketed_number = bool(
            re.search(r"[\[［〔【「]\s*\d{1,3}\s*[\]］〕】」）)]", raw)
        )
        if (
            not has_clear_bracketed_number
            or not candidates
            or candidates[0] not in reference_lookup
        ):
            candidates.insert(0, semantic_number)
    result = [
        number
        for number in candidates
        if number in reference_lookup
    ]
    return list(dict.fromkeys(result[:1]))


def _reference_alias_index(
    references: list[dict[str, Any]],
) -> dict[str, int]:
    aliases: dict[str, set[int]] = {}

    def add(alias: str, number: int) -> None:
        normalized = re.sub(r"[\s.]+", "", alias).upper()
        if len(normalized) < 2:
            return
        aliases.setdefault(normalized, set()).add(number)

    stopwords = {"BASED", "AIDED", "DRIVEN", "ENHANCED", "WITH", "AND", "OF", "THE"}
    for reference in references:
        number = int(reference["number"])
        text = str(reference.get("text") or "")
        chinese_author = re.match(r"\s*([\u3400-\u9fff]{2,5})[，,、]", text)
        if chinese_author:
            add(chinese_author.group(1), number)
        english_author = re.match(r"\s*([A-Z][A-Za-z-]{1,24})\b", text)
        if english_author:
            add(english_author.group(1), number)
        for acronym in re.findall(
            r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]*(?:[-.][A-Z0-9]+)+)(?![A-Za-z0-9])",
            text,
        ):
            add(acronym, number)
        for phrase in re.findall(
            r"([A-Z][A-Za-z-]+(?:\s+[A-Za-z-]+){0,6}\s+knowledge\s+tracing)",
            text,
            flags=re.I,
        ):
            words = re.findall(r"[A-Za-z]+", phrase)
            acronym = "".join(
                word[0].upper()
                for word in words
                if word.upper() not in stopwords
            )
            add(acronym, number)
    return {
        alias: next(iter(numbers))
        for alias, numbers in aliases.items()
        if len(numbers) == 1
    }


def _reference_author_year_index(
    references: list[dict[str, Any]],
) -> dict[tuple[str, str], int]:
    keys: dict[tuple[str, str], set[int]] = {}

    def add(alias: str, year: str, number: int) -> None:
        normalized = re.sub(r"[^A-Za-z'’-]+", " ", alias).strip()
        if not normalized:
            return
        tokens = normalized.split()
        if not tokens:
            return
        candidate = " ".join(tokens).replace("’", "'").upper()
        if len(candidate) < 2:
            return
        keys.setdefault((candidate, year.lower()), set()).add(number)

    def author_aliases(reference_text: str, year_start: int) -> list[str]:
        leading = reference_text[:year_start]
        leading = re.sub(r"^\s*(?:[\[［〔]?\d{1,3}[\]］〕.)、]?\s*)", "", leading)
        first_author = re.split(r"\s+(?:and|&)\s+|;", leading, maxsplit=1)[0]
        first_author = first_author.split(",", 1)[0]
        first_author = re.sub(r"\b[A-Z]\.\s*", " ", first_author)
        first_author = re.sub(r"\b[A-Z]\s+", " ", first_author)
        words = re.findall(r"[A-Z][A-Za-z'’.-]{1,40}", first_author)
        aliases: list[str] = []
        if words:
            for width in range(1, min(3, len(words)) + 1):
                aliases.append(" ".join(words[-width:]))
        old_match = re.match(
            r"\s*(?:[A-Z]\.\s+)?([A-Z][A-Za-z'’.-]{1,40})\b",
            reference_text,
        )
        if old_match:
            aliases.append(old_match.group(1))
        return list(dict.fromkeys(aliases))

    for reference in references:
        number = int(reference["number"])
        text = str(reference.get("text") or "")
        years = list(re.finditer(r"\b((?:19|20)\d{2}[a-z]?)\b", text))
        if not years:
            continue
        aliases = author_aliases(text, years[0].start())
        for year_match in years:
            year = year_match.group(1)
            for alias in aliases:
                add(alias, year, number)
    return {
        key: next(iter(numbers))
        for key, numbers in keys.items()
        if len(numbers) == 1
    }


def _author_year_numbers(
    citation_body: str,
    author_year_lookup: dict[tuple[str, str], int],
) -> list[int]:
    numbers: list[int] = []

    def lookup_numbers_for_part(part: str) -> list[int]:
        year_matches = list(re.finditer(r"\b((?:19|20)\d{2}[a-z]?)\b", part))
        if not year_matches:
            return []
        author_part = part[: year_matches[0].start()].strip(" ,")
        author_part = re.sub(r"\bet\s+al\.?\s*$", "", author_part, flags=re.I).strip()
        author_part = re.split(r"\s+(?:and|&)\s+", author_part, maxsplit=1)[0]
        author_part = re.sub(r"[^A-Za-z'’.-]+", " ", author_part).strip()
        if not author_part:
            return []
        author_tokens = re.findall(r"[A-Za-z'’.-]{2,40}", author_part)
        aliases = [
            " ".join(author_part.split()[-width:])
            for width in range(1, min(3, len(author_part.split())) + 1)
        ]
        for token in author_tokens:
            if token.lower().rstrip(".") not in {"and", "et", "al"}:
                aliases.insert(0, token)
                break
        aliases = list(dict.fromkeys(aliases))
        result: list[int] = []
        for year_match in year_matches:
            year = year_match.group(1).lower()
            matched_number = None
            for alias in aliases:
                key = (alias.replace("’", "'").upper(), year)
                matched_number = author_year_lookup.get(key)
                if matched_number is not None:
                    break
            if matched_number is None:
                return []
            result.append(matched_number)
        return result

    for part in re.split(r"\s*;\s*", citation_body):
        subparts = re.split(
            r",\s*(?=[A-Z][A-Za-z'’.-]{1,40}"
            r"(?:\s+(?:et\s+al\.?|and|&|[A-Z][A-Za-z'’.-]{1,40})){0,8}"
            r"\s*,\s*(?:19|20)\d{2}[a-z]?\b)",
            part,
        )
        for subpart in subparts:
            part_numbers = lookup_numbers_for_part(subpart)
            if not part_numbers:
                return []
            numbers.extend(part_numbers)
    return list(dict.fromkeys(numbers))


def _general_bracket_citation_numbers(
    body: str,
    reference_lookup: dict[int, dict[str, Any]],
    author_year_lookup: dict[tuple[str, str], int],
) -> tuple[list[int], list[str], str] | None:
    cleaned = _normalized_noise_text(body)
    if not cleaned:
        return None
    numeric_numbers = _citation_numbers(cleaned)
    if numeric_numbers and all(number in reference_lookup for number in numeric_numbers):
        return (
            numeric_numbers,
            [
                "reference_section_detected",
                "general_bracket_numeric_citation",
                "reference_number_exists",
            ],
            cleaned,
        )
    author_year_numbers = _author_year_numbers(cleaned, author_year_lookup)
    if author_year_numbers:
        return (
            author_year_numbers,
            [
                "reference_section_detected",
                "author_year_citation",
                "unique_author_year_reference",
            ],
            cleaned,
        )
    return None


def _numeric_bracket_context_allows_citation(
    text: str,
    start: int,
    end: int,
) -> bool:
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 12)]
    previous = before.rstrip()[-1:] if before.rstrip() else ""
    following = after.lstrip()[:1] if after.lstrip() else ""
    immediate_previous = text[start - 1 : start] if start > 0 else ""
    if immediate_previous.isdigit():
        return False
    if re.search(r"(?:[A-Za-z]\s*[∈∊∋=<>≤≥]|[=<>≤≥∈∊∋]\s*)$", before):
        return False
    if immediate_previous.isalpha() and re.match(r"^\s*[,;，；、)\]］〕）}]", after):
        return False
    if previous in "([{（｛,;，；:：=+-*/×÷" and following.isdigit():
        return False
    return True


def _citation_candidates_for_text(
    text: str,
    reference_lookup: dict[int, dict[str, Any]],
    alias_lookup: dict[str, int],
    author_year_lookup: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < existing_end and end > existing_start for existing_start, existing_end in occupied)

    def add_candidate(
        start: int,
        end: int,
        raw: str,
        numbers: list[int],
        evidence: list[str],
        display_body: str | None = None,
    ) -> None:
        if not raw or not numbers or overlaps(start, end):
            return
        candidates.append(
            {
                "start": start,
                "end": end,
                "raw_citation": raw,
                "citation_body": display_body or ",".join(str(number) for number in numbers),
                "numbers": numbers,
                "mapping_evidence": evidence,
            }
        )
        occupied.append((start, end))

    for match in GENERAL_BRACKET_CITATION_RE.finditer(text):
        open_char = match.group("open")
        close_char = match.group("close")
        body = match.group("body")
        parsed = _general_bracket_citation_numbers(
            body,
            reference_lookup,
            author_year_lookup or {},
        )
        if parsed is None:
            continue
        numbers, evidence, display_body = parsed
        if (
            "general_bracket_numeric_citation" in evidence
            and not _numeric_bracket_context_allows_citation(
                text,
                match.start(),
                match.end(),
            )
        ):
            continue
        add_candidate(
            match.start(),
            match.end(),
            match.group(0),
            numbers,
            evidence,
            display_body,
        )
    for match in MISSING_OPEN_BRACKET_RANGE_CITATION_RE.finditer(text):
        body = match.group("body")
        numbers = _citation_numbers(_normalized_noise_text(body))
        if (
            not numbers
            or any(number not in reference_lookup for number in numbers)
            or not _numeric_bracket_context_allows_citation(
                text,
                match.start(),
                match.end(),
            )
        ):
            continue
        add_candidate(
            match.start(),
            match.end(),
            match.group(0),
            numbers,
            [
                "reference_section_detected",
                "ocr_missing_open_citation_bracket",
                "general_bracket_numeric_citation",
                "reference_number_exists",
            ],
            _normalized_noise_text(body),
        )
    for match in INLINE_CITATION_RE.finditer(text):
        raw = match.group(0)
        body = _citation_match_body(match)
        if not _numeric_bracket_context_allows_citation(
            text,
            match.start(),
            match.end(),
        ):
            continue
        add_candidate(
            match.start(),
            match.end(),
            raw,
            _citation_numbers(body),
            ["reference_section_detected", "citation_bracket_syntax", "reference_number_exists"],
        )
    for match in AUTHOR_YEAR_CITATION_RE.finditer(text):
        body = match.group("body")
        numbers = _author_year_numbers(body, author_year_lookup or {})
        add_candidate(
            match.start(),
            match.end(),
            match.group(0),
            numbers,
            ["reference_section_detected", "author_year_citation", "unique_author_year_reference"],
            body,
        )
    for match in NARRATIVE_AUTHOR_YEAR_CITATION_RE.finditer(text):
        body = f"{match.group('author')}, {match.group('year')}"
        numbers = _author_year_numbers(body, author_year_lookup or {})
        add_candidate(
            match.start(),
            match.end(),
            match.group(0),
            numbers,
            [
                "reference_section_detected",
                "author_year_citation",
                "narrative_author_year_citation",
                "unique_author_year_reference",
            ],
            body,
        )
    for match in MALFORMED_CITATION_TOKEN_RE.finditer(text):
        raw = match.group(0)
        if (
            match.end() < len(text)
            and text[match.end()].isalpha()
            and re.search(r"[A-Za-z]", raw)
        ):
            continue
        if not _numeric_bracket_context_allows_citation(
            text,
            match.start(),
            match.end(),
        ):
            continue
        add_candidate(
            match.start(),
            match.end(),
            raw,
            _numbers_from_malformed_citation(raw, reference_lookup),
            ["reference_section_detected", "ocr_malformed_citation_token", "reference_number_exists"],
        )
    for match in AUTHOR_CITATION_ALIAS_RE.finditer(text):
        alias = re.sub(r"[\s.]+", "", match.group("alias")).upper()
        semantic_number = alias_lookup.get(alias)
        raw = match.group("raw") or ""
        if semantic_number is None or not raw.strip():
            continue
        add_candidate(
            match.start("raw"),
            match.end("raw"),
            raw,
            _numbers_from_malformed_citation(raw, reference_lookup, semantic_number),
            ["reference_section_detected", "unique_author_alias", "reference_number_exists"],
        )
    for match in MODEL_CITATION_ALIAS_RE.finditer(text):
        alias = re.sub(r"[\s.]+", "", match.group("alias")).upper()
        semantic_number = alias_lookup.get(alias)
        raw = match.group("raw") or ""
        if semantic_number is None or not raw.strip():
            continue
        add_candidate(
            match.start("raw"),
            match.end("raw"),
            raw,
            _numbers_from_malformed_citation(raw, reference_lookup, semantic_number),
            ["reference_section_detected", "unique_model_alias", "reference_number_exists"],
        )
    return sorted(candidates, key=lambda item: (item["start"], item["end"]))


def _reference_section_records(document_json: Any) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
]:
    records = structural_text_records(document_json)
    heading_index: int | None = None
    heading: dict[str, Any] | None = None
    for index, record in enumerate(records):
        if (
            str(record.get("label") or "").lower() == "section_header"
            and BIBLIOGRAPHY_HEADING_RE.fullmatch(str(record.get("text") or ""))
        ):
            heading_index = index
            heading = record
            break
    if heading_index is None:
        return None, []
    result: list[dict[str, Any]] = []
    for record in records[heading_index + 1 :]:
        label = str(record.get("label") or "").lower()
        text = str(record.get("text") or "")
        explicit_number, _body = _explicit_reference_number(text, record)
        numbered_reference_heading = label == "section_header" and explicit_number is not None
        if label == "section_header" and not numbered_reference_heading:
            break
        if label == "list_item" or numbered_reference_heading:
            result.append(record)
    return heading, result


def _citation_match_body(match: re.Match[str]) -> str:
    return (
        match.group("body")
        or match.group("mixed_body")
        or match.group("open_only_body")
        or ""
    )


def _explicit_reference_number(
    text: str,
    record: dict[str, Any] | None = None,
) -> tuple[int | None, str]:
    node = (record or {}).get("node")
    marker = str(node.get("marker") or "") if isinstance(node, dict) else ""
    marker_match = re.fullmatch(r"\s*[\[［〔]?(\d{1,3})[\]］〕.)、]?\s*", marker)
    if marker_match:
        return int(marker_match.group(1)), text.strip()
    original = str(node.get("orig") or "") if isinstance(node, dict) else ""
    source = original or text
    match = re.match(
        r"^\s*(?:"
        r"[\[［〔](\d{1,3})[\]］〕]|"
        r"[LlI|](\d{1,3})[」］〕]|"
        r"(\d{1,3})[.)、]"
        r")\s*(.*)$",
        source,
        re.S,
    )
    if not match:
        return None, text
    number = int(match.group(1) or match.group(2) or match.group(3))
    body = match.group(4).strip()
    return number, body


def bibliography_diagnostics(document_json: Any) -> dict[str, Any]:
    heading, records = _reference_section_records(document_json)
    if heading is None or not records:
        return {
            "available": False,
            "reason": "reference_section_not_found",
            "reference_count": 0,
            "citation_count": 0,
            "references": [],
            "citations": [],
            "unresolved_citations": [],
        }

    references: list[dict[str, Any]] = []
    explicit_numbers = [
        number
        for number, _body in (
            _explicit_reference_number(str(record.get("text") or ""), record)
            for record in records
        )
        if number is not None
    ]
    explicit_mode = bool(explicit_numbers)
    for list_index, record in enumerate(records):
        text = str(record.get("text") or "")
        explicit_number, body = _explicit_reference_number(text, record)
        previous = references[-1] if references else None
        cross_page_continuation = bool(
            previous
            and record.get("page_no") != previous.get("page_end")
            and explicit_number is None
            and REFERENCE_CONTINUATION_START_RE.match(text)
            and not REFERENCE_TRAILING_USAGE_PAGES_RE.search(
                str(previous.get("text") or "")
            )
        )
        if cross_page_continuation:
            previous["text"] = _join_note_text([str(previous["text"]), text])
            previous["source_texts"].append(text)
            previous["source_list_indexes"].append(list_index)
            previous["source_reading_orders"].append(record.get("reading_order"))
            previous["source_bboxes"].append(record.get("bbox"))
            previous["page_end"] = record.get("page_no")
            previous["continuation_count"] += 1
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {})["bibliography_reference"] = {
                    "reference_number": previous["number"],
                    "role": "cross_page_continuation",
                    "reference_id": previous["reference_id"],
                }
            continue
        if explicit_mode and explicit_number is None:
            continue
        number = explicit_number if explicit_number is not None else len(references) + 1
        reference_id = f"docling-reference-{number}"
        entry = {
            "number": number,
            "reference_id": reference_id,
            "text": body,
            "source_texts": [text],
            "source_list_indexes": [list_index],
            "source_reading_orders": [record.get("reading_order")],
            "source_bboxes": [record.get("bbox")],
            "page_start": record.get("page_no"),
            "page_end": record.get("page_no"),
            "continuation_count": 0,
            "number_source": "explicit_marker" if explicit_number is not None else "reference_list_order",
        }
        references.append(entry)
        node = record.get("node")
        if isinstance(node, dict):
            node.setdefault("local_ai_lab_qc", {})["bibliography_reference"] = {
                "reference_number": number,
                "role": "entry_start",
                "reference_id": reference_id,
            }

    reference_lookup = {item["number"]: item for item in references}
    alias_lookup = _reference_alias_index(references)
    author_year_lookup = _reference_author_year_index(references)
    reference_orders = {
        order
        for reference in references
        for order in reference["source_reading_orders"]
    }
    all_text_records = structural_text_records(document_json)
    if heading.get("reading_order") is not None:
        in_reference_section = False
        for record in all_text_records:
            if record.get("reading_order") == heading.get("reading_order"):
                in_reference_section = True
                reference_orders.add(record.get("reading_order"))
                continue
            label = str(record.get("label") or "").lower()
            explicit_number, _body = _explicit_reference_number(str(record.get("text") or ""), record)
            numbered_reference_heading = label == "section_header" and explicit_number is not None
            if (
                in_reference_section
                and label == "section_header"
                and not numbered_reference_heading
            ):
                break
            if in_reference_section and label in {
                "section_header",
                "list_item",
            }:
                reference_orders.add(record.get("reading_order"))
    citations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    occurrence_counts: dict[int, int] = {}
    for record in all_text_records:
        if record.get("reading_order") in reference_orders:
            continue
        label = str(record.get("label") or "").lower()
        if label.startswith("quarantined_") or label in {
            "formula",
            "table",
            "page_header",
            "page_footer",
            "footnote",
        }:
            continue
        text = str(record.get("text") or "")
        for citation_candidate in _citation_candidates_for_text(
            text,
            reference_lookup,
            alias_lookup,
            author_year_lookup,
        ):
            raw = citation_candidate["raw_citation"]
            citation_body = citation_candidate["citation_body"]
            numbers = citation_candidate["numbers"]
            missing = [number for number in numbers if number not in reference_lookup]
            if not numbers or missing:
                unresolved.append(
                    {
                        "page_no": record.get("page_no"),
                        "reading_order": record.get("reading_order"),
                        "node_text": text,
                        "raw_citation": raw,
                        "parsed_numbers": numbers,
                        "missing_reference_numbers": missing,
                        "reason": (
                            "citation_syntax_not_supported"
                            if not numbers
                            else "reference_number_not_found"
                        ),
                    }
                )
                continue
            links = []
            for number in numbers:
                occurrence_counts[number] = occurrence_counts.get(number, 0) + 1
                citation_id = (
                    f"docling-citation-{number}-{occurrence_counts[number]}"
                )
                links.append(
                    {
                        "number": number,
                        "reference_id": reference_lookup[number]["reference_id"],
                        "citation_id": citation_id,
                    }
                )
                reference_lookup[number].setdefault("backlink_ids", []).append(
                    citation_id
                )
            citation = {
                "page_no": record.get("page_no"),
                "reading_order": record.get("reading_order"),
                "node_text": text,
                "raw_citation": raw,
                "citation_body": citation_body,
                "numbers": numbers,
                "links": links,
                "confidence": "high",
                "mapping_evidence": citation_candidate["mapping_evidence"],
            }
            citations.append(citation)
            node = record.get("node")
            if isinstance(node, dict):
                node.setdefault("local_ai_lab_qc", {}).setdefault(
                    "bibliography_citations",
                    [],
                ).append(
                    {
                        "raw_citation": raw,
                        "numbers": numbers,
                        "reference_ids": [
                            link["reference_id"] for link in links
                        ],
                    }
                )

    return {
        "available": True,
        "reason": None,
        "heading": heading.get("text"),
        "heading_page_no": heading.get("page_no"),
        "numbering_mode": "explicit" if explicit_mode else "reference_list_order",
        "reference_count": len(references),
        "citation_count": len(citations),
        "linked_number_count": sum(len(item["links"]) for item in citations),
        "unresolved_citation_count": len(unresolved),
        "references": references,
        "citations": citations,
        "unresolved_citations": unresolved,
    }


def _linked_citation_html(citation: dict[str, Any]) -> str:
    link_lookup = {item["number"]: item for item in citation["links"]}
    body = str(citation.get("citation_body") or "")
    raw = str(citation.get("raw_citation") or "")
    open_wrapper = "["
    close_wrapper = "]"
    wrapper_match = GENERAL_BRACKET_CITATION_RE.fullmatch(raw)
    if wrapper_match:
        open_wrapper = wrapper_match.group("open")
        close_wrapper = wrapper_match.group("close")
    if "author_year_citation" in (citation.get("mapping_evidence") or []):
        if "narrative_author_year_citation" in (citation.get("mapping_evidence") or []):
            link = citation["links"][0] if citation.get("links") else None
            if not link:
                return html.escape(raw)
            return (
                f'<a id="{link["citation_id"]}" class="docling-citation" '
                f'href="#{link["reference_id"]}">{html.escape(raw)}</a>'
            )
        linked_parts: list[str] = []
        parts = re.split(
            r"(\s*;\s*|\s*,\s*(?=[A-Z][A-Za-z'’.-]{1,40}"
            r"(?:\s+(?:et\s+al\.?|and|&|[A-Z][A-Za-z'’.-]{1,40})){0,8}"
            r"\s*,\s*(?:19|20)\d{2}[a-z]?\b))",
            body,
        )
        visible_part_count = sum(1 for part in parts if part and not re.fullmatch(r"\s*[;,]\s*", part))
        number_index = 0
        for part in parts:
            if re.fullmatch(r"\s*[;,]\s*", part):
                linked_parts.append(part)
                continue
            if not part:
                continue
            number = (
                citation["numbers"][number_index]
                if number_index < len(citation.get("numbers") or [])
                else None
            )
            number_index += 1
            link = link_lookup.get(number)
            if not link:
                linked_parts.append(html.escape(part))
                continue
            hidden_targets = ""
            extra_numbers = (
                citation.get("numbers", [])[number_index:]
                if visible_part_count == 1
                else []
            )
            for extra_number in extra_numbers:
                extra_link = link_lookup.get(extra_number)
                if extra_link:
                    hidden_targets += (
                        f'<a id="{extra_link["citation_id"]}" '
                        f'class="docling-citation docling-citation-hidden-target" '
                        f'href="#{extra_link["reference_id"]}" '
                        f'aria-label="Reference {extra_number}"></a>'
                    )
                    number_index += 1
            linked_parts.append(
                f'<a id="{link["citation_id"]}" class="docling-citation" '
                f'href="#{link["reference_id"]}">{html.escape(part.strip())}</a>'
                f"{hidden_targets}"
            )
        return html.escape(open_wrapper) + "".join(linked_parts) + html.escape(close_wrapper)

    used_numbers: set[int] = set()

    def replace_number(match: re.Match[str]) -> str:
        number = int(match.group())
        link = link_lookup.get(number)
        if not link:
            return match.group()
        used_numbers.add(number)
        return (
            f'<a id="{link["citation_id"]}" class="docling-citation" '
            f'href="#{link["reference_id"]}">{number}</a>'
        )

    linked_body = re.sub(r"\d{1,3}", replace_number, body)
    hidden_targets = "".join(
        f'<a id="{link["citation_id"]}" class="docling-citation docling-citation-hidden-target" '
        f'href="#{link["reference_id"]}" aria-label="Reference {number}"></a>'
        for number, link in link_lookup.items()
        if number not in used_numbers
    )
    return html.escape(open_wrapper) + linked_body + hidden_targets + html.escape(close_wrapper)


def _citation_visible_context_matches(
    visible_text: str,
    node_text: str,
    raw_citation: str,
) -> bool:
    visible = _normalized_noise_text(visible_text)
    target = _normalized_noise_text(node_text)
    if visible == target:
        return True
    before, separator, after = node_text.partition(raw_citation)
    if not separator:
        return False
    before_words = _normalized_noise_text(before).split()[-6:]
    after_words = _normalized_noise_text(after).split()[:6]
    context_checks = [
        " ".join(words)
        for words in (before_words, after_words)
        if len(" ".join(words)) >= 12
    ]
    return bool(context_checks) and all(context in visible for context in context_checks)


def _link_bibliography_in_html(
    document_html: str,
    diagnostics: dict[str, Any],
) -> tuple[str, int, int]:
    if not diagnostics.get("available"):
        return document_html, 0, 0
    updated = document_html
    bibliography_style = """
<style id="docling-bibliography-link-style">
.docling-citation,
.docling-reference-backlink {
  text-decoration: none;
}
.docling-reference-entry:target {
  outline: 2px solid #2563eb;
  outline-offset: 3px;
}
.docling-reference-number {
  font-weight: 600;
  margin-right: .35rem;
}
.docling-citation-hidden-target {
  display: inline-block;
  width: 0;
  height: 0;
  overflow: hidden;
}
.docling-reference-backlinks {
  display: inline-flex;
  gap: .35rem;
  margin-left: .5rem;
}
</style>
"""
    if "docling-bibliography-link-style" not in updated:
        if "</head>" in updated:
            updated = updated.replace(
                "</head>",
                bibliography_style + "\n</head>",
                1,
            )
        else:
            updated = bibliography_style + "\n" + updated
    heading_match = next(
        (
            match
            for match in HTML_TEXT_BLOCK_RE.finditer(updated)
            if match.group("tag").lower().startswith("h")
            and BIBLIOGRAPHY_HEADING_RE.fullmatch(
                _normalized_noise_text(
                    html.unescape(HTML_TAG_RE.sub(" ", match.group("body")))
                )
            )
        ),
        None,
    )
    reference_matches: list[re.Match[str]] = []
    if heading_match:
        for match in HTML_TEXT_BLOCK_RE.finditer(updated, heading_match.end()):
            tag = match.group("tag").lower()
            visible = _normalized_noise_text(
                html.unescape(HTML_TAG_RE.sub(" ", match.group("body")))
            )
            explicit_number, _body = _explicit_reference_number(visible)
            numbered_reference_heading = tag.startswith("h") and explicit_number is not None
            if tag.startswith("h") and not numbered_reference_heading:
                break
            if tag == "li" or numbered_reference_heading:
                reference_matches.append(match)

    reference_replacements: list[tuple[int, int, str]] = []
    anchored_reference_ids: set[str] = set()
    for reference in diagnostics["references"]:
        source_indexes = reference.get("source_list_indexes") or []
        if not source_indexes:
            continue
        source_index = int(source_indexes[0])
        if source_index >= len(reference_matches):
            continue
        match = reference_matches[source_index]
        attrs = re.sub(r'\s+\bid\s*=\s*"[^"]*"', "", match.group("attrs"))
        class_match = re.search(r'\bclass\s*=\s*"([^"]*)"', attrs)
        if class_match:
            classes = class_match.group(1).split()
            if "docling-reference-entry" not in classes:
                classes.append("docling-reference-entry")
            joined_classes = " ".join(classes)
            attrs = (
                attrs[: class_match.start()]
                + f'class="{joined_classes}"'
                + attrs[class_match.end() :]
            )
        else:
            attrs += ' class="docling-reference-entry"'
        backlinks = " ".join(
            f'<a class="docling-reference-backlink" href="#{backlink_id}" '
            f'aria-label="Back to citation {reference["number"]}">↩</a>'
            for backlink_id in reference.get("backlink_ids") or []
        )
        reference_body = re.sub(
            r'^\s*<span\b[^>]*class="docling-reference-number"[^>]*>'
            r".*?</span>\s*",
            "",
            match.group("body"),
            count=1,
            flags=re.I | re.S,
        )
        reference_body = re.sub(
            r'\s*<span\b[^>]*class="docling-reference-backlinks"[^>]*>'
            r".*?</span>\s*$",
            "",
            reference_body,
            count=1,
            flags=re.I | re.S,
        )
        original_marker_present = bool(
            re.search(
                r"list-style-type\s*:\s*['\"]?\s*"
                r"(?:\\?\[|［|〔)\s*"
                + re.escape(str(reference["number"]))
                + r"\s*(?:\\?\]|］|〕)",
                attrs,
                flags=re.I,
            )
            or re.match(
                r"^\s*(?:"
                r"[\[［〔]\s*"
                + re.escape(str(reference["number"]))
                + r"\s*[\]］〕]|"
                r"[LlI|]\s*"
                + re.escape(str(reference["number"]))
                + r"\s*[」］〕]"
                r")",
                html.unescape(HTML_TAG_RE.sub("", reference_body)),
            )
        )
        visible_number = (
            ""
            if original_marker_present
            else (
                f'<span class="docling-reference-number">'
                f'[{reference["number"]}]</span> '
            )
        )
        body = (
            visible_number
            + reference_body
            + f'<span class="docling-reference-backlinks">{backlinks}</span>'
        )
        tag = match.group("tag")
        reference_replacements.append(
            (
                match.start(),
                match.end(),
                f'<{tag}{attrs} id="{reference["reference_id"]}">{body}</{tag}>',
            )
        )
        anchored_reference_ids.add(str(reference["reference_id"]))
    for start, end, replacement in sorted(reference_replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    reference_count = len(reference_replacements)

    citation_count = 0
    citation_groups: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for citation in diagnostics["citations"]:
        if any(
            str(link["reference_id"]) not in anchored_reference_ids
            for link in citation["links"]
        ):
            continue
        citation_groups.setdefault(
            (citation.get("reading_order"), str(citation["node_text"])),
            [],
        ).append(citation)
    for (_reading_order, node_text), citations in citation_groups.items():
        candidates = list(HTML_TEXT_BLOCK_RE.finditer(updated))
        candidates.extend(
            re.finditer(
                r'<(?P<tag>td|th)\b(?P<attrs>[^>]*)>'
                r'(?P<body>.*?)</(?P=tag)>',
                updated,
                flags=re.I | re.S,
            )
        )
        candidates.extend(
            re.finditer(
                r'<(?P<tag>div)\b(?P<attrs>[^>]*\bclass="[^"]*\bcaption\b[^"]*"[^>]*)>'
                r'(?P<body>.*?)</(?P=tag)>',
                updated,
                flags=re.I | re.S,
            )
        )
        for match in sorted(candidates, key=lambda item: item.start()):
            if (
                match.group("tag").lower() == "li"
                and "docling-reference-entry" in match.group("attrs")
            ):
                continue
            visible = html.unescape(HTML_TAG_RE.sub("", match.group("body")))
            if (
                not _citation_visible_context_matches(
                    visible,
                    node_text,
                    str(citations[0]["raw_citation"]),
                )
                or not any(
                    str(citation["raw_citation"]) in match.group("body")
                    for citation in citations
                )
            ):
                continue
            body = match.group("body")
            for citation in citations:
                linked = _linked_citation_html(citation)
                body, changed = re.subn(
                    re.escape(citation["raw_citation"]),
                    lambda _match, value=linked: value,
                    body,
                    count=1,
                )
                citation_count += changed
            replacement = (
                f"<{match.group('tag')}{match.group('attrs')}>"
                f"{body}</{match.group('tag')}>"
            )
            updated = updated[: match.start()] + replacement + updated[match.end() :]
            break
    return updated, reference_count, citation_count


def _link_bibliography_in_markdown(
    document_markdown: str,
    diagnostics: dict[str, Any],
) -> tuple[str, int, int]:
    if not diagnostics.get("available"):
        return document_markdown, 0, 0
    updated = document_markdown
    heading_match = re.search(
        r"(?im)^[ \t]{0,3}#{1,6}[ \t]+"
        r"(?:references|bibliography|参考文献)[ \t]*[:：]?[ \t]*$",
        updated,
    )
    reference_matches: list[re.Match[str]] = []
    if heading_match:
        next_heading = re.search(
            r"(?m)^[ \t]{0,3}#{1,6}[ \t]+.+$",
            updated[heading_match.end() :],
        )
        section_end = (
            heading_match.end() + next_heading.start()
            if next_heading
            else len(updated)
        )
        reference_line_re = re.compile(
            r"(?m)^(?P<indent>[ \t]*)(?P<marker>[-+]|\d+[.)])\s+"
            r"(?P<body>[^\n]+)$"
        )
        reference_matches = list(
            reference_line_re.finditer(
                updated,
                heading_match.end(),
                section_end,
            )
        )

    reference_replacements: list[tuple[int, int, str]] = []
    anchored_reference_ids: set[str] = set()
    for reference in diagnostics["references"]:
        source_indexes = reference.get("source_list_indexes") or []
        if not source_indexes:
            continue
        source_index = int(source_indexes[0])
        if source_index >= len(reference_matches):
            continue
        match = reference_matches[source_index]
        backlinks = " ".join(
            f'<a href="#{backlink_id}" aria-label="Back to citation '
            f'{reference["number"]}">↩</a>'
            for backlink_id in reference.get("backlink_ids") or []
        )
        reference_replacements.append(
            (
                match.start(),
                match.end(),
                f'{match.group("indent")}{reference["number"]}. '
                f'<a id="{reference["reference_id"]}"></a>'
                f'{match.group("body")} {backlinks}',
            )
        )
        anchored_reference_ids.add(str(reference["reference_id"]))
    for start, end, replacement in sorted(reference_replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    reference_count = len(reference_replacements)

    citation_count = 0
    citation_groups: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for citation in diagnostics["citations"]:
        if any(
            str(link["reference_id"]) not in anchored_reference_ids
            for link in citation["links"]
        ):
            continue
        citation_groups.setdefault(
            (citation.get("reading_order"), str(citation["node_text"])),
            [],
        ).append(citation)
    for (_reading_order, node_text), citations in citation_groups.items():
        block_match = None
        candidates = list(
            re.finditer(
                r"(?P<prefix>\A|\n[ \t]*\n)(?P<body>.*?)(?=\n[ \t]*\n|\Z)",
                updated,
                flags=re.S,
            )
        )
        candidates.extend(
            re.finditer(r"(?m)^(?P<body>[^\n]+)$", updated)
        )
        candidates.extend(
            re.finditer(r"(?P<body>(?<=\|)[^|\n]+(?=\|))", updated)
        )
        for candidate in sorted(candidates, key=lambda item: item.start("body")):
            visible = html.unescape(HTML_TAG_RE.sub("", candidate.group("body")))
            visible = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", visible)
            visible = re.sub(r"\*\*(.*?)\*\*", r"\1", visible, flags=re.S)
            visible = re.sub(r"__(.*?)__", r"\1", visible, flags=re.S)
            visible = re.sub(
                r"^[ \t]*(?:[-+]|\d+[.)])\s+",
                "",
                visible,
            )
            if (
                _citation_visible_context_matches(
                    visible,
                    node_text,
                    str(citations[0]["raw_citation"]),
                )
                and any(
                    str(citation["raw_citation"]) in candidate.group("body")
                    for citation in citations
                )
            ):
                block_match = candidate
                break
        if block_match is None:
            continue
        body = block_match.group("body")
        for citation in citations:
            linked = _linked_citation_html(citation)
            body, changed = re.subn(
                re.escape(citation["raw_citation"]),
                lambda _match, value=linked: value,
                body,
                count=1,
            )
            citation_count += changed
        updated = (
            updated[: block_match.start("body")]
            + body
            + updated[block_match.end("body") :]
        )

    return updated, reference_count, citation_count


APPENDIX_REFERENCE_RE = re.compile(r"\bAppendix\s+(?P<label>[A-Z](?:\.\d+){0,3})\b")
APPENDIX_HEADING_RE = re.compile(r"^\s*(?:Appendix\s+)?(?P<label>[A-Z](?:\.\d+){0,3})\b")


def _appendix_anchor_id(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"docling-appendix-{slug}"


def _link_appendix_references_in_html(document_html: str) -> tuple[str, int]:
    heading_replacements: list[tuple[int, int, str]] = []
    anchors: dict[str, str] = {}
    for match in HTML_TEXT_BLOCK_RE.finditer(document_html):
        tag = match.group("tag").lower()
        if not tag.startswith("h"):
            continue
        visible = _normalized_noise_text(
            html.unescape(HTML_TAG_RE.sub(" ", match.group("body")))
        )
        heading_match = APPENDIX_HEADING_RE.match(visible)
        if not heading_match:
            continue
        label = heading_match.group("label")
        anchor_id = _appendix_anchor_id(label)
        anchors.setdefault(label, anchor_id)
        attrs = match.group("attrs")
        if re.search(r'\bid\s*=', attrs):
            continue
        heading_replacements.append(
            (
                match.start(),
                match.end(),
                f'<{match.group("tag")}{attrs} id="{anchor_id}">{match.group("body")}</{match.group("tag")}>',
            )
        )
    if not anchors:
        return document_html, 0
    updated = document_html
    for start, end, replacement in sorted(heading_replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    link_replacements: list[tuple[int, int, str]] = []
    count = 0
    for match in HTML_TEXT_BLOCK_RE.finditer(updated):
        tag = match.group("tag").lower()
        if tag.startswith("h") or tag in {"script", "style"}:
            continue
        body = match.group("body")
        protected: list[str] = []

        def protect_anchor(anchor_match: re.Match[str]) -> str:
            protected.append(anchor_match.group(0))
            return f"@@DOCLING_ANCHOR_{len(protected) - 1}@@"

        working_body = re.sub(
            r"<a\b[^>]*>.*?</a>",
            protect_anchor,
            body,
            flags=re.I | re.S,
        )

        def replace(reference_match: re.Match[str]) -> str:
            nonlocal count
            label = reference_match.group("label")
            anchor_id = anchors.get(label)
            if not anchor_id:
                return reference_match.group(0)
            count += 1
            return f'<a class="docling-internal-reference" href="#{anchor_id}">{html.escape(reference_match.group(0))}</a>'

        new_body = APPENDIX_REFERENCE_RE.sub(replace, working_body)
        for index, anchor_html in enumerate(protected):
            new_body = new_body.replace(f"@@DOCLING_ANCHOR_{index}@@", anchor_html)
        if new_body != body:
            link_replacements.append(
                (
                    match.start("body"),
                    match.end("body"),
                    new_body,
                )
            )
    for start, end, replacement in sorted(link_replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated, count


def _structural_content_html(
    records: list[dict[str, Any]],
    note_groups: list[dict[str, Any]],
    reference_mappings: list[dict[str, Any]],
) -> str:
    if not records:
        return ""
    labels = {
        "page_header": "Page header",
        "page_footer": "Page footer",
        "footnote": "Footnote",
        "visual_annotation": "Figure or visual-region text",
        "math_font_noise": "Math-font noise",
    }
    backlinks: dict[str, list[str]] = {}
    for mapping in reference_mappings:
        backlinks.setdefault(mapping["note_id"], []).append(mapping["reference_id"])
    items = []
    for record in records:
        if record["kind"] == "footnote":
            continue
        reasons = ", ".join(str(reason) for reason in record.get("reasons") or [])
        items.append(
            '<article class="docling-structural-item" '
            f'data-kind="{html.escape(record["kind"], quote=True)}" '
            f'data-page="{html.escape(str(record.get("page_no")), quote=True)}">'
            '<header>'
            f'<strong>{labels.get(record["kind"], record["kind"])}</strong>'
            f'<span>Page {html.escape(str(record.get("page_no") or "unknown"))}</span>'
            '</header>'
            f'<p>{html.escape(record["text"])}</p>'
            '<small>'
            f'confidence={html.escape(str(record.get("confidence")))}; '
            f'evidence_score={html.escape(str(record.get("evidence_score")))}; '
            f'reasons={html.escape(reasons)}'
            '</small>'
            '</article>'
        )
    for note in note_groups:
        marker = note.get("marker")
        backlink_html = " ".join(
            f'<a class="docling-note-backlink" href="#{reference_id}">Back to reference</a>'
            for reference_id in backlinks.get(note["note_id"], [])
        )
        marker_html = (
            f'<sup class="docling-note-marker">{html.escape(str(marker))}</sup> '
            if marker
            else ""
        )
        items.append(
            '<article class="docling-structural-item docling-structural-note" '
            f'id="{note["note_id"]}" data-kind="footnote" '
            f'data-page="{html.escape(str(note.get("page_no")), quote=True)}">'
            '<header><strong>Footnote</strong>'
            f'<span>Page {html.escape(str(note.get("page_no") or "unknown"))}</span></header>'
            f'<p>{marker_html}{html.escape(str(note.get("text") or ""))}</p>'
            f'<small>assembly={html.escape(str(note.get("assembly_reason")))}; '
            f'confidence={html.escape(str(note.get("confidence")))}</small>'
            f'{backlink_html}'
            '</article>'
        )
    return (
        f'<section id="{STRUCTURAL_CONTENT_HTML_ID}" '
        'class="docling-structural-content" role="doc-endnotes" '
        'aria-labelledby="docling-structural-content-title">'
        '<h2 id="docling-structural-content-title">Extracted structural and visual notes</h2>'
        '<p class="docling-structural-content-note">'
        'High-confidence headers, footers, footnotes, and visual-region text isolated from the main reading flow.'
        '</p>'
        + "".join(items)
        + '</section>'
    )


def _append_structural_content_html(
    document_html: str,
    records: list[dict[str, Any]],
    note_groups: list[dict[str, Any]],
    reference_mappings: list[dict[str, Any]],
) -> str:
    appendix = _structural_content_html(records, note_groups, reference_mappings)
    if not appendix or f'id="{STRUCTURAL_CONTENT_HTML_ID}"' in document_html:
        return document_html
    style = """
<style id="docling-structural-content-style">
.docling-structural-content {
  margin: 3rem auto 1rem;
  padding: 1.25rem 0 0;
  border-top: 2px solid #5f6b75;
  max-width: 72rem;
}
.docling-structural-content-note,
.docling-structural-item small {
  color: #52606d;
}
.docling-structural-item {
  margin: 1rem 0;
  padding: .75rem 1rem;
  border-left: 3px solid #8b99a6;
  background: #f5f7f8;
}
.docling-structural-item header {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}
.docling-structural-item p {
  margin: .5rem 0;
  white-space: pre-wrap;
}
.docling-note-ref a,
.docling-note-backlink {
  text-decoration: none;
}
.docling-note-backlink {
  display: inline-block;
  margin-right: .75rem;
}
.docling-structural-note:target {
  outline: 2px solid #2563eb;
}
</style>
"""
    if "docling-structural-content-style" not in document_html:
        if "</head>" in document_html:
            document_html = document_html.replace("</head>", style + "\n</head>", 1)
        else:
            document_html = style + "\n" + document_html
    if "</body>" in document_html:
        return document_html.replace("</body>", appendix + "\n</body>", 1)
    return document_html + "\n" + appendix


def _structural_content_markdown(
    records: list[dict[str, Any]],
    note_groups: list[dict[str, Any]],
    reference_mappings: list[dict[str, Any]],
) -> str:
    if not records:
        return ""
    labels = {
        "page_header": "Page header",
        "page_footer": "Page footer",
        "footnote": "Footnote",
        "visual_annotation": "Figure or visual-region text",
        "math_font_noise": "Math-font noise",
    }
    lines = [
        STRUCTURAL_CONTENT_MD_START,
        "",
        "---",
        "",
        "## Extracted structural and visual notes",
        "",
        "High-confidence headers, footers, footnotes, and visual-region text isolated from the main reading flow.",
        "",
    ]
    for record in records:
        if record["kind"] == "footnote":
            continue
        lines.extend(
            [
                f"### Page {record.get('page_no') or 'unknown'} - {labels.get(record['kind'], record['kind'])}",
                "",
                record["text"],
                "",
                (
                    "<!-- structural evidence "
                    f"confidence={record.get('confidence')} "
                    f"score={record.get('evidence_score')} "
                    f"reasons={','.join(record.get('reasons') or [])} -->"
                ),
                "",
            ]
        )
    backlinks: dict[str, list[str]] = {}
    for mapping in reference_mappings:
        backlinks.setdefault(mapping["note_id"], []).append(mapping["reference_id"])
    for note in note_groups:
        marker = f"{note['marker']} " if note.get("marker") else ""
        lines.extend(
            [
                f'<a id="{note["note_id"]}"></a>',
                f"### Page {note.get('page_no') or 'unknown'} - Footnote",
                "",
                marker + str(note.get("text") or ""),
                "",
            ]
        )
        for reference_id in backlinks.get(note["note_id"], []):
            lines.extend([f"[Back to reference](#{reference_id})", ""])
        lines.extend(
            [
                (
                    "<!-- note assembly "
                    f"reason={note.get('assembly_reason')} "
                    f"confidence={note.get('confidence')} "
                    f"source_records={','.join(str(value) for value in note.get('source_record_indexes') or [])} -->"
                ),
                "",
            ]
        )
    lines.append(STRUCTURAL_CONTENT_MD_END)
    return "\n".join(lines) + "\n"


def _append_structural_content_markdown(
    document_markdown: str,
    records: list[dict[str, Any]],
    note_groups: list[dict[str, Any]],
    reference_mappings: list[dict[str, Any]],
) -> str:
    appendix = _structural_content_markdown(records, note_groups, reference_mappings)
    if not appendix or STRUCTURAL_CONTENT_MD_START in document_markdown:
        return document_markdown
    return document_markdown.rstrip() + "\n\n" + appendix


def _html_without_structural_content(document_html: str) -> str:
    return re.sub(
        rf'<section\b[^>]*id="{re.escape(STRUCTURAL_CONTENT_HTML_ID)}"[^>]*>.*?</section>',
        " ",
        document_html,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _markdown_without_structural_content(document_markdown: str) -> str:
    return re.sub(
        re.escape(STRUCTURAL_CONTENT_MD_START)
        + r".*?"
        + re.escape(STRUCTURAL_CONTENT_MD_END),
        " ",
        document_markdown,
        flags=re.DOTALL,
    )


def _without_bibliography_section_html(document_html: str) -> str:
    heading_match = next(
        (
            match
            for match in HTML_TEXT_BLOCK_RE.finditer(document_html)
            if match.group("tag").lower().startswith("h")
            and BIBLIOGRAPHY_HEADING_RE.fullmatch(
                _normalized_noise_text(
                    html.unescape(HTML_TAG_RE.sub("", match.group("body")))
                )
            )
        ),
        None,
    )
    if not heading_match:
        return document_html
    section_end = len(document_html)
    for match in HTML_TEXT_BLOCK_RE.finditer(document_html, heading_match.end()):
        if match.group("tag").lower().startswith("h"):
            section_end = match.start()
            break
    return document_html[: heading_match.start()] + document_html[section_end:]


def _without_bibliography_section_markdown(document_markdown: str) -> str:
    heading_match = re.search(
        r"(?im)^[ \t]{0,3}#{1,6}[ \t]+"
        r"(?:references|bibliography|参考文献)[ \t]*[:：]?[ \t]*$",
        document_markdown,
    )
    if not heading_match:
        return document_markdown
    next_heading = re.search(
        r"(?m)^[ \t]{0,3}#{1,6}[ \t]+.+$",
        document_markdown[heading_match.end() :],
    )
    section_end = (
        heading_match.end() + next_heading.start()
        if next_heading
        else len(document_markdown)
    )
    return document_markdown[: heading_match.start()] + document_markdown[section_end:]


def apply_structural_quarantine_to_outputs(
    output_dir: Path,
    document_json: Any,
    source_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply generic structural quarantine to JSON, HTML, and Markdown outputs."""
    qc = structural_noise_qc(document_json, source_evidence)
    candidates = qc["candidates"]
    quarantine_candidates = [
        item for item in candidates
        if item.get("action") == "quarantine_from_main_text_flow"
    ]
    export_records = _structural_export_records(candidates)
    note_groups = _build_structural_note_groups(
        export_records,
        source_evidence or {"available": False, "pages": {}},
    )
    inline_references = _pdf_inline_note_references(
        document_json,
        source_evidence or {"available": False, "pages": {}},
        note_groups,
    )
    html_path = output_dir / "document.html"
    if html_path.exists():
        html_reference_candidates = _html_inline_note_references(
            document_json,
            html_path.read_text(encoding="utf-8"),
            note_groups,
        )
        existing_reference_keys = {
            (
                item.get("page_no"),
                item.get("marker"),
                item.get("reading_order"),
            )
            for item in inline_references
        }
        inline_references.extend(
            item
            for item in html_reference_candidates
            if (
                item.get("page_no"),
                item.get("marker"),
                item.get("reading_order"),
            )
            not in existing_reference_keys
        )
    existing_reference_keys = {
        (
            item.get("page_no"),
            item.get("marker"),
            item.get("reading_order"),
        )
        for item in inline_references
    }
    inline_references.extend(
        item
        for item in _first_page_publication_note_references(
            document_json,
            note_groups,
            inline_references,
        )
        if (
            item.get("page_no"),
            item.get("marker"),
            item.get("reading_order"),
        )
        not in existing_reference_keys
    )
    reference_mappings, unresolved_references = _map_note_references(
        note_groups,
        inline_references,
    )
    bibliography = bibliography_diagnostics(document_json)
    linked_note_ids = {item["note_id"] for item in reference_mappings}
    unresolved_notes = [
        {
            "note_id": note["note_id"],
            "page_no": note.get("page_no"),
            "marker": note.get("marker"),
            "reason": (
                "note_content_empty"
                if not str(note.get("text") or "").strip()
                else "no_high_confidence_inline_reference"
            ),
        }
        for note in note_groups
        if note.get("marker") and note["note_id"] not in linked_note_ids
    ]

    json_path = output_dir / "document.json"
    if json_path.exists():
        json_path.write_text(json.dumps(document_json, indent=2, ensure_ascii=False), encoding="utf-8")

    html_replacements = 0
    if html_path.exists():
        html_text = html_path.read_text(encoding="utf-8")
        html_text, _style = _inject_english_review_style(html_text)
        quarantine_style = """
<style id="docling-structural-quarantine-style">
.docling-structural-quarantine {
  display: none;
}
</style>
"""
        if quarantine_candidates and "docling-structural-quarantine-style" not in html_text:
            if "</head>" in html_text:
                html_text = html_text.replace("</head>", quarantine_style + "\n</head>", 1)
            else:
                html_text = quarantine_style + "\n" + html_text
        for item in quarantine_candidates:
            if item.get("match_mode") == "fragment":
                html_text, changed = _replace_html_fragment_with_quarantine(html_text, item)
            else:
                html_text, changed = _replace_exact_paragraph_with_quarantine(html_text, item)
            if changed:
                html_replacements += 1
        html_text, private_use_sweep_count = _replace_private_use_math_noise_blocks_html(html_text)
        html_replacements += private_use_sweep_count
        html_text, html_reference_link_count = _link_note_references_in_html(
            html_text,
            reference_mappings,
        )
        (
            html_text,
            html_bibliography_entry_count,
            html_citation_link_count,
        ) = _link_bibliography_in_html(html_text, bibliography)
        html_text, html_internal_reference_link_count = _link_appendix_references_in_html(
            html_text
        )
        html_text = _append_structural_content_html(
            html_text,
            export_records,
            note_groups,
            reference_mappings,
        )
        html_path.write_text(html_text, encoding="utf-8")
    else:
        html_reference_link_count = 0
        html_bibliography_entry_count = 0
        html_citation_link_count = 0
        html_internal_reference_link_count = 0

    md_replacements = 0
    md_path = output_dir / "document.md"
    if md_path.exists():
        md_text = md_path.read_text(encoding="utf-8")
        for item in quarantine_candidates:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            replacement = (
                "\n\n<!-- local-ai-lab structural quarantine "
                f"kind={item.get('kind')} page={item.get('page_no')} "
                f"reasons={','.join(item.get('reasons') or [])} "
                "evidence=metadata.json -->\n\n"
            )
            md_text, count = _replace_markdown_quarantine_text(
                md_text,
                text,
                replacement,
                fragment=item.get("match_mode") == "fragment",
            )
            if count:
                md_replacements += 1
        md_text, private_use_sweep_count = _replace_private_use_math_noise_blocks_markdown(md_text)
        md_replacements += private_use_sweep_count
        md_text, markdown_reference_link_count = _link_note_references_in_markdown(
            md_text,
            reference_mappings,
        )
        (
            md_text,
            markdown_bibliography_entry_count,
            markdown_citation_link_count,
        ) = _link_bibliography_in_markdown(md_text, bibliography)
        md_text = _append_structural_content_markdown(
            md_text,
            export_records,
            note_groups,
            reference_mappings,
        )
        md_path.write_text(md_text, encoding="utf-8")
    else:
        markdown_reference_link_count = 0
        markdown_bibliography_entry_count = 0
        markdown_citation_link_count = 0

    final_html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    final_md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    body_html = _html_without_structural_content(final_html)
    body_md = _markdown_without_structural_content(final_md)
    html_without_references = _without_bibliography_section_html(body_html)
    markdown_without_references = _without_bibliography_section_markdown(body_md)
    unlinked_html_surface = re.sub(
        r'<a\b[^>]*class="docling-citation\b[^"]*"[^>]*>.*?</a>',
        "linked-citation",
        html_without_references,
        flags=re.I,
    )
    unlinked_markdown_surface = re.sub(
        r'<a\b[^>]*class="docling-citation\b[^"]*"[^>]*>.*?</a>',
        "linked-citation",
        markdown_without_references,
        flags=re.I,
    )
    mapped_citation_texts = {
        str(item["raw_citation"])
        for item in bibliography.get("citations") or []
    }
    visible_html_surface = _visible_html_text(unlinked_html_surface)
    visible_markdown_surface = _normalized_markdown_text(unlinked_markdown_surface)
    visible_unlinked_html_citations = [
        raw for raw in mapped_citation_texts if raw and raw in visible_html_surface
    ]
    visible_unlinked_markdown_citations = [
        raw for raw in mapped_citation_texts if raw and raw in visible_markdown_surface
    ]
    markdown_without_comments = re.sub(
        r"<!--.*?-->",
        " ",
        body_md,
        flags=re.DOTALL,
    )
    residuals: list[dict[str, Any]] = []
    for item in quarantine_candidates:
        target = _normalized_noise_text(str(item.get("text") or ""))
        residual_surfaces = []
        if (
            target
            and item.get("match_mode") == "fragment"
            and target in _visible_html_text(body_html)
        ):
            residual_surfaces.append("document.html")
        elif target and _html_has_exact_visible_block(body_html, target):
            residual_surfaces.append("document.html")
        if (
            target
            and item.get("match_mode") == "fragment"
            and target in _normalized_markdown_text(markdown_without_comments)
        ):
            residual_surfaces.append("document.md")
        elif target and _markdown_exact_text_pattern(target).search(markdown_without_comments):
            residual_surfaces.append("document.md")
        item["final_output_residual_surfaces"] = residual_surfaces
        if residual_surfaces:
            residuals.append(
                {
                    "kind": item.get("kind"),
                    "page_no": item.get("page_no"),
                    "text_preview": item.get("text_preview"),
                    "surfaces": residual_surfaces,
                }
            )

    qc["html_quarantine_replacement_count"] = html_replacements
    qc["markdown_quarantine_replacement_count"] = md_replacements
    qc["isolated_main_text_pollution_count"] = html_replacements + md_replacements
    qc["final_output_residual_count"] = len(residuals)
    qc["final_output_residuals"] = residuals
    qc["exported_structural_content_count"] = len(export_records)
    qc["exported_structural_content_counts_by_kind"] = {
        kind: sum(1 for item in export_records if item["kind"] == kind)
        for kind in ("page_header", "page_footer", "footnote")
    }
    qc["html_structural_content_count"] = sum(
        1
        for item in export_records
        if _html_has_exact_visible_block(final_html, item["text"])
    )
    qc["markdown_structural_content_count"] = sum(
        1
        for item in export_records
        if _markdown_exact_text_pattern(item["text"]).search(final_md)
    )
    qc["assembled_note_count"] = len(note_groups)
    qc["note_reference_candidate_count"] = len(inline_references)
    qc["note_reference_link_count"] = len(reference_mappings)
    qc["html_note_reference_link_count"] = html_reference_link_count
    qc["markdown_note_reference_link_count"] = markdown_reference_link_count
    qc["unresolved_note_reference_count"] = len(unresolved_references)
    qc["unresolved_note_references"] = unresolved_references
    qc["unresolved_structural_note_count"] = len(unresolved_notes)
    qc["unresolved_structural_notes"] = unresolved_notes
    qc["bibliography_reference_count"] = bibliography.get("reference_count", 0)
    qc["bibliography_citation_count"] = bibliography.get("citation_count", 0)
    qc["bibliography_linked_number_count"] = bibliography.get(
        "linked_number_count",
        0,
    )
    qc["html_bibliography_entry_count"] = html_bibliography_entry_count
    qc["html_citation_link_count"] = html_citation_link_count
    qc["html_internal_reference_link_count"] = html_internal_reference_link_count
    qc["markdown_bibliography_entry_count"] = markdown_bibliography_entry_count
    qc["markdown_citation_link_count"] = markdown_citation_link_count
    qc["html_visible_unlinked_citation_count"] = len(
        visible_unlinked_html_citations
    )
    qc["markdown_visible_unlinked_citation_count"] = len(
        visible_unlinked_markdown_citations
    )
    qc["unresolved_citation_count"] = bibliography.get(
        "unresolved_citation_count",
        0,
    )
    qc["unresolved_citations"] = bibliography.get("unresolved_citations", [])
    html_reference_ids = set(
        re.findall(r'\bid="(docling-reference-\d+)"', final_html)
    )
    html_reference_targets = set(
        re.findall(r'\bhref="#(docling-reference-\d+)"', final_html)
    )
    markdown_reference_ids = set(
        re.findall(r'\bid="(docling-reference-\d+)"', final_md)
    )
    markdown_reference_targets = set(
        re.findall(r'\bhref="#(docling-reference-\d+)"', final_md)
    )
    qc["html_broken_bibliography_target_count"] = len(
        html_reference_targets - html_reference_ids
    )
    qc["markdown_broken_bibliography_target_count"] = len(
        markdown_reference_targets - markdown_reference_ids
    )
    bibliography["output_validation"] = {
        "html_reference_anchor_count": html_bibliography_entry_count,
        "markdown_reference_anchor_count": markdown_bibliography_entry_count,
        "html_linked_citation_occurrence_count": html_citation_link_count,
        "markdown_linked_citation_occurrence_count": markdown_citation_link_count,
        "html_visible_unlinked_citation_count": len(
            visible_unlinked_html_citations
        ),
        "markdown_visible_unlinked_citation_count": len(
            visible_unlinked_markdown_citations
        ),
        "html_broken_reference_target_count": len(
            html_reference_targets - html_reference_ids
        ),
        "markdown_broken_reference_target_count": len(
            markdown_reference_targets - markdown_reference_ids
        ),
    }
    content_sidecar = {
        "schema_version": 2,
        "description": (
            "High-confidence headers, footers, and footnotes exported outside "
            "the main reading flow."
        ),
        "record_count": len(export_records),
        "counts_by_kind": qc["exported_structural_content_counts_by_kind"],
        "records": export_records,
        "notes": note_groups,
        "note_reference_mappings": reference_mappings,
        "unresolved_note_references": unresolved_references,
        "unresolved_notes": unresolved_notes,
    }
    content_sidecar_path = output_dir / "structural_content.json"
    content_sidecar_path.write_text(
        json.dumps(content_sidecar, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    qc["content_sidecar_path"] = content_sidecar_path.name
    bibliography_sidecar_path = output_dir / "bibliography_links.json"
    bibliography_sidecar_path.write_text(
        json.dumps(bibliography, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    qc["bibliography_sidecar_path"] = bibliography_sidecar_path.name
    sidecar_path = output_dir / "structural_regions.json"
    sidecar_path.write_text(json.dumps(qc, indent=2, ensure_ascii=False), encoding="utf-8")
    qc["sidecar_path"] = sidecar_path.name
    return qc


def formula_number_qc_diagnostics(
    formulas: list[dict[str, Any]],
    document_html: str,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    html_blocks = list(FORMULA_MATH_BLOCK_RE.finditer(document_html))
    for index, formula in enumerate(formulas, start=1):
        text = str(formula.get("text") or "")
        prov = first_prov(formula) or {}
        compact_numbers = _compact_formula_numbers(text)
        normal_numbers = [int(value) for value in FORMULA_NUMBER_RE.findall(text)]
        reasons: list[str] = []
        safe_recovered_number: int | None = None
        if compact_numbers and not normal_numbers:
            safe_recovered_number = compact_numbers[-1]
            reasons.append("equation_number_recoverable_from_formula_text")
        if MATH_TEXT_RE.search(text) and not compact_numbers:
            reasons.append("display_formula_missing_equation_number")
        if index <= len(html_blocks):
            html_block_text = html.unescape(html_blocks[index - 1].group(0))
            if compact_numbers and not any(f"({number})" in html_block_text for number in compact_numbers):
                reasons.append("html_formula_number_not_compactly_visible")
        if reasons:
            diagnostics.append(
                {
                    "index": index,
                    "text": text[:300],
                    "reasons": reasons,
                    "page_no": prov.get("page_no"),
                    "bbox": bbox_geometry(prov),
                    "recovered_number": safe_recovered_number,
                    "safe_to_recover": safe_recovered_number is not None,
                    "evidence": f"formulas/formula_{index}_context.png",
                }
            )
    return diagnostics


def sanitize_formula_display_text(formula_text: str) -> tuple[str, list[str]]:
    """Return a MathJax-display-safe formula body plus evidence-backed reasons."""
    reasons: list[str] = []
    display_text = formula_text
    if "&" in display_text and not ALIGNMENT_ENV_RE.search(display_text):
        sanitized = re.sub(r"\s*&\s*", " ", display_text)
        sanitized = re.sub(r"\s+", " ", sanitized).strip()
        if sanitized and sanitized != display_text:
            display_text = sanitized
            reasons.append("bare_alignment_marker_without_alignment_environment")
    return display_text, reasons


def formula_tex_qc_diagnostics(formulas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for index, formula in enumerate(formulas, start=1):
        text = str(formula.get("text") or "")
        prov = first_prov(formula) or {}
        display_text, reasons = sanitize_formula_display_text(text)
        if not reasons:
            continue
        diagnostics.append(
            {
                "index": index,
                "text": text[:400],
                "display_text": display_text[:400],
                "reasons": reasons,
                "page_no": prov.get("page_no"),
                "bbox": bbox_geometry(prov),
                "evidence": f"formulas/formula_{index}_context.png",
                "action": "sanitize_display_tex_preserve_raw_tex",
                "safe_to_apply": True,
            }
        )
    return diagnostics


def formula_second_pass_apply_all_review(
    formulas: list[dict[str, Any]],
    formula_number_diag: list[dict[str, Any]],
    formula_tex_diag: list[dict[str, Any]],
    formula_number_recovered: list[int],
) -> dict[str, Any]:
    number_by_index = {int(item["index"]): item for item in formula_number_diag}
    tex_by_index = {int(item["index"]): item for item in formula_tex_diag}
    reviewed: list[dict[str, Any]] = []
    for index, formula in enumerate(formulas, start=1):
        text = str(formula.get("text") or "")
        prov = first_prov(formula) or {}
        number_item = number_by_index.get(index)
        tex_item = tex_by_index.get(index)
        actions: list[str] = []
        quality_gate = "preserve_route_a"
        if index in formula_number_recovered:
            actions.append("html_number_recovered_from_formula_text")
            quality_gate = "enhance_html"
        if tex_item and tex_item.get("safe_to_apply"):
            actions.append("html_display_tex_sanitized")
            quality_gate = "enhance_html"
        if number_item and not number_item.get("safe_to_recover"):
            actions.append("diagnostic_evidence_only")
            if quality_gate != "enhance_html":
                quality_gate = "evidence_only"
        reviewed.append(
            {
                "index": index,
                "page_no": prov.get("page_no"),
                "eq_numbers": _compact_formula_numbers(text),
                "quality_gate": quality_gate,
                "actions": actions or ["reviewed_no_change"],
                "number_recovery": number_item,
                "tex_safety": tex_item,
                "evidence": f"formulas/formula_{index}_context.png",
            }
        )
    return {
        "policy": "apply_all_review_gate",
        "formula_count": len(formulas),
        "reviewed_count": len(reviewed),
        "enhanced_count": sum(1 for item in reviewed if item["quality_gate"] == "enhance_html"),
        "evidence_only_count": sum(1 for item in reviewed if item["quality_gate"] == "evidence_only"),
        "preserved_count": sum(1 for item in reviewed if item["quality_gate"] == "preserve_route_a"),
        "reviewed_formulas": reviewed,
    }


def formula_second_pass_alignment_diagnostics(
    replacement_log: list[dict[str, Any]],
    route_a_formula_count: int | None,
) -> dict[str, Any]:
    """Summarize formula second-pass coverage and anchor integrity."""
    route_a_formula_count = int(route_a_formula_count or 0)
    attempted_indexes = [
        int(entry["formula_no"])
        for entry in replacement_log
        if isinstance(entry.get("formula_no"), int)
    ]
    attempted_set = set(attempted_indexes)
    missing_attempt_indexes = [
        index for index in range(1, route_a_formula_count + 1) if index not in attempted_set
    ]
    eq_to_entries: dict[int, list[dict[str, Any]]] = {}
    sequence_mismatches: list[dict[str, Any]] = []
    missing_body: list[dict[str, Any]] = []
    image_formula_not_converted: list[dict[str, Any]] = []
    fallback_reasons: list[dict[str, Any]] = []
    anchor_mismatches: list[dict[str, Any]] = []
    crop_only_without_formula: list[dict[str, Any]] = []
    render_failed_latex: list[dict[str, Any]] = []
    second_pass_not_applied: list[dict[str, Any]] = []
    downstream_offset_risk_after: int | None = None

    for entry in replacement_log:
        formula_no = entry.get("formula_no")
        eq_number = entry.get("eq_number")
        status_value = str(entry.get("status") or "")
        route_a_text = str(entry.get("route_a_text") or "")
        candidate_text = str(entry.get("route_b_candidate") or "")
        anchor_match = entry.get("anchor_match") or {}
        if (
            "equation_number_mismatch" in (anchor_match.get("reasons") or [])
            or (anchor_match.get("page_order_distance") or 0) > 1
            or (anchor_match.get("y_distance") or 0) > 120
        ):
            anchor_mismatches.append(
                {
                    "formula_no": formula_no,
                    "anchor_id": entry.get("anchor_id"),
                    "status": status_value,
                    "anchor_match": anchor_match,
                    "reason": "formula_anchor_evidence_mismatch",
                }
            )
            if downstream_offset_risk_after is None and isinstance(formula_no, int):
                downstream_offset_risk_after = formula_no
        if isinstance(eq_number, int):
            eq_to_entries.setdefault(eq_number, []).append(entry)
            if isinstance(formula_no, int) and eq_number != formula_no:
                sequence_mismatches.append(
                    {
                        "formula_no": formula_no,
                        "eq_number": eq_number,
                        "status": status_value,
                        "page_no": entry.get("page_no"),
                        "bbox": entry.get("route_a_bbox"),
                        "reason": "formula_index_equation_number_mismatch",
                    }
                )
                if downstream_offset_risk_after is None:
                    downstream_offset_risk_after = formula_no
        if FORMULA_NUMBER_RE.fullmatch(route_a_text.strip()) or re.fullmatch(
            r"\s*\(\s*(?:\d\s*){1,3}\s*\)\s*",
            route_a_text,
        ):
            missing_body.append(
                {
                    "formula_no": formula_no,
                    "eq_number": eq_number,
                    "status": status_value,
                    "page_no": entry.get("page_no"),
                    "bbox": entry.get("route_a_bbox"),
                    "fallback_reason": entry.get("fallback_reason"),
                    "reason": "missing_body_with_number_only_output",
                }
            )
        if status_value != "replaced":
            second_pass_not_applied.append(
                {
                    "formula_no": formula_no,
                    "anchor_id": entry.get("anchor_id"),
                    "status": status_value,
                    "fallback_reason": entry.get("fallback_reason") or status_value,
                }
            )
            fallback_reasons.append(
                {
                    "formula_no": formula_no,
                    "eq_number": eq_number,
                    "status": status_value,
                    "fallback_reason": entry.get("fallback_reason") or status_value,
                    "page_no": entry.get("page_no"),
                    "bbox": entry.get("route_a_bbox"),
                }
            )
            if not candidate_text.strip() and (
                "number_only_missing_body" in (entry.get("reasons") or [])
                or FORMULA_NUMBER_RE.fullmatch(route_a_text.strip())
            ):
                image_formula_not_converted.append(
                    {
                        "formula_no": formula_no,
                        "eq_number": eq_number,
                        "status": status_value,
                        "page_no": entry.get("page_no"),
                        "bbox": entry.get("route_a_bbox"),
                        "reason": "image_formula_area_not_converted_by_second_pass",
                    }
                )
        if entry.get("crop_only_without_formula"):
            crop_only_without_formula.append(
                {
                    "formula_no": formula_no,
                    "anchor_id": entry.get("anchor_id"),
                    "status": status_value,
                    "fallback_reason": entry.get("fallback_reason"),
                    "evidence": entry.get("route_a_evidence"),
                }
            )
        if status_value == "render_failed_latex":
            render_failed_latex.append(
                {
                    "formula_no": formula_no,
                    "anchor_id": entry.get("anchor_id"),
                    "fallback_reason": entry.get("fallback_reason"),
                    "candidate_text": candidate_text[:400],
                }
            )

    duplicate_equation_numbers = [
        {
            "eq_number": eq_number,
            "formula_indexes": [
                entry.get("formula_no") for entry in entries if entry.get("formula_no") is not None
            ],
            "statuses": [entry.get("status") for entry in entries],
        }
        for eq_number, entries in sorted(eq_to_entries.items())
        if len(entries) > 1
    ]
    return {
        "route_a_formula_count": route_a_formula_count,
        "attempted_count": len(attempted_indexes),
        "all_formulas_attempted": not missing_attempt_indexes,
        "missing_attempt_indexes": missing_attempt_indexes,
        "sequence_mismatch_count": len(sequence_mismatches),
        "sequence_mismatches": sequence_mismatches,
        "duplicate_equation_number_count": len(duplicate_equation_numbers),
        "duplicate_equation_numbers": duplicate_equation_numbers,
        "missing_body_number_only_count": len(missing_body),
        "missing_body_number_only": missing_body,
        "image_formula_not_converted_count": len(image_formula_not_converted),
        "image_formula_not_converted": image_formula_not_converted,
        "fallback_count": len(fallback_reasons),
        "fallback_reasons": fallback_reasons,
        "anchor_mismatch_count": len(anchor_mismatches),
        "anchor_mismatches": anchor_mismatches,
        "crop_only_without_formula_count": len(crop_only_without_formula),
        "crop_only_without_formula": crop_only_without_formula,
        "render_failed_latex_count": len(render_failed_latex),
        "render_failed_latex": render_failed_latex,
        "second_pass_not_applied_count": len(second_pass_not_applied),
        "second_pass_not_applied": second_pass_not_applied,
        "downstream_offset_risk": downstream_offset_risk_after is not None,
        "downstream_offset_risk_after_formula": downstream_offset_risk_after,
    }


def recover_formula_numbers_in_html(
    output_dir: Path,
    document_html: str,
    formulas: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    tex_diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[str, list[int]]:
    recoverable = {
        int(item["index"]): int(item["recovered_number"])
        for item in diagnostics
        if item.get("safe_to_recover") and item.get("recovered_number")
    }
    tex_fixable = {
        int(item["index"]): item
        for item in tex_diagnostics or []
        if item.get("safe_to_apply")
    }
    target_indexes = sorted(set(recoverable) | set(tex_fixable))
    if not target_indexes:
        return document_html, []

    blocks = list(FORMULA_MATH_BLOCK_RE.finditer(document_html))
    replacements: list[tuple[int, re.Match[str], str]] = []
    sidecar_dir = output_dir
    for formula_index in target_indexes:
        if not (0 < formula_index <= len(blocks)):
            continue
        recovered_number = recoverable.get(formula_index)
        formula_text = str(formulas[formula_index - 1].get("text") or "").strip()
        if not formula_text:
            continue
        display_text, _sanitize_reasons = sanitize_formula_display_text(formula_text)
        status_value = (
            "qc_formula_number_recovery"
            if recovered_number is not None
            else "qc_formula_tex_safety"
        )
        replacement = _render_second_pass_formula_html(
            {
                "formula_no": formula_index,
                "status": status_value,
                "markdown_after": f"$${formula_text}$$",
                "display_override": display_text,
                "raw_tex": formula_text,
            },
            output_dir,
            sidecar_dir,
        )
        if recovered_number is not None:
            replacement = replacement.replace(
                f'data-formula-status="{status_value}"',
                (
                    f'data-formula-status="{status_value}" '
                    f'data-equation-number="{html.escape(str(recovered_number), quote=True)}"'
                ),
                1,
            )
        replacements.append((formula_index, blocks[formula_index - 1], replacement))

    if not replacements:
        return document_html, []
    updated = _replace_original_html_ranges(document_html, replacements)
    updated, _assets_injected = _ensure_formula_second_pass_html_assets(updated)
    return updated, sorted(index for index, _match, _replacement in replacements)


def footnote_review_diagnostics(document_json: Any) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    footnotes = extract_label_nodes(document_json, "footnote")
    anchors_by_page: dict[Any, list[str]] = {}
    for record in structural_text_records(document_json):
        text = str(record.get("text") or "").strip()
        if record.get("label", "").lower() == "footnote":
            continue
        page_no = record.get("page_no")
        if text in {"∗", "*", "†", "‡"} or re.search(r"\w\s+[∗†‡]\s+\w", text):
            anchors_by_page.setdefault(page_no, []).append(text[:80])

    footnote_geometries: list[tuple[int, Any, dict[str, float], str]] = []
    for index, node in enumerate(footnotes, start=1):
        prov = first_prov(node) or {}
        geometry = bbox_geometry(prov)
        if geometry:
            footnote_geometries.append((index, prov.get("page_no"), geometry, str(node.get("text") or "")))

    for index, node in enumerate(footnotes, start=1):
        text = str(node.get("text") or "")
        prov = first_prov(node) or {}
        geometry = bbox_geometry(prov)
        reasons: list[str] = []
        if re.fullmatch(r"\d+", text.strip()):
            reasons.append("isolated_numeric_footnote_fragment")
        if re.match(r"^\d+\s+\w+", text.strip()):
            reasons.append("numeric_marker_attached_to_text_fragment")
        if text.rstrip().endswith("-"):
            reasons.append("hyphenated_split_footnote_continuation")
        if geometry and geometry["b"] < 110:
            reasons.append("near_page_bottom_footnote")
        if geometry:
            for other_index, other_page, other_geometry, _other_text in footnote_geometries:
                if other_index == index or other_page != prov.get("page_no"):
                    continue
                horizontal_overlap = min(geometry["r"], other_geometry["r"]) - max(
                    geometry["l"], other_geometry["l"]
                )
                vertical_overlap = min(geometry["t"], other_geometry["t"]) - max(
                    geometry["b"], other_geometry["b"]
                )
                if horizontal_overlap > 0 and vertical_overlap > 0:
                    reasons.append("overlapping_footnote_bbox")
                    break
        if anchors_by_page.get(prov.get("page_no")) and re.fullmatch(r"\d+", text.strip()):
            reasons.append("anchor_content_marker_mismatch")
        if reasons:
            page_no = prov.get("page_no")
            diagnostics.append(
                {
                    "index": index,
                    "text": text[:240],
                    "reasons": reasons,
                    "page_no": page_no,
                    "bbox": geometry,
                    "evidence": f"pages/page_{page_no}.png" if page_no else None,
                    "nearby_anchor_examples": anchors_by_page.get(page_no, [])[:5],
                    "action": "diagnostic_only_no_reordering",
                }
            )
    return diagnostics


def _footnote_fragment_records(document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, node in enumerate(extract_label_nodes(document_json, "footnote"), start=1):
        prov = first_prov(node) or {}
        records.append(
            {
                "index": index,
                "text": str(node.get("text") or ""),
                "page_no": prov.get("page_no"),
                "bbox": bbox_geometry(prov),
            }
        )
    return records


def first_page_footnote_recovery_diagnostics(document_json: Any) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    page_one = [
        record
        for record in _footnote_fragment_records(document_json)
        if record.get("page_no") == 1 and record.get("bbox")
    ]
    for lead in page_one:
        lead_text = str(lead.get("text") or "").strip()
        if not lead_text.endswith("-"):
            continue
        lead_bbox = lead.get("bbox") or {}
        for tail in page_one:
            tail_text = str(tail.get("text") or "").strip()
            if tail["index"] == lead["index"] or not re.match(r"^\d+\s+\w+", tail_text):
                continue
            tail_bbox = tail.get("bbox") or {}
            if tail_bbox.get("t", 0) > lead_bbox.get("t", 0):
                continue
            if abs(tail_bbox.get("b", 0) - lead_bbox.get("b", 0)) > 25:
                continue
            number, tail_body = tail_text.split(None, 1)
            recovered = f"{number} {lead_text[:-1]}{tail_body}"
            diagnostics.append(
                {
                    "page_no": 1,
                    "footnote_number": number,
                    "lead_fragment_index": lead["index"],
                    "tail_fragment_index": tail["index"],
                    "lead_fragment": lead_text,
                    "tail_fragment": tail_text,
                    "recovered_text": recovered,
                    "reasons": [
                        "same_page_bottom_footnote_fragments",
                        "hyphenated_lead_fragment",
                        "numeric_tail_fragment_continues_hyphenated_word",
                    ],
                    "bbox": {
                        "lead": lead_bbox,
                        "tail": tail_bbox,
                    },
                    "evidence": "pages/page_1.png",
                    "action": "diagnostic_only_generic_quarantine_preferred",
                    "safe_to_apply": False,
                }
            )
            break
    if any(record["text"].strip() == "0" for record in page_one):
        diagnostics.append(
            {
                "page_no": 1,
                "footnote_number": "0",
                "reasons": ["isolated_numeric_marker_without_recoverable_body"],
                "evidence": "pages/page_1.png",
                "action": "diagnostic_only_no_recovery",
                "safe_to_apply": False,
            }
        )
    return diagnostics


def apply_first_page_footnote_html_recovery(
    document_html: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Preserve legacy call shape while preventing guess-reconstructed footnotes."""
    for item in diagnostics:
        item["safe_to_apply"] = False
        item["action"] = "diagnostic_only_generic_quarantine_preferred"
    return document_html, []


def layout_qc_diagnostics(document_json: Any) -> dict[str, Any]:
    records = structural_text_records(document_json)
    pages: dict[Any, list[dict[str, Any]]] = {}
    labels: dict[str, int] = {}
    for record in records:
        labels[record["label"]] = labels.get(record["label"], 0) + 1
        pages.setdefault(record.get("page_no"), []).append(record)
    page_diagnostics: list[dict[str, Any]] = []
    for page_no, page_records in sorted(pages.items(), key=lambda item: (item[0] is None, item[0] or 0)):
        x_centers = [
            (record.get("bbox") or {}).get("l", 0) + ((record.get("bbox") or {}).get("width", 0) / 2)
            for record in page_records
            if (record.get("bbox") or {}).get("width", 0) > 80
            and record["label"].lower() not in PAGE_EDGE_LABELS
        ]
        left = sum(1 for value in x_centers if value < 300)
        right = sum(1 for value in x_centers if value >= 300)
        page_diagnostics.append(
            {
                "page_no": page_no,
                "text_record_count": len(page_records),
                "wide_text_left_count": left,
                "wide_text_right_count": right,
                "layout_hint": "two_column_candidate" if left >= 3 and right >= 3 else "single_or_mixed_layout",
                "evidence": f"pages/page_{page_no}.png" if page_no else None,
            }
        )
    return {
        "label_counts": labels,
        "page_count": len([page for page in pages if page is not None]),
        "two_column_candidate_pages": [
            item["page_no"] for item in page_diagnostics if item["layout_hint"] == "two_column_candidate"
        ],
        "page_diagnostics": page_diagnostics,
    }


def write_formula_latex_sources(output_dir: Path, formulas: list[dict[str, Any]]) -> dict[str, Any]:
    if not formulas:
        return {"written": False, "path": None, "formula_count": 0}
    lines: list[str] = []
    for index, formula in enumerate(formulas, start=1):
        text = str(formula.get("text") or "").strip()
        prov = first_prov(formula) or {}
        display_text, reasons = sanitize_formula_display_text(text)
        lines.append(f"% Formula {index}")
        if prov.get("page_no") is not None:
            lines.append(f"% page_no: {prov.get('page_no')}")
        lines.append(f"% evidence: formulas/formula_{index}_context.png")
        if reasons:
            lines.append("% display_tex_sanitized: " + ",".join(reasons))
            lines.append("% raw_tex:")
            lines.append(text)
            lines.append("% display_tex:")
            lines.append(display_text)
        else:
            lines.append(text)
        lines.append("")
    path = output_dir / "formulas.tex"
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"written": True, "path": "formulas.tex", "formula_count": len(formulas)}


def math_review_diagnostics(document_html: str, document_json: Any) -> dict[str, Any]:
    decoded = html.unescape(DATA_IMAGE_RE.sub("data:image/<stripped>", document_html))
    text_nodes = [
        str(node.get("text") or "")
        for node in iter_nodes(document_json)
        if isinstance(node, dict) and isinstance(node.get("text"), str)
    ]
    math_text_nodes = [
        text
        for text in text_nodes
        if MATH_TEXT_RE.search(text)
    ]
    return {
        "mathml_block_count": document_html.count("<math"),
        "math_unicode_text_node_count": len(math_text_nodes),
        "math_unicode_text_examples": math_text_nodes[:10],
        "boxed_math_symbol_count": sum(decoded.count(char) for char in ("□", "▢", "◻", "☐", "■", "▣", "�")),
    }


def run_unified_review_qc(
    output_dir: Path,
    document_json: Any,
    formulas: list[dict[str, Any]],
    metadata: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return
    source_text_evidence = pdf_source_text_evidence(args.input_file)
    semantic_emphasis = apply_semantic_emphasis_to_outputs(
        output_dir,
        document_json,
        source_text_evidence,
    )
    document_html = html_path.read_text(encoding="utf-8")
    before_href_count = len(re.findall(r"href=\"", document_html))
    document_html, style_injected = _inject_english_review_style(document_html)
    document_html, autolink_count = _autolink_plain_urls(document_html)
    document_html, footnote_sup_count = _polish_footnote_superscripts(document_html)
    document_html, math_text_count = _mark_math_heavy_text(document_html)
    formula_second_pass_start = time.monotonic()
    formula_number_diag = formula_number_qc_diagnostics(formulas, document_html)
    formula_tex_diag = formula_tex_qc_diagnostics(formulas)
    formula_second_pass_owns_html = args.formula_second_pass_policy != "off"
    if formula_second_pass_owns_html:
        formula_number_recovered = []
    else:
        document_html, formula_number_recovered = recover_formula_numbers_in_html(
            output_dir,
            document_html,
            formulas,
            formula_number_diag,
            formula_tex_diag,
        )
    formula_second_pass_review = formula_second_pass_apply_all_review(
        formulas,
        formula_number_diag,
        formula_tex_diag,
        formula_number_recovered,
    )
    formula_second_pass_review["elapsed_seconds"] = round(
        time.monotonic() - formula_second_pass_start,
        6,
    )
    first_page_footnote_diag = first_page_footnote_recovery_diagnostics(document_json)
    first_page_footnote_applied: list[dict[str, Any]] = []
    html_path.write_text(document_html, encoding="utf-8")

    link_diag = pdf_annotation_link_diagnostics(args.input_file)
    footnote_diag = footnote_review_diagnostics(document_json)
    math_diag = math_review_diagnostics(document_html, document_json)
    header_footer_diag = header_footer_qc_diagnostics(document_json)
    layout_diag = layout_qc_diagnostics(document_json)
    formula_latex_sources = write_formula_latex_sources(output_dir, formulas)
    author_affiliation_recovery = recover_first_page_author_affiliations(
        output_dir,
        document_json,
        args.input_file,
    )
    author_reading_order_recovery = recover_first_page_author_reading_order(
        output_dir,
        document_json,
    )
    abstract_reading_order_recovery = recover_first_page_abstract_reading_order(
        output_dir,
        document_json,
    )
    structural_quarantine = apply_structural_quarantine_to_outputs(
        output_dir,
        document_json,
        source_text_evidence,
    )
    links_path = output_dir / "links.json"
    links_path.write_text(json.dumps(link_diag, indent=2, ensure_ascii=False), encoding="utf-8")

    metadata.update(
        {
            "pdf_link_diagnostics": link_diag,
            "pdf_annotation_link_count": link_diag.get("pdf_annotation_link_count"),
            "pdf_uri_link_count": link_diag.get("pdf_uri_link_count"),
            "pdf_goto_link_count": link_diag.get("pdf_goto_link_count"),
            "json_hyperlink_count": json_hyperlink_count(document_json),
            "html_plain_url_autolink_count": autolink_count,
            "html_href_count_before_review_polish": before_href_count,
            "html_href_count_after_review_polish": len(re.findall(r"href=\"", document_html)),
            "review_qc_style_injected": style_injected,
            "footnote_superscript_polish_count": footnote_sup_count,
            "math_text_polish_count": math_text_count,
            "footnote_review_diagnostics": footnote_diag,
            "math_review_diagnostics": math_diag,
            "formula_number_qc_diagnostics": formula_number_diag,
            "formula_number_recovered_html_indexes": formula_number_recovered,
            "formula_second_pass_owns_html": formula_second_pass_owns_html,
            "formula_tex_qc_diagnostics": formula_tex_diag,
            "formula_second_pass_apply_all_review": formula_second_pass_review,
            "first_page_footnote_recovery_diagnostics": first_page_footnote_diag,
            "first_page_footnote_recovery_applied": first_page_footnote_applied,
            "header_footer_qc_diagnostics": header_footer_diag,
            "layout_qc_diagnostics": layout_diag,
            "author_affiliation_recovery": author_affiliation_recovery,
            "author_reading_order_recovery": author_reading_order_recovery,
            "abstract_reading_order_recovery": abstract_reading_order_recovery,
            "semantic_emphasis": semantic_emphasis,
            "structural_quarantine_qc": structural_quarantine,
            "formula_latex_sources": formula_latex_sources,
        }
    )
    metadata.setdefault("generated_outputs", []).extend(
        [
            "links.json",
            structural_quarantine["sidecar_path"],
            structural_quarantine["content_sidecar_path"],
        ]
    )
    if formula_latex_sources.get("written"):
        metadata.setdefault("generated_outputs", []).append(str(formula_latex_sources["path"]))
    status["quality_signals"].update(
        {
            "pdf_annotation_link_count": metadata["pdf_annotation_link_count"],
            "pdf_uri_link_count": metadata["pdf_uri_link_count"],
            "pdf_goto_link_count": metadata["pdf_goto_link_count"],
            "json_hyperlink_count": metadata["json_hyperlink_count"],
            "html_plain_url_autolink_count": autolink_count,
            "html_href_count_after_review_polish": metadata["html_href_count_after_review_polish"],
            "footnote_review_diagnostics": footnote_diag,
            "math_review_diagnostics": math_diag,
            "formula_number_qc_diagnostics": formula_number_diag,
            "formula_number_recovered_html_indexes": formula_number_recovered,
            "formula_second_pass_owns_html": formula_second_pass_owns_html,
            "formula_tex_qc_diagnostics": formula_tex_diag,
            "formula_second_pass_apply_all_review": formula_second_pass_review,
            "first_page_footnote_recovery_diagnostics": first_page_footnote_diag,
            "first_page_footnote_recovery_applied": first_page_footnote_applied,
            "header_footer_qc_diagnostics": header_footer_diag,
            "layout_qc_diagnostics": layout_diag,
            "author_affiliation_recovery": author_affiliation_recovery,
            "author_reading_order_recovery": author_reading_order_recovery,
            "abstract_reading_order_recovery": abstract_reading_order_recovery,
            "semantic_emphasis": semantic_emphasis,
            "structural_quarantine_qc": structural_quarantine,
            "formula_latex_sources": formula_latex_sources,
        }
    )
    if autolink_count:
        status["warnings"].append(f"html_plain_urls_autolinked:{autolink_count}")
    if link_diag.get("pdf_annotation_link_count") and not metadata["json_hyperlink_count"]:
        status["warnings"].append(
            "pdf_annotations_not_propagated_by_docling_json:"
            f"links={link_diag.get('pdf_annotation_link_count')}:"
            f"uris={link_diag.get('pdf_uri_link_count')}:links.json"
        )
    for item in footnote_diag:
        status["warnings"].append(
            "suspicious_footnote:"
            f"{item['index']}:{','.join(item['reasons'])}:"
            f"{item.get('evidence')}"
        )
    for item in formula_number_diag:
        if item.get("safe_to_recover"):
            continue
        status["warnings"].append(
            "formula_number_qc:"
            f"{item['index']}:{','.join(item.get('reasons') or [])}:"
            f"{item.get('evidence')}"
        )
    if formula_number_recovered:
        status["warnings"].append(
            "formula_numbers_recovered_in_html:"
            + ",".join(str(index) for index in formula_number_recovered)
        )
    for item in formula_tex_diag:
        status["warnings"].append(
            "formula_tex_qc:"
            f"{item['index']}:{','.join(item.get('reasons') or [])}:"
            f"{item.get('action')}:{item.get('evidence')}"
        )
    if formula_second_pass_review.get("reviewed_count"):
        status["warnings"].append(
            "formula_second_pass_apply_all_review:"
            f"reviewed={formula_second_pass_review.get('reviewed_count')}:"
            f"enhanced={formula_second_pass_review.get('enhanced_count')}:"
            f"evidence_only={formula_second_pass_review.get('evidence_only_count')}:"
            f"elapsed={formula_second_pass_review.get('elapsed_seconds')}"
        )
    if semantic_emphasis.get("detected_span_count") and (
        semantic_emphasis.get("html_applied_span_count")
        < semantic_emphasis.get("detected_span_count")
        or semantic_emphasis.get("markdown_applied_span_count")
        < semantic_emphasis.get("detected_span_count")
    ):
        status["warnings"].append(
            "semantic_emphasis_partial_application:"
            f"detected={semantic_emphasis.get('detected_span_count')}:"
            f"html={semantic_emphasis.get('html_applied_span_count')}:"
            f"markdown={semantic_emphasis.get('markdown_applied_span_count')}"
        )
    for item in structural_quarantine.get("unresolved_note_references") or []:
        status["warnings"].append(
            "unresolved_note_reference:"
            f"page={item.get('page_no')}:marker={item.get('marker')}:"
            f"reason={item.get('reason')}"
        )
    for item in structural_quarantine.get("unresolved_structural_notes") or []:
        status["warnings"].append(
            "unresolved_structural_note:"
            f"page={item.get('page_no')}:marker={item.get('marker')}:"
            f"reason={item.get('reason')}"
        )
    for item in first_page_footnote_diag:
        status["warnings"].append(
            "first_page_footnote_qc:"
            f"{item.get('footnote_number')}:{','.join(item.get('reasons') or [])}:"
            f"diagnostic_only_generic_quarantine_preferred:{item.get('evidence')}"
        )
    if structural_quarantine.get("candidate_count"):
        status["warnings"].append(
            "structural_quarantine_applied:"
            f"candidates={structural_quarantine.get('candidate_count')}:"
            f"html={structural_quarantine.get('html_quarantine_replacement_count')}:"
            f"md={structural_quarantine.get('markdown_quarantine_replacement_count')}:"
            f"unresolved_footnotes={structural_quarantine.get('unresolved_footnote_count')}"
        )
    if author_affiliation_recovery.get("applied"):
        status["warnings"].append(
            "author_affiliation_recovered_from_pdf_text_layer:"
            f"html={author_affiliation_recovery.get('html_fragment_replacement_count')}:"
            f"md={author_affiliation_recovery.get('markdown_fragment_replacement_count')}"
        )
    for item in header_footer_diag:
        status["warnings"].append(
            "header_footer_qc:"
            f"{item['label']}:{item.get('page_no')}:{','.join(item.get('reasons') or [])}:"
            f"{item.get('action')}"
        )
    missing_formula_numbers = [
        item["index"]
        for item in formula_number_diag
        if "display_formula_missing_equation_number" in (item.get("reasons") or [])
    ]
    if missing_formula_numbers:
        status["warnings"].append(
            "display_formula_numbers_missing:"
            + ",".join(str(number) for number in missing_formula_numbers)
            + ":requires_structural_source_evidence_for_recovery"
        )


def is_cn_accepted_path(args: argparse.Namespace) -> bool:
    return args.input_file.name == "CN.pdf" and effective_cn_ocr_parity(args)


def effective_formula_second_pass_policy(args: argparse.Namespace) -> str:
    return args.formula_second_pass_policy


def cn_accepted_baseline_diagnostics(output_dir: Path) -> dict[str, Any]:
    document = _load_json_file(output_dir / "document.json")
    if not isinstance(document, dict):
        return {
            "ok": False,
            "baseline": CN_ACCEPTED_BASELINE,
            "reasons": ["document_json_missing_or_invalid"],
        }

    text_nodes = [
        str(node.get("text") or "")
        for node in iter_nodes(document)
        if isinstance(node, dict) and isinstance(node.get("text"), str)
    ]
    formulas = extract_label_nodes(document, "formula")
    equation_numbers: list[int | None] = []
    for formula in formulas:
        numbers = [
            int(re.sub(r"\s+", "", match.group(1)))
            for match in re.finditer(r"\(\s*((?:\d\s*)+)\)", str(formula.get("text") or ""))
        ]
        equation_numbers.append(numbers[-1] if numbers else None)

    joined_text = "\n".join(text_nodes)
    markdown = (output_dir / "document.md").read_text(encoding="utf-8")
    document_html = (
        (output_dir / "document.html").read_text(encoding="utf-8")
        if (output_dir / "document.html").exists()
        else ""
    )
    html_visible_text = html.unescape(HTML_TAG_RE.sub(" ", document_html))
    markdown_cn_character_count = len(CN_CHAR_RE.findall(markdown))
    html_cn_character_count = len(CN_CHAR_RE.findall(html_visible_text))
    reasons: list[str] = []
    gxx_count = len(GXX_RE.findall(joined_text))
    cn_character_count = len(CN_CHAR_RE.findall(joined_text))
    expected_numbers = CN_ACCEPTED_BASELINE["equation_numbers"]
    if gxx_count:
        reasons.append(f"gxx_count={gxx_count}")
    if len(formulas) != CN_ACCEPTED_BASELINE["formula_count"]:
        reasons.append(f"formula_count={len(formulas)}")
    if equation_numbers != expected_numbers:
        reasons.append("formula_equation_sequence_mismatch")
    if cn_character_count < CN_ACCEPTED_BASELINE["minimum_cn_character_count"]:
        reasons.append(f"cn_character_count={cn_character_count}")
    minimum_final_cn = CN_ACCEPTED_BASELINE["minimum_final_output_cn_character_count"]
    if markdown_cn_character_count < minimum_final_cn:
        reasons.append(f"final_markdown_cn_character_count={markdown_cn_character_count}")
    if html_cn_character_count < minimum_final_cn:
        reasons.append(f"final_html_cn_character_count={html_cn_character_count}")
    missing_polish = [
        formula_no
        for formula_no in CN_FINAL_POLISH_FORMULA_NUMBERS
        if formula_no not in equation_numbers
    ]
    if missing_polish:
        reasons.append(
            "missing_cn_final_polish_formulas="
            + ",".join(str(number) for number in missing_polish)
        )
    accepted_text = CN_FINAL_TEXT_CORRECTIONS[0][1]
    if accepted_text not in markdown:
        reasons.append("accepted_cn_text_correction_missing")

    return {
        "ok": not reasons,
        "baseline": CN_ACCEPTED_BASELINE,
        "gxx_count": gxx_count,
        "cn_character_count": cn_character_count,
        "markdown_cn_character_count": markdown_cn_character_count,
        "html_cn_character_count": html_cn_character_count,
        "formula_count": len(formulas),
        "equation_numbers": equation_numbers,
        "required_final_polish_formulas": list(CN_FINAL_POLISH_FORMULA_NUMBERS),
        "accepted_text_correction_present": accepted_text in markdown,
        "reasons": reasons,
    }


def record_cn_accepted_baseline(
    output_dir: Path,
    metadata: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    if not is_cn_accepted_path(args):
        return
    document_json = _load_json_file(output_dir / "document.json")
    if isinstance(document_json, dict):
        source_evidence = pdf_source_text_evidence(args.input_file)
        structural_qc = apply_structural_quarantine_to_outputs(
            output_dir,
            document_json,
            source_evidence,
        )
        metadata["cn_structural_qc_applied"] = True
        metadata["structural_quarantine_qc"] = structural_qc
        status["quality_signals"]["cn_structural_qc_applied"] = True
        status["quality_signals"]["structural_quarantine_qc"] = structural_qc
        metadata.setdefault("generated_outputs", []).extend(
            [
                structural_qc["sidecar_path"],
                structural_qc["content_sidecar_path"],
            ]
        )
        for item in structural_qc.get("unresolved_structural_notes") or []:
            status["warnings"].append(
                "unresolved_structural_note:"
                f"page={item.get('page_no')}:marker={item.get('marker')}:"
                f"reason={item.get('reason')}"
            )
    diagnostics = cn_accepted_baseline_diagnostics(output_dir)
    metadata["cn_processing_path"] = CN_ACCEPTED_BASELINE["name"]
    metadata["cn_unified_review_qc_skipped"] = True
    metadata["cn_accepted_baseline_regression"] = diagnostics
    status["quality_signals"]["cn_processing_path"] = CN_ACCEPTED_BASELINE["name"]
    status["quality_signals"]["cn_unified_review_qc_skipped"] = True
    status["quality_signals"]["cn_accepted_baseline_regression"] = diagnostics
    if not diagnostics["ok"]:
        status["ok"] = False
        status["success_class"] = "degraded_failure"
        status["warnings"].append(
            "cn_accepted_baseline_regression:"
            + ",".join(diagnostics.get("reasons") or ["unknown"])
        )
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def restore_review_artifact_layer(
    output_dir: Path,
    response: dict[str, Any],
    metadata: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    document = response.get("document") or {}
    document_json = document.get("json_content")
    tables = extract_table_nodes(document_json)
    formulas = extract_label_nodes(document_json, "formula")
    pictures = extract_label_nodes(document_json, "picture")

    table_outputs = write_table_review_artifacts(output_dir, tables)
    crop_counts, crop_warnings, formula_crop_diagnostics = render_page_images_and_crops(
        args.input_file, output_dir, tables, formulas, pictures
    )
    table_visual_fallbacks = inject_empty_table_visual_fallbacks(
        output_dir,
        document_json,
        tables,
    )
    diagnostics = formula_review_diagnostics(
        formulas,
        output_dir,
        document,
        args.input_file,
        formula_crop_diagnostics,
    )
    review_warnings: list[str] = []
    review_warnings.extend(crop_warnings)
    for item in diagnostics["suspicious_formula_diagnostics"]:
        review_warnings.append(
            "suspicious_formula:"
            f"{item['index']}:{','.join(item['reasons'])}:"
            f"{item['evidence']}"
        )
        if "source_crop_likely_useless_for_review" in item["reasons"]:
            review_warnings.append(
                "formula_source_crop_likely_useless:"
                f"{item['index']}:use_context_or_full_page:"
                f"{item.get('evidence')}:{item.get('full_page_evidence')}"
            )
    for item in diagnostics["missing_formula_diagnostics"]:
        review_warnings.append(
            "missing_or_incomplete_formula_evidence:"
            f"{item.get('index')}:{item.get('text')}:"
            f"page={((item.get('prov') or {}).get('page_no'))}"
        )
    metadata.update(crop_counts)
    metadata.update(diagnostics)
    metadata["table_visual_fallbacks"] = table_visual_fallbacks
    metadata["table_artifact_count"] = len(tables) + len(table_outputs) + crop_counts["table_image_count"]
    metadata["picture_artifact_count"] = crop_counts["picture_artifact_count"]
    metadata["asset_count"] = (
        crop_counts["page_image_count"]
        + crop_counts["table_image_count"]
        + crop_counts["formula_asset_count"]
        + crop_counts["formula_context_asset_count"]
        + crop_counts["picture_artifact_count"]
    )
    metadata["review_artifact_warnings"] = review_warnings
    metadata["unresolved_v1_parity_warnings"] = UNRESOLVED_V1_PARITY_WARNINGS
    metadata.setdefault("generated_outputs", []).extend(
        [
            "review_index.html",
            *[f"pages/page_{index}.png" for index in range(1, crop_counts["page_image_count"] + 1)],
            *table_outputs,
            *[
                f"formulas/formula_{index}.png"
                for index in range(1, crop_counts["formula_asset_count"] + 1)
            ],
            *[
                f"formulas/formula_{index}_context.png"
                for index in range(1, crop_counts["formula_context_asset_count"] + 1)
            ],
            *[
                f"pictures/picture_{index}.png"
                for index in range(1, crop_counts["picture_artifact_count"] + 1)
            ],
        ]
    )
    status["warnings"].extend(review_warnings)
    status["warnings"].extend(UNRESOLVED_V1_PARITY_WARNINGS)
    status["quality_signals"].update(
        {
            "page_image_count": metadata["page_image_count"],
            "formula_asset_count": metadata["formula_asset_count"],
            "formula_context_asset_count": metadata["formula_context_asset_count"],
            "formula_evidence_count": metadata["formula_evidence_count"],
            "missing_formula_evidence_count": metadata["missing_formula_evidence_count"],
            "suspicious_formula_diagnostics": metadata["suspicious_formula_diagnostics"],
            "formula_crop_diagnostics": metadata["formula_crop_diagnostics"],
            "table_artifact_count": metadata["table_artifact_count"],
            "picture_artifact_count": metadata["picture_artifact_count"],
            "table_visual_fallbacks": table_visual_fallbacks,
            "review_artifact_warnings": review_warnings,
            "unresolved_v1_parity_warnings": UNRESOLVED_V1_PARITY_WARNINGS,
            "cn_section_2_3_diagnostic_summary": metadata[
                "cn_section_2_3_diagnostic_summary"
            ],
        }
    )
    write_review_index(output_dir, metadata, status)
    add_document_review_banner(output_dir)
    formula_source_link_count = inject_formula_source_links(output_dir, formulas)
    metadata["formula_source_link_count"] = formula_source_link_count
    status["quality_signals"]["formula_source_link_count"] = formula_source_link_count
    if is_cn_accepted_path(args):
        metadata["cn_processing_path"] = CN_ACCEPTED_BASELINE["name"]
        metadata["cn_unified_review_qc_skipped"] = True
        status["quality_signals"]["cn_processing_path"] = CN_ACCEPTED_BASELINE["name"]
        status["quality_signals"]["cn_unified_review_qc_skipped"] = True
        status["warnings"].append(
            "cn_unified_review_qc_skipped:preserve_accepted_cn_0854aa1_path"
        )
    else:
        run_unified_review_qc(output_dir, document_json, formulas, metadata, status, args)
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _relative_output_link(output_dir: Path, target: Path) -> str:
    try:
        return target.relative_to(output_dir).as_posix()
    except ValueError:
        return target.as_posix()


def _strip_display_math_wrapper(text: str) -> str:
    body = text.strip()
    if body.startswith("$$") and body.endswith("$$") and len(body) >= 4:
        body = body[2:-2].strip()
    return body


def _second_pass_formula_display_text(entry: dict[str, Any]) -> str:
    display_override = str(entry.get("display_override") or "").strip()
    if display_override:
        candidate = display_override
    else:
        markdown_after = _strip_display_math_wrapper(str(entry.get("markdown_after") or ""))
        candidate = markdown_after or str(entry.get("route_b_candidate") or "").strip()
    equation_number = entry.get("eq_number")
    normalized, repairs = canonicalize_formula_output(
        candidate,
        equation_number if isinstance(equation_number, int) else None,
    )
    if repairs:
        entry.setdefault("final_output_repairs", []).extend(
            repair for repair in repairs if repair not in entry.get("final_output_repairs", [])
        )
    return normalized


def _second_pass_formula_raw_text(entry: dict[str, Any]) -> str:
    raw_tex = str(entry.get("raw_tex") or "").strip()
    if raw_tex:
        return raw_tex
    markdown_after = _strip_display_math_wrapper(str(entry.get("markdown_after") or ""))
    if markdown_after:
        return markdown_after
    return str(entry.get("route_b_candidate") or "").strip()


def _render_second_pass_formula_html(
    entry: dict[str, Any],
    output_dir: Path,
    sidecar_dir: Path,
) -> str:
    formula_no = entry.get("formula_no")
    display_text = _second_pass_formula_display_text(entry)
    raw_text = _second_pass_formula_raw_text(entry)
    source_link = f"formulas/formula_{formula_no}.png" if formula_no else None
    context_link = f"formulas/formula_{formula_no}_context.png" if formula_no else None
    review_link = _relative_output_link(output_dir, sidecar_dir / "review_index.html")
    links = []
    for label, href in (
        ("source image", source_link),
        ("context crop", context_link),
        ("second-pass review", review_link),
    ):
        if href:
            links.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>')
    return (
        '<div class="docling-formula-second-pass" '
        f'data-formula-index="{html.escape(str(formula_no), quote=True)}" '
        f'data-formula-status="{html.escape(str(entry.get("status") or ""), quote=True)}">'
        '<div class="docling-formula-second-pass-label">'
        f'Formula {html.escape(str(formula_no))} patched by formula second pass'
        '</div>'
        '<div class="docling-formula-render">'
        f'\\[{html.escape(display_text)}\\]'
        '</div>'
        '<pre class="docling-formula-tex">'
        f'{html.escape(raw_text)}'
        '</pre>'
        + (
            '<pre class="docling-formula-tex docling-formula-display-tex">'
            f'{html.escape(display_text)}'
            '</pre>'
            if display_text != raw_text
            else ""
        )
        +
        '<div class="docling-formula-source">'
        + " | ".join(links)
        + "</div>"
        "</div>"
    )


def _render_formula_fallback_html(
    entry: dict[str, Any],
    output_dir: Path,
    sidecar_dir: Path,
) -> str:
    formula_no = entry.get("formula_no")
    reason = str(entry.get("fallback_reason") or entry.get("status") or "second_pass_not_applied")
    raw_candidate = str(entry.get("route_b_candidate") or "").strip()
    source_formula, fallback_source = _best_formula_fallback_text(entry)
    links = [
        f'<a href="formulas/formula_{formula_no}.png">source image</a>',
        f'<a href="formulas/formula_{formula_no}_context.png">context crop</a>',
        f'<a href="{html.escape(_relative_output_link(output_dir, sidecar_dir / "review_index.html"))}">'
        "second-pass review</a>",
    ]
    candidate = (
        '<pre class="docling-formula-tex docling-formula-fallback-candidate">'
        f"{html.escape(raw_candidate[:1200])}</pre>"
        if raw_candidate and raw_candidate != source_formula
        else ""
    )
    source_formula_render = ""
    if source_formula and not _formula_output_safety_reasons(source_formula):
        source_formula_render = (
            '<div class="docling-formula-render docling-formula-preserved-source">'
            f"\\[{html.escape(source_formula)}\\]"
            "</div>"
        )
    elif source_formula:
        source_formula_render = (
            '<pre class="docling-formula-tex docling-formula-preserved-source">'
            f"{html.escape(source_formula)}</pre>"
        )
    else:
        source_formula_render = (
            '<p class="docling-formula-unavailable">'
            "Formula body could not be recovered safely; source evidence is preserved below."
            "</p>"
        )
    crop_path = output_dir / "formulas" / f"formula_{formula_no}.png"
    crop = (
        '<figure class="docling-formula-fallback-evidence">'
        f'<img src="formulas/formula_{formula_no}.png" alt="Formula {formula_no} source crop">'
        "</figure>"
        if crop_path.exists()
        else ""
    )
    return (
        '<div class="docling-formula-second-pass docling-formula-fallback" '
        f'data-formula-index="{formula_no}" '
        f'data-formula-status="{html.escape(str(entry.get("status") or "fallback"))}" '
        f'data-formula-fallback-reason="{html.escape(reason, quote=True)}">'
        '<div class="docling-formula-second-pass-label">'
        f"Formula {formula_no} kept at its source anchor: {html.escape(reason)}"
        "</div>"
        + source_formula_render
        + candidate
        + crop
        + f'<div class="docling-formula-fallback-source">Fallback source: {html.escape(fallback_source)}</div>'
        + '<div class="docling-formula-source">'
        + " | ".join(links)
        + "</div></div>"
    )


def _ensure_formula_second_pass_html_assets(html_text: str) -> tuple[str, bool]:
    """Add MathJax/styles for patched formula blocks while preserving raw TeX fallback."""
    if "docling-formula-second-pass-mathjax" in html_text:
        return html_text, False
    assets = """
<style id="docling-formula-second-pass-style">
.docling-formula-second-pass {
  border-left: 3px solid #2563eb;
  margin: 1rem 0;
  padding: 0.75rem 1rem;
  background: #f8fafc;
}
.docling-formula-second-pass-label {
  color: #475569;
  font: 0.85rem system-ui, sans-serif;
  margin-bottom: 0.5rem;
}
.docling-formula-render {
  overflow-x: auto;
}
.docling-formula-tex {
  background: #ffffff;
  border: 1px solid #cbd5e1;
  color: #0f172a;
  overflow-x: auto;
  padding: 0.5rem;
  white-space: pre-wrap;
}
.docling-formula-source {
  font: 0.85rem system-ui, sans-serif;
}
.docling-formula-unavailable {
  border: 1px solid #cbd5e1;
  background: #fff;
  padding: 0.6rem;
}
.docling-formula-fallback-evidence img {
  display: block;
  max-width: 100%;
}
.docling-formula-fallback-source {
  color: #64748b;
  font: 0.8rem system-ui, sans-serif;
}
</style>
<script id="docling-formula-second-pass-mathjax">
window.MathJax = window.MathJax || {
  tex: {inlineMath: [["\\\\(", "\\\\)"]], displayMath: [["\\\\[", "\\\\]"]]},
  svg: {fontCache: "global"}
};
</script>
<script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
"""
    if "</head>" in html_text:
        return html_text.replace("</head>", assets + "\n</head>", 1), True
    return assets + "\n" + html_text, True


FORMULA_MATH_BLOCK_RE = re.compile(r"<div><math\b(?:(?!</math></div>).)*?</math></div>", re.DOTALL)
FORMULA_FIGURE_BLOCK_RE = re.compile(r"<figure\b(?:(?!</figure>).)*?</figure>", re.DOTALL)
FORMULA_INDEX_ATTR_RE = re.compile(r'data-formula-index="(\d+)"')
FORMULA_OUTPUT_INDEX_RE = re.compile(
    r'<div class="docling-formula-second-pass[^"]*"[^>]*data-formula-index="(\d+)"'
)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_formula_anchor(text: str) -> str:
    alt_text = " ".join(re.findall(r'alt="([^"]*)"', text))
    text = HTML_TAG_RE.sub(" ", text)
    normalized = html.unescape(f"{text} {alt_text}").translate(
        str.maketrans({"（": "(", "）": ")", "［": "[", "］": "]"})
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _is_formula_figure_block(block: str) -> bool:
    if "<figcaption" in block:
        return False
    alt_match = re.search(r'alt="([^"]*)"', block)
    if not alt_match:
        return False
    alt_text = html.unescape(alt_match.group(1))
    return bool(
        re.search(
            r"(?:[=∑Σ]|\\(?:frac|sum|sqrt|begin|alpha|phi|sigma)|[（(]\s*\d+\s*[）)])",
            alt_text,
        )
    )


def _original_formula_visible_ranges(html_text: str) -> list[re.Match[str]]:
    blocks: list[re.Match[str]] = list(FORMULA_MATH_BLOCK_RE.finditer(html_text))
    blocks.extend(
        block
        for block in FORMULA_FIGURE_BLOCK_RE.finditer(html_text)
        if _is_formula_figure_block(block.group(0))
    )
    return sorted(blocks, key=lambda block: block.start())


def _formula_output_safety_reasons(text: str) -> list[str]:
    body = text.strip()
    reasons: list[str] = []
    reasons.extend(formula_hallucination_reasons(body))
    latex_ok, latex_reasons = validate_candidate_latex(body)
    if not latex_ok:
        reasons.extend(f"latex_{reason}" for reason in latex_reasons)
    if re.search(r"(?<![A-Za-z])(?:[A-Za-z]\s+){4,}[A-Za-z](?![A-Za-z])", body):
        reasons.append("garbled_letter_spaced_text")
    if CN_CHAR_RE.search(body):
        reasons.append("formula_contains_cjk_prose")
    if re.search(r"_\s*\{\s*(?:\\[,;:!]|\s)*_\s*\{", body):
        reasons.append("malformed_nested_subscript")
    if re.match(r"^\s*\\begin\s*\{\s*array\s*\}", body):
        relation_count = len(re.findall(r"(?<![<>])=(?!=)", body))
        row_count = len(re.findall(r"\\\\", body))
        if relation_count <= 1 and row_count <= 1:
            reasons.append("unnecessary_single_formula_array")
    for integral in re.finditer(
        r"\\int\s*_\s*\{\s*(?P<lower>[^{}]{1,32})\s*\}"
        r"\s*\^\s*\{\s*(?P<upper>[^{}]{1,32})\s*\}",
        body,
    ):
        lower = re.sub(r"\s+", "", integral.group("lower"))
        upper = re.sub(r"\s+", "", integral.group("upper"))
        if lower == upper:
            reasons.append("identical_integral_limits")
            break
    numbers = _compact_formula_numbers(body)
    if len(numbers) != len(set(numbers)):
        reasons.append("duplicate_equation_number")
    return list(dict.fromkeys(reasons))


def _formula_candidate_rank(text: str, equation_number: int | None) -> tuple[int, int, int]:
    normalized, _repairs = canonicalize_formula_output(text, equation_number)
    reasons = _formula_output_safety_reasons(normalized)
    body_length = len(re.sub(r"\s+", "", normalized))
    return (len(reasons), 0 if body_length >= 8 else 1, -min(body_length, 1200))


def _best_formula_fallback_text(entry: dict[str, Any]) -> tuple[str, str]:
    equation_number = entry.get("eq_number")
    eq_number = equation_number if isinstance(equation_number, int) else None
    candidates = [
        ("second_pass_candidate", str(entry.get("route_b_candidate") or "")),
        ("route_a_source", str(entry.get("route_a_text") or "")),
        ("raw_tex", str(entry.get("raw_tex") or "")),
    ]
    ranked: list[tuple[tuple[int, int, int], str, str]] = []
    for source, candidate in candidates:
        normalized, _repairs = canonicalize_formula_output(candidate, eq_number)
        if not normalized or formula_hallucination_reasons(normalized):
            continue
        ranked.append((_formula_candidate_rank(normalized, eq_number), normalized, source))
    if not ranked:
        return "", "unavailable"
    _rank, text, source = min(ranked, key=lambda item: item[0])
    return text[:1200], source


def _formula_fallback_contract_text(entry: dict[str, Any]) -> str:
    text, _source = _best_formula_fallback_text(entry)
    if text:
        return text
    formula_no = entry.get("eq_number") or entry.get("formula_no")
    suffix = f" \\quad ( {formula_no} )" if isinstance(formula_no, int) else ""
    return r"\text{Formula body unavailable; see source evidence}" + suffix


def _original_formula_html_ranges(html_text: str) -> tuple[list[re.Match[str]], dict[int, re.Match[str]]]:
    """Return formula MathML blocks and explicit data-formula-index mappings."""
    blocks = list(FORMULA_MATH_BLOCK_RE.finditer(html_text))
    by_index: dict[int, re.Match[str]] = {}
    for block in blocks:
        index_match = FORMULA_INDEX_ATTR_RE.search(block.group(0))
        if not index_match:
            continue
        formula_no = int(index_match.group(1))
        by_index.setdefault(formula_no, block)
    return blocks, by_index


def _find_formula_html_block_by_text(
    blocks: list[re.Match[str]],
    used_starts: set[int],
    formula_text: str,
) -> tuple[re.Match[str] | None, str | None]:
    """Find an unclaimed original MathML block containing the Route A formula text."""
    formula_anchor = _normalize_formula_anchor(formula_text)
    if not formula_anchor:
        return None, None

    candidates: list[str] = [formula_anchor]
    if len(formula_anchor) > 80:
        candidates.append(formula_anchor[:80])
    if len(formula_anchor) > 40:
        candidates.append(formula_anchor[:40])

    for candidate in candidates:
        if not candidate:
            continue
        matches: list[re.Match[str]] = []
        for block in blocks:
            if block.start() in used_starts:
                continue
            block_text = _normalize_formula_anchor(block.group(0))
            if candidate in block_text:
                matches.append(block)
        if len(matches) == 1:
            return matches[0], "route-a-text"
    return None, None


def _replace_original_html_ranges(
    html_text: str,
    replacements: list[tuple[int, re.Match[str], str]],
) -> str:
    """Apply replacements against byte ranges captured before any mutation."""
    result = html_text
    for _formula_no, match, replacement in sorted(
        replacements,
        key=lambda item: item[1].start(),
        reverse=True,
    ):
        result = result[: match.start()] + replacement + result[match.end() :]
    return result


def _formula_indexes_in_html(html_text: str) -> list[int]:
    return [int(match.group(1)) for match in FORMULA_OUTPUT_INDEX_RE.finditer(html_text)]


def _formula_anchor_range(html_text: str, formula_no: int) -> tuple[int, int] | None:
    existing = _find_existing_second_pass_formula_range(html_text, formula_no)
    if existing is not None:
        return existing
    marker = f'data-formula-index="{formula_no}"'
    for block in FORMULA_MATH_BLOCK_RE.finditer(html_text):
        if marker in block.group(0):
            return block.start(), block.end()
    return None


def _formula_neighborhood_insertion_index(
    html_text: str,
    entry: dict[str, Any],
    search_start: int = 0,
    search_end: int | None = None,
) -> tuple[int, str] | None:
    search_end = len(html_text) if search_end is None else search_end

    def find_snippet(text: str, *, reverse: bool) -> int:
        compact = " ".join(text.split())
        if len(compact) < 24:
            return -1
        probes = [compact, compact[:120], compact[:80], compact[:48]]
        for probe in probes:
            escaped = html.escape(probe, quote=False)
            index = (
                html_text.rfind(escaped, search_start, search_end)
                if reverse
                else html_text.find(escaped, search_start, search_end)
            )
            if index >= 0:
                return index
        return -1

    for before_text in reversed(entry.get("anchor_nearby_before") or []):
        match_at = find_snippet(str(before_text), reverse=True)
        if match_at < 0:
            continue
        paragraph_end = html_text.find("</p>", match_at, search_end)
        if paragraph_end >= 0:
            return paragraph_end + len("</p>"), "local-neighborhood-after"

    for after_text in entry.get("anchor_nearby_after") or []:
        match_at = find_snippet(str(after_text), reverse=False)
        if match_at < 0:
            continue
        paragraph_start = html_text.rfind("<p", search_start, match_at)
        if paragraph_start >= search_start:
            return paragraph_start, "local-neighborhood-before"
    return None


def _formula_anchor_insertion_index(
    html_text: str,
    formula_no: int,
    entry: dict[str, Any],
) -> tuple[int, str]:
    neighborhood = _formula_neighborhood_insertion_index(html_text, entry)
    if neighborhood is not None:
        return neighborhood
    output_markers = [
        (int(match.group(1)), match.start())
        for match in FORMULA_OUTPUT_INDEX_RE.finditer(html_text)
        if int(match.group(1)) > formula_no
    ]
    markers = output_markers or [
        (int(match.group(1)), match.start())
        for match in FORMULA_INDEX_ATTR_RE.finditer(html_text)
        if int(match.group(1)) > formula_no
    ]
    if markers:
        _next_no, marker_at = min(markers)
        second_pass_start = html_text.rfind(
            '<div class="docling-formula-second-pass"',
            0,
            marker_at,
        )
        if second_pass_start >= 0:
            return second_pass_start, "ordered-next-formula"
        for block in FORMULA_MATH_BLOCK_RE.finditer(html_text):
            if block.start() <= marker_at < block.end():
                return block.start(), "ordered-next-formula"
    body_end = html_text.rfind("</body>")
    return (
        body_end if body_end >= 0 else len(html_text),
        "document-end-fallback",
    )


def _infer_bounded_equation_number_sequence(entries: list[dict[str, Any]]) -> list[int]:
    recovered: list[int] = []
    index = 0
    while index < len(entries):
        if isinstance(entries[index].get("eq_number"), int):
            index += 1
            continue
        run_start = index
        while index < len(entries) and not isinstance(entries[index].get("eq_number"), int):
            index += 1
        run_end = index
        if run_start == 0 or run_end >= len(entries):
            continue
        previous = entries[run_start - 1].get("eq_number")
        following = entries[run_end].get("eq_number")
        run_length = run_end - run_start
        if (
            isinstance(previous, int)
            and isinstance(following, int)
            and following - previous == run_length + 1
        ):
            for offset, entry in enumerate(entries[run_start:run_end], start=1):
                entry["eq_number"] = previous + offset
                entry["equation_number_source"] = "bounded_rendered_sequence"
                recovered.append(int(entry["formula_no"]))
    return recovered


def patch_document_html_for_formula_second_pass(
    output_dir: Path,
    sidecar_dir: Path,
    replacement_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Render every second-pass outcome at its durable Route A formula anchor."""
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"ok": False, "error": f"document.html not found: {html_path}"}

    html_text = html_path.read_text(encoding="utf-8")
    patched_indexes: list[int] = []
    fallback_indexes: list[int] = []
    missing_indexes: list[int] = []
    duplicate_blocks_removed: dict[int, int] = {}
    patch_sources: dict[int, str] = {}
    entries = sorted(
        replacement_log,
        key=lambda item: int(item.get("formula_no") or 0),
    )
    original_blocks = _original_formula_visible_ranges(html_text)
    used_original_starts: set[int] = set()
    mapped_originals: dict[int, re.Match[str]] = {}
    for position, entry in enumerate(entries):
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int):
            continue
        match, source = _find_formula_html_block_by_text(
            original_blocks,
            used_original_starts,
            str(entry.get("route_a_text") or ""),
        )
        if match is None:
            eq_number = entry.get("eq_number")
            if isinstance(eq_number, int):
                match = _find_formula_html_block_by_number(
                    original_blocks,
                    used_original_starts,
                    eq_number,
                )
                if match is not None:
                    source = "rendered-equation-number"
        if (
            match is None
            and len(original_blocks) == len(entries)
            and position < len(original_blocks)
            and original_blocks[position].start() not in used_original_starts
            and (
                not FORMULA_INDEX_ATTR_RE.search(original_blocks[position].group(0))
                or int(FORMULA_INDEX_ATTR_RE.search(original_blocks[position].group(0)).group(1))
                == formula_no
            )
        ):
            match = original_blocks[position]
            source = "monotonic-rendered-position"
        if match is None:
            continue
        block_numbers = _compact_formula_numbers(_normalize_formula_anchor(match.group(0)))
        if not isinstance(entry.get("eq_number"), int):
            recovered_number = formula_no if formula_no in block_numbers else (
                block_numbers[-1] if len(block_numbers) == 1 else None
            )
            if recovered_number is not None:
                entry["eq_number"] = recovered_number
                entry["equation_number_source"] = "original_rendered_anchor"
        mapped_originals[formula_no] = match
        used_original_starts.add(match.start())
        patch_sources[formula_no] = f"replace-original-{source}"
        patched_indexes.append(formula_no)
        if entry.get("status") != "replaced":
            fallback_indexes.append(formula_no)
    recovered_sequence_indexes = _infer_bounded_equation_number_sequence(entries)
    original_replacements: list[tuple[int, re.Match[str], str]] = []
    for entry in entries:
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int) or formula_no not in mapped_originals:
            continue
        if entry.get("status") == "replaced":
            display_text = (
                _formula_text_with_number(
                    _second_pass_formula_display_text(entry),
                    int(entry["eq_number"]),
                )
                if isinstance(entry.get("eq_number"), int)
                else _second_pass_formula_display_text(entry)
            )
            entry["display_override"] = display_text
            entry["markdown_after"] = f"$${display_text}$$"
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if entry.get("status") == "replaced"
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        original_replacements.append((formula_no, mapped_originals[formula_no], replacement))
    if original_replacements:
        html_text = _replace_original_html_ranges(html_text, original_replacements)

    for entry in sorted(
        replacement_log,
        key=lambda item: int(item.get("formula_no") or 0),
        reverse=True,
    ):
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int):
            continue
        if formula_no in mapped_originals:
            continue
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if entry.get("status") == "replaced"
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        html_text, existing_changed, removed_count = _replace_existing_second_pass_formula_blocks(
            html_text,
            formula_no,
            replacement,
        )
        if existing_changed:
            patch_sources[formula_no] = "durable-existing-formula-block"
            if removed_count:
                duplicate_blocks_removed[formula_no] = removed_count
            patched_indexes.append(formula_no)
            if entry.get("status") != "replaced":
                fallback_indexes.append(formula_no)
            continue
        anchor_range = _formula_anchor_range(html_text, formula_no)
        if anchor_range is not None:
            start, end = anchor_range
            html_text = html_text[:start] + replacement + html_text[end:]
            patch_sources[formula_no] = "durable-data-formula-index"
        else:
            insertion_at, insertion_source = _formula_anchor_insertion_index(
                html_text,
                formula_no,
                entry,
            )
            html_text = html_text[:insertion_at] + replacement + "\n" + html_text[insertion_at:]
            patch_sources[formula_no] = f"anchor-missing-{insertion_source}"
            entry["html_anchor_status"] = f"anchor_missing_inserted_by_{insertion_source}"
        patched_indexes.append(formula_no)
        if entry.get("status") != "replaced":
            fallback_indexes.append(formula_no)

    restored_indexes: list[int] = []
    current_output_indexes = set(_formula_indexes_in_html(html_text))
    for entry in sorted(
        replacement_log,
        key=lambda item: int(item.get("formula_no") or 0),
        reverse=True,
    ):
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int) or formula_no in current_output_indexes:
            continue
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if entry.get("status") == "replaced"
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        insertion_at, insertion_source = _formula_anchor_insertion_index(
            html_text,
            formula_no,
            entry,
        )
        html_text = html_text[:insertion_at] + replacement + "\n" + html_text[insertion_at:]
        current_output_indexes.add(formula_no)
        restored_indexes.append(formula_no)
        patch_sources[formula_no] = f"anchor-restoration-{insertion_source}"

    assets_injected = False
    if patched_indexes:
        html_text, assets_injected = _ensure_formula_second_pass_html_assets(html_text)

    final_formula_indexes = _formula_indexes_in_html(html_text)
    duplicate_formula_indexes = sorted(
        index for index in set(final_formula_indexes) if final_formula_indexes.count(index) > 1
    )
    missing_indexes = sorted(
        int(entry["formula_no"])
        for entry in replacement_log
        if isinstance(entry.get("formula_no"), int)
        and int(entry["formula_no"]) not in set(final_formula_indexes)
    )

    html_path.write_text(html_text, encoding="utf-8")
    return {
        "ok": not missing_indexes and not duplicate_formula_indexes,
        "patched_indexes": sorted(patched_indexes),
        "fallback_indexes": sorted(fallback_indexes),
        "missing_indexes": missing_indexes,
        "duplicate_formula_indexes": duplicate_formula_indexes,
        "duplicate_blocks_removed": duplicate_blocks_removed,
        "restored_indexes": sorted(restored_indexes),
        "patch_sources": patch_sources,
        "recovered_equation_sequence_indexes": recovered_sequence_indexes,
        "final_formula_index_count": len(set(final_formula_indexes)),
        "rendering_assets_injected": assets_injected,
    }


def synchronize_formula_contract_outputs(
    output_dir: Path,
    replacement_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep final JSON and Markdown identical to the rendered formula outcome."""
    document = _load_json_file(output_dir / "document.json")
    json_patched: list[int] = []
    if isinstance(document, dict):
        formulas = extract_label_nodes(document, "formula")
        for entry in replacement_log:
            formula_no = entry.get("formula_no")
            if not isinstance(formula_no, int) or not (0 < formula_no <= len(formulas)):
                continue
            node = formulas[formula_no - 1]
            if entry.get("status") == "replaced":
                output_text = _second_pass_formula_display_text(entry)
            else:
                output_text = _formula_fallback_contract_text(
                    {**entry, "route_a_text": str(node.get("text") or "")}
                )
            if str(node.get("text") or "") != output_text:
                node["text"] = output_text
                json_patched.append(formula_no)
            node["local_ai_lab_formula_second_pass"] = {
                **(node.get("local_ai_lab_formula_second_pass") or {}),
                "status": entry.get("status"),
                "fallback_reason": entry.get("fallback_reason"),
                "final_html_anchor": entry.get("html_anchor_status") or "original_visible_formula_replaced",
                "equation_number": entry.get("eq_number"),
                "equation_number_source": entry.get("equation_number_source"),
            }
        (output_dir / "document.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    markdown_patched: list[int] = []
    md_path = output_dir / "document.md"
    if md_path.exists():
        md_text = md_path.read_text(encoding="utf-8")
        blocks = list(re.finditer(r"\$\$.*?\$\$", md_text, re.DOTALL))
        edits: list[tuple[int, int, str]] = []
        for entry in replacement_log:
            formula_no = entry.get("formula_no")
            if (
                not isinstance(formula_no, int)
                or not (0 < formula_no <= len(blocks))
            ):
                continue
            if entry.get("status") == "replaced":
                output_text = _second_pass_formula_display_text(entry)
            else:
                output_text = _formula_fallback_contract_text(
                    {
                        **entry,
                        "route_a_text": blocks[formula_no - 1].group(0)[2:-2].strip(),
                    }
                )
            replacement = f"$${output_text}$$"
            if entry.get("status") != "replaced":
                replacement += (
                    "\n<!-- formula-final-output-fallback "
                    f"formula={formula_no} reason="
                    f"{entry.get('fallback_reason') or entry.get('status')} -->"
                )
            block = blocks[formula_no - 1]
            if block.group(0) != replacement:
                edits.append((block.start(), block.end(), replacement))
                markdown_patched.append(formula_no)
        for start, end, replacement in sorted(edits, reverse=True):
            md_text = md_text[:start] + replacement + md_text[end:]
        if edits:
            md_path.write_text(md_text, encoding="utf-8")
    return {
        "ok": True,
        "json_patched_indexes": sorted(json_patched),
        "markdown_patched_indexes": sorted(markdown_patched),
    }


def validate_formula_second_pass_html(
    output_dir: Path,
    replacement_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify applied replacement text is visible or traceable in document.html."""
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"ok": False, "missing_replacements": [], "error": f"document.html not found: {html_path}"}
    raw_html = html_path.read_text(encoding="utf-8")
    decoded_html = html.unescape(raw_html)
    missing: list[int] = []
    fallback_missing: list[int] = []
    missing_equation_numbers: list[int] = []
    image_only_fallbacks: list[int] = []
    blank_visible_formulas: list[int] = []
    garbled_formula_indexes: list[int] = []
    json_mismatches: list[int] = []
    markdown_mismatches: list[int] = []
    document = _load_json_file(output_dir / "document.json")
    json_formulas = extract_label_nodes(document, "formula") if isinstance(document, dict) else []
    md_text = (output_dir / "document.md").read_text(encoding="utf-8") if (output_dir / "document.md").exists() else ""
    md_blocks = [match.group(0)[2:-2].strip() for match in re.finditer(r"\$\$.*?\$\$", md_text, re.DOTALL)]
    for entry in replacement_log:
        formula_no = entry.get("formula_no")
        marker = f'data-formula-index="{formula_no}"'
        if entry.get("status") == "replaced":
            display_text = _second_pass_formula_display_text(entry)
            render_marker = f"\\[{display_text}\\]"
            valid = (
                display_text in decoded_html
                and render_marker in decoded_html
                and marker in decoded_html
            )
        else:
            reason = str(entry.get("fallback_reason") or entry.get("status") or "")
            valid = (
                marker in decoded_html
                and "docling-formula-fallback" in decoded_html
                and reason in decoded_html
            )
            if not valid and isinstance(formula_no, int):
                fallback_missing.append(formula_no)
            if valid and isinstance(formula_no, int):
                html_range = _find_existing_second_pass_formula_range(raw_html, formula_no)
                block = raw_html[html_range[0] : html_range[1]] if html_range else ""
                if (
                    "docling-formula-preserved-source" not in block
                    and "docling-formula-unavailable" not in block
                ):
                    image_only_fallbacks.append(formula_no)
        if not valid:
            html_range = _find_existing_second_pass_formula_range(raw_html, formula_no)
            if html_range is not None:
                block = raw_html[html_range[0] : html_range[1]]
                if 'data-formula-status="cn_final_polish"' in block:
                    continue
            if isinstance(formula_no, int):
                missing.append(formula_no)
        if not isinstance(formula_no, int):
            continue
        html_range = _find_existing_second_pass_formula_range(raw_html, formula_no)
        block = raw_html[html_range[0] : html_range[1]] if html_range else ""
        visible_block = _visible_html_text(block)
        if not visible_block or (
            entry.get("status") == "replaced"
            and not _second_pass_formula_display_text(entry).strip()
        ):
            blank_visible_formulas.append(formula_no)
        if isinstance(entry.get("eq_number"), int):
            expected_number = int(entry["eq_number"])
            if expected_number not in _compact_formula_numbers(_normalize_formula_anchor(block)):
                missing_equation_numbers.append(formula_no)
        if entry.get("status") == "replaced":
            display_text = _second_pass_formula_display_text(entry)
            if _formula_output_safety_reasons(display_text):
                garbled_formula_indexes.append(formula_no)
            if formula_no > len(json_formulas) or str(json_formulas[formula_no - 1].get("text") or "").strip() != display_text:
                json_mismatches.append(formula_no)
            if formula_no > len(md_blocks) or md_blocks[formula_no - 1] != display_text:
                markdown_mismatches.append(formula_no)
    has_mathjax = "docling-formula-second-pass-mathjax" in decoded_html
    formula_indexes = _formula_indexes_in_html(raw_html)
    duplicate_formula_indexes = sorted(
        index for index in set(formula_indexes) if formula_indexes.count(index) > 1
    )
    expected_order = [
        int(entry["formula_no"])
        for entry in replacement_log
        if isinstance(entry.get("formula_no"), int)
    ]
    visible_offset = formula_indexes != expected_order
    remaining_original_formula_blocks = len(_original_formula_visible_ranges(raw_html))
    return {
        "ok": not any(
            (
                missing,
                duplicate_formula_indexes,
                missing_equation_numbers,
                image_only_fallbacks,
                blank_visible_formulas,
                garbled_formula_indexes,
                json_mismatches,
                markdown_mismatches,
                visible_offset,
                remaining_original_formula_blocks,
            )
        ),
        "missing_replacements": missing,
        "missing_fallbacks": fallback_missing,
        "duplicate_formula_indexes": duplicate_formula_indexes,
        "missing_equation_number_indexes": sorted(set(missing_equation_numbers)),
        "visible_formula_order": formula_indexes,
        "expected_formula_order": expected_order,
        "visible_offset": visible_offset,
        "remaining_original_formula_block_count": remaining_original_formula_blocks,
        "image_only_fallback_indexes": sorted(set(image_only_fallbacks)),
        "blank_visible_formula_indexes": sorted(set(blank_visible_formulas)),
        "garbled_formula_indexes": sorted(set(garbled_formula_indexes)),
        "json_formula_mismatch_indexes": sorted(set(json_mismatches)),
        "markdown_formula_mismatch_indexes": sorted(set(markdown_mismatches)),
        "mathjax_present": has_mathjax,
        "error": None,
    }


def _compact_formula_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for match in re.finditer(r"\(\s*(?:\\text\s*\{\s*)?\$?\s*((?:\d\s*){1,3})\s*\$?\s*(?:\}\s*)?\)", text):
        compact = re.sub(r"\s+", "", match.group(1))
        if compact:
            numbers.append(int(compact))
    for match in SPACED_FORMULA_NUMBER_RE.finditer(text):
        compact = re.sub(r"\s+", "", match.group(1))
        if compact:
            numbers.append(int(compact))
    deduped: list[int] = []
    for number in numbers:
        if number not in deduped:
            deduped.append(number)
    return deduped


def _formula_number_for_node(formula_no: int, node: dict[str, Any]) -> int | None:
    numbers = _compact_formula_numbers(str(node.get("text") or ""))
    if numbers:
        return numbers[0]
    if formula_no in CN_FINAL_POLISH_FORMULA_NUMBERS:
        return formula_no
    return None


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_formula_text_by_number(source_dir: Path) -> dict[int, str]:
    document = _load_json_file(source_dir / "document.json")
    if not isinstance(document, dict):
        return {}
    result: dict[int, str] = {}
    for index, formula in enumerate(extract_label_nodes(document, "formula"), start=1):
        formula_text = str(formula.get("text") or "").strip()
        formula_no = _formula_number_for_node(index, formula)
        if formula_no is None or not formula_text:
            continue
        result.setdefault(formula_no, formula_text)
    return result


def _load_formula_text_by_index(source_dir: Path) -> dict[int, str]:
    document = _load_json_file(source_dir / "document.json")
    if not isinstance(document, dict):
        return {}
    return {
        index: str(formula.get("text") or "").strip()
        for index, formula in enumerate(extract_label_nodes(document, "formula"), start=1)
        if str(formula.get("text") or "").strip()
    }


def _default_cn_guarded_fallback_dirs() -> list[Path]:
    baseline_output = Path(str(CN_ACCEPTED_BASELINE.get("output") or ""))
    return [baseline_output] if baseline_output.exists() else []


def _default_cn_route_b_dirs() -> list[Path]:
    route_b_output = Path(".runtime/review/docling-vlm-full-dir-review-2026-06-01/CN")
    return [route_b_output] if route_b_output.exists() else []


def _formula_text_with_number(text: str, formula_no: int) -> str:
    body = text.strip()
    if formula_no in _compact_formula_numbers(body):
        return body
    return f"{body} \\quad ( {formula_no} )"


def _cn_accepted_formula_source_texts(
    args: argparse.Namespace,
    sidecar_dir: Path,
) -> tuple[dict[int, str], dict[int, str]]:
    candidate_texts: dict[int, list[tuple[str, str]]] = {}
    guarded_sources = list(args.formula_second_pass_guarded_fallback_dir)
    if not guarded_sources:
        guarded_sources.extend(
            f"accepted_cn_baseline={path}" for path in _default_cn_guarded_fallback_dirs()
        )
    for value in guarded_sources:
        path_text = value.split("=", 1)[1] if "=" in value else value
        label = (
            "accepted_cn_baseline"
            if value.startswith("accepted_cn_baseline=")
            else "guarded_fallback_full"
        )
        for formula_no, text in _load_formula_text_by_index(Path(path_text)).items():
            candidate_texts.setdefault(formula_no, []).append((label, text))

    for route_b_dir in _default_cn_route_b_dirs():
        for formula_no, text in _load_formula_text_by_index(route_b_dir).items():
            candidate_texts.setdefault(formula_no, []).append(("route_b", text))

    summary = _load_json_file(sidecar_dir / "second_pass_summary.json")
    if isinstance(summary, dict):
        for entry in summary.get("replacement_log") or []:
            formula_no = entry.get("formula_no")
            if not isinstance(formula_no, int):
                continue
            candidate = str(entry.get("route_b_candidate") or "").strip()
            if candidate:
                candidate_texts.setdefault(formula_no, []).append(
                    (
                        str(entry.get("candidate_source") or "formula_second_pass"),
                        candidate,
                    )
                )

    current_document = _load_json_file(sidecar_dir / "document.json")
    if isinstance(current_document, dict):
        for formula_no, formula in enumerate(
            extract_label_nodes(current_document, "formula"),
            start=1,
        ):
            candidate_texts.setdefault(formula_no, []).append(
                ("current_formula_output", str(formula.get("text") or ""))
            )

    formula_texts: dict[int, str] = {}
    source_map: dict[int, str] = {}
    source_priority = {
        "guarded_fallback": 0,
        "guarded_fallback_full": 0,
        "accepted_cn_baseline": 0,
        "route_b": 1,
        "formula_second_pass": 1,
        "current_formula_output": 2,
    }
    for formula_no, candidates in candidate_texts.items():
        ranked: list[tuple[tuple[int, int, int, int], str, str]] = []
        for source, candidate in candidates:
            normalized, _repairs = canonicalize_formula_output(candidate, formula_no)
            if not normalized:
                continue
            base_rank = _formula_candidate_rank(normalized, formula_no)
            ranked.append(
                (
                    (
                        base_rank[0],
                        base_rank[1],
                        source_priority.get(source, 3),
                        base_rank[2],
                    ),
                    normalized,
                    source,
                )
            )
        if not ranked:
            continue
        _rank, selected, source = min(ranked, key=lambda item: item[0])
        formula_texts[formula_no] = selected
        source_map[formula_no] = source
    return formula_texts, source_map


def _patch_formula_json_nodes(
    output_dir: Path,
    formula_texts: dict[int, str],
    *,
    candidate_source: str = "cn_final_polish",
    prefer_index_anchor: bool = False,
) -> list[int]:
    json_path = output_dir / "document.json"
    document = _load_json_file(json_path)
    if not isinstance(document, dict):
        return []
    patched: list[int] = []
    for index, formula in enumerate(extract_label_nodes(document, "formula"), start=1):
        formula_no = index if prefer_index_anchor else _formula_number_for_node(index, formula)
        if formula_no not in formula_texts and index in formula_texts:
            formula_no = index
        if formula_no not in formula_texts:
            continue
        old_text = str(formula.get("text") or "")
        new_text = formula_texts[formula_no]
        safety_reasons = _formula_output_safety_reasons(new_text)
        formula["local_ai_lab_formula_second_pass"] = {
            "anchor_id": f"formula-{index}",
            "status": "replaced" if not safety_reasons else "final_output_unsafe",
            "candidate_source": candidate_source,
            "fallback_reason": ",".join(safety_reasons) or None,
        }
        if old_text == new_text:
            continue
        formula["text"] = new_text
        patched.append(formula_no)
    if patched:
        json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return patched


def _replace_markdown_formula_block(md_text: str, formula_no: int, formula_text: str) -> tuple[str, bool]:
    patterns = [
        r"\$\$[^$]*?\(\s*" + re.escape(str(formula_no)) + r"\s*\)[^$]*?\$\$",
    ]
    if formula_no >= 10:
        spaced = r"\s+".join(re.escape(char) for char in str(formula_no))
        patterns.append(r"\$\$[^$]*?\(\s*" + spaced + r"\s*\)[^$]*?\$\$")
    for pattern in patterns:
        updated, count = re.subn(
            pattern,
            lambda _match: f"$${formula_text}$$",
            md_text,
            count=1,
            flags=re.DOTALL,
        )
        if count:
            return updated, True
    return md_text, False


def _patch_markdown_formula_blocks(output_dir: Path, formula_texts: dict[int, str]) -> list[int]:
    md_path = output_dir / "document.md"
    if not md_path.exists():
        return []
    md_text = md_path.read_text(encoding="utf-8")
    patched: list[int] = []
    for formula_no, formula_text in formula_texts.items():
        md_text, changed = _replace_markdown_formula_block(md_text, formula_no, formula_text)
        if not changed:
            blocks = list(re.finditer(r"\$\$.*?\$\$", md_text, re.DOTALL))
            if 0 < formula_no <= len(blocks):
                match = blocks[formula_no - 1]
                md_text = md_text[: match.start()] + f"$${formula_text}$$" + md_text[match.end() :]
                changed = True
        if changed:
            patched.append(formula_no)
            safety_reasons = _formula_output_safety_reasons(formula_text)
            if safety_reasons:
                blocks = list(re.finditer(r"\$\$.*?\$\$", md_text, re.DOTALL))
                if 0 < formula_no <= len(blocks):
                    block = blocks[formula_no - 1]
                    comment = (
                        "\n<!-- formula-final-output-fallback "
                        f"formula={formula_no} reason={','.join(safety_reasons)} -->"
                    )
                    md_text = md_text[: block.end()] + comment + md_text[block.end() :]
    text_corrections: list[str] = []
    for pattern, new in CN_FINAL_TEXT_CORRECTIONS:
        md_text, count = pattern.subn(new, md_text)
        if count:
            text_corrections.append(new)
    if patched or text_corrections:
        md_path.write_text(md_text, encoding="utf-8")
    return patched


def _patch_html_text_corrections(html_text: str) -> tuple[str, list[str]]:
    applied: list[str] = []
    for pattern, new in CN_FINAL_TEXT_CORRECTIONS:
        html_text, count = pattern.subn(new, html_text)
        if count:
            applied.append(new)
    return html_text, applied


def _complete_cn_formula_html_sequence(
    html_text: str,
    output_dir: Path,
    sidecar_dir: Path,
    formula_texts: dict[int, str],
    formula_anchors: dict[int, dict[str, Any]],
) -> tuple[str, list[int]]:
    present = set(_formula_indexes_in_html(html_text))
    inserted: list[int] = []
    for formula_no in sorted(formula_texts, reverse=True):
        formula_text = formula_texts[formula_no]
        marker = f'data-formula-index="{formula_no}"'
        if marker in html_text:
            continue
        safety_reasons = _formula_output_safety_reasons(formula_text)
        entry = {
            "formula_no": formula_no,
            "status": "cn_final_polish" if not safety_reasons else "final_output_unsafe",
            "markdown_after": f"$${formula_text}$$",
            "route_a_text": formula_text,
            "fallback_reason": ",".join(safety_reasons) or None,
            **(formula_anchors.get(formula_no) or {}),
        }
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if not safety_reasons
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        next_numbers = sorted(number for number in present if number > formula_no)
        insertion_at = -1
        neighborhood = _formula_neighborhood_insertion_index(html_text, entry)
        if neighborhood is not None:
            insertion_at, _source = neighborhood
        if next_numbers:
            next_marker = f'data-formula-index="{next_numbers[0]}"'
            next_marker_at = html_text.find(next_marker)
            if insertion_at < 0 and next_marker_at >= 0:
                insertion_at = html_text.rfind(
                    '<div class="docling-formula-second-pass"',
                    0,
                    next_marker_at,
                )
        if insertion_at < 0:
            insertion_at = html_text.rfind("</body>")
        if insertion_at < 0:
            insertion_at = len(html_text)
        html_text = html_text[:insertion_at] + replacement + "\n" + html_text[insertion_at:]
        present.add(formula_no)
        inserted.append(formula_no)
    return html_text, sorted(inserted)


def _remove_numbered_original_formula_duplicates(
    html_text: str,
    formula_numbers: set[int],
) -> tuple[str, list[int]]:
    edits: list[tuple[int, int]] = []
    removed: list[int] = []
    for block in _original_formula_visible_ranges(html_text):
        numbers = _compact_formula_numbers(_normalize_formula_anchor(block.group(0)))
        matching = [number for number in numbers if number in formula_numbers]
        if len(matching) != 1:
            continue
        edits.append((block.start(), block.end()))
        removed.append(matching[0])
    for start, end in sorted(edits, reverse=True):
        html_text = html_text[:start] + html_text[end:]
    return html_text, sorted(removed)


def _remove_adjacent_original_formula_duplicates(
    html_text: str,
    formula_numbers: set[int],
) -> tuple[str, int]:
    edits: list[tuple[int, int]] = []
    second_pass_ranges = [
        formula_range
        for formula_no in formula_numbers
        for formula_range in _find_existing_second_pass_formula_ranges(html_text, formula_no)
    ]
    second_pass_ranges.sort()
    if not second_pass_ranges:
        return html_text, 0
    for original in _original_formula_visible_ranges(html_text):
        previous = next(
            (
                formula_range
                for formula_range in reversed(second_pass_ranges)
                if formula_range[1] <= original.start()
            ),
            None,
        )
        if previous is None:
            continue
        between = html_text[previous[1] : original.start()]
        if HTML_TAG_RE.sub("", between).strip():
            continue
        edits.append((original.start(), original.end()))
    for start, end in sorted(edits, reverse=True):
        html_text = html_text[:start] + html_text[end:]
    return html_text, len(edits)


def _repair_formula_visible_order(
    html_text: str,
    formula_anchors: dict[int, dict[str, Any]],
) -> tuple[str, list[int]]:
    repaired: list[int] = []
    for _attempt in range(len(formula_anchors)):
        changed = False
        for formula_no in sorted(formula_anchors):
            next_no = formula_no + 1
            if next_no not in formula_anchors:
                continue
            current_range = _find_existing_second_pass_formula_range(html_text, formula_no)
            next_range = _find_existing_second_pass_formula_range(html_text, next_no)
            if current_range is None or next_range is None or current_range[0] < next_range[0]:
                continue
            previous_range = _find_existing_second_pass_formula_range(html_text, formula_no - 1)
            block = html_text[current_range[0] : current_range[1]]
            html_text = html_text[: current_range[0]] + html_text[current_range[1] :]
            updated_next_range = _find_existing_second_pass_formula_range(html_text, next_no)
            lower_bound = previous_range[1] if previous_range is not None else 0
            upper_bound = updated_next_range[0] if updated_next_range is not None else len(html_text)
            entry = formula_anchors.get(formula_no) or {}
            neighborhood = _formula_neighborhood_insertion_index(
                html_text,
                entry,
                lower_bound,
                upper_bound,
            )
            insertion_at = neighborhood[0] if neighborhood is not None else upper_bound
            html_text = html_text[:insertion_at] + block + "\n" + html_text[insertion_at:]
            repaired.append(formula_no)
            changed = True
        if not changed:
            break
    return html_text, repaired


def _deduplicated_missing_formula_indexes(
    html_text: str,
    missing_indexes: list[int],
    patched_indexes: list[int],
    formula_texts: dict[int, str],
) -> list[int]:
    deduplicated: list[int] = []
    patched_by_text: dict[str, list[int]] = {}
    for index in patched_indexes:
        normalized = _normalized_noise_text(formula_texts.get(index, ""))
        if normalized:
            patched_by_text.setdefault(normalized, []).append(index)
    for index in missing_indexes:
        normalized = _normalized_noise_text(formula_texts.get(index, ""))
        if not normalized:
            continue
        source_indexes = patched_by_text.get(normalized) or []
        if any(f'data-formula-index="{source_index}"' in html_text for source_index in source_indexes):
            deduplicated.append(index)
    return deduplicated


def _patch_html_formula_blocks(
    output_dir: Path,
    sidecar_dir: Path,
    formula_texts: dict[int, str],
    *,
    status_label: str = "cn_final_polish",
    complete_missing_sequence: bool = True,
    allow_formula_number_match: bool = True,
) -> dict[str, Any]:
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"ok": False, "error": f"document.html not found: {html_path}"}
    html_text = html_path.read_text(encoding="utf-8")
    document = _load_json_file(output_dir / "document.json")
    formula_anchors: dict[int, dict[str, Any]] = {}
    if isinstance(document, dict):
        for formula in extract_formulas(document):
            formula_no = formula.get("formula_no")
            if isinstance(formula_no, int):
                formula_anchors[formula_no] = {
                    "anchor_nearby_before": formula.get("nearby_before"),
                    "anchor_nearby_after": formula.get("nearby_after"),
                }
    html_text, text_corrections = _patch_html_text_corrections(html_text)
    patched_indexes: list[int] = []
    missing_indexes: list[int] = []
    patch_sources: dict[int, str] = {}
    remaining_formula_texts: dict[int, str] = {}
    assets_injected = False
    for formula_no, formula_text in formula_texts.items():
        safety_reasons = _formula_output_safety_reasons(formula_text)
        entry = {
            "formula_no": formula_no,
            "status": status_label if not safety_reasons else "final_output_unsafe",
            "markdown_after": f"$${formula_text}$$",
            "route_a_text": formula_text,
            "fallback_reason": ",".join(safety_reasons) or None,
        }
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if not safety_reasons
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        html_text, changed = _replace_existing_second_pass_formula_block(
            html_text,
            formula_no,
            replacement,
        )
        if changed:
            patched_indexes.append(formula_no)
            patch_sources[formula_no] = "existing-second-pass-block"
            html_text, injected_now = _ensure_formula_second_pass_html_assets(html_text)
            assets_injected = assets_injected or injected_now
        else:
            remaining_formula_texts[formula_no] = formula_text

    original_blocks = _original_formula_visible_ranges(html_text)
    original_by_index: dict[int, re.Match[str]] = {}
    for block in original_blocks:
        index_match = FORMULA_INDEX_ATTR_RE.search(block.group(0))
        if index_match:
            original_by_index.setdefault(int(index_match.group(1)), block)
    replacements: list[tuple[int, re.Match[str], str]] = []
    used_original_starts: set[int] = set()
    for formula_no, formula_text in remaining_formula_texts.items():
        entry = {
            "formula_no": formula_no,
            "status": (
                status_label
                if not _formula_output_safety_reasons(formula_text)
                else "final_output_unsafe"
            ),
            "markdown_after": f"$${formula_text}$$",
            "route_a_text": formula_text,
            "fallback_reason": ",".join(_formula_output_safety_reasons(formula_text)) or None,
        }
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if entry["status"] == status_label
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        match = original_by_index.get(formula_no)
        if match is None:
            match, source = _find_formula_html_block_by_text(
                original_blocks,
                used_original_starts,
                formula_text,
            )
            if match is not None:
                patch_sources[formula_no] = str(source)
        else:
            patch_sources[formula_no] = "data-formula-index"
        if match is None and allow_formula_number_match:
            old_text_probe = _find_formula_html_block_by_number(original_blocks, used_original_starts, formula_no)
            if old_text_probe is not None:
                match = old_text_probe
                patch_sources[formula_no] = "formula-number"
        if match is None and 0 < formula_no <= len(original_blocks):
            order_probe = original_blocks[formula_no - 1]
            if order_probe.start() not in used_original_starts:
                match = order_probe
                patch_sources[formula_no] = "formula-order"
        if match is None:
            missing_indexes.append(formula_no)
            continue
        replacements.append((formula_no, match, replacement))
        used_original_starts.add(match.start())
        patched_indexes.append(formula_no)
    if replacements:
        html_text = _replace_original_html_ranges(html_text, replacements)
        html_text, injected_now = _ensure_formula_second_pass_html_assets(html_text)
        assets_injected = assets_injected or injected_now
    still_missing: list[int] = []
    for formula_no in missing_indexes:
        formula_text = formula_texts[formula_no]
        safety_reasons = _formula_output_safety_reasons(formula_text)
        entry = {
            "formula_no": formula_no,
            "status": status_label if not safety_reasons else "final_output_unsafe",
            "markdown_after": f"$${formula_text}$$",
            "route_a_text": formula_text,
            "fallback_reason": ",".join(safety_reasons) or None,
        }
        replacement = (
            _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
            if not safety_reasons
            else _render_formula_fallback_html(entry, output_dir, sidecar_dir)
        )
        html_text, changed = _replace_existing_second_pass_formula_block(
            html_text,
            formula_no,
            replacement,
        )
        if changed:
            patched_indexes.append(formula_no)
            patch_sources[formula_no] = "existing-second-pass-block"
            html_text, injected_now = _ensure_formula_second_pass_html_assets(html_text)
            assets_injected = assets_injected or injected_now
        else:
            still_missing.append(formula_no)
    missing_indexes = still_missing
    if complete_missing_sequence:
        html_text, sequence_completion_indexes = _complete_cn_formula_html_sequence(
            html_text,
            output_dir,
            sidecar_dir,
            formula_texts,
            formula_anchors,
        )
    else:
        sequence_completion_indexes = []
    if sequence_completion_indexes:
        html_text, injected_now = _ensure_formula_second_pass_html_assets(html_text)
        assets_injected = assets_injected or injected_now
        for formula_no in sequence_completion_indexes:
            patch_sources[formula_no] = "ordered-cn-sequence-completion"
            if formula_no not in patched_indexes:
                patched_indexes.append(formula_no)
        missing_indexes = [
            formula_no
            for formula_no in missing_indexes
            if formula_no not in sequence_completion_indexes
        ]
    html_text, removed_original_duplicates = _remove_numbered_original_formula_duplicates(
        html_text,
        set(formula_texts),
    )
    html_text, adjacent_original_duplicate_count = _remove_adjacent_original_formula_duplicates(
        html_text,
        set(formula_texts),
    )
    html_text, visible_order_repairs = _repair_formula_visible_order(
        html_text,
        formula_anchors,
    )
    deduplicated_missing_indexes = _deduplicated_missing_formula_indexes(
        html_text,
        missing_indexes,
        patched_indexes,
        formula_texts,
    )
    missing_indexes = [
        index for index in missing_indexes if index not in deduplicated_missing_indexes
    ]
    html_path.write_text(html_text, encoding="utf-8")
    return {
        "ok": not missing_indexes,
        "patched_indexes": patched_indexes,
        "missing_indexes": sorted(set(missing_indexes)),
        "deduplicated_missing_indexes": deduplicated_missing_indexes,
        "patch_sources": patch_sources,
        "sequence_completion_indexes": sequence_completion_indexes,
        "removed_original_duplicate_indexes": removed_original_duplicates,
        "removed_adjacent_original_duplicate_count": adjacent_original_duplicate_count,
        "visible_order_repair_indexes": visible_order_repairs,
        "text_corrections": text_corrections,
        "rendering_assets_injected": assets_injected,
    }


def _find_formula_html_block_by_number(
    blocks: list[re.Match[str]],
    used_starts: set[int],
    formula_no: int,
) -> re.Match[str] | None:
    patterns = [r"\(\s*" + re.escape(str(formula_no)) + r"\s*\)"]
    if formula_no >= 10:
        patterns.append(r"\(\s*" + r"\s+".join(re.escape(char) for char in str(formula_no)) + r"\s*\)")
    candidates: list[re.Match[str]] = []
    for block in blocks:
        if block.start() in used_starts:
            continue
        block_text = _normalize_formula_anchor(block.group(0))
        if any(re.search(pattern, block_text) for pattern in patterns):
            candidates.append(block)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _find_existing_second_pass_formula_ranges(
    html_text: str,
    formula_no: int,
) -> list[tuple[int, int]]:
    marker = f'data-formula-index="{formula_no}"'
    ranges: list[tuple[int, int]] = []
    search_from = 0
    while True:
        marker_at = html_text.find(marker, search_from)
        if marker_at < 0:
            return ranges
        search_from = marker_at + len(marker)
        start = html_text.rfind('<div class="docling-formula-second-pass', 0, marker_at)
        if start < 0:
            continue
        if ranges and ranges[-1][0] == start:
            continue
        depth = 0
        for tag_match in re.finditer(r"</?div\b[^>]*>", html_text[start:], flags=re.I):
            tag = tag_match.group(0)
            if tag.startswith("</"):
                depth -= 1
                if depth == 0:
                    ranges.append((start, start + tag_match.end()))
                    break
            else:
                depth += 1


def _find_existing_second_pass_formula_range(
    html_text: str,
    formula_no: int,
) -> tuple[int, int] | None:
    ranges = _find_existing_second_pass_formula_ranges(html_text, formula_no)
    return ranges[0] if ranges else None


def _replace_existing_second_pass_formula_blocks(
    html_text: str,
    formula_no: int,
    replacement: str,
) -> tuple[str, bool, int]:
    ranges = _find_existing_second_pass_formula_ranges(html_text, formula_no)
    if not ranges:
        return html_text, False, 0
    edits: list[tuple[int, int, str]] = [(ranges[0][0], ranges[0][1], replacement)]
    edits.extend((start, end, "") for start, end in ranges[1:])
    for start, end, text in sorted(edits, reverse=True):
        html_text = html_text[:start] + text + html_text[end:]
    return html_text, True, max(0, len(ranges) - 1)


def _replace_existing_second_pass_formula_block(
    html_text: str,
    formula_no: int,
    replacement: str,
) -> tuple[str, bool]:
    html_text, changed, _removed_count = _replace_existing_second_pass_formula_blocks(
        html_text,
        formula_no,
        replacement,
    )
    return html_text, changed


def apply_cn_final_document_polish(
    output_dir: Path,
    sidecar_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.input_file.name != "CN.pdf":
        return {"ok": True, "applied": False, "reason": "not_cn_pdf"}
    if not args.formula_second_pass_guarded_fallback_dir and not _default_cn_guarded_fallback_dirs():
        return {
            "ok": True,
            "applied": False,
            "reason": "no_guarded_fallback_source_for_cn_final_polish",
        }
    formula_texts, source_map = _cn_accepted_formula_source_texts(args, sidecar_dir)
    missing_sources = [
        formula_no
        for formula_no in CN_ACCEPTED_BASELINE["equation_numbers"]
        if formula_no not in formula_texts
    ]
    json_patched = _patch_formula_json_nodes(output_dir, formula_texts)
    markdown_patched = _patch_markdown_formula_blocks(output_dir, formula_texts)
    html_patch = _patch_html_formula_blocks(output_dir, sidecar_dir, formula_texts)
    ok = not missing_sources and bool(html_patch.get("ok"))
    return {
        "ok": ok,
        "applied": True,
        "formula_texts": sorted(formula_texts),
        "candidate_sources": source_map,
        "missing_source_formulas": missing_sources,
        "document_json_patched": json_patched,
        "document_md_patched": markdown_patched,
        "document_html_patch": html_patch,
    }


def _current_formula_display_texts(output_dir: Path) -> tuple[dict[int, str], dict[int, str]]:
    document = _load_json_file(output_dir / "document.json")
    if not isinstance(document, dict):
        return {}, {}
    formula_texts: dict[int, str] = {}
    source_map: dict[int, str] = {}
    for index, formula in enumerate(extract_label_nodes(document, "formula"), start=1):
        raw_text = str(formula.get("text") or "").strip()
        if not raw_text:
            continue
        eq_numbers = _compact_formula_numbers(raw_text)
        eq_number = eq_numbers[0] if eq_numbers else None
        normalized = normalize_formula_candidate(raw_text)
        if eq_number is not None:
            normalized, _repairs = canonicalize_formula_output(normalized, eq_number)
        formula_texts[index] = normalized or raw_text
        source_map[index] = "current_formula_json"
    return formula_texts, source_map


def apply_current_formula_display_fallback(
    output_dir: Path,
    metadata: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
    *,
    reason: str,
) -> dict[str, Any]:
    def write_state() -> None:
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    if args.input_file.name == "CN.pdf":
        result = {"ok": True, "applied": False, "reason": "skip_cn_accepted_formula_path"}
        metadata["current_formula_display_fallback"] = result
        status["quality_signals"]["current_formula_display_fallback"] = result
        write_state()
        return result
    html_path = output_dir / "document.html"
    if html_path.exists() and "docling-formula-second-pass" in html_path.read_text(encoding="utf-8"):
        result = {"ok": True, "applied": False, "reason": "formula_second_pass_blocks_already_present"}
        metadata["current_formula_display_fallback"] = result
        status["quality_signals"]["current_formula_display_fallback"] = result
        write_state()
        return result
    formula_texts, source_map = _current_formula_display_texts(output_dir)
    if not formula_texts:
        result = {"ok": True, "applied": False, "reason": "no_formula_candidates"}
        metadata["current_formula_display_fallback"] = result
        status["quality_signals"]["current_formula_display_fallback"] = result
        write_state()
        return result
    sidecar_dir = output_dir / "formula_display_fallback"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    json_patched = _patch_formula_json_nodes(
        output_dir,
        formula_texts,
        candidate_source="current_formula_display_fallback",
        prefer_index_anchor=True,
    )
    markdown_patched = _patch_markdown_formula_blocks(output_dir, formula_texts)
    html_patch = _patch_html_formula_blocks(
        output_dir,
        sidecar_dir,
        formula_texts,
        status_label="current_formula_display_fallback",
        complete_missing_sequence=False,
        allow_formula_number_match=False,
    )
    unsafe_indexes = [
        formula_no
        for formula_no, formula_text in formula_texts.items()
        if _formula_output_safety_reasons(formula_text)
    ]
    result = {
        "ok": bool(html_patch.get("ok")),
        "applied": True,
        "reason": reason,
        "formula_count": len(formula_texts),
        "formula_texts": sorted(formula_texts),
        "candidate_sources": source_map,
        "unsafe_formula_indexes": unsafe_indexes,
        "document_json_patched": json_patched,
        "document_md_patched": markdown_patched,
        "document_html_patch": html_patch,
    }
    metadata["current_formula_display_fallback"] = result
    status["quality_signals"]["current_formula_display_fallback"] = result
    status["warnings"].append(
        "current_formula_display_fallback:"
        f"{reason}:patched={len(html_patch.get('patched_indexes') or [])}:"
        f"unsafe={len(unsafe_indexes)}"
    )
    if result["ok"] and status.get("success_class") == "failure":
        status["success_class"] = "degraded_success"
    write_state()
    return result


def run_optional_formula_second_pass(
    output_dir: Path,
    metadata: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Run optional formula-only second pass and update adapter metadata/status."""
    requested_policy = args.formula_second_pass_policy
    policy = effective_formula_second_pass_policy(args)
    metadata["formula_second_pass_requested_policy"] = requested_policy
    metadata["formula_second_pass_policy"] = policy
    status["quality_signals"]["formula_second_pass_requested_policy"] = requested_policy
    status["quality_signals"]["formula_second_pass_policy"] = policy
    if requested_policy != policy:
        status["warnings"].append(
            f"formula_second_pass_policy_resolved:{requested_policy}->{policy}:"
            "preserve_accepted_cn_0854aa1_path"
        )
    if policy == "off":
        apply_current_formula_display_fallback(
            output_dir,
            metadata,
            status,
            args,
            reason="formula_second_pass_policy_off",
        )
        return

    def write_updated_contract_state() -> None:
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (output_dir / "status.json").write_text(
            json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    route_b_dir = args.formula_second_pass_route_b_dir
    sidecar_dir = args.formula_second_pass_output_dir or (output_dir / "formula_second_pass")
    if route_b_dir is None:
        current_document = _load_json_file(output_dir / "document.json")
        current_formula_count = (
            len(extract_label_nodes(current_document, "formula"))
            if isinstance(current_document, dict)
            else 0
        )
        if current_formula_count == 0:
            formula_summary = {
                "ok": True,
                "policy": policy,
                "route_a_dir": str(output_dir),
                "route_b_dir": None,
                "output_dir": str(sidecar_dir),
                "route_a_formula_count": 0,
                "route_b_formula_count": None,
                "suspicious_formula_count": 0,
                "second_pass_attempted_count": 0,
                "replaced_count": 0,
                "no_match_count": 0,
                "fallback_count": 0,
                "alignment_diagnostics": formula_second_pass_alignment_diagnostics([], 0),
                "error": None,
            }
            metadata["formula_second_pass"] = formula_summary
            metadata["formula_second_pass_applied"] = False
            status["quality_signals"]["formula_second_pass"] = formula_summary
            status["quality_signals"]["formula_second_pass_applied"] = False
            status["warnings"].append("formula_second_pass_skipped:no_formula_candidates")
            write_updated_contract_state()
            return
        message = "formula_second_pass_route_b_dir_required"
        if is_cn_accepted_path(args):
            cn_final_polish = apply_cn_final_document_polish(output_dir, sidecar_dir, args)
            metadata["cn_final_document_polish"] = cn_final_polish
            status["quality_signals"]["cn_final_document_polish"] = cn_final_polish
            metadata["formula_second_pass"] = {
                "ok": bool(cn_final_polish.get("ok")),
                "error": message,
                "fallback": "accepted_cn_baseline_final_polish",
            }
            status["warnings"].append(message)
            status["quality_signals"]["formula_second_pass"] = metadata["formula_second_pass"]
            if not cn_final_polish.get("ok"):
                status["ok"] = False
                status["success_class"] = "degraded_failure"
            write_updated_contract_state()
            return
        fallback_result = apply_current_formula_display_fallback(
            output_dir,
            metadata,
            status,
            args,
            reason=message,
        )
        metadata["formula_second_pass"] = {
            "ok": bool(fallback_result.get("ok")),
            "error": message,
            "fallback": "current_formula_display_fallback",
        }
        status["warnings"].append(message)
        status["quality_signals"]["formula_second_pass"] = metadata["formula_second_pass"]
        if not fallback_result.get("ok"):
            status["ok"] = False
            status["success_class"] = "degraded_failure"
        write_updated_contract_state()
        return

    result = run_formula_second_pass(
        route_a_dir=output_dir,
        route_b_dir=route_b_dir,
        output_dir=sidecar_dir,
        review_candidate_args=args.formula_second_pass_review_candidate_dir,
        guarded_fallback_args=args.formula_second_pass_guarded_fallback_dir,
        guarded_fallback_eqs=set(args.formula_second_pass_guarded_fallback_eq),
        apply_all=policy == "apply-all",
    )
    formula_summary = {
        "ok": bool(result.get("ok")),
        "policy": policy,
        "route_a_dir": str(output_dir),
        "route_b_dir": str(route_b_dir),
        "output_dir": str(sidecar_dir),
        "review_html_path": result.get("review_html_path"),
        "route_a_formula_count": result.get("route_a_formula_count"),
        "route_b_formula_count": result.get("route_b_formula_count"),
        "suspicious_formula_count": result.get("suspicious_formula_count"),
        "second_pass_attempted_count": result.get("second_pass_attempted_count"),
        "replaced_count": result.get("replaced_count"),
        "no_match_count": result.get("no_match_count"),
        "fallback_count": result.get("fallback_count"),
        "crop_only_without_formula_count": result.get("crop_only_without_formula_count"),
        "render_failed_latex_count": result.get("render_failed_latex_count"),
        "guarded_fallback_eqs": sorted(args.formula_second_pass_guarded_fallback_eq),
        "error": result.get("error"),
    }
    alignment_diag = formula_second_pass_alignment_diagnostics(
        list(result.get("replacement_log") or []),
        result.get("route_a_formula_count"),
    )
    formula_summary["alignment_diagnostics"] = alignment_diag
    metadata["formula_second_pass"] = formula_summary
    status["quality_signals"]["formula_second_pass"] = formula_summary

    if not result.get("ok"):
        status["warnings"].append(f"formula_second_pass_failed:{result.get('error')}")
        status["ok"] = False
        status["success_class"] = "degraded_failure"
        write_updated_contract_state()
        return

    metadata.setdefault("generated_outputs", []).extend(
        [
            str((sidecar_dir / "document.md").relative_to(output_dir))
            if sidecar_dir.is_relative_to(output_dir)
            else str(sidecar_dir / "document.md"),
            str((sidecar_dir / "document.json").relative_to(output_dir))
            if sidecar_dir.is_relative_to(output_dir)
            else str(sidecar_dir / "document.json"),
            str((sidecar_dir / "second_pass_summary.json").relative_to(output_dir))
            if sidecar_dir.is_relative_to(output_dir)
            else str(sidecar_dir / "second_pass_summary.json"),
            str((sidecar_dir / "review_index.html").relative_to(output_dir))
            if sidecar_dir.is_relative_to(output_dir)
            else str(sidecar_dir / "review_index.html"),
        ]
    )
    status["warnings"].append(
        "formula_second_pass_completed:"
        f"{policy}:suspicious={result.get('suspicious_formula_count')}:"
        f"attempted={result.get('second_pass_attempted_count')}:"
        f"replaced={result.get('replaced_count')}:no_match={result.get('no_match_count')}"
    )
    if not alignment_diag.get("all_formulas_attempted"):
        status["warnings"].append(
            "formula_second_pass_missing_attempts:"
            + ",".join(str(index) for index in alignment_diag.get("missing_attempt_indexes") or [])
        )
    if alignment_diag.get("sequence_mismatch_count"):
        status["warnings"].append(
            "formula_sequence_mismatch:"
            f"count={alignment_diag.get('sequence_mismatch_count')}:"
            f"downstream_offset_after={alignment_diag.get('downstream_offset_risk_after_formula')}"
        )
    if alignment_diag.get("duplicate_equation_number_count"):
        status["warnings"].append(
            "duplicate_equation_numbers:"
            f"count={alignment_diag.get('duplicate_equation_number_count')}"
        )
    if alignment_diag.get("missing_body_number_only_count"):
        status["warnings"].append(
            "formula_missing_body_number_only:"
            f"count={alignment_diag.get('missing_body_number_only_count')}"
        )
    if alignment_diag.get("image_formula_not_converted_count"):
        status["warnings"].append(
            "image_formula_not_converted:"
            f"count={alignment_diag.get('image_formula_not_converted_count')}"
        )
    if alignment_diag.get("anchor_mismatch_count"):
        status["warnings"].append(
            "formula_anchor_mismatch:"
            f"count={alignment_diag.get('anchor_mismatch_count')}:"
            f"downstream_shift={alignment_diag.get('downstream_offset_risk')}"
        )
    if alignment_diag.get("crop_only_without_formula_count"):
        status["warnings"].append(
            "formula_crop_only_without_formula:"
            f"count={alignment_diag.get('crop_only_without_formula_count')}"
        )
    if alignment_diag.get("render_failed_latex_count"):
        status["warnings"].append(
            "formula_render_failed_latex:"
            f"count={alignment_diag.get('render_failed_latex_count')}"
        )
    if alignment_diag.get("second_pass_not_applied_count"):
        status["warnings"].append(
            "formula_second_pass_not_applied:"
            f"count={alignment_diag.get('second_pass_not_applied_count')}"
        )
    if policy in {"apply", "apply-all"}:
        shutil.copyfile(sidecar_dir / "document.md", output_dir / "document.md")
        shutil.copyfile(sidecar_dir / "document.json", output_dir / "document.json")
        patched_document = _load_json_file(output_dir / "document.json")
        if isinstance(patched_document, dict):
            patched_formulas = extract_label_nodes(patched_document, "formula")
            formula_latex_sources = write_formula_latex_sources(output_dir, patched_formulas)
            metadata["formula_latex_sources"] = formula_latex_sources
            status["quality_signals"]["formula_latex_sources"] = formula_latex_sources
        if is_cn_accepted_path(args):
            html_patch = {
                "ok": True,
                "applied": False,
                "reason": "cn_accepted_path_owns_formula_html",
            }
        else:
            html_patch = patch_document_html_for_formula_second_pass(
                output_dir,
                sidecar_dir,
                list(result.get("replacement_log") or []),
            )
        contract_sync = synchronize_formula_contract_outputs(
            output_dir,
            list(result.get("replacement_log") or []),
        )
        cn_final_polish = apply_cn_final_document_polish(output_dir, sidecar_dir, args)
        validation_log = list(result.get("replacement_log") or [])
        if cn_final_polish.get("applied"):
            final_document = _load_json_file(output_dir / "document.json")
            if isinstance(final_document, dict):
                validation_log = []
                for formula_no, formula in enumerate(
                    extract_label_nodes(final_document, "formula"),
                    start=1,
                ):
                    formula_text = str(formula.get("text") or "")
                    formula_meta = formula.get("local_ai_lab_formula_second_pass") or {}
                    formula_status = str(formula_meta.get("status") or "replaced")
                    validation_log.append(
                        {
                            "formula_no": formula_no,
                            "status": "replaced" if formula_status == "replaced" else formula_status,
                            "display_override": formula_text,
                            "route_a_text": formula_text,
                            "eq_number": formula_no,
                            "fallback_reason": formula_meta.get("fallback_reason"),
                        }
                    )
        html_gate = validate_formula_second_pass_html(
            output_dir,
            validation_log,
        )
        metadata["formula_second_pass_html_patch"] = html_patch
        metadata["formula_second_pass_html_gate"] = html_gate
        metadata["formula_second_pass_contract_sync"] = contract_sync
        metadata["cn_final_document_polish"] = cn_final_polish
        status["quality_signals"]["formula_second_pass_html_patch"] = html_patch
        status["quality_signals"]["formula_second_pass_html_gate"] = html_gate
        status["quality_signals"]["formula_second_pass_contract_sync"] = contract_sync
        status["quality_signals"]["cn_final_document_polish"] = cn_final_polish
        if not html_patch.get("ok") or not html_gate.get("ok") or not cn_final_polish.get("ok"):
            status["ok"] = False
            status["success_class"] = "degraded_failure"
            status["warnings"].append(
                "formula_second_pass_html_gate_failed:"
                f"missing={html_gate.get('missing_replacements')}:"
                f"unpatched={html_patch.get('missing_indexes')}"
            )
            if not cn_final_polish.get("ok"):
                status["warnings"].append(
                    "cn_final_document_polish_failed:"
                    f"missing_sources={cn_final_polish.get('missing_source_formulas')}:"
                    f"html_missing={cn_final_polish.get('document_html_patch', {}).get('missing_indexes')}"
                )
        elif any(
            source.startswith("anchor-missing-")
            for source in (html_patch.get("patch_sources") or {}).values()
        ):
            status["warnings"].append(
                "formula_html_anchor_missing_fallback:"
                + ",".join(
                    str(index)
                    for index, source in (html_patch.get("patch_sources") or {}).items()
                    if source.startswith("anchor-missing-")
                )
            )
        metadata["formula_second_pass_applied"] = True
        status["quality_signals"]["formula_second_pass_applied"] = True
    else:
        metadata["formula_second_pass_applied"] = False
        status["quality_signals"]["formula_second_pass_applied"] = False

    write_updated_contract_state()


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

    original_input_file = args.input_file
    text_layer_recovery = find_text_layer_recovery_source(original_input_file)
    conversion_args = args
    if text_layer_recovery.get("applied") and text_layer_recovery.get("source_path"):
        conversion_args = args_with_conversion_input(
            args,
            Path(str(text_layer_recovery["source_path"])),
        )

    name = args.job_id or args.sample_name or args.input_file.stem
    output_dir = args.output_root / name

    try:
        version = get_json(f"{args.serve_url.rstrip('/')}/version")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "blocked": f"Docling Server is not reachable: {exc}",
                    "start_command": START_COMMAND,
                },
                indent=2,
            )
        )
        return 2

    response, metadata, status = run_conversion(conversion_args, name, force_ocr=False)
    metadata["original_input_file"] = str(original_input_file)
    metadata["conversion_input_file"] = str(conversion_args.input_file)
    metadata["text_layer_recovery"] = text_layer_recovery
    status["quality_signals"]["text_layer_recovery"] = text_layer_recovery
    if text_layer_recovery.get("applied"):
        status["warnings"].insert(
            0,
            "image_only_pdf_text_layer_recovery_used:"
            f"{text_layer_recovery.get('reason')}",
        )
    elif (text_layer_recovery.get("input_profile") or {}).get("image_only_candidate"):
        status["warnings"].insert(
            0,
            "image_only_pdf_no_text_layer_recovery_source:"
            f"{text_layer_recovery.get('reason')};ocr_quality_requires_manual_review",
        )
    metadata["docling_serve_version"] = version
    status["docling_serve_version"] = version

    gxx_count = metadata["text_quality_gxx_count"]
    gxx_density = metadata["text_quality_gxx_density"]
    cn_ocr_parity = effective_cn_ocr_parity(conversion_args)
    should_fallback = (
        effective_ocr_fallback_policy(args) == "gxx"
        and gxx_count >= args.gxx_count_threshold
        and gxx_density >= args.gxx_density_threshold
    )

    if should_fallback:
        first_pass_response = response
        try:
            response, metadata, status = run_conversion(
                conversion_args,
                name,
                force_ocr=True,
                cn_ocr_parity=cn_ocr_parity,
            )
        except Exception as exc:
            page_count = page_count_from_document(
                (first_pass_response.get("document") or {}).get("json_content")
            )
            if cn_ocr_parity and is_transient_http_error(exc) and page_count:
                response, metadata, status = run_cn_chunked_fallback(
                    conversion_args,
                    name,
                    page_count=page_count,
                )
                metadata["original_input_file"] = str(original_input_file)
                metadata["conversion_input_file"] = str(conversion_args.input_file)
                metadata["text_layer_recovery"] = text_layer_recovery
                status["quality_signals"]["text_layer_recovery"] = text_layer_recovery
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
                    conversion_args,
                    force_ocr=True,
                    cn_ocr_parity=cn_ocr_parity,
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
                            if cn_ocr_parity
                            else "ocr_fallback_force_ocr_request"
                        ),
                    ],
                    conversion_args,
                    output_dir,
                )
                metadata["original_input_file"] = str(original_input_file)
                metadata["conversion_input_file"] = str(conversion_args.input_file)
                metadata["text_layer_recovery"] = text_layer_recovery
                status["quality_signals"]["text_layer_recovery"] = text_layer_recovery
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
            metadata["original_input_file"] = str(original_input_file)
            metadata["conversion_input_file"] = str(conversion_args.input_file)
            metadata["text_layer_recovery"] = text_layer_recovery
            status["quality_signals"]["text_layer_recovery"] = text_layer_recovery
            metadata["ocr_fallback_reason"] = "gxx_quality_failure"
            metadata["ocr_fallback_mode"] = (
                "full_document_ocrmac" if cn_ocr_parity else "full_document"
            )
            metadata["ocr_fallback_pages"] = "all"
            status["warnings"].insert(
                0,
                (
                    "text_quality_failed_gxx; forced OCR fallback via "
                    + (
                        "Docling Server OCRMac full-page request"
                        if cn_ocr_parity
                        else "Docling Server force_ocr=true"
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
        "Review artifacts are adapter-owned post-processing outputs, not native Docling Server outputs.",
        "This adapter is a minimal n8n-callable boundary, not a product decision to make it the long-term service.",
    ]
    status["warnings"].extend(gaps)
    if gaps and status["ok"]:
        status["success_class"] = "degraded_success"
    metadata["known_gaps"] = gaps
    metadata["n8n_callable"] = True
    metadata["effective_page_range"] = selected_page_range

    write_contract_outputs(output_dir, response, metadata, status)
    restore_review_artifact_layer(output_dir, response, metadata, status, conversion_args)
    run_optional_formula_second_pass(output_dir, metadata, status, conversion_args)
    record_cn_accepted_baseline(output_dir, metadata, status, conversion_args)
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
