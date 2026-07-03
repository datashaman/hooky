"""Auto-split from agent_runtime.py — see docs for module boundaries."""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.request

from pathlib import Path
from typing import Any

from hooky.runtime.project_env import normalize_subprocess_output


def wait_for_http_url(url: str, wait_seconds: int) -> bool:
    deadline = time.monotonic() + max(wait_seconds, 0)
    while time.monotonic() <= deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def run_shell_command(command: str, *, cwd: Path, timeout_seconds: int) -> tuple[subprocess.CompletedProcess[str], bool]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return subprocess.CompletedProcess(command, process.returncode, stdout=stdout or "", stderr=stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return (
            subprocess.CompletedProcess(
                command,
                124,
                stdout=normalize_subprocess_output(stdout or exc.stdout),
                stderr=normalize_subprocess_output(stderr or exc.stderr),
            ),
            True,
        )


def long_running_bash_violation(command: str) -> str | None:
    lowered = " ".join(command.lower().split())
    if shell_backgrounds_process(command):
        return "bash command starts a background process; use start_process/read_process/stop_process instead"
    server_patterns = [
        "npm run dev",
        "npm run preview",
        "yarn dev",
        "yarn preview",
        "pnpm dev",
        "pnpm preview",
        "bun dev",
        "vite --host",
        "vite preview",
        "next dev",
        "python -m http.server",
        "python3 -m http.server",
        "rails server",
        "flask run",
        "uvicorn ",
    ]
    if any(pattern in lowered for pattern in server_patterns) and not any(help_flag in lowered for help_flag in (" --help", " -h")):
        return "bash command appears to start a long-running server; use start_process/read_process/stop_process instead"
    return None


def shell_backgrounds_process(command: str) -> bool:
    for index, char in enumerate(command):
        if char != "&":
            continue
        previous_char = command[index - 1] if index > 0 else ""
        next_char = command[index + 1] if index + 1 < len(command) else ""
        if previous_char == "&" or next_char == "&":
            continue
        if previous_char in {">", "<"}:
            continue
        return True
    return False


def stop_managed_process(process: subprocess.Popen[str]) -> bool:
    if process.poll() is not None:
        close_process_log(process)
        return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        close_process_log(process)
        return False
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    close_process_log(process)
    return True


def requested_ports_from_command(command: str) -> list[int]:
    ports: set[int] = set()
    tokens = shell_tokens_for_ports(command)
    for index, token in enumerate(tokens):
        if token in {"--port", "-p"} and index + 1 < len(tokens):
            add_port(ports, tokens[index + 1])
            continue
        for prefix in ("--port=", "-p=", "PORT=", "port="):
            if token.startswith(prefix):
                add_port(ports, token[len(prefix) :])
                break
    for match in re.finditer(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])[:/](\d{2,5})", command):
        add_port(ports, match.group(1))
    for match in re.finditer(r"(?<![\w.:-]):(\d{2,5})(?!\d)", command):
        add_port(ports, match.group(1))
    return sorted(ports)


def process_url_from_ports(ports: list[int]) -> str | None:
    if not ports:
        return None
    return f"http://127.0.0.1:{ports[0]}"


def shell_tokens_for_ports(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def add_port(ports: set[int], value: str) -> None:
    try:
        port = int(str(value).strip())
    except ValueError:
        return
    if 1 <= port <= 65535:
        ports.add(port)


def tcp_port_is_listening(port: int) -> bool:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def allocate_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def process_tree_pids(pid: int) -> set[int]:
    pids = {pid}
    try:
        completed = subprocess.run(["pgrep", "-P", str(pid)], text=True, capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return pids
    if completed.returncode not in {0, 1}:
        return pids
    for line in completed.stdout.splitlines():
        try:
            child = int(line.strip())
        except ValueError:
            continue
        if child not in pids:
            pids.update(process_tree_pids(child))
    return pids


def process_listeners(pid: int) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for candidate_pid in sorted(process_tree_pids(pid)):
        try:
            completed = subprocess.run(
                ["lsof", "-nP", "-a", "-p", str(candidate_pid), "-iTCP", "-sTCP:LISTEN"],
                text=True,
                capture_output=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            port = listener_port_from_name(parts[-2] if parts[-1] == "(LISTEN)" else parts[-1])
            if port is None:
                continue
            listeners.append(
                {
                    "pid": candidate_pid,
                    "command": parts[0],
                    "host": listener_host_from_name(parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]),
                    "port": port,
                }
            )
    return dedupe_listeners(listeners)


def listener_port_from_name(name: str) -> int | None:
    match = re.search(r":(\d+)(?:\s|$)", name)
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def listener_host_from_name(name: str) -> str:
    return name.rsplit(":", 1)[0]


def dedupe_listeners(listeners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any]] = set()
    deduped: list[dict[str, Any]] = []
    for listener in listeners:
        key = (listener.get("pid"), listener.get("host"), listener.get("port"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(listener)
    return deduped


def close_process_log(process: subprocess.Popen[str]) -> None:
    handle = getattr(process, "_hooky_log_handle", None)
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass

