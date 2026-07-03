from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path

from hooky.runtime import ToolRuntime


class ProcessToolsTests(unittest.TestCase):
    def test_start_process_fails_when_requested_port_is_busy(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = int(sock.getsockname()[1])
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            runtime = ToolRuntime(working_folder=Path(tmp.name), final_report_schema={"type": "object"}, max_cost_usd=1, max_seconds=30)

            result = runtime.start_process({"command": f"PORT={port} python3 -m http.server"})

            self.assertFalse(result["ok"])
            self.assertIn(port, result["requested_ports"])
            self.assertIn("already in use", result["error"])

    def test_start_and_list_processes_include_detected_ports(self) -> None:
        if not lsof_available():
            self.skipTest("lsof is required for listener detection")
        with tempfile.TemporaryDirectory() as tmp:
            port = free_tcp_port()
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )
            command = (
                f'PORT={port} python3 -c "'
                "import os, socket, time; "
                "s=socket.socket(); "
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                "s.bind(('127.0.0.1', int(os.environ['PORT']))); "
                "s.listen(1); "
                "time.sleep(60)"
                '"'
            )

            result = runtime.start_process({"command": command, "wait_seconds": 1})
            try:
                self.assertTrue(result["ok"], result)
                self.assertIn(port, result["requested_ports"])
                self.assertIn(port, result["ports"])
                self.assertEqual(result["url"], f"http://127.0.0.1:{port}")
                listed = runtime.list_processes({})["processes"]
                self.assertEqual(len(listed), 1)
                self.assertIn(port, listed[0]["ports"])
                self.assertEqual(listed[0]["url"], f"http://127.0.0.1:{port}")
                read = runtime.read_process({"process_id": result["process_id"]})
                self.assertEqual(read["url"], f"http://127.0.0.1:{port}")
            finally:
                runtime.cleanup_processes()

    def test_start_process_can_auto_allocate_port_env(self) -> None:
        if not lsof_available():
            self.skipTest("lsof is required for listener detection")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ToolRuntime(
                working_folder=Path(tmp),
                final_report_schema={"type": "object"},
                max_cost_usd=1,
                max_seconds=30,
            )
            command = (
                'python3 -c "'
                "import os, socket, time; "
                "s=socket.socket(); "
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                "s.bind(('127.0.0.1', int(os.environ['PORT']))); "
                "s.listen(1); "
                "time.sleep(60)"
                '"'
            )

            result = runtime.start_process({"command": command, "auto_allocate_port": True, "wait_seconds": 1})
            try:
                self.assertTrue(result["ok"], result)
                self.assertIsInstance(result["allocated_port"], int)
                self.assertIn(result["allocated_port"], result["requested_ports"])
                self.assertIn(result["allocated_port"], result["ports"])
            finally:
                runtime.cleanup_processes()


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def lsof_available() -> bool:
    import shutil

    return shutil.which("lsof") is not None


if __name__ == "__main__":
    unittest.main()
