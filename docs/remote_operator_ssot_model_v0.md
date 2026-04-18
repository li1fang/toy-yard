# Remote Operator SSOT Model v0

## Core Decision

`GitHub` is the source of truth for:

- code
- schemas
- runbooks
- deployable branch/commit identity

`Remote Operator Mode` is **not** a second code-authoring system.

It is the runtime control plane for:

- node reachability
- code deployment to runtime nodes
- controlled `toyyard` execution
- packet handoff orchestration
- runtime audit trails

## What This Means In Practice

### Code and Contracts

Best practice:

- all normal code changes happen in the main repository workflow
- runtime nodes update by explicit `remote sync-code --branch <branch>`
- deployed nodes should be identifiable by exact branch + commit

Avoid:

- editing source directly on Linux as a normal workflow
- letting Windows and Linux drift into separate unpublished hotfix lines
- treating SSH access as a substitute for reviewable source history

Emergency hotfixes are allowed only as exceptions, and should be upstreamed back to GitHub immediately.

### Runtime Warehouse State

Each node remains authoritative for its own local runtime state:

- local SQLite
- local managed files
- local canonical media presence
- local packet inbox/outbox

Remote Operator can instruct a node to mutate that local state through approved `toyyard` commands, but that does not make another node the owner of the local warehouse.

### Cross-Node Exchange

Cross-node exchange should remain explicit and lane-shaped:

- index packets
- consumer packets
- future result packets

Do not try to make every CRUD action mirror instantly across all nodes.

That would blur ownership, complicate failure recovery, and turn `toy-yard` into a distributed database before we actually need one.

## Recommended Operating Model

### 1. GitHub as code SSOT

- develop on reviewed branches
- merge intentionally
- deploy exact commits to runtime nodes

### 2. Remote Operator as runtime deploy/control plane

- probe nodes
- inspect status
- sync code
- run approved `toyyard` commands
- orchestrate packet handoff

### 3. Packets as cross-node data sync surface

- sync indexes where needed
- sync consumer-facing packets where needed
- keep raw media ownership local unless there is a clear replication policy

### 4. Explicit authority by lane

For each lane, decide separately:

- which node can author canonical entries
- which nodes may import as replica/index-only
- which packets are safe to hand off

## Synchronization Level

The best default is **not** "sync every add/delete/update immediately".

The better default is:

- sync code by commit
- sync data by packet
- sync runtime actions by explicit operator command
- keep audit trails for every remote operation

This keeps the system understandable.

## Near-Term Recommendation

For the current stage:

1. Keep GitHub as the only code SSOT.
2. Use Remote Operator to keep Linux aligned to reviewed commits.
3. Use explicit packet handoff for cross-node warehouse exchange.
4. Do not introduce full bidirectional automatic warehouse mirroring yet.
5. If a lane truly needs multi-node replication, define it lane-by-lane with authority and replica rules first.
