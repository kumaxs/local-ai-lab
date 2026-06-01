# Docling Serve Quality Parity Adapter

Date: 2026-06-01

## Summary

Docling Serve 1.20.0 can be used as the central model execution backend, but a naked `/v1/convert/source` request is not quality-parity with the previous `services/docling-service` quality-first behavior. The parity path needs a small request/post-processing adapter that preserves the quality policy around bad Chinese text layers, OCR fallback, Granite MLX formula enrichment, table structure, image export, and metadata/status diagnostics.

The adapter/spec added under `docs/integrations/docling-serve-quality-parity/` demonstrates this shape without integrating n8n or modifying `docling-mcp`.

## Environment

- Docling Serve: 1.20.0
- Docling: 2.95.0
- docling-core: 2.77.0
- docling-ibm-models: 3.13.2
- Runtime: project-local `.runtime/docling-serve/.venv`
- Serve endpoint: `http://127.0.0.1:5001/v1/convert/source`
- Stable standard pipeline device: `DOCLING_DEVICE=cpu`
- Formula path: request-level Granite MLX `code_formula_custom_config`
- Existing Granite MLX cache: `/Users/zeyuan/.cache/docling/models/ibm-granite--granite-docling-258M-mlx`

Validated startup shape:

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

## What Serve Request Options Can Cover

Docling Serve OpenAPI exposes the core options needed for model execution parity:

- `do_ocr` and `force_ocr`
- `do_table_structure`
- `table_mode=accurate`
- `table_cell_matching=true`
- `do_formula_enrichment`
- `code_formula_custom_config`
- `engine_options.engine_type=mlx`
- `image_export_mode`
- `include_images`
- `images_scale`
- `page_range`

The adapter sends Granite MLX formula enrichment with `engine_options.engine_type="mlx"` and an official custom code/formula config pointing at the Granite Docling MLX override repo, rather than the disallowed direct `granite_docling` preset.

## What Requires Lightweight Post-Processing

The following prior project behavior is not supplied as a complete policy by Serve itself and should remain in a thin adapter layer:

- `/Gxx` bad text-layer detection with regex `/G[0-9A-Fa-f]{2}`;
- OCR fallback decision after measuring generated text quality;
- contract-equivalent files: `document.md`, `document.html`, `document.json`, `metadata.json`, `status.json`;
- metadata/status quality signals and warnings for n8n;
- table node counting and `tables/table_N.json` writing from returned JSON;
- HTML/local-reference checks;
- policy warnings when quality parity is partial.

## What Serve Output Alone Does Not Reproduce

Serve response content alone does not reproduce the previous custom review-artifact layer:

- formula source/context crop traceability near converted formulas;
- `Formula not decoded` replacement/link behavior;
- page/table/formula crop generation;
- rich `assets/` organization;
- table Markdown/HTML artifact reconstruction from table nodes;
- a durable product decision about whether this adapter becomes the long-term n8n boundary.

Those gaps are not model-execution blockers, but they matter for quality parity and manual review.

## Validation Commands

Formula/Granite MLX page:

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

Chinese OCR fallback page:

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

Table page:

```bash
python3 docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py \
  --serve-url http://127.0.0.1:5001 \
  --input-file /Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/table-heavy-ai-table-transformer.pdf \
  --output-root /tmp/docling-serve-quality-parity \
  --sample-name table_transformer_p1 \
  --page-start 1 \
  --page-end 1
```

## Results

| Sample | Page range | Wall time | OCR fallback | `/Gxx` count | `/Gxx` density | Formula placeholders | Formulas | Tables | Image refs | Notes |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `CN.pdf` no forced fallback | 1-1 | 2.0s | no | 9406 | 0.007294 | 0 | 0 | 0 | 0 | Confirms the bad text-layer detector is necessary. |
| `CN.pdf` parity fallback | 1-1 | 14.0s | yes | 10 | 0.00000797 | 0 | 0 | 0 | 0 | Serve `force_ocr=true` greatly improves page 1, but does not exactly reproduce prior `/Gxx=0` whole-document result. |
| `two-col-arxiv-ai-transformers-gnn.pdf` | 2-2 | 8.0s | no | 15 | 0.0000166 | 0 | 4 | 0 | 2 embedded | Granite MLX formula enrichment worked through custom config. |
| `table-heavy-ai-table-transformer.pdf` | 1-1 | 2.0s | no | 12 | 0.0000125 | 0 | 0 | 1 | 0 | Table node counted and `tables/table_1.json` written by adapter. |

Generated validation outputs were written only under `/tmp/docling-serve-quality-parity/` and should not be committed.

## Quality Parity Assessment

Status: pass with gaps.

Docling Serve can execute the core parsing/OCR/table/formula model work, including Granite MLX formula enrichment, through official request options. Quality parity still requires a thin adapter because Serve does not make the project-specific quality decisions or write the full review contract by itself.

The most important positive result is that the CN page-level bad text-layer detector reproduced the expected decision: the non-forced path had `/Gxx=9406`, while the forced OCR path dropped to `/Gxx=10`. The formula page also confirmed request-level Granite MLX usage, with four formula nodes and zero `Formula not decoded` placeholders on the bounded sample.

The main remaining risk is review traceability. Previous `services/docling-service` work added formula/table/page crop links so humans could verify difficult regions. Serve returns HTML/Markdown/JSON, but not the same source/context crop layer. n8n should not use a naked Serve response if parity with the previous quality-first parser is required.

## Deferred Items

Do not solve these in the Serve validation phase:

- live n8n workflow modification;
- permanent product decision about whether this adapter is the long-term wrapper;
- docling-mcp schema or transport changes;
- MPS/auto device retry;
- custom formula/table crop reconstruction.

## Recommendation

Proceed to n8n HTTP integration only through the parity adapter/spec shape, not through a naked Docling Serve request. The next phase should implement the minimal n8n-side or service-side boundary that:

1. sends the validated Serve request options;
2. applies `/Gxx` detection and OCR fallback;
3. writes contract outputs and metadata/status warnings;
4. preserves Granite MLX formula request config;
5. explicitly records the current traceability gaps until source/context crops are restored or deemed unnecessary.
