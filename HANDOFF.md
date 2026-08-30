# Local AI Lab Session Handoff

> Status: completed 2026-08-30 implementation/evaluation handoff snapshot.
>
> A later session receiving `交接继续` must read `AGENTS.md` and this file
> completely, validate the recorded state, and continue from the explicit
> quality-release blockers below rather than repeating completed implementation,
> Docker/macOS smoke, or regression work.

## Final 2026-08-30 snapshot

### Repository and delivery state

- Writable repository: `/private/tmp/local-ai-lab-aug30-webui`.
- Branch: `main`; push remote: `github` =
  `git@github.com:kumaxs/local-ai-lab.git`.
- Last engineering commit before this documentation closure: `5ad1aa8`.
- At handoff completion, the documentation/cleanup commit is expected to be the
  current `HEAD` and `github/main`; always verify both hashes and a clean status.
- Canonical `/Users/zeyuan/Projects/local-ai-lab` remained a stale read-only
  source of historical runtime PDFs during this task. It was not pulled,
  reset, rebased, or overwritten.
- `/Users/zeyuan/Local-AI-Lab` is a retired tombstone.
- No tag, GitHub Release, or published container image was created. Public
  `v1.1.1` remains the legacy no-Web-UI checkpoint; current UI deployment uses
  the post-1.1.1 source build until a new release is intentionally published.

Relevant pushed commits after the earlier handoff are:

- `ecf7f7f docs(docling): record release integrity audit`
- `20f5f52 fix(docling): avoid duplicate conversion submissions`
- `c89d851 fix(webui): bound job messages`
- `e20010e fix(docling): bind final formula and region evidence`
- `fa8d9ba fix(docling): seal formula and region evidence`
- `5ad1aa8 fix(docling): harden macOS process lifecycle`

### User goal outcome

The requested implementation work is complete:

- one same-origin Web UI for direct/macOS and Docker deployment;
- PDF upload, queue pagination, trusted phase progress, output inventory,
  individual/ZIP download, terminal deletion, TTL countdowns, and storage view;
- nine live-editable lifecycle settings backed by SQLite CAS revisions, with
  `SQLite override > environment > default`, `null` override removal, and
  explicitly non-retroactive existing deadlines;
- source-bound region gates for figure/picture OCR, header/footer, table,
  formula, inline math, algorithm, and code quality;
- indentation-preserving algorithm/code delivery, cross-page algorithm
  evidence, operator-preserving inline-math identity, independent table
  topology/occupancy checks, and fail-closed sidecar publication;
- source-built Docker and direct macOS deployment validation;
- diverse fresh and old-corpus evaluation, documentation, and handoff.

The quality objective is **not production-approved**. New and old documents
complete conversion, but most still fail the stricter evidence gate. This is an
honest remaining product limitation, not unfinished wiring or a hidden service
crash.

### Web UI and API boundary

The API serves `/`, `/ui`, and `/ui/` from the packaged Python assets. Default
addresses are Docker `http://127.0.0.1:8766/ui/` and direct/macOS
`http://127.0.0.1:8000/ui/`. No Node runtime or extra Compose service is needed.

Operational boundaries that must remain documented and tested:

- cursor pages contain at most 100 jobs and counts describe only the visible
  page;
- phase/error text is a 280-Unicode-character preview scanned from at most the
  first 65,536 UTF-16 code units;
  full terminal diagnostics are read from `status.json`;
- browser bearer tokens stay only in memory; token changes and `401` clear
  protected state and invalidate delayed requests;
- unauthenticated downloads use native streaming; protected in-page downloads
  are capped at 256 MiB and larger artifacts require a streaming API client;
- submit each PDF once, retain `job_id`, and poll `GET /v1/jobs/{job_id}`;
  repeated multipart submission is not polling, and uncertain retries reuse one
  `Idempotency-Key`;
- editable runtime values are input, success-output, failed-output, job,
  staging, temp, cleanup interval, idempotency, and download-lease TTLs.
  Paths, token, model/engine, concurrency/capacity, and webhook delivery-history
  TTL remain deployment/read-only settings.

### Docker end-to-end evidence

An isolated source-built Compose stack processed Pseudo2CodeQA as real job
`f83a0e65-f9bc-4ec3-b34e-888e2b7333e5` in about 54 seconds. The client submitted
once and polled by GET. The published job directory contained 43 regular files;
formula occurrence coverage was `2 / 2`, both inline-math records were bound,
and the strict region
gate had 168 records (`165 verified_semantic`, `3 visual_only`, `0 unresolved`).
Manifest-bound downloads and archive CRC validation passed.

