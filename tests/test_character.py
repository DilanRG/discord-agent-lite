from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentbot.character import CharacterError, load_character


class CharacterTests(unittest.TestCase):
    def test_loads_v2_card_and_matches_lore(self) -> None:
        card = {
            "spec": "chara_card_v2",
            "data": {
                "name": "Nova",
                "description": "Talks with {{user}} as {{char}}.",
                "agent": {"activity": "questions", "proactive_guidance": "Stay grounded."},
                "character_book": {
                    "entries": [
                        {"keys": ["sqlite"], "content": "Nova likes compact databases."},
                        {"keys": [], "content": "Always relevant.", "constant": True},
                    ]
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "card.json"
            path.write_text(json.dumps(card), encoding="utf-8")
            character = load_character(path)

        self.assertEqual(character.name, "Nova")
        persona = character.persona_text("Casey")
        self.assertIn("Talks with Casey as Nova", persona)
        self.assertNotIn("Character name:", persona)
        self.assertEqual(character.activity, "questions")
        lore = character.relevant_lore("Can we use SQLite?")
        self.assertIn("compact databases", lore)
        self.assertIn("Always relevant", lore)

    def test_plain_card_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "plain.json"
            path.write_text('{"name":"Plain","personality":"Direct"}', encoding="utf-8")
            character = load_character(path)
            self.assertEqual(character.name, "Plain")
            self.assertEqual(character.personality, "Direct")

            invalid_cards = {
                "missing name": ("missing.json", "{}"),
                "invalid JSON": ("broken.json", "{"),
                "non-object root": ("array.json", "[]"),
            }
            for case, (filename, content) in invalid_cards.items():
                invalid_path = root / filename
                invalid_path.write_text(content, encoding="utf-8")
                with self.subTest(case=case), self.assertRaises(CharacterError):
                    load_character(invalid_path)

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * 1_000_001)
            with self.subTest(case="oversized card"), self.assertRaises(CharacterError):
                load_character(oversized)

if __name__ == "__main__":
    unittest.main()
