# Docling Serve n8n Parity Boundary

Date: 2026-06-01

## Summary

The docs-only Docling Serve parity probe has been promoted into a minimal
n8n-callable command boundary:

```text
docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py
```

Docling Serve remains the model execution backend. The adapter owns only the
quality-first policy and contract-output mapping needed to avoid regressing to a
naked `/v1/convert/source` call.

No live n8n workflow, `services/n8n-paper-pipeline` runtime code,
`local-ai-python-worker`, or `docling-mcp` internals were changed.

## n8n Call Shape

Recommended command shape for a future approved n8n command/worker boundary:

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

- OCR fallback policy: `gxx`
- formula policy: `granite_mlx`
- table mode: accurate table structure with cell matching
- image export: embedded

## Output Contract

The adapter writes:

```text
<output-root>/<job-id>/
  document.md
  document.html
  document.json
  metadata.json
  status.json
  tables/table_N.json
```

`tables/table_N.json` is written only when table nodes exist in Serve JSON. The
adapter does not fabricate table artifacts.

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

`success_class=degraded_success` means the conversion completed but known parity
gaps or warnings remain. n8n should not treat it as a clean quality success.

## Preserved Policy

The boundary preserves the previous quality-first behavior that can be preserved
without adding a large service:

- `/Gxx` regex detection using `/G[0-9A-Fa-f]{2}`;
- threshold-based OCR fallback through a second Serve request with
  `force_ocr=true`;
- explicit Granite MLX formula request through `code_formula_custom_config` and
  `engine_options.engine_type="mlx"`;
- accurate table structure request options;
- contract-equivalent output files;
- metadata/status quality signals and warnings.

## Serve Startup Dependency

The adapter expects Docling Serve to already be running with the validated local
configuration:

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

If Serve is unreachable, the adapter exits non-zero and prints JSON containing
the exact startup command.

## Validation

Static validation completed because Docling Serve was not running during this
task and the instruction was not to reinstall it:

- `python3 -m py_compile docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py`
- `python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py --help`
- unreachable-Serve JSON failure path against `CN.pdf`

The previous parity smoke at commit `7ec4fe1362a9b986c1861c3a281ffaf75563bf63`
validated the same request shape against live Serve:

- CN page 1 no fallback: `/Gxx=9406`, density `0.007294`
- CN page 1 forced OCR: `/Gxx=10`, density `0.00000797`
- formula sample: Granite MLX formula request worked, 4 formula nodes, 0
  placeholders
- table sample: 1 table node, `tables/table_1.json` written

## Remaining Deferred Gaps

The adapter still records these as warnings:

- Serve response does not provide prior custom formula/source crop links.
- Serve response does not write standalone rich `assets/` and `tables/` unless
  post-processed.
- Table JSON is written from table nodes, but table HTML/Markdown artifact
  reconstruction is not implemented here.
- This command is a minimal integration boundary, not a permanent product
  decision about the long-term service shape.

## Recommendation

The next step is a controlled n8n HTTP/command smoke that calls this boundary,
waits for completion, reads `status.json` and `metadata.json`, and routes
`success_class=degraded_success` separately from hard failures. Do not wire n8n
directly to naked Docling Serve conversion.
