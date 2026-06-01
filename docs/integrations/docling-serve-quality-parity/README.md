# Docling Serve Quality Parity Adapter

This directory documents a minimal parity adapter/spec for using Docling Serve as
the model execution backend while preserving the quality-first behavior that was
previously proven in `services/docling-service`.

It is not a live n8n integration and it does not modify `docling-mcp`.

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

## Probe Script

`quality_parity_adapter.py` demonstrates:

- `/Gxx` detection and density calculation;
- optional OCR fallback through `force_ocr=true`;
- explicit Granite MLX formula custom config;
- accurate table mode request options;
- embedded/referenced image export options;
- contract-equivalent output mapping:
  `document.md`, `document.html`, `document.json`, `metadata.json`, `status.json`;
- best-effort `tables/table_N.json` extraction from Serve JSON table nodes;
- warnings for gaps that official Serve output does not reproduce by itself.

Example:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-transformers-gnn.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --sample-name transformers_gnn_p2_formula \
  --page-start 2 \
  --page-end 2 \
  --enable-formula-mlx
```

For CN bad text-layer detection:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/CN.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --sample-name CN_p1 \
  --page-start 1 \
  --page-end 1 \
  --force-ocr-on-gxx
```

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
