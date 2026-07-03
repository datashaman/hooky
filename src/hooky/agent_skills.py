from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    path: Path
    body: str


def discover_skills(working_folder: Path) -> list[AgentSkill]:
    skills: dict[str, AgentSkill] = {}
    for root in skill_roots(working_folder):
        for path in sorted(root.glob("*/SKILL.md")):
            skill = read_skill(path)
            if skill:
                skills[skill.name] = skill
    return [skills[name] for name in sorted(skills)]


def skill_roots(working_folder: Path) -> list[Path]:
    roots = [
        BUNDLED_SKILLS_ROOT,
        Path.home() / ".agents" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    for env_name in ("HOOKY_SKILL_ROOTS", "AGENT_SKILL_ROOTS", "SKILLS_PATH"):
        for raw_path in os.environ.get(env_name, "").split(os.pathsep):
            if raw_path.strip():
                roots.append(Path(raw_path).expanduser())
    roots.append(working_folder / ".agents" / "skills")
    deduped: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            deduped.append(resolved)
    return deduped


def read_skill(path: Path) -> AgentSkill | None:
    text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    name = str(metadata.get("name") or path.parent.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not name:
        return None
    return AgentSkill(name=name, description=description, path=path, body=body.strip())


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    metadata: dict[str, Any] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value in {"|", ">"}:
            block_lines: list[str] = []
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if next_line and not next_line.startswith((" ", "\t")) and ":" in next_line:
                    break
                block_lines.append(next_line.strip())
                index += 1
            metadata[key] = "\n".join(block_lines).strip() if value == "|" else " ".join(item for item in block_lines if item).strip()
            continue
        metadata[key] = value.strip('"').strip("'")
        index += 1
    return metadata, body


def skill_catalog(skills: list[AgentSkill]) -> str:
    if not skills:
        return "Available Agent Skills: none"
    lines = ["Available Agent Skills (load details on demand with activate_skill):"]
    for skill in skills:
        detail = f" - {skill.name}"
        if skill.description:
            detail += f": {skill.description}"
        lines.append(detail)
    return "\n".join(lines)


def skill_context(skills: list[AgentSkill], active_names: list[str]) -> str:
    active = [skill for skill in skills if skill.name in set(active_names)]
    if not active:
        return "Active Agent Skills: none"
    parts = ["Active Agent Skills:"]
    for skill in active:
        parts.append(f"\n--- {skill.name} ({skill.path.as_posix()}) ---\n{skill.body}")
    return "\n".join(parts)


def skill_resources(skill: AgentSkill, max_items: int = 200) -> list[dict[str, Any]]:
    root = skill.path.parent.resolve()
    resources: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path == skill.path or not path.is_file():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue
        resources.append(
            {
                "path": resolved.relative_to(root).as_posix(),
                "bytes": resolved.stat().st_size,
            }
        )
        if len(resources) >= max_items:
            break
    return resources


def resolve_skill_resource(skill: AgentSkill, resource_path: str) -> Path:
    root = skill.path.parent.resolve()
    resolved = (root / resource_path).resolve()
    if resolved == skill.path.resolve():
        raise ValueError("use activate_skill to read SKILL.md")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"skill resource escapes skill directory: {resource_path}") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"skill resource not found: {resource_path}")
    return resolved