The Web UI state machine has deterministic Node coverage for stale token,
navigation, refresh, configuration, output, bounded-message, and cyclic cursor
races, plus server integration coverage. This Docker run was an API/UI-assets
E2E, not a claim of manual browser visual review.

### macOS lifecycle evidence

`5ad1aa8` replaces the wrapper-only lifecycle with instance-bound
supervisor/guard/child management. It uses flock, atomic metadata, Darwin birth
identity, SID/listener/health checks, symlink-safe bounded log rotation,
conservative legacy-PID migration, bounded exact-instance shutdown, and
recovery from supervisor, guard, child, or dual death.

Focused process-lifecycle tests pass 26/26 and focused distribution tests pass
24/24 (50 combined); the complete service/distribution discovery passes
218/218. Real Darwin smoke covered default ports, custom `55001/58001`
ports across a fresh shell, normal stop, SIGKILL recovery for each role and dual
death, and bounded SIGSTOP escalation. The evaluation service was then stopped;
status reported backend/API `stopped`, and ports `5001`/`8000` had no listeners.

### Document-quality evidence

Formula ownership is intentionally narrow: Route A remains the non-formula
document baseline. An accepted private `formula_service` sidecar is authoritative
for formulas and skips generic second pass; an explicit Route-B/guarded
`apply-all` result becomes final only after full source, occurrence, JSON,
Markdown, and rollback-protected final-surface gates pass.

Fresh direct set (no service exceptions/timeouts):

```text
cases: 6
strict passed / failed: 1 / 5
region records: 1248
verified_semantic: 763
visual_only: 35
unresolved / critical: 450 / 450
```

Only Pseudo2CodeQA passed (`168 verified`, `3 visual`, `0 unresolved` in the
direct run). PP-FormulaNet, LongDocBench, FRCD, CN, and Transformers GNN failed
closed on formula/inline binding, cross-page algorithm, picture/main-flow, or
table evidence. See the evaluation report for per-case values.

Fresh old-ten reconversion also had no service exception/timeout and reproduced
every locked page plus algorithm/code/table/formula high-confidence/ambiguous
inventory count exactly. Its strict result was:

```text
cases: 10
strict passed / failed: 0 / 10
source region records: 3151
retained/classified records: 3150
verified_semantic: 2899
visual_only: 58
unresolved / critical: 193 / 193
truncated cases: 1 (Donut exceeded the 1000-record sidecar cap by one)
```

The latest read-only stored 23-output compatibility replay was `0 / 23` strict
passes with `3383 verified_semantic`, `131 visual_only`, `800 unresolved`, and
`800 critical`, with no replay errors or truncation. Legacy success labels were
not grandfathered.

The sealed Doc2DB score remains scalar-only: 24 pages, tables `7 / 0`, formulas
`1 / 6`, algorithms `0 / 0`, and code `0 / 0`. It was not visually inspected or
used for tuning.

### Validation baseline

```text
Full quality-parity unittest discovery: 755 tests, OK, 5 skipped
Docling service/distribution unittest discovery: 218 tests, OK
Focused macOS process lifecycle: 26 tests, OK
Focused distribution/release bundle: 24 tests, OK
Web UI concurrent-state Node suite: 16 tests, OK
Source-built Docker Pseudo2CodeQA job and archive: passed
New-six direct conversion completion: 6 / 6; strict quality: 1 / 6
Old-ten direct conversion completion: 10 / 10; strict quality: 0 / 10
Old-ten inventory baseline equality: 10 / 10
```

The durable evaluation record is
`docs/integrations/docling-serve-quality-parity/evaluation/results-2026-08-30.md`.
PDFs and generated conversion outputs are not committed.

### Cleanup state

After recording the scalar results, this task removed its three `/private/tmp`
corpus/input directories, the new-six/old-ten/live-smoke review directories,
and generated `__pycache__` directories. It also removed the isolated
`docling-aug30-smoke` containers, network, five named volumes, and three local
images whose Compose labels identified that project. Those job files and model
volumes are intentionally not recoverable; their durable non-PDF evidence is
the evaluation report and this handoff. No task-owned service or Docker resource
remains running.

### Remaining risks and next action

