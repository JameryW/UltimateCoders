"""Validate repository-local wiring for the Codex issue workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / ".agents" / "skills"
REQUIRED_SKILLS = {
    "setup-matt-pocock-skills": "setup-matt-pocock-skills",
    "grill-with-docs": "grill-with-docs",
    "triage": "triage",
    "domain-modeling": "domain-modeling",
    "codebase-design": "codebase-design",
    "prototype": "prototype",
    "to-spec": "to-spec",
    "to-tickets": "to-tickets",
    "wayfinder": "wayfinder",
    "implement": "implement",
    "tdd": "tdd",
    "code-review": "code-review",
    "grilling": "grilling",
    "ultimatecoders-issue-flow": "ultimatecoders-issue-flow",
}
REQUIRED_FILES = (
    ROOT / "docs" / "agents" / "issue-tracker.md",
    ROOT / "docs" / "agents" / "triage-labels.md",
    ROOT / "docs" / "agents" / "domain.md",
    ROOT / "docs" / "agents" / "mattpocock-skills.md",
    ROOT / "docs" / "workflows" / "codex-issue-flow.md",
    ROOT / "AGENTS.md",
)


def skill_name(skill_file: Path) -> Optional[str]:
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\s*$([\s\S]*?)^---\s*$", text, re.MULTILINE)
    if match is None:
        return None
    name = re.search(r"^name:\s*['\"]?([^'\"\n]+)", match.group(1), re.MULTILINE)
    return name.group(1).strip() if name else None


def main() -> int:
    failures: list[str] = []

    for directory, expected_name in REQUIRED_SKILLS.items():
        skill_file = SKILLS_DIR / directory / "SKILL.md"
        if not skill_file.is_file():
            failures.append(f"missing skill: {skill_file.relative_to(ROOT)}")
            continue
        actual_name = skill_name(skill_file)
        if actual_name != expected_name:
            failures.append(
                f"skill name mismatch: {skill_file.relative_to(ROOT)} "
                f"declares {actual_name!r}, expected {expected_name!r}"
            )

    for path in REQUIRED_FILES:
        if not path.is_file():
            failures.append(f"missing workflow file: {path.relative_to(ROOT)}")

    if (ROOT / "AGENTS.md").is_file():
        agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        if "$ultimatecoders-issue-flow" not in agents_text:
            failures.append("AGENTS.md does not point to $ultimatecoders-issue-flow")

    entry_skill = SKILLS_DIR / "ultimatecoders-issue-flow" / "SKILL.md"
    if entry_skill.is_file():
        entry_text = entry_skill.read_text(encoding="utf-8")
        if "[TODO" in entry_text:
            failures.append("entry skill contains an unfinished TODO")
        for reference in (
            "docs/agents/issue-tracker.md",
            "docs/agents/domain.md",
            ".trellis/workflow.md",
            "$to-tickets",
            "$tdd",
            "$code-review",
        ):
            if reference not in entry_text:
                failures.append(f"entry skill is missing reference: {reference}")

    entry_metadata = SKILLS_DIR / "ultimatecoders-issue-flow" / "agents" / "openai.yaml"
    if not entry_metadata.is_file():
        failures.append("entry skill is missing agents/openai.yaml")
    elif "default_prompt:" not in entry_metadata.read_text(encoding="utf-8"):
        failures.append("entry skill metadata is missing a default prompt")

    if failures:
        print("Codex issue workflow validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Codex issue workflow validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
