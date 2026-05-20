# Resource Registry: {task_title}

- Slug: `{slug}`
- Created: `{created_at}`
- Agents: {agents}

Use this file as the shared resource index for direct agent collaboration.
Register data, scripts, experiment outputs, figures, citations, reports, and
blocking dependencies that another agent may need.

## Resource Ledger

| Resource ID | Owner | Type | Path / Link | Status | Reusable By | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| RES-000 | leader | task brief | `README.md` | available | all | Initial task context. |

## Resource Types

- `dataset`: raw or processed data.
- `script`: code, experiment runner, plotting utility, or validation tool.
- `result`: JSON, CSV, logs, metrics, tables, or diagnostics.
- `figure`: generated plot or diagram.
- `citation`: source, paper, DOI, BibTeX entry, or source-check note.
- `proof`: assumptions, derivation, theorem, counterexample, or math note.
- `report`: LaTeX, PDF, slides, or final write-up material.
- `decision`: Leader decision, user override, acceptance rule, or rejected claim.

## Usage Rules

- Agents may request a resource directly in `logs/inter_agent_dialogue.md`.
- Resource owners should answer with a concrete path, command, citation, or
  reason the resource is unavailable.
- If a resource is stale, invalidated, private, unsafe, or outside scope, mark it
  `blocked` or `invalidated` and escalate to Leader when needed.
