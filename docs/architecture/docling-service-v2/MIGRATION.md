# Docling Service v2 Migration Plan (v1 contract-preserving)

This plan preserves the existing public `/v1/jobs` behavior for all clients and
performs staged adoption through isolated v2 execution only.
It does not add endpoints, request models, or new production parser classes in this document.

## 0. Invariants inherited from v1 canonical behavior

- Public API path remains `/v1/jobs`; contract remains OpenAPI v1.x.
- Public job states are exactly: `queued`, `running`, `succeeded`, `failed`, `interrupted`.
- SQLite remains authoritative state store; legacy rollback representation remains a strict ten-field `legacy_record`.
- Required output files remain:
  - `document.html`
  - `document.md`
  - `document.json`
  - `metadata.json`
  - `status.json`
- Archive endpoint must preserve v1 behavior, including omission of `source.pdf` from ZIP contents.
- Webhook payload schema and event cadence remain unchanged in v1-visible paths.
- Download leases, temp/queue protections, and staging publish mechanics remain aligned with existing service logic.
- Existing v1 idempotency behavior must be consumed as-is; v2 cannot redefine request semantics.
- Existing implementation TTL defaults are inherited during compatibility
  stages; they are not permanent v2 architecture constants. Any v2 extension
  uses independent namespaces/budgets:
  - input TTL: `86400`
  - success output TTL: `604800`
  - failed/interrupted output TTL: `172800`
  - job TTL: `2592000`
  - webhook delivery TTL: `604800`
  - staging and temp TTL: `3600`
  - idempotency TTL: `86400`
  - download lease TTL: `300`

Migration-specific open decisions include:
- `source.pdf` visibility
- `source.pdf` retention window
- v2-visible `outputs` and `manifest` visibility/retention in non-shadow stages
Current status: **deferred decision**.

## Stage 0 — Design lock and acceptance matrix

### Mandatory completion

1. Align all v2 contracts to `INTERFACES.md`.
2. Confirm canonical assumptions against implementation evidence in:
   - `services/docling-service/docling_service/contract.py`
   - `services/docling-service/docling_service/api_models.py`
   - `services/docling-service/docling_service/persistence.py`
   - `services/docling-service/docling_service/release.py`
3. Publish and freeze the exact `status` vs `ok` trap policy:
   - `ok` is formal control.
   - `status` is user-facing.
4. Publish the region split/merge lineage contract that the `Reconciler`,
   `Adjudicator`, and `ArtifactPublisher` must preserve.
5. Validate migration playbook security review: adapter sandbox/egress, path and
   URI boundaries, renderer injection safety, secret handling, and temp cleanup.
6. Specify generation-based atomic publication, authorization epochs, restart
   recovery, and cleanup leases before any shadow writer exists.

### Exit criteria

- No unresolved factual mismatch with canonical v1 behavior.
- Contract tests above pass for offline fixtures (reader + schema + retention checks).
- Kill-switch, crash recovery, stale-generation cleanup, and rollback runbooks
  are written and rehearse-able.

## Stage 1 — Offline Bridge

Goal: run v2 pipeline only as a read-only shadow path.

Actions:
- Snapshot the exact immutable PDF input used by the v1 run, then execute v2
  against that same source identity in an isolated v2 root.
- An optional `LegacyOutputAdapter` may import existing v1 outputs as explicitly
  untrusted candidate records for comparison; it cannot use those outputs as
  source truth or validation evidence.
- Do not mutate v1 `state` files, manifest, or webhook schedules.
- Record full invocation context (`timeout_ms`, `cancel`, `idempotency_key`, and resource budget).
- Enforce bounded `SourceSnapshot` and raw vendor payload quarantine.
- Persist schema-aligned source SHA-256, adapter/engine IDs and versions,
  `model_config_sha256`, producer `correlation_group`, and validator
  implementation/independence identities plus policy/trust-registry digests for
  reproducibility.

Exit criteria:
- LAIL-IR passes schema and semantic validation, including reference, digest,
  bbox, graph, lineage, and delivery-count invariants.
- The compatibility publisher atomically produces the exact five required v1
  files and passes the existing v1 contract fixtures.
- `status` vs `ok` mapping is explicitly produced and reviewed. In particular,
  the existing standalone writer's display value `status="success"` is not a
  formal success record; the compatibility layer MUST emit `status.json.ok=true`
  and metadata binding the original/visual SHA-256 values to the immutable
  `source.pdf` identity.
- No writes affect v1 payloads, manifests, or webhook payloads.

## Stage 2 — Offline Calibration (no production traffic)

Goal: calibrate confidence and policy thresholds from offline samples only.

Actions:
- Use held-out calibration datasets only.
- Fit threshold policy and policy exceptions in a read-only environment.
- Collect split/merge lineage, unresolved-region counts, and `status`/`ok` trap outcomes.
- Produce calibration artifacts with pinned validator/model/data fingerprints.

