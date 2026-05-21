#!/usr/bin/env python3
"""
Workflow bootstrap entrypoint backed by the structured request ledger.

Usage:
python3 tools/start_workflow.py --slug shortest-path-proof
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from multiagent_web import (
    ROOT,
    append_text,
    append_workflow_request,
    load_request_store,
    load_task_manifest,
    norm_status,
    read_text,
    retire_legacy_workflow_state,
    task_manifest_path,
    utc_now,
    workflow_store_path,
)

TASKS_DIR = ROOT / "tasks"
REQUIRED_BRIEF_SECTIONS = (
    "Problem Statement",
    "Literature Plan",
    "Mathematical Plan",
    "Algorithm Plan",
    "Experiment Plan",
    "Report Plan",
)


def ensure_file(path: Path, header: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header.rstrip() + "\n", encoding="utf-8", newline="\n")


def section_body(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    lines = markdown.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == marker:
            start = idx + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def missing_required_brief_sections(markdown: str) -> list[str]:
    missing: list[str] = []
    for heading in REQUIRED_BRIEF_SECTIONS:
        body = section_body(markdown, heading)
        normalized = " ".join(body.split()).lower()
        if not normalized or normalized.startswith("todo"):
            missing.append(heading)
    return missing


def bootstrap_request(base: Path, slug: str, manifest: dict[str, object]) -> str:
    requests = load_request_store(base)
    for row in requests:
        if (
            row.get("parent", "none") == "none"
            and row.get("to", "") == "leader"
            and norm_status(row.get("status", "")) in {"open", "queued", "running", "blocked"}
        ):
            return str(row.get("request_id", ""))

    title = str(manifest.get("title") or slug)
    artifact_paths = [
        "README.md",
        str(task_manifest_path(base).relative_to(base)),
        "notes/task_brief.md",
        "logs/workflow.db",
    ]
    return append_workflow_request(
        slug,
        parent="none",
        from_role="leader",
        to_role="leader",
        request_type="instruction",
        need=(
            f"Read the task brief and manifest for `{title}`, decompose the work, and create the next "
            "specialist requests in the structured ledger. Keep all work and outputs inside the task workspace."
        ),
        artifact="; ".join(artifact_paths),
        why="Workflow bootstrap must begin from the workflow database truth source instead of legacy json/md state files.",
        leader_review="yes",
    )


def start_workflow(slug: str) -> dict[str, object]:
    task_dir = TASKS_DIR / slug
    readme = task_dir / "README.md"
    if not readme.exists():
        return {"error": f"Task {slug} not found"}
    readme_text = read_text(readme)
    missing_sections = missing_required_brief_sections(readme_text)
    if len(missing_sections) == len(REQUIRED_BRIEF_SECTIONS):
        return {
            "error": (
                f"Task {slug} has an unfilled brief: all required planning sections are TODO. "
                "Fill README.md or notes/task_brief.md before starting the workflow."
            )
        }

    manifest = load_task_manifest(task_dir)
    manifest_path = task_manifest_path(task_dir)
    if not manifest_path.exists():
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    ensure_file(task_dir / "logs" / "agent_interactions.md", f"# Agent Interactions: {slug}")
    ensure_file(task_dir / "logs" / "override_log.md", f"# Override Log: {slug}")
    ensure_file(task_dir / "logs" / "run_log.md", f"# Run Log: {slug}")

    removed_legacy = retire_legacy_workflow_state(task_dir)
    load_request_store(task_dir)
    bootstrap_id = bootstrap_request(task_dir, slug, manifest)

    append_text(
        task_dir / "logs" / "agent_interactions.md",
        (
            f"\n## Workflow Bootstrap - {utc_now()}\n\n"
            "- From: workflow_bootstrap\n"
            "- To: leader\n"
            f"- Topic: bootstrap:{slug}\n"
            f"- Artifact: `{workflow_store_path(task_dir).relative_to(task_dir)}`\n"
            "- Summary:\n"
            f"  - Bootstrap request: `{bootstrap_id}`\n"
            f"  - Manifest: `{task_manifest_path(task_dir).relative_to(task_dir)}`\n"
            f"  - Removed legacy files: `{', '.join(removed_legacy) if removed_legacy else 'none'}`\n"
        ),
    )

    task_brief = readme_text
    title = str(manifest.get("title") or slug)
    instructions = (
        "# Workflow bootstrap complete\n\n"
        f"- Task: `{title}`\n"
        f"- Slug: `{slug}`\n"
        f"- Manifest: `{task_manifest_path(task_dir)}`\n"
        f"- Workflow database: `{workflow_store_path(task_dir)}`\n"
        f"- Bootstrap request: `{bootstrap_id}`\n"
        f"- Legacy files removed: `{', '.join(removed_legacy) if removed_legacy else 'none'}`\n\n"
        "Next action:\n"
        "1. Start the dashboard server if it is not running.\n"
        "2. Open the task in the GUI.\n"
        f"3. Run the open leader request `{bootstrap_id}`.\n\n"
        "Leader context snippet:\n\n"
        "```text\n"
        f"Task: {title}\n"
        f"Working directory: {task_dir}\n"
        f"Manifest: {task_manifest_path(task_dir)}\n"
        f"Bootstrap request: {bootstrap_id}\n\n"
        f"{task_brief[:1500]}\n"
        "```"
    )

    return {
        "status": "started",
        "slug": slug,
        "manifest": str(task_manifest_path(task_dir)),
        "workflow_db": str(workflow_store_path(task_dir)),
        "bootstrap_request_id": bootstrap_id,
        "removed_legacy_files": removed_legacy,
        "message": "Workflow bootstrap created in the workflow database.",
        "instructions": instructions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a task workflow from the structured request ledger.")
    parser.add_argument("--slug", required=True, help="Task slug")
    parser.add_argument("--json", action="store_true", help="Print JSON result instead of the text instructions.")
    args = parser.parse_args()

    result = start_workflow(args.slug)
    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(str(result["instructions"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