Do not publish a production-quality claim or unattended high-fidelity release
from this state. The next quality workstream should make generic, evidence-bound
improvements for FRCD/CN/PP-FormulaNet/Transformers-GNN formula and inline math,
LongDocBench picture/table residuals, and Donut record-volume handling. It must
then rerun all fresh six, old ten, and stored 23 outputs without inventory
regression or unresolved critical regions.

If a UI-containing public release is desired after that decision, bump every
version source together, build and verify both archives and all three images,
run the release workflow, and state explicitly that `v1.1.1` never contained
the UI. Do not merely retag current source or mix it with old published images.

## Superseded intermediate 2026-08-30 snapshot (historical)

> The section below was written before service authorization, Docker/macOS E2E,
> lifecycle hardening, and the final new/old evaluations. Its pending-smoke
> instructions and old counts are preserved only as history and must not be
> executed or treated as current state.

### Repository and branch state

- Prepared at: `2026-08-30 04:29 EDT` (`America/New_York`).
- Writable working repository: `/private/tmp/local-ai-lab-aug30-webui`.
- Branch: `main`.
- Push remote: `github` = `git@github.com:kumaxs/local-ai-lab.git`.
- Engineering HEAD before this documentation/handoff commit:
  `ffd7f9e`.
- `github/main` was the same commit immediately before this handoff-only commit.
  After resuming, validate that `git rev-parse HEAD` and
  `git rev-parse github/main` agree and inspect `git log -3`.
- The writable repository was clean before this handoff edit.
- Canonical repository `/Users/zeyuan/Projects/local-ai-lab` was clean but stale
  at `fdaa248e66a8bf839cc212e2cbde1aaa82e6293d`. Do not pull, reset, rebase, or
  overwrite it without the user's explicit authorization.
- `/Users/zeyuan/Local-AI-Lab` is a retired tombstone, not the active repository.

The staging repository is retained because the user-requested smoke is still
pending. It contains the repository README and all pushed work. Do not delete it
until its state has been safely reconciled with the canonical repository.

### User goal status

The offline implementation, tests, evaluation records, documentation, commits,
and pushes requested on 2026-08-30 are complete. Fresh end-to-end conversions
and a live browser smoke under the new code remain unverified because they
require explicit authorization to start local services. The current source
tree supports direct and source-built Docker deployment; a new formal tagged
release containing the UI has not been published, and published `v1.1.1`
artifacts remain the legacy no-UI checkpoint.

Completed and pushed work includes:

- `7003c72 feat(docling): add secure operations web UI`
- `5ae0748 fix(docling): block picture OCR algorithm promotion`
- `6cf0103 fix(docling): detect cross-page procedure algorithms`
- `dd2c4dc feat(docling): add region-level quality gate`
- `f406023 fix(docling): fail closed on unbound cross-page algorithms`
- `f747b7e fix(docling): bind region evidence to final artifacts`
- `569f1d5 fix(docling): close region evidence bypasses`
- `f83afa7 docs(docling): record August quality evaluation`
- `ed51a12 docs: refresh session handoff`
- `20b9a7a fix(docling): close web ui and region audit gaps`
- `220f9f3 fix(webui): bound automatic refresh during navigation`
- `3360d64 docs(docling): record completion audit and handoff`
- `ffd7f9e fix(release): fail closed on version drift`

Several intervening corpus/test commits (`5ae7a5a`, `eda3755`, `6b1a2a8`,
`9128d7d`, and `19084c9`) are also already on `github/main`.

### Web UI and configuration management

The Docling service now serves the operations UI from `/`, `/ui`, and `/ui/` in
the same API process, for both Docker and direct deployment. It provides:

- upload with real browser upload progress;
- queue, job phase, and processing-progress visibility;
- artifact inventory, download, and explicit deletion;
- server-time-based expiry countdowns and expired-output state;
- storage/cleanup status;
- cursor-based previous/next pages of at most 100 jobs, current-page counts,
  and refresh of the visible page without stale row accumulation;
- optimistic-concurrency runtime configuration for input, successful-output,
  failed-output, job, staging, temporary-file, cleanup-interval, idempotency,
  and download-lease TTL values.

Runtime settings use `SQLite override > environment > default`; setting a value
to `null` clears its override. Updating a TTL does not silently recalculate
existing deadlines. The Janitor safely adopts changes without duplicate loops.

