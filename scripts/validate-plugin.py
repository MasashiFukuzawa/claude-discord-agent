#!/usr/bin/env python3
"""Validate the portable subset of Claude plugin and skill metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert plugin["name"] == "discord-agent"
    assert marketplace["plugins"][0]["name"] == plugin["name"]

    skill_path = ROOT / "skills" / "discord-agent" / "SKILL.md"
    text = skill_path.read_text()
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    raw = match.group(1)
    metadata = yaml.safe_load(raw)
    assert isinstance(metadata, dict), "frontmatter must be a YAML mapping"
    assert list(metadata) == ["name", "description"], (
        f"unsupported frontmatter keys: {list(metadata)}"
    )
    assert metadata["name"] == "discord-agent"
    description = metadata["description"]
    assert isinstance(description, str), "description must be a string"
    assert 120 <= len(description) <= 500, len(description)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
