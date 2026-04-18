# Toy-Yard Remote Operator Mode v0

## Goal

Define the first safe version of direct remote operation for a non-local `toy-yard` node.

The intent is not:

- full machine takeover
- ad hoc shell access without rules
- bypassing warehouse contracts

The intent is:

- operate the remote `toy-yard` project directly over SSH
- reduce repeated human handoff
- keep code, warehouse state, transport, and secrets clearly separated

This document defines the first acceptable boundary for that mode.

## Short Version

`GitHub is the code source of truth.`

`SSH is the operator channel.`

`toy-yard CLI is the write boundary.`

`Packets remain the cross-node exchange unit.`

If one sentence has to carry the whole design, that is the sentence.

## Why This Exists

We already proved the following:

- Windows and Linux can both run `toy-yard`
- SSH is now available between nodes
- the first Linux `audio index packet` was exported, transferred, imported, and verified successfully

That means the next inefficiency is obvious:

- we no longer want every Linux-side action to depend on manual relay through another operator

At the same time, we do **not** want to turn a remote warehouse node into an unbounded shell playground.

So the correct next step is:

- direct remote operation
- with explicit operating boundaries

## Operating Model

Remote Operator Mode v0 treats the Linux node as a managed `toy-yard` node, not as a general-purpose machine we casually mutate.

The model is split into four layers:

### 1. Code Plane

Code is controlled through Git.

Rules:

- the canonical code source of truth is the GitHub repository
- remote nodes should pull approved branches or approved commits
- direct hand-edits on the remote node are discouraged
- if an emergency hotfix happens on a remote node, it must be reconciled back into Git quickly

In practice:

- code changes still originate in the repo
- remote operation consumes those changes

### 2. Operator Plane

SSH is the operator path.

Rules:

- remote execution happens through explicit SSH identity and host profiles
- transport metadata is non-secret and machine-readable
- secrets remain local to trusted nodes or secret stores

Remote Operator Mode v0 assumes:

- `ssh-info.json` exists for the remote node
- credential material is referenced, not committed

### 3. Warehouse Plane

The remote warehouse is real data, not disposable build output.

Rules:

- warehouse changes should happen through `toyyard` commands
- direct DB edits are out of bounds
- direct bulk file manipulation inside canonical storage is out of bounds unless a runbook explicitly allows it
- runtime state should remain reconstructable from warehouse commands and packet history where possible

This is the most important safety line in the design.

### 4. Exchange Plane

Packets remain the preferred exchange unit between nodes.

Rules:

- cross-node visibility should prefer packet exchange over raw filesystem sync
- large media data is not automatically synchronized in v0
- imported remote records should remain truthfully marked as remote or index-only when appropriate

Remote operation does not delete the need for packets.
It just means we can drive packet workflows directly on the remote node.

## What Remote Operator Mode v0 Allows

These actions are in scope for direct remote execution.

### A. Code/Environment Actions

- `git fetch`
- `git pull`
- `git checkout <approved-branch>`
- create/update Python virtual environment
- `pip install -e .`
- lightweight dependency refresh tied to the repo

### B. Toy-Yard CLI Actions

- `toyyard init`
- `toyyard ingest ...`
- `toyyard import ...`
- `toyyard export ...`
- `toyyard report ...`
- `toyyard inspect ...`
- lane-specific smoke runs

### C. Packet Operations

- export packet on the remote node
- upload/download packet through approved transport
- verify inbox/outbox paths
- run import on the receiving node

### D. Read-Only Diagnostics

- inspect logs
- inspect CLI output
- inspect packet files
- inspect generated reports
- verify SSH reachability
- verify path existence

## What Remote Operator Mode v0 Does Not Allow

These actions are explicitly out of scope.

### A. Direct Warehouse Surgery

- editing SQLite by hand
- bulk rewriting canonical directories manually
- deleting warehouse trees outside explicit runbook steps
- moving canonical artifacts around outside `toyyard`

### B. Unbounded Machine Administration

- changing unrelated system services
- broad firewall reconfiguration unless the task is explicitly network-related
- modifying unrelated user environment state
- using the remote machine as a general shell sandbox

### C. Secret Material In Repo

- no private keys in Git
- no passwords in Git
- no embedding secret payloads in non-secret host profile files

### D. Implicit Multi-Node Data Merge

- no silent dual-write conflict resolution
- no pretending synced index metadata equals synced media ownership
- no raw directory mirroring as a substitute for packet design

## Trust Boundary

Remote Operator Mode v0 is based on a narrow trust contract.

We trust ourselves to:

