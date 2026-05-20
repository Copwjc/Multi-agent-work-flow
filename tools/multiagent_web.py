#!/usr/bin/env python3
"""Local web dashboard for the multi-agent collaboration workflow.

The server uses only the Python standard library. It serves a small browser UI,
loads task logs, visualizes agent request chains, and lets the user append an
intervention, direct request, or Super Admin Override while a task is running.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# ── httpx（支持流式 HTTP + 连接池）──
try:
    import httpx
except ImportError:  # pragma: no cover - environment boundary
    httpx = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = Path(__file__).resolve().parent / "web_gui"
TOOLS_DIR = Path(__file__).resolve().parent


def _load_create_task() -> Any:
    """Lazy-load create_task module without affecting sys.path permanently."""
    spec = importlib.util.spec_from_file_location(
        "create_task", TOOLS_DIR / "create_task.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_create_task_module: Any | None = None


def _get_create_task() -> Any:
    global _create_task_module
    if _create_task_module is None:
        _create_task_module = _load_create_task()
    return _create_task_module
RUNS: dict[str, dict[str, Any]] = {}
RUN_LOCK = threading.Lock()

# ── 并发控制：最多同时运行 2 个 Agent ──
MAX_CONCURRENT_RUNS = 2
_RUN_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_RUNS)

# ── 共享 httpx 客户端（连接池复用）──
_shared_http_client: httpx.Client | None = None
_http_client_lock = threading.Lock()


def require_httpx() -> None:
    """Fail fast with setup instructions instead of installing at runtime."""
    if httpx is None:
        raise RuntimeError(
            "Missing dependency: httpx. Set up the local virtual environment first:\n"
            "  python3 -m venv .venv\n"
            "  ./.venv/bin/python -m pip install -r requirements.txt\n"
            "Then start the dashboard with:\n"
            "  ./.venv/bin/python tools/multiagent_web.py --port 8765"
        )


def get_http_client() -> httpx.Client:
    """获取共享的 httpx 客户端（懒初始化，连接池复用）。"""
    require_httpx()
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        with _http_client_lock:
            if _shared_http_client is None or _shared_http_client.is_closed:
                _shared_http_client = httpx.Client(
                    timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
                    limits=httpx.Limits(max_connections=MAX_CONCURRENT_RUNS, max_keepalive_connections=MAX_CONCURRENT_RUNS),
                    follow_redirects=True,
                )
    return _shared_http_client


DEFAULT_AGENTS_LIST = ("leader", "literature", "math", "algorithm", "experiment", "latex")
REQUEST_RETRY_COOLDOWN_SECONDS = 60
MAX_LOCAL_COMMANDS_PER_RUN = 10
LOCAL_COMMAND_TIMEOUT_SECONDS = 180
LOCAL_COMMAND_OUTPUT_LIMIT = 12000

ROLE_ALIASES = {
    "leader": "leader",
    "literature": "literature_collector",
    "collector": "literature_collector",
    "lit": "literature_collector",
    "literature_collector": "literature_collector",
    "math": "mathematician",
    "mathematician": "mathematician",
    "algorithm": "code_expert",
    "algorithm_designer": "code_expert",
    "experiment": "code_expert",
    "experiment_runner": "code_expert",
    "code": "code_expert",
    "code_expert": "code_expert",
    "latex": "latex_writer",
    "writer": "latex_writer",
    "latex_writer": "latex_writer",
}

SPECIALIST_ROLES = (
    "literature_collector",
    "mathematician",
    "code_expert",
    "latex_writer",
)

VALID_INTERVENTION_TYPES = {
    "instruction",
    "resource_request",
    "evidence_request",
    "literature_request",
    "baseline_request",
    "theory_check",
    "implementation_check",
    "source_check",
    "writing_gap",
    "super_admin_override",
}

WATCHED_TASK_PATHS = (
    "README.md",
    "logs/agent_interactions.md",
    "logs/inter_agent_dialogue.md",
    "logs/override_log.md",
    "notes/task_brief.md",
    "notes/leader_summary.md",
    "notes/resource_registry.md",
    "notes/request_priorities.json",
    "notes/override_directive.md",
    "experiments/analysis.md",
    "experiments/outputs/latest.json",
    "report/main.tex",
    "report/main.pdf",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_role(role: str) -> str:
    value = str(role or "").strip()
    return ROLE_ALIASES.get(value.lower(), value)


def task_root(slug: str) -> Path:
    safe_slug = Path(slug).name
    return ROOT / "tasks" / safe_slug


def task_version(slug: str) -> int:
    base = task_root(slug)
    version = 0
    for rel_path in WATCHED_TASK_PATHS:
        path = base / rel_path
        if path.exists():
            version = max(version, path.stat().st_mtime_ns)
    figures = base / "report" / "figures"
    if figures.exists():
        for path in figures.glob("*"):
            if path.is_file():
                version = max(version, path.stat().st_mtime_ns)
    agent_runs = base / "logs" / "agent_runs"
    if agent_runs.exists():
        for path in agent_runs.glob("*"):
            if path.is_file():
                version = max(version, path.stat().st_mtime_ns)
    return version


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def append_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def indent_block(value: str) -> str:
    lines = value.strip().splitlines() or ["(empty)"]
    return "\n".join(f"  {line}" for line in lines)


def table_cell(value: str, limit: int = 120) -> str:
    text = " ".join(value.strip().splitlines()) or "(empty)"
    text = text.replace("|", "\\|")
    return text[:limit]


def split_markdown_row(row: str) -> list[str]:
    row = row.strip()
    if not row.startswith("|") or not row.endswith("|"):
        return []
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    return [strip_markdown(cell) for cell in cells]


def strip_markdown(value: str) -> str:
    return value.replace("`", "").strip()


def parse_table(markdown: str, heading: str) -> list[dict[str, str]]:
    lines = markdown.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip().lower() == heading.lower():
            start = idx + 1
            break
    if start is None and heading.lower() == "## request ledger":
        for idx, line in enumerate(lines):
            cells = split_markdown_row(line)
            normalized = [cell.lower().replace(" ", "_") for cell in cells]
            if "request_id" in normalized and "status" in normalized:
                start = idx
                break
    if start is None:
        return []

    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("## ") and header is not None:
            break
        if not stripped.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = split_markdown_row(stripped)
        if not cells:
            continue
        if all(set(cell) <= {"-", " "} for cell in cells):
            continue
        if header is None:
            header = [normalize_table_header(cell) for cell in cells]
            continue
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))
        rows.append(dict(zip(header, cells[: len(header)])))
    return rows


def normalize_table_header(value: str) -> str:
    key = value.lower().replace(" / ", "_").replace("/", "_").replace(" ", "_")
    return {
        "artifact": "artifact_resource",
        "resource": "artifact_resource",
    }.get(key, key)


def parse_dialogue_entries(markdown: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    chunks = re.split(r"\n##\s+", "\n" + markdown)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk.startswith("REQ-"):
            continue
        lines = chunk.splitlines()
        request_id = strip_markdown(lines[0].strip())
        item: dict[str, str] = {"request_id": request_id, "body": chunk}
        for line in lines[1:]:
            match = re.match(r"-\s+([^:]+):\s*(.*)", line)
            if match:
                key = match.group(1).strip().lower().replace(" / ", "_").replace(" ", "_")
                item[key] = strip_markdown(match.group(2).strip())
        entries.append(item)
    return entries


def parse_activity_entries(markdown: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    chunks = re.split(r"\n##\s+", "\n" + markdown)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        first_line = chunk.splitlines()[0].strip()
        if not (
            first_line.startswith("Web Intervention - ")
            or first_line.startswith("GUI Event - ")
            or first_line.startswith("Round ")
            or first_line.startswith("Agent Run ")
        ):
            continue
        item: dict[str, str] = {"title": first_line, "body": chunk}
        for line in chunk.splitlines()[1:]:
            match = re.match(r"-\s+([^:]+):\s*(.*)", line)
            if match:
                key = match.group(1).strip().lower().replace(" ", "_")
                item[key] = strip_markdown(match.group(2).strip())
        summary_match = re.search(r"- Summary:\n((?:\s{2,}.+\n?)+)", chunk)
        if summary_match:
            item["summary"] = "\n".join(line.strip() for line in summary_match.group(1).splitlines())
        entries.append(item)
    return entries[-40:]


def extract_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line.removeprefix("# ").strip()
    return fallback


def list_tasks() -> list[dict[str, str]]:
    root = ROOT / "tasks"
    if not root.exists():
        return []
    tasks: list[dict[str, str]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if not (path / "README.md").exists():
            continue
        readme = read_text(path / "README.md")
        tasks.append({"slug": path.name, "title": extract_title(readme, path.name)})
    return tasks


def extract_agents_from_md(markdown: str) -> set[str]:
    for line in markdown.splitlines():
        if line.startswith("- Agents:"):
            raw = line.removeprefix("- Agents:").strip()
            return {canonical_role(a.strip()) for a in raw.split(",") if a.strip()}
    return set()


def request_priority_path(base: Path) -> Path:
    return base / "notes" / "request_priorities.json"


def load_request_priorities(base: Path) -> dict[str, int]:
    path = request_priority_path(base)
    if not path.exists():
        return {}
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {}
    values = raw.get("priorities", raw) if isinstance(raw, dict) else {}
    priorities: dict[str, int] = {}
    if not isinstance(values, dict):
        return priorities
    for request_id, value in values.items():
        try:
            priorities[str(request_id)] = int(value)
        except (TypeError, ValueError):
            continue
    return priorities


def save_request_priorities(base: Path, priorities: dict[str, int]) -> None:
    cleaned = {
        request_id: int(priority)
        for request_id, priority in sorted(priorities.items())
        if request_id and int(priority) != 0
    }
    write_text(
        request_priority_path(base),
        json.dumps(
            {
                "updated_at": utc_now(),
                "priorities": cleaned,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def load_task_state(slug: str) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)

    dialogue = read_text(base / "logs" / "inter_agent_dialogue.md")
    interactions = read_text(base / "logs" / "agent_interactions.md")
    resources_md = read_text(base / "notes" / "resource_registry.md")
    summary = read_text(base / "notes" / "leader_summary.md")
    brief = read_text(base / "notes" / "task_brief.md") or read_text(base / "README.md")

    requests = parse_table(dialogue, "## Request Ledger")
    priorities = load_request_priorities(base)
    for index, row in enumerate(requests):
        request_id = strip_markdown(row.get("request_id", ""))
        row["priority"] = priorities.get(request_id, 0)
        row["ledger_index"] = index
    resources = parse_table(resources_md, "## Resource Ledger")
    entries = parse_dialogue_entries(dialogue)
    activity = parse_activity_entries(interactions)
    brief_agents = extract_agents_from_md(brief)
    
    agents = sorted(
        {
            canonical_role(value)
            for row in requests
            for key in ("from", "to")
            for value in [row.get(key, "")]
            if value
        }
        | {
            canonical_role(value)
            for row in resources
            for value in [row.get("owner", "")]
            if value
        }
        | {"leader"}
        | brief_agents
    )
    agents = [agent for agent in agents if agent != "user"]
    profile_roles = list(("leader",) + SPECIALIST_ROLES)
    for agent in agents:
        if agent not in profile_roles:
            profile_roles.append(agent)
    agent_profiles = [
        {
            "role": role,
            "path": f"agents/{role}.md",
            "exists": (ROOT / "agents" / f"{role}.md").exists(),
        }
        for role in profile_roles
    ]

    return {
        "slug": slug,
        "version": task_version(slug),
        "title": extract_title(read_text(base / "README.md"), slug),
        "agents": agents,
        "agent_profiles": agent_profiles,
        "requests": requests,
        "dialogue_entries": entries,
        "activity": activity,
        "resources": resources,
        "brief": brief,
        "summary": summary,
        "interactions": interactions,
        "raw_dialogue": dialogue,
        "raw_resources": resources_md,
        "runs": list_task_runs(slug),
    }


def list_task_runs(slug: str) -> list[dict[str, Any]]:
    base = task_root(slug) / "logs" / "agent_runs"
    file_runs: list[dict[str, Any]] = []
    if base.exists():
        for path in sorted(base.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            file_runs.append(
                {
                    "run_id": path.stem,
                    "status": "logged",
                    "provider": "",
                    "started_at": "",
                    "finished_at": "",
                    "log_path": str(path.relative_to(task_root(slug))),
                }
            )
    with RUN_LOCK:
        live_runs = [run.copy() for run in RUNS.values() if run.get("slug") == slug]
    by_id: dict[str, dict[str, Any]] = {run["run_id"]: run for run in file_runs}
    for run in live_runs:
        by_id[run["run_id"]] = run
    return sorted(by_id.values(), key=lambda r: r.get("started_at", ""), reverse=True)[:20]


def update_request_priority_web(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)
    request_id = strip_markdown(str(payload.get("request_id") or ""))
    if not request_id:
        raise ValueError("request_id is required")

    dialogue_path = base / "logs" / "inter_agent_dialogue.md"
    rows = parse_table(read_text(dialogue_path), "## Request Ledger")
    known_ids = {strip_markdown(row.get("request_id", "")) for row in rows}
    if request_id not in known_ids:
        raise ValueError(f"unknown request_id: {request_id}")

    priorities = load_request_priorities(base)
    current = int(priorities.get(request_id, 0))
    action = str(payload.get("action") or "up").strip().lower()
    if action == "top":
        new_priority = max([0, *priorities.values()]) + 1
    elif action == "up":
        new_priority = current + 1
    elif action == "down":
        new_priority = current - 1
    elif action == "reset":
        new_priority = 0
    elif action == "set":
        try:
            new_priority = int(payload.get("priority"))
        except (TypeError, ValueError) as exc:
            raise ValueError("priority must be an integer") from exc
    else:
        raise ValueError("action must be one of: top, up, down, reset, set")
    new_priority = max(-99, min(999, new_priority))

    if new_priority == 0:
        priorities.pop(request_id, None)
    else:
        priorities[request_id] = new_priority
    save_request_priorities(base, priorities)
    append_text(
        base / "logs" / "agent_interactions.md",
        (
            f"\n## GUI Event - Priority Change - {utc_now()}\n\n"
            f"- Request ID: `{request_id}`\n"
            f"- Action: `{action}`\n"
            f"- Priority: `{new_priority}`\n"
        ),
    )
    return {
        "ok": True,
        "request_id": request_id,
        "priority": new_priority,
        "priorities": priorities,
    }


def load_run_log(slug: str, run_id: str, max_chars: int = 12000) -> dict[str, Any]:
    safe_run_id = Path(run_id).name
    if safe_run_id != run_id or not re.fullmatch(r"run-[A-Za-z0-9T+:-]+", safe_run_id):
        raise ValueError("invalid run id")
    base = task_root(slug)
    log_path = base / "logs" / "agent_runs" / f"{safe_run_id}.log"
    try:
        log_path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("invalid log path") from exc
    if log_path.resolve().parent != (base / "logs" / "agent_runs").resolve():
        raise ValueError("invalid log path")
    content = read_text(log_path)
    return {
        "run_id": safe_run_id,
        "log_path": str(log_path.relative_to(base)),
        "truncated": len(content) > max_chars,
        "log": content[-max_chars:],
    }


def next_request_id() -> str:
    timestamp = utc_now().replace("-", "").replace(":", "").replace("+00:00", "Z")
    return f"REQ-{timestamp}-{uuid.uuid4().hex[:4]}"


def ensure_dialogue_log(path: Path, title: str, slug: str) -> None:
    if path.exists():
        return
    write_text(
        path,
        (
            f"# Inter-Agent Dialogue: {title}\n\n"
            f"- Slug: `{slug}`\n"
            f"- Created: `{utc_now()}`\n\n"
            "## Request Ledger\n\n"
            "| Request ID | Parent | Status | From | To | Type | Need | Artifact / Resource |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n\n"
            "## Dialogue Entries\n"
        ),
    )


def add_dialogue_ledger_row(path: Path, row: str) -> None:
    content = read_text(path)
    marker = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    if marker not in content:
        append_text(path, "\n## Request Ledger Update\n\n" + row)
        return
    before, after = content.split(marker, 1)
    section_marker = "\n## Dialogue Entries"
    if section_marker in after:
        ledger_rows, rest = after.split(section_marker, 1)
        updated_rows = ledger_rows.rstrip() + "\n" + row.rstrip() + "\n\n"
        write_text(path, before + marker + updated_rows + "## Dialogue Entries" + rest)
        return
    write_text(path, before + marker + after.rstrip() + "\n" + row.rstrip() + "\n")


def norm_status(status: str) -> str:
    return str(status or "").replace("`", "").strip().lower()


def extract_request_id(text: str) -> str:
    match = re.search(r"Request ID:\s*`?([^\s`]+)`?", text)
    return match.group(1) if match else ""


def active_run_for_request(slug: str, request_id: str) -> dict[str, Any] | None:
    if not request_id:
        return None
    with RUN_LOCK:
        for run in RUNS.values():
            if (
                run.get("slug") == slug
                and run.get("request_id") == request_id
                and run.get("status") in {"queued", "running"}
            ):
                return run.copy()
    return None


def recent_failed_run_for_request(slug: str, request_id: str) -> dict[str, Any] | None:
    if not request_id:
        return None
    now = time.time()
    with RUN_LOCK:
        failed = [
            run.copy()
            for run in RUNS.values()
            if (
                run.get("slug") == slug
                and run.get("request_id") == request_id
                and run.get("status") == "failed"
                and isinstance(run.get("finished_ts"), (int, float))
                and now - float(run.get("finished_ts")) < REQUEST_RETRY_COOLDOWN_SECONDS
            )
        ]
    if not failed:
        return None
    return sorted(failed, key=lambda run: float(run.get("finished_ts", 0)), reverse=True)[0]


def update_request_status(path: Path, request_id: str, status: str, response_note: str = "") -> bool:
    """Update one request in the ledger and matching dialogue entry."""

    if not request_id or not path.exists():
        return False
    content = read_text(path)
    lines = content.splitlines()
    changed = False
    in_entry = False

    for idx, line in enumerate(lines):
        if line.startswith("|"):
            cells = line.split("|")
            if len(cells) >= 9:
                row_request_id = cells[1].replace("`", "").strip()
                if row_request_id == request_id:
                    cells[3] = f" `{status}` "
                    lines[idx] = "|".join(cells)
                    changed = True
        if re.match(r"^(##|###)\s+" + re.escape(request_id) + r"\s*$", line.strip()):
            in_entry = True
            continue
        if in_entry and re.match(r"^(##|###)\s+", line.strip()):
            in_entry = False
        if in_entry and line.strip().startswith("- Status:"):
            lines[idx] = f"- Status: `{status}`"
            changed = True

    if changed:
        updated = "\n".join(lines).rstrip() + "\n"
        if response_note:
            updated += (
                f"\n## State Update - {utc_now()}\n\n"
                f"- Request ID: `{request_id}`\n"
                f"- Status: `{status}`\n"
                "- Note:\n"
                f"{indent_block(response_note)}\n"
            )
        write_text(path, updated)
    return changed


def request_exists(
    dialogue_path: Path,
    *,
    from_role: str | None = None,
    to_role: str,
    request_type: str,
    parent: str | None = None,
    active_only: bool = True,
) -> bool:
    rows = parse_table(read_text(dialogue_path), "## Request Ledger")
    for row in rows:
        if from_role is not None and canonical_role(row.get("from", "")) != canonical_role(from_role):
            continue
        if canonical_role(row.get("to", "")) != canonical_role(to_role):
            continue
        if str(row.get("type", "")).replace("`", "").strip() != request_type:
            continue
        if parent is not None and strip_markdown(row.get("parent", "")) != parent:
            continue
        if active_only and norm_status(row.get("status", "")) not in {"open", "running", "queued"}:
            continue
        return True
    return False


def direct_request_exists(
    dialogue_path: Path,
    *,
    from_role: str,
    to_role: str,
    active_only: bool = False,
) -> bool:
    rows = parse_table(read_text(dialogue_path), "## Request Ledger")
    for row in rows:
        if canonical_role(row.get("from", "")) != canonical_role(from_role):
            continue
        if canonical_role(row.get("to", "")) != canonical_role(to_role):
            continue
        if active_only and norm_status(row.get("status", "")) not in {"open", "running", "queued"}:
            continue
        return True
    return False


def find_request_row(dialogue_path: Path, request_id: str) -> dict[str, str]:
    if not request_id:
        return {}
    rows = parse_table(read_text(dialogue_path), "## Request Ledger")
    for row in rows:
        if strip_markdown(row.get("request_id", "")) == request_id:
            return row
    return {}


def append_workflow_request(
    slug: str,
    *,
    parent: str,
    from_role: str,
    to_role: str,
    request_type: str,
    need: str,
    artifact: str,
    why: str,
    leader_review: str = "yes",
) -> str:
    base = task_root(slug)
    dialogue_path = base / "logs" / "inter_agent_dialogue.md"
    ensure_dialogue_log(dialogue_path, slug, slug)
    request_id = next_request_id()
    from_role = canonical_role(from_role)
    to_role = canonical_role(to_role)
    row = (
        f"| `{request_id}` | `{parent or 'none'}` | `open` | `{from_role}` | `{to_role}` | "
        f"`{request_type}` | {table_cell(need)} | `{artifact}` |\n"
    )
    add_dialogue_ledger_row(dialogue_path, row)
    append_text(
        dialogue_path,
        (
            f"\n## {request_id}\n\n"
            f"- Time: `{utc_now()}`\n"
            f"- Parent: `{parent or 'none'}`\n"
            f"- From: `{from_role}`\n"
            f"- To: `{to_role}`\n"
            f"- Type: `{request_type}`\n"
            "- Status: `open`\n"
            f"- Artifact / Resource: `{artifact}`\n"
            f"- Leader Review Required: {leader_review}\n"
            "- Need:\n"
            f"{indent_block(need)}\n"
            "- Why:\n"
            f"{indent_block(why)}\n"
            "- Response:\n"
            "  - TODO\n"
        ),
    )
    return request_id


def append_intervention(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)

    message = str(payload.get("message", "")).strip()
    if not message:
        raise ValueError("message is required")
    intervention_type = str(payload.get("type", "instruction")).strip()
    if intervention_type not in VALID_INTERVENTION_TYPES:
        raise ValueError(f"invalid intervention type: {intervention_type}")
    target = canonical_role(str(payload.get("target", "leader")).strip() or "leader")
    parent = str(payload.get("parent", "none")).strip() or "none"
    artifact = str(payload.get("artifact", "logs/agent_interactions.md")).strip()
    timestamp = utc_now()

    append_text(
        base / "logs" / "agent_interactions.md",
        (
            f"\n## Web Intervention - {timestamp}\n\n"
            f"- From: user\n"
            f"- To: {target}\n"
            f"- Topic: {intervention_type}\n"
            f"- Artifact: {artifact or '(none)'}\n"
            "- Summary:\n"
            f"{indent_block(message)}\n"
        ),
    )

    request_id = ""
    if intervention_type == "super_admin_override":
        directive = (
            f"\n## Active Override\n\n"
            f"- Triggered at: `{timestamp}`\n"
            "- User correction:\n"
            f"{indent_block(message)}\n"
            "- Reason:\n  TODO\n"
            "- New direction:\n  TODO\n"
            "- Stop doing:\n  TODO\n"
            "- Continue doing:\n  TODO\n"
            "- New acceptance criteria:\n  TODO\n"
        )
        write_text(base / "notes" / "override_directive.md", directive)
        append_text(
            base / "logs" / "override_log.md",
            (
                f"\n## Override - {timestamp}\n\n"
                "- User correction:\n"
                f"{indent_block(message)}\n"
                "- Leader action: pause current direction, mark impacted artifacts, redispatch affected agents.\n"
            ),
        )
    elif intervention_type != "instruction":
        request_id = next_request_id()
        dialogue_log = base / "logs" / "inter_agent_dialogue.md"
        ensure_dialogue_log(dialogue_log, slug, slug)
        row = (
            f"| `{request_id}` | `{parent}` | `open` | `user` | `{target}` | "
            f"`{intervention_type}` | {table_cell(message)} | `{artifact or 'user intervention'}` |\n"
        )
        add_dialogue_ledger_row(dialogue_log, row)
        append_text(
            dialogue_log,
            (
                f"\n## {request_id}\n\n"
                f"- Time: `{timestamp}`\n"
                f"- Parent: `{parent}`\n"
                "- From: `user`\n"
                f"- To: `{target}`\n"
                f"- Type: `{intervention_type}`\n"
                "- Status: `open`\n"
                f"- Artifact / Resource: `{artifact or 'user intervention'}`\n"
                "- Leader Review Required: yes\n"
                "- Need:\n"
                f"{indent_block(message)}\n"
                "- Response:\n"
                "  - TODO\n"
            ),
        )

    return {"ok": True, "request_id": request_id, "timestamp": timestamp}


def create_task_web(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a new task workspace via the web API."""
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("title is required")
    slug = str(payload.get("slug", "")).strip() or None
    agents_raw = str(payload.get("agents", "")).strip()
    agents_str = agents_raw if agents_raw else ",".join(DEFAULT_AGENTS_LIST)
    force = bool(payload.get("force", False))

    ct = _get_create_task()
    args = ct.parse_args([
        title,
        "--root", str(ROOT),
        "--agents", agents_str,
    ])
    if slug:
        args.slug = slug
    args.force = force
    args.dry_run = False

    rc = ct.create_workspace(args)
    actual_slug = slug or ct.slugify(title)
    task_path = ROOT / "tasks" / actual_slug

    if rc == 2:
        raise FileExistsError(
            f"Task '{actual_slug}' already exists. Set force=true to overwrite."
        )

    # Count created files
    created = []
    for child in task_path.rglob("*"):
        if child.is_file():
            created.append(str(child.relative_to(ROOT)))

    return {
        "ok": True,
        "slug": actual_slug,
        "title": title,
        "path": str(task_path),
        "files_count": len(created),
        "files": created,
    }


