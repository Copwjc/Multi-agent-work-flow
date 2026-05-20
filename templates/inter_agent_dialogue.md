# Inter-Agent Dialogue: {task_title}

- Slug: `{slug}`
- Created: `{created_at}`
- Agents: {agents}

Use this file for concrete direct agent-to-agent requests and replies. Keep the
global summary in `agent_interactions.md`; keep the evidence and resource chain
here. Agents do not need to route routine requests through Leader, but any
conflict, scope change, or externally visible claim still needs Leader review.

## Request Ledger

| Request ID | Parent | Status | From | To | Type | Need | Artifact / Resource |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Dialogue Entries

### Example

- Request ID: `REQ-example`
- Parent: `none`
- From: `latex_writer`
- To: `code_expert`
- Type: `evidence_request`
- Status: `open`
- Need: Provide the experiment result supporting a report claim.
- Why: The LaTeX report should not make unsupported claims.
- Artifact / Resource: `report/main.tex`
- Leader Review Required: yes
- Response:
  - TODO

### Literature Example

- Request ID: `REQ-literature-example`
- Parent: `none`
- From: `leader`
- To: `literature_collector`
- Type: `literature_request`
- Status: `open`
- Need: Map the mainstream methods, representative papers, baselines, datasets, and metrics.
- Why: Mathematical and implementation choices should be grounded in current research routes.
- Artifact / Resource: `notes/literature_review.md`
- Leader Review Required: no
- Response:
  - TODO
