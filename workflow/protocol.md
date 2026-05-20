# Multi-Agent Coordination Protocol

This project treats Codex as the Leader agent and uses specialist agents as a
collaborative mesh. The Leader owns task decomposition, consistency checks, and
final delivery, but specialist agents may directly request evidence, theory,
implementation details, data, citations, figures, compute results, or writing
support from each other without waiting for a Leader relay.

## Roles

- Leader: decomposes the user request, assigns work, integrates outputs, and
  checks quality before delivery.
- Literature Collector: searches and synthesizes literature, research routes,
  baselines, datasets, metrics, and source-backed claims.
- Mathematician: formalizes the problem, states assumptions, proves claims, and
  identifies edge cases or counterexamples.
- Code Expert: implements algorithms, tests them, runs experiments, and reports
  implementation assumptions.
- LaTeX Writer: turns validated theory and experiment results into a compilable
  paper-style report.
- Auditor/Logger: records decisions, agent handoffs, and final acceptance status.
- User Super Admin: can force a direction correction when the task drifts from
  the intended goal. The Leader must pause, record, redirect, and resume.

## Handoff Rules

1. Each worker owns a clearly bounded file set.
2. Literature review maps the research landscape before theory, code, and
   report claims are finalized.
3. Mathematical definitions are treated as the source of truth for algorithms.
4. Code results must not be used as theorem proof.
5. The report may only claim what is backed by literature, proof, tests,
   experiments, or Leader decisions.
6. The Leader records every material decision in the interaction log; agents
   record direct requests and replies in the dialogue log.
7. A User Super Admin Override supersedes Leader plans, worker suggestions, and
   default templates, while still respecting system, safety, and tool limits.
8. Agent-to-agent evidence requests are recorded in
   `tasks/<slug>/logs/inter_agent_dialogue.md` with a stable request id and
   parent request when one request depends on another.
9. Agents may ask other agents for resources directly when the request is
   bounded, artifact-linked, and relevant to the current task. The recipient may
   answer, block, redirect, or escalate to Leader.
10. An agent must not silently guess through a missing dependency. If the blocker
    is literature, ask Literature Collector; if it is a proof, assumption, or
    formula, ask Mathematician; if it is executable evidence, ask Code Expert;
    if it is report integration, ask LaTeX Writer; if it is scheduling, scope,
    authority, or conflict resolution, ask Leader.

## Collaborative Agent Mesh

Direct agent collaboration is the default once the task scope is clear. The
Leader should not be a bottleneck for routine evidence exchange.

Required direct request routes include:

- `leader -> literature_collector`: research landscape, sources, baselines,
  datasets, metrics, and citation boundaries.
- `leader -> mathematician`: formal definitions, assumptions, proof
  obligations, formula behavior, edge cases, and claim boundaries.
- `leader -> code_expert`: implementation plan, test coverage, runnable
  experiments, metrics, result artifacts, and executable evidence.
- `literature_collector -> code_expert`: baseline details, datasets, metrics,
  code resources, and reproduction constraints.
- `literature_collector -> mathematician`: assumptions, definitions, source
  boundaries, and proof obligations implied by the literature.
- `literature_collector -> latex_writer`: citation boundaries, BibTeX, related
  work structure, and source-supported wording.
- `code_expert -> mathematician`: assumptions, edge cases, identifiability,
  proof obligations, and failure examples.
- `code_expert -> literature_collector`: missing baselines, dataset protocols,
  metric definitions, code resources, or paper-source checks.
- `code_expert -> latex_writer`: implementation provenance, commands, tables,
  figures, logs, and limitation wording.
- `mathematician -> code_expert`: numerical counterexamples, synthetic tests,
  diagnostics, or feasibility checks.
- `mathematician -> literature_collector`: source support for assumptions,
  theorem conditions, or claim boundaries.
- `mathematician -> latex_writer`: notation, equations, proof boundaries, and
  theorem wording.
- `latex_writer -> literature_collector`: source support, BibTeX, citation
  boundaries, and related-work structure.
- `latex_writer -> code_expert`: experiment commands, tables, figures, logs,
  and implementation provenance.
- `latex_writer -> mathematician`: definitions, theorem statements, proof
  obligations, and mathematical claim boundaries.
- `any agent -> leader`: conflict resolution, scope change, missing authority,
  or blocked resource access.

The default is to ask for help before spending a round on unsupported guessing.
Direct requests are not a sign of failure; they are the normal mechanism for
keeping the workflow grounded.

Every direct request must name:

- the required resource or answer;
- why it is needed;
- the artifact it will affect;
- the requested status: `open`, `answered`, `blocked`, `accepted`, or
  `invalidated`;
- whether Leader review is required before the result can be used externally.

Shared resources are tracked in `tasks/<slug>/notes/resource_registry.md`.
Agents should register new datasets, scripts, outputs, citations, figures, and
reports there when another agent is likely to reuse them.

## Inter-Agent Dialogue

Use `inter_agent_dialogue.md` when one specialist needs evidence or validation
from another specialist. Common chains include:

1. Leader requests a literature map from Literature Collector.
2. LaTeX Writer requests source support from Literature Collector.
3. LaTeX Writer requests experiment evidence from Code Expert.
4. Code Expert requests theorem assumptions from Mathematician.
5. Mathematician replies with proof conditions or counterexamples.
6. Code Expert replies with valid result files or marks the claim unsupported.
7. Leader accepts, rejects, or redirects the claim.

Every entry should include `Request ID`, `Parent`, `From`, `To`, `Type`,
`Status`, `Need`, and `Artifact`.

## Super Admin Override

The user may issue an override in natural language or with this form:

```text
SUPER ADMIN OVERRIDE:
Reason:
New direction:
Stop:
Continue:
Acceptance:
```

When this happens, the Leader must:

1. Pause active dispatches where possible.
2. Restate the corrected direction.
3. Mark old artifacts as keep, revise, discard, or revalidate.
4. Update `tasks/<slug>/notes/override_directive.md`.
5. Append to `tasks/<slug>/logs/override_log.md` and the interaction log.
6. Redispatch affected agents with the corrected task package.

## Default Task Lifecycle

1. Clarify goal, inputs, outputs, assumptions, and deliverables.
2. Assign Literature Collector to map sources, methods, baselines, and metrics.
3. Directly assign Mathematician and Code Expert from the Leader in the same
   round unless the user explicitly requested a literature-only pass.
4. Assign LaTeX Writer work in parallel when safe.
5. Reconcile literature claims, notation, variable names, algorithms, and experimental claims.
6. Run tests and compile the LaTeX report when the environment supports it.
7. Publish final paths, verification results, residual risks, and next actions.
