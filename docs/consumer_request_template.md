# Consumer Request Template v0

Use this template before requesting a new consumer lane or changing an existing consumer seam.

Do not start by editing `toy-yard`.

## 1. Consumer Identity

- consumer name:
- repo or project:
- primary owner:
- contact path:

## 2. What This Consumer Actually Does

- one-sentence description:
- execution environment:
  - Unreal
  - Blender
  - Houdini
  - viewer
  - validator
  - other
- is this a real downstream consumer, or just a temporary experiment?

## 3. Asset Scope

- asset families:
  - 3d
  - motion
  - image
  - audio
  - mixed
- first real sample:
- expected future scale:

## 4. Packet Intake

- can the consumer read an existing `toy-yard` packet?
- if yes, which packet?
- if no, what is missing?
- what is the narrowest acceptable first packet?

## 5. First Trial Node

- proposed trial node name:
- first operation to prove:
- success condition:
- smallest useful evidence:

## 6. Result Contract

- does the consumer return machine-readable results?
- result file format:
- minimum fields:
- where should result files land before import?

## 7. Ownership Boundary

Please classify the boundary in plain language.

- what belongs to `toy-yard`:
- what belongs to the consumer:
- what should definitely not move into `toy-yard`:

## 8. Failure Routing

- producer-side failures:
- consumer-side failures:
- unknowns still needing discussion:

## 9. Runbook Ownership

- does this consumer need a project-specific runbook after seam acceptance?
- if yes, who maintains it?
- where will it live?

## 10. Requested Outcome

Choose one:

- review whether an existing packet is enough
- request a new packet lane
- request result-import support
- request a narrow shadow trial

## 11. Notes

- extra context:
- constraints:
- deadline pressure if any:
