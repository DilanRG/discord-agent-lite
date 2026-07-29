from __future__ import annotations

import unittest

from agentbot.llm import LLMProvider, ProviderError
from agentbot.prompt_formats import PromptTurn


class StubProvider(LLMProvider):
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
    ) -> str:
        del system_prompt, user_prompt, max_tokens, temperature
        del history, post_history, scope, task, context_tokens
        self.calls += 1
        return self.response

    def status(self) -> dict[str, object]:
        return {"provider": "stub"}


class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_validation_accepts_or_rejects_one_generation(self) -> None:
        provider = StubProvider("valid")
        result = await provider.generate_validated(
            system_prompt="system",
            user_prompt="user",
            max_tokens=20,
            temperature=0.2,
            validator=lambda value: value == "valid",
        )
        self.assertEqual(result, "valid")
        self.assertEqual(provider.calls, 1)

        provider.response = "invalid"
        with self.assertRaises(ProviderError):
            await provider.generate_validated(
                system_prompt="system",
                user_prompt="user",
                max_tokens=20,
                temperature=0.2,
                validator=lambda value: value == "valid",
            )
        self.assertEqual(provider.calls, 2)


if __name__ == "__main__":
    unittest.main()