- operate `toy-yard` intentionally
- prefer repo-backed change history
- use runbooks
- avoid side-channel warehouse mutation

We do not rely on:

- memory alone
- ad hoc shell improvisation
- undocumented machine state

That is the difference between "direct control" and "chaotic access."

## Required Inputs

Before a node can enter Remote Operator Mode v0, these must exist:

### 1. Remote Host Profile

A non-secret machine-readable profile, for example:

- `linux-toyyard-01-ssh-info.json`

It should include:

- host / IP
- port
- username
- path style
- upload directories if relevant
- shell family
- verification status
- credential reference

### 2. Credential Reference

Credential material must be available through:

- local secret file
- SSH key path
- SSH agent
- password vault
- environment-variable indirection

But never committed into the repo.

### 3. Remote Project Root

The remote `toy-yard` root must be explicitly known, for example:

- `/srv/toy-yard`

### 4. Remote Runbook

There must be a runbook covering:

- how to update code
- how to activate environment
- how to run approved commands
- how to validate success
- when to stop and escalate

## Minimum Runbook Shape

Every remote node admitted to this mode should have at least one runbook that answers:

1. How do we SSH in?
2. Where is the project root?
3. Which branch/commit are we allowed to deploy?
4. How do we update the environment safely?
5. Which `toyyard` commands are normal?
6. How do we verify the node is healthy afterward?
7. Which operations require human escalation?

If a node cannot answer these, it is not ready for Remote Operator Mode.

## Standard Execution Pattern

The default remote operation pattern is:

1. Verify host identity and SSH reachability
2. Enter project root
3. Confirm branch / commit
4. Pull approved code
5. Refresh environment if needed
6. Run one bounded `toyyard` command or one bounded command sequence
7. Capture output
8. Verify result with a `report` or equivalent check
9. Stop

That last step matters.

The mode should favor short, bounded sessions over long-lived shell drift.

## Escalation Gates

Remote Operator Mode v0 should stop and escalate when any of the following happens:

### Gate 1: Transport Failure

Examples:

- SSH unreachable
- host key mismatch
- credential failure
- SCP/SFTP path mismatch

### Gate 2: Environment Drift

Examples:

- repo root not where expected
- virtual environment broken
- dependency set no longer matches repo expectation

### Gate 3: Warehouse Risk

Examples:

- command implies broad destructive mutation
- warehouse state appears inconsistent
- packet import/export results contradict prior lineage expectations

### Gate 4: System Boundary Crossing

Examples:

- task now requires service management outside the project
- task now requires OS-level reconfiguration
- task now requires secret rotation or manual host repair

When any of these appear, Remote Operator Mode should pause instead of "just trying stuff."

## Logging And Evidence

Every meaningful remote operation should leave enough evidence to reconstruct:

- what node was touched
- what code version was used
- what command was run
- what packet or sample was affected
- what verification succeeded or failed

In v0 this can still be lightweight:

- terminal output
- packet names
- report snapshots
- short operator notes

It does not need a full orchestration platform yet.

## Relationship To Linux v0

Linux v0 packet exchange remains valid under this mode.

The difference is:

- before: we asked the Linux side to run the commands
- now: we are preparing to run those commands ourselves through the same SSH seam

So Remote Operator Mode v0 is not a new warehouse model.
It is an operational upgrade on top of the same packet-first model.

## Initial Adoption Plan

The recommended first adoption path is:

### Phase 1: Read-Only Remote Confidence

- SSH to the Linux node
- verify repo root
- verify branch / commit
- run read-only checks and `toyyard report ...`

### Phase 2: Controlled Project Updates

- perform `git pull`
- run `pip install -e .` if needed
- run bounded smoke commands

### Phase 3: Controlled Lane Operations

- run audio packet export
- run import/report verification
- capture evidence

### Phase 4: Routine Node Operation

- remote `toy-yard` operation becomes normal
- human relay is only needed for system/secret/network repair

This is the right order because it grows confidence without betting the warehouse all at once.

## What Success Looks Like

Remote Operator Mode v0 is successful when:

- we can directly SSH to the Linux node
- we can safely update the remote `toy-yard` checkout
- we can run bounded `toyyard` operations ourselves
- we can verify outcomes with reports
- we no longer need routine human relay for normal warehouse work
- we still keep enough boundary that remote warehouse state is not casually corrupted

## Future Direction

If v0 works, later versions can grow toward:

- multi-node operator profiles
- packet replication automation
- lane-specific remote runbooks
- stronger audit trails
- eventually true dual-write / dual-index coordination

But v0 should stay deliberately modest.

Its job is not to be impressive.
Its job is to make direct remote operation boring and safe.