def normalize_agent_id(value: str) -> str:
    role = re.sub(r"[^A-Za-z0-9_ -]+", "", value.strip()).lower()
    role = re.sub(r"[\s-]+", "_", role).strip("_")
    if not role:
        raise ValueError("agent role is required")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role):
        raise ValueError("agent role must start with a letter and use letters, numbers, or underscores")
    if role in {"user", "workflow", "none", "todo"}:
        raise ValueError(f"reserved agent role: {role}")
    return canonical_role(role)


def add_agent_to_markdown(path: Path, role: str, title: str) -> None:
    if not path.exists():
        return
    content = read_text(path)
    lines = content.splitlines()
    updated_lines: list[str] = []
    found_agents = False
    for line in lines:
        if line.startswith("- Agents:"):
            found_agents = True
            raw = line.removeprefix("- Agents:").strip()
            agents = [canonical_role(a.strip()) for a in raw.split(",") if a.strip()]
            if role not in agents:
                agents.append(role)
            line = "- Agents: " + ", ".join(agents)
        updated_lines.append(line)

    content = "\n".join(updated_lines).rstrip() + "\n"
    if not found_agents:
        content += f"\n- Agents: {role}\n"

    marker = "## Agent Roles"
    role_line = f"- `{role}`: {title or role}"
    if marker not in content:
        content += f"\n{marker}\n\n{role_line}\n"
    elif f"`{role}`" not in content:
        before, after = content.split(marker, 1)
        section = marker + after
        next_section = re.search(r"\n## (?!Agent Roles)", section)
        if next_section:
            insert_at = next_section.start()
            section = section[:insert_at].rstrip() + f"\n{role_line}\n" + section[insert_at:]
        else:
            section = section.rstrip() + f"\n{role_line}\n"
        content = before + section

    write_text(path, content)


