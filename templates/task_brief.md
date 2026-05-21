# Task Brief: {task_title}

- Slug: `{slug}`
- Created: `{created_at}`
- Workspace: `{task_path}`
- Agents: {agents}

## Goal

Define the mathematical problem, expected algorithmic deliverable, experiment plan, and LaTeX report target.

## Super Admin Override

The user can forcibly correct the task direction at any time. If that happens,
update `notes/override_directive.md`, append `logs/override_log.md`, and treat
the latest valid override as the task direction source of truth.

## Agent Roles

{agent_list}

## Problem Statement

TODO: state inputs, outputs, constraints, assumptions, and success criteria.

## Literature Plan

TODO: list search keywords, core venues, representative papers, baselines, datasets, metrics, and expected research routes.

## Mathematical Plan

TODO: list definitions, conjectures, lemmas, proof obligations, and edge cases.

## Algorithm Plan

TODO: describe data structures, complexity target, implementation language, and API shape.

## Experiment Plan

TODO: define baselines, generated or real datasets, metrics, random seeds, and acceptance thresholds.

## Report Plan

TODO: outline the final LaTeX paper sections, figures, tables, and citations.

## Done Criteria

- Literature review maintained in `notes/literature_review.md`.
- Mathematical reasoning checked against edge cases.
- Algorithm implemented under `experiments/src/`.
- Tests added under `experiments/tests/`.
- Experiments are reproducible from `experiments/`.
- Task structure declared in `notes/task_manifest.json`.
- Results and limitations summarized in `notes/leader_summary.md`.
- Paper draft maintained in `report/main.tex`.
- Any Super Admin Override is reflected in `notes/override_directive.md` and `logs/override_log.md`.
