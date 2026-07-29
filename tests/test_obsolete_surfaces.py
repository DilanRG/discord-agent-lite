from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ObsoleteSurfaceTests(unittest.TestCase):
    def test_retired_settings_and_blanket_filters_do_not_remain(self) -> None:
        checks = (
            (
                "SOCIAL_NAMESPACE",
                ("agentbot/app.py", ".env.example", "README.md", "MIGRATION.md"),
            ),
            ("_SENSITIVE_PROFILE_PATTERNS", ("agentbot/policy.py",)),
            ("profile_text_is_sensitive", ("agentbot/policy.py",)),
        )
        for symbol, relative_paths in checks:
            for relative_path in relative_paths:
                with self.subTest(symbol=symbol, path=relative_path):
                    source = (ROOT / relative_path).read_text(encoding="utf-8")
                    self.assertNotIn(symbol, source, relative_path)


if __name__ == "__main__":
    unittest.main()
