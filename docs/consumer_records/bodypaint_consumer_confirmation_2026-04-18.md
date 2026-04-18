# BodyPaint Consumer Confirmation

- date: `2026-04-18`
- consumer: `BodyPaint`
- status: `confirmed_compatible`
- producer_repo: `toy-yard`
- lane: `bodypaint`

## Summary

`BodyPaint` confirmed that it can continue to directly consume the current `toy-yard` BodyPaint packet.

Current conclusion:

- no additional producer-side contract changes are required for BodyPaint
- the current packet shape is acceptable
- the latest producer-side hardening is treated as a stabilizing change, not a breaking one

## Required Fields

BodyPaint reported these fields as required:

- `sample_id`
- `package_id`
- `source_model.raw_path` or `source_model.staged_path`
- `expected_outputs.normalized_glb`
- `expected_outputs.painted_glb`
- `expected_outputs.analysis_json`

## Optional But Currently Read

BodyPaint reported these fields as currently read, but not required for execution:

- `source_model.provider`
- `source_model.upstream_profile`
- `source_model.upstream_manifest_path`
- `expected_outputs.manual_overrides`
- `viewer.override_path`
- `viewer.preferred_model`
- `viewer.fallback_model`
- `viewer.analysis`

## Not Used As Run Preconditions

BodyPaint reported these are not currently startup blockers:

- `bodypaint_packet_check.json`
- `communication_signal`
- `engineering_mask`

## Consumer Assumptions

BodyPaint confirmed these assumptions:

- `source_model` and `expected_outputs` path semantics are stable
- the processor runs against the current packet
- result import does not require the export profile directory to remain the long-term canonical evidence location

## Current Producer Impact

Current producer-side impact is:

- no required rollback to old path conventions
- no required new BodyPaint-specific fields
- no immediate producer-side contract changes requested

## Agreed Boundary

Current working boundary:

- `toy-yard` owns producer packet, packet self-check, and result import
- `BodyPaint` owns consumer execution, normalization, geometry analysis, engineering paint, and viewer-side debugging

## Recommended Follow-Up

If BodyPaint expands consumer-side capability later, the expected path is:

- seam review first
- producer changes only if the seam review actually requires them

This record is a confirmation artifact, not a hard schema contract.