def create_agent_web(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)
    role = normalize_agent_id(str(payload.get("role") or payload.get("name") or ""))
    title = str(payload.get("title", "")).strip() or role.replace("_", " ").title()
    mission = str(payload.get("mission", "")).strip() or (
        "Support the Leader by completing assigned requests, writing durable artifacts, "
        "and marking the request as answered when done."
    )
    overwrite = bool(payload.get("overwrite", False))

    agents_dir = ROOT / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    profile_path = agents_dir / f"{role}.md"
    created_profile = False
    if not profile_path.exists() or overwrite:
        write_text(
            profile_path,
            (
                f"# {title}\n\n"
                f"- Role ID: `{role}`\n"
                f"- Created: `{utc_now()}`\n\n"
                "## Mission\n\n"
                f"{mission}\n\n"
                "## Operating Rules\n\n"
                "- Treat the Leader as the orchestration owner.\n"
                "- Work only on requests addressed to this role unless the Leader redirects you.\n"
                "- Save durable outputs to task artifacts when possible.\n"
                "- Update `logs/inter_agent_dialogue.md` from `open` to `answered` after completing a request.\n"
            ),
        )
        created_profile = True

    add_agent_to_markdown(base / "notes" / "task_brief.md", role, title)
    add_agent_to_markdown(base / "README.md", role, title)
    append_text(
        base / "logs" / "agent_interactions.md",
        (
            f"\n## Agent Added: {role}\n\n"
            f"- Time: `{utc_now()}`\n"
            "- From: `web_gui`\n"
            "- To: `leader`\n"
            "- Type: `agent_added`\n"
            f"- Profile: `agents/{role}.md`\n"
            f"- Title: {title}\n"
            f"- Mission: {mission}\n"
        ),
    )

    return {
        "ok": True,
        "role": role,
        "title": title,
        "profile": f"agents/{role}.md",
        "created_profile": created_profile,
        "task": slug,
    }


