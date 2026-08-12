# Local AI Lab Session Handoff

> Status: active handoff snapshot.
>
> Prepared after the user said `准备交接`. A later session receiving
> `交接继续` must read `AGENTS.md` and this file completely, validate the
> recorded state, and then continue with the v2 design work without repeating
> the completed v1.1.1 release.

## Snapshot metadata

- Prepared at: `2026-08-12 12:42 EDT` (`America/New_York`).
- Canonical repository: `/Users/zeyuan/Projects/local-ai-lab`.
- GitHub: `git@github.com:kumaxs/local-ai-lab.git`.
- Branch: `main`.
- Released engineering commit:
  `eeda304f20e6d91fe11fc1368c0ce1bea4a7520a`.
- Remote `origin/main` at release verification:
  `eeda304f20e6d91fe11fc1368c0ce1bea4a7520a`.
- Annotated tag object: `ebda083c94478763fa5207d072dcdb97f66c4f4a`.
- Tag target: `v1.1.1^{}` =
  `eeda304f20e6d91fe11fc1368c0ce1bea4a7520a`.
- Release commit message:
  `fix: harden Docling quality evidence and recovery`.
- Expected final branch shape: the release commit above followed by one
  handoff-only commit that changes this file. Validate with `git log -2`.
- No untracked files were present during handoff inspection.

Important path warning: `/Users/zeyuan/Local-AI-Lab` is a retired tombstone.
Always work in the canonical path above.

## Objective and scope

The completed workstream hardened the existing Docling v1 conversion path,
published it as the reproducible `v1.1.1` checkpoint, and retained a unified
23-paper manual-review bundle. The user then decided that further paper-level
patching is not a viable path: known-paper tests pass, but new document types
continue exposing new semantic failures.

The next workstream is **Docling v2 architecture**, not another round of v1
paper-specific fixes. V1 remains available and released, but is not approved
for unattended high-fidelity production use.

## Completed and verified

### 1. Docling v1.1.1 quality hardening

Release commit `eeda304` contains:

- midpoint/page/column-clamped source crops with provenance verification;
- closed, accessible, idempotent source-evidence disclosures for tables,
  formulas, inline math, algorithms, and code;
- local-first, fail-closed image-table recovery with strict geometry, bounds,
  semantic-hint, overlap, grid, and hostile-offset checks;
- paragraph-scope evidence for mixed prose and inline math, while preserving
  repaired span text and column bounds;
- guarded CJK fallback that preserves the prior surface when machine formula
  normalization cannot be proven safe;
- strict unique CJK equation occurrence, tag, body-identity, and source binding;
- malformed URL/path/bbox/crop/table input rejection and Markdown code-fence
  protection;
- expanded regression coverage for all of the above.

Primary implementation files:

- `docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py`
- `docs/integrations/docling-serve-quality-parity/semantic_reflow.py`
- `docs/integrations/docling-serve-quality-parity/test_quality_parity_adapter.py`
- `docs/integrations/docling-serve-quality-parity/test_semantic_readability_regressions.py`
- `docs/integrations/docling-serve-quality-parity/test_source_evidence_identity.py`

### 2. Version, push, tag, and GitHub Release

- `main` push: `5ffd64c..eeda304` succeeded.
- Annotated tag `v1.1.1` was pushed successfully.
- GitHub Actions run:
  `https://github.com/kumaxs/local-ai-lab/actions/runs/31617262871`.
- Run ID `31617262871`: `completed/success`.
- All seven jobs succeeded: validation, macOS package, generic bundle, API
  image, backend image, formula image, and GitHub Release creation.
- Published Release:
  `https://github.com/kumaxs/local-ai-lab/releases/tag/v1.1.1`.
- Release ID: `369387802`.
- Release is public, not a draft, and not a prerelease.

Published assets and GitHub-reported digests:

- `docling-service-1.1.1.tar.gz`
  (`sha256:cc7b60bae4e4cfca2fcb1bde717cf7baf9782ee1cb89b433fac1a7397d2efc4a`)
- `docling-service-1.1.1.tar.gz.sha256`
- `docling-service-1.1.1.zip`
  (`sha256:099bc5767287a3de0345a61a3d0e2c2b77653f26b2adcfe1fbaf77dcf2360bdb`)
- `docling-service-1.1.1.zip.sha256`
- `SHA256SUMS`

The workflow also published `1.1.1` and refreshed `latest` for:

- `ghcr.io/kumaxs/local-ai-lab-docling-api`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula`

### 3. Manual-review bundle

Persistent local review entry:

`/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-full-human-review-2026-08-12/review_index.html`

Its directory contains a README and manifest and is intentionally ignored by
Git. It indexes 23 unique papers, including 12 current-comparable cases and
three fresh holdouts. It is retained outside the service Janitor for operator
review; do not delete it until superseded or explicitly released by the user.

The three holdouts are now **development evidence**, not future blind tests:

1. C-Eval: picture-contained OCR was misclassified as semantic code/algorithm,
   a figure was omitted, and picture-contained math triggered a formula gate.
2. Lattice: Algorithm 2 was misbound to Algorithm 1 in HTML, Algorithm 2's
   evidence omitted its final step, Algorithms 3 and 4 were discarded or lacked
   source evidence, and formulas 1–5 were appendix-only in HTML.
3. PdfTable: automated gates passed, but manual review found collapsed table
   rows, concatenated numeric cells, and omitted Figures 3 and 4.

This proves that v1's producer and validators can agree on the same incorrect
structure. Release Notes explicitly state that v1.1.1 is a checkpoint, not a
production-readiness approval.

## Tests and validation

Final local validation before the release commit/tag:

```text
Parity suite: 631 tests, OK, 5 skipped
Service suite: 165 tests, OK
Compile check: 32 release/task Python files compiled from source
Compose validation: compose.yaml and compose.release.yaml passed
Shell validation: docker-up.sh, docker-down.sh, macOS install scripts passed
git diff --check: passed
```

Exact suite commands:

```bash
cd /Users/zeyuan/Projects/local-ai-lab/docs/integrations/docling-serve-quality-parity
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /Users/zeyuan/Projects/local-ai-lab/.runtime/docling-release/macos/venv/bin/python \
  -m unittest discover -s . -p 'test_*.py' -q

