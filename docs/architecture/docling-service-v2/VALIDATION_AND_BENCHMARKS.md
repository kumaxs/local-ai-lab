# Docling Service v2 Validation and Benchmark Protocol

Status: **Proposed / design-only**

Protocol version: `0.1.0-draft`

Date: `2026-09-03`

## 1. Evidence boundary

The current review corpus contains 23 unique papers, including three items that
were originally fresh holdouts. All 23 have now been inspected and therefore are
development evidence only. This package makes no sealed-set quality claim.

The PdfTable case confirms why structural self-consistency is insufficient:

- Table 2 rows were collapsed and values concatenated.
- Table 3 changed `2144 + 2764` into `21442764` and left the next row empty.
- Figures 3 and 4 were omitted.

Those failures passed prior machine gates. V2 acceptance therefore requires
source-bound evidence and a validator that is independent of the producing
parser, not merely agreement among artifacts produced by the same path.

## 2. Validator trust contract

Producer conformance checks may report syntax, schema, and internal consistency,
but they do not certify semantics. A report can contribute to semantic
certification only when its implementation:

1. reads the immutable source snapshot or independently curated ground truth;
2. does not consume candidate content as its observation source;
3. declares an `independence_group` describing correlated code, model, data, and
   human-annotation dependencies; and
4. emits `pass`, `fail`, or `indeterminate` for every check.

`independence_group` is trust-domain metadata, never a scheduler or deduplication
key. Confidence from a producer or a correlated validator cannot substitute for
source verification.

The trusted launcher computes implementation/config digests and loads an
immutable trust registry pinned in `run.trust_registry_sha256`. The registry
attests exact producer, evidence-provider, and validator identity/version/digest
tuples plus their dependency graph. Unknown, revoked, stale, or merely
self-declared identities cannot certify output.

The coverage side is independently reproducible from the schema-valid
[strict-machine-v1 CoveragePolicy](policies/strict-machine-v1.coverage-policy.json),
validated by [its Draft 2020-12 schema](schemas/lail-coverage-policy.schema.json).
The fixture policy's RFC 8785 canonical SHA-256 is
`c8e2c8015a883cb94d08d853056aa7f8de31082ea8bbea38ce62d66f879fdc9d`.
The launcher requires complete, unique rules for all controlled roles, semantic
payload kinds, criticality floors, and check classes before accepting that
digest. A run may not supply an implementation-local substitute.

### Schema-aligned validation report

```json
{
  "report_id": "vr-01234567",
  "run_id": "run-example-001",
  "source_snapshot_id": "src-example-001",
  "policy_sha256": "c8e2c8015a883cb94d08d853056aa7f8de31082ea8bbea38ce62d66f879fdc9d",
  "trust_registry_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "validator_id": "lail.table.source-structure",
  "validator_version": "0.1.0",
  "implementation_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "independence_group": "table-oracle-a",
  "source_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
  "execution_status": "completed",
  "created_at": "2026-09-03T12:00:00Z",
  "checks": [
    {
      "check_id": "table-topology-1",
      "result": "pass",
      "severity": "p1",
      "category": "table_topology",
      "check_class": "table",
      "target_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "subject": "reg-01234567",
      "evidence_ids": ["ev-01234567"],
      "reason_codes": ["source_grid_matches"],
      "metric": {
        "name": "grits_top",
        "value": 0.99
      }
    }
  ]
}
```

Region criticality is resolved by looking up each check's `subject` in LAIL-IR;
it is not duplicated as a mutable field inside the report.

## 3. Required validation coverage

| Class | Source-independent basis |
| --- | --- |
| Geometry and layout | Curated geometry labels, independent detector, or deterministic source geometry |
| Text | Curated text spans, independent OCR, or blind human transcription |
| Reading order | Curated order graph or blind human ordering |
| Table topology, location, content | Cell-level ground truth or an independent table oracle plus source crops |
| Formula occurrence, recognition, context | Source formula labels/visuals plus an independent recognizer or blind human review |
| Figures and captions | Source page inventory plus independent detection/binding review |
| Algorithms and code | Source-bound structural labels or blind human review |
| Provenance | Digest identity and manifest checks |
| Security | Policy lint and isolated scanner output |
| Operations | Runtime telemetry and queue/lease logs |

Every semantically accepted region requires a producer-independent,
source-bound passing report for every check class required by the frozen
role/payload policy. Multiple reports from the same independence group count as
one evidence basis. The same policy maps each check class to acceptable evidence
kinds and coverage roles; a generic check cannot stand in for a table, formula,
figure, algorithm, code, or other class-specific check.

Evidence providers participating in that basis must also be independent of the
selected producer according to the pinned dependency graph. A model-based
source observer is blind to candidate hypotheses; a deterministic comparator
may see a hypothesis only after source observations are sealed.

