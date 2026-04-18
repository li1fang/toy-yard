# R1.5: BodyPaint Export Handoff

Status: pass

## Scope

This checkpoint records the first successful controlled handoff for a directory-style export packet in `Remote Operator Mode`.

Lane:

- `bodypaint`

Shape:

- remote export on source host
- peer-to-peer `scp -r`
- target-side packet shape verification

## Source And Target

Current validation used the Windows runtime node as both source and target.

That may look unusual, but it is still valuable because it proves:

- remote export is machine-readable
- the operator can drive recursive transfer
- the target drop directory contract works
- target-side verification can reason over the received packet

## Executed Flow

Command family:

- `remote handoff bodypaint-view`

Profile:

- `remote-bodypaint-cassia-selfdrop`

Selected package:

- `pkg_mingchao-sample-4f64c1a1-character-d214086d_7d8f416608`

Target transfer profile:

- `toy_yard_bodypaint_packets`

## Result

Observed result:

- source export passed
- target drop directory was created
- recursive `scp` transfer passed
- target verification passed

Target verification confirmed:

- `summary/bodypaint_suite_summary.json` exists
- `summary/bodypaint_packet_registry.json` exists
- `summary/bodypaint_packet_check.json` exists
- `summary/communication_signal.json` exists
- registry contains at least one packet

## Notes

- `packet_check_status = attention`
- `communication_signal_status = blocker`

These are lane-level packet semantics, not transfer failures.

For this checkpoint, the acceptance target is:

- successful export handoff
- successful target packet-shape verification

It does not require BodyPaint consumer execution.

## Meaning

`Remote Operator Mode` now has two validated handoff shapes:

1. index packet handoff with target import (`audio`)
2. export directory handoff with target packet verification (`bodypaint`)
