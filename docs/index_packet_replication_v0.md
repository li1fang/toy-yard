# Toy-Yard Index Packet Replication v0

## Goal

Make `toy-yard` usable across multiple nodes without turning the warehouse into a giant file-sync problem.

The first rule is simple:

- sync **index packets**
- do **not** sync heavy media data by default

That gives us cross-node visibility first, and cross-node media movement only when a lane truly needs it.

## Why This Exists

We already have a canonical local warehouse on Windows.
We now also want a Linux-side `toy-yard` node that can:

- catalog its own sessions
- export machine-readable packet summaries
- send those summaries to Windows over SSH
- let Windows import them as `remote_index_only`

This is the smallest useful version of a multi-node warehouse.

## v0 Scope

Linux v0 is intentionally narrow:

- lane: `audio`
- direction: `linux -> windows`
- transport: `ssh/scp`
- replicated unit: `audio index packet`
- replicated data: `index only`, not media payloads

Out of scope for v0:

- bidirectional replication
- automatic conflict resolution
- full media sync
- background daemonization
- shared remote DB

## Design Principles

1. Canonical warehouse remains local per node.
2. Cross-node exchange is file-based.
3. Non-secret transport metadata is shareable.
4. Secrets stay outside the repo.
5. Imported remote packets must remain idempotent.
6. Remote packets must never pretend to be local media ownership.

## Core Objects

### 1. Index Packet

An index packet is the smallest cross-node exchange unit.

For audio v0 it is:

- schema: `audio_index_packet_v0`
- kind: `audio_index`
- lane: `audio`

It contains:

- canonical sample/package identity
- source metadata
- artifact metadata
- origin node identity
- payload hash

It does **not** contain copied media bytes.

### 2. SSH Host Profile

The transport side is split in two layers:

- `ssh-info.json`
  - non-secret
  - readable by AI and automation
  - contains host, port, username, path style, target directories, and credential reference
- `ssh-secret.json` or local secret store
  - secret
  - only readable on trusted nodes
  - contains the actual credential material or the secure lookup path

The key rule is:

`toy-yard` may automate from credential references, but never from secret material committed into the repo.

### 3. Packet Outbox / Inbox

Each node owns a local packet exchange directory:

- local outbox/inbox root: `_exchange/index_packets`

For Linux v0:

- Linux exports packets into its own outbox
- Linux sends packets to the Windows inbox over `scp`
- Windows imports the packet from its local inbox path

## Import Semantics

When Windows imports a Linux audio index packet:

- the sample/package are created or refreshed canonically
- media availability is marked as `remote_index_only`
- `replicated_from_node` is recorded
- source/package artifacts are stored as remote metadata, not as local copied media

This keeps the warehouse truthful:

- Windows knows the item exists
- Windows knows which node owns the current media reality
- Windows does not fake local possession

## Linux v0 Operational Shape

### Export

Linux exports one packet:

```bash
toyyard --root /srv/toy-yard export audio-index-packet \
  --session-id <session_id> \
  --node-id <linux_node_id>
```

### Transfer

Linux sends the packet:

```bash
scp -P 22 \
  /srv/toy-yard/_exchange/index_packets/<packet>.json \
  <user>@<windows_host>:C:/Projects/toy-yard/_exchange/index_packets/
```

### Import

Windows imports the packet:

```powershell
toyyard --root C:\Projects\toy-yard import audio-index-packet `
  --packet-path C:\Projects\toy-yard\_exchange\index_packets\<packet>.json
```

### Verify

Windows verifies catalog visibility:

```powershell
toyyard --root C:\Projects\toy-yard report audio-catalog
```

Expected result:

- the imported row exists
- `availability = remote_index_only`
- `replicated_from_node = <linux_node_id>`

## Failure Ownership

For Linux v0 we keep routing simple:

- SSH reachability / auth / upload path failure
  - transport or operator issue
- packet schema or payload invalid
  - producer-side issue
- packet imports but catalog semantics are wrong
  - `toy-yard` implementation issue

This is enough for v0.

## Roadmap Beyond v0

### v0

- one lane
- one direction
- packet only

### v1

- more lanes
- more packet types
- stronger runbooks
- more than one verified SSH host profile

### v2

- true dual-write / dual-index discipline
- both nodes can publish authoritative packets for their own lanes
- explicit reconciliation rules where identities overlap

## Current Recommendation

Use Linux v0 to prove the exchange seam first.

Do not jump straight to full bidirectional sync.

The right sequence is:

1. stabilize packet-only exchange
2. stabilize per-lane ownership
3. then expand toward real dual-write / dual-index

That keeps the warehouse honest and keeps the transport layer boring, which is exactly what we want.