Constraints:
- No code or model replacement is performed in this stage.
- No live traffic routing occurs.
- Only the reviewed provisional floors may be evaluated; a run cannot invent
  easier thresholds or SLOs after seeing calibration results.

Exit criteria:
- Calibration artifacts are reproducible and peer-reviewed.
- No critical contradiction in `ok` vs `status` control logic.
- Stage 3 readiness review approved.

## Stage 3 — Isolated Production Shadow

Goal: run v2 in real production ingress with no user-visible behavior change.

Mandatory requirements:
- `v2` MUST run in isolated execution boundaries:
  - separate root
  - separate output/input namespaces
  - separate cache and lease namespace
  - separate quotas/limits and TTL tracking
- `ShadowRunner` MUST keep v1 behavior byte-for-byte unchanged for:
  - `/v1/jobs` output payloads
  - v1 state transitions
  - v1 webhook event payload schema and cadence
- v2 must read the same immutable source snapshot as v1 and may compare read-only
  v1 outputs, but must not write back to v1 storage.
- Shadow publication uses a separate generation root and authorization epoch;
  it has no active pointer on any v1-visible surface. The scheduler mints an
  authenticated shadow-only publish authorization, and renderer/publisher both
  require the matching semantic-gate attestation; changing IR `delivery.mode`
  cannot expand that authorization.
- Kill-switch is mandatory and immediate: revoke the v2 epoch before stopping
  admission so late completions cannot publish, change state, or enqueue
  webhooks.
- Rollback means atomically selecting the last contract-valid v1 generation, stopping
  v2 consumers, and recording the authorization/audit event. Mixed-generation
  files are never served.

Exit criteria:
- Multiple release-day shadow windows complete with no delta in v1 contract surfaces.
- No cross-namespace resource contamination.
- Crash-at-each-publication-phase, in-flight kill-switch race, restart recovery,
  two-publisher compare-and-swap, direct gate-bypass denial, shadow-mode/context
  mismatch denial, public-surface denial, and rollback drills execute
  successfully.

## Stage 4 — Opt-in Human

Goal: include v2 adjudication only for explicit opt-in jobs.

Actions:
- Add opt-in gating in internal scheduling only (no public endpoint change).
- Expose v2 output artifacts only in internal review channels.
- Keep v1 output, v1 state, and v1 webhook behavior unchanged.
- `visual_only` and `unresolved` outcomes do not auto-promote to machine acceptance.

Rules:
- Any `ok=false` requires manual policy review before a v2 artifact leaves the
  internal review channel.
- `status` is display-only in this stage; automation uses `ok`.
- `source.pdf` visibility and retention remain **deferred decision** until explicit compliance approval.

Exit criteria:
- Manual review path stable with bounded queueing and clear escalation.
- No regression in v1 observable contract.

## Stage 5 — Strict Machine

Goal: v2 controls machine delivery while v1 remains rollback mirror, not v2-verified path.

Actions:
- Promote v2 adjudication and publisher for default machine acceptance only
  where `ok=true` and all critical regions are `verified_semantic`.
- Keep v1 path writable only as rollback mirror.
- Continue to preserve the v1 API, state, manifest, webhook, and exact five-file
  contract; semantic artifact bytes may differ because v2 is the producer.
- Maintain immediate rollback via kill-switch and namespace deactivation. A
  rollback changes the active generation path; it never silently substitutes v1
  output into a v2 result or labels v1 output as v2-verified.
- Publish through a verified generation manifest and one atomic active pointer;
  fencing tokens prevent pre-rollback workers from committing afterward.

Non-obvious rule:
- Stage 5 must not describe v1 fallback as "v2 verified."

Exit criteria:
- `status` and `ok` handling are formally codified and audited.
- `source.pdf` visibility/retention decisions are closed or explicitly tracked as accepted exceptions.
- v1 rollback, stale-worker fencing, partial-generation recovery, and webhook
  non-duplication simulations pass.

## Hard transition rules (apply to all stages)

- No external endpoint additions.
- No new public model classes in the migration envelope.
- `Reconciler` outputs remain provisional; only `Adjudicator` decides final states:
  - `verified_semantic`
  - `visual_only`
  - `unresolved`
- `independence_group` is trust-domain metadata only; do not use it as scheduling key.
- Raw vendor payloads remain bounded and quarantined, never treated as direct source truth.

## Cross-stage artifacts

- Stage 0/1: offline bridge report, legacy invariant checklist, migration exception log.
- Stage 2: calibration manifest and threshold pack.
- Stage 3/4/5: kill-switch drill log, rollback log, status-vs-ok audit log, shadow diff log.
- All artifacts keep immutable fingerprints and deterministic generation metadata.
