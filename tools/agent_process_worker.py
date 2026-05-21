#!/usr/bin/env python3
"""Execute one agent run inside a local subprocess."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from multiagent_web import run_agent_process_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one multi-agent job in a local subprocess.")
    parser.add_argument("--job", required=True, help="Path to the JSON job payload.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    job_path = Path(args.job)
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"job file not found: {job_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"invalid job json: {exc}", file=sys.stderr)
        return 2
    if not isinstance(job, dict):
        print("job payload must be a JSON object", file=sys.stderr)
        return 2
    return run_agent_process_job(job)


if __name__ == "__main__":
    raise SystemExit(main())
