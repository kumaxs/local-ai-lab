# Docling Serve Formula/MLX Validation

Date: 2026-05-27

## Summary

Docling Serve can expose formula enrichment through the official `/v1/convert/source` API, and it can run the local Granite-Docling-CodeFormula MLX model when custom code/formula configuration is explicitly enabled at the Serve process level.

This means Docling Serve can remain the single backend candidate for n8n, but the n8n request/preset policy needs to be explicit. A plain CPU Serve request does not decode formulas. A plain `code_formula_preset=granite_docling` request is rejected by default policy. Setting the Serve default preset to Granite is still not enough under `DOCLING_DEVICE=cpu`, because `auto_inline` chooses the non-MLX base repo and looks for `ibm-granite--granite-docling-258M`, which is not cached locally. The working official path is:

- start Serve with `DOCLING_DEVICE=cpu` for standard-pipeline stability;
- start Serve with `DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true`;
- send `do_formula_enrichment=true`;
- send `code_formula_custom_config` based on `CodeFormulaVlmOptions.from_preset("granite_docling")`;
- set `engine_options.engine_type="mlx"` in that custom config.

No Docling Serve package internals, `docling-mcp`, n8n, Docker, or global Python were modified.

## Versions

Validated local runtime:

| Component | Version |
| --- | --- |
| `docling-serve` | 1.20.0 |
| `docling` | 2.95.0 |
| `docling-core` | 2.77.0 |
| `docling-jobkit` | 1.20.0 |
| `mlx-vlm` | 0.5.0 |
| `mlx` | 0.31.2 |
| Python | 3.13.13 |

Model cache:

```text
/Users/zeyuan/.cache/docling/models
```

Relevant local model:

```text
/Users/zeyuan/.cache/docling/models/ibm-granite--granite-docling-258M-mlx
```

Observed size:

```text
611M
```

## API And Config Findings

OpenAPI/Pydantic service options expose:

- `do_formula_enrichment`
- `do_code_enrichment`
- `code_formula_preset`
- `code_formula_custom_config`
- `page_range`
- normal conversion controls such as `to_formats`, `image_export_mode`, `do_ocr`, `do_table_structure`, and `include_images`

Installed Docling presets include:

- `codeformulav2`
- `granite_docling`

The `granite_docling` preset includes an MLX override:

```text
repo_id = ibm-granite/granite-docling-258M-mlx
engine_type = mlx
```

Serve policy behavior:

- Direct request with `code_formula_preset="granite_docling"` failed:
  `Code/formula preset 'granite_docling' is not allowed. Allowed presets: default`
- Starting Serve with `DOCLING_SERVE_DEFAULT_CODE_FORMULA_PRESET=granite_docling` and sending `code_formula_preset="default"` failed under `DOCLING_DEVICE=cpu`:
  `Model 'ibm-granite/granite-docling-258M' not found in artifacts_path`
- Starting Serve with `DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true` and sending an explicit custom config with `engine_options.engine_type="mlx"` succeeded.

## Startup Commands Tried

Stable CPU Serve control:

```bash
UVICORN_WORKERS=1 \
DOCLING_DEVICE=cpu \
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

Formula MLX-capable Serve:

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

Default Apple Silicon/MPS test:

```bash
UVICORN_WORKERS=1 \
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

The MPS test still failed with:

```text
Page 2: Cannot convert a MPS Tensor to float64 dtype as the MPS framework doesn't support float64. Please use float32 instead.
```

## Endpoint And Options

Endpoint:

```text
POST http://127.0.0.1:5001/v1/convert/source
```

Source form:

```json
{
  "kind": "file",
  "filename": "two-col-arxiv-ai-transformers-gnn.pdf",
  "base64_string": "<base64 PDF bytes>"
}
```

Common bounded options:

```json
{
  "from_formats": ["pdf"],
  "to_formats": ["md", "json", "html"],
  "image_export_mode": "referenced",
  "do_ocr": true,
  "force_ocr": false,
  "ocr_preset": "auto",
  "do_table_structure": true,
  "table_mode": "accurate",
  "include_images": true,
  "images_scale": 2.0,
  "page_range": [2, 2]
}
```

Working formula/MLX additions:

