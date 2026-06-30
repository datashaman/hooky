from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = [
    REPO_ROOT / "tests/fixtures/projects/todomvc",
    REPO_ROOT / "tests/fixtures/builder_agent/todomvc_approved_tests",
]


class TodoMvcFixtureTests(unittest.TestCase):
    def test_todomvc_fixtures_are_runnable_vite_shells(self) -> None:
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.relative_to(REPO_ROOT).as_posix()):
                package = json.loads((fixture / "package.json").read_text(encoding="utf-8"))
                scripts = package.get("scripts") or {}
                dependencies = package.get("dependencies") or {}
                self.assertIn("dev", scripts)
                self.assertIn("build", scripts)
                self.assertIn("test", scripts)
                self.assertIn("vite", scripts["dev"])
                self.assertIn("vite build", scripts["build"])
                self.assertIn("todomvc-app-css", dependencies)
                self.assertIn("todomvc-common", dependencies)
                self.assertTrue((fixture / "index.html").exists())
                self.assertTrue((fixture / "src/main.jsx").exists())
                self.assertTrue((fixture / "src/App.jsx").exists())
                self.assertTrue((fixture / "src/styles.css").exists())
                index = (fixture / "index.html").read_text(encoding="utf-8")
                main = (fixture / "src/main.jsx").read_text(encoding="utf-8")
                self.assertIn('id="root"', index)
                self.assertIn('/src/main.jsx', index)
                self.assertIn("todomvc-common/base.css", main)
                self.assertIn("todomvc-app-css/index.css", main)


if __name__ == "__main__":
    unittest.main()
