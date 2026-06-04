# Docling Server Quality Parity Adapter Boundary

This directory documents a minimal parity adapter/spec for using Docling Server
(`docling-serve`) as the model execution backend while preserving the
quality-first behavior that was previously proven in Docling V1
(`services/docling-service`).

Project naming:

- Docling V1: the earlier `services/docling-service` quality-first route.
- Docling Server: the official `docling-serve` HTTP API backend.
- Route A: the current Docling Server quality-parity adapter.
- Route B: the `VlmPipeline` evaluation route only.

It is also the minimal n8n-callable command boundary for the next integration
phase. It is not a live n8n workflow change and it does not modify
`docling-mcp`.

Route A is acceptable only after Docling V1 parity is preserved. Do not use a
naked Docling Server API response as the user-facing contract.

## Startup Requirement

The validated Serve startup policy is:

```bash
UVICORN_WORKERS=1 \
DOCLING_DEVICE=cpu \
DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true \
DOCLING_SERVE_ALLOW_CUSTOM_OCR_CONFIG=true \
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

For CN OCR parity, the Docling Server virtual environment must include
`ocrmac`. Install it only into the project-local Serve venv, not globally:

```bash
.runtime/docling-serve/.venv/bin/python -m pip install ocrmac
```

## n8n-Callable Boundary

`quality_parity_adapter.py` is intentionally small and stdlib-only. n8n can call
it later through an approved command/worker boundary while Docling Server remains
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
--formula-second-pass-policy off
--timeout-seconds 1200
--image-export-mode embedded
--cn-ocr-parity
--cn-ocr-request-shape preset
--cn-ocr-chunk-size 1
```

Defaults are quality-first and aligned with Docling V1 where the behavior is
portable:

- `/Gxx` text-layer detection is enabled with OCR fallback policy `gxx`,
  defaulting to the Docling V1 trigger of count >= 10 and density >= 0.002;
- Granite MLX formula enrichment is requested with `formula-policy=granite_mlx`;
- accurate table structure options are requested;
- embedded image mode is used so `document.html` is self-contained where Serve
  provides images.

For Chinese bad text-layer parity, use `--cn-ocr-parity`. This keeps the normal
first pass unchanged, then on `/Gxx` failure requests the old known-good OCRMac
full-page OCR behavior through Serve:

```text
force_ocr=true
ocr_preset=ocrmac
ocr_lang=["zh-Hans", "zh-Hant", "en-US"]
```

If that full-document fallback receives a transient Serve `503`/`504`, the
adapter retries all pages through `page_range` chunks using the same OCRMac
Chinese settings, then merges the chunk outputs into the contract files. A
failed required OCR fallback is reported as `failure` or `degraded_failure`, not
as `degraded_success`.

If the deployed Serve requires explicit custom OCR configuration, start it with
`DOCLING_SERVE_ALLOW_CUSTOM_OCR_CONFIG=true` and run with:

```bash
--cn-ocr-parity --cn-ocr-request-shape custom
```

The adapter writes one job directory under `--output-root`:

```text
<output-root>/<job-id>/
  document.md
  document.html
  document.json
  metadata.json
  status.json
  tables/table_N.json
  review_index.html
  pages/page_N.png
  tables/table_N.html
  tables/table_N.csv
  tables/table_N.png
  formulas/formula_N_context.png
  pictures/picture_N.png
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
- best-effort `tables/table_N.json`, `tables/table_N.html`, and
  `tables/table_N.csv` extraction from Serve JSON table nodes;
- adapter-owned review artifacts: rendered page images, table crops, formula
  source/context crops, picture crops, and `review_index.html`;
- warnings for missing/incomplete formulas and suspicious formula text such as
  likely column contamination.
- explicit unresolved-gap warnings for footnotes, PDF links, inline formula HTML
  rendering, and math symbol rendering.

The review artifact layer is intentionally post-processing owned by this
adapter. Docling Server remains the execution backend for Route A.

## Optional Formula Second Pass

`quality_parity_adapter.py` can optionally run `formula_only_second_pass.py`
after the Route A adapter output and review artifacts are written. This is off
by default and remains evidence-first:

- Route A remains the document backbone.
- Route B is used only as a formula candidate source.
- `route-a-full` or any other fallback source is used only when explicitly
  passed as a guarded fallback source and only for allowlisted equation numbers.
- `review` mode writes sidecar evidence without replacing contract files.
- `apply` mode writes the same sidecar evidence, then patches
  `document.md`, `document.json`, and the affected formula blocks in
  `document.html` with rendered display math, traceable raw TeX, and review
  links. The final HTML display text is taken from the patched markdown body so
  restored equation numbers are preserved.

CN reviewed command shape:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/CN.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --job-id CN \
  --cn-ocr-parity \
  --formula-second-pass-policy apply \
  --formula-second-pass-route-b-dir /path/to/route-b/CN \
  --formula-second-pass-guarded-fallback-dir route-a-full=/path/to/route-a-full/CN \
  --formula-second-pass-guarded-fallback-eq 5 \
  --formula-second-pass-guarded-fallback-eq 7 \
  --formula-second-pass-guarded-fallback-eq 8
```

