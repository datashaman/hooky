"""Final-report and file-write validators enforced per loop role, plus taste-rubric helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hooky.roles.runner import read_loop_proposal, runtime_path, runtime_rel


def proposal_checklist_items(proposal: str) -> list[str]:
    items: list[str] = []
    for raw_line in proposal.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("- [ ] ", "- [x] ", "- [X] ")):
            items.append(line[6:].strip())
        elif line.startswith(("- ", "* ")):
            items.append(line[2:].strip())
        else:
            numbered = re.match(r"^\d+\.\s+(.+)$", line)
            if numbered:
                items.append(numbered.group(1).strip())
    return [item for item in items if item]


def validate_planner_report(report: dict[str, Any], working_folder: Path) -> None:
    contract_rel = runtime_rel("contract.md")
    if report.get("contract_path") != contract_rel:
        raise ValueError(f"planner must report contract_path={contract_rel}")
    contract = runtime_path(working_folder, "contract.md")
    if not contract.exists() or not contract.read_text(encoding="utf-8").strip():
        raise ValueError(f"planner must write non-empty {contract_rel}")


def validate_generator_contract_report(report: dict[str, Any], working_folder: Path) -> None:
    contract_rel = runtime_rel("contract.md")
    feature_list_rel = runtime_rel("feature_list.json")
    if report.get("contract_path") != contract_rel:
        raise ValueError(f"generator must report contract_path={contract_rel}")
    if report.get("feature_list_path") != feature_list_rel:
        raise ValueError(f"generator must report feature_list_path={feature_list_rel}")
    contract = runtime_path(working_folder, "contract.md")
    feature_list = runtime_path(working_folder, "feature_list.json")
    contract_text = contract.read_text(encoding="utf-8") if contract.exists() else ""
    if not contract.exists() or "## Done Criteria" not in contract_text:
        raise ValueError(f"generator must write done criteria into {contract_rel}")
    if reference_visual_rubric_required(read_loop_proposal(working_folder)) and not taste_rubric_is_substantive(contract_text):
        raise ValueError("generator must define a substantive Taste Rubric for reference visual requirements")
    if not feature_list.exists():
        raise ValueError(f"generator must write {feature_list_rel}")
    payload = json.loads(feature_list.read_text(encoding="utf-8"))
    if not isinstance(payload.get("features"), list):
        raise ValueError("feature_list.json must include features array")
    proposal_items = proposal_checklist_items(read_loop_proposal(working_folder))
    if proposal_items and len(payload["features"]) < len(proposal_items):
        raise ValueError(f"feature_list.json must cover every proposal checklist item: {len(payload['features'])} features for {len(proposal_items)} proposal items")


def validate_generator_contract_write(working_folder: Path, path: Path, content: str) -> None:
    relative = path.resolve().relative_to(working_folder.resolve()).as_posix()
    if relative == runtime_rel("contract.md"):
        if "## Done Criteria" not in content or len(content.strip()) < 100:
            raise ValueError("contract.md writes must preserve a substantive ## Done Criteria section")
        if reference_visual_rubric_required(read_loop_proposal(working_folder)) and not taste_rubric_is_substantive(content):
            raise ValueError("contract.md must include a substantive Taste Rubric for reference visual requirements")
        return
    if relative == runtime_rel("feature_list.json"):
        payload = json.loads(content)
        if not isinstance(payload.get("features"), list):
            raise ValueError("feature_list.json writes must include a features array")
        return
    raise ValueError(f"unexpected generator contract write: {relative}")


def validate_evaluator_contract_report(report: dict[str, Any], working_folder: Path) -> None:
    if not isinstance(report.get("accepted"), bool):
        raise ValueError("evaluator contract report must include accepted boolean")
    if not isinstance(report.get("review"), str) or not report["review"].strip():
        raise ValueError("evaluator contract report must include review text")
    changes = report.get("required_changes")
    if not isinstance(changes, list) or any(not isinstance(item, str) for item in changes):
        raise ValueError("evaluator contract report must include required_changes strings")
    if not report["accepted"] and not [item for item in changes if item.strip()]:
        raise ValueError("rejected contract must include required_changes")
    if report["accepted"]:
        contract = runtime_path(working_folder, "contract.md")
        contract_text = contract.read_text(encoding="utf-8") if contract.exists() else ""
        if reference_visual_rubric_required(read_loop_proposal(working_folder)) and not taste_rubric_is_substantive(contract_text):
            raise ValueError("accepted contract must include a substantive Taste Rubric for reference visual requirements")


def validate_generator_implementation_report(report: dict[str, Any], working_folder: Path) -> None:
    for key in ("changed_files", "tests_run", "failures"):
        value = report.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"generator implementation report must include {key} strings")
    for raw_path in report.get("changed_files", []):
        path = (working_folder / raw_path).resolve()
        try:
            relative = path.relative_to(working_folder.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"changed file is outside workspace: {raw_path}") from exc
        if relative.startswith(".hooky/"):
            raise ValueError(f"generator must not report .hooky changes: {raw_path}")


def validate_evaluator_attempt_report(report: dict[str, Any], working_folder: Path) -> None:
    if report.get("recommendation") not in {"continue", "restart-attempt", "restart-contract", "stop"}:
        raise ValueError("evaluator recommendation is invalid")
    if not isinstance(report.get("bottleneck"), str) or not report["bottleneck"].strip():
        raise ValueError("evaluator report must include a non-empty bottleneck")
    score = report.get("score")
    if not isinstance(score, int | float) or score < 0 or score > 1:
        raise ValueError("evaluator score must be between 0 and 1")
    findings = report.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise ValueError("evaluator findings must be strings")
    if report.get("status") == "fail" and not [item for item in findings if item.strip()]:
        raise ValueError("failed evaluator report must include findings")
    contract = runtime_path(working_folder, "contract.md")
    contract_text = contract.read_text(encoding="utf-8") if contract.exists() else ""
    if taste_rubric_is_substantive(contract_text):
        rubric_scores = report.get("rubric_scores")
        if not isinstance(rubric_scores, dict) or not rubric_scores:
            raise ValueError("evaluator report must include rubric_scores when contract.md defines a Taste Rubric")
        required_axes = {"design", "originality", "craft", "functionality"}
        missing_axes = sorted(required_axes.difference(str(key) for key in rubric_scores))
        if missing_axes:
            raise ValueError("rubric_scores missing required axes: " + ", ".join(missing_axes))
        if any(not isinstance(value, int | float) or value < 0 or value > 1 for value in rubric_scores.values()):
            raise ValueError("rubric_scores values must be between 0 and 1")
        if not isinstance(report.get("score_explanation"), str) or not report["score_explanation"].strip():
            raise ValueError("evaluator report must include score_explanation when grading a Taste Rubric")


def find_markdown_heading_line(lines: list[str], heading: str) -> int | None:
    marker = f"## {heading}".lower()
    for index, line in enumerate(lines):
        if line.strip().lower() == marker:
            return index
    return None


def find_markdown_section_end(lines: list[str], start: int) -> int:
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            return index
    return len(lines)


def markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    heading_line = find_markdown_heading_line(lines, heading)
    if heading_line is None:
        return ""
    start = heading_line + 1
    end = find_markdown_section_end(lines, start)
    return "\n".join(lines[start:end]).strip()


def markdown_without_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = find_markdown_heading_line(lines, heading)
    if start is None:
        return text
    end = find_markdown_section_end(lines, start + 1)
    return "\n".join(lines[:start] + lines[end:])


def taste_rubric_is_substantive(contract: str) -> bool:
    rubric = markdown_section(contract, "Taste Rubric").lower()
    if not rubric:
        return False
    stripped = re.sub(r"[\s_*`.-]+", " ", rubric).strip()
    if not stripped:
        return False
    if all(term in rubric for term in ("optional", "subjective")) and len(stripped) < 120:
        return False
    # An explicit "not applicable"/"n/a" disclaimer overrides the axis-keyword
    # scan below - otherwise a sentence like "no canonical-template reference
    # to grade against" gets misread as a substantive rubric just because it
    # contains the word "reference", even though it's a negation.
    if re.search(r"\bnot applicable\b|\bn/a\b", stripped):
        return False
    placeholders = ("optional", "required only", "subjective quality matters", "_")
    if any(axis in rubric for axis in ("design", "originality", "craft", "functionality", "weight", "reference")):
        return True
    return not any(term in rubric for term in placeholders)


def taste_rubric_required(contract: str) -> bool:
    without_rubric = markdown_without_section(contract, "Taste Rubric")
    taste_terms = (
        "taste",
        "aesthetic",
        "aesthetics",
        "beautiful",
        "polished",
        "delightful",
        "premium",
        "original",
        "originality",
        "brand",
        "branded",
        "visual design",
        "craft",
    )
    return any(term in without_rubric.lower() for term in taste_terms)


def reference_visual_rubric_required(text: str) -> bool:
    lower = text.lower()
    reference_terms = (
        "reference implementation",
        "reference site",
        "reference visual",
        "template",
        "canonical",
        "look and behave exactly",
        "visually match",
    )
    visual_terms = ("visual", "layout", "style", "css", "html", "ui", "screenshot")
    if any(term in lower for term in reference_terms) and any(term in lower for term in visual_terms):
        return True
    todomvc_terms = ("todomvc", "todomvc-app-css", "todomvc-common", "todoapp", "app-spec.md")
    return any(term in lower for term in todomvc_terms) and any(term in lower for term in ("canonical", "template", "official", "css", "visual"))