```json
{
  "do_formula_enrichment": true,
  "do_code_enrichment": false,
  "code_formula_custom_config": {
    "engine_options": {
      "engine_type": "mlx"
    },
    "model_spec": {
      "name": "Granite-Docling-258M",
      "default_repo_id": "ibm-granite/granite-docling-258M",
      "engine_overrides": {
        "mlx": {
          "repo_id": "ibm-granite/granite-docling-258M-mlx"
        }
      }
    },
    "scale": 2.0,
    "extract_code": true,
    "extract_formulas": true
  }
}
```

The real request was generated from `CodeFormulaVlmOptions.from_preset("granite_docling").model_dump(mode="json")` and then changed only `engine_options.engine_type` to `mlx`.

## Smoke Results

Sample:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-transformers-gnn.pdf
```

Page range:

```text
[2, 2]
```

This page was selected after a bounded page scan showed Docling Serve emitted 4 formula items on page 2.

| Run | Device | Formula config | Result | Wall time | Serve processing time | Formula items | `Formula not decoded` | LaTeX-like count |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Control | CPU | formula off | success | 2.03s | 0.46s | 4 | 4 | 0 |
| Granite MLX | CPU standard pipeline + MLX formula engine | custom config, `engine_type=mlx` | success | 6.03s | 4.70s | 4 | 0 | 60 |
| MPS/auto | default Apple Silicon auto device | formula off | failure | 2.02s | 0.42s | n/a | n/a | n/a |

Formula output examples from the Granite MLX run included LaTeX-like text such as:

```text
h _ { i } ^ { \\ell + 1 } = A t t e n t i o n ...
= \\sum _ { j \\in \\mathcal { S } } ...
w _ { i j } = s o f t \\max ...
= \\frac { \\exp ... } { \\sum ... }
```

The serializer still emitted `Could not parse formula with MathML` warnings in the Serve logs, but the Markdown/JSON output no longer contained `Formula not decoded` for the tested page.

## CN Control Observation

A bounded CPU control on `CN.pdf`, page 1, without OCR fallback policy logic produced `/Gxx=9399`. This is expected because this raw Serve request did not include the custom `services/docling-service` quality policy that detects bad `/Gxx` layers and forces OCR fallback. It is not evidence that Serve cannot OCR CN; it means n8n integration must explicitly carry forward the quality-policy request behavior or preflight logic.

## Comparison With Previous `services/docling-service` Granite MLX Baseline

Previous service-local baseline:

- used Granite-Docling-CodeFormula MLX when present;
- reduced formula placeholders to 0 on tested English formula pages;
- produced review crops/context links around formulas;
- still had known limitations around missed CN formula regions and inline/text-interleaved formulas.

Official Docling Serve now matches the important structured-formula part on a bounded English formula page when configured with explicit MLX custom code/formula options:

- formula placeholders dropped from 4 to 0;
- formula items became LaTeX-like text;
- runtime remained bounded on a single formula page.

What Serve does not provide by itself:

- the custom review HTML/crop traceability layer from `services/docling-service`;
- `/Gxx` quality preflight and automatic OCR fallback policy;
- formula source/context links beside converted formulas;
- per-paper metadata/status quality contract currently produced by the custom wrapper.

## Recommendation

Proceed toward n8n HTTP integration with Docling Serve as the single backend, but do not treat the default Serve request as equivalent to the previous quality-first parser.

Recommended next integration policy:

1. Start Serve conservatively with `DOCLING_DEVICE=cpu` until the MPS float64 issue is resolved upstream or avoided through official config.
2. Enable `DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true`.
3. For formula-relevant documents, send official `code_formula_custom_config` with Granite Docling and `engine_type=mlx`.
4. Carry forward quality-policy logic at the n8n/request layer:
   `/Gxx` detection, OCR fallback decision, table/image options, and warnings.
5. Keep `services/docling-service` as the reference implementation for review artifact behavior until n8n reproduces the needed status/metadata and human-review outputs from Serve responses.

Docling Serve is sufficient as the central model execution backend for the next n8n HTTP smoke. Formula-enhanced parsing does not require a separate model runtime, but it does require explicit official Serve configuration and request options.