## 4. Hard gates

These gates are architectural invariants, not tunable score thresholds:

| Gate | Requirement |
| --- | --- |
| Region disposition | Every region has exactly one `final_state`: `verified_semantic`, `visual_only`, or `unresolved`. No region may disappear silently. |
| Source-wide inventory | Every accepted strict output has one policy-complete, target-digest-bound inventory entry and independent pass per page. Every required class has exactly one result; content-present observations map exactly once except declared split/merge, while blank pages require explicit page-support evidence and no content region. |
| Strict critical content | Every critical region is `verified_semantic`; critical `visual_only` or `unresolved` fails closed. |
| Criticality integrity | Role/criticality is derived from source observation plus the digest-pinned policy; missing/conflicting labels or any downgrade from the policy floor fails closed. |
| Accepted critical error | Zero P0/P1 semantic errors among accepted strict outputs. |
| Source/target binding | Every applicable check binds the exact source, snapshot, run, policy/registry epoch, subject, canonical final target, and compatible evidence geometry/asset identity. |
| Validator availability | A missing or `indeterminate` required check blocks strict acceptance. |
| Independent review | Until a validated independent cell/structure oracle exists, every applicable critical region in the sealed set receives blind human acceptance review. |
| Provenance | Document, asset, producer, validator, policy, and dataset identities are immutable and digest-pinned. |
| Trust attestation | Every producer/provider/validator identity, implementation digest, and trust group is authorized by the pinned registry. |
| Security | Any source leakage, path/URI leakage, executable parser markup, or unresolved security-critical finding blocks release. |

`human_reading` may render a clearly labelled `visual_only` region for review.
It never upgrades that region to semantic acceptance. `shadow` publishes
nothing to the public v1 surface.

An auditable failed strict IR may carry an unavailable/failed report with
indeterminate checks only when delivery is failed, ineligible, non-renderable,
and explicitly blocked by that report. This is semantic acceptance of the
failure record, not strict delivery acceptance.

Criticality describes the source region; severity describes the impact of a
wrong disposition. A frozen policy—not an individual validator—maps category,
criticality, and failure mode to severity. P0 covers security/data-loss or
systemic integrity failure; P1 covers silent corruption, omission, misbinding,
or wrong ordering of critical semantic content; P2 covers material non-critical
error; P3 is cosmetic. `SemanticIRValidator` rejects a report whose declared
severity disagrees with that policy, so a validator cannot downgrade a hard
gate.

## 5. Dataset splits and custody

### Development

Used for implementation, debugging, prompt/model changes, and regression tests.
The existing 23 reviewed papers belong here.

### Calibration

Used only to fit calibrated confidence and approve threshold policy. Parser,
validator, and reconciliation behavior is frozen before a calibration run.
Calibration observations never move into the sealed set.

### Sealed holdout

Held by a custodian outside the implementation workspace. Implementers receive
only the immutable evaluation manifest and final aggregate/adjudicated report,
not source items or per-item feedback before the acceptance decision.

Split construction MUST:

- group by source family, template, publisher, lineage, and near-duplicate
  content before allocation;
- keep a family in exactly one split;
- stratify document difficulty and the region taxonomy;
- pin document IDs, annotations, evaluator code, and dataset revision by digest;
- log every access to sealed source or annotations.

If any sealed item is viewed for debugging, it is permanently downgraded to
development. Its replacement must come from a different, previously unexposed
source family with equivalent taxonomy coverage; sampling another member of the
same family is not a repair.

## 6. Annotation protocol

- Two annotators work blind to producer identity and one another's labels.
- A third adjudicator resolves disagreements.
- Critical tables, formulas, figures, algorithms, and code require source-level
  region, content, and relation labels.
- Annotation guidelines, tooling version, agreement statistics, and adjudication
  reasons are digest-pinned with the dataset.
- Annotator overlap with parser training or prompt construction is disclosed as
  a correlation risk.

## 7. Provisional high-fidelity floors

The following are proposed starting floors, not active release claims. User
approval and a valid calibration run are required before enforcement.

Unless a row states otherwise, each reported slice needs at least 30
source-family-distinct documents and 200 applicable regions. A rare-category
slice may use 20 documents and 100 regions, but must be labelled low-support.
No pass/fail quality claim is made below minimum support.

Every score includes a 95% interval resampled at the source-family level. A
higher-is-better floor uses the lower bound; a lower-is-better ceiling uses the
upper bound. Point estimates alone cannot pass a release gate.

