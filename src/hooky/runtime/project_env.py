"""Project environment detection and test-command inference."""

from __future__ import annotations

import json
import re
import shlex

from pathlib import Path
from typing import Any

from hooky.runtime.evidence import read_text_prefix, relative_to
from hooky.runtime.text import dedupe_strings, single_line


def detect_project_environment(root: Path) -> dict[str, Any]:
    lockfiles = [
        name
        for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "uv.lock", "requirements.txt", "Cargo.lock", "go.sum")
        if (root / name).exists()
    ]
    package_json_path = root / "package.json"
    package_json: dict[str, Any] = {}
    scripts: dict[str, str] = {}
    package_manager = ""
    if package_json_path.exists():
        try:
            package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_json = {}
        scripts = {str(key): str(value) for key, value in (package_json.get("scripts") or {}).items()}
        package_manager = package_manager_from_package_json(package_json)
    if not package_manager:
        package_manager = package_manager_from_lockfiles(lockfiles)
    test_commands = likely_test_commands(root, package_manager, scripts)
    return {
        "package_manager": package_manager,
        "lockfiles": lockfiles,
        "scripts": scripts,
        "test_commands": test_commands,
        "has_package_json": package_json_path.exists(),
        "languages": language_hints(root),
    }


def package_manager_from_package_json(package_json: dict[str, Any]) -> str:
    raw = str(package_json.get("packageManager") or "")
    if raw.startswith("pnpm@"):
        return "pnpm"
    if raw.startswith("yarn@"):
        return "yarn"
    if raw.startswith("bun@"):
        return "bun"
    if raw.startswith("npm@"):
        return "npm"
    return ""


def package_manager_from_lockfiles(lockfiles: list[str]) -> str:
    if "pnpm-lock.yaml" in lockfiles:
        return "pnpm"
    if "yarn.lock" in lockfiles:
        return "yarn"
    if "bun.lockb" in lockfiles:
        return "bun"
    if "package-lock.json" in lockfiles:
        return "npm"
    if "uv.lock" in lockfiles:
        return "uv"
    if "Cargo.lock" in lockfiles:
        return "cargo"
    return "npm" if lockfiles and any(name.startswith("package") for name in lockfiles) else ""


def likely_test_commands(root: Path, package_manager: str, scripts: dict[str, str]) -> list[str]:
    commands: list[str] = []
    if scripts.get("test"):
        commands.append(f"{package_manager or 'npm'} test")
    if (root / "playwright.config.js").exists() or (root / "playwright.config.cjs").exists() or (root / "playwright.config.mjs").exists():
        runner = "npx"
        if package_manager == "pnpm":
            runner = "pnpm exec"
        elif package_manager == "yarn":
            runner = "yarn"
        elif package_manager == "bun":
            runner = "bunx"
        commands.append(f"{runner} playwright test")
    if (root / "pytest.ini").exists() or (root / "tests").exists() and any((root / "tests").glob("test_*.py")):
        commands.append("pytest")
    if (root / "Cargo.toml").exists():
        commands.append("cargo test")
    if (root / "go.mod").exists():
        commands.append("go test ./...")
    return dedupe_strings(commands)


def language_hints(root: Path) -> list[str]:
    hints = []
    markers = {
        "javascript": ["package.json"],
        "python": ["pyproject.toml", "requirements.txt"],
        "rust": ["Cargo.toml"],
        "go": ["go.mod"],
        "php": ["composer.json"],
        "ruby": ["Gemfile"],
    }
    for language, names in markers.items():
        if any((root / name).exists() for name in names):
            hints.append(language)
    return hints


def default_test_command(environment: dict[str, Any], list_only: bool) -> str:
    commands = environment.get("test_commands") if isinstance(environment.get("test_commands"), list) else []
    if commands:
        command = str(commands[0])
    else:
        package_manager = str(environment.get("package_manager") or "npm")
        command = f"{package_manager} test"
    if list_only and "playwright test" in command and " --list" not in command:
        command += " --list"
    return command


def append_shell_arg(command: str, value: str) -> str:
    return command + " " + shlex.quote(value)


def append_test_name_filter(command: str, test_name: str) -> str:
    if "playwright test" in command:
        return command + " -g " + shlex.quote(test_name)
    if "pytest" in command:
        return command + " -k " + shlex.quote(test_name)
    return command


def normalize_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def parse_test_output(output: str, returncode: int) -> dict[str, Any]:
    failed_tests = extract_failed_test_names(output)
    counts = parse_test_counts(output)
    passed = returncode == 0
    return {
        "passed": passed,
        "failed": not passed,
        "failed_tests": failed_tests[:50],
        "counts": counts,
        "truncated": len(failed_tests) > 50,
    }


def latest_failed_run_tests_event(tool_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(tool_events):
        if event.get("name") != "run_tests":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False:
            return event
    return None


def latest_error_context_artifacts(root: Path, max_artifacts: int, max_bytes: int) -> list[dict[str, Any]]:
    if max_artifacts <= 0:
        return []
    candidates = sorted(
        (path for path in (root / "test-results").rglob("error-context.md") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    artifacts: list[dict[str, Any]] = []
    for path in candidates[:max_artifacts]:
        content = read_text_prefix(path, max_bytes)
        artifacts.append(
            {
                "path": relative_to(path, root),
                "bytes": path.stat().st_size,
                "content": content,
                "truncated": path.stat().st_size > len(content.encode("utf-8")),
            }
        )
    return artifacts


def render_failure_context_bundle(
    *,
    command: str,
    returncode: Any,
    timed_out: bool,
    output_path: str,
    failed_tests: list[str],
    output_tail: str,
    artifacts: list[dict[str, Any]],
) -> str:
    lines = [
        "# Latest Test Failure Context",
        "",
        "## Command",
        "",
        f"- command: `{command}`",
        f"- returncode: {returncode}",
        f"- timed_out: {timed_out}",
        f"- output_path: `{output_path}`",
        "",
        "## Failed Tests",
        "",
    ]
    if failed_tests:
        lines.extend(f"- {single_line(item, 500)}" for item in failed_tests[:20])
    else:
        lines.append("- No individual failed test names were parsed from output.")
    lines.extend(["", "## Output Tail", "", "```text", output_tail.strip(), "```", ""])
    if artifacts:
        lines.extend(["## Related Error Context Artifacts", ""])
        for artifact in artifacts:
            lines.extend(
                [
                    f"### `{artifact['path']}`",
                    "",
                    f"- bytes: {artifact['bytes']}",
                    f"- truncated: {artifact['truncated']}",
                    "",
                    "```markdown",
                    str(artifact.get("content") or "").strip(),
                    "```",
                    "",
                ]
            )
    else:
        lines.extend(["## Related Error Context Artifacts", "", "No `test-results/**/error-context.md` artifacts were found.", ""])
    return "\n".join(lines)


def extract_failed_test_names(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if re.match(r"^\d+\)\s+", stripped):
            names.append(re.sub(r"\s+", " ", stripped))
        elif "›" in stripped and ("failed" in stripped.lower() or re.search(r"^\d+\)", stripped)):
            names.append(re.sub(r"\s+", " ", stripped))
    return dedupe_strings(names)


def parse_test_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("failed", "passed", "skipped", "timed out"):
        match = re.search(rf"(\d+)\s+{re.escape(key)}", output, flags=re.IGNORECASE)
        if match:
            counts[key.replace(" ", "_")] = int(match.group(1))
    return counts

