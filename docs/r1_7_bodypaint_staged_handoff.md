# R1.7 BodyPaint Staged Export Handoff

## Status

`pass`

## Purpose

Validate that directory-style Remote Operator handoff can isolate partial transfers from consumer-visible packet directories.

## Nodes

- Source host: `windows-3070ti-operator-target`
- Target host: `linux-toyyard-01`
- Lane: `bodypaint`
- Transfer topology: `peer_to_peer`

## Run

- Profile: `remote-bodypaint-cassia-staged-to-linux`
- Package: `pkg_mingchao-sample-4f64c1a1-character-d214086d_7d8f416608`
- Operation id: `op_remote-handoff-bodypaint-view_8095335463`
- Transfer id: `xfer_bodypaint_123f88c60636`

## Staging Paths

- Source export root: `C:\Projects\toy-yard\05_publish\bodypaint\remote-bodypaint-cassia-staged-to-linux`
- Target staging root: `/srv/toy-yard/_exchange/consumer_packets/bodypaint/.incoming/op_remote-handoff-bodypaint-view_8095335463/remote-bodypaint-cassia-staged-to-linux`
- Target final root: `/srv/toy-yard/_exchange/consumer_packets/bodypaint/remote-bodypaint-cassia-staged-to-linux`

## Verification

- Source file count: `7`
- Staged file count: `7`
- Final file count: `7`
- Source tree hash: `c120ee15ace80a4e305fc148d1782a37b2d51d5abf7d26107e4172f7f865a5e6`
- Staged tree hash: `c120ee15ace80a4e305fc148d1782a37b2d51d5abf7d26107e4172f7f865a5e6`
- Final tree hash: `c120ee15ace80a4e305fc148d1782a37b2d51d5abf7d26107e4172f7f865a5e6`
- `transfer.staged_tree_hash_match = true`
- `transfer.tree_hash_match = true`
- `promote_result.payload.promoted = true`

## Notes

The packet still carries BodyPaint content-level warnings:

- `bodypaint_packet_check.status = attention`
- `communication_signal.status = blocker`

Those do not invalidate this checkpoint. R1.7 validates transport isolation, staged verification, promotion, and final tree-hash integrity.
