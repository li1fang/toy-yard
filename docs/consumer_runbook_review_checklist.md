# Consumer Runbook Review Checklist v0

Use this checklist when reviewing a new consumer request or a consumer-specific runbook.

## Admission Checklist

- Is this a real downstream consumer, not just a vague tool idea?
- Is the asset scope clear enough?
- Is the first trial node narrow enough?
- Is there a credible packet shape?
- Is there a credible result shape?
- Is ownership between `toy-yard` and the consumer explicit?
- Would admitting this consumer drag `toy-yard` into execution-platform business?

If the answer to the last question is `yes`, stop and re-scope.

## Boundary Checklist

- `toy-yard` stays responsible for warehouse truth
- `toy-yard` stays responsible for packet export
- `toy-yard` does not become the runtime executor
- consumer-specific validation remains with the consumer
- project-specific runbook remains with the consumer project

## Packet Checklist

- portable paths only for active consumption
- original paths remain lineage only
- packet self-check exists or is planned
- trial packet is as small as possible
- packet fields are consumer-facing, not warehouse-internal leakage

## Result Checklist

- result is machine-readable
- result has enough fields for ownership routing
- result can be stably imported back into canonical storage
- success and failure are both representable

## Process Gate

Reject direct `toy-yard` edits if:

- the consumer has not read the generic runbook
- the request template is not filled in
- the first seam node is still undefined
- the boundary is still blurry
- the requested change is really consumer business, not warehouse business

## Review Outcomes

- `approve_generic_intake_only`
- `approve_existing_packet_trial`
- `approve_new_packet_lane_trial`
- `needs_rewrite_before_review`
- `reject_out_of_scope`

## Existing Consumer Follow-Up

If the consumer is already active and confirms current compatibility, ask for a short consumer confirmation record when useful.

This is recommended, not mandatory.
