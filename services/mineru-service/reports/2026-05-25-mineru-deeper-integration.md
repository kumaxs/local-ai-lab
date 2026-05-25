# MinerU Local VLM+MLX Deeper Integration

## Summary

Implemented a first complete multi-page MinerU review-output wrapper under `services/mineru-service/`. The wrapper keeps MinerU as a same-level local parser candidate beside Docling and writes the Local AI Lab parser contract:

- `document.html`
- `document.md`
- `document.json`
- `metadata.json`
- `status.json`
- `assets/`
- official-shape MinerU review artifacts under `official/<file_stem>/vlm/`

This is not a Docling patch and does not replace Docling. It is the first usable MinerU multi-page review-output path for local manual inspection and later AI-reading workflow design.

## What Was Implemented

- Added `mineru_service.contract.convert_pdf_to_contract()`.
- Added CLI command:

```bash
PYTHONPATH=services/mineru-service /tmp/mineru-service-venv/bin/python -m mineru_service.cli convert \
  --pdf /path/to/input.pdf \
  --output-dir /path/to/output-dir \
  --pages 1,3-4
```

- Added contract HTML rendering with valid local links for page images, formula crops/context crops, table crops, and image-region crops.
- Added local HTML reference checking.
- Added page-range parsing, metadata/status generation, output registration, and service-local tests.
- Added `services/mineru-service/reports/samples/.gitignore` so retained generated review outputs are available locally but not committed.

## Official MinerU Artifacts Observed and Wrapped

The integration preserves the official MinerU artifact shape:

```text
official/<file_stem>/vlm/<file_stem>.md
official/<file_stem>/vlm/<file_stem>_content_list.json
official/<file_stem>/vlm/<file_stem>_content_list_v2.json
official/<file_stem>/vlm/<file_stem>_middle.json
official/<file_stem>/vlm/<file_stem>_model.json
official/<file_stem>/vlm/images/
```

Because this task explicitly forbids MinerU pipeline and hybrid backends, the wrapper does not call the full MinerU CLI pipeline. Instead, it uses local `MinerUClient.two_step_extract()` through the MLX backend and writes official-shape artifacts from the returned blocks. This keeps the implementation local, reversible, and inside the approved VLM+MLX path.

`document.md` is generated from the MinerU block output and preserves equation blocks and HTML table content. `document.json` uses the generated `content_list_v2` structure as its source. `assets/` contains page images and detected formula/table/image region crops.

## Local VLM+MLX Confirmation

Runtime path:

- backend: `local_vlm_mlx`
- pipeline backend: disabled
- hybrid backend: disabled
- EXO: disabled
- model: `mineru2.5-pro-2604-1.2b-mlx-bf16`
- source repo: `carlesonielfa/MinerU2.5-Pro-2604-1.2B-mlx-bf16`
- local path: `/Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16`
- 8-bit model: not downloaded; keep as future runtime optimization only

The existing service-local compatibility view is still used so the downloaded MLX export can be loaded without mutating the model cache.

## 1036 x 1036 Layout Protocol

The wrapper preserves the required MinerU2.5 protocol:

- `layout_image_size = (1036, 1036)` is passed to `MinerUClient`.
- Rendered page images are passed to `two_step_extract()`.
- MinerU's client handles layout preparation at 1036 x 1036.
- Content extraction operates on rendered page images and natural crop dimensions.
- The wrapper does not pass original full-resolution PDF page images directly to the layout pass.

This is recorded in `metadata.json` as `layout_image_size` and `content_pass_image_policy`.

## Output Structure

Curated review outputs were preserved under:

```text
services/mineru-service/reports/samples/mineru-integration-2026-05-25/
```

Validated output directories:

```text
CN-pages-1-3/
table-heavy-ai-table-transformer-page-1/
two-col-arxiv-ai-gat-pages-1-2/
```

Generated sample outputs are intentionally ignored by git and retained locally for manual review.

## Sample Validation

