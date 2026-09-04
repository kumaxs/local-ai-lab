# LAIL-IR design fixtures

These are design-contract fixtures, not production evidence.

- `minimal-verified.document-ir.json` is the positive shape and semantic
  baseline. Its source/model hashes, versions, timestamps, and content are
  illustrative. Its policy digest is real and binds the exact
  [`strict-machine-v1` CoveragePolicy](../policies/strict-machine-v1.coverage-policy.json).
- `validation-cases.json` declares RFC 6902-style mutations over that baseline.
  Each case states whether JSON Schema or the mandatory semantic validator must
  accept or reject it.

An implementation test runner must deep-copy the baseline, apply every patch in
order, run the named phase, and assert the exact expected disposition and rule.
Schema acceptance never skips semantic validation. The `failed-strict-valid`
case is intentionally accepted: a failed strict attempt must remain
representable and auditable, while the `strict-ineligible-green` case proves it
cannot look successful or renderable.

The semantic cases cover invariants that Draft 2020-12 cannot express directly:
digest equality, unique identifiers, exhaustive reference closure, graph
acyclicity, target/report applicability, per-class source inventory,
role/check/evidence compatibility, trust-domain independence, source-bound
assets, geometry, ordered algorithm steps, and aggregate counts.
Coverage-policy digest, role and payload applicability, evidence-kind and
coverage-role compatibility, and run/policy/registry replay each have an
explicit negative mutation.