cd /Users/zeyuan/Projects/local-ai-lab
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=services/docling-service \
  .runtime/docling-release/macos/venv/bin/python \
  -m unittest discover services/docling-service/tests -p 'test_*.py' -q
```

The release bundle was built and verified twice in automatically cleaned
`/private/tmp` directories. The final verification embedded commit
`eeda304f20e6d91fe11fc1368c0ce1bea4a7520a`, version `1.1.1`, 72 files, and
both `linux/amd64` and `linux/arm64` platform declarations.

## Runtime and external state

- Existing listeners at final inspection:
  - `127.0.0.1:5001`, Python PID `12313` (Docling backend)
  - `127.0.0.1:8000`, Python PID `12919` (local API)
- No listener was found on `8001` or `8766`.
- This release/handoff turn did not start, stop, or restart either service.
- No external job remains in progress; Release run `31617262871` is complete.
- `gh auth status` reports the saved `kumaxs` token as invalid. Git SSH push
  succeeded, and release verification used the public GitHub API. A future task
  that requires authenticated `gh` operations should run `gh auth login` first.
- Release verification temporary directories were removed automatically.

## In progress

No implementation, conversion, release, or CI operation is in progress.

V2 has been designed at the proposal level only. No v2 production source,
schema, migration, or benchmark implementation has been created yet.

## Blockers, risks, and failed approaches

### Production-readiness blocker

V1 is blocked from unattended high-fidelity production use. Publishing
`v1.1.1` does not remove this block. The fresh holdout review demonstrated
semantic false positives, omissions, and a false-success gate.

### Architectural root cause

The two main v1 files total roughly 35,000 lines and mix parsing, paper repair,
source evidence, mutation of final HTML/Markdown, and validation. Many gates
verify consistency among artifacts produced by the same assumptions instead
of independently checking the PDF. Adding another paper-specific rule should
be treated as an anti-pattern, even when it makes a known fixture pass.

### V2 direction already agreed with the user

1. Freeze v1 except for security, data-loss, or release-critical fixes.
2. Introduce a typed region-level Document IR containing stable IDs, page/bbox,
   hierarchy, provenance, candidates, confidence evidence, and final state.
3. Run multiple parser candidates (initially Docling standard, Docling VLM, and
   a separately benchmarked modular alternative such as PP-StructureV3) and
   reconcile at region level rather than selecting one whole-document winner.
4. Make HTML, Markdown, and JSON pure renderers from accepted IR; do not repair
   final output with regex/string patches.
5. Build validators independent of the producer: geometry/text agreement,
   table topology/content/location metrics, figure coverage, formula occurrence
   and context, algorithm title/step/source binding, and reading order.
6. Use explicit outcomes: `verified_semantic`, `visual_only`, `unresolved`.
   Strict machine mode fails on unresolved critical regions; human-reading mode
   may retain clearly labeled visual-only regions.
7. Separate development, calibration, and sealed holdout sets. Once a holdout
   is inspected it becomes development data and must be replaced.
8. Release on per-category floors and zero silent P0/P1 failures, not a single
   average score or artifact-consistency gate.

Useful primary references previously reviewed:

- Docling architecture: `https://docling-project.github.io/docling/concepts/architecture/`
- Docling Technical Report: `https://arxiv.org/abs/2408.09869`
- OmniDocBench: `https://github.com/opendatalab/OmniDocBench`
- PP-StructureV3: `https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html`
- GriTS: `https://www.microsoft.com/en-us/research/publication/grits-grid-table-similarity-metric-for-table-structure-recognition/`
- olmOCR benchmark: `https://github.com/allenai/olmocr`
- Selective classification/reject option: `https://arxiv.org/abs/1705.08500`

## Next action

When the user says `交接继续` in a new session:

1. Read `AGENTS.md` and this file completely.
2. Change to `/Users/zeyuan/Projects/local-ai-lab` and validate branch, HEAD,
   status, tag, and this snapshot against current reality.
3. Do not reopen the completed v1.1.1 release or resume per-paper patching.
4. Start with a design-only v2 package:
   - architecture decision record;
   - typed Document IR schema and invariants;
   - parser-adapter/reconciliation interfaces;
   - independent validation contract;
   - benchmark taxonomy, dataset split, sealed-holdout protocol, and release
     thresholds;
   - staged migration plan that keeps v1 available while v2 runs in shadow.
5. Review that design with the user before implementing production v2 code or
   selecting/fine-tuning a new model.
