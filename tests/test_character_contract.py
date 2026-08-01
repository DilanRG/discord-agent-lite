from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_character_contract import evaluate_character_contract


class CharacterContractTests(unittest.TestCase):
    def test_contract_checks_hash_prompt_boundaries_and_constrained_instruction_survival(self) -> None:
        card = {
            "spec": "chara_card_v2",
            "data": {
                "name": "Rook",
                "description": "description-head " + ("d" * 300) + " description-tail",
                "personality": "personality-head " + ("p" * 300) + " personality-tail",
                "scenario": "scenario-head " + ("s" * 200) + " scenario-tail",
                "system_prompt": (
                    "instructions-head "
                    + ("i" * 500)
                    + " authority-middle-sentinel "
                    + ("i" * 500)
                    + " instructions-tail"
                ),
                "post_history_instructions": (
                    "post-head " + ("h" * 500) + " post-tail"
                ),
                "first_mes": "first-message-must-not-enter-the-system-prompt",
                "mes_example": "START\n{{user}}: hi\n{{char}}: hey",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rook.json"
            raw = json.dumps(card).encode("utf-8")
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()

            passed = evaluate_character_contract(
                path,
                expected_sha256=digest,
            )
            mismatched = evaluate_character_contract(
                path,
                expected_sha256="0" * 64,
            )

        self.assertTrue(passed["passed"])
        self.assertTrue(passed["checks"]["normal_prompt"])
        self.assertTrue(passed["checks"]["proactive_prompt"])
        self.assertTrue(passed["checks"]["core_card_fields_full"])
        self.assertTrue(passed["checks"]["character_instructions_full"])
        self.assertTrue(passed["checks"]["first_message_normal_style_example"])
        self.assertTrue(passed["checks"]["proactive_opening_example_absent"])
        self.assertTrue(passed["checks"]["card_system_first"])
        self.assertTrue(passed["checks"]["identity_not_reframed"])
        self.assertTrue(passed["checks"]["post_history_guidance_full"])
        self.assertTrue(passed["checks"]["discord_identity_cue"])
        self.assertTrue(passed["checks"]["discord_delivery_cue"])
        self.assertTrue(passed["checks"]["discord_delivery_cue_final"])
        self.assertTrue(passed["checks"]["no_competing_framework"])
        self.assertFalse(mismatched["passed"])
        self.assertFalse(mismatched["checks"]["sha256"])


if __name__ == "__main__":
    unittest.main()
