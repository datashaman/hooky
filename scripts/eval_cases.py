#!/usr/bin/env python3
"""Shared helpers for multi-case model evals."""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import artifact_policy


class AttemptTimeoutError(TimeoutError):
    pass


def load_cases(cases_root: Path) -> list[dict[str, Any]]:
    cases = []
    for manifest_path in sorted(cases_root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases.append({"path": manifest_path.parent, "manifest": manifest})
    if not cases:
        raise RuntimeError(f"No eval cases found under {cases_root}")
    return cases


def combined_tool_use(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"tool_calls": 0, "tools": {}, "compactions": 0, "pre_compaction_archives": 0}
    for result in case_results:
        tool_use = result.get("tool_use") or {}
        summary["tool_calls"] += int(tool_use.get("tool_calls") or 0)
        summary["compactions"] += int(tool_use.get("compactions") or 0)
        summary["pre_compaction_archives"] += int(tool_use.get("pre_compaction_archives") or 0)
        for name, count in (tool_use.get("tools") or {}).items():
            summary["tools"][name] = summary["tools"].get(name, 0) + int(count)
    return summary


def combined_artifact_policy(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    return artifact_policy.merge_reports(*(result.get("artifact_policy") or {} for result in case_results))


class attempt_time_limit:
    def __init__(self, label: str, seconds: int):
        self.label = label
        self.seconds = max(1, seconds)
        self.previous_handler = None

    def __enter__(self):
        self.previous_handler = signal.getsignal(signal.SIGALRM)

        def timeout_handler(_signum, _frame):
            raise AttemptTimeoutError(f"{self.label} timed out after {self.seconds}s")

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, _exc_type, _exc, _tb):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous_handler)
