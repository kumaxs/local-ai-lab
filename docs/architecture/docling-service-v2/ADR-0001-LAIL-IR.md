# ADR-0001: LAIL-IR Region Pipeline for Docling Service v2

Status: **Proposed**

Type: **design-only**

Date: `2026-09-03`

Decision owner: `Local AI Lab (handoff follow-through)`

## Context

`Docling Service v1.1.1` remains the current released checkpoint and is frozen
as a compatibility baseline, not as an unattended high-fidelity production
approval or the long-term design.
Handoff evidence shows parser-specific fixes can still produce internally
consistent-but-wrong outputs. We need a region-centric architecture that:

- rejects mutable parser truth,
- separates parser confidence from source verification,
- keeps rendering strictly downstream from adjudicated regional truth.

## Artifacts in this package

- [LAIL-IR schema](schemas/lail-document-ir.schema.json)
- [Coverage-policy schema](schemas/lail-coverage-policy.schema.json)
- [Frozen strict fixture policy](policies/strict-machine-v1.coverage-policy.json)
- [Example IR](examples/minimal-verified.document-ir.json)
- [Schema and semantic mutation cases](examples/validation-cases.json)
- [Interfaces](INTERFACES.md)
- [Validation and benchmarks](VALIDATION_AND_BENCHMARKS.md)
- [Migration](MIGRATION.md)

## Decision

We define v2 as a deterministic six-stage pipeline:

1. Source snapshot.
2. Parallel candidate-adapter and independent source-evidence acquisition.
3. Region reconcile.
4. Independent validation.
5. Policy adjudication and semantic-invariant gate.
6. Pure render and atomic publication.

`DoclingDocument`, `LAIL-IR`, and Docling Service execution are distinct and
non-overlapping in responsibility.

- **`DoclingDocument`**: upstream vendor-native parse artifact, treated as one
  candidate representation.
- **`LAIL-IR`**: vendor-neutral canonical intermediate representation.
- **`Docling Service v2`**: stage orchestration, candidate execution, adjudication,
  and immutable output publication.

## Pipeline details

### 1) Source snapshot

- Snapshot is created before any parser call.
- Inputs are represented by **opaque source handles**, never absolute filesystem
  paths in IR-level records.
- The service resolves an opaque snapshot ID through a quarantined,
  access-controlled runtime map. Filesystem paths never enter LAIL-IR.

Mandatory snapshot fields:

- `source_snapshot_id` (opaque, confined)
- `sha256`
- `media_type` (`application/pdf`)
- `size_bytes`
- `page_count`

Acquisition time, input channel, and storage handles are runtime audit metadata,
not portable IR fields.

### 2) Candidate adapters

Each adapter returns a `CandidateBundle` containing:

- a source-bound `producer_run_id` and `source_snapshot_id`,
- adapter, engine, model/config digests in the producer-run record,
- zero or more typed region candidates with page ID, point bbox, role, payload,
  evidence IDs, and raw confidence channels,
- an optional bounded vendor-payload manifest containing only media type, byte
  size, and SHA-256 digest.

The modular PP-StructureV3 route is a **benchmark producer candidate only**:

- It is benchmarked against the same source snapshots and split protocol.
- It is not an independent validator.
- It is not required for production-mode adjudication unless explicitly promoted
  in a future milestone.

Every adapter is least-privilege: read-only snapshot access, bounded isolated
scratch storage, no ambient credentials/host filesystem, and no network egress
except an explicitly policy-allow-listed local model endpoint recorded in the
run manifest.

The `SourceEvidenceProvider` runs from the immutable snapshot without reading
candidate content. It may run in parallel with adapters and emits source crops,
geometry, OCR/visual observations, and provider trust-domain metadata for the
reconciler and validators.
For strict delivery it inventories every page independently of parser proposals;
the serialized manifest records the policy-required evidence classes and their
`observed|absent|indeterminate` state. Each content-bearing observation must map
to an IR region, while a blank page requires an explicit source-backed blank
pass and page-support evidence.

### 3) Region reconcile

