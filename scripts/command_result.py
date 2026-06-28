#!/usr/bin/env python3
"""Shared subprocess result model for deterministic eval commands."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any


def run_command(command: list[str], cwd: Path, timeout: int, *, tail_chars: int = 4000) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        return {
            "command": command,
            "cwd": cwd.as_posix(),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-tail_chars:],
            "stderr": completed.stderr[-tail_chars:],
            "timed_out": False,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        return {
            "command": command,
            "cwd": cwd.as_posix(),
            "returncode": 124,
            "stdout": (exc.stdout or "")[-tail_chars:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-tail_chars:] if isinstance(exc.stderr, str) else "",
            "timed_out": True,
            "duration_ms": duration_ms,
        }


def redacted(result: dict[str, Any], *, tail_chars: int = 1200) -> dict[str, Any]:
    return {
        "command": result.get("command"),
        "cwd": result.get("cwd"),
        "returncode": result.get("returncode"),
        "timed_out": result.get("timed_out", False),
        "duration_ms": result.get("duration_ms"),
        "stdout_tail": str(result.get("stdout") or "")[-tail_chars:],
        "stderr_tail": str(result.get("stderr") or "")[-tail_chars:],
    }
