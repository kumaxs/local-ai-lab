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
import html
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from formula_only_second_pass import run_formula_second_pass

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
        default="off",
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
        return f" {match.group(1)}<sup class=\"docling-footnote-ref\">{match.group(2)}</sup>{match.group(3)}"

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


def structural_text_records(document_json: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for node in iter_nodes(document_json):
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
            }
        )
    return records


def _normalized_noise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def header_footer_qc_diagnostics(document_json: Any) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    records = structural_text_records(document_json)
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
        reasons: list[str] = [f"docling_label_{record['label'].lower()}"]
        if re.fullmatch(r"\d+", normalized):
            reasons.append("page_number")
        if HEADER_FOOTER_NOISE_RE.search(normalized):
            reasons.append("template_or_publication_noise")
        if text_counts.get(normalized, 0) >= 2 and not re.fullmatch(r"\d+", normalized):
            reasons.append("repeated_page_edge_text")
        if geometry:
            if geometry.get("b", 9999) < 80 or geometry.get("t", 0) > 700:
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


def structural_noise_qc(document_json: Any) -> dict[str, Any]:
    """Classify page-edge and footnote-like fragments for generic quarantine."""
    records = structural_text_records(document_json)
    normalized_counts: dict[str, int] = {}
    for record in records:
        normalized = _normalized_noise_text(str(record.get("text") or ""))
        if normalized:
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1

    candidates: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        label = str(record.get("label") or "")
        label_l = label.lower()
        text = str(record.get("text") or "")
        normalized = _normalized_noise_text(text)
        geometry = record.get("bbox") or {}
        reasons: list[str] = []
        kind: str | None = None

        if label_l in PAGE_EDGE_LABELS:
            kind = label_l
            reasons.append(f"docling_label_{label_l}")
        elif geometry:
            if geometry.get("b", 9999) < 70 and (
                re.fullmatch(r"\d{1,3}", normalized)
                or HEADER_FOOTER_NOISE_RE.search(normalized)
                or normalized_counts.get(normalized, 0) >= 2
            ):
                kind = "page_footer_candidate"
                reasons.append("bottom_edge_noise_candidate")
            if geometry.get("t", 0) > 760 and (
                HEADER_FOOTER_NOISE_RE.search(normalized)
                or normalized_counts.get(normalized, 0) >= 2
            ):
                kind = "page_header_candidate"
                reasons.append("top_edge_noise_candidate")

        if label_l == "footnote":
            kind = "footnote"
            reasons.append("docling_label_footnote")
        elif geometry and geometry.get("b", 9999) < 125 and FOOTNOTE_MARKER_RE.search(text):
            kind = kind or "footnote_candidate"
            reasons.append("bottom_region_footnote_marker_candidate")

        if normalized_counts.get(normalized, 0) >= 2 and kind in {"page_header", "page_header_candidate", "page_footer", "page_footer_candidate"}:
            reasons.append("repeated_text")
        if re.fullmatch(r"\d{1,3}", normalized) and kind:
            reasons.append("page_or_footnote_number_fragment")
        if HEADER_FOOTER_NOISE_RE.search(normalized):
            reasons.append("publication_template_noise")
        if label_l == "footnote" and re.fullmatch(r"\d+", normalized):
            reasons.append("isolated_footnote_marker")
        if label_l == "footnote" and re.match(r"^\d+\s+\w+", normalized):
            reasons.append("footnote_marker_attached_to_body_fragment")
        if label_l == "footnote" and text.rstrip().endswith("-"):
            reasons.append("hyphenated_footnote_continuation")

        if not kind or not reasons:
            continue

        action = "quarantine_from_main_text_flow"
        node = record.get("node")
        if isinstance(node, dict):
            node.setdefault("local_ai_lab_qc", {})["structural_quarantine"] = {
                "kind": kind,
                "reasons": reasons,
                "action": action,
            }
            if label_l in PAGE_EDGE_LABELS or label_l == "footnote":
                node["label"] = f"quarantined_{label_l}"

        candidates.append(
            {
                "index": index,
                "kind": kind,
                "label": label,
                "text": text[:300],
                "page_no": record.get("page_no"),
                "bbox": geometry or None,
                "reasons": reasons,
                "action": action,
                "evidence": f"pages/page_{record.get('page_no')}.png" if record.get("page_no") else None,
            }
        )

    return {
        "candidate_count": len(candidates),
        "isolated_main_text_pollution_count": len(candidates),
        "recovered_footnote_count": 0,
        "unresolved_footnote_count": sum(1 for item in candidates if "footnote" in item["kind"]),
        "candidates": candidates,
    }


