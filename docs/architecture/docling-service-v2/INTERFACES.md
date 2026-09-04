# Docling Service v2 Interface Contracts

Scope: this document defines the v2 internal component contracts only.
No new public endpoints, no new API models, and no new production models are introduced.
Terminology follows the v2 design package: `README.md`, `ADR-0001-LAIL-IR.md`, and `schemas/lail-document-ir.schema.json`.

## Scope Anchors

- `DoclingDocument` is a parser output only.
- `LAIL-IR` is the only canonical intermediate representation for adjudication and delivery.
- `Docling Service v2` is orchestration around snapshot, adapter, reconcile, validate, adjudicate, render, publish, and shadow compare.
- No component may bypass these contracts by claiming parser-native output as authoritative.

## Shared Contract Requirements (MUST/SHOULD)

### 1) Invocation context (context-first)

Every component call MUST receive a control context with all of:

- `job_id` (string)
- `deadline_at_utc` (timestamp)
- `timeout_ms` (integer, hard execution cap)
- optional `cancel_token`
- optional `idempotency_key`
- bounded `resource_budget`:
  - `max_cpu_ms`
  - `max_memory_mb`
  - `max_disk_bytes`
  - `max_pages` (where page-aware)
  - `max_candidates`

`timeout_ms`, `deadline_at_utc`, `cancel_token`, and `idempotency_key` are invocation-level semantics only.
They MUST NOT be documented as globally reusable dedupe keys for all semantically equal input.
A component should assume identical documents can receive different idempotency keys unless upstream explicitly reuses the key.

### 2) Common failure semantics

- `ok`: boolean (formal machine consumable outcome)
- `status`: advisory string (for UI/diagnostic display)
- `retryable`: boolean
- `error_class`: one of `validation`, `resource`, `timeout`, `cancelled`, `vendor`, `storage`, `security`, `internal`
- `error_code`: stable internal code, never user data in logs
- `error_message`: short, non-sensitive text

Do not use `status` as the formal gate control.
All control logic MUST consume `ok`.

### 3) Provenance and reproducibility

LAIL-IR records provenance without a storage locator:

- `source.sha256` is the source-snapshot fingerprint.
- Each `run.producer_runs[]` record includes adapter and engine IDs/versions,
  `model_config_sha256`, a declared `correlation_group`, and the exact
  `source_snapshot_id`/`source_sha256` it consumed.
- Every candidate references exactly one `producer_run_id`.
- Every validation report includes validator ID/version,
  `implementation_sha256`, `independence_group`, source/snapshot/run identity,
  and the exact policy/trust-registry digests.

Component result envelopes may carry diagnostic timing and resource metadata but
MUST NOT redefine those IR identities. Published files are represented out of
band by immutable artifact records containing `artifact_id`, `issued_at_utc`,
`run_id`, SHA-256, and an opaque/root-confined lookup reference. LAIL-IR itself
never contains an artifact retrieval URI.

### 4) Security / path / network boundaries

- Storage paths are resolved only inside a dedicated, allow-listed runtime root.
- LAIL-IR and public records expose opaque handles or content digests, never
  filesystem paths, symlink targets, or retrieval URIs.
- No assumptions on external object stores or remote URI schemes.
- No parser/network calls may occur inside renderer stage.
- Outputs containing source-derived content MUST be bound to source-local immutable evidence IDs.

### 5) Raw vendor payload safety

- Raw parser payloads are treated as untrusted input.
- Raw payload storage MUST be immutable, quarantined, and size/TTL bounded.
- Access to raw payloads must require dedicated evidence access rights and cannot be used as the sole validation source.

## Component Contracts

## 1) SourceSnapshot

### Purpose

Create and validate the immutable source boundary consumed by all later stages.

### Input

- `job_id`
- an internal upload/staging handle
- invocation context (from shared contract)

### Output (MUST)

`SourceSnapshot` object includes:

- `source_snapshot_id`
- `job_id`
- `sha256`
- `media_type` (`application/pdf`)
- `page_count`
- `size_bytes`
- `created_at_utc`
- `read_only`
- `source_handle` (opaque runtime lookup key)
- `evidence_store_handle` (opaque runtime lookup key)
- `manifest_sha256`

Only the schema-defined source fields are serialized into LAIL-IR. Runtime
handles and audit fields remain in the access-controlled service envelope.