| Sample | Pages | Runtime | Peak footprint | Formulas | Tables | Image regions | Assets | Broken HTML refs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `CN.pdf` | 1, 3 | 94.449 s metadata / 96.91 s `time` | ~4.96 GB | 10 | 0 | 2 | 24 | 0 |
| `table-heavy-ai-table-transformer.pdf` | 1 | 26.732 s metadata / 29.26 s `time` | ~4.83 GB | 0 | 1 | 0 | 2 | 0 |
| `two-col-arxiv-ai-gat.pdf` | 1-2 | 47.741 s metadata / 50.35 s `time` | ~4.95 GB | 0 | 0 | 0 | 2 | 0 |

Tests:

```text
PYTHONPATH=services/mineru-service python3 -m unittest discover services/mineru-service/tests
8 tests passed.

PYTHONPATH=services/mineru-service /tmp/mineru-service-venv/bin/python -m unittest discover services/mineru-service/tests
8 tests passed.
```

## Comparison to Docling Fallback

### CN Formula Detection

MinerU detected 10 equation blocks on CN page 3 and generated source/context crops for each. This includes the previously important formula:

```text
\boldsymbol {e} _ {q _ {i} \rightarrow c _ {p}} =
\sum_ {h = 1} ^ {N} \boldsymbol {e} _ {h \rightarrow p},
\boldsymbol {Q} _ {i, h} = 1 \tag {7}
```

This is better than the Docling fallback baseline for CN formula-region detection. The rendered LaTeX-like syntax still needs manual review, but the formula is present and linked to visual evidence.

### Inline/Text-interleaved Formulas

MinerU emits some inline math in text-like blocks and separate display equations when it detects them. Inline/text-interleaved formulas are still not guaranteed to get independent crop assets because the model may not emit them as separate equation blocks. This remains a limitation for later full-document parser evaluation.

### Tables

The table-heavy sample produced an HTML table with row/column spans plus a table crop artifact. This is promising and more directly reviewable than a placeholder-only output. Table success should still be judged per paper because this wrapper only validates bounded pages.

### Images and Regions

CN page 3 produced two image-region crops, and all processed pages include full page images. The wrapper now links or displays these regions in `document.html`. Figure caption association and full production asset semantics are not implemented yet.

### HTML/Markdown Usability

`document.html` is human-reviewable for the bounded samples:

- extracted text is in page order;
- formula blocks show decoded output and nearby source/context links;
- table HTML renders directly and links to a crop;
- image regions display as thumbnails;
- page images are linked from page headers;
- broken local refs count is 0 for all validated samples.

`document.md` is usable as a MinerU-derived markdown review artifact, but it is still generated from blocks rather than the full MinerU CLI pipeline output.

## Model Management

The existing `mineru_service.model_registry` remains the recommended lightweight model registry direction:

- model id;
- source repo;
- local path;
- quantization;
- expected runtime;
- required packages;
- health-check files;
- disk size;
- cleanup policy.

Recommended next step is to evolve this into a shared local functional-model manifest across parser services. EXO should remain out of this path because the user has already validated poor compatibility for functional document models; direct `mlx-vlm` remains the practical runtime candidate.

## Known Limitations

- The wrapper writes official-shape artifacts from `MinerUClient` blocks, not from MinerU pipeline/hybrid CLI output.
- Full-document processing was not run because current runtime is roughly 30-50 seconds per page after model load, and the goal here was bounded multi-page contract output.
- Formula and table crops depend on MinerU bounding boxes being present and aligned to the rendered page image.
- Inline formulas embedded in text may not have separate crops.
- Figure/table caption linkage is not yet structured.
- `document.html` is intended for review, not final publication.
- No n8n integration was added.

## Go / No-go

Go for deeper MinerU integration as a same-level parser candidate.

MinerU local VLM+MLX now has a real contract-output wrapper and shows better promise than Docling on the CN formula-7 hard case. It should not replace Docling yet. The next engineering step is a fuller writer pass: whole-document batching, richer official artifact compatibility, better crop scaling controls, table/figure caption association, and a common parser contract interface shared with Docling.