def normalize_chat_url(base_url: str) -> str:
    """Convert a base URL to the full /chat/completions endpoint.

    Handles:
      - already ends with /chat/completions  → return as-is
      - ends with /v1 or /v3 or /vN         → append /chat/completions
      - anything else                        → append /v1/chat/completions
    """
    value = base_url.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    import re as _re
    if _re.search(r"/v\d+$", value):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def build_runner_prompt(slug: str, user_prompt: str, mode: str, role: str) -> str:
    role = canonical_role(role)
    base = task_root(slug)
    
    # Global context
    protocol = read_text(ROOT / "workflow" / "protocol.md")
    
    # Task context
    brief = read_text(base / "notes" / "task_brief.md") or read_text(base / "README.md")
    summary = read_text(base / "notes" / "leader_summary.md")
    dialogue = read_text(base / "logs" / "inter_agent_dialogue.md")
    resources = read_text(base / "notes" / "resource_registry.md")
    
    role_profile = ""
    if role:
        agent_file = ROOT / "agents" / f"{role}.md"
        if agent_file.exists():
            role_profile = f"Your specific Agent Role Profile:\n{read_text(agent_file)}\n\n"
        else:
            role_profile = (
                f"You are acting as the '{role}' agent. No matching file was found under "
                f"`agents/`, so follow the Global Workflow Protocol strictly.\n\n"
            )
    available_roles = list(("leader",) + SPECIALIST_ROLES)
    for agent in sorted(extract_agents_from_md(brief)):
        if agent not in available_roles and agent != "user":
            available_roles.append(agent)
    dispatch_roles = ", ".join(role for role in available_roles if role != "leader")
    orchestration_rule = (
        "Leader execution rule: do not stop at a summary. When the next step belongs to a "
        "specialist, append one or more open rows to `logs/inter_agent_dialogue.md` for "
        f"these available specialist agents: {dispatch_roles}. Each row must include a concrete "
        "Need, Artifact / Resource, and Status `open`. Also append a short decision record "
        "to `logs/agent_interactions.md`."
        if role == "leader"
        else (
            "Specialist execution rule: process the assigned Request ID when present. Update "
            "`logs/inter_agent_dialogue.md` from `open` to `answered`, write any durable work "
            "to the relevant artifact, and ask another agent for help before guessing through a "
            "missing dependency. Literature gaps go to Literature Collector, proof/formula gaps "
            "go to Mathematician, executable evidence gaps go to Code Expert, report-integration "
            "gaps go to LaTeX Writer, and scheduling/conflict gaps go to Leader. For algorithmic "
            "work, Code Expert and Mathematician must exchange at least one direct check."
        )
    )
    mode_rule = (
        "Return a concrete coding plan only. Do not edit files."
        if mode == "plan_only"
        else (
            "Execute the requested workflow in the task workspace and update artifacts as needed.\n\n"
            "*** CRITICAL: FILE MODIFICATION PERMISSION GRANTED ***\n"
            "To save, create, or overwrite a file, you MUST wrap the file contents in the following exact format:\n\n"
            "```file path=\"relative/path/to/file.ext\"\n"
            "Your file contents go here...\n"
            "```\n\n"
            "If you do not use this exact syntax, your files will NOT be saved to disk. "
            "You can save multiple files by providing multiple such blocks. Paths must be relative to the task root.\n\n"
            "*** LOCAL EXECUTION PERMISSION GRANTED WITH WHITELIST ***\n"
            "After writing files, you may request local verification with explicit command blocks. "
            "Use only this format, one command per line:\n\n"
            "```command cwd=\".\"\n"
            "python3 -m unittest discover -s tests -v\n"
            "```\n\n"
            "Allowed commands include task-local Python scripts, `python3 -m unittest`, `python3 -m pytest`, "
            "`pytest`, and LaTeX commands such as `pdflatex`, `xelatex`, `lualatex`, and `bibtex`. "
            "Do not use shell operators, redirection, secrets, network downloads, package installation, or destructive commands."
        )
    )
    return (
        f"You are an AI agent attached to a multi-agent workflow.\n"
        f"Your assigned role: {role}\n\n"
        f"{role_profile}"
        f"Task slug: {slug}\n"
        f"Task path: {base}\n\n"
        "User request:\n"
        f"{user_prompt.strip()}\n\n"
        "Global Workflow Protocol:\n"
        f"{protocol}\n\n"
        "Task brief:\n"
        f"{brief[-4000:]}\n\n"
        "Leader summary:\n"
        f"{summary[-3000:]}\n\n"
        "Inter-agent dialogue:\n"
        f"{dialogue[-4000:]}\n\n"
        "Resource registry:\n"
        f"{resources[-3000:]}\n\n"
        "Output requirements:\n"
        "- Be concrete and artifact-oriented.\n"
        "- Mention files that should be changed or inspected.\n"
        "- If executing, keep logs and avoid secrets.\n"
        f"- Always use available role ids: {', '.join(available_roles)}.\n"
        f"- {orchestration_rule}\n"
        "--- IMPORTANT INSTRUCTIONS ---\n"
        f"Mode: {mode}\n"
        f"{mode_rule}\n"
        "WARNING: The dialogue ledger MUST be written to EXACTLY `logs/inter_agent_dialogue.md` (NOT notes/).\n"
        "WARNING: The resource registry MUST be written to EXACTLY `notes/resource_registry.md`.\n"
    )


