# R1.6 BodyPaint Cross-Node Export Handoff

## Status

`pass`

## Purpose

Validate that Remote Operator Mode can move a directory-style consumer packet across real nodes, not only self-drop on the same Windows host.

## Nodes

- Source host: `windows-3070ti-operator-target`
- Target host: `linux-toyyard-01`
- Transfer topology: `peer_to_peer`
- Lane: `bodypaint`

## Command Shape

```text
toyyard --root C:\Projects\toy-yard remote handoff bodypaint-view ^
  --source-host windows-3070ti-operator-target ^
  --target-host linux-toyyard-01 ^
  --profile remote-bodypaint-cassia-to-linux ^
  --package pkg_mingchao-sample-4f64c1a1-character-d214086d_7d8f416608 ^
  --target-transfer-profile toy_yard_bodypaint_packets
```

## Result

- Operation id: `op_remote-handoff-bodypaint-view_9dccc82df3`
- Source export root: `C:\Projects\toy-yard\05_publish\bodypaint\remote-bodypaint-cassia-to-linux`
- Target export root: `/srv/toy-yard/_exchange/consumer_packets/bodypaint/remote-bodypaint-cassia-to-linux`
- Target packet shape verification: `pass`
- Registry packet count: `1`

## Notes

The BodyPaint packet itself still reported content-level attention/blocker semantics:

- `bodypaint_packet_check.status = attention`
- `communication_signal.status = blocker`

Those are lane/content signals, not Remote Operator transport failures. This checkpoint only validates export-directory handoff, target packet shape, and cross-node delivery.

## Follow-Up

After this checkpoint, directory-style handoff integrity was upgraded to record source and target tree manifests and require matching tree hashes.

The next hardening step adds atomic target staging:

- transfer first lands under `.incoming/<operation_id>/<profile>/`
- staged shape and tree hash are verified before promotion
- the final consumer-visible directory is updated only after staged verification passes
- an existing final directory is moved aside under `.previous/<operation_id>`

## Hashcheck Re-Run

After adding tree hash verification, the same Windows-to-Linux lane was re-run with:

- Profile: `remote-bodypaint-cassia-hashcheck-to-linux`
- Operation id: `op_remote-handoff-bodypaint-view_c0408a7418`
- Source file count: `7`
- Target file count: `7`
- Source tree hash: `c50b61993815ff1ae88a6874069b0004bce177b621ad0641b61fd8e9924df3f7`
- Target tree hash: `c50b61993815ff1ae88a6874069b0004bce177b621ad0641b61fd8e9924df3f7`
- `transfer.tree_hash_match = true`

This confirms that cross-node directory handoff is now shape-verified and hash-verified.