Security and failure-path work includes a restrictive CSP, no browser storage,
memory-only token handling, `410 Gone` for expired outputs, symlink/TOCTOU
hardening, directory-fd/inode checks, and a 256 MiB protected browser Blob cap.
Token save/replace/clear and `401` now invalidate delayed job, configuration,
output, upload, storage, and download responses and clear protected UI data.
Refresh/navigation requests use independent generations; cyclic or
non-advancing cursors are quarantined. One automatic tick queues behind a
normal navigation, and the second takes over a hung navigation, so automatic
refresh cannot remain suppressed indefinitely.
The direct-install default port is `8000`; Docker publishes `8766`.

### Release deployment integrity

An offline bundle audit found a deployment-critical drift path after the first
handoff: the builder accepted a different `--version` while copying stale
`1.1.1` Compose image defaults and embedded service versions. A nominal future
bundle could therefore have pulled the published no-UI `v1.1.1` images.

`ffd7f9e` makes source and archive release identity fail-closed across
`pyproject.toml`, API/formula/package constants, source/release Compose tags,
Dockerfile arguments, the macOS installer, and active bundle documentation.
The copied snapshot is revalidated before the output directory is created.
The verifier also rejects duplicate manifest keys, unknown/duplicate Compose
services, malformed Python 3.9 TOML fallback input, hidden/conflicting version
markers, unsafe paths, links, special files, and duplicate archive members.
Independent adversarial review found no remaining P0/P1/P2 after the fixes.

An audit-only bundle from exact commit
`ffd7f9e682344a02b7de48cbd080a0efd482ea12` verified tar and zip checksums,
78 files, both declared Linux platforms, and byte-identical Web UI assets in
source/tar/zip. No release/tag/image was published; formal `v1.1.1` remains
the older no-UI checkpoint.

### Literature-quality work

The region-level gate now fails closed unless final artifacts are independently
bound to source evidence. Important covered cases include:

- page-header/footer and picture-OCR quarantine so they cannot pollute body text
  or be promoted to algorithms;
- page/bbox/union identity for formula, algorithm, table, and inline-math
  evidence, including cross-page procedure algorithms;
- algorithm-sidecar kind, contributor, hash, final-node, and candidate binding;
- strict table topology: bounded geometry and work, valid bounds/spans, no
  overlap, no row collapse/crossing, and full declared-grid occupancy;
- algorithm `table_grid` contributors independently require a real non-empty,
  bounded, non-overlapping, fully occupied grid; the ordinary empty-table
  fallback cannot promote an empty or sparse algorithm contributor;
- normalized inline-math comparison that retains operators, relations,
  punctuation, and Unicode math symbols, while rejecting truncated candidates;
- typed, bounded, duplicate-free formula/inline/structural collections and
  explicit failure for malformed or partial sidecar state;
- persistence of caller-supplied status/metadata when sidecars are written, so
  a late sidecar failure cannot leave a false-success record.

The completion audit removed an unbound text-only axis-tail heuristic because
it could erase legitimate captions or short prose. Visual/chart material is
now removed only through source-bound structure, page/bbox, picture overlap,
repetition, or equivalent evidence. Independent final review found no
reproducible code-level P0/P1/P2 after these fixes, but this does not supersede
the fresh E2E acceptance blocker below.

### Validation evidence

Final offline validation on the pushed code:

```text
Focused region-gate suite: 86 tests, OK
Full quality-parity suite: 724 tests, OK, 5 skipped
Docling service suite: 189 tests, OK
Release distribution gate: 21 tests, OK
Python 3.9 release-gate subset: 4 tests, OK
Web UI concurrent-state suite: 9 tests, OK
Python compile checks: passed
JavaScript syntax check: passed
JSON/jq checks: passed
Source and release Compose config checks: passed
Release/deployment shell syntax checks: passed
git diff --check: passed
```

No service process was started, stopped, or reconfigured during this work.
Read-only inspection found no listener on `5001`, `8000`, `8001`, or `8766`.

### Evaluation corpus and results

Versioned evaluation records are in:

- `docs/integrations/docling-serve-quality-parity/evaluation/results-2026-08-30.md`
- `docs/integrations/docling-serve-quality-parity/evaluation/corpus-lock-2026-08-30.json`

The fresh, diverse corpus contains LongDocBench, Pseudo2CodeQA, PP-FormulaNet,
FRCD (including a page 7-to-8 cross-page algorithm), and a sealed Doc2DB paper.
The sealed paper was inventoried but not visually inspected or used for tuning.
The historical ten-PDF inventory remains locked for regression comparison.