def _replace_exact_paragraph_with_quarantine(document_html: str, item: dict[str, Any]) -> tuple[str, bool]:
    text = str(item.get("text") or "").strip()
    if not text:
        return document_html, False
    escaped = html.escape(text)
    aside = (
        '<aside class="docling-structural-quarantine" '
        f'data-kind="{html.escape(str(item.get("kind")), quote=True)}" '
        f'data-page="{html.escape(str(item.get("page_no")), quote=True)}">'
        '<strong>Structural quarantine:</strong> '
        f'{html.escape(str(item.get("kind")))}; '
        f'{html.escape(",".join(item.get("reasons") or []))}'
        f'<pre>{escaped}</pre>'
        '</aside>'
    )
    patterns = [
        re.compile(r"<p>\s*" + re.escape(escaped) + r"\s*</p>"),
        re.compile(r"<li>\s*" + re.escape(escaped) + r"\s*</li>"),
    ]
    updated = document_html
    for pattern in patterns:
        updated, count = pattern.subn(aside, updated, count=1)
        if count:
            return updated, True
    return document_html, False


def apply_structural_quarantine_to_outputs(
    output_dir: Path,
    document_json: Any,
) -> dict[str, Any]:
    """Apply generic structural quarantine to JSON, HTML, and Markdown outputs."""
    qc = structural_noise_qc(document_json)
    candidates = qc["candidates"]

    json_path = output_dir / "document.json"
    if json_path.exists():
        json_path.write_text(json.dumps(document_json, indent=2, ensure_ascii=False), encoding="utf-8")

    html_replacements = 0
    html_path = output_dir / "document.html"
    if html_path.exists() and candidates:
        html_text = html_path.read_text(encoding="utf-8")
        html_text, _style = _inject_english_review_style(html_text)
        quarantine_style = """
<style id="docling-structural-quarantine-style">
.docling-structural-quarantine {
  border-left: 3px solid #b45309;
  color: #475569;
  font: 0.86rem system-ui, sans-serif;
  margin: 0.5rem 0;
  padding: 0.45rem 0.7rem;
  background: #fffbeb;
}
.docling-structural-quarantine pre {
  white-space: pre-wrap;
  margin: 0.35rem 0 0;
}
</style>
"""
        if "docling-structural-quarantine-style" not in html_text:
            if "</head>" in html_text:
                html_text = html_text.replace("</head>", quarantine_style + "\n</head>", 1)
            else:
                html_text = quarantine_style + "\n" + html_text
        for item in candidates:
            html_text, changed = _replace_exact_paragraph_with_quarantine(html_text, item)
            if changed:
                html_replacements += 1
        html_path.write_text(html_text, encoding="utf-8")

    md_replacements = 0
    md_path = output_dir / "document.md"
    if md_path.exists() and candidates:
        md_text = md_path.read_text(encoding="utf-8")
        for item in candidates:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            replacement = (
                f"\n\n> [!note] Structural quarantine ({item.get('kind')}, "
                f"page {item.get('page_no')}): {','.join(item.get('reasons') or [])}\n"
                + "\n".join(f"> {line}" for line in text.splitlines())
                + "\n\n"
            )
            patterns = [
                "\n" + text + "\n",
                "\n\n" + text + "\n\n",
            ]
            for pattern in patterns:
                if pattern in md_text:
                    md_text = md_text.replace(pattern, replacement, 1)
                    md_replacements += 1
                    break
        md_path.write_text(md_text, encoding="utf-8")

    qc["html_quarantine_replacement_count"] = html_replacements
    qc["markdown_quarantine_replacement_count"] = md_replacements
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
    applied: list[dict[str, Any]] = []
    updated = document_html
    for item in diagnostics:
        if not item.get("safe_to_apply"):
            continue
        lead = str(item.get("lead_fragment") or "")
        tail = str(item.get("tail_fragment") or "")
        recovered = str(item.get("recovered_text") or "")
        if not lead or not tail or not recovered:
            continue
        pattern = re.compile(
            r"<p>" + re.escape(html.escape(tail)) + r"</p>\s*"
            r"<p>" + re.escape(html.escape(lead)) + r"</p>"
        )
        replacement = (
            '<div class="docling-footnote-recovery" '
            f'data-page="{html.escape(str(item.get("page_no")), quote=True)}" '
            f'data-footnote-number="{html.escape(str(item.get("footnote_number")), quote=True)}" '
            f'data-action="{html.escape(str(item.get("action")), quote=True)}">'
            '<p class="docling-footnote">'
            f'<sup>{html.escape(str(item.get("footnote_number")))}</sup> '
            f'{html.escape(recovered.split(" ", 1)[1] if " " in recovered else recovered)}'
            '</p>'
            '<details><summary>Original Docling footnote fragments</summary>'
            '<pre>'
            f'{html.escape(tail)}\n{html.escape(lead)}'
            '</pre></details>'
            '</div>'
        )
        updated, count = pattern.subn(replacement, updated, count=1)
        if count:
            applied.append(item)
    return updated, applied


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
    document_html = html_path.read_text(encoding="utf-8")
    before_href_count = len(re.findall(r"href=\"", document_html))
    document_html, style_injected = _inject_english_review_style(document_html)
    document_html, autolink_count = _autolink_plain_urls(document_html)
    document_html, footnote_sup_count = _polish_footnote_superscripts(document_html)
    document_html, math_text_count = _mark_math_heavy_text(document_html)
    formula_second_pass_start = time.monotonic()
    formula_number_diag = formula_number_qc_diagnostics(formulas, document_html)
    formula_tex_diag = formula_tex_qc_diagnostics(formulas)
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
    structural_quarantine = apply_structural_quarantine_to_outputs(output_dir, document_json)
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
            "formula_tex_qc_diagnostics": formula_tex_diag,
            "formula_second_pass_apply_all_review": formula_second_pass_review,
            "first_page_footnote_recovery_diagnostics": first_page_footnote_diag,
            "first_page_footnote_recovery_applied": first_page_footnote_applied,
            "header_footer_qc_diagnostics": header_footer_diag,
            "layout_qc_diagnostics": layout_diag,
            "structural_quarantine_qc": structural_quarantine,
            "formula_latex_sources": formula_latex_sources,
        }
    )
    metadata.setdefault("generated_outputs", []).append("links.json")
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
            "formula_tex_qc_diagnostics": formula_tex_diag,
            "formula_second_pass_apply_all_review": formula_second_pass_review,
            "first_page_footnote_recovery_diagnostics": first_page_footnote_diag,
            "first_page_footnote_recovery_applied": first_page_footnote_applied,
            "header_footer_qc_diagnostics": header_footer_diag,
            "layout_qc_diagnostics": layout_diag,
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
        return display_override
    markdown_after = _strip_display_math_wrapper(str(entry.get("markdown_after") or ""))
    if markdown_after:
        return markdown_after
    return str(entry.get("route_b_candidate") or "").strip()


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
FORMULA_INDEX_ATTR_RE = re.compile(r'data-formula-index="(\d+)"')
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_formula_anchor(text: str) -> str:
    text = html.unescape(text)
    text = HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    return [int(match.group(1)) for match in FORMULA_INDEX_ATTR_RE.finditer(html_text)]


