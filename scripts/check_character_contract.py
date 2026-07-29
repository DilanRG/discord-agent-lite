#!/usr/bin/env python3
"""Check a character card and its prompt contract without printing card content."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentbot.character import Character, load_character
from agentbot.orchestrator import AgentCore


def _prompt_core(
    character: Character,
    *,
    normal_max_chars: int,
    proactive_max_chars: int,
) -> tuple[tuple[str, str], tuple[str, str]]:
    # Card rendering depends only on the context limit and immutable Character.
    # Avoid databases, providers, or network clients here.
    core = object.__new__(AgentCore)
    core.settings = SimpleNamespace(provider_context_tokens=8192)  # type: ignore[assignment]
    core.character = character  # type: ignore[assignment]
    lore = character.relevant_lore(" ".join(key for item in character.lore for key in item.keys))
    return (
        core._card_prompts(lore=lore, max_chars=normal_max_chars),
        core._card_prompts(
            lore=lore,
            max_chars=proactive_max_chars,
            include_opening_example=False,
        ),
    )


def evaluate_character_contract(
    path: Path,
    *,
    expected_sha256: str = "",
    normal_max_chars: int = 9500,
    proactive_max_chars: int = 7067,
) -> dict[str, Any]:
    """Return content-free integrity and prompt-survival evidence for one card."""
    character_path = Path(path).resolve()
    raw = character_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    character = load_character(character_path)
    (normal_prompt, normal_post), (proactive_prompt, proactive_post) = _prompt_core(
        character,
        normal_max_chars=normal_max_chars,
        proactive_max_chars=proactive_max_chars,
    )
    placeholder_user = "the current user"
    rendered_system = character.render(character.system_prompt, placeholder_user)
    rendered_post_history = character.render(
        character.post_history_instructions,
        placeholder_user,
    )
    rendered_first_message = character.render(character.first_message, placeholder_user).strip()
    rendered_core = tuple(
        character.render(value, placeholder_user).strip()
        for value in (
            character.description,
            character.personality,
            character.scenario,
        )
        if value.strip()
    )
    rendered_examples = character.render(
        character.example_dialogue,
        placeholder_user,
    ).strip()
    selected_lore = character.relevant_lore(
        " ".join(key for item in character.lore for key in item.keys)
    ).strip()
    prompts = (normal_prompt, proactive_prompt)
    post_prompts = (normal_post, proactive_post)
    unresolved = ("{{char}}", "{{user}}", "<BOT_NAME>")
    expected_digest = expected_sha256.strip().casefold()

    checks = {
        "sha256": not expected_digest or digest == expected_digest,
        "normal_length": len(normal_prompt) + len(normal_post) <= normal_max_chars,
        "proactive_length": len(proactive_prompt) + len(proactive_post) <= proactive_max_chars,
        "card_system_first": all(
            not rendered_system or prompt.startswith(rendered_system) for prompt in prompts
        ),
        "identity_not_reframed": all(
            "CHARACTER\n\nName:" not in prompt and "Character name:" not in prompt
            for prompt in prompts
        ),
        "core_card_fields_full": all(
            value in prompt for prompt in prompts for value in rendered_core
        ),
        "character_instructions_full": all(
            not rendered_system or rendered_system in prompt for prompt in prompts
        ),
        "post_history_guidance_full": all(
            not rendered_post_history or post.endswith(rendered_post_history)
            for post in post_prompts
        ),
        "normal_examples_full": not rendered_examples or rendered_examples in normal_prompt,
        "normal_selected_lore_full": not selected_lore or selected_lore in normal_prompt,
        "placeholders_resolved": all(
            marker not in prompt
            for prompt in (*prompts, *post_prompts)
            for marker in unresolved
        ),
        "first_message_normal_style_example": (
            not rendered_first_message
            or rendered_first_message in normal_prompt
        ),
        "proactive_opening_example_absent": (
            "OPENING MESSAGE EXAMPLE" not in proactive_prompt
        ),
        "discord_delivery_cue": all(
            "your next Discord message" in post
            and "character's next" not in post
            for post in post_prompts
        ),
        "no_competing_framework": all("FRAMEWORK" not in prompt for prompt in prompts),
    }
    checks["normal_prompt"] = all(
        checks[name]
        for name in (
            "normal_length",
            "card_system_first",
            "identity_not_reframed",
            "core_card_fields_full",
            "character_instructions_full",
            "post_history_guidance_full",
            "normal_examples_full",
            "normal_selected_lore_full",
            "placeholders_resolved",
            "first_message_normal_style_example",
            "discord_delivery_cue",
            "no_competing_framework",
        )
    )
    checks["proactive_prompt"] = all(
        checks[name]
        for name in (
            "proactive_length",
            "card_system_first",
            "identity_not_reframed",
            "core_card_fields_full",
            "character_instructions_full",
            "post_history_guidance_full",
            "placeholders_resolved",
            "proactive_opening_example_absent",
            "discord_delivery_cue",
            "no_competing_framework",
        )
    )
    return {
        "passed": all(checks.values()),
        "path": str(character_path),
        "character": character.name,
        "sha256": digest,
        "bytes": len(raw),
        "normal_prompt_chars": len(normal_prompt),
        "normal_post_history_chars": len(normal_post),
        "proactive_prompt_chars": len(proactive_prompt),
        "proactive_post_history_chars": len(proactive_post),
        "checks": checks,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a card hash and prompt contract without displaying card content."
    )
    parser.add_argument("--character", type=Path, required=True)
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--normal-max-chars", type=int, default=9500)
    parser.add_argument("--proactive-max-chars", type=int, default=7067)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.normal_max_chars < 2200 or args.proactive_max_chars < 2200:
        raise SystemExit("prompt limits must be at least 2200 characters")
    result = evaluate_character_contract(
        args.character,
        expected_sha256=args.expected_sha256,
        normal_max_chars=args.normal_max_chars,
        proactive_max_chars=args.proactive_max_chars,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        status = "PASS" if result["passed"] else "FAIL"
        print(
            f"{status} character={result['character']} sha256={result['sha256']} "
            f"normal_chars={result['normal_prompt_chars']} "
            f"normal_post_history_chars={result['normal_post_history_chars']} "
            f"proactive_chars={result['proactive_prompt_chars']}"
        )
        for name, passed in result["checks"].items():
            print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
