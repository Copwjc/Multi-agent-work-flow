#!/usr/bin/env python3
"""Tkinter control panel for the multi-agent workflow.

The GUI is intentionally standard-library only. It creates or opens task
workspaces, captures user goals, appends handoff entries, records Super Admin
Overrides, and displays the current agent interaction logs.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import create_task  # noqa: E402


TEXT_FONT = ("Consolas", 10)
DEFAULT_AGENTS = ",".join(create_task.DEFAULT_AGENTS)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def task_root() -> Path:
    return ROOT / "tasks"


def list_task_slugs() -> list[str]:
    root = task_root()
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def read_text(path: Path) -> str:
    if not path.exists():
        return f"{path} does not exist yet.\n"
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def indent_block(value: str) -> str:
    lines = value.strip().splitlines() or ["(empty)"]
    return "\n".join(f"  {line}" for line in lines)


def table_cell(value: str, limit: int = 80) -> str:
    text = " ".join(value.strip().splitlines()) or "(empty)"
    text = text.replace("|", "\\|")
    return text[:limit]


def upsert_markdown_section(path: Path, heading: str, body: str) -> None:
    content = read_text(path) if path.exists() else f"# {path.stem}\n"
    section = f"\n{heading}\n\n{body.strip()}\n"
    pattern = re.compile(rf"\n{re.escape(heading)}\n.*?(?=\n## |\Z)", re.S)
    if pattern.search(content):
        content = pattern.sub(section, content)
    else:
        content = content.rstrip() + section + "\n"
    write_text(path, content)


def ensure_dialogue_log(path: Path) -> None:
    if path.exists():
        return
    content = (
        "# Inter-Agent Dialogue\n\n"
        "Use this file for concrete agent-to-agent requests and replies. Keep the global\n"
        "summary in `agent_interactions.md`; keep the evidence chain here.\n\n"
        "## Request Ledger\n\n"
        "| Request ID | Parent | Status | From | To | Type | Need | Artifact |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n\n"
        "## Dialogue Entries\n"
    )
    write_text(path, content)


def add_dialogue_ledger_row(path: Path, row: str) -> None:
    ensure_dialogue_log(path)
    content = read_text(path)
    marker = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    if marker not in content:
        append_text(path, "\n## Request Ledger Update\n\n" + row)
        return
    before, after = content.split(marker, 1)
    updated = before + marker + "\n" + row.rstrip() + after
    write_text(path, updated)


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class MultiAgentGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Multi-Agent Workflow Console")
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)

        self.title_var = tk.StringVar()
        self.slug_var = tk.StringVar()
        self.agents_var = tk.StringVar(value=DEFAULT_AGENTS)
        self.selected_task_var = tk.StringVar()
        self.from_var = tk.StringVar(value="user")
        self.to_var = tk.StringVar(value="leader")
        self.topic_var = tk.StringVar(value="instruction")
        self.request_id_var = tk.StringVar()
        self.parent_request_var = tk.StringVar(value="none")
        self.dialogue_type_var = tk.StringVar(value="evidence_request")
        self.dialogue_status_var = tk.StringVar(value="open")
        self.artifact_var = tk.StringVar(value="logs/inter_agent_dialogue.md")
        self.status_var = tk.StringVar(value=f"Workspace: {ROOT}")

        self._build_layout()
        self.refresh_task_list()

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        left = ttk.Frame(self.root, padding=10)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(self.root, padding=(0, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self._build_task_panel(left)
        self._build_goal_panel(left)
        self._build_handoff_panel(left)
        self._build_log_panel(right)

        status = ttk.Label(self.root, textvariable=self.status_var, anchor="w")
        status.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

    def _build_task_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Task Workspace", padding=8)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Existing task").grid(row=0, column=0, sticky="w")
        self.task_combo = ttk.Combobox(frame, textvariable=self.selected_task_var, state="readonly")
        self.task_combo.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.task_combo.bind("<<ComboboxSelected>>", lambda _event: self.open_selected_task())

        ttk.Button(frame, text="Refresh", command=self.refresh_task_list).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(frame, text="Title").grid(row=1, column=0, sticky="w", pady=(8, 0))
        title_entry = ttk.Entry(frame, textvariable=self.title_var)
        title_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=(8, 0))
        title_entry.bind("<KeyRelease>", self._suggest_slug)

        ttk.Label(frame, text="Slug").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.slug_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=(8, 0)
        )

        ttk.Label(frame, text="Agents").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.agents_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        buttons.columnconfigure((0, 1, 2), weight=1)
        ttk.Button(buttons, text="Create / Open", command=self.create_or_open_task).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(buttons, text="Open Selected", command=self.open_selected_task).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(buttons, text="Reveal Folder", command=self.reveal_task_folder).grid(
            row=0, column=2, sticky="ew"
        )

    def _build_goal_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="User Instruction / Task Goal", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        parent.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.goal_text = ScrolledText(frame, height=12, width=48, wrap="word", font=TEXT_FONT)
        self.goal_text.grid(row=0, column=0, sticky="nsew")

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        buttons.columnconfigure((0, 1, 2), weight=1)
        ttk.Button(buttons, text="Save Brief", command=self.save_brief).grid(row=0, column=0, sticky="ew")
        ttk.Button(buttons, text="Append Instruction", command=self.append_user_instruction).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(buttons, text="Super Admin Override", command=self.record_override).grid(
            row=0, column=2, sticky="ew"
        )

    def _build_handoff_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Agent Handoff / Dialogue Entry", padding=8)
        frame.grid(row=2, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="From").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.from_var, width=14).grid(row=0, column=1, sticky="ew", padx=(6, 8))
        ttk.Label(frame, text="To").grid(row=0, column=2, sticky="w")
        ttk.Entry(frame, textvariable=self.to_var, width=14).grid(row=0, column=3, sticky="ew", padx=(6, 0))

        ttk.Label(frame, text="Topic").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.topic_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=(8, 0)
        )

        ttk.Label(frame, text="Request ID").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.request_id_var).grid(
            row=2, column=1, sticky="ew", padx=(6, 8), pady=(8, 0)
        )
        ttk.Label(frame, text="Parent").grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.parent_request_var).grid(
            row=2, column=3, sticky="ew", padx=(6, 0), pady=(8, 0)
        )

        ttk.Label(frame, text="Type").grid(row=3, column=0, sticky="w", pady=(8, 0))
        type_combo = ttk.Combobox(
            frame,
            textvariable=self.dialogue_type_var,
            values=(
                "evidence_request",
                "literature_request",
                "source_check",
                "baseline_request",
                "theory_check",
                "implementation_check",
                "writing_gap",
                "reply",
                "blocked",
                "leader_decision",
            ),
        )
        type_combo.grid(row=3, column=1, sticky="ew", padx=(6, 8), pady=(8, 0))
        ttk.Label(frame, text="Status").grid(row=3, column=2, sticky="w", pady=(8, 0))
        status_combo = ttk.Combobox(
            frame,
            textvariable=self.dialogue_status_var,
            values=("open", "answered", "blocked", "accepted", "invalidated"),
        )
        status_combo.grid(row=3, column=3, sticky="ew", padx=(6, 0), pady=(8, 0))

        ttk.Label(frame, text="Artifact").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.artifact_var).grid(
            row=4, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=(8, 0)
        )

        ttk.Label(frame, text="Need / Reply").grid(row=5, column=0, sticky="nw", pady=(8, 0))
        self.summary_text = ScrolledText(frame, height=5, width=48, wrap="word", font=TEXT_FONT)
        self.summary_text.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(buttons, text="Append Summary Event", command=self.append_handoff).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(buttons, text="Append Dialogue Request / Reply", command=self.append_dialogue).grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.grid(row=0, column=0, sticky="nsew")

        self.agent_log_view = self._add_log_tab(notebook, "Agent Interactions")
        self.dialogue_log_view = self._add_log_tab(notebook, "Inter-Agent Dialogue")
        self.override_log_view = self._add_log_tab(notebook, "Override Log")
        self.brief_view = self._add_log_tab(notebook, "Task Brief")

        buttons = ttk.Frame(parent)
        buttons.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(buttons, text="Refresh Logs", command=self.refresh_logs).grid(row=0, column=0, sticky="ew")
        ttk.Button(buttons, text="Clear Input Boxes", command=self.clear_inputs).grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )

    def _add_log_tab(self, notebook: ttk.Notebook, title: str) -> ScrolledText:
        frame = ttk.Frame(notebook)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        view = ScrolledText(frame, wrap="word", font=TEXT_FONT)
        view.grid(row=0, column=0, sticky="nsew")
        view.configure(state="disabled")
        notebook.add(frame, text=title)
        return view

    def _suggest_slug(self, _event: tk.Event[tk.Widget]) -> None:
        if self.slug_var.get().strip():
            return
        title = self.title_var.get().strip()
        if title:
            self.slug_var.set(create_task.slugify(title))

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def selected_slug(self) -> str:
        return self.slug_var.get().strip() or self.selected_task_var.get().strip()

    def selected_task_dir(self) -> Path:
        return task_root() / self.selected_slug()

    def require_task(self) -> Path | None:
        slug = self.selected_slug()
        if not slug:
            messagebox.showwarning("Task required", "Choose or create a task workspace first.")
            return None
        task_dir = task_root() / slug
        if not task_dir.exists():
            messagebox.showwarning("Task missing", f"Task workspace does not exist:\n{task_dir}")
            return None
        return task_dir

    def refresh_task_list(self) -> None:
        slugs = list_task_slugs()
        self.task_combo["values"] = slugs
        if slugs and not self.selected_task_var.get():
            self.selected_task_var.set(slugs[0])
            self.slug_var.set(slugs[0])
        self.set_status(f"Found {len(slugs)} task workspace(s).")

    def create_or_open_task(self) -> None:
        title = self.title_var.get().strip()
        if not title:
            messagebox.showwarning("Title required", "Enter a task title before creating a workspace.")
            return

        slug = self.slug_var.get().strip() or create_task.slugify(title)
        self.slug_var.set(slug)
        agents = self.agents_var.get().strip() or DEFAULT_AGENTS
        task_dir = task_root() / slug

        if not task_dir.exists():
            args = Namespace(
                title=title,
                slug=slug,
                root=ROOT,
                agents=agents,
                force=False,
                dry_run=False,
            )
            result = create_task.create_workspace(args)
            if result != 0:
                messagebox.showerror("Create failed", f"create_task.py exited with code {result}")
                return
            self.append_task_event("leader", "all", "workspace-created", f"Created task workspace for {title}.")

        self.selected_task_var.set(slug)
        self.refresh_task_list()
        self.refresh_logs()
        self.set_status(f"Active task: {task_dir}")

    def open_selected_task(self) -> None:
        slug = self.selected_task_var.get().strip()
        if not slug:
            return
        self.slug_var.set(slug)
        task_dir = task_root() / slug
        if task_dir.exists():
            self.title_var.set(self._title_from_brief(task_dir) or slug)
        self.refresh_logs()
        self.set_status(f"Active task: {task_dir}")

    def reveal_task_folder(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        try:
            open_path(task_dir)
        except Exception as exc:  # pragma: no cover - platform shell behavior
            messagebox.showerror("Open failed", str(exc))

    def _title_from_brief(self, task_dir: Path) -> str:
        brief = task_dir / "notes" / "task_brief.md"
        if not brief.exists():
            brief = task_dir / "README.md"
        if not brief.exists():
            return ""
        first = brief.read_text(encoding="utf-8").splitlines()[0:1]
        if first and first[0].startswith("# "):
            return first[0].removeprefix("# ").replace("Task Brief: ", "")
        return ""

    def goal_value(self) -> str:
        return self.goal_text.get("1.0", "end").strip()

    def save_brief(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        goal = self.goal_value()
        if not goal:
            messagebox.showwarning("Goal required", "Enter a user instruction or task goal first.")
            return

        timestamp = utc_now()
        body = f"- Updated: `{timestamp}`\n\n{goal}"
        for rel_path in ("README.md", "notes/task_brief.md"):
            upsert_markdown_section(task_dir / rel_path, "## GUI Captured User Goal", body)

        self.append_task_event("user", "leader", "task-goal", goal, artifact="notes/task_brief.md")
        self.refresh_logs()
        self.set_status("Saved task goal and appended an interaction entry.")

    def append_user_instruction(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        instruction = self.goal_value()
        if not instruction:
            messagebox.showwarning("Instruction required", "Enter an instruction first.")
            return
        self.append_task_event("user", "leader", "instruction", instruction, artifact="logs/agent_interactions.md")
        self.refresh_logs()
        self.set_status("Appended user instruction to the interaction log.")

    def record_override(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        correction = self.goal_value()
        if not correction:
            messagebox.showwarning("Override required", "Enter the correction directive first.")
            return

        timestamp = utc_now()
        directive = (
            f"- Triggered at: `{timestamp}`\n"
            f"- User correction:\n{indent_block(correction)}\n"
            "- Reason:\n  TODO\n"
            "- New direction:\n  TODO\n"
            "- Stop doing:\n  TODO\n"
            "- Continue doing:\n  TODO\n"
            "- New acceptance criteria:\n  TODO\n"
        )
        upsert_markdown_section(task_dir / "notes" / "override_directive.md", "## Active Override", directive)

        entry = (
            f"\n## Override - {timestamp}\n\n"
            f"- User correction:\n{indent_block(correction)}\n"
            "- Leader action: pause current direction, mark impacted artifacts, redispatch affected agents.\n"
        )
        append_text(task_dir / "logs" / "override_log.md", entry)
        self.append_task_event(
            "user",
            "leader",
            "super-admin-override",
            correction,
            artifact="notes/override_directive.md; logs/override_log.md",
        )
        self.refresh_logs()
        self.set_status("Recorded Super Admin Override.")

    def append_handoff(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        summary = self.summary_text.get("1.0", "end").strip()
        if not summary:
            messagebox.showwarning("Summary required", "Enter a handoff summary first.")
            return
        self.append_task_event(
            self.from_var.get().strip() or "unknown",
            self.to_var.get().strip() or "unknown",
            self.topic_var.get().strip() or "handoff",
            summary,
            artifact="logs/agent_interactions.md",
        )
        self.refresh_logs()
        self.set_status("Appended manual handoff entry.")

    def append_dialogue(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        summary = self.summary_text.get("1.0", "end").strip()
        if not summary:
            messagebox.showwarning("Need or reply required", "Enter the request need or reply summary first.")
            return

        request_id = self.request_id_var.get().strip() or self.next_request_id()
        self.request_id_var.set(request_id)
        parent = self.parent_request_var.get().strip() or "none"
        from_agent = self.from_var.get().strip() or "unknown"
        to_agent = self.to_var.get().strip() or "unknown"
        dialogue_type = self.dialogue_type_var.get().strip() or "evidence_request"
        dialogue_status = self.dialogue_status_var.get().strip() or "open"
        artifact = self.artifact_var.get().strip() or "(none)"
        timestamp = utc_now()

        entry = (
            f"\n## {request_id}\n\n"
            f"- Time: `{timestamp}`\n"
            f"- Parent: `{parent}`\n"
            f"- From: `{from_agent}`\n"
            f"- To: `{to_agent}`\n"
            f"- Type: `{dialogue_type}`\n"
            f"- Status: `{dialogue_status}`\n"
            f"- Artifact: `{artifact}`\n"
            "- Need / Reply:\n"
            f"{indent_block(summary)}\n"
        )
        dialogue_log = task_dir / "logs" / "inter_agent_dialogue.md"
        ensure_dialogue_log(dialogue_log)

        ledger_row = (
            f"| `{request_id}` | `{parent}` | `{dialogue_status}` | `{from_agent}` | `{to_agent}` | "
            f"`{dialogue_type}` | {table_cell(summary)} | `{artifact}` |\n"
        )
        add_dialogue_ledger_row(dialogue_log, ledger_row)
        append_text(dialogue_log, entry)

        self.append_task_event(
            from_agent,
            to_agent,
            dialogue_type,
            f"{request_id}: {summary}",
            artifact="logs/inter_agent_dialogue.md",
        )
        self.refresh_logs()
        self.set_status(f"Appended inter-agent dialogue entry {request_id}.")

    def next_request_id(self) -> str:
        stamp = utc_now().replace("-", "").replace(":", "").replace("+00:00", "Z")
        return f"REQ-{stamp}"

    def append_task_event(
        self,
        from_agent: str,
        to_agent: str,
        topic: str,
        summary: str,
        artifact: str = "",
    ) -> None:
        task_dir = self.selected_task_dir()
        timestamp = utc_now()
        entry = (
            f"\n## GUI Event - {timestamp}\n\n"
            f"- From: {from_agent}\n"
            f"- To: {to_agent}\n"
            f"- Topic: {topic}\n"
            f"- Artifact: {artifact or '(none)'}\n"
            "- Summary:\n"
            f"{indent_block(summary)}\n"
        )
        append_text(task_dir / "logs" / "agent_interactions.md", entry)

    def refresh_logs(self) -> None:
        task_dir = self.require_task()
        if task_dir is None:
            return
        self._set_view(self.agent_log_view, read_text(task_dir / "logs" / "agent_interactions.md"))
        self._set_view(self.dialogue_log_view, read_text(task_dir / "logs" / "inter_agent_dialogue.md"))
        self._set_view(self.override_log_view, read_text(task_dir / "logs" / "override_log.md"))
        self._set_view(self.brief_view, read_text(task_dir / "notes" / "task_brief.md"))
        self.set_status(f"Loaded logs from {task_dir}")

    def _set_view(self, widget: ScrolledText, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def clear_inputs(self) -> None:
        for widget in (self.goal_text, self.summary_text):
            widget.delete("1.0", "end")
        self.set_status("Cleared input boxes.")


def run_check() -> int:
    print("Tkinter GUI dependencies are available.")
    print(f"Workspace root: {ROOT}")
    print(f"Existing tasks: {', '.join(list_task_slugs()) or '(none)'}")
    return 0


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the multi-agent workflow GUI.")
    parser.add_argument("--check", action="store_true", help="Validate imports without opening the GUI.")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        return run_check()

    root = tk.Tk()
    MultiAgentGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
