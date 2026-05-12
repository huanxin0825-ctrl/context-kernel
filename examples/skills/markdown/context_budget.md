# Context Budget

Assemble minimal context under a fixed token budget.

## Intent

Use when a request asks for context selection, prompt reduction, token savings, or budget-aware routing.

## Inputs

- request
- token_budget
- available_context

## Outputs

- context_packet
- budget_report
- omissions

## Constraints

- Include task-critical evidence before convenience context.
- Prefer skill contracts over full procedures.
- Explain important omissions.

## Failure Modes

- Budget is too small for the requested task.
- Relevant memory cannot be found.
- Skill contracts are too vague to route safely.

## Procedure

- Estimate request cost.
- Reserve space for model response.
- Select relevant memory and skills.
- Check the final packet against the budget.

## Examples

- Build a 1200-token packet for a CLI implementation task.