### Rules

- MUST be immutable after creation.
- MUST reject unsupported formats, unreadable inputs, truncated files, and path escape attempts.
- MUST write `source_snapshot_status` that records integrity checks and evidence-binding status.
- MUST be the **only** source accepted by `SourceEvidenceProvider`.

### Failure

- Missing / corrupt source: `ok=false, retryable=false, error_class=validation`
- Temporary IO or lease failure: `retryable=true`
- Timeout/cancel: `ok=false, retryable=true/false` as context dictates

## 2) ParserAdapter and CandidateBundle

### Purpose

Execute parser candidates over an immutable `SourceSnapshot` and emit immutable candidate records.
No adapter output is authoritative.

### Input

- `SourceSnapshot`
- adapter-specific config
- invocation context

### Output

`CandidateBundle` MUST include:

- `bundle_id`
- `producer_run_id`, referencing a source-bound producer-run record
- `source_snapshot_id`
- `candidates[]`, each matching the schema fields `candidate_id`, `native_id`,
  `page_id`, `bbox`, `role`, typed `payload`, `evidence_ids`, and four-channel
  `raw_confidence`; its optional producer-native body is represented only by the
  schema's bounded `vendor_payload` digest record
- formal result envelope (`ok`, retryability, and bounded vendor-exit metadata)

### Hard constraints

- Candidate generation MUST be read-only from the snapshot.
- `ParserAdapter` candidates are evidence only and **must not** become the unique truth source.
- Candidate payloads can be missing or partial by page; partial output is allowed
  only with `status=partial`, explicit missing scope, and `ok=false`.
- Parser candidates are immutable once produced.
- Every adapter runs with least privilege: read-only access to its snapshot,
  isolated bounded scratch space, no ambient credentials or host-filesystem
  access, and outbound network denied by default. A required local parser/model
  endpoint must be explicitly allow-listed in policy and recorded in the run
  manifest; general egress is forbidden.
- Parser-native HTML, Markdown, links, and filenames remain untrusted data and
  cannot cross directly into renderer templates or artifact references.

### Failure

- Parser runtime exception: `ok=false, status=failed, retryable=true|false`
- Resource exceeded: `error_class=resource`

## 3) SourceEvidenceProvider

### Purpose

Build source-bound evidence indices from immutable snapshots to support independent validation and reconciliation.

### Input

- `SourceSnapshot`
- policy-defined `CoveragePolicy` derived from the document class and delivery
  mode, never from parser candidates; the design fixture is the schema-valid
  [strict-machine-v1 policy](policies/strict-machine-v1.coverage-policy.json)
- invocation context

### Output

`SourceEvidenceBundle` includes:

- evidence index keyed by source page and geometry
- content-addressed source assets plus schema-valid `evidence[]` records
- a full-document coverage manifest with every page and required evidence class
  marked `observed`, `absent`, or `indeterminate`, including a source-backed
  page classification in `{content_present, blank, indeterminate}`; the manifest
  is serialized without loss as top-level `source_inventory[]`
- extraction provenance (`provider_id`, `provider_version`,
  `provider_implementation_sha256`, `independence_group`, `observed_at`)

### Rules

- MUST inspect only immutable `SourceSnapshot` content as source truth.
- MUST NOT receive candidate IDs, bboxes, roles, text, geometry, confidence, or
  parser-native payloads. Mutating or deleting any candidate therefore cannot
  change source observations or the coverage manifest.
- Under `strict_machine`, it inventories every source page and every evidence
  class required by policy. A parser cannot hide an omitted region by failing to
  propose its bbox.
- Every content-bearing inventory item is emitted as
  `coverage_role=region_observation` with independently observed role and
  criticality; page raster/support evidence uses `page_support`. A strict result
  requires every region observation to be accounted for by a region, plus a
  passing source-inventory check for every page. Each serialized inventory entry
  records the source-backed `page_content_state`, the policy-required evidence
  classes, and exactly one `observed|absent|indeterminate` result per class.
  `content_present` requires at least one region observation; `blank` requires
  page-support evidence and no content region; `indeterminate` can only block
  strict acceptance. A genuinely blank page is therefore an explicit attested
  result, not an inference from absent evidence.
- MUST NOT perform network calls.
- Evidence records MUST include source snapshot ID, source SHA-256, page ID,
  normalized source geometry, and provider trust domain.

### Failure

- evidence miss: emit explicit missing scope; the consuming validator records
  `indeterminate`, while the provider does not decide criticality or delivery
  eligibility
- evidence generation outage: `retryable=true` only when transient

## 4) Reconciler

### Purpose

Align candidate hypotheses into a provisional, adjudication-ready region graph.

### Input

- candidate graph fragments
- SourceEvidenceBundle
- policy parameters

### Output

`ReconcilerResult` with:

- `provisional_region_graph` (nodes + candidate references + split/merge lineage)
- `merge_plans`
- `split_plans`
- `lineage_graph` (predecessor and split/merge proposal links only)
- `graph_checksum`
- `integrity_checks`

### Rules

- MUST emit a **provisional** graph and **must not** select final outcomes.
- MUST preserve contradictory hypotheses with rational, structured reasons.
- Must enforce DAG constraints (no cycles in split/merge or parent/child proposals).
- MUST emit policy-scoped lineage with `operation` and `predecessor_ids`; it may
  annotate conflicts but cannot assign a final state.

### Failure

- unrecoverable conflict: `ok=false, retryable=false`
- partial graph recovery: `ok=true` only when conflicts and missing scope are
  explicit for downstream validation; the reconciler never pre-adjudicates them

## 5) Validator and ValidationReport

### Purpose

Run independent checks against `ReconcilerResult`.

### Input

- `SourceSnapshot` and `SourceEvidenceBundle`
- a `ValidationTargetSet` containing region ID, page ID, bbox, role, check class,
  canonical payload, selected candidate/evidence IDs, and a target digest. Raw
  confidence, vendor payloads, and parser markup are forbidden.
- the exact schema-valid CoveragePolicy artifact whose RFC 8785 digest equals
  `run.policy_sha256`

### Output

`ValidationReport` matches the schema and includes:

- `report_id`
- `run_id`, `source_snapshot_id`, `policy_sha256`, and
  `trust_registry_sha256`
- `validator_id`, `validator_version`, and `implementation_sha256`
- `independence_group` (trust domain/correlation key)
- `source_sha256` and `created_at`
- `execution_status` in `{completed, partial, unavailable, failed}`
- `checks[]`, each with `check_id`, `result` in
  `{pass, fail, indeterminate}`, `severity` in `{p0,p1,p2,p3}`, `category`,
  controlled `check_class`, `subject`, `target_sha256`, `evidence_ids`,
  `reason_codes`, and optional metric name/value

`target_sha256` is computed by the trusted launcher over UTF-8 RFC 8785 JSON
Canonicalization Scheme bytes. A region target is exactly
`{target_schema, source_sha256, region_id, page_id, bbox, role, payload,
selected_candidate_ids, evidence_ids}` with both ID arrays sorted. A page
inventory target is exactly `{target_schema, inventory}`, where `inventory` is
the complete schema-valid `source_inventory[]` entry without projection.
Required classes and class results use the frozen CoveragePolicy order; every
ID array is lexicographically sorted before hashing. The schema tags are
`lail.validation-target.region/1` and
`lail.validation-target.page-inventory/1`, respectively.

The surrounding component result envelope carries `ok` and advisory `status`;
those control fields are not embedded in LAIL-IR.

Source observations are sealed before a hypothesis reaches a validator. A
model-based observer cannot see hypothesis content. A deterministic comparator
may read an adjudication hypothesis only after the evidence bundle is immutable;
changing that hypothesis may change pass/fail, but MUST NOT regenerate or alter
the source observation, evidence IDs, or provider calls.

### Hard constraints

- **Pure**: the validator MUST NOT mutate `LAIL-IR`, `CandidateBundle`, or any upstream artifacts.
- `independence_group` describes trust correlation, not scheduling group.
- The frozen policy maps each region role/payload to all required check classes
  and maps each check class to allowed evidence kinds and coverage roles. A
  generic text or page-support pass cannot certify a table, formula, figure,
  algorithm, code region, or any other mismatched class.
- Policy resolution is mechanical: validate against
  [the policy schema](schemas/lail-coverage-policy.schema.json), require exactly
  one rule for every controlled role, payload kind, criticality floor, and check
  class, then take the ordered union of the role rule followed by the semantic
  payload-kind rule. Unknown, missing, or duplicate selectors and any local
  policy override fail closed.
