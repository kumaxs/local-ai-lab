# Real Paper Validation - docling-service

## Summary

Validated the real `--converter docling` path against five local paper PDFs from `/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs`.

Result: 5 tested, 5 passed, 0 failed. Each successful conversion produced non-empty `document.md`, `document.html`, `document.json`, `metadata.json`, and `status.json`. Optional `text.txt` and `doctags.txt` were also produced for every sample.

No original PDFs were copied into output directories or committed.

## Environment

- Repository: `/Users/zeyuan/Projects/local-ai-lab`
- Commit under test: `fa1f421891ee47edd2418ca9e4fb80f195987709`
- CLI path: `PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli`
- Converter: `--converter docling`
- Docling version: `2.95.0`
- Date: `2026-05-22`

## Model/cache note

Docling model cache was reused from `/Users/zeyuan/.cache/docling/models`.

The local cache includes the layout model repo downloaded from hf-mirror:

- `docling-project/docling-layout-heron`
- Local path: `/Users/zeyuan/.cache/docling/models/docling-project--docling-layout-heron`

The existing hfd mirror cache also includes:

- `ds4sd/docling-models`
- Local path: `/Users/zeyuan/.cache/docling/hf-mirror/ds4sd/docling-models`

No model cache files are committed in this repository.

## Sample selection

Samples were chosen to cover a small double-column AI paper, another double-column arXiv-style paper, a table-heavy paper, a layout-focused PDF, and a short/possibly atypical filename.

| Sample | PDF size | Reason |
| --- | ---: | --- |
| `two-col-arxiv-ai-transformers-gnn.pdf` | 581,111 bytes | Small double-column paper; representative manual review sample |
| `two-col-arxiv-ai-bert.pdf` | 775,166 bytes | Second arXiv-style AI paper |
| `table-heavy-ai-complex-tables-gtr.pdf` | 1,120,439 bytes | Table-heavy paper |
| `layout-doc-ai-layoutlm.pdf` | 1,078,240 bytes | Layout-focused document |
| `CN.pdf` | 1,518,145 bytes | Short filename and potentially atypical structure |

## Results table

| Sample | Exit | Status | Wall time | Required outputs | Optional outputs | Original copied | Warnings |
| --- | ---: | --- | ---: | --- | --- | --- | --- |
| `two-col-arxiv-ai-transformers-gnn` | 0 | `success` | 5.705s | yes | `text.txt`, `doctags.txt` | no | none |
| `two-col-arxiv-ai-bert` | 0 | `success` | 10.159s | yes | `text.txt`, `doctags.txt` | no | none |
| `table-heavy-ai-complex-tables-gtr` | 0 | `success` | 6.961s | yes | `text.txt`, `doctags.txt` | no | none |
| `layout-doc-ai-layoutlm` | 0 | `success` | 5.951s | yes | `text.txt`, `doctags.txt` | no | none |
| `CN` | 0 | `success` | 6.917s | yes | `text.txt`, `doctags.txt` | no | none |

## Output quality notes

- Markdown and HTML outputs were generated for all samples and were non-empty.
- `document.json` was generated for all samples and carries the Docling document structure.
- `metadata.json` recorded `docling_version: 2.95.0` for all samples.
- `status.json` recorded `status: success` for all samples.
- Link, table, and asset counts remain conservative in this minimal writer. The current writer does not claim reliable link/table extraction or asset export.
- The representative manual review sample is small enough to inspect quickly while still exercising a real multi-page paper path.

## Failures / warnings

No sample failed.

No status warnings were recorded in the generated `status.json` files. During conversion, Docling printed repeated formula parsing messages for at least one paper, but the CLI contract still returned `ok: true` and all required outputs were produced.

## Manual review sample paths

Review sample outputs were copied to:

`services/docling-service/reports/samples/two-col-arxiv-ai-transformers-gnn/`
`services/docling-service/reports/samples/two-col-arxiv-ai-bert/`
`services/docling-service/reports/samples/table-heavy-ai-complex-tables-gtr/`
`services/docling-service/reports/samples/layout-doc-ai-layoutlm/`
`services/docling-service/reports/samples/CN/`

Each sample directory includes derived files:

- `README.md`
- `document.md`
- `document.html`
- `document.json`
- `metadata.json`
- `status.json`
- `text.txt`
- `doctags.txt`

The original PDF was not copied.

## Recommendation

The real Docling CLI path is ready for manual output review. Review `document.md`, `document.html`, and `document.json` in the sample directory first. After review, decide whether the next iteration should improve output quality metrics and metadata extraction, or start a worker bridge while keeping the n8n integration separate.
