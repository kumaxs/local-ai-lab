# Docling Serve Quality Parity Adapter Boundary

This directory documents a minimal parity adapter/spec for using Docling Serve as
the model execution backend while preserving the quality-first behavior that was
previously proven in `services/docling-service`.

It is also the minimal n8n-callable command boundary for the next integration
phase. It is not a live n8n workflow change and it does not modify
`docling-mcp`.

## Startup Requirement

The validated Serve startup policy is:

```bash
UVICORN_WORKERS=1 \
DOCLING_DEVICE=cpu \
DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true \
DOCLING_SERVE_ENG_KIND=local \
DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1 \
DOCLING_SERVE_ENG_LOC_SHARE_MODELS=true \
DOCLING_SERVE_ARTIFACTS_PATH=/Users/zeyuan/.cache/docling/models \
DOCLING_SERVE_LOAD_MODELS_AT_BOOT=true \
DOCLING_SERVE_OPTIONS_CACHE_SIZE=2 \
.runtime/docling-serve/.venv/bin/docling-serve run \
  --host 127.0.0.1 \
  --port 5001 \
  --artifacts-path /Users/zeyuan/.cache/docling/models
```

`DOCLING_DEVICE=cpu` keeps the standard PDF pipeline off the currently failing
Apple MPS float64 path. Formula enrichment still uses Granite MLX through the
request-level `code_formula_custom_config`.

## n8n-Callable Boundary

`quality_parity_adapter.py` is intentionally small and stdlib-only. n8n can call
it later through an approved command/worker boundary while Docling Serve remains
the only model execution backend.

The command shape is:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /absolute/path/to/input.pdf \
  --output-root /absolute/path/to/output-root \
  --job-id <n8n-job-id-or-uuid>
```

Optional controls:

```bash
--page-start 1 --page-end 3
--ocr-fallback-policy gxx
--formula-policy granite_mlx
--timeout-seconds 1200
--image-export-mode embedded
```

Defaults are quality-first:

- `/Gxx` text-layer detection is enabled with OCR fallback policy `gxx`;
- Granite MLX formula enrichment is requested with `formula-policy=granite_mlx`;
- accurate table structure options are requested;
- embedded image mode is used so `document.html` is self-contained where Serve
  provides images.

The adapter writes one job directory under `--output-root`:

```text
<output-root>/<job-id>/
  document.md
  document.html
  document.json
  metadata.json
  status.json
  tables/table_N.json
```

n8n should read:

- `status.json.ok`
- `status.json.success_class`
- `status.json.warnings`
- `status.json.quality_signals`
- `metadata.json.ocr_fallback_used`
- `metadata.json.text_quality_gxx_count`
- `metadata.json.text_quality_gxx_density`
- `metadata.json.formula_placeholder_count`
- `metadata.json.table_count`
- `metadata.json.generated_outputs`
- `metadata.json.output_dir`

Success classes:

- `success`: Serve succeeded and no adapter quality caveat was recorded.
- `degraded_success`: Serve succeeded but known parity gaps or warnings remain.
- `failure`: Serve conversion failed.

If Serve is not reachable, the command exits non-zero and prints JSON containing
the validated local start command.

The adapter retries transient Serve `503`/`504` responses. This is useful during
full-document review runs because the single local engine can briefly be
unavailable while expensive OCR/formula work is draining.

## Preserved Quality Policy

The adapter preserves:

- `/Gxx` detection and density calculation;
- OCR fallback through `force_ocr=true` when the `/Gxx` policy fails;
- explicit Granite MLX formula custom config;
- accurate table mode request options;
- embedded/referenced image export options;
- contract-equivalent output mapping:
  `document.md`, `document.html`, `document.json`, `metadata.json`, `status.json`;
- best-effort `tables/table_N.json` extraction from Serve JSON table nodes;
- warnings for gaps that official Serve output does not reproduce by itself.

Formula sample:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-transformers-gnn.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --job-id transformers_gnn_p2_formula \
  --page-start 2 \
  --page-end 2
```

For CN bad text-layer detection:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/CN.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --job-id CN_p1 \
  --page-start 1 \
  --page-end 1
```

For a table page:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/table-heavy-ai-table-transformer.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --job-id table_transformer_p1 \
  --page-start 1 \
  --page-end 1
```

## Timeout Guidance

Use at least 1200 seconds for formula-heavy full documents. Bounded page ranges
can use lower timeouts, but n8n should avoid treating a timeout as a quality
failure; it is an operational failure and should be retried or escalated.

## Full Directory Review Helper

`batch_full_dir_review.py` is a manual review helper. It is not a production n8n
integration. It calls `quality_parity_adapter.py` once per PDF, continues after
failures, and writes `run_summary.json` plus `run_summary.md`.

Example:

```bash
python3 docs/integrations/docling-serve-quality-parity/batch_full_dir_review.py \
  --input-dir /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs \
  --output-root /Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-serve-full-dir-review-2026-06-01 \
  --serve-url http://127.0.0.1:5001 \
  --adapter /Users/zeyuan/Projects/local-ai-lab/docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --timeout-seconds 1800
```

Use an ignored output root. The preferred `services/docling-service/reports/samples/`
path is not currently ignored for new generated files.

## Known Gaps

Docling Serve can execute the core models, but it does not by itself reproduce
all prior project quality behavior:

- no built-in `/Gxx` quality policy;
- no automatic Chinese OCR fallback decision;
- no project metadata/status contract;
- no formula/table/page source crop traceability layer;
- no standalone `assets/` and rich table artifacts unless post-processed.

The next n8n phase should treat this as the request/post-processing spec rather
than use a naked default `/v1/convert/source` request.