- Validator output must never be used to rewrite source pages or recover text by final-string patching.

### Failure

- missing critical independent checks: `ok=false, status` must reflect blocking failure regardless of soft degradations.
- unavailable/failed validator execution emits a source-bound report with
  `execution_status=unavailable|failed` and one or more `indeterminate` checks.
  Those checks may have empty `evidence_ids`, carry an explicit reason code, and
  always block strict acceptance; evidence is never fabricated.

## 6) OutcomePolicy and Adjudicator

### Purpose

Convert validation results into final deliverability mode.

### Input

- provisional LAIL-IR from `ReconcilerResult`
- one or more `ValidationReport`
- policy thresholds
- required coverage constraints

### Output

`AdjudicationResult`:

- a resolution per region whose `final_state` is exactly
  `verified_semantic`, `visual_only`, or `unresolved`
- a document-level `delivery` record with mode, state, eligibility, counts, and
  blocking report references
- `decision_reason_codes`
- `review_required`
- `required_actions[]`
- formal component result envelope (`ok` plus advisory `status`)

### Rules

- MUST produce exactly one of:
  - `verified_semantic`
  - `visual_only`
  - `unresolved`
- `visual_only` and `unresolved` are non-verified states. Under
  `strict_machine`, any critical region in either state blocks delivery; there is
  no policy override inside a strict run.
- `outcome_policy` is separate from validation policy and is explicit in context.

### Failure

- inconsistent report inputs: `ok=false`, advisory `status=unresolved`, and an
  explicit blocking reason

## 7) SemanticIRValidator

### Purpose

Enforce cross-object invariants that JSON Schema cannot express. This gate runs
after adjudication and before any renderer or publisher. Schema-valid is
necessary but never sufficient for delivery.

### Input

- complete adjudicated LAIL-IR
- immutable trust-domain registry and semantic policy
- invocation context

### Mandatory checks

- `document_id == "doc-" + source.sha256`; every evidence, source-inventory
  entry, asset, and validation report carries the same source SHA-256 and source
  snapshot where applicable.
- Every producer run carries the same `source_snapshot_id` and `source_sha256`
  as the document source.
- `run.policy_sha256` is the lowercase SHA-256 of the complete schema-valid
  CoveragePolicy value after UTF-8 RFC 8785 canonicalization, and its
  `policy_id`/`delivery_mode` match `run.policy_id`/`delivery.mode`.
  `run.trust_registry_sha256` resolves to the immutable registry artifact
  authorized for the run; `delivery.policy_id` matches `run.policy_id`. Every
  inventory entry and report repeats the run/snapshot/policy/registry identity
  required by its schema; replay from another run, policy, or registry epoch is
  rejected.
- IDs are unique by namespace, regardless of whether whole objects differ.
- Reference closure is exhaustive across all declared reference-bearing fields:
  page regions; asset backing evidence; evidence page/asset; inventory page and
  evidence; candidate producer/page/evidence; region page/candidate/evidence,
  lineage predecessors, and all resolution references; relation endpoints and
  evidence/reports; validation subjects/evidence; delivery blocking reports;
  and nested figure captions/assets and algorithm-step evidence. Identifiers
  such as `run_id`, `check_id`, or `native_id` are identities, not implicit
  references.
- Page numbers are contiguous and 1-based; page membership, page count, bbox
  ordering/bounds, asset content addresses, delivery counts, and table-cell
  bounds agree with their referenced objects. Every asset is source/snapshot
  bound and backed by same-document evidence before a payload may render it.
- Self-relations are rejected. Every relation type except non-causal
  `references` is acyclic, as is the lineage predecessor graph. Lineage requires
  `original=0`, `split=1`, `remap=1`, and `merge>=2` predecessors; all
  predecessors resolve, differ from the successor, and satisfy policy scope.
- Exactly one `source_inventory` entry exists per page. Its source/policy and
  provider attestation match the run; its required class set exactly equals the
  digest-pinned CoveragePolicy; and it contains exactly one result per class.
- Every observed class points only to compatible `region_observation` evidence.
  Each observation maps to exactly one same-page region unless an explicit,
  policy-valid split/merge relation accounts for cardinality; geometry overlap
  and role/class mapping must meet the frozen policy. Dangling observations,
  giant catch-all regions, duplicate reuse, and missing required classes fail.
