# Docling Service Test Plan

## Purpose

This document defines the v0 test plan for `docling-service`, a local foundational document parsing service for Local AI Lab.

The test plan validates the accepted `docs/DOCLING_SERVICE_CONTRACT.md` and the passed `docs/DOCLING_SERVICE_DESIGN.md` before any implementation, deployment, or integration work begins.

## Current Phase

Current phase: test plan only.

This document does not implement `docling-service`, does not deploy Docling, does not run Docling, does not create service directories, does not create `artifacts/`, and does not create sample files.

No changes are made to:

- `n8n-paper-pipeline`
- `local-ai-python-worker`
- n8n workflows
- Docker or compose configuration
- runtime service state

## Test Scope

The v0 validation scope is single-file local document parsing against the contract.

The tests will validate:

- One request maps to one local input file.
- `job_uuid` is the only authoritative job identity.
- PDF is the first supported target.
- Required outputs are produced on success.
- Required status and metadata fields are present.
- Failure modes are explicit and stable.
- External links are preserved as metadata only and are never fetched.
- Original files are never copied into artifact directories.
- Runtime artifacts are written only under `artifacts/docling-service/<job_uuid>/`.

## Non-goals

The v0 test plan does not validate:

- Batch orchestration.
- n8n workflow changes.
- `n8n-paper-pipeline` main-path replacement.
- `local-ai-python-worker` runtime changes.
- AI close reading or academic judgment.
- Remote URL download or ingestion.
- Public service exposure.
- Long-running queue behavior.
- Production deployment.
- OCR at scale.

## Test Environment Assumptions

Future test execution assumes:

