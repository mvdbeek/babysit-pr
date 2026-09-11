"""Validate this repository's skill metadata without an external skill installation."""

import re
from pathlib import Path

import yaml


def validate_skill(root: Path) -> list[str]:
    errors: list[str] = []
    path = root / "SKILL.md"
    try:
        text = path.read_text()
    except OSError as exc:
        return [str(exc)]
    frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not frontmatter:
        return ["SKILL.md must begin with YAML frontmatter"]
    try:
        metadata = yaml.safe_load(frontmatter.group(1))
    except yaml.YAMLError as exc:
        return [f"Invalid skill YAML: {exc}"]
    if not isinstance(metadata, dict):
        return ["Skill frontmatter must be a mapping"]
    allowed = {"name", "description", "license", "allowed-tools", "metadata"}
    if set(metadata) - allowed:
        errors.append("Unexpected skill metadata keys")
    name = metadata.get("name")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or len(name) > 64
    ):
        errors.append("Skill name must be lowercase hyphenated text, at most 64 characters")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        errors.append("Skill description must contain 1–1024 characters")
    elif "<" in description or ">" in description or description.startswith("[TODO:"):
        errors.append("Skill description contains a placeholder or angle brackets")
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
        if "://" not in target and not target.startswith(("#", "/")):
            if not (root / target.split("#", 1)[0]).exists():
                errors.append(f"Broken local skill link: {label} ({target})")
    return errors


def main() -> int:
    errors = validate_skill(Path(__file__).resolve().parent.parent)
    for error in errors:
        print(error)
    if not errors:
        print("Skill metadata and local reference links are valid")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
