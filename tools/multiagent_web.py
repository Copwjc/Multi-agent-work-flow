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
import sqlite3
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
CANCELLED_RUNS: set[str] = set()
REQUEST_LOCK = threading.RLock()
RUN_PROCESSES: dict[str, subprocess.Popen[str]] = {}
RUN_PROCESS_LOCK = threading.Lock()

# ── 并发控制：解除并行限制，允许高并发 ──
MAX_CONCURRENT_RUNS = 100
_RUN_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_RUNS)

# ── 共享 httpx 客户端（连接池复用）──
_shared_http_client: httpx.Client | None = None
_http_client_lock = threading.Lock()


class RunCancelled(RuntimeError):
    """Raised when the user cancels a running agent task."""


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
                    timeout=httpx.Timeout(connect=30.0, read=1800.0, write=30.0, pool=30.0),
                    limits=httpx.Limits(max_connections=MAX_CONCURRENT_RUNS, max_keepalive_connections=MAX_CONCURRENT_RUNS),
                    follow_redirects=True,
                )
    return _shared_http_client


DEFAULT_AGENTS_LIST = ("leader", "literature", "math", "algorithm", "experiment", "latex")
REQUEST_RETRY_COOLDOWN_SECONDS = 60
MAX_LOCAL_COMMANDS_PER_RUN = 10
API_VERIFY_TIMEOUT_SECONDS = 1800
LOCAL_COMMAND_TIMEOUT_SECONDS = 3600
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

CORE_WATCHED_TASK_PATHS = (
    "README.md",
    "logs/agent_interactions.md",
    "logs/override_log.md",
    "logs/workflow.db",
    "notes/task_brief.md",
    "notes/leader_summary.md",
    "notes/resource_registry.md",
    "notes/task_manifest.json",
    "notes/override_directive.md",
    "report/main.pdf",
)

WATCHED_TASK_DIRS = (
    "logs/agent_runs",
    "experiments/outputs",
    "experiments/figures",
    "report/figures",
)

LEGACY_WORKFLOW_STATE_PATHS = (
    "logs/request_ledger.json",
    "logs/inter_agent_dialogue.md",
    "notes/request_priorities.json",
    "logs/workflow_state.json",
    "logs/dispatch_queue.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_role(role: str) -> str:
    value = str(role or "").strip()
    return ROLE_ALIASES.get(value.lower(), value)


def task_root(slug: str) -> Path:
    safe_slug = Path(slug).name
    return ROOT / "tasks" / safe_slug


def task_manifest_path(base: Path) -> Path:
    return base / "notes" / "task_manifest.json"


def workflow_store_path(base: Path) -> Path:
    return base / "logs" / "workflow.db"


def default_task_manifest(base: Path) -> dict[str, Any]:
    slug = base.name
    python_candidates = [
        "experiments/.venv/bin/python",
        ".venv/bin/python",
        "venv/bin/python",
        "experiments/.venv/Scripts/python.exe",
        ".venv/Scripts/python.exe",
        "venv/Scripts/python.exe",
    ]
    return {
        "version": 1,
        "slug": slug,
        "title": extract_title(read_text(base / "README.md"), slug),
        "agents": [],
        "artifacts": {
            "task_brief": ["notes/task_brief.md"],
            "literature_review": ["notes/literature_review.md"],
            "leader_summary": ["notes/leader_summary.md"],
            "resource_registry": ["notes/resource_registry.md"],
            "workflow_store": ["logs/workflow.db"],
            "interaction_log": ["logs/agent_interactions.md"],
            "override_log": ["logs/override_log.md"],
            "run_log": ["logs/run_log.md"],
            "code": ["experiments/src/solution.py"],
            "tests": ["experiments/tests/test_solution.py"],
            "experiments": ["experiments/run_experiment.py", "experiments/analysis.md"],
            "report": ["report/main.tex"],
        },
        "entrypoints": {
            "python_candidates": python_candidates,
            "test_commands": ["python -m unittest discover -s tests -v"],
            "report_commands": ["pdflatex main.tex"],
            "experiment_cwd": "experiments",
            "report_cwd": "report",
        },
        "workflow": {
            "primary_code_artifacts": ["experiments/src/solution.py"],
            "primary_test_artifacts": ["experiments/tests/test_solution.py"],
            "analysis_artifacts": ["experiments/analysis.md", "experiments/run_experiment.py"],
            "report_artifacts": ["report/main.tex", "notes/leader_summary.md"],
        },
    }


def load_task_manifest(base: Path) -> dict[str, Any]:
    manifest = default_task_manifest(base)
    path = task_manifest_path(base)
    if path.exists():
        try:
            raw = json.loads(read_text(path))
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                manifest[key] = value
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    manifest["artifacts"] = artifacts
    entrypoints = manifest.get("entrypoints")
    if not isinstance(entrypoints, dict):
        entrypoints = {}
    manifest["entrypoints"] = entrypoints
    workflow = manifest.get("workflow")
    if not isinstance(workflow, dict):
        workflow = {}
    manifest["workflow"] = workflow
    return manifest


def manifest_paths(manifest: dict[str, Any], section: str) -> list[str]:
    artifacts = manifest.get("artifacts", {})
    values = artifacts.get(section, []) if isinstance(artifacts, dict) else []
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(value) for value in values if str(value).strip()]
    return []


def workflow_paths(manifest: dict[str, Any], section: str, fallback: list[str]) -> list[str]:
    workflow = manifest.get("workflow", {})
    values = workflow.get(section, []) if isinstance(workflow, dict) else []
    if isinstance(values, str):
        return [values]
    if isinstance(values, list) and values:
        return [str(value) for value in values if str(value).strip()]
    return fallback


def resolve_task_python(base: Path) -> tuple[str, str]:
    manifest = load_task_manifest(base)
    entrypoints = manifest.get("entrypoints", {})
    candidates = entrypoints.get("python_candidates", []) if isinstance(entrypoints, dict) else []
    if isinstance(candidates, str):
        candidates = [candidates]
    normalized: list[str] = []
    for candidate in candidates:
        value = str(candidate).strip()
        if value and value not in normalized:
            normalized.append(value)
    if sys.executable not in normalized:
        normalized.append(sys.executable)
    for candidate in normalized:
        path = Path(candidate)
        resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
        if resolved.exists() and resolved.is_file():
            return str(resolved), str(path if not path.is_absolute() else resolved)
    return sys.executable, sys.executable


def manifest_watch_paths(manifest: dict[str, Any]) -> list[str]:
    relpaths: set[str] = set()
    for bucket_name in ("artifacts", "workflow"):
        bucket = manifest.get(bucket_name, {})
        if not isinstance(bucket, dict):
            continue
        for value in bucket.values():
            if isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    relpaths.add(cleaned)
                continue
            if isinstance(value, list):
                for item in value:
                    cleaned = str(item).strip()
                    if cleaned:
                        relpaths.add(cleaned)
    return sorted(relpaths)


def iter_task_watch_paths(base: Path) -> list[Path]:
    manifest = load_task_manifest(base)
    relpaths = set(CORE_WATCHED_TASK_PATHS)
    relpaths.update(manifest_watch_paths(manifest))
    watched = [base / rel_path for rel_path in sorted(relpaths)]
    for rel_dir in WATCHED_TASK_DIRS:
        directory = base / rel_dir
        if not directory.exists():
            continue
        watched.extend(path for path in sorted(directory.rglob("*")) if path.is_file())
    return watched


