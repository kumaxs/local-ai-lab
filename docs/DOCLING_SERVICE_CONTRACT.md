# Docling Service Contract

## 1. Purpose

`docling-service` is a local foundational document parsing service for Local AI Lab. Its job is to convert one local document into structured artifacts that can be consumed by downstream workflows.

The current primary use case is paper PDF parsing, but the service is a document parsing service, not a paper-only service. It is not a submodule of `n8n-paper-pipeline` and does not own the current paper intake main path.

## 2. Current Phase

This document defines the v0 contract only.

- No implementation.
- No deployment.
- No Docker.
- No service start.
- No n8n workflow change.
- No `local-ai-python-worker` runtime change.
- No `n8n-paper-pipeline` main-path replacement.

## 3. Non-goals

`docling-service` v0 does not:

- Perform batch orchestration.
- Download remote URLs.
- Access external links.
- Perform close reading or final academic judgment.
- Replace the future AI reading workflow.
- Replace `n8n-paper-pipeline`.
- Copy original PDFs or original documents into artifact directories.

## 4. Caller Model

Direct external callers are out of scope for v0.

The expected caller is local automation. `n8n` may trigger the flow indirectly, and `local-ai-python-worker` may expose controlled jobs that call the service later. Batch orchestration belongs to `n8n` or upstream automation.

One request handles one local file.

## 5. Identity Contract

`job_uuid` is required for every request.

- `job_uuid` must be UUIDv4.
- `job_uuid` is the only authoritative identifier for the job.
- `display_name`, `original_name`, and `source_name` are optional, non-unique, human-readable names for search, display, and filename hints.
- Filename must not be used as identity.
- Upstream systems should use `job_uuid` across n8n automation and downstream status tracking.

Example:

```json
{
  "job_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "display_name": "attention-is-all-you-need.pdf",
  "input_file_path": "/absolute/local/path/to/file.pdf"
}
```

## 6. Input Contract

The v0 input contract accepts:

- One local file path.
- Absolute path preferred.
- PDF as the first supported target.
- Future supported document formats may follow Docling capabilities, but contract v0 validates PDF first.
- No remote URL input.
- No batch input.
- No arbitrary shell command.
- Optional `requested_outputs`.
- Optional `image_export_mode` with values `referenced`, `embedded`, or `placeholder`; default is `referenced`.
- Optional `timeout_seconds`; default is `300`; maximum is to be defined in the test plan.

## 7. Output Contract

Required outputs:

- `document.html`
- `document.md`
- `document.json`
- `metadata.json`
- `status.json`

Conditional outputs:

- `assets/`
- `links.json`
- `tables/`
- `text.txt`
- `doctags.txt`

HTML is a priority output for structure and machine-readable reading. Markdown is a priority output for archival, Obsidian-style review, and paper intake cards. JSON is a priority output for structured programmatic consumption and lossless Docling Document serialization.

`assets/` is required when referenced images are emitted. `tables/` is optional until sample validation proves stability. `links.json` is required when links are detected or preserved.

## 8. Output Directory Layout

```text
artifacts/docling-service/<job_uuid>/
  document.html
  document.md
  document.json
  metadata.json
  status.json
  links.json
  assets/
  tables/
  text.txt
  doctags.txt
```

`artifacts/` is runtime output and must not be committed.

This task does not create `artifacts/`. This task does not modify `.gitignore`. A later implementation must verify `.gitignore` before creating runtime artifacts.

## 9. Original File Policy

The original file is not copied into the artifact directory.

`metadata.json` must record:

- `original_file_path`
- `sha256`

`metadata.json` should also record:

- `file_size`
- `mtime`
- Detected MIME or document type if available

Artifacts are derived materials only. The original file remains the evidence source.

## 10. Image and Asset Policy

The default image mode is referenced assets.

- `referenced`: HTML and Markdown refer to files under `assets/`.
- `embedded`: images may be embedded when explicitly requested.
- `placeholder`: placeholders may be used for minimal output.

Referenced assets should live under `assets/`. This contract does not guarantee every PDF image can be extracted until sample validation confirms behavior.

## 11. Link Policy

Links may be preserved in HTML, Markdown, or JSON if supported.

Links should be recorded in `links.json` when detected. External URLs must not be fetched. External URLs must not trigger downloads.

Links are metadata, not an evidence replacement.

## 12. Timeout and Failure Policy

The default `timeout_seconds` is `300`.

Failure status values:

- `failed_timeout`: the conversion exceeded the configured timeout.
- `failed_invalid_input`: the input path is missing, non-local, unreadable, or invalid.
- `failed_unsupported_format`: the input format is not supported by contract v0.
- `failed_conversion`: Docling or the conversion layer failed to produce required outputs.
- `failed_internal`: the service failed due to an internal error.

`docling-service` v0 does not retry internally. Retry and batch policy belongs to `n8n` or upstream automation.

Failure must not delete the original file or existing pipeline outputs.

## 13. Status JSON Contract

`status.json` must include at least:

- `job_uuid`
- `status`: `success`, `failed_timeout`, `failed_invalid_input`, `failed_unsupported_format`, `failed_conversion`, or `failed_internal`
- `started_at`
- `finished_at`
- `duration_seconds`
- `input_file_path`
- `input_sha256`
- `output_dir`
- `outputs_written`
- `warnings`
- `error_code`
- `error_message`

## 14. Metadata JSON Contract

`metadata.json` must include at least:

- `job_uuid`
- `display_name`
- `original_name`
- `source_name`
- `input_file_path`
- `input_sha256`
- `file_size_bytes`
- `input_mtime`
- `detected_format`
- `page_count` if available
- `docling_version` if available
- `image_export_mode`
- `requested_outputs`
- `generated_outputs`
- `link_count` if available
- `table_count` if available
- `asset_count` if available

## 15. Integration Boundary

`docling-service` does not own `n8n-paper-pipeline`.

`n8n-paper-pipeline` may consume Docling artifacts for paper workflows. `docling-service` can serve other document workflows later.

`local-ai-python-worker` may become the controlled bridge for local automation. `n8n` handles batch orchestration. The AI reading workflow handles close reading and must trace back to the original file.

## 16. Security Boundary

The v0 boundary is:

- Local file only.
- No remote URL fetching.
- No arbitrary shell execution.
- No public exposure.
- No secrets in artifacts.
- No original file copying.
- Artifacts must remain ignored runtime outputs.

## 17. Validation Requirements

The follow-up test plan must validate at least:

- Text-heavy PDF.
- Paper with figures.
- Paper with tables.
- Paper with internal and external links.
- Scanned or OCR-needed PDF.
- Large PDF near timeout.
- Malformed PDF.
- Unsupported file.
- Duplicate `display_name` with different `job_uuid`.
- Same file submitted twice with different `job_uuid`.

## 18. Acceptance Criteria

This contract is accepted when:

- It does not imply implementation.
- It does not deploy Docling.
- It preserves `docling-service` as a standalone local document parsing service.
- It uses UUIDv4 `job_uuid` as identity.
- It keeps original files out of artifacts.
- It defines required HTML, Markdown, and JSON outputs.
- It defines timeout, failure, status, and metadata contracts.
- It keeps `n8n-paper-pipeline` and AI reading workflow boundaries clear.

## 19. Next Step

The next step is `docs/DOCLING_SERVICE_TEST_PLAN.md`.

It is not implementation, not deployment, not an n8n workflow change, and not an `n8n-paper-pipeline` main-path replacement.