| Slice | Metric | Proposed floor |
| --- | --- | ---: |
| Layout regions | Macro-F1 | >= 0.98 |
| Text | Normalized edit distance | <= 0.02 |
| Reading order | Pairwise inversion rate | <= 0.02 |
| Table topology | GriTS Top | >= 0.97 |
| Table location | GriTS Loc | >= 0.95 |
| Table content | GriTS Con | >= 0.95 |
| Formula occurrence | Precision/recall F1 | >= 0.99 |
| Formula recognition and context | Exact/source-normalized F1 | >= 0.97 |
| Figure coverage | Recall | >= 0.99 |
| Figure-caption binding | Accuracy | >= 0.98 |
| Algorithm title and ordered steps | F1 | >= 0.99 |
| Algorithm critical ordering | Error count | 0 |
| Code structure and order | F1 | >= 0.99 |
| Required provenance | Completeness | 1.00 |
| Path/token/security leakage | Incident count | 0 |

Metrics are reported per document family and per applicable content category,
not only as a pooled average. Performance, latency, and cost SLOs remain a
separate open decision and cannot weaken semantic hard gates.

## 8. Selective acceptance

The primary metric unit is a source-bound region after independent oracle or
blind-human adjudication. For a sealed run:

- **selective risk** =
  incorrectly accepted `verified_semantic` regions of any criticality / all
  accepted `verified_semantic` regions;
- **critical P0/P1 risk** =
  accepted critical regions adjudicated as a P0/P1 error / all accepted critical
  `verified_semantic` regions;
- **strict delivery coverage** =
  strict documents accepted / all sealed documents submitted under the frozen
  strict policy, including timeouts, crashes, validation failures, and
  abstentions;
- **abstention rate** =
  documents rejected because a required region is non-verified / strict
  documents attempted.

A denominator of zero is `undefined`, never zero. It fails minimum support and
cannot produce a pass claim. Metrics are stratified by criticality, severity,
content category, and source family; pooled totals cannot hide a failing slice.

The proposed coverage floor is a one-sided 95% lower bound of `>= 0.90`; the hard
risk rule remains zero observed P0/P1 errors among accepted critical content.
Low coverage is reported as abstention, not hidden by removing difficult
documents from the denominator. Calibrated confidence may choose when to
abstain, but it cannot override a failing or indeterminate independent check.

This risk/coverage framing follows selective-classification practice: a system
may refuse uncertain examples, and both the residual accepted risk and coverage
must be reported. Family-cluster bootstrap intervals use a predeclared seed and
replicate count in the run manifest; exact intervals are used when a slice is too
small for a stable bootstrap, without bypassing minimum support.

## 9. Acceptance flow

1. Freeze parser adapters, reconciler, validators, renderers, policy, and dataset
   manifests.
2. Run development regression suites.
3. Run calibration to fit confidence only; do not edit code or models from its
   results.
4. Seal the threshold pack.
5. Have the independent custodian execute the sealed evaluation once.
6. Apply hard gates before provisional score floors.
7. Require blind human acceptance for every sealed critical region until its
   independent automated oracle has itself been validated.
8. Publish an immutable manifest containing failures and abstentions as well as
   accepted results.

Any code, model, prompt, policy, or threshold change after step 3 starts a new
versioned evaluation cycle.

## 10. External benchmark policy

External benchmarks provide taxonomy coverage and comparable metrics; they do
not replace project-specific sealed evidence.

- [OmniDocBench](https://github.com/opendatalab/OmniDocBench) contributes
  document-layout, span, geometry, and reading-order labels. Pin the repository
  commit and dataset revision.
- [GriTS](https://www.microsoft.com/en-us/research/publication/grits-grid-table-similarity-metric-for-table-structure-recognition/)
  contributes topology, location, and content metrics. Pin evaluator source and
  dependency lock.
- [olmOCR Bench](https://github.com/allenai/olmocr/blob/main/olmocr/bench/README.md)
  contributes machine-checkable text, order, table, and math facts. Pin its
  benchmark and data revisions.

Candidate parser benchmarks and validator benchmarks are reported separately.
PP-StructureV3 may be evaluated as a modular producer candidate, never counted as
an independent validator of its own output.

## 11. Reproducibility boundary

Every run manifest pins source IDs, split IDs, annotation revision, validator
implementation digests, adapter/model/config digests, policy ID, evaluator
version, environment lock, and random seeds where applicable.

The claim is manifest-level reproducibility. Byte-identical inference is not
promised for nondeterministic models; observed nondeterminism is measured and
reported.

## References

- [Docling architecture](https://docling-project.github.io/docling/concepts/architecture/)
- [OmniDocBench](https://github.com/opendatalab/OmniDocBench)
- [GriTS](https://www.microsoft.com/en-us/research/publication/grits-grid-table-similarity-metric-for-table-structure-recognition/)
- [olmOCR Bench](https://github.com/allenai/olmocr/blob/main/olmocr/bench/README.md)
- [Selective classification](https://arxiv.org/abs/1705.08500)
