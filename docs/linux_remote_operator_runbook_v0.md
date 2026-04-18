# Toy-Yard Linux Remote Operator Runbook v0

## Goal

Operate the Linux `toy-yard` runtime node directly from the Windows operator host through `toyyard remote ...`.

This runbook assumes:

- the Linux runtime root is `/srv/toy-yard`
- the repo checkout is discovered at `/srv/toy-yard/repo`
- node transport stays `peer_to_peer`
- packet exchange remains index-first

## Normal Sequence

1. register the Linux host profile
2. run `remote probe`
3. run `remote status`
4. run `remote sync-code`
5. run bounded `remote toyyard ...`
6. if needed, run `remote handoff audio-index`
7. inspect the JSON result under `_exchange/operator_runs`

## Approved Commands

Normal Linux-side operations should stay inside:

- `toyyard remote probe`
- `toyyard remote status`
- `toyyard remote sync-code`
- `toyyard remote toyyard -- report ...`
- `toyyard remote toyyard -- inspect ...`
- `toyyard remote toyyard -- export ...`
- `toyyard remote handoff audio-index`

## Health Checks

The Linux node is considered healthy when:

- SSH is reachable
- `/srv/toy-yard` exists
- `/srv/toy-yard/repo/toyyard.py` exists
- git branch / commit can be read
- Python is available
- `_db/toyyard.sqlite` exists
- `_exchange/index_packets` exists
- `python3 toyyard.py --root /srv/toy-yard report audio-catalog` works

## Stop And Escalate

Stop instead of improvising when:

- SSH auth or host key breaks
- `/srv/toy-yard/repo` is missing or not a git checkout
- `git pull` would require conflict resolution
- the DB is missing or corrupted
- a task requires direct SQLite edits
- a task requires broad filesystem mutation outside `toyyard`

## First Runtime Node

For `R1`, the Linux node is the first formal remote runtime node.

That means:

- day-to-day project operations can be driven remotely
- secrets still stay outside the repo
- packet transfers remain explicit
- the node is managed, not casually hand-edited
