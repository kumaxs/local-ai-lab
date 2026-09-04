# Local AI Lab Docling Service v2 Design Overview

Status: **Proposed / design-only**

Source snapshot: `2026-09-03`

## Scope and objective

This package defines the proposed v2 design for Docling Service and explicitly
targets one objective: replacing paper-specific patching with a typed, verifiable
pipeline grounded in source-PDF evidence.

It does not implement production code.

## Core decision (high level)

- `DoclingDocument` (upstream vendor output) is a parser artifact and is not a
  stable internal truth. Upstream Docling's own “v2” document-format naming is
  distinct from this project's `Docling Service v2` boundary.
- `LAIL-IR` is the internal, vendor-neutral typed region graph used as the
  canonical conversion truth.
- Source evidence is inventoried across every page and policy-required class
  independently of parser proposals. The manifest carries explicit
  `content_present`, `blank`, or `indeterminate` state so absence cannot
  masquerade as a blank page or an unrequested evidence class.
- `Docling Service v2` is the service orchestration layer that creates
  snapshots, runs multiple candidates, reconciles them into `LAIL-IR`, validates
  independently, adjudicates final state, enforces cross-object semantic
  invariants, and renders final artifacts through pure renderers. Rendering and
  publication require an IR-bound semantic-gate attestation; publication surface
  comes from authenticated scheduler authorization, never mutable IR mode.

## Why this starts now

The handoff confirms that:

- v1 has been stabilized as `v1.1.1` and frozen as a checkpoint, not as an
  unattended high-fidelity baseline.
- Paper-specific repair paths can still satisfy internal consistency while remaining
  semantically wrong.
- The next milestone is a region-IR design with independent validation and strict
  machine gating.

Traceability references:

- [HANDOFF](../../../HANDOFF.md)
- [v1 release notes](../../../services/docling-service/release/RELEASE_NOTES.md)

## Pipeline in one sentence

`Source snapshot -> candidate/source evidence -> reconcile -> independent
validate -> adjudicate -> semantic gate -> pure render`.

```mermaid
flowchart LR
  A[Input PDF] --> B[SourceSnapshot]
  B --> S[SourceEvidenceProvider]
  B --> C1[Adapter: Docling standard]
  B --> C2[Adapter: Docling VLM]
  B --> C3[Adapter: modular benchmark candidate]
  C1 --> D[CandidateBundle]
  C2 --> D
  C3 --> D
  D --> E[Region Reconcile]
  S --> E
  E --> F[IndependentValidators]
  S --> F
  F --> G[Adjudication Policy]
  G --> V[SemanticIRValidator]
  V --> H[Renderer Pool (HTML/MD/JSON)]
  H --> I[Output + Provenance Artifact]
```

## Scope and non-goals

### In scope

- IR schema, reconciliation contract, and stage boundaries.
- Candidate interface for parser adapters and benchmark candidate policy.
- Trust boundaries, provenance discipline, confidence model, and output-state
  semantics.
- Validation contract and release gates.
- Migration and deprecation plan for v1.

### Out of scope

- Parser training or fine-tuning.
- Container/runtime infra refactor.
- Production migration execution and full test rollout.
- Any destructive file or service changes in canonical repositories.

## Documents map

| Purpose | File |
| --- | --- |
| Handoff context and constraints | [HANDOFF.md](../../../HANDOFF.md) |
| v1 checkpoint evidence and outcomes | [release notes](../../../services/docling-service/release/RELEASE_NOTES.md) |
| Current v1 delivery behavior | [OUTPUTS.md](../../../services/docling-service/docs/OUTPUTS.md) |
| Input snapshot and audit constraints | [quality-parity continuation status](../../integrations/docling-serve-quality-parity/CONTINUATION_STATUS_2026-08-10.md) |
| LAIL-IR schema | [schemas/lail-document-ir.schema.json](schemas/lail-document-ir.schema.json) |
| Coverage-policy schema | [schemas/lail-coverage-policy.schema.json](schemas/lail-coverage-policy.schema.json) |
| Frozen strict fixture policy | [policies/strict-machine-v1.coverage-policy.json](policies/strict-machine-v1.coverage-policy.json) |
| Positive and mutation fixtures | [examples/README.md](examples/README.md) |
| Interface contracts | [INTERFACES.md](INTERFACES.md) |
| Migration plan | [MIGRATION.md](MIGRATION.md) |
| Validation and benchmark policy | [VALIDATION_AND_BENCHMARKS.md](VALIDATION_AND_BENCHMARKS.md) |
| Formal decision record | [ADR-0001-LAIL-IR.md](ADR-0001-LAIL-IR.md) |

## v1 freeze statement

- `Docling Service v1.1.1` is retained for existing usage and API compatibility.
- No new paper-specific patching is added while v2 design and implementation are
  prepared.
- v1 receives only security, data-loss prevention, or release-critical defect
  patches before v2 migration.

## Review gates and open user decisions

### Required review gates

1. Architecture acceptance review against this README and ADR.
2. IR and validator schema review for source-only evidence consistency.
3. Release-gate policy review (`verified_semantic`, `visual_only`, `unresolved`).
4. Benchmark split and candidate policy review.
5. Security and trust-boundary review (no parser HTML trust).

### Open user decisions

1. API/output exposure policy for `visual_only` and `unresolved`.
2. Source/evidence retention and privacy boundaries.
3. Sealed-custody strategy and annotation budget.
4. Exact provisional thresholds, minimum support, and performance SLOs.
5. Parser/model selection for mandatory production candidates.

## strict_machine outcome rule (decided)

`strict_machine` is fixed: every **critical** region must be
`verified_semantic`. A critical `visual_only` or `unresolved` region makes the
strict delivery ineligible and the job fails closed.

## Official references used by design

- [Docling architecture](https://docling-project.github.io/docling/concepts/architecture/)
- [DoclingDocument](https://docling-project.github.io/docling/concepts/docling_document/)
- [Docling technical report](https://arxiv.org/abs/2408.09869)
- [PP-StructureV3](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
  (benchmark candidate only)
- [OmniDocBench](https://github.com/opendatalab/OmniDocBench)
- [GriTS table metric](https://www.microsoft.com/en-us/research/publication/grits-grid-table-similarity-metric-for-table-structure-recognition/)
- [olmOCR benchmark](https://github.com/allenai/olmocr/blob/main/olmocr/bench/README.md)

## Acceptance for design completion

This v2 design package is complete when:

- The terminology boundaries above are accepted.
- `LAIL-IR` schema and adapter lifecycle are approved.
- The three final states and adjudication policy are agreed.
- Trust boundaries and pure-renderer constraints are accepted.
- All open decisions are decided by user.
