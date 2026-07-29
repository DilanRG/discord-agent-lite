from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agentbot.settings import Settings


@contextmanager
def loaded_settings(tmp_path: Path, **overrides: str):
    character_path = tmp_path / "test-character.json"
    character_path.write_text(
        json.dumps(
            {
                "name": "Example Agent",
                "description": "A synthetic character chatting with {{user}} for tests.",
                "personality": "Concise and curious.",
                "system_prompt": "Reply as {{char}}, the configured test character.",
                "post_history_instructions": "Write one Discord message.",
                "first_mes": "Hey {{user}}, ready?",
                "mes_example": "{{user}}: hello\n{{char}}: hey there",
            }
        ),
        encoding="utf-8",
    )
    env = {
        "DISCORD_TOKEN": "test-token",
        "CHARACTER_FILE": str(character_path),
        "DATABASE_PATH": str(tmp_path / "agent.db"),
        "LOG_PATH": str(tmp_path / "agent.log"),
        "LLM_PROVIDER": "horde",
        "SUMMARY_AFTER_MESSAGES": "12",
        "SUMMARY_KEEP_MESSAGES": "4",
    }
    env.update(overrides)
    with patch.dict(os.environ, env, clear=True):
        yield Settings.load(env_file=tmp_path / "does-not-exist.env")
