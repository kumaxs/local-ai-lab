# Docling Formula MLX Validation and Phase Closeout

Date: 2026-05-25

## Current Docling Phase Closeout

The current Docling phase is now a usable article-review baseline:

- Chinese text quality is fixed for the known CN sample through OCR fallback (`/Gxx=0`).
- Images are linked/displayed in `document.html`.
- Tables are acceptable for current review needs, with table crops as fallback.
- Formula review is no longer blocked by bare `Formula not decoded` text: unresolved formulas link to high-resolution context crops.
- Structured formula extraction was the remaining weak point, especially for CN formulas and inline/text-interleaved formulas.

This task validated Docling's Granite-Docling-CodeFormula MLX path and closed the current Docling formula phase. MinerU and other parser research were intentionally not started here.

## Model Selection Method

Docling 2.95.0 exposes supported code/formula presets through:

```python
CodeFormulaVlmOptions.from_preset("codeformulav2")
CodeFormulaVlmOptions.from_preset("granite_docling", engine_options=MlxVlmEngineOptions())
```

Verified local preset facts:

- `codeformulav2`: `docling-project/CodeFormulaV2`, Transformers path.
- `granite_docling`: `ibm-granite/granite-docling-258M`, with MLX engine override to `ibm-granite/granite-docling-258M-mlx`.

The service now prefers Granite MLX only when both are locally available:

- model cache: `/Users/zeyuan/.cache/docling/models/ibm-granite--granite-docling-258M-mlx`
- runtime: `mlx-vlm` / `mlx`

If Granite MLX is unavailable, the service preserves the existing CodeFormulaV2 fallback. The user-facing command remains:

```bash
--converter docling
```

## Models Present or Downloaded

Already present:

- `/Users/zeyuan/.cache/docling/models/docling-project--CodeFormulaV2`
- `/Users/zeyuan/.cache/docling/models/docling-project--docling-models`
- `/Users/zeyuan/.cache/docling/models/docling-project--TableFormerV2`

Downloaded during this task with `/Users/zeyuan/Local-AI-Lab/hfd.sh`:

- `/Users/zeyuan/.cache/docling/models/ibm-granite--granite-docling-258M-mlx`

Installed into the service-local venv, not global Python:

- `mlx-vlm`
- `mlx`

No model files, venv files, PDFs, or temporary outputs are committed.

## CodeFormulaV2 vs Granite MLX

Temporary comparison outputs:

```text
/tmp/docling-mlx-compare/
```

| Sample | Model | OCR | Wall sec | Formula placeholders | Formula crops | Context crops | Tables | Local broken refs | Max RSS |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| CN.pdf | Granite MLX | true | 72.544 | 0 | 24 | 24 | 6 | 0 | ~4.0 GB |
| table-heavy-ai-table-transformer.pdf | CodeFormulaV2 | false | 32.602 | 0 | 1 | 1 | 5 | 0 | ~4.9 GB |
| table-heavy-ai-table-transformer.pdf | Granite MLX | false | 14.084 | 0 | 1 | 1 | 5 | 0 | ~3.9 GB |
| two-col-arxiv-ai-gat.pdf | CodeFormulaV2 | false | 161.536 | 0 | 6 | 6 | 3 | 0 | ~4.3 GB |
| two-col-arxiv-ai-gat.pdf | Granite MLX | false | 18.110 | 0 | 6 | 6 | 3 | 0 | ~4.1 GB |

Earlier bounded CN validation with CodeFormulaV2 on OCR fallback was manually stopped after about 11 minutes. That was not quota exhaustion and not a program error, but it also was not a successful structured CN formula path. Granite MLX completed the equivalent CN OCR+formula path in about 72 seconds and produced 0 formula placeholders.

## Default Policy Decision

Default changed: Granite-Docling-CodeFormula MLX is now the preferred local Apple Silicon formula policy when present.

Fallback preserved:

- If Granite MLX model or runtime is missing, normal text-layer formula extraction falls back to CodeFormulaV2.
- If no formula model is usable, the existing high-resolution formula crop/context fallback remains.

## Final Service CLI Validation

Temporary output root:

```text
/tmp/docling-mlx-final/
```

Command shape remained:

```bash
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli --converter docling ...
```

| Sample | Runtime sec | OCR | /Gxx | Formula model | Placeholders | Crops | Context crops | Tables | Assets | Broken local refs |
| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CN.pdf | 76.168 | true | 0 | granite_docling_mlx | 0 | 24 | 24 | 6 | 77 | 0 |
| table-heavy-ai-table-transformer.pdf | 17.384 | false | 0 | granite_docling_mlx | 0 | 1 | 1 | 5 | 25 | 0 |
| two-col-arxiv-ai-gat.pdf | 21.490 | false | 0 | granite_docling_mlx | 0 | 6 | 6 | 3 | 31 | 0 |

Status warnings correctly record:

```text
formula_enrichment_enabled_granite_docling_mlx
```

## Inline and Block Formula Behavior

Block/display formulas:

- Granite MLX is clearly faster than CodeFormulaV2 on the tested English samples.
- Granite MLX also improved the CN OCR fallback path from review-only placeholders to structured formula output with 0 placeholders.

Inline/text-interleaved formulas:

- The tested samples still depend on Docling's ability to create formula regions.
- Inline formulas embedded inside normal paragraph text may remain ordinary text or may be imperfectly represented.
- This is still a Docling/model capability limitation, not an HTML/writer issue.

## Table, Image, and OCR Regression

No regression was observed in the bounded final validation:

- CN OCR fallback remains effective (`/Gxx=0`).
- HTML image/formula/table local refs were valid.
- Table counts and table crops were preserved.
- Formula crop/context fallback remains present even when structured formula output succeeds.

## Remaining Limitations

- Granite MLX requires a service-local optional runtime (`mlx-vlm`) and a local model cache.
- The service does not download the model implicitly during conversion.
- Inline formula detection is still limited by Docling's region detection.
- Docling still prints MathML parse warnings on some samples even when final placeholders are 0.
- CPU/MLX memory pressure was roughly 3.9-4.9 GB in this small comparison.

## Recommendation

Keep Granite MLX as the preferred local formula model for Apple Silicon. Preserve CodeFormulaV2 as fallback.

Next parser research should move outside this Docling phase:

- compare MinerU/Marker or a formula-specific VLM on CN and inline formulas;
- evaluate region-level formula second pass only for pages where Docling still leaves placeholders;
- keep Docling as the current first-pass article review baseline.
