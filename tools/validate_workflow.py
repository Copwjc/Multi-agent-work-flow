#!/usr/bin/env python3
"""Validate the multi-agent workflow scaffold.

The script intentionally uses only the Python standard library so it can run in
fresh Codex workspaces without installing dependencies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_TASK_PATHS = {
    "logs/inter_agent_dialogue.md",
    "logs/request_ledger.json",
    "notes/request_priorities.json",
}

REQUIRED_PATHS = [
    "README.md",
    "workflow/protocol.md",
    "docs/super_admin_override.md",
    "agents/leader.md",
    "agents/literature_collector.md",
    "agents/mathematician.md",
    "agents/code_expert.md",
    "agents/latex_writer.md",
    "tools/create_task.py",
    "tools/agent_process_worker.py",
    "tools/multiagent_gui.py",
    "tools/multiagent_web.py",
    "tools/web_gui/index.html",
    "tools/web_gui/style.css",
    "tools/web_gui/app.js",
    "templates/task_brief.md",
    "templates/agent_interactions.md",
    "templates/literature_review.md",
    "templates/leader_summary.md",
    "templates/override_directive.md",
    "templates/resource_registry.md",
    "docs/workflow_checklist.md",
    "logs/agent_interactions.md",
    "notes/leader_summary.md",
]

TASK_REQUIRED_PATHS = [
    "README.md",
    "notes/task_manifest.json",
    "report/main.tex",
    "notes/task_brief.md",
    "notes/literature_review.md",
    "notes/leader_summary.md",
    "notes/resource_registry.md",
    "logs/agent_interactions.md",
    "logs/override_log.md",
    "logs/run_log.md",
]


def load_manifest_paths(task_root: Path) -> list[str]:
    path = task_root / "notes" / "task_manifest.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    artifacts = raw.get("artifacts", {}) if isinstance(raw, dict) else {}
    if not isinstance(artifacts, dict):
        return []
    rel_paths: list[str] = []
    for value in artifacts.values():
        if isinstance(value, str):
            text = value.strip()
            if text and text not in LEGACY_TASK_PATHS:
                rel_paths.append(text)
            continue
        if isinstance(value, list):
            for item in value:
                text = str(item).strip()
                if text and text not in LEGACY_TASK_PATHS:
                    rel_paths.append(text)
    return sorted(set(rel_paths))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the multi-agent workflow scaffold.")
    parser.add_argument(
        "--task",
        help="Optional task slug under tasks/ to validate in addition to the top-level scaffold.",
    )
    return parser.parse_args()


def collect_missing_or_empty(root: Path, rel_paths: list[str]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    empty: list[str] = []

    for rel_path in rel_paths:
        path = root / rel_path
        if not path.exists():
            missing.append(rel_path)
            continue
        if path.is_file() and path.stat().st_size == 0:
            empty.append(rel_path)
    return missing, empty


def main() -> int:
    args = parse_args()
    missing, empty = collect_missing_or_empty(ROOT, REQUIRED_PATHS)

    if missing or empty:
        print("Workflow scaffold validation failed.")
        if missing:
            print("\nMissing paths:")
            for rel_path in missing:
                print(f"- {rel_path}")
        if empty:
            print("\nEmpty files:")
            for rel_path in empty:
                print(f"- {rel_path}")
        return 1

    print("Workflow scaffold validation passed.")
    print(f"Checked {len(REQUIRED_PATHS)} required paths.")

    if args.task:
        task_root = ROOT / "tasks" / args.task
        task_paths = sorted(set(TASK_REQUIRED_PATHS + load_manifest_paths(task_root)))
        task_missing, task_empty = collect_missing_or_empty(task_root, task_paths)
        if task_missing or task_empty:
            print(f"\nTask validation failed for {args.task}.")
            if task_missing:
                print("\nMissing task paths:")
                for rel_path in task_missing:
                    print(f"- {rel_path}")
            if task_empty:
                print("\nEmpty task files:")
                for rel_path in task_empty:
                    print(f"- {rel_path}")
            return 1
        print(f"Task validation passed for {args.task}.")
        print(f"Checked {len(task_paths)} task paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
