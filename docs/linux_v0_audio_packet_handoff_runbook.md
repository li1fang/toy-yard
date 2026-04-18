# Toy-Yard Linux v0 Audio Packet Handoff Runbook

## Goal

Execute the first real `linux -> windows` Toy-Yard handoff for the audio lane.

This runbook covers only:

1. Linux export
2. Linux `scp` upload
3. Windows packet import
4. Windows catalog verification

It does **not** cover media sync.
This is an index-only handoff.

## Preconditions

### Linux Side

- `toy-yard` is installed and working
- warehouse root exists at `/srv/toy-yard`
- the target audio session has already been imported
- a usable SSH credential exists for the Windows host

### Windows Side

- `toy-yard` exists at `C:\Projects\toy-yard`
- SSH is reachable on the Windows host
- packet inbox exists at:
  - `C:\Projects\toy-yard\_exchange\index_packets`

### Transport Contract

- upload target uses Windows drive-style SCP path:
  - `C:/Projects/toy-yard/_exchange/index_packets/`

## Step 1: Export The Audio Index Packet On Linux

### Generic Command

```bash
toyyard --root /srv/toy-yard export audio-index-packet \
  --session-id <session_id> \
  --node-id <linux_node_id>
```

### Current Smoke Example

```bash
toyyard --root /srv/toy-yard export audio-index-packet \
  --session-id audio-session-smoke-001 \
  --node-id linux-audio-01
```

Expected packet path:

```text
/srv/toy-yard/_exchange/index_packets/audio__linux-audio-01__audio-session-smoke-001.json
```

## Step 2: Probe SSH From Linux

Before upload, confirm the Windows host is actually reachable.

```bash
ssh -p 22 garro@192.168.1.44
```

If this fails, stop here.
Do not continue to packet import troubleshooting until transport is green.

## Step 3: Upload The Packet To Windows

### Current Smoke Example

```bash
scp -P 22 \
  /srv/toy-yard/_exchange/index_packets/audio__linux-audio-01__audio-session-smoke-001.json \
  garro@192.168.1.44:C:/Projects/toy-yard/_exchange/index_packets/
```

### Generic Form

```bash
scp -P 22 \
  /srv/toy-yard/_exchange/index_packets/<packet>.json \
  <windows_user>@<windows_host>:C:/Projects/toy-yard/_exchange/index_packets/
```

Expected result:

- packet file appears in:
  - `C:\Projects\toy-yard\_exchange\index_packets`

## Step 4: Import The Packet On Windows

Run this on Windows:

```powershell
toyyard --root C:\Projects\toy-yard import audio-index-packet `
  --packet-path C:\Projects\toy-yard\_exchange\index_packets\audio__linux-audio-01__audio-session-smoke-001.json
```

Expected JSON shape:

```json
{
  "packet_id": "...",
  "packet_path": "...",
  "origin_node_id": "linux-audio-01",
  "canonical_sample_id": "...",
  "canonical_package_id": "...",
  "source_artifacts": 0,
  "package_artifacts": 0,
  "availability": "remote_index_only"
}
```

The exact IDs will vary, but:

- `origin_node_id` should match Linux
- `availability` should be `remote_index_only`

## Step 5: Verify On Windows

### Catalog View

```powershell
toyyard --root C:\Projects\toy-yard report audio-catalog
```

Expected row characteristics:

- the imported audio entry is visible
- `availability = remote_index_only`
- `replicated_from_node = linux-audio-01`

## Failure Routing

### Transport Failures

Examples:

- `No route to host`
- auth rejected
- bad SCP path

Action:

- fix SSH / network / host profile first

### Packet Failures

Examples:

- schema mismatch
- malformed JSON
- missing required packet identity fields

Action:

- fix producer packet generation on the Linux side

### Import Semantics Failures

Examples:

- packet imports but does not appear in `audio-catalog`
- imported row lacks `remote_index_only`
- `replicated_from_node` missing

Action:

- treat as `toy-yard` implementation issue

## Exit Criteria

This Linux v0 handoff is considered successful when all of the following are true:

1. Linux exports a packet successfully
2. Linux uploads the packet successfully via `scp`
3. Windows imports the packet successfully
4. Windows `audio-catalog` shows:
   - `availability = remote_index_only`
   - correct `replicated_from_node`

Once that is green, we can move from "local closed loop" to "real cross-node packet exchange."
