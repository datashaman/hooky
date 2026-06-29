from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    path: Path
    body: str


def discover_skills(working_folder: Path) -> list[AgentSkill]:
    skills: dict[str, AgentSkill] = {}
    for root in [BUNDLED_SKILLS_ROOT, working_folder / ".agents" / "skills"]:
        for path in sorted(root.glob("*/SKILL.md")):
            skill = read_skill(path)
            if skill:
                skills[skill.name] = skill
    return [skills[name] for name in sorted(skills)]


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
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata, body


def skill_catalog(skills: list[AgentSkill]) -> str:
    if not skills:
        return "Available Agent Skills: none"
    lines = ["Available Agent Skills:"]
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
