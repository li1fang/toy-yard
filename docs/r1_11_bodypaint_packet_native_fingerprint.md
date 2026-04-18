# R1.11 BodyPaint Packet-Native Fingerprint

## Goal

Move BodyPaint stable packet identity out of Remote Operator-only recomputation and into packet-native export metadata.

## What Changed

BodyPaint export now writes:

- `summary/bodypaint_packet_identity.json`

That sidecar contains:

- stable packet fingerprint
- ignored volatile keys
- excluded files
- per-file logical fingerprints

Remote Operator now prefers this packet-native identity during:

- existing-target verified skip
- staged transfer verification
- final promoted target verification

If the sidecar is missing, Remote Operator falls back to the previous local recomputation path.

## Why This Matters

This makes logical packet identity part of the packet contract instead of an operator-only convenience.

Practical benefits:

- repeated exports can be recognized as the same logical packet even when `generated_at_utc` changes
- operators and future consumers can share one logical identity source
- older packets remain compatible through fallback recomputation

## Current Scope

Implemented for:

- `bodypaint-view`

Not yet generalized to:

- other export-directory lanes

## Current Volatile Keys

Ignored in the stable BodyPaint packet fingerprint:

- `generated_at_utc`

## Real Validation

Validated with a real remote handoff:

- profile: `remote-bodypaint-cassia-r1-11-packet-identity`
- package: `pkg_mingchao-sample-4f64c1a1-character-d214086d_7d8f416608`
- operation: `op_remote-handoff-bodypaint-view_4b966c168f`

Observed result:

- handoff status: `pass`
- source fingerprint source: `packet_identity`
- selected transport: `scp`
- stable fingerprint match: `true`
- tree hash match: `true`
