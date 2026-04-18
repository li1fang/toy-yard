# Toy-Yard BodyPaint Remote Handoff Runbook v0

## Goal

Run a controlled remote handoff for a directory-style export packet, using the BodyPaint lane as the first wrapper.

This is the first step beyond `audio-index`:

- source node exports a BodyPaint publish profile
- operator triggers peer-to-peer transfer
- target node receives the export directory under a declared transfer profile
- target verification checks the packet shape, not consumer execution

## Why BodyPaint

BodyPaint is a good first export-directory lane because:

- the packet contract is already explicit
- the export returns a stable `export_root`
- the lane already has a confirmed consumer record
- the next useful operation is directory handoff, not database import

## Command Shape

```text
toyyard remote handoff bodypaint-view \
  --source-host <host> \
  --target-host <host> \
  --profile <profile> \
  [--sample <sample_ref> | --package <package_id> ...] \
  [--aiue-pmx-profile <profile>] \
  --target-transfer-profile toy_yard_bodypaint_packets
```

## What It Does

1. runs remote `toyyard export bodypaint-view` on the source node
2. reads `export_root` from the source JSON result
3. optionally creates the target drop directory if the transfer profile allows it
4. creates a target staging directory:
   - `<target_profile_dir>/.incoming/<operation_id>/`
5. runs peer-to-peer `scp -r` from source node to target staging
6. computes a source directory tree manifest before transfer
7. verifies the staged export directory contains:
   - `summary/bodypaint_suite_summary.json`
   - `summary/bodypaint_packet_registry.json`
   - `summary/bodypaint_packet_check.json`
   - `summary/communication_signal.json`
8. computes a staged directory tree manifest after transfer
9. compares source and staged tree hashes
10. promotes the staged directory to:
    - `<target_profile_dir>/<profile>/`
11. verifies the final target export directory shape and tree hash
12. if the final target directory already exists and already matches the source stable packet fingerprint, the operator skips transfer and returns a verified no-op result

## Important Boundary

This handoff does **not** mean:

- BodyPaint execution happened
- results were imported
- the target became the canonical source of the model

It only means:

- the packet was exported correctly
- the packet was transferred correctly
- the receiving side now has a consumable export directory

## Current Acceptance

For v0, target verification is green when:

- all four summary files exist
- the registry contains at least one packet
- the source export directory exists before transfer
- the staged export directory tree hash matches the source tree hash
- the final target export directory tree hash matches the source tree hash
- staging promotion succeeds

Consumer execution still belongs to the BodyPaint lane itself.

## Transfer Integrity

The BodyPaint handoff now records both sides of the directory transfer:

- `source_tree_manifest.payload.files[*].sha256`
- `source_tree_manifest.payload.tree_hash`
- `staged_tree_manifest.payload.files[*].sha256`
- `staged_tree_manifest.payload.tree_hash`
- `target_tree_manifest.payload.files[*].sha256`
- `target_tree_manifest.payload.tree_hash`
- `transfer.staged_tree_hash_match`
- `transfer.tree_hash_match`
- `promote_result.payload.promoted`
- `transfer.selected_transport`
- `transfer.resume_supported`
- `transfer.selection_reason`

This is still not a resumable transfer protocol. It is the first integrity and isolation layer:

- if `scp -r` returns success but the staged tree differs, the handoff fails at `staging_hash_verify`
- if staging is valid but promotion fails, the handoff fails at `target_promote`
- if the final target tree differs after promotion, the handoff fails at `target_hash_verify`

The target consumer-visible directory is only updated after staged shape and hash checks pass.

Repeated runs are idempotent at the transfer level: when the final target directory already exists, passes packet-shape verification, and has the same stable packet fingerprint as the source, the operator returns `transfer.skipped_existing_verified = true` and does not re-run `scp`.

For BodyPaint v0, the stable packet fingerprint ignores approved volatile JSON keys:

- `generated_at_utc`

When the source and target are both `bash` hosts and both expose `rsync`, the operator may use resumable `rsync` instead of `scp`. Mixed-shell pairs, including the current Windows -> Linux lane, continue to fall back to `scp`.
