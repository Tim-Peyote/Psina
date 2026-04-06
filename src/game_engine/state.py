"""Game state management helpers."""

from dataclasses import dataclass, field


@dataclass
class Character:
    name: str
    player_id: int
    character_class: str = "fighter"
    level: int = 1
    hp: int = 10
    inventory: list[str] = field(default_factory=list)


@dataclass
class WorldState:
    location: str = "starting_village"
    npcs: dict[str, str] = field(default_factory=dict)
    active_quests: list[str] = field(default_factory=list)
    completed_quests: list[str] = field(default_factory=list)


@dataclass
class GameState:
    phase: str = "intro"
    characters: dict[int, Character] = field(default_factory=dict)
    world: WorldState = field(default_factory=WorldState)
    turn: int = 0
    initiative_order: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "characters": {
                str(k): {
                    "name": v.name,
                    "character_class": v.character_class,
                    "level": v.level,
                    "hp": v.hp,
                    "inventory": v.inventory,
                }
                for k, v in self.characters.items()
            },
            "world": {
                "location": self.world.location,
                "npcs": self.world.npcs,
                "active_quests": self.world.active_quests,
                "completed_quests": self.world.completed_quests,
            },
            "turn": self.turn,
            "initiative_order": self.initiative_order,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GameState":
        state = cls()
        state.phase = data.get("phase", "intro")
        state.turn = data.get("turn", 0)
        state.initiative_order = data.get("initiative_order", [])

        chars = data.get("characters", {})
        for k, v in chars.items():
            state.characters[int(k)] = Character(
                name=v["name"],
                player_id=int(k),
                character_class=v.get("character_class", "fighter"),
                level=v.get("level", 1),
                hp=v.get("hp", 10),
                inventory=v.get("inventory", []),
            )

        world_data = data.get("world", {})
        state.world.location = world_data.get("location", "starting_village")
        state.world.npcs = world_data.get("npcs", {})
        state.world.active_quests = world_data.get("active_quests", [])
        state.world.completed_quests = world_data.get("completed_quests", [])

        return state
