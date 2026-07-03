"""Managed background-process tools, mixed into ToolRuntime."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

from hooky.runtime.evidence import read_tail, relative_to
from hooky.runtime.process import (
    allocate_tcp_port,
    process_listeners,
    process_url_from_ports,
    requested_ports_from_command,
    stop_managed_process,
    tcp_port_is_listening,
    wait_for_http_url,
)


class ProcessToolsMixin:
    """Start/stop/inspect managed background process tool handlers."""

    def start_process(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args["command"])
        wait_for_url = str(args.get("wait_for_url") or "").strip()
        requested_ports = set(requested_ports_from_command(command + " " + wait_for_url))
        explicit_port = int(args.get("port") or 0)
        if explicit_port:
            requested_ports.add(explicit_port)
        allocated_port = None
        env = os.environ.copy()
        if bool(args.get("auto_allocate_port")):
            allocated_port = explicit_port if explicit_port else allocate_tcp_port()
            requested_ports.add(allocated_port)
            env["PORT"] = str(allocated_port)
            env["HOOKY_PORT"] = str(allocated_port)
        requested_ports_list = sorted(requested_ports)
        busy_ports = [port for port in requested_ports_list if tcp_port_is_listening(port)]
        if busy_ports:
            return {
                "ok": False,
                "error": "requested port already in use: " + ", ".join(str(port) for port in busy_ports),
                "command": command,
                "requested_ports": requested_ports_list,
                "allocated_port": allocated_port,
                "ports": [],
                "listeners": [],
            }
        process_id = f"proc-{self.next_process_id}"
        self.next_process_id += 1
        log_path = self.working_folder / ".hooky" / "managed-processes" / f"{process_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=self.working_folder,
            shell=True,
            text=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        process._hooky_log_handle = log_handle  # type: ignore[attr-defined]
        process._hooky_log_path = log_path  # type: ignore[attr-defined]
        process._hooky_command = command  # type: ignore[attr-defined]
        process._hooky_name = str(args.get("name") or "process")  # type: ignore[attr-defined]
        process._hooky_requested_ports = requested_ports_list  # type: ignore[attr-defined]
        process._hooky_allocated_port = allocated_port  # type: ignore[attr-defined]
        self.managed_processes[process_id] = process
        wait_seconds = int(args.get("wait_seconds") or 0)
        ready = False
        if wait_for_url:
            ready = wait_for_http_url(wait_for_url, wait_seconds)
        elif wait_seconds > 0:
            time.sleep(wait_seconds)
        listeners = process_listeners(process.pid)
        ports = sorted({int(item["port"]) for item in listeners})
        url = process_url_from_ports(ports)
        return {
            "ok": process.poll() is None,
            "process_id": process_id,
            "pid": process.pid,
            "name": process._hooky_name,  # type: ignore[attr-defined]
            "command": command,
            "requested_ports": requested_ports_list,
            "allocated_port": allocated_port,
            "ports": ports,
            "url": url,
            "listeners": listeners,
            "log_path": relative_to(log_path, self.working_folder),
            "ready": ready if wait_for_url else None,
            "returncode": process.poll(),
            "output": read_tail(log_path, 4000),
        }

    def read_process(self, args: dict[str, Any]) -> dict[str, Any]:
        process = self.require_process(str(args["process_id"]))
        log_path = process._hooky_log_path  # type: ignore[attr-defined]
        listeners = process_listeners(process.pid)
        ports = sorted({int(item["port"]) for item in listeners})
        return {
            "ok": True,
            "process_id": str(args["process_id"]),
            "running": process.poll() is None,
            "returncode": process.poll(),
            "ports": ports,
            "url": process_url_from_ports(ports),
            "listeners": listeners,
            "output": read_tail(log_path, int(args.get("max_bytes") or 8000)),
        }

    def stop_process(self, args: dict[str, Any]) -> dict[str, Any]:
        process_id = str(args["process_id"])
        process = self.require_process(process_id)
        stopped = stop_managed_process(process)
        self.managed_processes.pop(process_id, None)
        return {
            "ok": True,
            "process_id": process_id,
            "stopped": stopped,
            "returncode": process.poll(),
            "output": read_tail(process._hooky_log_path, 4000),  # type: ignore[attr-defined]
        }

    def list_processes(self, _args: dict[str, Any]) -> dict[str, Any]:
        processes = []
        for process_id, process in self.managed_processes.items():
            listeners = process_listeners(process.pid)
            ports = sorted({int(item["port"]) for item in listeners})
            processes.append(
                {
                    "process_id": process_id,
                    "pid": process.pid,
                    "name": process._hooky_name,  # type: ignore[attr-defined]
                    "command": process._hooky_command,  # type: ignore[attr-defined]
                    "running": process.poll() is None,
                    "returncode": process.poll(),
                    "requested_ports": list(getattr(process, "_hooky_requested_ports", requested_ports_from_command(process._hooky_command))),  # type: ignore[attr-defined]
                    "allocated_port": getattr(process, "_hooky_allocated_port", None),
                    "ports": ports,
                    "url": process_url_from_ports(ports),
                    "listeners": listeners,
                    "log_path": relative_to(process._hooky_log_path, self.working_folder),  # type: ignore[attr-defined]
                }
            )
        return {
            "ok": True,
            "processes": processes,
        }

    def require_process(self, process_id: str) -> subprocess.Popen[str]:
        process = self.managed_processes.get(process_id)
        if process is None:
            raise ValueError(f"unknown managed process: {process_id}")
        return process

    def cleanup_processes(self) -> None:
        for process_id, process in list(self.managed_processes.items()):
            stop_managed_process(process)
            self.managed_processes.pop(process_id, None)