A strict read-only replay of the existing 23-paper artifacts produced:

```text
cases: 23
passed: 0
failed: 23
verified_semantic: 1981
visual_only: 131
unresolved: 1120
critical_unresolved: 1120
```

This is an intentional fail-closed gate result, not a claim that all extracted
content is unusable. It exposes remaining critical structural uncertainty. The
new `2607.25988` case verified its algorithm and 11 of 12 ordinary tables; its
remaining table failed because the declared 11x8 grid has an incomplete header
slot. PdfTable still exposes row collapse/incomplete occupancy, and the old
table-transformer case still exposes incomplete grids, row collapse, and an
algorithm bbox mismatch.

Do not overstate two offline signals. A zero algorithm/code/table inventory
count with proof `healthy` means the inventory completed consistently; it does
not independently prove visual absence. Inline math currently preserves
operator-bearing identity and bound source anchors/crops, but ordinary prose is
not universally rendered as inline MathML or a dedicated semantic math span.
Both remain fresh-E2E/manual-review questions.

Fresh source PDFs and rendered inspection pages are temporarily retained at
`/private/tmp/docling-eval-2026-08-30` only for the pending authorized smoke.
They are deliberately uncommitted; that directory now contains a `README.md`
with ownership, sealed-input, and cleanup instructions. Delete that exact
directory after the smoke and documentation update, or if the user decides not
to run the smoke.

### Remaining blocker and exact next action

The fresh corpus has been inventoried and the offline gates have been tested,
but it has not yet been converted end to end by the local Docling backend,
formula service, and API under this branch. The Web UI has likewise passed
syntax/service tests but not a live browser upload/queue/TTL/download smoke.

Repository rules require explicit user permission before starting those local
services. On authorization:

1. Start the local Docling backend, formula service, and API without changing
   the user's existing configuration.
2. Convert a Chinese case, an English/two-column case, and at least one fresh
   calibration case; include the sealed case only as a blind final observation.
3. Verify source-to-final region sidecars and inspect formula, inline math,
   algorithm/code indentation, tables, headers, and footers.
4. Exercise Web UI upload progress, queue/phase display, TTL configuration and
   countdown, artifact download, expiry behavior, and deletion.
5. Stop only processes started by this task, record results in the evaluation
   docs and this handoff, run the relevant regression suites, commit, and push.
6. Only after that evidence, decide whether to publish a new tagged release
   containing the UI; do not describe `v1.1.1` as containing it.
7. Remove `/private/tmp/docling-eval-2026-08-30` and any new transient files.

Do not mark the goal complete before this smoke is either run successfully or
the user explicitly accepts its omission.

## Archived 2026-08-12 snapshot

Everything below this point is the preserved earlier release handoff. It is
historical context and must not override the active 2026-08-30 state above.

### Snapshot metadata

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

### Objective and scope

The completed workstream hardened the existing Docling v1 conversion path,
published it as the reproducible `v1.1.1` checkpoint, and retained a unified
23-paper manual-review bundle. The user then decided that further paper-level
patching is not a viable path: known-paper tests pass, but new document types
continue exposing new semantic failures.

The next workstream is **Docling v2 architecture**, not another round of v1
paper-specific fixes. V1 remains available and released, but is not approved
for unattended high-fidelity production use.

### Completed and verified

#### 1. Docling v1.1.1 quality hardening

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

#### 2. Version, push, tag, and GitHub Release

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

#### 3. Manual-review bundle

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

### Tests and validation

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

### Runtime and external state

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

### In progress

No implementation, conversion, release, or CI operation is in progress.

V2 has been designed at the proposal level only. No v2 production source,
schema, migration, or benchmark implementation has been created yet.

### Blockers, risks, and failed approaches

#### Production-readiness blocker

V1 is blocked from unattended high-fidelity production use. Publishing
`v1.1.1` does not remove this block. The fresh holdout review demonstrated
semantic false positives, omissions, and a false-success gate.

#### Architectural root cause

The two main v1 files total roughly 35,000 lines and mix parsing, paper repair,
source evidence, mutation of final HTML/Markdown, and validation. Many gates
verify consistency among artifacts produced by the same assumptions instead
of independently checking the PDF. Adding another paper-specific rule should
be treated as an anti-pattern, even when it makes a known fixture pass.

#### V2 direction already agreed with the user

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

### Next action

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