def start_agent_run(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)
    protocol = str(payload.get("protocol", "openai")).strip()
    role = canonical_role(str(payload.get("role", "leader")).strip() or "leader")
    mode = str(payload.get("mode", "plan_only")).strip()
    if mode not in {"plan_only", "execute"}:
        raise ValueError("mode must be plan_only or execute")
    user_prompt = str(payload.get("prompt", "")).strip()
    if not user_prompt:
        raise ValueError("prompt is required")
    if protocol != "openai":
        raise ValueError(f"unknown protocol: {protocol}")
    request_id = extract_request_id(user_prompt)
    active_run = active_run_for_request(slug, request_id)
    if active_run is not None:
        raise ValueError(
            f"request {request_id} is already {active_run.get('status')} "
            f"as {active_run.get('run_id')}"
        )
    recent_failed = recent_failed_run_for_request(slug, request_id)
    if recent_failed is not None:
        raise ValueError(
            f"request {request_id} failed recently as {recent_failed.get('run_id')}; "
            f"wait {REQUEST_RETRY_COOLDOWN_SECONDS}s before retrying"
        )

    run_id = f"run-{utc_now().replace('-', '').replace(':', '').replace('+00:00', 'Z')}-{uuid.uuid4().hex[:6]}"
    run_dir = base / "logs" / "agent_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{run_id}.log"
    run = {
        "run_id": run_id,
        "slug": slug,
        "protocol": protocol,
        "role": role,
        "mode": mode,
        "status": "queued",
        "request_id": request_id,
        "started_at": utc_now(),
        "finished_at": "",
        "log_path": str(log_path.relative_to(base)),
        "progress": "排队中…",
        "progress_ts": 0.0,
    }
    with RUN_LOCK:
        RUNS[run_id] = run

    # 将 run_id 传入 payload，供 streaming 进度回调使用
    payload["_run_id"] = run_id

    # 并发限制：超过 MAX_CONCURRENT_RUNS 时等待
    acquired = _RUN_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        run["status"] = "queued"
        run["progress"] = f"排队等待…（并发上限 {MAX_CONCURRENT_RUNS}，当前已满）"
        update_run(run_id, status="queued", progress=run["progress"])
        # 启动线程，线程内部会阻塞等待信号量
        thread = threading.Thread(
            target=_run_with_semaphore,
            args=(run_id, slug, protocol, role, mode, user_prompt, payload, log_path),
            daemon=True,
        )
    else:
        _RUN_SEMAPHORE.release()  # 释放刚获取的，由 worker 函数管理
        thread = threading.Thread(
            target=_run_with_semaphore,
            args=(run_id, slug, protocol, role, mode, user_prompt, payload, log_path),
            daemon=True,
        )
    thread.start()
    return run


def _run_with_semaphore(
    run_id: str,
    slug: str,
    protocol: str,
    role: str,
    mode: str,
    user_prompt: str,
    payload: dict[str, Any],
    log_path: Path,
) -> None:
    """包装 run_agent_worker，通过信号量控制并发。"""
    update_run(run_id, progress="等待执行…", progress_ts=time.time())
    _RUN_SEMAPHORE.acquire()
    try:
        update_run(run_id, progress="正在执行…", progress_ts=time.time())
        run_agent_worker(run_id, slug, protocol, role, mode, user_prompt, payload, log_path)
    finally:
        _RUN_SEMAPHORE.release()


def update_run(run_id: str, **values: Any) -> None:
    with RUN_LOCK:
        if run_id in RUNS:
            RUNS[run_id].update(values)


def log_run(log_path: Path, text: str) -> None:
    append_text(log_path, text)


def extract_and_write_files(base_path: Path, text: str) -> str:
    import re
    pattern = r'```(?:file\s+path|filepath)="([^"]+)"\n(.*?)\n```'
    matches = list(re.finditer(pattern, text, re.DOTALL))
    edited = []
    skipped = []
    protected_paths = {
        "logs/inter_agent_dialogue.md",
        "logs/agent_interactions.md",
    }
    for match in matches:
        rel_path = match.group(1).strip()
        file_content = match.group(2)
        normalized_rel = Path(rel_path).as_posix()
        if normalized_rel in protected_paths:
            skipped.append(rel_path)
            continue
        safe_path = (base_path / rel_path).resolve()
        try:
            safe_path.relative_to(base_path.resolve())
            write_text(safe_path, file_content)
            edited.append(rel_path)
        except ValueError:
            pass
    notices = []
    if edited:
        notices.append("**System Notice:** 自动写入了以下文件：\n" + "\n".join(f"- `{p}`" for p in edited))
    if skipped:
        notices.append(
            "**System Notice:** 跳过了受保护的工作流台账文件，状态机将负责更新：\n"
            + "\n".join(f"- `{p}`" for p in skipped)
        )
    if notices:
        return text + "\n\n" + "\n\n".join(notices)
    return text


def _resolve_task_path(base_path: Path, cwd: str) -> Path:
    target = (base_path / (cwd or ".")).resolve()
    target.relative_to(base_path.resolve())
    return target


