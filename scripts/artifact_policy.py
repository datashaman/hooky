#!/usr/bin/env python3
"""Shared artifact policy checks for SDLC agent evals."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def pass_report(**extra: Any) -> dict[str, Any]:
    report = {
        "status": "pass",
        "findings": [],
        "required_files": [],
        "unexpected_files": [],
        "protected_changes": [],
    }
    report.update(extra)
    return report


def fail_report(findings: list[str], **extra: Any) -> dict[str, Any]:
    report = pass_report(**extra)
    report["status"] = "fail"
    report["findings"] = findings
    return report


def validate_files(
    root: Path,
    *,
    required_files: list[str] | set[str] | None = None,
    allowed_files: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    required = sorted(set(required_files or []))
    allowed = set(allowed_files or [])
    produced = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()} if root.exists() else set()
    missing = [path for path in required if path not in produced]
    unexpected = sorted(produced - allowed) if allowed else []
    findings = []
    if missing:
        findings.append(f"missing required artifact files: {missing}")
    if unexpected:
        findings.append(f"unexpected artifact files: {unexpected}")
    return fail_report(
        findings,
        required_files=required,
        unexpected_files=unexpected,
        protected_changes=[],
        produced_files=sorted(produced),
    ) if findings else pass_report(
        required_files=required,
        unexpected_files=[],
        protected_changes=[],
        produced_files=sorted(produced),
    )


def snapshot(
    root: Path,
    *,
    include_prefixes: list[str] | set[str] | None = None,
    exclude_prefixes: list[str] | set[str] | None = None,
) -> dict[str, str]:
    includes = tuple(normalize_prefix(prefix) for prefix in (include_prefixes or []))
    excludes = tuple(normalize_prefix(prefix) for prefix in (exclude_prefixes or []))
    hashes = {}
    if not root.exists():
        return hashes
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if includes and not path_matches_prefix(relative, includes):
            continue
        if path_matches_prefix(relative, excludes):
            continue
        hashes[relative] = hash_file(path)
    return hashes


def protected_changes(
    root: Path,
    before: dict[str, str],
    *,
    include_prefixes: list[str] | set[str] | None = None,
    exclude_prefixes: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    after = snapshot(root, include_prefixes=include_prefixes, exclude_prefixes=exclude_prefixes)
    changed = sorted(path for path, digest in before.items() if after.get(path) != digest)
    added = sorted(path for path in after if path not in before)
    removed = sorted(path for path in before if path not in after)
    protected = sorted(set(changed + added + removed))
    findings = [f"protected files changed: {protected}"] if protected else []
    return fail_report(
        findings,
        protected_changes=protected,
        changed_files=changed,
        added_files=added,
        removed_files=removed,
    ) if findings else pass_report(
        protected_changes=[],
        changed_files=[],
        added_files=[],
        removed_files=[],
    )


def merge_reports(*reports: dict[str, Any]) -> dict[str, Any]:
    merged = pass_report()
    merged["required_files"] = []
    merged["unexpected_files"] = []
    merged["protected_changes"] = []
    for report in reports:
        merged["findings"].extend(report.get("findings", []))
        merged["required_files"].extend(report.get("required_files", []))
        merged["unexpected_files"].extend(report.get("unexpected_files", []))
        merged["protected_changes"].extend(report.get("protected_changes", []))
    merged["required_files"] = sorted(set(merged["required_files"]))
    merged["unexpected_files"] = sorted(set(merged["unexpected_files"]))
    merged["protected_changes"] = sorted(set(merged["protected_changes"]))
    merged["status"] = "fail" if merged["findings"] else "pass"
    return merged


def normalize_prefix(prefix: str) -> str:
    return prefix.strip("/")


def path_matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()