- If the IR claims strict success, every page has a passing independent
  `source_inventory` check whose `target_sha256` matches that page's complete
  inventory entry. A content-present page has at least one observed class and
  mapped region; a blank page has only `absent` class results, page-support
  evidence, and no content region. Any indeterminate class, page-support-only
  content-present page, or missing observation blocks strict success.
- Region role and criticality equal the strongest source-observation and frozen
  policy classification. Missing/conflicting classification fails closed;
  adjudication cannot downgrade a critical table, formula, figure, algorithm,
  code, or policy-designated region to evade strict gating.
- Every `verified_semantic` region has source-bound evidence and at least one
  passing check for every role/payload-required `check_class` in the frozen
  policy. Each subject targets that region (or an explicitly allowed enclosing
  relation), the report is bound to the same source/run/policy/registry, and
  its evidence IDs resolve to the same source/page geometry. The check's
  `target_sha256` must equal the validator's recomputation of the exact final
  region target; a stale report cannot certify a changed payload, role, bbox, or
  selected candidate/evidence set.
- Each passing check uses only evidence kinds and coverage roles allowed for its
  `check_class`; a generic text, provenance, or page-support observation cannot
  substitute for table topology, formula, figure, algorithm, code, or other
  class-specific evidence.
- A passing report can certify selected candidates only when the frozen
  trust-domain registry establishes independence between its
  `independence_group` and every selected producer's `correlation_group`.
  Every evidence record supporting that report must likewise be independent of
  those producers. Different strings alone do not prove independence; the
  registry evaluates the complete provider/validator/producer dependency graph.
- The trusted launcher computes implementation/config digests; components may
  not self-assert them. The registry must attest the exact producer adapter,
  engine/model config, evidence-provider ID/version/implementation digest, and
  validator ID/version/implementation digest with their allowed trust groups.
  Unknown, revoked, stale, or mismatched entries fail closed.
- Strict eligibility, state, renderability, blocking reasons/report references,
  and critical-region final states are mutually consistent. Eligible strict
  success is always `state=succeeded`, `renderable=true`, with empty blocking
  arrays and zero non-verified critical regions.
- Table row/column spans and all ordered payloads are internally valid. Algorithm
  step IDs are unique, ordinals are exactly contiguous and zero-based, and every
  step-level evidence reference resolves and is source/geometry compatible.
- A validator/provider outage may remain a valid, auditable IR only when its
  report/check is explicit, strict delivery is failed and ineligible, rendering
  is disabled, and the blocking reason/report references close. This accepts the
  failure record, never the delivery.

### Output and gate capability

The validator always returns an IR-validity result and canonical IR SHA-256. It
issues a short-lived, locally authenticated `SemanticGateAttestation` only for a
valid renderable outcome. The capability binds `attestation_id`, IR/source
SHA-256, run ID, policy/trust-registry digests, admitted mode, scheduler
authorization epoch, `render_allowed`, allowed surface class, expiry, and gate
key ID. A MAC or signature from the isolated gate authority makes these fields
non-forgeable by parser, renderer, or publisher processes.

A coherent failed strict audit record may receive an IR-valid result, but it
receives no render/publish capability. An invariant-invalid IR likewise receives
no capability.

### Failure

Any invariant mismatch is `ok=false, retryable=false, error_class=validation`.
A coherent failed/ineligible outcome is not an invariant mismatch; it may be
retained for bounded internal diagnosis. Neither kind invokes a renderer or
publisher.

## 8) Renderer

### Purpose

Render final reading surfaces from adjudicated LAIL-IR only.

### Input

- adjudicated `LAIL-IR`
- a successful `SemanticGateAttestation` bound to the canonical IR SHA-256,
  run/policy/registry digests, authorization epoch, and allowed render mode
- renderer profile
- invocation context

### Output

- `document.html`
- `document.md`
- `document.json`
- renderer digest and escaping manifest

### Hard constraints

- Renderer MUST consume only adjudicated regions.
- Renderer recomputes the canonical IR SHA-256 and verifies the gate attestation
  with the local gate authority. Missing, expired, failed, wrong-mode, or
  mismatched attestations are rejected before reading payloads.
