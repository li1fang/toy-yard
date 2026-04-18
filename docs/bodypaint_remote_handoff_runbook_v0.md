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
4. runs peer-to-peer `scp -r` from source node to target node
5. computes a source directory tree manifest before transfer
6. verifies the target export directory contains:
   - `summary/bodypaint_suite_summary.json`
   - `summary/bodypaint_packet_registry.json`
   - `summary/bodypaint_packet_check.json`
   - `summary/communication_signal.json`
7. computes a target directory tree manifest after transfer
8. compares source and target tree hashes

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
- the target export directory tree hash matches the source tree hash

Consumer execution still belongs to the BodyPaint lane itself.

## Transfer Integrity

The BodyPaint handoff now records both sides of the directory transfer:

- `source_tree_manifest.payload.files[*].sha256`
- `source_tree_manifest.payload.tree_hash`
- `target_tree_manifest.payload.files[*].sha256`
- `target_tree_manifest.payload.tree_hash`
- `transfer.tree_hash_match`

This is still not a resumable transfer protocol. It is the first integrity layer: if `scp -r` returns success but the target tree differs, the handoff fails at `target_hash_verify`.
