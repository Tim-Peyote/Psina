"""SKILL.md parser — reads and parses skill metadata.

Implements lazy loading:
- Discovery: reads only name + description (~50-100 tokens)
- Activation: reads full SKILL.md (~5000 tokens)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

logger = __import__("structlog").get_logger()


@dataclass
class SkillMetadata:
    """Parsed SKILL.md metadata."""

    slug: str
    name: str
    description: str
    license: str = "MIT"
    compatibility: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    full_content: str = ""  # Full markdown content (loaded on activation)
    is_loaded: bool = False  # Whether full content has been loaded


# YAML frontmatter parser (minimal, no external deps)
def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content."""
    match = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
    if not match:
        return {}

    yaml_str = match.group(1)
    result = {}

    # Parse simple key: value pairs
    current_key = None
    current_list = None
    current_dict = None

    for line in yaml_str.split("\n"):
        # Skip empty lines
        if not line.strip():
            continue

        # Top-level key: value
        if not line.startswith(" ") and not line.startswith("-"):
            match_kv = re.match(r"^(\w[\w_-]*):\s*(.*)", line)
            if match_kv:
                key = match_kv.group(1)
                value = match_kv.group(2).strip()

                if value:
                    # Remove quotes if present
                    value = value.strip("\"'")
                    result[key] = value
                    current_key = key
                    current_list = None
                    current_dict = None
                else:
                    # Nested structure (dict or list follows)
                    result[key] = {}
                    current_key = key
                    current_dict = result[key]
                    current_list = None
            continue

        # List item (- value)
        stripped = line.strip()
        if stripped.startswith("- "):
            val = stripped[2:].strip().strip("\"'")
            if current_key:
                if not isinstance(result.get(current_key), list):
                    result[current_key] = []
                result[current_key].append(val)
            continue

        # Nested key-value (under a dict key)
        if ":" in stripped and current_dict is not None:
            nested_match = re.match(r"^(\w[\w_-]*):\s*(.*)", stripped)
            if nested_match:
                nk = nested_match.group(1)
                nv = nested_match.group(2).strip().strip("\"'")
                # Check if it's a list key (requires, optional, etc.)
                if not nv:
                    result[current_key][nk] = []
                    current_dict[current_key + "." + nk] = result[current_key][nk]
                else:
                    result[current_key][nk] = nv
                continue

    return result


def discover_skill(skill_path: Path) -> SkillMetadata | None:
    """Read only name + description from SKILL.md (Discovery phase).

    Returns None if SKILL.md doesn't exist or is malformed.
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(content)

    name = frontmatter.get("name", skill_path.name)
    description = frontmatter.get("description", "")

    if not description:
        logger.warning("SKILL.md has no description", path=str(skill_path))
        return None

    return SkillMetadata(
        slug=name,
        name=frontmatter.get("name", name),
        description=description[:1024],  # Cap at 1024 chars per spec
        license=frontmatter.get("license", "MIT"),
        compatibility=frontmatter.get("compatibility", {}),
        metadata=frontmatter.get("metadata", {}),
    )


def activate_skill(skill_path: Path) -> SkillMetadata | None:
    """Load full SKILL.md content (Activation phase).

    Returns metadata with full_content populated.
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(content)

    # Get the markdown body (after frontmatter)
    match = re.match(r"^---\n.*?\n---\n(.*)", content, re.DOTALL)
    body = match.group(1).strip() if match else ""

    name = frontmatter.get("name", skill_path.name)

    return SkillMetadata(
        slug=name,
        name=name,
        description=frontmatter.get("description", "")[:1024],
        license=frontmatter.get("license", "MIT"),
        compatibility=frontmatter.get("compatibility", {}),
        metadata=frontmatter.get("metadata", {}),
        full_content=body,
        is_loaded=True,
    )
