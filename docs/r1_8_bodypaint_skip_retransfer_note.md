# R1.8 BodyPaint Skip-Retransfer Note

## Observation

The operator now has a code path for:

- detect verified existing final target
- skip `scp`
- return a verified no-op handoff result

## What Happened In Real Re-Run

Re-running:

- profile: `remote-bodypaint-cassia-staged-to-linux`
- source host: `windows-3070ti-operator-target`
- target host: `linux-toyyard-01`

did **not** trigger `skipped_existing_verified`.

Instead, the operator performed a fresh staged transfer and promotion.

## Why

The exported BodyPaint packet is not yet byte-stable across runs.

At least these files currently carry volatile values:

- `bodypaint_input_manifest.json`
- `bodypaint_packet_check.json`
- `bodypaint_packet_registry.json`
- `bodypaint_suite_summary.json`
- `communication_signal.json`

Each of them includes:

- `generated_at_utc`

That makes the raw source tree hash differ across runs, even when the logical packet content is effectively the same.

## Conclusion

`skip_existing_verified` is implemented at the Remote Operator layer, but practical skip behavior still depends on packet determinism.

The next refinement should be:

- introduce a stable packet fingerprint for skip/resume identity
- keep raw tree hash for transport integrity