- MUST read only prevalidated evidence IDs and deterministic templates.
- MUST NOT call parser, model APIs, or network.
- **No final-string repair**: renderer cannot post-hoc patch semantic errors via regex/string concatenation and cannot use parser-native artifacts as truth.
- Must be deterministic for same inputs.
- HTML uses context-sensitive text/attribute escaping, rejects event-handler
  attributes, and applies a fixed safe-element policy. Markdown raw HTML is
  disabled or escaped. Links and embedded assets accept only policy-approved
  opaque IDs/relative references; `javascript:`, `file:`, remote HTTP(S), and
  unapproved `data:` schemes are rejected.
- The renderer emits a content-security and escaping manifest suitable for the
  publication surface; it never treats source or parser strings as templates.

### Failure

- renderer bug, malformed template, or output write failure: `ok=false`

## 9) ArtifactPublisher

### Purpose

Publish immutable final artifacts and delivery metadata.

### Input

- renderer outputs
- `adjudication_result`
- the verified `SemanticGateAttestation` plus renderer manifest, both bound to
  the same canonical IR SHA-256
- an authenticated scheduler `PublishAuthorization` carrying `run_id`, admitted
  mode, allowed publication surface/root, authorization epoch, expiry, expected
  prior generation, and monotonic per-job sequence
- retention policy
- output manifest policy

### Output

- published artifact records with `artifact_id`, opaque or root-confined relative
  reference, checksum, `visibility`, and `ttl_seconds`
- job manifest entry updates

### Hard constraints

- Publish paths must remain under the component's configured root and use bounded names.
- The publisher verifies the gate authority and scheduler authorization through
  separate local trust roots, then requires run, mode, IR digest, epoch, surface,
  renderer digest, and expiry to agree. Direct invocation, replay under another
  run/policy/epoch, or a missing/mismatched attestation fails closed.
- Artifact records are immutable once written; no absolute or remote URI is
  serialized.
- The authenticated scheduler authorization—not mutable `delivery.mode`—selects
  the only permitted surface/root. The IR mode must match it. A shadow
  authorization can name only the isolated internal shadow-report root and can
  never write or swap a public/v1-visible artifact, pointer, manifest, state, or
  webhook, even if the IR mode is tampered.
- Must enforce visibility policy (`readable`, `restricted`, or `internal`) without leaking absolute paths.
- A v1 compatibility publisher, not the renderer, MUST atomically emit the exact
  five required files: `document.html`, `document.md`, `document.json`,
  `metadata.json`, and `status.json`. Its formal `status.json.ok` must be `true`
  for success, while metadata retains original/visual SHA-256 bindings.
- Publication uses generation fencing: write all files and a checksum manifest
  to a new generation directory, flush/verify them, write a commit marker, then
  atomically swap one active-generation pointer. Existing generations are never
  mutated in place.
- Each publish attempt uses the scheduler authorization epoch/fencing token. A
  late v2 completion after kill-switch or rollback cannot swap the active
  pointer, update v1 state, or enqueue webhooks.
- The active pointer swap is a compare-and-swap over expected prior generation,
  authorization epoch, and per-job monotonic publication sequence. Concurrent
  same-epoch publishers cannot both commit; the loser is marked superseded and
  cannot retry without re-reading state. Idempotent replay of an already
  committed generation returns its existing artifact record.
- Restart recovery ignores uncommitted generations, verifies the active
  generation before serving, and cleans abandoned staging only after its lease
  expires. Cleanup never removes the active or rollback generation.

### Failure

- storage quota exceeded, fs errors, or manifest mismatch: `ok=false, error_class=storage/resource`

## 10) ShadowRunner

### Purpose

Run isolated side-by-side comparisons between v1 and v2 behavior.

### Input

- the same immutable source snapshot used by the v1 execution
- v1 job ID plus read-only output/state snapshot
- v2 run configuration
- scope filter
- invocation context

### Output

- `shadow_report_id`
- `v1_bytes_profile`
- `v2_result_profile`
- `status_delta`
- `state_delta`
- `webhook_delta`
- `error_diff`

### Hard constraints

- MUST keep v1 `/v1/jobs` behavior, outputs, state transitions, and webhook payloads byte-for-byte unchanged.
- Comparison is side-channel only; does not alter v1 state/manifest/records.
- Must run in isolated quota/TTL/root budgets and independent download lease namespace.
- Must support immediate kill-switch and rollback.
- Kill-switch revokes the current v2 publication epoch before stopping new
  admission. In-flight work may finish only as non-publishable diagnostic data;
  it cannot race a rollback pointer or v1 webhook/state transition.

