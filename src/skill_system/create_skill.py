"""CLI scaffold for creating new skills.

Usage:
    python -m src.skill_system.create_skill --name "weather" --description "Weather forecasting"
"""

import argparse
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).parent.parent / "skills"

SKILL_MD_TEMPLATE = """---
name: {slug}
description: "{description}"
license: MIT
compatibility:
  requires:
    - LLM API
---

# {name}

## Instructions

{description}

## Behavior

- Respond naturally in the chat's language and style
- Use context from the conversation to give relevant answers
- Be helpful but maintain the bot's personality

## Triggers

Keywords that activate this skill:
- {slug}
"""

HANDLER_TEMPLATE = '''"""Custom handler for {name} skill."""

from __future__ import annotations

import structlog

from src.message_processor.processor import NormalizedMessage
from src.skill_system.state_manager import skill_state_manager
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()


async def process_message(
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """Process a message for the {name} skill."""
    state = await skill_state_manager.get_state("{slug}", chat_id, default={{}})

    # TODO: implement skill logic
    llm = LLMProvider.get_provider()
    messages = [
        {{"role": "system", "content": "You are handling the {name} skill."}},
        {{"role": "user", "content": msg.text}},
    ]
    response = await llm.generate_response(messages=messages)

    await skill_state_manager.set_state("{slug}", chat_id, state)
    return response
'''


def create_skill(name: str, description: str, with_handler: bool = False) -> None:
    """Create a new skill scaffold."""
    slug = name.lower().replace(" ", "_").replace("-", "_")
    skill_dir = SKILLS_DIR / slug

    if skill_dir.exists():
        print(f"Error: Skill directory already exists: {skill_dir}")
        sys.exit(1)

    skill_dir.mkdir(parents=True)

    # Create __init__.py
    (skill_dir / "__init__.py").write_text("")

    # Create SKILL.md
    skill_md = SKILL_MD_TEMPLATE.format(
        slug=slug,
        name=name,
        description=description,
    )
    (skill_dir / "SKILL.md").write_text(skill_md)

    # Create handler.py if requested
    if with_handler:
        handler = HANDLER_TEMPLATE.format(name=name, slug=slug)
        (skill_dir / "handler.py").write_text(handler)

    print(f"Skill created: {skill_dir}")
    print(f"  SKILL.md: {skill_dir / 'SKILL.md'}")
    if with_handler:
        print(f"  handler.py: {skill_dir / 'handler.py'}")
    print(f"\nNext steps:")
    print(f"  1. Edit SKILL.md with detailed instructions")
    if with_handler:
        print(f"  2. Implement logic in handler.py")
    print(f"  3. Restart the bot to discover the new skill")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new skill scaffold")
    parser.add_argument("--name", required=True, help="Skill name (e.g. 'weather')")
    parser.add_argument("--description", required=True, help="Short description")
    parser.add_argument("--handler", action="store_true", help="Create custom handler.py")
    args = parser.parse_args()

    create_skill(args.name, args.description, with_handler=args.handler)


if __name__ == "__main__":
    main()
