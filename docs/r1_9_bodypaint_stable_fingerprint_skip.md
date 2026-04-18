# R1.9 BodyPaint Stable Fingerprint Skip

## Status

`pass`

## Purpose

Validate that repeated BodyPaint handoff can skip transfer even when raw export files are not byte-stable across runs.

## Run

- Profile: `remote-bodypaint-cassia-staged-to-linux`
- Source host: `windows-3070ti-operator-target`
- Target host: `linux-toyyard-01`
- Operation id: `op_remote-handoff-bodypaint-view_9485071d31`
- Transfer id: `xfer_bodypaint_7ef881ac9380`

## Outcome

- `transfer.skipped_existing_verified = true`
- `transfer.command_transport = null`
- `transfer.skip_reason = verified_target_packet_fingerprint_already_matches_source`
- `transfer.stable_fingerprint_match = true`
- `transfer.tree_hash_match = false`

## Interpretation

This is the intended v0 behavior:

- raw tree hash continues to reflect byte-level export differences
- stable packet fingerprint ignores approved volatile fields
- skip/retry identity now follows the stable packet fingerprint

## Stable Fingerprint

- Source stable fingerprint: `2266406714b4b35bfcec8ff0bafc76af7003dab03d7b8178ed9628e9731843f8`
- Target stable fingerprint: `2266406714b4b35bfcec8ff0bafc76af7003dab03d7b8178ed9628e9731843f8`
- Ignored volatile keys:
  - `generated_at_utc`

## Raw Tree Hash

- Source tree hash: `d4b6aa752ece4948634ce54a5672dec523f4158f0a6a785a3b9edcb22ecc39b8`
- Target tree hash: `e1ce156270db359e487d2d2d67ad948446fef347717d35235c0d5471ecede52b`

These raw hashes differ because the export is not byte-stable across runs. That no longer blocks skip if the stable packet fingerprint matches.