Candidates are converted into region proposals and reconciled into `LAIL-IR` via
document-level region graph matching:

- geometry-constrained alignment,
- hierarchy inference and read-order reconstruction,
- candidate merge/override using policy and explicit uncertainty marks.

No repair passes or textual monkey-patches are allowed at this stage.

### 4) Independent validate

Validators are independent of parser claims and read source evidence:

- formula body occurrence + bbox sanity,
- table topology and content consistency,
- algorithm step/title/source binding,
- figure and page coverage,
- reading order coverage and duplicate/omit checks.

Validation outputs are immutable `validation_reports` bound to the source,
snapshot, run, policy, trust-registry epoch, and canonical target SHA-256. They
cannot be folded back into parser outputs, replayed across runs, or used to
certify a changed final payload.

Coverage is not an implementation-local table. The immutable, schema-valid
`CoveragePolicy` artifact maps every controlled role and semantic payload kind
to required check classes, and every check class to allowed evidence kinds,
coverage roles, and subject scopes. The trusted launcher hashes the RFC 8785
canonical policy value into `run.policy_sha256`; missing or duplicate rules,
local overrides, and digest mismatches fail closed.

### 5) Policy adjudication

Adjudication consumes reconciled IR + validation evidence and assigns each region:

- `verified_semantic`
- `visual_only`
- `unresolved`

Before rendering, `SemanticIRValidator` enforces digest identity, unique IDs,
reference closure, bbox/page/count consistency, validation applicability,
producer/validator independence, and relation/lineage acyclicity. JSON Schema
shape validation alone can never authorize delivery.
It also applies the digest-pinned criticality policy and trust registry to the
complete producer/evidence-provider/validator chain. Missing inventory,
unattested implementations, source mismatches, or criticality downgrades fail
closed. Validator outage is represented as an unavailable source-bound report
with indeterminate checks, never fabricated evidence. Such a coherent failed,
ineligible IR is retained as an audit record but is not renderable or publishable.

### 6) Pure render and publication

Renderer stage accepts **only adjudicated IR plus a matching successful semantic
gate attestation** and emits HTML/Markdown/JSON from that IR. No parser HTML is
trusted or directly injected.

`strict_machine` policy is fixed: every critical region must be
`verified_semantic`. Critical `visual_only` and `unresolved` outcomes both fail
closed.

## LAIL-IR graph contract

### Node model (vendor-neutral)

- `region_id`
- `role`: a controlled role such as `paragraph`, `table`, `display_formula`,
  `algorithm`, `figure`, `caption`, or `code_block`.
- `page_id`, whose page record carries a **1-based** `page_number`.
- `bbox`: `{left, top, right, bottom}` in page-local PDF points.
- `candidate_ids` and `evidence_ids`.
- `lineage`: stability scope, reconciliation policy ID, operation, and
  predecessor region IDs.
- `resolution.final_state`: exactly `verified_semantic`, `visual_only`, or
  `unresolved`.

### Edge model

- `contains`
- `in_order`
- `caption_of`
- `label_of`
- `footnote_of`
- `continuation_of`
- `references`
- `derived_from`
- `inline_in`

Every causal or structural relation graph (`contains`, `in_order`,
`caption_of`, `label_of`, `footnote_of`, `continuation_of`, `derived_from`, and
`inline_in`) must be acyclic; only non-causal `references` may contain cycles.
The lineage graph is also acyclic and uses exact predecessor cardinality:
`original=0`, `split=1`, `remap=1`, and `merge>=2`.

## Bbox contract

- `bbox` uses top-left anchored coordinate semantics and PDF point units.
- Half-open boundaries: `[left, right)`, `[top, bottom)`.
- Non-empty and ordered (`right > left`, `bottom > top`) constraints are
  enforced by the semantic validator because JSON Schema cannot express the
  cross-field comparison.
- Regions are validated with deterministic geometric matching before confidence
  scoring.

## ID stability and lineage

### Region ID rules

- `region_id` MUST NOT depend on candidate text hashes or candidate list order.
- Stability scope is: same `source.sha256` + same `schema_version` + same
  `reconciliation_policy` + equivalent geometry/anchors.
