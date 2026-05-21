# Architecture

## Purpose

`n8n-paper-pipeline` is the business pipeline for Local AI Lab document and paper intake.

Its job is to receive files, classify source types, coordinate parsing, organize parser artifacts, and prepare downstream materials for Paper Intake Cards, OpenClaw deep reading, and Obsidian knowledge capture.

It should not grow into a standalone PDF reader. Direct PDF text extraction is now considered a legacy compatibility path.

## Runtime Relationship

The current observed runtime relationship is:

```text
n8n
  -> HTTP request
local-ai-python-worker
  -> project bind mount
/pipelines/n8n-paper-pipeline
  -> host project
/Users/zeyuan/Projects/n8n-paper-pipeline
```

Observed containers:

- `n8n`: running on host port `5678`.
- `local-ai-python-worker`: running on host port `8765`.

Observed non-Docker schedulers:

- No related LaunchAgents were found.
- No related cron jobs were found.
- No direct host process for the paper pipeline was found.

## Current Entry Point

The current project entry point is:

```bash
scripts/process_inbox.py
```

Expected arguments:

```bash
python3 scripts/process_inbox.py \
  --input-dir n8n_inbox \
  --output-dir n8n_outputs \
  --state n8n_state/processed_index.json
```

Responsibilities:

- Resolve project-relative input, output, and state paths.
- Scan files in the inbox.
- Compute SHA-256 for deduplication.
- Detect source type.
- Route PDFs through the legacy PDF extractor.
- Route HTML or unsupported inputs through source-type handling.
- Write per-file outputs and run summaries.
- Persist processed state.

## Current Script Roles

- `scripts/process_inbox.py`: current n8n-facing inbox processor.
- `scripts/intake_detect.py`: source-type detector and router.
- `scripts/pdf_extract.py`: legacy direct PDF text extractor.
- `scripts/batch_test_pdf_extract.py`: legacy batch smoke test harness.

## Legacy Boundary

The legacy PDF extraction path uses `pdfplumber` and `pypdf` to produce:

- raw text;
- page-grouped Markdown;
- metadata JSON;
- extraction quality flags.

This is useful for compatibility, smoke tests, and fallback behavior. It should not become the main semantic document parsing layer because it does not reliably preserve layout, tables, figures, equations, image regions, or reading order.

## Future Docling-Ready Boundary

The future path should separate intake orchestration from document parsing:

```text
process_inbox.py or successor
  -> source detection
  -> parser adapter
  -> Docling service or local Docling runner
  -> normalized artifact bundle
  -> Paper Intake Card
  -> OpenClaw-ready package
  -> Obsidian-ready note material
```

The future artifact root is:

```text
future/docling-ready/
```

This structure is intentionally not wired into the current runtime yet.

## Operational Guardrails

Until the runtime migration is explicit:

- Do not delete legacy scripts.
- Do not move `scripts/process_inbox.py`.
- Do not modify Docker Compose as part of documentation-only cleanup.
- Do not assume `n8n` directly mounts this project.
- Keep future Docling artifacts isolated from current `n8n_outputs`.