When enabled, the adapter records `metadata.json:formula_second_pass`,
`metadata.json:formula_second_pass_applied`,
`metadata.json:formula_second_pass_html_gate`, and
`status.json:quality_signals.formula_second_pass`. If `apply` reports
replacements but the final decoded `document.html` does not contain each patched
formula text, its MathJax display wrapper, and a traceable formula marker, the
adapter marks the result as a `degraded_failure`. The default sidecar output is:

```text
<output-root>/<job-id>/formula_second_pass/
  document.md
  document.json
  second_pass_summary.json
  review_index.html
```

See `docling_v1_parity_checklist.md` before making further parser improvements.
For the current `CN.pdf` formula `(3)` and `(5)` investigation, see
`cn_formula_quality_diagnostics.md`.

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
  --cn-ocr-parity
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

## Route B: VLM Pipeline Evaluation Helper

`vlm_full_dir_review.py` is a separate evaluation-only route. It explicitly uses
Docling `VlmPipeline` via `pipeline_cls=VlmPipeline`, writes per-PDF review
outputs, and records every input PDF in `run_summary.json` and
`run_summary.md`. It does not replace the Route A Server adapter.

Manual review found that Route B can produce cleaner formulas on `CN.pdf`
section 2.3, but it fatally drops right-column text on pages 3 and 4, wrongly
concatenates left-column text, does not meaningfully improve footnotes, emits
inline formula text without proper HTML rendering, and renders most pictures as
black blocks. Route B must remain evaluation-only and must not become the
default route without explicit approval and a separate design.

The helper prefers the local Granite Docling MLX cache:

```text
/Users/zeyuan/.cache/docling/models/ibm-granite--granite-docling-258M-mlx
```

If that cache is missing, the helper records a failure instead of downloading a
large model. Each PDF runs in a worker subprocess, so a failed or timed-out
document does not stop the batch.

Example:

```bash
.runtime/docling-serve/.venv/bin/python \
  docs/integrations/docling-serve-quality-parity/vlm_full_dir_review.py \
  --input-dir /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs \
  --output-root /Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-vlm-full-dir-review-2026-06-01 \
  --timeout-seconds 900 \
  --document-timeout 780
```

Per-PDF outputs, when conversion succeeds:

```text
<output-root>/<job-id>/
  document.md
  document.html
  document.json
  metadata.json
  status.json
  review_index.html
  pages/page_N.png
  tables/table_N.json
  tables/table_N.html
  tables/table_N.csv
```

Summary rows include the input filename, job id, output directory, model,
processed page count, success class, runtime, warnings/failure reason, output
presence flags, and observed table/formula/image indicators.

## Known Gaps

Docling Server can execute the core models, but the project still owns quality
policy and review packaging around it:

- no built-in `/Gxx` quality policy;
- no automatic Chinese OCR fallback decision;
- no project metadata/status contract;
- no formula/table/page source crop traceability layer unless post-processed;
- no standalone review assets and rich table artifacts unless post-processed.

The next n8n phase should treat this as the request/post-processing spec rather
than use a naked default `/v1/convert/source` request.
