from __future__ import annotations

import unittest

from scripts.simulate_discord import _named_expectation_met, _style_expectation_met


class SimulateDiscordScriptTests(unittest.TestCase):
    def test_named_expectation_is_optional_and_case_insensitive(self) -> None:
        self.assertTrue(_named_expectation_met("", "Example Agent"))
        self.assertTrue(_named_expectation_met("example agent", "Example Agent"))
        self.assertFalse(_named_expectation_met("Rook", "Example Agent"))

    def test_style_diagnostics_are_observations_unless_explicitly_asserted(self) -> None:
        issues = ("stage_direction",)
        self.assertTrue(_style_expectation_met({}, issues))
        self.assertFalse(_style_expectation_met({"expect_style_pass": True}, issues))
        self.assertTrue(_style_expectation_met({"expect_style_pass": False}, issues))
        self.assertTrue(
            _style_expectation_met(
                {"expected_style_issues": ["stage_direction"]},
                issues,
            )
        )
        self.assertFalse(_style_expectation_met({"expected_style_issues": []}, issues))


if __name__ == "__main__":
    unittest.main()
