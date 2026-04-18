# R1: Linux Remote Operator Runtime Node

Status: pass

## Scope

This checkpoint records the first end-to-end validation of `Remote Operator Mode v1` for `toy-yard`.

The validated path is:

1. register Linux and Windows host profiles
2. probe both nodes over SSH
3. read Linux runtime warehouse status
4. sync Linux code and editable install
5. run a remote `toyyard` report on Linux
6. orchestrate a real Linux-to-Windows `audio-index` handoff
7. verify Windows imported the packet as a replica entry

## Registered nodes

- source host: `linux-toyyard-01`
- target host: `windows-3070ti-operator-target`

Both hosts were registered from local non-secret SSH host profiles under:

- `_exchange/operator_hosts/linux-toyyard-01.ssh-info.json`
- `_exchange/operator_hosts/windows-3070ti-operator-target.ssh-info.json`

## Probe results

- Linux probe: `pass`
  - resolved host command returned `whipz`
- Windows probe: `pass`
  - resolved host command returned `light`

This confirms the operator can reach both endpoints directly over SSH.

## Linux runtime status

Remote status returned:

- `project_root = /srv/toy-yard`
- `repo_root = /srv/toy-yard/repo`
- `git_branch = codex/toy-yard-clean-history`
- `git_commit = 0810fa4`
- `python_version = 3.13.12`
- `db_exists = true`
- `index_packet_root_exists = true`
- `toyyard_entry_exists = true`

This is sufficient for Linux v0 runtime operation.

## Sync result

`remote sync-code --install-editable` passed against Linux.

The node confirmed:

- target branch is available
- repository is reachable from origin
- editable install can be refreshed in the remote venv

## Remote toyyard execution

Remote execution of:

```text
report audio-catalog
```

passed on Linux and returned the expected local audio catalog row.

This confirms the operator path is not limited to SSH reachability; it can run approved `toyyard` commands remotely.

## Real handoff result

Operation:

- `remote handoff audio-index`
- `session_id = audio-session-smoke-001`
- `source_node_id = linux-audio-01`
- `target_transfer_profile = toy_yard_index_packets`
- `import_target = true`

Observed result:

- Linux exported packet:
  - `audio__linux-audio-01__audio-session-smoke-001.json`
- Linux performed peer-to-peer `scp` directly to Windows
- Windows imported the packet successfully
- source and target SHA256 matched

Windows verification confirmed:

- `availability = remote_index_only`
- `replicated_from_node = linux-audio-01`
- canonical sample and package IDs match the Linux source packet lineage

## Acceptance

`R1` is considered passed because all required conditions were satisfied:

- Linux node is formally registered in the operator plane
- `remote probe` passed
- `remote status` returned runtime warehouse state
- `remote sync-code` passed
- remote `toyyard` execution passed
- real Linux-to-Windows packet handoff passed
- target import passed
- target catalog semantics are replica-correct
- operation result files were written under `_exchange/operator_runs`

## Notes

- This checkpoint validates `audio / index packet` as the first remote-operated lane.
- The authority/replica boundary remains unchanged: replica imports do not claim source media ownership.
- This checkpoint does not introduce automatic merge, dual-write reconciliation, or cross-node database sync.
