# Remote Operator Transfer Hardening v0

## Goal

Make Remote Operator Mode trustworthy for larger packets without turning it into a hidden file-sync daemon.

The near-term principle is:

- operator initiates
- nodes transfer directly
- every transfer is auditable
- every integrity claim is machine-readable

## Current Baseline

Remote Operator Mode already supports:

- SSH probe/status/sync-code
- controlled remote `toyyard` commands
- file packet handoff for `audio-index`
- export-directory handoff for `bodypaint-view`
- operator run records under `_exchange/operator_runs`

`audio-index` already verifies a single packet hash before target import.

`bodypaint-view` now verifies directory integrity by comparing source and target tree hashes.

## Hardening Order

### H1: Directory Tree Hash Verification

Status: `implemented for bodypaint-view`

For directory-style handoff, the operator records:

- source file list
- per-file `sha256`
- per-file byte size
- source tree hash
- target file list
- target tree hash
- `transfer.tree_hash_match`

This catches partial or corrupted directory transfers even when `scp -r` exits successfully.

### H2: Atomic Target Staging

Status: `implemented for bodypaint-view`

Directory handoff should write into:

```text
<target_profile_dir>/.incoming/<operation_id>/
```

Then, only after shape and hash verification pass, promote to:

```text
<target_profile_dir>/<profile>/
```

Failed staging directories should be retained with a run id, not silently deleted.

The current BodyPaint implementation transfers into `.incoming/<operation_id>/<profile>/`, verifies staged shape/hash, then promotes that staged directory to the consumer-visible profile directory. If the final directory already exists, it is moved aside under `.previous/<operation_id>` before promotion.

### H3: Idempotent Resume Metadata

Status: `implemented for bodypaint with packet-native stable fingerprint`

Each handoff should have a stable transfer identity derived from:

- source host
- target host
- lane
- profile
- source tree hash
- target transfer profile

If the same transfer is retried, the operator should detect whether the target already has the verified tree.

The current BodyPaint result now includes `transfer_identity.transfer_id`, derived from source host, target host, lane, profile, stable packet fingerprint, and target transfer profile. If the final target directory already exists, still passes packet-shape verification, and already matches the source stable packet fingerprint, the operator skips re-transfer and returns a verified no-op result.

BodyPaint now publishes that stable packet fingerprint inside packet-native metadata:

- `summary/bodypaint_packet_identity.json`

Remote Operator prefers the packet-native identity sidecar and only falls back to local recomputation for older packets.

The current v0 stable fingerprint ignores:

- `generated_at_utc`

### H4: Resumable Transfer Capability

Status: `implemented for bash-to-bash transport selection; scp fallback remains default for mixed shells`

Current implementation:

- detect `rsync` / `scp` / `sftp` availability per host during remote status or handoff planning
- use `rsync --partial --append-verify` when both source and target are `bash` hosts and both expose `rsync`
- fall back to `scp` for mixed-shell pairs or hosts without `rsync`
- record `transfer.selected_transport`, `transfer.resume_supported`, and `transfer.selection_reason` in the result payload

Current practical consequence:

- the current Windows -> Linux BodyPaint lane still falls back to `scp`
- Linux -> Linux lanes can now use resumable `rsync`
- the transport seam is now explicit instead of being hidden inside the shell command string

### H5: Chunked Large Artifact Transfer

Status: `future`

For very large model, video, or dataset payloads, add a chunk manifest:

- chunk size
- chunk count
- per-chunk `sha256`
- whole-object `sha256`
- final assembly hash

Chunking is not the first default because it adds complexity and is unnecessary for small packet directories.

### H6: Parallel Transfer and Bandwidth Policy

Status: `future`

Only after H1-H5 are stable:

- parallel chunk upload
- bandwidth limits
- priority classes
- transfer queue visibility

## Recommendation

Do not start with chunking.

The practical next steps are:

1. Make every directory handoff hash-verified.
2. Add atomic target staging.
3. Add idempotent transfer identity.
4. Add rsync resume when available.
5. Add chunking only for large media lanes that need it.