- Tests run in the engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`.
- `docling-service` is local only and not publicly exposed.
- Inputs are local files supplied by absolute path.
- Runtime outputs, if implementation later creates them, are ignored and remain outside Git.
- The default timeout is `300` seconds.
- The maximum timeout accepted by v0 tests is `300` seconds unless a later accepted contract revision changes it.
- Tests do not modify the current `n8n -> local-ai-python-worker -> n8n-paper-pipeline` main path.
- Tests do not write Google Drive.

## Required Sample Matrix

Future sample validation must include at least the following cases:

| Case | Input type | Expected validation focus |
| --- | --- | --- |
| `text_heavy_pdf` | Text-heavy PDF | Markdown, HTML, JSON, metadata, and text extraction completeness. |
| `paper_with_figures` | PDF with figures | `image_export_mode=referenced`, `assets/`, asset counts, and figure references. |
| `paper_with_tables` | PDF with tables | Table preservation, `table_count`, optional `tables/`, and structured JSON content. |
| `paper_with_links` | PDF with internal and external links | `links.json`, internal link preservation, external URL metadata, and no fetching. |
| `scanned_or_ocr_needed_pdf` | Scanned or OCR-needed PDF | `needs_ocr` or warning behavior, no silent success with empty content. |
| `large_pdf_near_timeout` | Large PDF near 300s timeout | Duration measurement, timeout enforcement, and `failed_timeout` handling. |
| `malformed_pdf` | Broken or malformed PDF | `failed_conversion` or `failed_invalid_input`, safe error message, no partial success. |
| `unsupported_file` | Unsupported file type | `failed_unsupported_format`, no shell execution, no misleading parsed status. |
| `duplicate_display_name` | Two files with same `display_name` and different `job_uuid` | Identity isolation by UUID, no overwrite by display name. |
| `same_file_twice` | Same file submitted twice with different `job_uuid` | Separate output directories, same `sha256`, distinct job identity. |

This document does not provide or create those samples.

## Test Case Template

Each future test case should record:

- Test case ID.
- Sample description.
- Input file path.
- `job_uuid`.
- Optional `display_name`, `original_name`, and `source_name`.
- Requested outputs.
- `image_export_mode`.
- `timeout_seconds`.
- Expected status.
- Expected outputs.
- Expected metadata fields.
- Expected warnings or failure code.
- Stop condition, if triggered.
- Result: pass, fail, blocked, or skipped.

Example request shape:

```json
{
  "job_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "display_name": "sample.pdf",
  "input_file_path": "/absolute/local/path/to/sample.pdf",
  "requested_outputs": ["html", "markdown", "json", "metadata", "status"],
  "image_export_mode": "referenced",
  "timeout_seconds": 300
}
```

## Required Output Validation

For successful parses, validate that the output directory contains:

- `document.html`
- `document.md`
- `document.json`
- `metadata.json`
- `status.json`

Validate conditional outputs:

- `assets/` exists when referenced images are emitted.
- `links.json` exists when links are detected or preserved.
- `tables/` exists only when table extraction is emitted.
- `text.txt` and `doctags.txt` are present when requested or generated by the implementation.

The response body should return status and artifact paths, not large document bodies.

## Metadata / Status Validation

`status.json` must include:

- `job_uuid`
- `status`
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

`metadata.json` must include:

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

Timestamps must be parseable. `duration_seconds` must be numeric and non-negative.

## Identity Validation for UUIDv4

Validate that:

- `job_uuid` is required.
- `job_uuid` must be UUIDv4.
- Invalid UUIDs return `failed_invalid_input`.
- Missing UUIDs return `failed_invalid_input`.
- `display_name`, `original_name`, and `source_name` never become authoritative identity.
- Duplicate display names with different UUIDs write to separate directories.
- Same file submitted twice with different UUIDs writes to separate directories.

## Image Mode Validation

Validate all supported `image_export_mode` values:

- `referenced`: HTML and Markdown refer to files under `assets/`.
- `embedded`: images may be embedded only when explicitly requested.
- `placeholder`: image placeholders may be used for minimal output.

Invalid image modes must return `failed_invalid_input`.

Referenced mode is the default and must not require the caller to set it explicitly.

## Link Handling Validation

Validate that:

- Internal links are preserved when supported by the parser.
- External URLs are preserved as metadata where possible.
- `links.json` records detected links when links are found.
- External URLs are never fetched.
- External URLs never trigger downloads.
- Link handling does not require network access.
- Links are metadata and do not replace the original file as evidence.

## Original File Policy Validation

Validate that:

- The original file is never copied into `artifacts/docling-service/<job_uuid>/`.
- `metadata.json` records `original_file_path` or equivalent source path metadata.
- `metadata.json` records `sha256` as `input_sha256` or an accepted equivalent.
- `status.json` records `input_sha256`.
- Derived outputs can be deleted without deleting or mutating the original file.

## Timeout / Failure Validation

Validate the default timeout:

- Missing `timeout_seconds` uses `300`.
- Values above the v0 maximum are rejected or clamped according to the final implementation decision before sample validation starts.
- A conversion exceeding the configured timeout returns `failed_timeout`.

Validate failure status values:

- `failed_timeout`: conversion exceeded configured timeout.
- `failed_invalid_input`: input path is missing, non-local, unreadable, invalid, or request fields are invalid.
- `failed_unsupported_format`: input format is unsupported by v0.
- `failed_conversion`: conversion failed to produce required outputs.
- `failed_internal`: service failed due to an internal error.

Failure responses must include safe, short error messages and must not delete original files or existing pipeline outputs.

## Security Validation

Validate that:

- Inputs are local files only.
- Remote URL input is rejected.
- Arbitrary shell commands are rejected and never executed.
- The service is not publicly exposed in v0.
- No tokens, cookies, credentials, environment values, or secrets are written to artifacts.
- No original files are copied to artifacts.
- Runtime artifacts remain ignored and are not committed.

## Artifact Layout Validation

Validate that successful and failed jobs use:

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

Only outputs relevant to the request and parser result need to exist, except required outputs on success.

No artifact path may be derived from `display_name` as identity. `job_uuid` controls the directory.

## Acceptance Criteria

The v0 test plan is accepted when it:

- Covers every required sample category from the contract.
- Keeps `docling-service` standalone and local.
- Does not imply implementation or deployment.
- Does not create `services/docling-service/`.
- Does not create `artifacts/`.
- Does not create sample files.
- Preserves the current `n8n-paper-pipeline` main path.
- Validates UUIDv4 identity.
- Validates required outputs, metadata, status, image modes, links, original file policy, timeout behavior, failure status values, security, and artifact layout.

## Stop Conditions

Future test execution must stop if:

- Required outputs are missing on a reported success.
- `job_uuid` is not treated as the only authoritative identity.
- Original files are copied into artifact directories.
- External URLs are fetched.
- Arbitrary shell execution is possible from request input.
- Secrets appear in artifacts.
- Runtime outputs are staged for Git.
- The current `n8n-paper-pipeline` main path is changed.
- The service requires n8n workflow changes for v0 validation.
- Failure statuses are ambiguous or collapse into generic success.

## Next Step After Test Plan

After this test plan is reviewed, the next step is to decide whether to prepare a minimal implementation plan.

That next step must still avoid deployment, n8n workflow changes, `n8n-paper-pipeline` main-path replacement, and Google Drive writes unless separately authorized.