def task_version(slug: str) -> int:
    base = task_root(slug)
    version = 0
    for path in iter_task_watch_paths(base):
        if path.exists():
            version = max(version, path.stat().st_mtime_ns)
    return version


def retire_legacy_workflow_state(base: Path) -> list[str]:
    removed: list[str] = []
    for rel_path in LEGACY_WORKFLOW_STATE_PATHS:
        path = base / rel_path
        if not path.exists():
            continue
        path.unlink()
        removed.append(rel_path)
    return removed


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


def run_status_path(base: Path, run_id: str) -> Path:
    return base / "logs" / "agent_runs" / f"{run_id}.status.json"


def run_job_path(base: Path, run_id: str) -> Path:
    return base / "logs" / "agent_runs" / f"{run_id}.job.json"


def run_proc_log_path(base: Path, run_id: str) -> Path:
    return base / "logs" / "agent_runs" / f"{run_id}.proc.log"


def load_run_status_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def write_run_status_file(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


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
        idx = 1
        while idx < len(lines):
            line = lines[idx]
            match = re.match(r"-\s+([^:]+):\s*(.*)", line)
            if not match:
                idx += 1
                continue
            key = match.group(1).strip().lower().replace(" / ", "_").replace(" ", "_")
            value = match.group(2).strip()
            block_lines: list[str] = []
            lookahead = idx + 1
            while lookahead < len(lines):
                candidate = lines[lookahead]
                if re.match(r"-\s+[^:]+:\s*", candidate):
                    break
                if candidate.startswith("## "):
                    break
                if candidate.startswith("  ") or candidate.startswith("\t"):
                    block_lines.append(candidate.strip())
                    lookahead += 1
                    continue
                if not candidate.strip():
                    lookahead += 1
                    continue
                break
            if block_lines:
                merged = "\n".join(block_lines).strip()
                item[key] = strip_markdown(merged if not value else f"{value}\n{merged}")
            else:
                item[key] = strip_markdown(value)
            idx = lookahead
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


def task_base_from_request_ref(path: Path) -> Path:
    if path.name in {"inter_agent_dialogue.md", "agent_interactions.md", "override_log.md"}:
        return path.parent.parent
    if path.name == "logs":
        return path.parent
    return path


def init_workflow_store(base: Path) -> sqlite3.Connection:
    path = workflow_store_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            parent TEXT NOT NULL DEFAULT 'none',
            status TEXT NOT NULL DEFAULT 'open',
            from_role TEXT NOT NULL DEFAULT '',
            to_role TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT '',
            need TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            artifact_resource TEXT NOT NULL DEFAULT '',
            leader_review_required TEXT NOT NULL DEFAULT 'yes',
            why TEXT NOT NULL DEFAULT '',
            response TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            closed_by TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


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


REQUEST_STORE_FIELDS = (
    "request_id",
    "parent",
    "status",
    "from",
    "to",
    "type",
    "need",
    "context",
    "artifact_resource",
    "leader_review_required",
    "why",
    "response",
    "note",
    "created_at",
    "updated_at",
    "run_id",
    "closed_by",
    "priority",
)


def normalize_request_record(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {field: "" for field in REQUEST_STORE_FIELDS}
    for key in REQUEST_STORE_FIELDS:
        value = row.get(key, "")
        if value is None:
            value = ""
        normalized[key] = str(value).strip() if key != "response" else str(value).rstrip()
    normalized["request_id"] = strip_markdown(normalized["request_id"])
    normalized["parent"] = strip_markdown(normalized["parent"]) or "none"
    normalized["status"] = norm_status(normalized["status"]) or "open"
    normalized["from"] = canonical_role(normalized["from"])
    normalized["to"] = canonical_role(normalized["to"])
    normalized["type"] = strip_markdown(normalized["type"])
    normalized["artifact_resource"] = strip_markdown(normalized["artifact_resource"])
    normalized["leader_review_required"] = normalized["leader_review_required"] or "yes"
    try:
        normalized["priority"] = str(int(normalized.get("priority", "") or 0))
    except ValueError:
        normalized["priority"] = "0"
    return normalized


def _request_rows_from_dialogue_markdown(markdown: str) -> list[dict[str, Any]]:
    rows = parse_table(markdown, "## Request Ledger")
    entries = parse_dialogue_entries(markdown)
    by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for row in rows:
        normalized = normalize_request_record(row)
        request_id = normalized["request_id"]
        if not request_id:
            continue
        by_id[request_id] = normalized
        ordered_ids.append(request_id)
    for entry in entries:
        normalized = normalize_request_record(entry)
        request_id = normalized["request_id"]
        if not request_id:
            continue
        existing = by_id.get(request_id, {})
        merged = existing | normalized
        merged["response"] = existing.get("response", "") or normalized.get("response", "")
        by_id[request_id] = normalize_request_record(merged)
        if request_id not in ordered_ids:
            ordered_ids.append(request_id)
    return [by_id[request_id] for request_id in ordered_ids if request_id in by_id]


def load_legacy_request_rows(base: Path) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    legacy_json = base / "logs" / "request_ledger.json"
    if legacy_json.exists():
        try:
            raw = json.loads(read_text(legacy_json))
        except json.JSONDecodeError:
            raw = {}
        payload = raw.get("requests", []) if isinstance(raw, dict) else []
        if isinstance(payload, list):
            requests = [normalize_request_record(item) for item in payload if isinstance(item, dict)]
    dialogue_rows = _request_rows_from_dialogue_markdown(read_text(base / "logs" / "inter_agent_dialogue.md"))
    if not requests:
        requests = dialogue_rows
    elif dialogue_rows:
        by_id = {row["request_id"]: row for row in dialogue_rows}
        merged_rows: list[dict[str, Any]] = []
        for row in requests:
            richer = by_id.get(row["request_id"], {})
            merged = row | {key: value for key, value in richer.items() if value and not row.get(key)}
            merged_rows.append(normalize_request_record(merged))
        requests = merged_rows
    priorities = load_request_priorities(base)
    for row in requests:
        request_id = strip_markdown(row.get("request_id", ""))
        if request_id in priorities:
            row["priority"] = str(priorities[request_id])
    return [row for row in requests if row.get("request_id")]


def write_request_rows(conn: sqlite3.Connection, requests: list[dict[str, Any]], *, replace: bool = False) -> None:
    cleaned = [normalize_request_record(row) for row in requests if row.get("request_id")]
    cleaned.sort(key=lambda row: (row.get("created_at", ""), row.get("request_id", "")))
    known_ids = {row["request_id"] for row in cleaned}
    for row in cleaned:
        parent = strip_markdown(str(row.get("parent", ""))) or "none"
        if parent == "none" or parent in known_ids:
            row["parent"] = parent
            continue
        note = str(row.get("note", "")).strip()
        repair = f"Orphan parent `{parent}` was reset to `none` during ledger validation."
        row["parent"] = "none"
        row["note"] = f"{note}\n{repair}".strip() if note else repair
    if replace:
        conn.execute("DELETE FROM requests")
    conn.executemany(
        """
        INSERT INTO requests (
            request_id, parent, status, from_role, to_role, type, need, context,
            artifact_resource, leader_review_required, why, response, note,
            created_at, updated_at, run_id, closed_by, priority
        )
        VALUES (
            :request_id, :parent, :status, :from, :to, :type, :need, :context,
            :artifact_resource, :leader_review_required, :why, :response, :note,
            :created_at, :updated_at, :run_id, :closed_by, :priority
        )
        ON CONFLICT(request_id) DO UPDATE SET
            parent=excluded.parent,
            status=excluded.status,
            from_role=excluded.from_role,
            to_role=excluded.to_role,
            type=excluded.type,
            need=excluded.need,
            context=excluded.context,
            artifact_resource=excluded.artifact_resource,
            leader_review_required=excluded.leader_review_required,
            why=excluded.why,
            response=excluded.response,
            note=excluded.note,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            run_id=excluded.run_id,
            closed_by=excluded.closed_by,
            priority=excluded.priority
        """,
        cleaned,
    )
    conn.commit()


def request_row_from_sql(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["priority"] = int(item.get("priority") or 0)
    return item


def load_request_store(base: Path) -> list[dict[str, Any]]:
    with REQUEST_LOCK:
        conn = init_workflow_store(base)
        try:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            if count == 0:
                legacy_rows = load_legacy_request_rows(base)
                if legacy_rows:
                    write_request_rows(conn, legacy_rows, replace=True)
                    retire_legacy_workflow_state(base)
            rows = conn.execute(
                """
                SELECT request_id, parent, status, from_role AS "from", to_role AS "to",
                       type, need, context, artifact_resource, leader_review_required,
                       why, response, note, created_at, updated_at, run_id, closed_by,
                       priority
                FROM requests
                ORDER BY created_at, request_id
                """
            ).fetchall()
            return [request_row_from_sql(row) for row in rows]
        finally:
            conn.close()


def save_request_store(base: Path, requests: list[dict[str, Any]]) -> None:
    with REQUEST_LOCK:
        conn = init_workflow_store(base)
        try:
            write_request_rows(conn, requests, replace=True)
        finally:
            conn.close()


def load_task_state(slug: str) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)

    interactions = read_text(base / "logs" / "agent_interactions.md")
    resources_md = read_text(base / "notes" / "resource_registry.md")
    summary = read_text(base / "notes" / "leader_summary.md")
    brief = read_text(base / "notes" / "task_brief.md") or read_text(base / "README.md")
    manifest = load_task_manifest(base)

    requests = load_request_store(base)
    for index, row in enumerate(requests):
        row["ledger_index"] = index
    resources = parse_table(resources_md, "## Resource Ledger")
    entries = [
        {
            "request_id": row.get("request_id", ""),
            "parent": row.get("parent", ""),
            "from": row.get("from", ""),
            "to": row.get("to", ""),
            "type": row.get("type", ""),
            "status": row.get("status", ""),
            "need": row.get("need", ""),
            "context": row.get("context", ""),
            "why": row.get("why", ""),
            "response": row.get("response", ""),
            "artifact_resource": row.get("artifact_resource", ""),
        }
        for row in requests
    ]
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
        "raw_dialogue": "",
        "raw_resources": resources_md,
        "manifest": manifest,
        "runs": list_task_runs(slug),
        "max_concurrent": MAX_CONCURRENT_RUNS,
    }


def list_task_runs(slug: str) -> list[dict[str, Any]]:
    base = task_root(slug) / "logs" / "agent_runs"
    file_runs: list[dict[str, Any]] = []
    if base.exists():
        status_paths = sorted(base.glob("*.status.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if status_paths:
            for path in status_paths[:20]:
                payload = load_run_status_file(path)
                run_id = str(payload.get("run_id") or path.stem.removesuffix(".status"))
                file_runs.append(
                    {
                        "run_id": run_id,
                        "slug": payload.get("slug", slug),
                        "status": payload.get("status", "logged"),
                        "protocol": payload.get("protocol", ""),
                        "provider": payload.get("protocol", ""),
                        "role": payload.get("role", ""),
                        "mode": payload.get("mode", ""),
                        "request_id": payload.get("request_id", ""),
                        "started_at": payload.get("started_at", ""),
                        "finished_at": payload.get("finished_at", ""),
                        "log_path": payload.get("log_path", str((base / f"{run_id}.log").relative_to(task_root(slug)))),
                        "progress": payload.get("progress", ""),
                        "pid": payload.get("pid", 0),
                    }
                )
        else:
            for path in sorted(base.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                file_runs.append(
                    {
                        "run_id": path.stem,
                        "slug": slug,
                        "status": "logged",
                        "provider": "",
                        "protocol": "",
                        "role": "",
                        "mode": "",
                        "request_id": "",
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

    rows = load_request_store(base)
    known_ids = {strip_markdown(row.get("request_id", "")) for row in rows}
    if request_id not in known_ids:
        raise ValueError(f"unknown request_id: {request_id}")

    current = int(next((row.get("priority", 0) for row in rows if row.get("request_id") == request_id), 0) or 0)
    action = str(payload.get("action") or "up").strip().lower()
    if action == "top":
        new_priority = max([0, *(int(row.get("priority", 0) or 0) for row in rows)]) + 1
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

    for row in rows:
        if row.get("request_id") == request_id:
            row["priority"] = new_priority
            row["updated_at"] = utc_now()
            break
    save_request_store(base, rows)
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
    base = task_root(slug) / "logs" / "agent_runs"
    if base.exists():
        for path in sorted(base.glob("*.status.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            payload = load_run_status_file(path)
            if (
                payload.get("request_id") == request_id
                and payload.get("status") in {"queued", "running"}
            ):
                return payload
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
        base = task_root(slug) / "logs" / "agent_runs"
        if base.exists():
            for path in sorted(base.glob("*.status.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                payload = load_run_status_file(path)
                if (
                    payload.get("request_id") == request_id
                    and payload.get("status") == "failed"
                    and isinstance(payload.get("finished_ts"), (int, float))
                    and now - float(payload.get("finished_ts")) < REQUEST_RETRY_COOLDOWN_SECONDS
                ):
                    failed.append(payload)
    if not failed:
        return None
    return sorted(failed, key=lambda run: float(run.get("finished_ts", 0)), reverse=True)[0]


TERMINAL_REQUEST_STATUSES = {"answered", "accepted", "completed", "skipped", "invalidated", "error"}


def child_requests_for(rows: list[dict[str, Any]], request_id: str) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if strip_markdown(str(row.get("parent", ""))) == request_id
    ]


def request_has_pending_children(rows: list[dict[str, Any]], request_id: str) -> bool:
    children = child_requests_for(rows, request_id)
    return bool(children) and any(
        norm_status(str(child.get("status", ""))) not in TERMINAL_REQUEST_STATUSES
        for child in children
    )


def refresh_blocked_final_reviews(base: Path) -> list[str]:
    """Reopen blocked final reviews once all corrective child requests finish."""
    with REQUEST_LOCK:
        rows = load_request_store(base)
        reopened: list[str] = []
        for row in rows:
            request_id = strip_markdown(str(row.get("request_id", "")))
            if not request_id:
                continue
            if canonical_role(str(row.get("to", ""))) != "leader":
                continue
            if str(row.get("type", "")).strip() != "final_review":
                continue
            if norm_status(str(row.get("status", ""))) != "blocked":
                continue
            children = child_requests_for(rows, request_id)
            if not children:
                continue
            if any(norm_status(str(child.get("status", ""))) not in TERMINAL_REQUEST_STATUSES for child in children):
                continue
            row["status"] = "open"
            row["updated_at"] = utc_now()
            row["note"] = (
                "Corrective child requests are complete; final_review is reopened "
                "for Leader re-evaluation."
            )
            reopened.append(request_id)
        if reopened:
            save_request_store(base, rows)
        return reopened


def update_request_status(path: Path, request_id: str, status: str, response_note: str = "") -> bool:
    """Update one request in the workflow database."""

    if not request_id:
        return False
    base = task_base_from_request_ref(path)
    with REQUEST_LOCK:
        rows = load_request_store(base)
        changed = False
        for row in rows:
            if strip_markdown(row.get("request_id", "")) != request_id:
                continue
            row["status"] = norm_status(status)
            row["updated_at"] = utc_now()
            if response_note:
                row["note"] = response_note
                row["response"] = response_note
            changed = True
            break
        if changed:
            save_request_store(base, rows)
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
    rows = load_request_store(task_base_from_request_ref(dialogue_path))
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
    rows = load_request_store(task_base_from_request_ref(dialogue_path))
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
    rows = load_request_store(task_base_from_request_ref(dialogue_path))
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
    context: str = "",
) -> str:
    base = task_root(slug)
    request_id = next_request_id()
    from_role = canonical_role(from_role)
    to_role = canonical_role(to_role)
    with REQUEST_LOCK:
        rows = load_request_store(base)
        rows.append(
            {
                "request_id": request_id,
                "parent": parent or "none",
                "status": "open",
                "from": from_role,
                "to": to_role,
                "type": request_type,
                "need": need,
                "context": context,
                "artifact_resource": artifact,
                "leader_review_required": leader_review,
                "why": why,
                "response": "TODO",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        save_request_store(base, rows)
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
    artifact = str(payload.get("artifact", "logs/workflow.db")).strip()
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

    request_id = append_workflow_request(
        slug,
        parent=parent,
        from_role="user",
        to_role=target,
        request_type=intervention_type,
        need=message,
        artifact=artifact or "user intervention",
        why=(
            "User submitted a live workflow intervention. The backend structured workflow "
            "engine owns this request and will advance downstream agent work after execution."
        ),
        leader_review="yes",
        context="Created from the web intervention input; do not hand-edit workflow ledger files.",
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
                "- Do not edit workflow ledger files directly; the backend state machine updates request status.\n"
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
    workflow_requests = json.dumps(load_request_store(base), ensure_ascii=False, indent=2)
    resources = read_text(base / "notes" / "resource_registry.md")
    manifest = json.dumps(load_task_manifest(base), ensure_ascii=False, indent=2)
    
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
        f"specialist, state the handoff plan for these available specialist agents: {dispatch_roles}. "
        "Include concrete Need and Artifact / Resource targets in your output. Do not edit workflow "
        "ledger files directly; the backend state machine will create structured downstream requests."
        if role == "leader"
        else (
            "Specialist execution rule: process the assigned Request ID when present. Write durable "
            "work to the relevant artifact, and ask another agent for help before guessing through a "
            "missing dependency. Literature gaps go to Literature Collector, proof/formula gaps "
            "go to Mathematician, executable evidence gaps go to Code Expert, report-integration "
            "gaps go to LaTeX Writer, and scheduling/conflict gaps go to Leader. Do not edit workflow "
            "ledger files directly; the backend state machine will mark the current request answered "
            "and create required follow-up requests."
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
            "You can save multiple files by providing multiple such blocks. Paths must be relative to the task root.\n"
            "All code, tests, data, experiment outputs, and generated figures/images MUST live under `experiments/`: "
            "use `experiments/src/` for code, `experiments/tests/` for tests, `experiments/outputs/` for data/results, "
            "and `experiments/figures/` for plots/images. Do not write new code to top-level `src/`, tests to top-level "
            "`tests/`, or figures to `report/figures/`.\n\n"
            "Do not edit protected workflow state files directly. The server-side state machine owns request logs, "
            "request status transitions, and related protected ledger updates.\n\n"
            "*** LOCAL EXECUTION PERMISSION GRANTED WITH WHITELIST ***\n"
            "After writing files, you may request local verification with explicit command blocks. "
            "Use only this format, one command per line:\n\n"
            "```command cwd=\"experiments\"\n"
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
        "Workflow request state:\n"
        f"{workflow_requests[-4000:]}\n\n"
        "Resource registry:\n"
        f"{resources[-3000:]}\n\n"
        "Task manifest:\n"
        f"{manifest}\n\n"
        "Output requirements:\n"
        "- Be concrete and artifact-oriented.\n"
        "- Mention files that should be changed or inspected.\n"
        "- If executing, keep logs and avoid secrets.\n"
        f"- Always use available role ids: {', '.join(available_roles)}.\n"
        f"- {orchestration_rule}\n"
        "--- IMPORTANT INSTRUCTIONS ---\n"
        f"Mode: {mode}\n"
        f"{mode_rule}\n"
        "WARNING: Do not write `logs/inter_agent_dialogue.md`, `logs/request_ledger.json`, "
        "or other workflow ledger files directly.\n"
        "WARNING: The resource registry MUST be written to EXACTLY `notes/resource_registry.md`.\n"
    )


def start_agent_run(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)
    refresh_blocked_final_reviews(base)
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
    if request_id:
        request_row = find_request_row(base, request_id)
        rows = load_request_store(base)
        if (
            str(request_row.get("type", "")).strip() == "final_review"
            and canonical_role(str(request_row.get("to", ""))) == "leader"
            and norm_status(str(request_row.get("status", ""))) == "blocked"
            and request_has_pending_children(rows, request_id)
        ):
            raise ValueError(
                f"request {request_id} is blocked; wait for its corrective child requests "
                "to finish before retrying final_review"
            )
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
    status_path = run_status_path(base, run_id)
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
        "status_path": str(status_path.relative_to(base)),
        "progress": "排队中…",
        "progress_ts": 0.0,
        "pid": 0,
    }
    with RUN_LOCK:
        RUNS[run_id] = run
    write_run_status_file(status_path, run)
    threading.Thread(
        target=launch_agent_process_with_semaphore,
        args=(run_id, slug, protocol, role, mode, user_prompt, payload, log_path),
        daemon=True,
    ).start()
    return run


def launch_agent_process_with_semaphore(
    run_id: str,
    slug: str,
    protocol: str,
    role: str,
    mode: str,
    user_prompt: str,
    payload: dict[str, Any],
    log_path: Path,
) -> None:
    update_run(run_id, status="queued", progress="排队等待本地子进程槽位…", progress_ts=time.time())
    _RUN_SEMAPHORE.acquire()
    try:
        if is_run_cancelled(run_id):
            update_run(run_id, status="cancelled", progress="已中断", finished_at=utc_now(), finished_ts=time.time())
            _RUN_SEMAPHORE.release()
            return
        launch_agent_process(run_id, slug, protocol, role, mode, user_prompt, payload, log_path)
    except Exception as exc:
        _RUN_SEMAPHORE.release()
        update_run(run_id, status="failed", finished_at=utc_now(), finished_ts=time.time(), error=str(exc))


def launch_agent_process(
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
    status_path = run_status_path(base, run_id)
    job_path = run_job_path(base, run_id)
    proc_log_path = run_proc_log_path(base, run_id)
    job_payload = {
        "run_id": run_id,
        "slug": slug,
        "protocol": protocol,
        "role": role,
        "mode": mode,
        "prompt": user_prompt,
        "payload": payload,
        "log_path": str(log_path),
        "status_path": str(status_path),
    }
    write_text(job_path, json.dumps(job_payload, ensure_ascii=False, indent=2) + "\n")
    proc_log_path.parent.mkdir(parents=True, exist_ok=True)
    proc_log = proc_log_path.open("a", encoding="utf-8", newline="\n")
    process = subprocess.Popen(
        [
            sys.executable,
            str(TOOLS_DIR / "agent_process_worker.py"),
            "--job",
            str(job_path),
        ],
        cwd=str(ROOT),
        stdout=proc_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with RUN_PROCESS_LOCK:
        RUN_PROCESSES[run_id] = process
    update_run(
        run_id,
        status="running",
        progress="本地子进程已启动",
        progress_ts=time.time(),
        pid=process.pid,
    )
    threading.Thread(
        target=watch_agent_process,
        args=(run_id, process, proc_log, status_path),
        daemon=True,
    ).start()


def watch_agent_process(
    run_id: str,
    process: subprocess.Popen[str],
    proc_log_handle: Any,
    status_path: Path,
) -> None:
    return_code = process.wait()
    _RUN_SEMAPHORE.release()
    try:
        proc_log_handle.close()
    except Exception:
        pass
    with RUN_PROCESS_LOCK:
        RUN_PROCESSES.pop(run_id, None)
    payload = load_run_status_file(status_path)
    status = str(payload.get("status") or "")
    updates: dict[str, Any] = {
        "pid": 0,
        "finished_ts": time.time(),
    }
    if payload:
        for key in ("status", "progress", "progress_ts", "started_at", "finished_at", "error"):
            if key in payload:
                updates[key] = payload[key]
    if not status or status in {"queued", "running"}:
        updates["status"] = "finished" if return_code == 0 else "failed"
        updates["finished_at"] = utc_now()
        if return_code != 0:
            updates["error"] = f"agent process exited with code {return_code}"
    update_run(run_id, **updates)


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
        if is_run_cancelled(run_id):
            update_run(
                run_id,
                status="cancelled",
                progress="已中断",
                finished_at=utc_now(),
                finished_ts=time.time(),
            )
            return
        update_run(run_id, progress="正在执行…", progress_ts=time.time())
        run_agent_worker(run_id, slug, protocol, role, mode, user_prompt, payload, log_path)
    finally:
        _RUN_SEMAPHORE.release()


def is_run_cancelled(run_id: str) -> bool:
    with RUN_LOCK:
        return run_id in CANCELLED_RUNS or RUNS.get(run_id, {}).get("status") == "cancelled"


def cancel_runs(slug: str, run_id: str | None = None) -> dict[str, Any]:
    base = task_root(slug)
    if not base.exists():
        raise FileNotFoundError(slug)
    cancelled: list[str] = []
    now = utc_now()
    with RUN_LOCK:
        for candidate, run in RUNS.items():
            if run.get("slug") != slug:
                continue
            if run_id and candidate != run_id:
                continue
            if run.get("status") not in {"queued", "running"}:
                continue
            CANCELLED_RUNS.add(candidate)
            run.update(
                {
                    "status": "cancelled",
                    "progress": "用户已中断",
                    "finished_at": now,
                    "finished_ts": time.time(),
                    "error": "cancelled by user",
                }
            )
            cancelled.append(candidate)
            log_path_value = run.get("log_path")
            if log_path_value:
                log_run(base / str(log_path_value), f"\n## Cancelled\n\n- Time: `{now}`\n- Reason: user interrupt\n")
            with RUN_PROCESS_LOCK:
                process = RUN_PROCESSES.get(candidate)
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
    if cancelled:
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## GUI Event - Runs Interrupted - {now}\n\n"
                "- From: web_gui\n"
                "- To: workflow\n"
                "- Type: interrupt\n"
                "- Summary:\n"
                f"{indent_block('Cancelled runs: ' + ', '.join(cancelled))}\n"
            ),
        )
    return {"ok": True, "cancelled": cancelled}


def schedule_server_restart() -> dict[str, Any]:
    def restart_later() -> None:
        time.sleep(0.35)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    threading.Thread(target=restart_later, daemon=True).start()
    return {"ok": True, "message": "server restart scheduled"}


def update_run(run_id: str, **values: Any) -> None:
    with RUN_LOCK:
        if run_id in RUNS:
            RUNS[run_id].update(values)
            run = RUNS[run_id].copy()
            status_ref = run.get("status_path")
            if status_ref:
                path = Path(status_ref)
                if not path.is_absolute():
                    path = task_root(str(run.get("slug", ""))) / str(status_ref)
                write_run_status_file(path, run)


def log_run(log_path: Path, text: str) -> None:
    append_text(log_path, text)


def extract_and_write_files(base_path: Path, text: str) -> str:
    import re
    pattern = r'```(?:file\s+path|filepath)="([^"]+)"\n(.*?)\n```'
    matches = list(re.finditer(pattern, text, re.DOTALL))
    edited = []
    remapped = []
    skipped = []
    protected_paths = {
        "logs/inter_agent_dialogue.md",
        "logs/agent_interactions.md",
        "logs/request_ledger.json",
        "logs/workflow.db",
        "logs/workflow_state.json",
        "logs/dispatch_queue.json",
    }
    for match in matches:
        rel_path = match.group(1).strip()
        file_content = match.group(2)
        normalized_rel = Path(rel_path).as_posix()
        if normalized_rel in protected_paths:
            skipped.append(rel_path)
            continue
        target_rel = normalize_experiment_artifact_path(normalized_rel)
        if target_rel != normalized_rel:
            remapped.append((normalized_rel, target_rel))
        safe_path = (base_path / target_rel).resolve()
        try:
            safe_path.relative_to(base_path.resolve())
            write_text(safe_path, file_content)
            edited.append(target_rel)
        except ValueError:
            pass
    notices = []
    if edited:
        notices.append("**System Notice:** 自动写入了以下文件：\n" + "\n".join(f"- `{p}`" for p in edited))
    if remapped:
        notices.append(
            "**System Notice:** 已按任务约定将代码/数据/图像路径归一到 `experiments/`：\n"
            + "\n".join(f"- `{src}` → `{dst}`" for src, dst in remapped)
        )
    if skipped:
        notices.append(
            "**System Notice:** 跳过了受保护的工作流台账文件，状态机将负责更新：\n"
            + "\n".join(f"- `{p}`" for p in skipped)
        )
    if notices:
        return text + "\n\n" + "\n\n".join(notices)
    return text


def normalize_experiment_artifact_path(rel_path: str) -> str:
    normalized = Path(rel_path).as_posix().lstrip("/")
    parts = normalized.split("/")
    if not parts:
        return normalized
    if parts[0] == "src":
        return Path("experiments", "src", *parts[1:]).as_posix()
    if parts[0] == "tests":
        return Path("experiments", "tests", *parts[1:]).as_posix()
    if parts[0] == "figures":
        return Path("experiments", "figures", *parts[1:]).as_posix()
    if parts[0] in {"outputs", "data", "datasets"}:
        return Path("experiments", *parts).as_posix()
    if len(parts) >= 2 and parts[0] == "report" and parts[1] == "figures":
        return Path("experiments", "figures", *parts[2:]).as_posix()
    if len(parts) == 1:
        suffix = Path(parts[0]).suffix.lower()
        if suffix == ".py":
            return Path("experiments", parts[0]).as_posix()
        if suffix in {".csv", ".json", ".jsonl", ".npy", ".npz", ".parquet", ".pkl", ".txt"}:
            return Path("experiments", "outputs", parts[0]).as_posix()
        if suffix in {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".webp", ".gif"}:
            return Path("experiments", "figures", parts[0]).as_posix()
    return normalized


def _resolve_task_path(base_path: Path, cwd: str) -> Path:
    raw = (cwd or ".").strip()
    rel = Path(raw)
    base_resolved = base_path.resolve()
    parts = rel.parts
    if raw in {"", "."}:
        target = base_resolved
    elif rel.is_absolute():
        target = rel.resolve()
    elif len(parts) >= 2 and parts[0] == "tasks" and parts[1] == base_path.name:
        # Agents often write project-root-relative cwd="tasks/<slug>" even
        # though command blocks already execute relative to the task root.
        target = (ROOT / rel).resolve()
    else:
        target = (base_path / rel).resolve()
    target.relative_to(base_resolved)
    if not target.exists() or not target.is_dir():
        raise ValueError(f"cwd does not exist inside task: {cwd}")
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


def is_python_executable_name(value: str) -> bool:
    lowered = Path(value).name.lower()
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", lowered))


def execute_local_command_blocks(base_path: Path, text: str) -> tuple[str, list[dict[str, Any]]]:
    pattern = r'```(?:command|cmd)(?:\s+cwd="([^"]*)")?\n(.*?)\n```'
    blocks = list(re.finditer(pattern, text, re.DOTALL))
    if not blocks:
        return text, []

    notices: list[str] = []
    results: list[dict[str, Any]] = []
    command_count = 0
    resolved_python, python_label = resolve_task_python(base_path)
    for block in blocks[:MAX_LOCAL_COMMANDS_PER_RUN]:
        cwd_raw = block.group(1) or "."
        try:
            cwd = _resolve_task_path(base_path, cwd_raw)
        except ValueError:
            notices.append(f"### Rejected command block\n\n- cwd outside task: `{cwd_raw}`")
            results.append(
                {
                    "status": "rejected",
                    "cwd": cwd_raw,
                    "command": "",
                    "reason": f"cwd outside task: {cwd_raw}",
                }
            )
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
                results.append({"status": "rejected", "cwd": str(cwd), "command": command, "reason": str(exc)})
                continue
            reason = validate_local_command(base_path, cwd, args)
            if reason:
                notices.append(f"### Rejected command\n\n- command: `{command}`\n- reason: {reason}")
                results.append({"status": "rejected", "cwd": str(cwd), "command": command, "reason": reason})
                continue
            if is_python_executable_name(args[0]):
                args[0] = resolved_python
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
                results.append(
                    {
                        "status": "completed",
                        "started_at": started,
                        "cwd": str(cwd.relative_to(base_path.resolve()) or "."),
                        "command": command,
                        "argv": args,
                        "python": python_label if is_python_executable_name(command.split()[0]) else "",
                        "exit_code": completed.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                )
                notices.append(
                    "\n".join(
                        [
                            "### Local Command",
                            "",
                            f"- started: `{started}`",
                            f"- cwd: `{cwd.relative_to(base_path.resolve()) or '.'}`",
                            f"- command: `{command}`",
                            f"- python: `{python_label}`" if is_python_executable_name(command.split()[0]) else "- python: `(n/a)`",
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
                            f"- started: `{started}`",
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
                results.append(
                    {
                        "status": "timeout",
                        "started_at": started,
                        "cwd": str(cwd.relative_to(base_path.resolve()) or "."),
                        "command": command,
                        "argv": args,
                        "python": python_label if is_python_executable_name(command.split()[0]) else "",
                        "exit_code": -1,
                        "stdout": (exc.stdout or "")[-LOCAL_COMMAND_OUTPUT_LIMIT:] if isinstance(exc.stdout, str) else "",
                        "stderr": (exc.stderr or "")[-LOCAL_COMMAND_OUTPUT_LIMIT:] if isinstance(exc.stderr, str) else "",
                    }
                )
    if notices:
        return text + "\n\n## Local Execution Results\n\n" + "\n\n".join(notices), results
    return text, results


def auto_feedback_to_leader(slug: str, role: str, prompt: str) -> None:
    role = canonical_role(role)
    request_id = extract_request_id(prompt)
    if not request_id:
        return

    base = task_root(slug)
    update_request_status(
        base,
        request_id,
        "answered",
        f"{role} completed the assigned runner task.",
    )


def consolidate_redundant_requests(dialogue_path: Path) -> None:
    """Only merge exact duplicate requests within the same parent context."""
    base = task_base_from_request_ref(dialogue_path)
    if not base.exists():
        return
    rows = load_request_store(base)
    active_rows = [
        row for row in rows
        if norm_status(row.get("status", "")) in {"open", "queued", "running"}
    ]
    seen: dict[tuple[str, str, str, str, str, str], str] = {}
    to_close: list[tuple[str, str]] = []
    for row in active_rows:
        parent = strip_markdown(row.get("parent", "")) or "none"
        req_type = str(row.get("type", "")).strip()
        to_role = canonical_role(row.get("to", ""))

        key = (
            parent,
            canonical_role(row.get("from", "")),
            to_role,
            req_type,
            str(row.get("artifact_resource", "")).strip(),
            " ".join(str(row.get("need", "")).split()),
        )
        current_id = strip_markdown(row.get("request_id", ""))
        if not current_id:
            continue
        earlier_id = seen.get(key)
        if earlier_id:
            to_close.append(
                (
                    current_id,
                    f"Closed by Leader consolidation: merged with exact duplicate request `{earlier_id}`.",
                )
            )
            continue
        seen[key] = current_id
    for req_id, reason in to_close:
        update_request_status(dialogue_path, req_id, "skipped", reason)


def consolidate_all_tasks() -> None:
    """Consolidate requests for all existing tasks in the workspace."""
    root = ROOT / "tasks"
    if not root.exists():
        return
    for path in root.iterdir():
        if not path.is_dir():
            continue
        consolidate_redundant_requests(path)


def advance_workflow_state(
    slug: str,
    *,
    role: str,
    prompt: str,
    run_id: str,
    output_artifact: str,
    agent_output: str = "",
    local_command_results: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Deterministically move the task to the next agent requests."""

    role = canonical_role(role)
    base = task_root(slug)
    dialogue_path = base

    consolidate_redundant_requests(dialogue_path)

    current_request_id = extract_request_id(prompt)
    current_request = find_request_row(dialogue_path, current_request_id) if current_request_id else {}
    current_from = canonical_role(current_request.get("from", ""))
    current_type = current_request.get("type", "")
    manifest = load_task_manifest(base)
    literature_artifacts = "; ".join(manifest_paths(manifest, "literature_review") or ["notes/literature_review.md", "report/references.bib"])
    math_artifacts = "; ".join(workflow_paths(manifest, "math_artifacts", ["notes/math_model.md", "notes/open_questions.md"]))
    code_artifacts = "; ".join(workflow_paths(manifest, "primary_code_artifacts", manifest_paths(manifest, "code") or ["experiments/src/solution.py"]))
    test_artifacts = "; ".join(workflow_paths(manifest, "primary_test_artifacts", manifest_paths(manifest, "tests") or ["experiments/tests/test_solution.py"]))
    experiment_artifacts = "; ".join(
        workflow_paths(
            manifest,
            "analysis_artifacts",
            manifest_paths(manifest, "experiments") or ["experiments/run_experiment.py", "experiments/analysis.md"],
        )
    )
    report_artifacts = "; ".join(workflow_paths(manifest, "report_artifacts", manifest_paths(manifest, "report") or ["report/main.tex", "notes/leader_summary.md"]))

    status_to_set = "answered"
    final_review_gate_reason = ""
    if role == "leader" and current_type == "final_review":
        command_results = list(local_command_results or [])
        verification_failures = [
            item for item in command_results
            if item.get("status") != "completed" or int(item.get("exit_code", 1)) != 0
        ]
        other_open = [
            row for row in load_request_store(base)
            if strip_markdown(row.get("request_id", "")) != current_request_id
            and norm_status(row.get("status", "")) in {"open", "queued", "running", "blocked"}
        ]
        
        # Check if the leader explicitly accepted the task in its output.
        # Avoid treating generic words like "completed" as acceptance because
        # phrases such as "not completed" previously closed final reviews.
        lowered_output = agent_output.lower()
        leader_accepted = any(
            kw in lowered_output
            for kw in ["accepted", "acceptance granted", "验收通过", "满足验收标准", "可以交付"]
        )
        
        if verification_failures:
            final_review_gate_reason = "local verification commands reported non-zero exit status or timeout"
            status_to_set = "blocked"
        elif other_open and not leader_accepted:
            final_review_gate_reason = f"other requests remain open or blocked ({len(other_open)} items)"
            status_to_set = "blocked"
        elif leader_accepted:
            status_to_set = "accepted"
        else:
            # If no commands run and no acceptance, we don't strictly block it anymore, just mark as answered
            status_to_set = "answered"

    if current_request_id:
        base_reason = (
            f"{role} completed `{run_id}` and wrote `{output_artifact}`."
            if not final_review_gate_reason
            else f"{role} completed `{run_id}` but final review is blocked: {final_review_gate_reason}."
        )
        if agent_output:
            agent_context_short = (agent_output[:2000] + "..." if len(agent_output) > 2000 else agent_output).strip()
            base_reason += f"\n\nContext:\n{agent_context_short}"
        update_request_status(
            dialogue_path,
            current_request_id,
            status_to_set,
            base_reason,
        )

    parent = current_request_id or run_id
    created: list[str] = []

    agent_context = (agent_output[:2000] + "..." if len(agent_output) > 2000 else agent_output).strip() if agent_output else ""

    def create_once(to_role: str, request_type: str, need: str, artifact: str, why: str) -> None:
        dedupe_parent = parent if role == "leader" else None
        if request_exists(
            dialogue_path,
            from_role=role,
            to_role=to_role,
            request_type=request_type,
            parent=dedupe_parent,
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
            context=agent_context,
        )
        created.append(request_id)

    if role == "leader":
        if current_type == "final_review" and status_to_set == "blocked":
            failed_commands = list(local_command_results or [])
            test_failed = any(
                Path(str(item.get("argv", [""])[0])).name.lower().startswith("python")
                or str(item.get("command", "")).startswith("pytest")
                or "pytest" in str(item.get("command", ""))
                or "unittest" in str(item.get("command", ""))
                for item in failed_commands
                if item.get("status") != "completed" or int(item.get("exit_code", 1)) != 0
            )
            report_failed = any(
                Path(str(item.get("argv", [""])[0])).name.lower() in {"pdflatex", "xelatex", "lualatex", "bibtex"}
                for item in failed_commands
                if item.get("status") != "completed" or int(item.get("exit_code", 1)) != 0
            )
            if test_failed:
                create_once(
                    "code_expert",
                    "implementation_check",
                    (
                        "Fix failing executable tests/experiments."
                    ),
                    "; ".join(filter(None, [code_artifacts, test_artifacts, experiment_artifacts])),
                    "Leader final_review is blocked until local executable verification succeeds with zero exit codes.",
                )
            if report_failed:
                create_once(
                    "latex_writer",
                    "writing_gap",
                    (
                        "Fix failing report compilation."
                    ),
                    report_artifacts,
                    "Leader final_review is blocked until report compilation succeeds with zero exit codes.",
                )
        elif status_to_set != "accepted":
            create_once(
                "literature_collector",
                "literature_request",
                (
                    "Map the research landscape, baselines, and boundaries."
                ),
                literature_artifacts,
                "The workflow requires literature grounding before math, implementation, and report claims are finalized.",
            )
            create_once(
                "mathematician",
                "theory_check",
                (
                    "Define math variables, assumptions, and edge cases."
                ),
                math_artifacts,
                "Leader must give Mathematician direct ownership of the mathematical basis instead of routing all theory through literature.",
            )
            create_once(
                "code_expert",
                "implementation_check",
                (
                    "Implement validation plan, check logic paths and edge cases."
                ),
                "; ".join(filter(None, [code_artifacts, test_artifacts, experiment_artifacts, "experiments/outputs/", "experiments/figures/"])),
                "Leader must give Code Expert direct ownership of executable evidence instead of waiting for collector relay.",
            )
    elif role == "literature_collector":
        if current_from == "code_expert":
            create_once(
                "code_expert",
                "baseline_request",
                (
                    "Revise plan using literature baselines and metrics."
                ),
                experiment_artifacts or "experiments/analysis.md",
                "Code Expert requested literature support and should continue from the sourced baseline map.",
            )
        elif current_from == "latex_writer":
            create_once(
                "latex_writer",
                "source_check",
                (
                    "Integrate citations and mark unsupported claims."
                ),
                report_artifacts,
                "LaTeX Writer requested source support and needs a source-grounded writing pass.",
            )
        else:
            create_once(
                "mathematician",
                "theory_check",
                (
                    "Convert direction into formal definitions and proof boundaries."
                ),
                math_artifacts,
                "The implementation and report need explicit assumptions and non-overclaiming boundaries.",
            )
            create_once(
                "code_expert",
                "baseline_request",
                (
                    "Design reproducible experiment plan from literature map."
                ),
                "; ".join(filter(None, [code_artifacts, test_artifacts, experiment_artifacts, "experiments/outputs/", "experiments/figures/"])),
                "The workflow must move from research map to runnable validation instead of stopping at literature review.",
            )
            create_once(
                "latex_writer",
                "source_check",
                (
                    "Prepare source boundaries and related-work structure."
                ),
                report_artifacts,
                "The report should receive source boundaries before final writing.",
            )
    elif role == "mathematician":
        if current_from == "latex_writer":
            create_once(
                "latex_writer",
                "theory_check",
                (
                    "Integrate formal mathematical definitions into report."
                ),
                report_artifacts,
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
                        "Check if math assumptions are supported by literature."
                    ),
                    "; ".join(filter(None, [literature_artifacts, math_artifacts])),
                    "Mathematical assumptions should be grounded in source evidence when the report cites them.",
                )
            create_once(
                "code_expert",
                "implementation_check",
                (
                    "Implement algorithm and edge cases with tests."
                ),
                "; ".join(filter(None, [code_artifacts, test_artifacts])),
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
                    "Provide dataset and reproduction details."
                ),
                experiment_artifacts or "experiments/analysis.md",
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
                    "Validate algorithm mathematically against edge cases."
                ),
                "; ".join(filter(None, [math_artifacts, test_artifacts, experiment_artifacts])),
                "Algorithm design must be checked against rigorous mathematical assumptions before report integration.",
            )
        else:
            create_once(
                "latex_writer",
                "evidence_request",
                (
                    "Integrate validated code, experiments, and limitations into the report."
                ),
                report_artifacts,
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
                    "Provide citation support for all report claims."
                ),
                report_artifacts,
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
                    "Provide checked mathematical definitions, theorems, and proof boundaries."
                ),
                "; ".join(filter(None, [report_artifacts, math_artifacts])),
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
                    "Provide reproducible experiment commands and figures."
                ),
                "; ".join(filter(None, [report_artifacts, experiment_artifacts, "experiments/outputs/", "experiments/figures/"])),
                "The report cannot finalize implementation or experiment claims without Code Expert support.",
            )
            requested_support = True
        if not requested_support:
            create_once(
                "leader",
                "final_review",
                (
                    "Final Review: Check cross-artifact consistency and accept or request rework."
                ),
                "; ".join(filter(None, [report_artifacts, "logs/workflow.db"])),
                "Final delivery needs Leader arbitration and explicit residual-risk handling.",
            )

    reopened = refresh_blocked_final_reviews(base)
    if reopened:
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Workflow State Advance - {utc_now()}\n\n"
                f"- From: state_machine\n"
                f"- To: workflow\n"
                f"- Topic: reopen:final_review\n"
                f"- Artifact: logs/workflow.db\n"
                "- Summary:\n"
                f"{indent_block('Reopened final_review requests: ' + ', '.join(reopened))}\n"
            ),
        )

    if role == "leader" and status_to_set == "accepted":
        if not request_exists(
            dialogue_path,
            from_role="leader",
            to_role="leader",
            request_type="leader_decision",
            parent=current_request_id or None,
            active_only=False,
        ):
            decision_id = append_workflow_request(
                slug,
                parent=current_request_id or "none",
                from_role="leader",
                to_role="leader",
                request_type="leader_decision",
                need="Task accepted by Leader final_review.",
                artifact="logs/workflow.db",
                why="Record the explicit final arbitration required by the inter-agent dialogue protocol.",
                context=agent_context,
            )
            update_request_status(
                dialogue_path,
                decision_id,
                "answered",
                f"Leader accepted `{current_request_id or run_id}` in `{run_id}`.",
            )
            created.append(decision_id)

    if created:
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Workflow State Advance - {utc_now()}\n\n"
                f"- From: state_machine\n"
                f"- To: workflow\n"
                f"- Topic: advance:{role}\n"
                f"- Artifact: logs/workflow.db\n"
                "- Summary:\n"
                f"{indent_block('Created requests: ' + ', '.join(created))}\n"
            ),
        )
    elif role == "leader" and status_to_set == "accepted":
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Workflow State Advance - {utc_now()}\n\n"
                f"- From: state_machine\n"
                f"- To: workflow\n"
                f"- Topic: advance:{role}\n"
                f"- Artifact: logs/workflow.db\n"
                "- Summary:\n"
                f"{indent_block('Workflow successfully completed and accepted by Leader.')}\n"
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
        if is_run_cancelled(run_id):
            raise RunCancelled("cancelled by user")
        local_command_results: list[dict[str, Any]] = []
        if mode == "execute":
            result = extract_and_write_files(base, result)
            if is_run_cancelled(run_id):
                raise RunCancelled("cancelled by user")
            result, local_command_results = execute_local_command_blocks(base, result)
	            
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
                agent_output=result,
                local_command_results=local_command_results,
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
    except RunCancelled as exc:
        message = str(exc) or "cancelled by user"
        log_run(log_path, "\n## Cancelled\n\n" + message + "\n")
        append_text(
            base / "logs" / "agent_interactions.md",
            (
                f"\n## Agent Run Cancelled - {utc_now()}\n\n"
                "- From: web_runner\n"
                "- To: leader\n"
                f"- Topic: {protocol}:{mode}:cancelled\n"
                f"- Artifact: logs/agent_runs/{run_id}.log\n"
                "- Summary:\n"
                f"  {message}\n"
            ),
        )
        update_run(run_id, status="cancelled", finished_at=utc_now(), finished_ts=time.time(), error=message)
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
        try:
            request_id = extract_request_id(user_prompt)
            if request_id:
                update_request_status(
                    base,
                    request_id,
                    "error",
                    f"Agent run {run_id} raised {message}",
                )
        except Exception:
            pass  # best-effort; never let error-recording crash the handler


def run_agent_process_job(job: dict[str, Any]) -> int:
    run_id = str(job["run_id"])
    slug = str(job["slug"])
    protocol = str(job["protocol"])
    role = str(job["role"])
    mode = str(job["mode"])
    user_prompt = str(job["prompt"])
    payload = dict(job.get("payload") or {})
    base = task_root(slug)
    log_path = Path(str(job["log_path"]))
    status_path = Path(str(job["status_path"]))

    with RUN_LOCK:
        RUNS[run_id] = {
            "run_id": run_id,
            "slug": slug,
            "protocol": protocol,
            "role": role,
            "mode": mode,
            "status": "queued",
            "request_id": extract_request_id(user_prompt),
            "started_at": utc_now(),
            "finished_at": "",
            "log_path": str(log_path.relative_to(base)),
            "status_path": str(status_path),
            "progress": "子进程准备执行…",
            "progress_ts": 0.0,
            "pid": os.getpid(),
        }
    update_run(run_id, status="running", progress="子进程执行中", progress_ts=time.time(), pid=os.getpid())
    run_agent_worker(run_id, slug, protocol, role, mode, user_prompt, payload, log_path)
    final_status = RUNS.get(run_id, {}).get("status", "finished")
    return 0 if final_status in {"finished", "accepted"} else 1


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

    timeout_seconds = float(payload.get("timeout_seconds", 1800))
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
                if is_run_cancelled(str(payload.get("_run_id", ""))):
                    raise RunCancelled("cancelled by user")
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
        timeout=httpx.Timeout(
            connect=30.0,
            read=API_VERIFY_TIMEOUT_SECONDS,
            write=30.0,
            pool=30.0,
        ),
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
            if path == "/api/server/restart":
                self.write_json(schedule_server_restart())
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
            if path.startswith("/api/tasks/") and path.endswith("/runs/interrupt"):
                slug = unquote(path.removeprefix("/api/tasks/").removesuffix("/runs/interrupt").strip("/"))
                payload = self.read_json()
                run_id = str(payload.get("run_id") or "").strip() or None
                self.write_json(cancel_runs(slug, run_id))
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
    def periodic_consolidate() -> None:
        while True:
            try:
                consolidate_all_tasks()
            except Exception as e:
                print(f"Error in periodic task consolidation: {e}")
            time.sleep(30)

    threading.Thread(target=periodic_consolidate, daemon=True).start()

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
