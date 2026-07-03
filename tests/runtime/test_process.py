from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from hooky.runtime import requested_ports_from_command, run_shell_command


class ProcessTests(unittest.TestCase):
    def test_requested_ports_are_parsed_from_common_explicit_forms(self) -> None:
        self.assertEqual(requested_ports_from_command("npm run dev -- --port 5173"), [5173])
        self.assertEqual(requested_ports_from_command("PORT=4173 npm start"), [4173])
        self.assertEqual(requested_ports_from_command("serve http://127.0.0.1:8080"), [8080])

    def test_timed_out_shell_command_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "marker"
            command = (
                'python3 -c "'
                "import pathlib, subprocess, time; "
                f"p=pathlib.Path({str(marker)!r}); "
                "subprocess.Popen(['python3','-c',"
                "'import pathlib,time; time.sleep(2); pathlib.Path(%r).write_text(\\\"leaked\\\")' % str(p)]); "
                "time.sleep(5)"
                '"'
            )

            completed, timed_out = run_shell_command(command, cwd=root, timeout_seconds=1)
            time.sleep(2.5)

            self.assertTrue(timed_out)
            self.assertEqual(completed.returncode, 124)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
