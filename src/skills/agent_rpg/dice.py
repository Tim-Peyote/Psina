"""RPG dice roller — supports D20, PbtA, advantage/disadvantage.

Adapted from openclaw/skills/agent-rpg/scripts/dice.py
"""

from __future__ import annotations

import random
import re


class DiceResult:
    """Result of a dice roll."""

    def __init__(
        self,
        expression: str,
        rolls: list[int],
        modifier: int,
        total: int,
        result_text: str = "",
        is_critical: bool = False,
        is_fumble: bool = False,
    ) -> None:
        self.expression = expression
        self.rolls = rolls
        self.modifier = modifier
        self.total = total
        self.result_text = result_text
        self.is_critical = is_critical
        self.is_fumble = is_fumble

    def __str__(self) -> str:
        parts = [f"🎲 `{self.expression}`"]
        parts.append(f"→ [{', '.join(str(r) for r in self.rolls)}]")
        if self.modifier != 0:
            parts.append(f"{'+' if self.modifier > 0 else ''}{self.modifier}")
        parts.append(f"= **{self.total}**")
        if self.is_critical:
            parts.append("\n🌟 КРИТИЧЕСКИЙ УСПЕХ!")
        elif self.is_fumble:
            parts.append("\n💥 КРИТИЧЕСКИЙ ПРОВАЛ!")
        if self.result_text:
            parts.append(f"\n{self.result_text}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "expression": self.expression,
            "rolls": self.rolls,
            "modifier": self.modifier,
            "total": self.total,
            "result_text": self.result_text,
            "is_critical": self.is_critical,
            "is_fumble": self.is_fumble,
        }


def roll(
    expression: str,
    advantage: bool = False,
    disadvantage: bool = False,
) -> DiceResult | None:
    """Parse and execute a dice expression.

    Supported formats:
    - XdY+Z (e.g., 1d20+5, 2d6, 3d8-1)
    - pbta+Z (Powered by the Apocalypse: 2d6+Z)
    """
    # PbtA match
    pbta_match = re.match(r"pbta([+-]\d+)?", expression.lower())
    if pbta_match:
        count = 2
        sides = 6
        modifier = int(pbta_match.group(1)) if pbta_match.group(1) else 0
        is_pbta = True
    else:
        # Standard XdY+Z
        match = re.match(r"(\d+)d(\d+)([+-]\d+)?", expression.lower())
        if not match:
            return None
        count = int(match.group(1))
        sides = int(match.group(2))
        modifier = int(match.group(3)) if match.group(3) else 0
        is_pbta = False

    def do_rolls() -> list[int]:
        return [random.randint(1, sides) for _ in range(count)]

    # Advantage / Disadvantage (roll twice, take best/worst)
    if advantage and disadvantage:
        advantage = False
        disadvantage = False

    rolls1 = do_rolls()
    total1 = sum(rolls1) + modifier

    if advantage or disadvantage:
        rolls2 = do_rolls()
        total2 = sum(rolls2) + modifier
        if advantage:
            final_total = max(total1, total2)
            rolls = rolls1 if total1 >= total2 else rolls2
            adv_text = " (преимущество)"
        else:
            final_total = min(total1, total2)
            rolls = rolls1 if total1 <= total2 else rolls2
            adv_text = " (помеха)"
        total = final_total
    else:
        rolls = rolls1
        total = total1
        adv_text = ""

    # Determine result text
    result_text = ""
    is_critical = False
    is_fumble = False

    if is_pbta:
        if total >= 10:
            result_text = "🌟 FULL SUCCESS (10+)"
        elif total >= 7:
            result_text = "⚡ PARTIAL SUCCESS (7-9) — успех ценой"
        else:
            result_text = "💥 MISS (6-) — ГМ делает жёсткий ход"
    elif sides == 20 and count == 1:
        if rolls[0] == 20:
            result_text = "🌟 КРИТИЧЕСКИЙ УСПЕХ! (Nat 20)"
            is_critical = True
        elif rolls[0] == 1:
            result_text = "💥 КРИТИЧЕСКИЙ ПРОВАЛ! (Nat 1)"
            is_fumble = True

    return DiceResult(
        expression=f"{expression}{adv_text}",
        rolls=rolls,
        modifier=modifier,
        total=total,
        result_text=result_text,
        is_critical=is_critical,
        is_fumble=is_fumble,
    )
