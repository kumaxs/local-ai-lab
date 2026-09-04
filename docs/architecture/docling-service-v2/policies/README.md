# Coverage policy fixtures

These files are immutable, design-contract policy inputs rather than production
configuration.

- `strict-machine-v1.coverage-policy.json` is the complete role/payload to
  required-check mapping for the positive fixture. It also freezes inventory
  classes, role criticality floors, forbidden verified roles, and each check
  class's allowed evidence kinds, coverage roles, and subject scopes.
- The instance validates against
  [`../schemas/lail-coverage-policy.schema.json`](../schemas/lail-coverage-policy.schema.json).

The trusted launcher validates the policy, verifies that every controlled enum
has exactly one applicable rule, canonicalizes the complete JSON value with RFC
8785 JCS, and stores the lowercase SHA-256 digest as `run.policy_sha256`. The
same digest must be repeated by source inventory entries and validation reports.
Any local override, missing/duplicate rule, unknown selector, or digest mismatch
fails before adjudication.

For this exact fixture value, that digest is
`c8e2c8015a883cb94d08d853056aa7f8de31082ea8bbea38ce62d66f879fdc9d`.
