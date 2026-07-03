"""Evaluator-evidence, visual-evidence, and placeholder-test validation for loop attempts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hooky import roles, runtime
from hooky.cli.paths import loop_attempt_dir, loop_contract_path, loop_feature_list_path, loop_proposal_path


def enforce_loop_evaluator_evidence(workspace: Path, attempt_id: str, report: dict[str, Any]) -> dict[str, Any]:
    if report.get("status") != "pass":
        return report
    evidence_failures: list[str] = []
    contract_text = loop_contract_path(workspace).read_text(encoding="utf-8", errors="ignore") if loop_contract_path(workspace).exists() else ""
    rubric_restart_required = roles.taste_rubric_required(contract_text) and not roles.taste_rubric_is_substantive(contract_text)
    if rubric_restart_required:
        evidence_failures.append("Evaluator cannot pass this attempt: subjective quality is requested but contract.md does not define a substantive Taste Rubric.")
    if has_placeholder_only_tests(workspace):
        evidence_failures.append("Evaluator cannot pass this attempt: the executable test suite appears to contain only placeholder tests.")
    events = loop_attempt_tool_events(workspace, attempt_id)
    test_failures = loop_attempt_test_evidence_failures(events)
    evidence_failures.extend(test_failures)
    if loop_attempt_requires_visual_evidence(workspace) and not loop_attempt_captured_visual_snapshot(events):
        evidence_failures.append("Evaluator cannot pass this attempt: browser/UI work needs capture_visual_snapshot evidence from the running app.")
    reference_visual_failures = loop_attempt_reference_visual_evidence_failures(workspace, events)
    evidence_failures.extend(reference_visual_failures)
    visual_failures = loop_attempt_blocking_visual_failures(workspace, events, report)
    evidence_failures.extend(visual_failures)
    if not evidence_failures:
        return report
    amended = dict(report)
    findings = amended.get("findings") if isinstance(amended.get("findings"), list) else []
    amended["findings"] = [*findings, *evidence_failures]
    amended["status"] = "fail"
    amended["recommendation"] = "restart-contract" if rubric_restart_required else "continue"
    current_bottleneck = amended.get("bottleneck")
    if rubric_restart_required:
        amended["bottleneck"] = "missing_taste_rubric"
    elif test_failures:
        amended["bottleneck"] = "verification_tests_failed"
    elif not isinstance(current_bottleneck, str) or not current_bottleneck.strip() or current_bottleneck == "none_visible_after_trace_review":
        amended["bottleneck"] = "verification_evidence"
    current_score = amended.get("score")
    amended["score"] = min(float(current_score), 0.5) if isinstance(current_score, int | float) else 0.5
    return amended


def loop_attempt_test_evidence_failures(events: list[dict[str, Any]]) -> list[str]:
    test_events = [event for event in events if tool_event_is_test_execution(event)]
    if not test_events:
        return []
    latest = test_events[-1]
    result = latest.get("result")
    if not isinstance(result, dict):
        return ["Evaluator cannot pass this attempt: latest executable test evidence is malformed."]
    if tool_result_passed(result):
        return []
    command = str(result.get("command") or "test command")
    reason = str(result.get("error") or result.get("summary") or result.get("output_tail") or "failed")
    return [f"Evaluator cannot pass this attempt: latest executable test evidence failed ({command}): {runtime.single_line(reason, 240)}"]


def tool_event_is_test_execution(event: dict[str, Any]) -> bool:
    name = str(event.get("name") or "")
    if name == "run_tests":
        return True
    if name != "bash":
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    command = str(result.get("command") or "").lower()
    test_terms = (
        "npm test",
        "pnpm test",
        "yarn test",
        "pytest",
        "playwright test",
        "vitest",
        "jest",
    )
    return any(term in command for term in test_terms)


def tool_result_passed(result: dict[str, Any]) -> bool:
    if result.get("ok") is True:
        return True
    if result.get("passed") is True:
        return True
    if result.get("returncode") == 0 and result.get("timed_out") is not True and result.get("failed") is not True:
        return True
    return False


def loop_attempt_blocking_visual_failures(workspace: Path, events: list[dict[str, Any]], report: dict[str, Any]) -> list[str]:
    if not loop_attempt_requires_visual_evidence(workspace):
        return []
    failures: list[str] = []
    failures.extend(blocking_visual_failures_from_snapshot_events(events))
    findings_text = " ".join(str(item) for item in report.get("findings", []) if isinstance(item, str)).lower()
    bottleneck_text = str(report.get("bottleneck") or "").lower()
    report_text = f"{findings_text} {bottleneck_text}"
    if report_mentions_blocking_visual_defect(report_text):
        failures.append("Evaluator cannot pass this attempt: visual findings report clipped/off-screen/overflowing primary UI content.")
    return runtime.dedupe_strings(failures)


def loop_attempt_reference_visual_evidence_failures(workspace: Path, events: list[dict[str, Any]]) -> list[str]:
    if not loop_attempt_requires_reference_visual_evidence(workspace):
        return []
    count = loop_attempt_visual_snapshot_count(events)
    if count >= 2:
        return []
    return ["Evaluator cannot pass this attempt: reference/canonical UI work needs visual snapshots from multiple states, not only first load."]


def loop_attempt_tool_events(workspace: Path, attempt_id: str) -> list[dict[str, Any]]:
    tool_events = loop_attempt_dir(workspace, attempt_id) / "traces" / "tool_events.json"
    if not tool_events.exists():
        return []
    try:
        events = json.loads(tool_events.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def blocking_visual_failures_from_snapshot_events(events: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for event in events:
        if event.get("name") != "capture_visual_snapshot":
            continue
        result = event.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            continue
        content_bounds = metrics.get("contentBounds")
        if isinstance(content_bounds, dict) and numeric_less_than(content_bounds.get("y"), 0):
            failures.append("Evaluator cannot pass this attempt: visual snapshot primary content starts above the viewport.")
        if metrics.get("horizontalOverflow") is True:
            failures.append("Evaluator cannot pass this attempt: visual snapshot has horizontal overflow.")
        overlaps = metrics.get("sampleHeadingInteractiveOverlaps")
        if isinstance(overlaps, list) and overlaps:
            failures.append("Evaluator cannot pass this attempt: visual snapshot has heading/control overlap.")
        clipped = metrics.get("sampleClippedElements")
        if isinstance(clipped, list):
            for item in clipped:
                if clipped_item_is_blocking(item):
                    failures.append("Evaluator cannot pass this attempt: visual snapshot has clipped visible text or controls.")
                    break
    return runtime.dedupe_strings(failures)


def numeric_less_than(value: object, limit: float) -> bool:
    return isinstance(value, int | float) and float(value) < limit


def clipped_item_is_blocking(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    tag = str(item.get("tag") or "").lower()
    role = str(item.get("role") or "").lower()
    text = str(item.get("text") or item.get("label") or "").strip()
    blocking_tags = {"h1", "h2", "h3", "button", "input", "textarea", "select", "a", "label"}
    blocking_roles = {"button", "link", "textbox", "checkbox", "radio", "combobox", "heading"}
    return bool(text) or tag in blocking_tags or role in blocking_roles


def report_mentions_blocking_visual_defect(text: str) -> bool:
    if not text:
        return False
    visual_terms = (
        "clipped",
        "cut off",
        "cut-off",
        "off-screen",
        "offscreen",
        "above the viewport",
        "outside the viewport",
        "overflow",
        "overlap",
        "hidden primary",
    )
    primary_terms = (
        "heading",
        "title",
        "text",
        "content",
        "control",
        "button",
        "input",
        "link",
        "todos",
        "primary",
        "ui",
        "layout",
    )
    decorative_terms = ("decorative", "background", "ornament", "purely decorative")
    if any(term in text for term in decorative_terms) and not any(term in text for term in primary_terms):
        return False
    return any(term in text for term in visual_terms) and any(term in text for term in primary_terms)


def has_placeholder_only_tests(workspace: Path) -> bool:
    test_root = workspace / "tests"
    if not test_root.exists():
        return False
    test_files = [path for path in test_root.rglob("*") if path.is_file() and path.suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py"} and path.name not in {".gitkeep"}]
    if not test_files:
        return False
    return all(is_placeholder_test_file(path) for path in test_files)


def is_placeholder_test_file(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    compact = " ".join(content.lower().split())
    markers = [
        "dummy test",
        "placeholder test",
        "expect(true).tobe(true)",
        "expect(true).toequal(true)",
        "assert true",
        "assert.true",
        "self.asserttrue(true)",
    ]
    return any(marker in compact for marker in markers)


def loop_attempt_requires_visual_evidence(workspace: Path) -> bool:
    contract_text = ""
    for path in [loop_contract_path(workspace), loop_feature_list_path(workspace)]:
        if path.exists():
            contract_text += "\n" + path.read_text(encoding="utf-8", errors="ignore").lower()
    ui_words = ("browser", "ui", "visual", "layout", "css", "html", "react", "vite", "vue", "svelte", "angular")
    if any(word in contract_text for word in ui_words):
        return True
    package_path = workspace / "package.json"
    if not package_path.exists():
        return False
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    return any(name in dependencies for name in ("react", "vue", "svelte", "@angular/core", "vite", "@vitejs/plugin-react"))


def loop_attempt_requires_reference_visual_evidence(workspace: Path) -> bool:
    text = ""
    for path in [loop_contract_path(workspace), loop_feature_list_path(workspace), loop_proposal_path(workspace)]:
        if path.exists():
            text += "\n" + path.read_text(encoding="utf-8", errors="ignore")
    return (
        roles.reference_visual_rubric_required(text)
        or roles.taste_rubric_is_substantive(text)
        and any(term in text.lower() for term in ("reference", "canonical", "template", "official css", "todomvc"))
    )


def loop_attempt_captured_visual_snapshot(events: list[dict[str, Any]]) -> bool:
    return loop_attempt_visual_snapshot_count(events) > 0


def loop_attempt_visual_snapshot_count(events: list[dict[str, Any]]) -> int:
    count = 0
    for event in events:
        if event.get("name") != "capture_visual_snapshot":
            continue
        result = event.get("result")
        if isinstance(result, dict) and result.get("ok") is True and result.get("screenshot_path"):
            count += 1
    return count