def _is_task_local_path(base_path: Path, cwd: Path, value: str) -> bool:
    if not value or value.startswith("-"):
        return True
    path = Path(value)
    if path.is_absolute():
        candidate = path.resolve()
    else:
        candidate = (cwd / path).resolve()
    try:
        candidate.relative_to(base_path.resolve())
        return True
    except ValueError:
        return False


def validate_local_command(base_path: Path, cwd: Path, args: list[str]) -> str | None:
    if not args:
        return "empty command"
    program = Path(args[0]).name
    lowered = program.lower()
    if any(token in {";", "&&", "||", "|", ">", ">>", "<"} for token in args):
        return "shell operators and redirection are not allowed"

    python_names = {"python", "python3"}
    if lowered in python_names or args[0] in {sys.executable, "./.venv/bin/python", ".venv/bin/python"}:
        if len(args) >= 3 and args[1] == "-m" and args[2] in {"unittest", "pytest"}:
            return None
        if len(args) >= 2 and args[1].endswith(".py") and _is_task_local_path(base_path, cwd, args[1]):
            return None
        return "python commands must run a task-local .py script, `-m unittest`, or `-m pytest`"

    if lowered == "pytest":
        return None

    if lowered in {"pdflatex", "xelatex", "lualatex", "bibtex"}:
        for arg in args[1:]:
            if arg.startswith("-"):
                continue
            # bibtex commonly receives a basename rather than a path.
            if lowered == "bibtex" and "/" not in arg and "\\" not in arg:
                continue
            if not _is_task_local_path(base_path, cwd, arg):
                return f"path outside task is not allowed: {arg}"
        return None

    return f"command is not allowed: {args[0]}"


def execute_local_command_blocks(base_path: Path, text: str) -> str:
    pattern = r'```(?:command|cmd)(?:\s+cwd="([^"]*)")?\n(.*?)\n```'
    blocks = list(re.finditer(pattern, text, re.DOTALL))
    if not blocks:
        return text

    notices: list[str] = []
    command_count = 0
    for block in blocks[:MAX_LOCAL_COMMANDS_PER_RUN]:
        cwd_raw = block.group(1) or "."
        try:
            cwd = _resolve_task_path(base_path, cwd_raw)
        except ValueError:
            notices.append(f"### Rejected command block\n\n- cwd outside task: `{cwd_raw}`")
            continue

        for raw_line in block.group(2).splitlines():
            command = raw_line.strip()
            if not command or command.startswith("#"):
                continue
            command_count += 1
            if command_count > MAX_LOCAL_COMMANDS_PER_RUN:
                notices.append("### Skipped remaining commands\n\n- reason: command limit reached")
                break
            try:
                args = shlex.split(command)
            except ValueError as exc:
                notices.append(f"### Rejected command\n\n- command: `{command}`\n- reason: {exc}")
                continue
            reason = validate_local_command(base_path, cwd, args)
            if reason:
                notices.append(f"### Rejected command\n\n- command: `{command}`\n- reason: {reason}")
                continue
            started = utc_now()
            try:
                completed = subprocess.run(
                    args,
                    cwd=str(cwd),
                    text=True,
                    capture_output=True,
                    timeout=LOCAL_COMMAND_TIMEOUT_SECONDS,
                    check=False,
                )
                stdout = (completed.stdout or "")[-LOCAL_COMMAND_OUTPUT_LIMIT:]
                stderr = (completed.stderr or "")[-LOCAL_COMMAND_OUTPUT_LIMIT:]
                notices.append(
                    "\n".join(
                        [
                            "### Local Command",
                            "",
                            f"- started: `{started}`",
                            f"- cwd: `{cwd.relative_to(base_path.resolve()) or '.'}`",
                            f"- command: `{command}`",
                            f"- exit_code: `{completed.returncode}`",
                            "",
                            "stdout:",
                            "```text",
                            stdout.rstrip() or "(empty)",
                            "```",
                            "",
                            "stderr:",
                            "```text",
                            stderr.rstrip() or "(empty)",
                            "```",
                        ]
                    )
                )
            except subprocess.TimeoutExpired as exc:
                notices.append(
                    "\n".join(
                        [
                            "### Local Command Timeout",
                            "",
                            f"- command: `{command}`",
                            f"- timeout_seconds: `{LOCAL_COMMAND_TIMEOUT_SECONDS}`",
                            "",
                            "stdout:",
                            "```text",
                            (exc.stdout or "")[-LOCAL_COMMAND_OUTPUT_LIMIT:] if isinstance(exc.stdout, str) else "(empty)",
                            "```",
                            "",
                            "stderr:",
                            "```text",
                            (exc.stderr or "")[-LOCAL_COMMAND_OUTPUT_LIMIT:] if isinstance(exc.stderr, str) else "(empty)",
                            "```",
                        ]
                    )
                )
    if notices:
        return text + "\n\n## Local Execution Results\n\n" + "\n\n".join(notices)
    return text


def auto_feedback_to_leader(slug: str, role: str, prompt: str) -> None:
    role = canonical_role(role)
    request_id = extract_request_id(prompt)
    if not request_id:
        return

    base = task_root(slug)
    dialogue_path = base / "logs" / "inter_agent_dialogue.md"
    update_request_status(
        dialogue_path,
        request_id,
        "answered",
        f"{role} completed the assigned runner task.",
    )


