#!/usr/bin/env python3
"""Create a lightweight multi-agent task workspace.

The tool is intentionally small and standard-library only so it can run on
Windows and Linux without project setup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Dict, Iterable, List, Mapping, Optional


DEFAULT_AGENTS = (
    "leader",
    "literature_collector",
    "mathematician",
    "code_expert",
    "latex_writer",
)

AGENT_ALIASES = {
    "literature": "literature_collector",
    "collector": "literature_collector",
    "lit": "literature_collector",
    "math": "mathematician",
    "algorithm": "code_expert",
    "experiment": "code_expert",
    "code": "code_expert",
    "latex": "latex_writer",
    "writer": "latex_writer",
}

DIRECTORIES = (
    "experiments",
    "experiments/src",
    "experiments/tests",
    "experiments/data",
    "experiments/outputs",
    "experiments/figures",
    "report",
    "notes",
    "logs",
)

TEMPLATE_DESTINATIONS = {
    "task_brief.md": ("README.md", "notes/task_brief.md"),
    "agent_interactions.md": ("logs/agent_interactions.md",),
    "literature_review.md": ("notes/literature_review.md",),
    "leader_summary.md": ("notes/leader_summary.md",),
    "override_directive.md": ("notes/override_directive.md",),
    "resource_registry.md": ("notes/resource_registry.md",),
}


class KeepUnknownFormatter(Formatter):
    """Leave unknown template placeholders untouched."""

    def get_value(self, key: object, args: Iterable[object], kwargs: Mapping[str, str]) -> str:
        if isinstance(key, str):
            return kwargs.get(key, "{" + key + "}")
        return Formatter.get_value(self, key, args, kwargs)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create tasks/<slug>/ for a multi-agent math/algorithm/research workflow."
    )
    parser.add_argument("title", help="Human-readable task title or problem name.")
    parser.add_argument(
        "--slug",
        help="Directory name under tasks/. Defaults to a slug made from title.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root. Defaults to the parent of this script directory.",
    )
    parser.add_argument(
        "--agents",
        default=",".join(DEFAULT_AGENTS),
        help="Comma-separated agent names to place in notes and logs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite generated files if the task directory already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the files that would be created without writing them.",
    )
    return parser.parse_args(argv)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or "task")[:64].strip("-") or "task"


def split_agents(value: str) -> List[str]:
    agents: List[str] = []
    for part in value.split(","):
        agent = part.strip()
        if not agent:
            continue
        canonical = AGENT_ALIASES.get(agent.lower(), agent)
        if canonical not in agents:
            agents.append(canonical)
    return agents or list(DEFAULT_AGENTS)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def render(template: str, values: Mapping[str, str]) -> str:
    return KeepUnknownFormatter().format(template, **values)


def read_template(root: Path, filename: str) -> str:
    path = root / "templates" / filename
    if not path.exists():
        raise FileNotFoundError(f"Missing template: {path}")
    return path.read_text(encoding="utf-8")


def latex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def generated_files(title: str, slug: str, agents: List[str]) -> Dict[str, str]:
    agent_comments = "\n".join(f"# - {agent}: TODO" for agent in agents)
    latex_title = latex_escape(title)
    manifest = {
        "version": 1,
        "slug": slug,
        "title": title,
        "agents": agents,
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
            "python_candidates": [
                "experiments/.venv/bin/python",
                ".venv/bin/python",
                "venv/bin/python",
                "experiments/.venv/Scripts/python.exe",
                ".venv/Scripts/python.exe",
                "venv/Scripts/python.exe",
            ],
            "test_commands": ["python -m unittest discover -s tests -v"],
            "report_commands": ["pdflatex main.tex"],
            "experiment_cwd": "experiments",
            "report_cwd": "report",
        },
        "workflow": {
            "primary_code_artifacts": ["experiments/src/solution.py"],
            "primary_test_artifacts": ["experiments/tests/test_solution.py"],
            "analysis_artifacts": ["experiments/run_experiment.py", "experiments/analysis.md"],
            "report_artifacts": ["report/main.tex", "notes/leader_summary.md"],
        },
    }
    return {
        "experiments/src/__init__.py": '"""Solution package for this task workspace."""\n',
        "experiments/src/solution.py": textwrap.dedent(
            f"""\
            \"\"\"Algorithm implementation entry point for {title}.\"\"\"

            from __future__ import annotations


            def solve(*args, **kwargs):
                \"\"\"Implement the main algorithm here.

                Replace the signature once the problem inputs are fixed.
                \"\"\"
                raise NotImplementedError("Implement solve() for this task")


            if __name__ == "__main__":
                print("Implement a CLI or smoke check in experiments/src/solution.py")
            """
        ),
        "experiments/tests/test_solution.py": textwrap.dedent(
            """\
            import unittest

            from src import solution


            class SolutionSmokeTest(unittest.TestCase):
                def test_solve_is_defined(self):
                    self.assertTrue(callable(solution.solve))


            if __name__ == "__main__":
                unittest.main()
            """
        ),
        "experiments/run_experiment.py": textwrap.dedent(
            f"""\
            \"\"\"Experiment runner for {title}.

            Keep experiments reproducible: record parameters, seeds, data
            sources, and the exact command used in ../logs/run_log.md.
            \"\"\"

            from __future__ import annotations

            import json
            from datetime import datetime, timezone
            from pathlib import Path


            def main() -> None:
                result = {{
                    "task": {title!r},
                    "slug": {slug!r},
                    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "status": "placeholder",
                    "metrics": {{}},
                }}
                out_dir = Path(__file__).resolve().parent / "outputs"
                out_dir.mkdir(exist_ok=True)
                out_path = out_dir / "latest.json"
                out_path.write_text(json.dumps(result, indent=2) + "\\n", encoding="utf-8")
                print(f"Wrote {{out_path}}")


            if __name__ == "__main__":
                main()
            """
        ),
        "experiments/analysis.md": textwrap.dedent(
            f"""\
            # Experiment Analysis: {title}

            Record experiment purpose, setup, metrics, results, anomalies, and
            whether the results support the mathematical or algorithmic claims.
            """
        ),
        "report/main.tex": textwrap.dedent(
            f"""\
            \\documentclass[11pt]{{article}}
            \\usepackage[margin=1in]{{geometry}}
            \\usepackage{{amsmath, amssymb, amsthm}}
            \\usepackage{{algorithm}}
            \\usepackage{{algpseudocode}}
            \\usepackage{{booktabs}}
            \\usepackage{{hyperref}}

            \\title{{{latex_title}}}
            \\author{{Multi-Agent Workflow}}
            \\date{{\\today}}

            \\begin{{document}}
            \\maketitle

            \\begin{{abstract}}
            Summarize the problem, theoretical result, algorithmic approach,
            and experimental validation.
            \\end{{abstract}}

            \\section{{Problem}}
            State the mathematical problem and assumptions.

            \\section{{Related Work}}
            Summarize the literature routes, baselines, datasets, and gaps.

            \\section{{Method}}
            Present definitions, lemmas, algorithm design, and complexity.

            \\section{{Experiments}}
            Describe reproducible experiments and report results.

            \\section{{Discussion}}
            Explain limitations, failed attempts, and next steps.

            \\bibliographystyle{{plain}}
            \\bibliography{{references}}
            \\end{{document}}
            """
        ),
        "report/references.bib": "% Add BibTeX entries here.\n",
        "logs/run_log.md": textwrap.dedent(
            f"""\
            # Run Log: {title}

            Record commands, datasets, parameters, seeds, and outputs.

            ## Entries

            - `{utc_now()}` workspace created by `tools/create_task.py`.
            """
        ),
        "logs/override_log.md": textwrap.dedent(
            f"""\
            # Super Admin Override Log: {title}

            Record user-forced direction corrections. The newest valid entry is
            the task direction source of truth after system, safety, and tool
            constraints.

            ## Entries

            No override has been issued yet.
            """
        ),
        "notes/open_questions.md": textwrap.dedent(
            f"""\
            # Open Questions: {title}

            {agent_comments}
            """
        ),
        "notes/task_manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }


def relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def write_file(path: Path, content: str, force: bool, dry_run: bool) -> str:
    if path.exists() and not force:
        return "exists"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    return "written"


def create_workspace(args: argparse.Namespace) -> int:
    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    title = args.title.strip()
    slug = slugify(args.slug or title)
    agents = split_agents(args.agents)
    task_dir = root / "tasks" / slug

    if task_dir.exists() and not args.force:
        print(
            f"Task directory already exists: {task_dir}\n"
            "Use --force to overwrite generated files.",
            file=sys.stderr,
        )
        return 2
    if task_dir.exists() and args.force:
        print(
            "WARNING: --force overwrites generated files with template TODO sections. "
            "Fill the task brief before starting the workflow.",
            file=sys.stderr,
        )

    created_at = utc_now()
    values = {
        "task_title": title,
        "slug": slug,
        "created_at": created_at,
        "date": created_at[:10],
        "project_root": str(root),
        "task_path": str(task_dir),
        "agents": ", ".join(agents),
        "agent_list": "\n".join(f"- {agent}" for agent in agents),
    }

    planned: Dict[str, str] = {}
    for directory in DIRECTORIES:
        if not args.dry_run:
            (task_dir / directory).mkdir(parents=True, exist_ok=True)

    for template_name, destinations in TEMPLATE_DESTINATIONS.items():
        rendered = render(read_template(root, template_name), values)
        for destination in destinations:
            planned[destination] = rendered

    planned.update(generated_files(title, slug, agents))

    statuses = []
    for destination, content in sorted(planned.items()):
        path = task_dir / destination
        status = write_file(path, content, args.force, args.dry_run)
        statuses.append((status, relative(path, root)))

    action = "Would create" if args.dry_run else "Created"
    print(f"{action} task workspace: {relative(task_dir, root)}")
    for status, path in statuses:
        marker = "skip " if status == "exists" else "write"
        print(f"  {marker} {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return create_workspace(args)


if __name__ == "__main__":
    raise SystemExit(main())
