# R1.10 Remote Transport Selection Note

## Status

`pass`

## Purpose

Record the first real validation of H4 transport selection in Remote Operator Mode.

## Host Capability Check

- Linux host: `linux-toyyard-01`
  - `scp_available = true`
  - `sftp_available = true`
  - `rsync_available = true`
- Windows host: `windows-3070ti-operator-target`
  - `scp_available = true`
  - `sftp_available = true`
  - `rsync_available = false`

## Real Mixed-Shell Validation

Profile:

- `remote-bodypaint-cassia-h4-transport-check`

Operation:

- `op_remote-handoff-bodypaint-view_53400e6844`

Result:

- `status = pass`
- `transfer.selected_transport = scp`
- `transfer.resume_supported = false`
- `transfer.selection_reason = rsync_requires_bash_on_both_nodes`
- `transfer.tree_hash_match = true`
- `transfer.stable_fingerprint_match = true`

## Interpretation

This is the intended H4 v1 behavior:

- transport selection is now explicit and machine-readable
- mixed-shell Windows -> Linux handoff still falls back to `scp`
- resumable `rsync` remains available for future bash -> bash lanes

## Next Refinement

The next practical refinement is:

- keep this selection logic
- add packet-native stable fingerprint metadata
- then introduce resumable `rsync` in the first real bash -> bash consumer lane