def advance_workflow_state(
    slug: str,
    *,
    role: str,
    prompt: str,
    run_id: str,
    output_artifact: str,
) -> list[str]:
    """Deterministically move the task to the next agent requests."""

    role = canonical_role(role)
    base = task_root(slug)
    dialogue_path = base / "logs" / "inter_agent_dialogue.md"
    ensure_dialogue_log(dialogue_path, slug, slug)

    current_request_id = extract_request_id(prompt)
    current_request = find_request_row(dialogue_path, current_request_id) if current_request_id else {}
    current_from = canonical_role(current_request.get("from", ""))
    if current_request_id:
        update_request_status(
            dialogue_path,
            current_request_id,
            "answered",
            f"{role} completed `{run_id}` and wrote `{output_artifact}`.",
        )

    parent = current_request_id or run_id
    created: list[str] = []

    def create_once(to_role: str, request_type: str, need: str, artifact: str, why: str) -> None:
        if request_exists(
            dialogue_path,
            from_role=role,
            to_role=to_role,
            request_type=request_type,
            parent=parent,
            active_only=False,
        ):
            return
        request_id = append_workflow_request(
            slug,
            parent=parent,
            from_role=role,
            to_role=to_role,
            request_type=request_type,
            need=need,
            artifact=artifact,
            why=why,
        )
        created.append(request_id)

    if role == "leader":
        create_once(
            "literature_collector",
            "literature_request",
            (
                "Map the research landscape for the task: representative papers, mainstream routes, "
                "baselines, datasets, metrics, source boundaries, and recommended direction."
            ),
            "notes/literature_review.md; report/references.bib",
            "The workflow requires literature grounding before math, implementation, and report claims are finalized.",
        )
        create_once(
            "mathematician",
            "theory_check",
            (
                "Start formal grounding directly from the user brief and current task state: define variables, "
                "assumptions, claim boundaries, expected formula behavior, and edge cases. Ask Literature Collector "
                "for source support when assumptions need citations, and ask Code Expert for executable checks when "
                "a formula or invariant should be validated numerically."
            ),
            "notes/math_model.md; notes/open_questions.md",
            "Leader must give Mathematician direct ownership of the mathematical basis instead of routing all theory through literature.",
        )
        create_once(
            "code_expert",
            "implementation_check",
            (
                "Start the runnable implementation and validation plan directly from the user brief and current task state: "
                "identify code paths, tests, experiment commands, metrics, and expected artifacts. Ask Mathematician for "
                "formal assumptions before hard-coding algorithm claims, and ask Literature Collector for missing baseline "
                "or dataset protocol evidence."
            ),
            "src/solution.py; tests/test_solution.py; experiments/run_experiment.py; experiments/analysis.md",
            "Leader must give Code Expert direct ownership of executable evidence instead of waiting for collector relay.",
        )
    elif role == "literature_collector":
        if current_from == "code_expert":
            create_once(
                "code_expert",
                "baseline_request",
                (
                    "Use the returned literature baseline, dataset, metric, and source-boundary information "
                    "to revise the implementation or experiment plan. If any mathematical assumption is still "
                    "unclear, request a theory_check from Mathematician before reporting results."
                ),
                "notes/algorithm_survey.md; experiments/analysis.md",
                "Code Expert requested literature support and should continue from the sourced baseline map.",
            )
        elif current_from == "latex_writer":
            create_once(
                "latex_writer",
                "source_check",
                (
                    "Integrate the returned citations, source boundaries, and BibTeX entries into the report; "
                    "mark unsupported claims instead of filling them from memory."
                ),
                "report/main.tex; report/references.bib",
                "LaTeX Writer requested source support and needs a source-grounded writing pass.",
            )
        else:
            create_once(
                "mathematician",
                "theory_check",
                (
                    "Convert the literature-backed task direction into formal definitions, assumptions, "
                    "claim boundaries, proof obligations, and edge cases."
                ),
                "notes/math_model.md; notes/open_questions.md",
                "The implementation and report need explicit assumptions and non-overclaiming boundaries.",
            )
            create_once(
                "code_expert",
                "baseline_request",
                (
                    "Design the implementation and experiment plan from the literature map: baselines, "
                    "metrics, reproducible commands, test coverage, and expected artifacts."
                ),
                "src/solution.py; tests/test_solution.py; experiments/run_experiment.py; experiments/analysis.md",
                "The workflow must move from research map to runnable validation instead of stopping at literature review.",
            )
            create_once(
                "latex_writer",
                "source_check",
                (
                    "Prepare report source boundaries, related-work structure, citation expectations, and "
                    "unsupported-claim markers from the literature map."
                ),
                "report/main.tex; report/references.bib",
                "The report should receive source boundaries before final writing.",
            )
    elif role == "mathematician":
        if current_from == "latex_writer":
            create_once(
                "latex_writer",
                "theory_check",
                (
                    "Integrate the returned definitions, theorem/proof boundaries, notation, and unsupported "
                    "mathematical claims into the report."
                ),
                "report/main.tex",
                "LaTeX Writer requested mathematical support and needs checked wording.",
            )
        else:
            if not direct_request_exists(
                dialogue_path,
                from_role="mathematician",
                to_role="literature_collector",
                active_only=False,
            ):
                create_once(
                    "literature_collector",
                    "source_check",
                    (
                        "Check whether the mathematical assumptions, theorem conditions, and claim boundaries "
                        "are supported by the literature map; return source-backed caveats or missing references."
                    ),
                    "notes/literature_review.md; notes/math_model.md",
                    "Mathematical assumptions should be grounded in source evidence when the report cites them.",
                )
            create_once(
                "code_expert",
                "implementation_check",
                (
                    "Implement or revise the algorithm using the mathematical assumptions and edge cases; "
                    "add tests that cover the stated proof obligations, expected formula outputs, and failure modes."
                ),
                "src/solution.py; tests/test_solution.py",
                "The math output must be turned into executable, testable behavior.",
            )
    elif role == "code_expert":
        if not direct_request_exists(
            dialogue_path,
            from_role="code_expert",
            to_role="literature_collector",
            active_only=False,
        ):
            create_once(
                "literature_collector",
                "baseline_request",
                (
                    "Provide missing or confirmatory baseline, dataset, metric, source, and reproduction-protocol "
                    "details needed by the implementation and experiments. Mark unavailable evidence explicitly."
                ),
                "notes/algorithm_survey.md; experiments/analysis.md",
                "Code Expert should ask Literature Collector for missing literature or benchmark support before guessing.",
            )
        has_math_validation = request_exists(
            dialogue_path,
            from_role="code_expert",
            to_role="mathematician",
            request_type="theory_check",
            active_only=False,
        )
        if not has_math_validation:
            create_once(
                "mathematician",
                "theory_check",
                (
                    "Validate the implemented algorithm against the formal assumptions: identify required invariants, "
                    "derive expected outputs for edge cases or synthetic checks, and flag any claim that the code or "
                    "experiment does not mathematically justify."
                ),
                "notes/math_model.md; tests/test_solution.py; experiments/analysis.md",
                "Algorithm design must be checked against rigorous mathematical assumptions before report integration.",
            )
        else:
            create_once(
                "latex_writer",
                "evidence_request",
                (
                    "Integrate the mathematically checked implementation, tests, experiment commands, result files, "
                    "and limitations into the report without unsupported claims."
                ),
                "report/main.tex; notes/leader_summary.md",
                "The report should only use claims backed by code, tests, experiments, proof, or sources.",
            )
    elif role == "latex_writer":
        requested_support = False
        if not direct_request_exists(
            dialogue_path,
            from_role="latex_writer",
            to_role="literature_collector",
            active_only=False,
        ):
            create_once(
                "literature_collector",
                "source_check",
                (
                    "Provide citation support, BibTeX, source boundaries, and related-work wording for all report claims. "
                    "Flag claims that lack source support."
                ),
                "report/main.tex; report/references.bib",
                "The report cannot finalize source-backed claims without Literature Collector support.",
            )
            requested_support = True
        if not direct_request_exists(
            dialogue_path,
            from_role="latex_writer",
            to_role="mathematician",
            active_only=False,
        ):
            create_once(
                "mathematician",
                "theory_check",
                (
                    "Provide checked definitions, theorem statements, formula derivations, proof boundaries, and "
                    "unsupported mathematical-claim markers for the report."
                ),
                "report/main.tex; notes/math_model.md",
                "The report cannot finalize mathematical claims without Mathematician support.",
            )
            requested_support = True
        if not direct_request_exists(
            dialogue_path,
            from_role="latex_writer",
            to_role="code_expert",
            active_only=False,
        ):
            create_once(
                "code_expert",
                "evidence_request",
                (
                    "Provide experiment commands, result files, tables, figures, implementation paths, and limitations "
                    "for report integration."
                ),
                "report/main.tex; experiments/analysis.md; report/figures/",
                "The report cannot finalize implementation or experiment claims without Code Expert support.",
            )
            requested_support = True
        if not requested_support:
            create_once(
                "leader",
                "final_review",
                (
                    "Review all open or blocked requests, check consistency across literature, math, code, "
                    "experiments, and report, then decide whether the task is accepted or needs rework."
                ),
                "notes/leader_summary.md; logs/inter_agent_dialogue.md; report/main.tex",
                "Final delivery needs Leader arbitration and explicit residual-risk handling.",
            )

    if created:
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Workflow State Advance - {utc_now()}\n\n"
                f"- From: state_machine\n"
                f"- To: workflow\n"
                f"- Topic: advance:{role}\n"
                f"- Artifact: logs/inter_agent_dialogue.md\n"
                "- Summary:\n"
                f"{indent_block('Created requests: ' + ', '.join(created))}\n"
            ),
        )
    return created


