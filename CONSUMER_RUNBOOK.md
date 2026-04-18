# Toy-Yard Consumer Onboarding Runbook v0

`toy-yard` welcomes new downstream consumers, but the admission path is now explicit:

1. read this runbook first
2. submit a consumer request using the shared template
3. wait for warehouse-side review and admission scope
4. only after approval, implement or review packet changes

New consumers should **not** start by directly editing `toy-yard`.
That workflow is too risky because it mixes:

- warehouse concerns
- packet contract concerns
- consumer runtime concerns
- unclear failure ownership

This runbook exists to keep those layers separate.

## What Toy-Yard Owns

`toy-yard` owns the generic producer side:

- canonical warehouse truth
- sample/package/artifact lineage
- portable packet export
- packet self-check
- producer-side communication signal
- result import contract when the consumer seam is accepted
- review of packet contract changes

`toy-yard` does **not** own consumer runtime execution.

That means `toy-yard` does not automatically own:

- Unreal import logic
- Blender execution
- Houdini graph execution
- DCC-specific validation internals
- viewer/runtime semantics inside the consumer project

## What A Consumer Owns

Each admitted consumer owns its own execution lane:

- packet consumption
- runtime/tool execution
- consumer-specific validation
- consumer-side communication signal if needed
- machine-readable result writing
- project-specific operating runbook

So the rule is:

- `toy-yard` writes the **generic onboarding runbook**
- each consumer project writes and maintains its **project-specific runbook**
- `toy-yard` reviews the seam, not the whole consumer business

## Admission Model

Every new consumer starts with the same generic flow.

### Phase 0: Generic Intake

This phase answers one question:

`Should this consumer exist as a toy-yard seam at all?`

Required output:

- one completed consumer request
- one short statement of boundary and ownership
- one proposed first packet shape or proof that an existing packet shape is enough

No code changes in `toy-yard` are allowed yet, except tiny exploratory branches that are explicitly review-only.

### Phase 1: Minimal Seam Trial

This phase answers:

`Can toy-yard hand off one stable packet and receive one stable result?`

Required output:

- one narrow trial node
- one named packet root
- one first operation
- one machine-readable result shape
- one clear owner routing rule

This phase should stay small.
Do not try to solve the entire consumer workflow at once.

### Phase 2: Consumer-Specific Runbook

Only after Phase 1 is accepted does the consumer project maintain its own runbook.

That runbook belongs with the consumer project, not with `toy-yard`.

It should document:

- how to execute the consumer
- local environment expectations
- consumer-specific retries
- consumer-specific artifact locations
- trial commands
- consumer-owned failure classes

`toy-yard` may review that runbook, but does not become its primary owner.

## Required Questions For Every New Consumer

Before a new consumer is admitted, we need answers to these:

1. What asset families does it consume?
2. Does it consume an existing `toy-yard` packet, or does it need a new packet?
3. What is the first narrow operation we want to prove?
4. What machine-readable result should it return?
5. What failures belong to `toy-yard`, and what failures belong to the consumer?
6. Does the consumer need a project-specific runbook after the seam is accepted?

If these answers are still fuzzy, the consumer is not ready to modify `toy-yard`.

## Default Policy For Unknown Consumers

If a consumer is new or poorly understood, for example `autohoudini` today, the default policy is:

- treat it as `unknown_consumer`
- use generic intake only
- do not build a dedicated lane yet
- do not accept direct repo edits as the first step

That keeps us from accidentally designing around assumptions that later turn out to be wrong.

## What To Do Instead Of Editing First

The preferred path is:

1. read this runbook
2. fill in the consumer request template
3. propose the first seam node
4. get warehouse-side review
5. only then make approved changes

## Admission Outcomes

After review, a consumer request should end in one of four outcomes:

- `accepted_as_existing_packet_consumer`
- `accepted_with_new_packet_lane`
- `needs_more_definition`
- `rejected_not_a_toy_yard_concern`

These outcomes are enough for the first version of the process.

## Consumer Confirmation Record

For active consumers that have already validated a seam, a short consumer confirmation record is recommended.

Recommended contents:

- current compatibility conclusion
- required fields
- optional fields currently read
- fields not currently used as startup blockers
- current boundary understanding
- whether producer-side change is currently required

This is recommended, not mandatory.

Why:

- it gives `toy-yard` a lightweight compatibility memory
- it reduces repeated re-discovery during later packet changes
- it does not force every consumer into heavy process overhead

## Current Examples

- `AiUE`: admitted consumer with its own execution lane
- `BodyPaint`: admitted processor/viewer lane with packet export and result import
- `autohoudini`: not yet admitted; generic intake only

## Short Rule

New consumer rule:

`read runbook -> submit request -> review seam -> then edit`
