# Toy-Yard Lane Authority / Replica Note v0

## Goal

Record the boundary that keeps multi-node `toy-yard` sane before true dual-write arrives.

## Current Rule

Each node is authoritative for its own local warehouse writes.

Cross-node imports are replica records unless a future lane contract says otherwise.

## Audio Lane

For the current `audio / index packet` lane:

- source node exports an index packet
- target node imports metadata only
- imported entries must stay truthfully marked as:
  - `availability = remote_index_only`
  - `replicated_from_node = <origin node>`

That means the target catalog gains visibility, not ownership of the media payload.

## What This Prevents

This boundary prevents:

- silent ownership drift
- fake local availability
- accidental dual-write assumptions
- treating packet replication as raw media replication

## Future Lanes

- `motion`: reserved for a future runtime authority note
- `image`: reserved for a future runtime authority note

For now, only `audio` has an explicit runtime replica rule in active use.
