from __future__ import annotations

import unittest

from hooky.runtime import available_tool_names


class SchemasTests(unittest.TestCase):
    def test_available_tools_include_visual_snapshot(self) -> None:
        self.assertIn("capture_visual_snapshot", available_tool_names())

    def test_available_tools_include_latest_test_failure_context(self) -> None:
        self.assertIn("latest_test_failure_context", available_tool_names())

    def test_available_tools_include_edit_files(self) -> None:
        self.assertIn("edit_files", available_tool_names())

    def test_available_tools_include_time_extension_request(self) -> None:
        self.assertIn("request_time_extension", available_tool_names())

    def test_available_tools_include_search_files(self) -> None:
        names = available_tool_names()

        self.assertIn("search_files", names)

    def test_available_tools_include_evidence_capture(self) -> None:
        names = available_tool_names()
        self.assertIn("append_evidence_note", names)
        self.assertIn("append_evidence_command", names)
        self.assertIn("append_evidence_screenshot", names)


if __name__ == "__main__":
    unittest.main()
