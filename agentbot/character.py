from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CharacterError(ValueError):
    """Raised when a character card cannot be loaded safely."""


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()[:limit]


@dataclass(frozen=True, slots=True)
class LoreEntry:
    keys: tuple[str, ...]
    content: str
    enabled: bool = True
    constant: bool = False


@dataclass(frozen=True, slots=True)
class Character:
    name: str
    description: str
    personality: str
    scenario: str
    system_prompt: str
    post_history_instructions: str
    first_message: str
    example_dialogue: str
    activity: str
    proactive_guidance: str
    lore: tuple[LoreEntry, ...]

    def render(self, text: str, user_name: str = "user") -> str:
        return (
            text.replace("{{char}}", self.name)
            .replace("{{user}}", user_name)
            .replace("<BOT_NAME>", self.name)
        )

    def relevant_lore(self, text: str, max_entries: int = 3, max_chars: int = 2400) -> str:
        lowered = text.casefold()
        selected: list[str] = []
        used = 0
        for entry in self.lore:
            if not entry.enabled:
                continue
            matches = entry.constant or any(key.casefold() in lowered for key in entry.keys if key)
            if not matches:
                continue
            remaining = max_chars - used
            if remaining <= 0:
                break
            content = entry.content[:remaining]
            selected.append(content)
            used += len(content)
            if len(selected) >= max_entries:
                break
        return "\n\n".join(selected)

    def persona_text(self, user_name: str = "user") -> str:
        parts: list[str] = []
        if self.system_prompt:
            parts.append(self.render(self.system_prompt, user_name))
        else:
            parts.append(f"You are {self.name}.")
        if self.description:
            parts.append(f"Description: {self.render(self.description, user_name)}")
        if self.personality:
            parts.append(f"Personality: {self.render(self.personality, user_name)}")
        if self.scenario:
            parts.append(f"Scenario: {self.render(self.scenario, user_name)}")
        if self.post_history_instructions:
            parts.append(
                "Post-history instructions: "
                + self.render(self.post_history_instructions, user_name)
            )
        return "\n".join(parts)


def load_character(path: str | Path) -> Character:
    character_path = Path(path)
    if not character_path.is_file():
        raise CharacterError(f"Character file not found: {character_path}")
    try:
        if character_path.stat().st_size > 1_000_000:
            raise CharacterError("Character file exceeds the 1 MB safety limit")
    except OSError as exc:
        raise CharacterError(f"Could not inspect character file: {exc}") from exc

    try:
        raw = json.loads(character_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CharacterError(f"Could not read character file: {exc}") from exc

    if not isinstance(raw, dict):
        raise CharacterError("Character file must contain a JSON object")

    if raw.get("spec") == "chara_card_v2":
        data = raw.get("data")
        if not isinstance(data, dict):
            raise CharacterError("chara_card_v2 file has no data object")
    else:
        data = raw

    name = _text(data.get("name"), 80)
    if not name:
        raise CharacterError("Character field 'name' is required")

    agent_config = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    book = data.get("character_book") if isinstance(data.get("character_book"), dict) else {}
    entries = book.get("entries") if isinstance(book.get("entries"), list) else []
    lore: list[LoreEntry] = []
    for item in entries[:100]:
        if not isinstance(item, dict):
            continue
        keys_raw = item.get("keys") if isinstance(item.get("keys"), list) else []
        keys = tuple(_text(key, 100) for key in keys_raw if _text(key, 100))
        content = _text(item.get("content"), 2000)
        if not content:
            continue
        lore.append(
            LoreEntry(
                keys=keys,
                content=content,
                enabled=bool(item.get("enabled", True)),
                constant=bool(item.get("constant", False)),
            )
        )

    return Character(
        name=name,
        description=_text(data.get("description"), 6000),
        personality=_text(data.get("personality"), 3500),
        scenario=_text(data.get("scenario"), 3500),
        system_prompt=_text(data.get("system_prompt"), 6000),
        post_history_instructions=_text(data.get("post_history_instructions"), 3000),
        first_message=_text(data.get("first_mes") or data.get("first_message"), 1800),
        example_dialogue=_text(data.get("mes_example") or data.get("example_dialogue"), 4000),
        activity=_text(agent_config.get("activity"), 128),
        proactive_guidance=_text(agent_config.get("proactive_guidance"), 1200),
        lore=tuple(lore),
    )