- For ambiguous anchors, `region_id` is regenerated via a local deterministic
  remap pass (`run_local_id_remap`); the remap is recorded with
  `lineage.operation=remap` and `predecessor_ids`.

### Immutable provenance

- Candidate payloads are persisted with bounded retention and digest-labeled
  storage keys.
- Rejected or superseded payloads are moved to a bounded quarantine path/ring
  (never discarded silently).
- Provenance digests are included in final metadata and are immutable once
  published.

## Evidence and confidence

Three confidence planes are separated:

- `candidate.raw_confidence`: parser-native signals with their native scale.
- source-bound evidence plus raw `validation_reports` from independent
  validators.
- `resolution.calibrated_confidence`: an optional advisory score calibrated only
  from the calibration set, never from sealed holdout.

Calibrated confidence is used only for advisory thresholds in release planning and
adjudication reports.

## Delivery modes and benchmark split

### Delivery modes (only)

- `strict_machine`
- `human_reading`
- `shadow`

### Benchmark splits

- `dev` (iteration)
- `calibration` (threshold tuning)
- `sealed_holdout` (performance validation and refusal to calibrate against).

Benchmark splits are scoped separately from the three delivery modes.

## Renderer and trust boundaries

Renderers are pure functions over adjudicated IR:

- no trust in parser-injected HTML,
- no side effects,
- deterministic serialization,
- no runtime egress from renderer.
- context-sensitive HTML/Markdown escaping and a fixed safe URI/element policy.

Strict behavior:

- if any critical region is not `verified_semantic`, publication for
  `strict_machine` must fail; the job does not produce a green-pass artifact.

Publication is generation-based: all files and their checksum manifest are
written, flushed, verified, and commit-marked in a new immutable generation
before one active pointer is atomically swapped. Authorization-epoch fencing
prevents stale workers from publishing after a kill-switch or rollback. Restart
recovery never serves or promotes an incomplete generation.
The pointer update is compare-and-swap over the prior generation, authorization
epoch, and per-job sequence so two same-epoch publishers cannot both commit.
The publisher derives its only allowed surface from an authenticated scheduler
authorization and requires a gate attestation bound to the same IR/run/epoch;
mutable IR mode cannot promote shadow output or bypass semantic validation.

## Consequences

Positive consequences:

- Parser replacement is localized to adapters; LAIL-IR, adjudication, and
  renderers remain vendor-neutral.
- Every accepted semantic region has source-bound evidence and independent
  validation lineage.
- Abstention is represented explicitly instead of being hidden by a plausible
  renderer output.

Costs and constraints:

- Source evidence, candidates, and reports require bounded storage and retention
  policy.
- Region reconciliation and sealed annotation add latency and operational cost.
- Schema evolution requires explicit migrations and compatibility fixtures.
- JSON Schema handles shape; a separate semantic validator must enforce graph,
  reference, geometry, digest-identity, and count invariants.

## Rejected alternatives

- **Treat `DoclingDocument` as canonical truth:** rejected because it is a
  producer-native representation and couples trust to one parser.
- **Select the highest parser confidence:** rejected because correlated parser
  scores are not source verification.
- **Repair final HTML/Markdown strings:** rejected because it loses region
  lineage and can create internally consistent but semantically false output.
- **Silently fall back from v2 to v1:** rejected because v1 output must never be
  relabeled as v2-verified.

## Open questions (remaining)

1. API/output exposure policy for each final state under `human_reading` and
   `shadow`.
2. Source/evidence retention window and privacy boundaries.
3. Sealed custody + annotation budget per batch.
4. Provisional thresholds, minimum support and SLO targets.
5. Parser/model default set for mandatory production mode.

## External references

- [Docling architecture](https://docling-project.github.io/docling/concepts/architecture/)
- [DoclingDocument](https://docling-project.github.io/docling/concepts/docling_document/)
- [Docling technical report](https://arxiv.org/abs/2408.09869)
- [PP-StructureV3](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