def run_agent_worker(
    run_id: str,
    slug: str,
    protocol: str,
    role: str,
    mode: str,
    user_prompt: str,
    payload: dict[str, Any],
    log_path: Path,
) -> None:
    base = task_root(slug)
    update_run(run_id, status="running")
    timestamp = utc_now()
    log_run(
        log_path,
        (
            f"# Agent Run {run_id}\n\n"
            f"- Started: `{timestamp}`\n"
            f"- Protocol: `{protocol}`\n"
            f"- Role: `{role}`\n"
            f"- Mode: `{mode}`\n"
            f"- Task: `{slug}`\n\n"
            "## Prompt\n\n"
            f"{user_prompt}\n\n"
        ),
    )
    append_text(
        base / "logs" / "agent_interactions.md",
        (
            f"\n## Agent Run Started - {timestamp}\n\n"
            "- From: web_runner\n"
            "- To: workflow\n"
            f"- Topic: {protocol}:{role}:{mode}\n"
            f"- Artifact: logs/agent_runs/{run_id}.log\n"
            "- Summary:\n"
            f"{indent_block(user_prompt)}\n"
        ),
    )
    try:
        result = run_openai_compatible(slug, protocol, role, mode, user_prompt, payload)
        if mode == "execute":
            result = extract_and_write_files(base, result)
            result = execute_local_command_blocks(base, result)
	            
        log_run(log_path, "\n## Result\n\n" + result + "\n")
        plan_path = base / "notes" / f"runner_{run_id}.md"
        write_text(plan_path, f"# Runner Output: {run_id}\n\n{result}\n")
        created_requests: list[str] = []
        if mode == "execute":
            created_requests = advance_workflow_state(
                slug,
                role=role,
                prompt=user_prompt,
                run_id=run_id,
                output_artifact=str(plan_path.relative_to(base)),
            )
            if created_requests:
                log_run(
                    log_path,
                    "\n## Workflow State Advance\n\n"
                    + "\n".join(f"- {request_id}" for request_id in created_requests)
                    + "\n",
                )
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Agent Run Finished - {utc_now()}\n\n"
                "- From: web_runner\n"
                "- To: leader\n"
                f"- Topic: {protocol}:{mode}:finished\n"
                f"- Artifact: notes/{plan_path.name}; logs/agent_runs/{run_id}.log\n"
                "- Summary:\n"
                f"  Runner completed and wrote `{plan_path.relative_to(base)}`."
                f"{' Created requests: ' + ', '.join(created_requests) if created_requests else ''}\n"
            ),
        )
        update_run(run_id, status="finished", finished_at=utc_now(), finished_ts=time.time())
    except Exception as exc:  # pragma: no cover - thread boundary
        message = f"{type(exc).__name__}: {exc}"
        log_run(log_path, "\n## Error\n\n" + message + "\n")
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Agent Run Failed - {utc_now()}\n\n"
                "- From: web_runner\n"
                "- To: leader\n"
                f"- Topic: {protocol}:{mode}:failed\n"
                f"- Artifact: logs/agent_runs/{run_id}.log\n"
                "- Summary:\n"
                f"  {message}\n"
            ),
        )
        update_run(run_id, status="failed", finished_at=utc_now(), finished_ts=time.time(), error=message)


def run_openai_compatible(
    slug: str,
    protocol: str,
    role: str,
    mode: str,
    user_prompt: str,
    payload: dict[str, Any],
) -> str:
    """调用 OpenAI 兼容 API，使用 httpx 流式模式实时接收内容。"""
    base_url = str(payload.get("base_url") or "").strip()
    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    if not base_url:
        raise ValueError("base_url is required for OpenAI-compatible providers")
    if not model:
        raise ValueError("model is required for OpenAI-compatible providers")
    if not api_key:
        raise ValueError("api_key is required")

    prompt = build_runner_prompt(slug, user_prompt, mode, role)
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a senior coding planning agent. Return concise, executable plans and avoid unsupported claims.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": float(payload.get("temperature", 0.2)),
        "stream": True,
    }
    # 不设置 max_tokens，让模型自由生成

    timeout_seconds = float(payload.get("timeout_seconds", 600))
    url = normalize_chat_url(base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    client = get_http_client()
    collected_chunks: list[str] = []
    start_time = time.time()
    last_progress_time = start_time

    try:
        with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
            timeout=httpx.Timeout(connect=30.0, read=timeout_seconds, write=30.0, pool=30.0),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                # SSE 格式: "data: {...}"
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            collected_chunks.append(text)
                    except json.JSONDecodeError:
                        continue

                # 每 5 秒更新一次进度
                now = time.time()
                if now - last_progress_time >= 5.0:
                    chars = sum(len(c) for c in collected_chunks)
                    update_run(
                        payload.get("_run_id", ""),
                        progress=f"生成中… 已收到 {chars} 字符（{now - start_time:.0f}s）",
                        progress_ts=now,
                    )
                    last_progress_time = now
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"API 返回 HTTP {exc.response.status_code}: {exc.response.text[:500]}") from exc
    except httpx.TimeoutException as exc:
        raise TimeoutError(
            f"API 调用超时（{timeout_seconds}s），已收到 {sum(len(c) for c in collected_chunks)} 字符。"
            f"提示：可尝试增大超时或简化 prompt。"
        ) from exc
    except httpx.ConnectError as exc:
        raise ConnectionError(f"无法连接 API 端点 {url}：{exc}") from exc

    result = "".join(collected_chunks).strip()
    if not result:
        raise RuntimeError(f"API 返回空内容，原始响应: chunks={len(collected_chunks)}")
    return result


def verify_openai_compatible(payload: dict[str, Any]) -> str:
    """快速验证 API 连通性（非流式，5 tokens）。"""
    base_url = str(payload.get("base_url") or "").strip()
    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    if not base_url:
        raise ValueError("base_url is required")
    if not model:
        raise ValueError("model is required")
    if not api_key:
        raise ValueError("api_key is required")

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    client = get_http_client()
    response = client.post(
        normalize_chat_url(base_url),
        json=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0),
    )
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("No choices returned")
    return "验证成功"


class MultiAgentWebHandler(SimpleHTTPRequestHandler):
    server_version = "MultiAgentWeb/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self) -> None:
        if self.path.endswith((".js", ".css", ".html")) or self.path == "/":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/tasks":
                self.write_json({"tasks": list_tasks()})
                return
            if path == "/api/providers":
                self.write_json({"providers": {}})
                return
            if path.startswith("/api/tasks/") and path.endswith("/state"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/state").strip("/"))
                self.write_json(load_task_state(slug))
                return
            if path.startswith("/api/tasks/") and "/runs/" in path and path.endswith("/log"):
                rest = path.removeprefix("/api/tasks/")
                slug_part, run_part = rest.split("/runs/", 1)
                slug = unquote(slug_part.strip("/"))
                run_id = unquote(run_part.removesuffix("/log").strip("/"))
                self.write_json(load_run_log(slug, run_id))
                return
            if path.startswith("/api/tasks/") and path.endswith("/events"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/events").strip("/"))
                self.stream_task_events(slug)
                return
            if path == "/api/runs":
                # 返回所有 runs（含 progress 进度）
                with RUN_LOCK:
                    runs_snapshot = list(RUNS.values())
                self.write_json({"runs": runs_snapshot, "max_concurrent": MAX_CONCURRENT_RUNS})
                return
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()
        except FileNotFoundError:
            self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self.write_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            # 创建新任务
            if path == "/api/tasks":
                payload = self.read_json()
                self.write_json(create_task_web(payload), HTTPStatus.CREATED)
                return
            if path == "/api/verify":
                payload = self.read_json()
                try:
                    msg = verify_openai_compatible(payload)
                    self.write_json({"ok": True, "message": msg})
                except Exception as exc:
                    self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if path.startswith("/api/tasks/") and path.endswith("/interventions"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/interventions").strip("/"))
                payload = self.read_json()
                self.write_json(append_intervention(slug, payload))
                return
            if path.startswith("/api/tasks/") and path.endswith("/agents"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/agents").strip("/"))
                payload = self.read_json()
                self.write_json(create_agent_web(slug, payload), HTTPStatus.CREATED)
                return
            if path.startswith("/api/tasks/") and path.endswith("/requests/priority"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/requests/priority").strip("/"))
                payload = self.read_json()
                self.write_json(update_request_priority_web(slug, payload))
                return
            if path.startswith("/api/tasks/") and path.endswith("/runs"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/runs").strip("/"))
                payload = self.read_json()
                self.write_json(start_agent_run(slug, payload), HTTPStatus.ACCEPTED)
                return
            self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except FileNotFoundError:
            self.write_json({"error": "task not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self.write_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        if not raw:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def write_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_task_events(self, slug: str) -> None:
        if not task_root(slug).exists():
            self.write_json({"error": "task not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_version = -1
        deadline = time.time() + 60 * 30
        while time.time() < deadline:
            current_version = task_version(slug)
            if current_version != last_version:
                payload = json.dumps(
                    {"slug": slug, "version": current_version, "timestamp": utc_now()},
                    ensure_ascii=False,
                )
                try:
                    self.wfile.write(f"event: state\n".encode("utf-8"))
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                last_version = current_version
            else:
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            time.sleep(1.0)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the multi-agent web dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        require_httpx()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), MultiAgentWebHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Multi-agent web dashboard running at {url}")
    print("Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