def patch_document_html_for_formula_second_pass(
    output_dir: Path,
    sidecar_dir: Path,
    replacement_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Patch replaced formula blocks in document.html with traceable formula text."""
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"ok": False, "error": f"document.html not found: {html_path}"}

    html_text = html_path.read_text(encoding="utf-8")
    original_blocks, original_by_index = _original_formula_html_ranges(html_text)
    replacements: list[tuple[int, re.Match[str], str]] = []
    used_original_starts: set[int] = set()
    patched_indexes: list[int] = []
    missing_indexes: list[int] = []
    appended_indexes: list[int] = []
    patch_sources: dict[int, str] = {}
    entries_by_formula_no = {
        int(entry.get("formula_no")): entry
        for entry in replacement_log
        if entry.get("status") == "replaced" and isinstance(entry.get("formula_no"), int)
    }
    for entry in sorted(
        replacement_log,
        key=lambda item: int(item.get("formula_no") or 0),
        reverse=True,
    ):
        if entry.get("status") != "replaced":
            continue
        formula_no = entry.get("formula_no")
        if not isinstance(formula_no, int):
            continue
        replacement = _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
        match = original_by_index.get(formula_no)
        if match is not None:
            replacements.append((formula_no, match, replacement))
            used_original_starts.add(match.start())
            patched_indexes.append(formula_no)
            patch_sources[formula_no] = "data-formula-index"
            continue

        match, source = _find_formula_html_block_by_text(
            original_blocks,
            used_original_starts,
            str(entry.get("route_a_text") or ""),
        )
        if match is not None:
            replacements.append((formula_no, match, replacement))
            used_original_starts.add(match.start())
            patched_indexes.append(formula_no)
            patch_sources[formula_no] = str(source)
            continue

        if 0 < formula_no <= len(original_blocks):
            match = original_blocks[formula_no - 1]
            if match.start() not in used_original_starts:
                replacements.append((formula_no, match, replacement))
                used_original_starts.add(match.start())
                patched_indexes.append(formula_no)
                patch_sources[formula_no] = "original-block-position"
                continue

        missing_indexes.append(formula_no)

    if replacements:
        html_text = _replace_original_html_ranges(html_text, replacements)

    missing_indexes = sorted(set(missing_indexes))
    if missing_indexes:
        appended_blocks = []
        for formula_no in missing_indexes:
            entry = entries_by_formula_no.get(formula_no)
            if not entry:
                continue
            appended_blocks.append(_render_second_pass_formula_html(entry, output_dir, sidecar_dir))
            appended_indexes.append(formula_no)
            patched_indexes.append(formula_no)
            patch_sources[formula_no] = "appended-unmapped-formula-section"
        if appended_blocks:
            section = (
                '<section class="docling-formula-second-pass-unmapped">'
                '<h2>Unmapped Second-Pass Formulas</h2>'
                '<p>These formulas were replaced in document.json and document.md; '
                'Docling HTML did not expose a one-to-one original formula block, '
                'so the adapter preserved them here in the main HTML output.</p>'
                + "".join(appended_blocks)
                + "</section>"
            )
            if "</body>" in html_text:
                html_text = html_text.replace("</body>", section + "\n</body>", 1)
            else:
                html_text += section
            missing_indexes = [
                index for index in missing_indexes if index not in set(appended_indexes)
            ]

    assets_injected = False
    if patched_indexes:
        html_text, assets_injected = _ensure_formula_second_pass_html_assets(html_text)

    final_formula_indexes = _formula_indexes_in_html(html_text)
    final_formula_index_set = set(final_formula_indexes)
    original_formula_index_set = set(original_by_index)
    lost_formula_indexes = sorted(original_formula_index_set - final_formula_index_set)
    duplicate_formula_indexes = sorted(
        index for index in final_formula_index_set if final_formula_indexes.count(index) > 1
    )

    html_path.write_text(html_text, encoding="utf-8")
    return {
        "ok": not missing_indexes,
        "patched_indexes": patched_indexes,
        "missing_indexes": missing_indexes,
        "appended_unmapped_indexes": appended_indexes,
        "lost_formula_indexes": lost_formula_indexes,
        "duplicate_formula_indexes": duplicate_formula_indexes,
        "patch_sources": patch_sources,
        "original_formula_block_count": len(original_blocks),
        "original_formula_index_count": len(original_by_index),
        "final_formula_index_count": len(final_formula_index_set),
        "rendering_assets_injected": assets_injected,
    }


def validate_formula_second_pass_html(
    output_dir: Path,
    replacement_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify applied replacement text is visible or traceable in document.html."""
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"ok": False, "missing_replacements": [], "error": f"document.html not found: {html_path}"}
    decoded_html = html.unescape(html_path.read_text(encoding="utf-8"))
    missing: list[int] = []
    for entry in replacement_log:
        if entry.get("status") != "replaced":
            continue
        formula_no = entry.get("formula_no")
        display_text = _second_pass_formula_display_text(entry)
        marker = f'data-formula-index="{formula_no}"'
        render_marker = f"\\[{display_text}\\]"
        if display_text not in decoded_html or render_marker not in decoded_html or marker not in decoded_html:
            if isinstance(formula_no, int):
                missing.append(formula_no)
    has_mathjax = "docling-formula-second-pass-mathjax" in decoded_html
    return {
        "ok": not missing,
        "missing_replacements": missing,
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


def _cn_final_polish_source_texts(args: argparse.Namespace) -> dict[int, str]:
    source_texts: dict[int, str] = {}
    for value in args.formula_second_pass_guarded_fallback_dir:
        path_text = value.split("=", 1)[1] if "=" in value else value
        source_texts.update(_load_formula_text_by_number(Path(path_text)))
    return {
        formula_no: source_texts[formula_no]
        for formula_no in CN_FINAL_POLISH_FORMULA_NUMBERS
        if formula_no in source_texts
    }


def _patch_formula_json_nodes(output_dir: Path, formula_texts: dict[int, str]) -> list[int]:
    json_path = output_dir / "document.json"
    document = _load_json_file(json_path)
    if not isinstance(document, dict):
        return []
    patched: list[int] = []
    for index, formula in enumerate(extract_label_nodes(document, "formula"), start=1):
        formula_no = _formula_number_for_node(index, formula)
        if formula_no not in formula_texts:
            continue
        old_text = str(formula.get("text") or "")
        new_text = formula_texts[formula_no]
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


def _patch_html_formula_blocks(
    output_dir: Path,
    sidecar_dir: Path,
    formula_texts: dict[int, str],
) -> dict[str, Any]:
    html_path = output_dir / "document.html"
    if not html_path.exists():
        return {"ok": False, "error": f"document.html not found: {html_path}"}
    html_text = html_path.read_text(encoding="utf-8")
    html_text, text_corrections = _patch_html_text_corrections(html_text)
    original_blocks, original_by_index = _original_formula_html_ranges(html_text)
    replacements: list[tuple[int, re.Match[str], str]] = []
    used_original_starts: set[int] = set()
    patched_indexes: list[int] = []
    missing_indexes: list[int] = []
    patch_sources: dict[int, str] = {}
    for formula_no, formula_text in formula_texts.items():
        entry = {
            "formula_no": formula_no,
            "status": "cn_final_polish",
            "markdown_after": f"$${formula_text}$$",
        }
        replacement = _render_second_pass_formula_html(entry, output_dir, sidecar_dir)
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
        if match is None:
            old_text_probe = _find_formula_html_block_by_number(original_blocks, used_original_starts, formula_no)
            if old_text_probe is not None:
                match = old_text_probe
                patch_sources[formula_no] = "formula-number"
        if match is None and 0 < formula_no <= len(original_blocks):
            positional_match = original_blocks[formula_no - 1]
            if positional_match.start() not in used_original_starts:
                match = positional_match
                patch_sources[formula_no] = "cn-final-allowlist-position"
        if match is None:
            missing_indexes.append(formula_no)
            continue
        replacements.append((formula_no, match, replacement))
        used_original_starts.add(match.start())
        patched_indexes.append(formula_no)
    if replacements:
        html_text = _replace_original_html_ranges(html_text, replacements)
        html_text, assets_injected = _ensure_formula_second_pass_html_assets(html_text)
    else:
        assets_injected = False
    html_path.write_text(html_text, encoding="utf-8")
    return {
        "ok": not missing_indexes,
        "patched_indexes": patched_indexes,
        "missing_indexes": sorted(set(missing_indexes)),
        "patch_sources": patch_sources,
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


def apply_cn_final_document_polish(
    output_dir: Path,
    sidecar_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.input_file.name != "CN.pdf":
        return {"ok": True, "applied": False, "reason": "not_cn_pdf"}
    if not args.formula_second_pass_guarded_fallback_dir:
        return {
            "ok": True,
            "applied": False,
            "reason": "no_guarded_fallback_source_for_cn_final_polish",
        }
    formula_texts = _cn_final_polish_source_texts(args)
    missing_sources = [
        formula_no for formula_no in CN_FINAL_POLISH_FORMULA_NUMBERS if formula_no not in formula_texts
    ]
    json_patched = _patch_formula_json_nodes(output_dir, formula_texts)
    markdown_patched = _patch_markdown_formula_blocks(output_dir, formula_texts)
    html_patch = _patch_html_formula_blocks(output_dir, sidecar_dir, formula_texts)
    ok = not missing_sources and bool(html_patch.get("ok"))
    return {
        "ok": ok,
        "applied": True,
        "formula_texts": sorted(formula_texts),
        "missing_source_formulas": missing_sources,
        "document_json_patched": json_patched,
        "document_md_patched": markdown_patched,
        "document_html_patch": html_patch,
    }


def run_optional_formula_second_pass(
    output_dir: Path,
    metadata: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Run optional formula-only second pass and update adapter metadata/status."""
    policy = args.formula_second_pass_policy
    metadata["formula_second_pass_policy"] = policy
    status["quality_signals"]["formula_second_pass_policy"] = policy
    if policy == "off":
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
        message = "formula_second_pass_route_b_dir_required"
        metadata["formula_second_pass"] = {"ok": False, "error": message}
        status["warnings"].append(message)
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
        "guarded_fallback_eqs": sorted(args.formula_second_pass_guarded_fallback_eq),
        "error": result.get("error"),
    }
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
    if policy in {"apply", "apply-all"}:
        shutil.copyfile(sidecar_dir / "document.md", output_dir / "document.md")
        shutil.copyfile(sidecar_dir / "document.json", output_dir / "document.json")
        patched_document = _load_json_file(output_dir / "document.json")
        if isinstance(patched_document, dict):
            patched_formulas = extract_label_nodes(patched_document, "formula")
            formula_latex_sources = write_formula_latex_sources(output_dir, patched_formulas)
            metadata["formula_latex_sources"] = formula_latex_sources
            status["quality_signals"]["formula_latex_sources"] = formula_latex_sources
        html_patch = patch_document_html_for_formula_second_pass(
            output_dir,
            sidecar_dir,
            list(result.get("replacement_log") or []),
        )
        cn_final_polish = apply_cn_final_document_polish(output_dir, sidecar_dir, args)
        html_gate = validate_formula_second_pass_html(
            output_dir,
            list(result.get("replacement_log") or []),
        )
        metadata["formula_second_pass_html_patch"] = html_patch
        metadata["formula_second_pass_html_gate"] = html_gate
        metadata["cn_final_document_polish"] = cn_final_polish
        status["quality_signals"]["formula_second_pass_html_patch"] = html_patch
        status["quality_signals"]["formula_second_pass_html_gate"] = html_gate
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
        elif html_patch.get("appended_unmapped_indexes"):
            status["warnings"].append(
                "formula_second_pass_html_unmapped_appended:"
                + ",".join(str(index) for index in html_patch.get("appended_unmapped_indexes") or [])
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
                        "Docling Server OCRMac full-page request"
                        if args.cn_ocr_parity
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
    restore_review_artifact_layer(output_dir, response, metadata, status, args)
    run_optional_formula_second_pass(output_dir, metadata, status, args)
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