## Contract Test Requirements

All v2 components MUST pass the following tests before stage promotion:

1. **Schema fixtures**
   - validate a successful strict example and a failed-strict example; reject an
     unknown producer status, URI/path field, missing validation references, and
     a critical non-verified region labelled strict-successful; represent a true
     validator outage as an unavailable report with indeterminate checks

2. **SourceSnapshot immutability**
   - mutate candidate data and verify snapshot hash/manifest are unaffected

3. **Parser sandbox and non-authority**
   - deny host-path, credential, and unapproved egress access; inject conflicting
     parser output and prove it cannot directly define delivery

4. **Source evidence isolation**
   - mutate/delete candidate text, geometry, JSON, and complete proposals; prove
     full-page source observations and coverage remain unchanged and every
     source inventory item is still surfaced
   - remove one policy-required evidence class, use page-support-only evidence
     on a content-present page, duplicate one observation across regions, and
     use an overbroad catch-all bbox; each attempted strict success must fail

5. **Reconciler provisionality**
   - require explicit conflict/missing-scope annotations and no resolution or
     adjudication field

6. **Semantic source/reference integrity**
   - reject mismatched snapshot/source/policy digests, duplicate IDs with
     different bodies, stale report run/policy/registry identity,
     unknown/wrong-type references at every nesting depth, invalid page
     membership/bboxes, cross-source or unbacked assets, selected candidates
     outside a region, bad table bounds, non-contiguous algorithm ordinals, and
     incorrect delivery counts

7. **Graph integrity**
   - reject relation self-loops, cycles in every causal/structural relation type,
     and bad lineage cardinality, missing predecessors, or lineage cycles;
     `references` is the only relation type allowed to be cyclic

8. **Validator purity, applicability, and independence**
   - prove validation does not mutate inputs; reject empty/unrelated evidence,
     a report whose subject does not cover the region, another source's report,
     a validator/evidence provider correlated with a selected producer, and any
     unattested/revoked implementation according to the frozen trust registry;
     after sealing source observations, mutate the hypothesis and prove no
     provider reruns or evidence bytes/IDs change (only deterministic comparison
     results may change)
   - mutate the final payload after a passing report and prove target-digest
     mismatch; use a text/page-support check for a table/formula/figure and prove
     the frozen role-to-check/evidence-class matrix rejects it

9. **Adjudicator cardinality and strict fail-closed behavior**
   - allow only `verified_semantic | visual_only | unresolved`; prove either
     non-verified state on a critical region forces `strict_machine` to
     `state=failed`, `renderable=false`, and nonempty blocking reasons/reports
   - delete all proposals or downgrade source-designated criticality and prove
     source-inventory/criticality policy rejects vacuous strict success

10. **Renderer isolation and injection safety**
    - enforce no network/parser calls or final-string repair; test HTML/Markdown
      context escaping and reject script/event payloads, unsafe URI schemes,
      path traversal, and parser-supplied raw markup

11. **Atomic publication and recovery**
    - crash before/after every write, flush, commit-marker, and pointer-swap
      phase; prove readers see one complete generation and restart cleanup cannot
      delete active/rollback generations; race two same-epoch valid publishers
      and prove compare-and-swap commits exactly one monotonic generation
    - invoke renderer/publisher without a gate attestation and replay an
     attestation against a changed IR, run, policy, or epoch; all attempts fail

12. **Kill-switch race and shadow invariance**
    - revoke the publication epoch during in-flight work and prove late v2
      completion cannot alter v1 API bytes, state, manifests, files, or webhook
      cadence; hold a shadow scheduler authorization while mutating the IR mode,
      then call the generic publisher and prove mode mismatch is rejected and no
      public/v1-visible surface can be selected

13. **v1 compatibility and `status`/`ok` trap**
    - verify the exact five files, formal `status.json.ok=true`, metadata/source
      identity, archive exclusion, leases, states, idempotency, legacy ten-field
      mirror, and webhook contract

14. **No path/URI/secret leakage**
    - scan IR, reports, logs, manifests, artifact records, rendered surfaces, and
      archives for filesystem paths, retrieval URIs, credentials, and raw tokens
