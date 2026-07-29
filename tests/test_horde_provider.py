from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable

from agentbot.horde_client import (
    HordeClientError,
    HordeGeneration,
    HordeNoWorkerError,
    HordeOutputError,
    HordeTimeoutError,
    HordeTransportError,
)
from agentbot.horde_router import HordeRouter
from agentbot.llm import HordeProvider, ProviderError


REFERENCE = (
    "name,parameters_bn,display_name,url,baseline,description,style,tags,instruct_format,settings\n"
    'org/Alpha-8B,8,,,,,chat,"chat, popular",ChatML,"{}"\n'
    'org/Beta-8B,8,,,,,chat,chat,ChatML,"{}"\n'
)


class FakeHordeClient:
    def __init__(self) -> None:
        self.refreshes = 0
        self.reference_fetches = 0
        self.generated_models: list[str] = []
        self.alpha_mode = "no_worker"
        self.beta_text = "routed response"
        self.include_alpha_after_refresh = False
        self.fail_refresh_after_first = False
        self.reference_failure_after_first = ""
        self.alpha_eta = 0
        self.beta_eta = 0

    async def fetch_live_models(self):
        self.refreshes += 1
        if self.fail_refresh_after_first and self.refreshes > 1:
            raise HordeTransportError("metadata refresh failed")
        if self.refreshes == 1 or self.include_alpha_after_refresh:
            return [
                {
                    "name": "koboldcpp/Alpha-8B",
                    "count": 2,
                    "performance": 20,
                    "eta": self.alpha_eta,
                },
                {
                    "name": "koboldcpp/Beta-8B",
                    "count": 1,
                    "performance": 10,
                    "eta": self.beta_eta,
                },
            ]
        return [
            {
                "name": "koboldcpp/Beta-8B",
                "count": 1,
                "performance": 10,
                "eta": self.beta_eta,
            }
        ]

    async def fetch_workers(self):
        if self.refreshes == 1 or self.include_alpha_after_refresh:
            models = ("koboldcpp/Alpha-8B", "koboldcpp/Beta-8B")
        else:
            models = ("koboldcpp/Beta-8B",)
        return [
            {
                "id": f"worker-{self.refreshes}",
                "online": True,
                "trusted": True,
                "maintenance_mode": False,
                "paused": False,
                "flagged": False,
                "threads": 2,
                "max_context_length": 8192,
                "max_length": 1024,
                "bridge_agent": "AI Horde Worker:24:fixture",
                "models": list(models),
            }
        ]

    async def fetch_reference_csv(self):
        self.reference_fetches += 1
        if self.reference_fetches > 1:
            if self.reference_failure_after_first == "transport":
                raise HordeTransportError("reference refresh failed")
            if self.reference_failure_after_first == "schema":
                raise HordeClientError("reference schema mismatch")
        return REFERENCE

    async def generate(self, **kwargs):
        model = str(kwargs["model"])
        self.generated_models.append(model)
        if model == "koboldcpp/Alpha-8B":
            if self.alpha_mode == "no_worker":
                raise HordeNoWorkerError("No compatible worker", error_code="NoValidWorkers")
            if self.alpha_mode == "timeout":
                raise HordeTimeoutError("generation timed out")
            if self.alpha_mode == "empty":
                raise HordeOutputError(
                    "AI Horde returned an empty generation",
                    empty=True,
                    malformed=False,
                )
            if self.alpha_mode == "transport":
                raise HordeTransportError("temporary submission failure")
            if self.alpha_mode == "auth":
                raise HordeClientError("InvalidAPIKey: invalid API key")
            if self.alpha_mode == "slow":
                await asyncio.sleep(0.05)
            if self.alpha_mode == "slow_cancel":
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.08)
                    raise
            if self.alpha_mode == "malformed":
                return HordeGeneration(
                    text="malformed structured output",
                    model=model,
                    worker_id="worker-malformed",
                    worker_name="malformed worker",
                    latency_seconds=0.75,
                    truncated=False,
                    malformed=False,
                )
        return HordeGeneration(
            text=self.beta_text,
            model=model,
            worker_id="worker-final",
            worker_name="final worker",
            latency_seconds=1.25,
            truncated=False,
            malformed=False,
        )


class HordeProviderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _router() -> HordeRouter:
        return HordeRouter(
            metadata_ttl_seconds=90,
            sticky_seconds=1800,
            min_parameters_bn=7,
            max_selections=16,
            max_metrics=16,
            trusted_workers=True,
            clock=lambda: 0.0,
        )

    @staticmethod
    async def _generate_chat(
        provider: HordeProvider,
        *,
        user_prompt: str = "hello",
        max_tokens: int = 72,
    ) -> str:
        return await provider.generate(
            system_prompt="rules",
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=0.7,
            scope="g:1:c:10",
            task="chat",
            context_tokens=8192,
        )

    @staticmethod
    async def _generate_reflection(
        provider: HordeProvider,
        *,
        user_prompt: str,
        validator: Callable[[str], bool],
    ) -> str:
        return await provider.generate_validated(
            system_prompt="rules",
            user_prompt=user_prompt,
            max_tokens=300,
            temperature=0.2,
            scope="g:1:u:7",
            task="reflection",
            context_tokens=4096,
            validator=validator,
        )

    async def test_chat_routes_around_queue_longer_than_attempt_budget(self) -> None:
        router = self._router()
        client = FakeHordeClient()
        client.alpha_eta = 75
        client.beta_eta = 5
        provider = HordeProvider(
            client=client,
            router=router,
            trusted_workers=True,
            interactive_timeout_seconds=45,
        )

        result = await self._generate_chat(provider)

        self.assertEqual(result, "routed response")
        self.assertEqual(client.generated_models, ["koboldcpp/Beta-8B"])

    async def test_no_worker_forces_refresh_and_one_reselection(self) -> None:
        router = self._router()
        client = FakeHordeClient()
        outcomes: list[dict[str, object]] = []
        provider = HordeProvider(
            client=client,
            router=router,
            trusted_workers=True,
            outcome_recorder=lambda **item: outcomes.append(item),
        )
        result = await self._generate_chat(provider, max_tokens=500)
        self.assertEqual(result, "routed response")
        self.assertEqual(
            client.generated_models,
            ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
        )
        self.assertEqual(client.refreshes, 2)
        self.assertEqual(client.reference_fetches, 1)
        self.assertEqual([item["success"] for item in outcomes], [False, True])
        self.assertEqual(
            provider.status()["selected_models"][0]["model"],
            "koboldcpp/Beta-8B",
        )

    async def test_retryable_attempt_failures_route_to_one_different_model(self) -> None:
        cases = (
            {
                "name": "invalid_structured_output",
                "alpha_mode": "malformed",
                "validated": True,
                "expected_successes": [False, True],
                "expected_error_kind": "malformed",
                "expected_flag": "malformed",
            },
            {
                "name": "provider_timeout",
                "alpha_mode": "timeout",
                "validated": False,
                "expected_successes": [False, True],
                "expected_error_kind": "timeout",
                "expected_flag": None,
            },
            {
                "name": "empty_generation",
                "alpha_mode": "empty",
                "validated": False,
                "expected_successes": None,
                "expected_error_kind": "output",
                "expected_flag": "empty",
            },
            {
                "name": "transient_transport_failure",
                "alpha_mode": "transport",
                "validated": False,
                "expected_successes": None,
                "expected_error_kind": None,
                "expected_flag": None,
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                client = FakeHordeClient()
                client.alpha_mode = case["alpha_mode"]
                outcomes: list[dict[str, object]] = []
                provider = HordeProvider(
                    client=client,
                    router=self._router(),
                    trusted_workers=True,
                    outcome_recorder=lambda **item: outcomes.append(item),
                )

                if case["validated"]:
                    result = await self._generate_reflection(
                        provider,
                        user_prompt="reflect",
                        validator=lambda text: text == "routed response",
                    )
                else:
                    result = await self._generate_chat(provider)

                self.assertEqual(result, "routed response")
                self.assertEqual(
                    client.generated_models,
                    ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
                )
                if case["expected_successes"] is not None:
                    self.assertEqual(
                        [item["success"] for item in outcomes],
                        case["expected_successes"],
                    )
                if case["expected_error_kind"] is not None:
                    self.assertEqual(
                        outcomes[0]["error_kind"],
                        case["expected_error_kind"],
                    )
                if case["expected_flag"] is not None:
                    self.assertTrue(outcomes[0][case["expected_flag"]])

    async def test_nonretryable_provider_error_does_not_spend_a_second_attempt(self) -> None:
        client = FakeHordeClient()
        client.alpha_mode = "auth"
        outcomes: list[dict[str, object]] = []
        provider = HordeProvider(
            client=client,
            router=self._router(),
            trusted_workers=True,
            outcome_recorder=lambda **item: outcomes.append(item),
        )

        with self.assertRaisesRegex(ProviderError, "InvalidAPIKey"):
            await self._generate_chat(provider)

        self.assertEqual(client.generated_models, ["koboldcpp/Alpha-8B"])
        self.assertEqual(outcomes, [])

    async def test_failed_forced_refresh_uses_bounded_stale_metadata(self) -> None:
        client = FakeHordeClient()
        client.fail_refresh_after_first = True
        provider = HordeProvider(
            client=client,
            router=self._router(),
            trusted_workers=True,
        )

        result = await self._generate_chat(provider)

        self.assertEqual(result, "routed response")
        self.assertEqual(client.refreshes, 2)
        self.assertEqual(
            client.generated_models,
            ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
        )

    async def test_unavailable_fresh_metadata_or_reference_is_not_hidden(self) -> None:
        cases = (
            {
                "name": "expired_stale_metadata",
                "failure": "metadata_transport",
                "error": "metadata refresh failed",
            },
            {
                "name": "nontransport_metadata_error",
                "failure": "metadata_schema",
                "error": "metadata schema mismatch",
            },
            {
                "name": "expired_model_reference",
                "failure": "reference_transport",
                "error": "reference refresh failed",
            },
            {
                "name": "nontransport_model_reference_error",
                "failure": "reference_schema",
                "error": "reference schema mismatch",
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                client = FakeHordeClient()
                client.alpha_mode = "ok"
                provider = HordeProvider(
                    client=client,
                    router=self._router(),
                    trusted_workers=True,
                )
                await self._generate_chat(provider)
                client.alpha_mode = "no_worker"

                if case["failure"] == "metadata_transport":
                    client.fail_refresh_after_first = True
                    provider._metadata_refreshed_at -= (
                        provider._max_stale_metadata_seconds + 1
                    )
                elif case["failure"] == "metadata_schema":
                    async def invalid_live_models():
                        raise HordeClientError("metadata schema mismatch")

                    client.fetch_live_models = invalid_live_models
                elif case["failure"] == "reference_transport":
                    client.reference_failure_after_first = "transport"
                    provider._reference_refreshed_at -= (
                        provider._max_stale_reference_seconds + 1
                    )
                else:
                    client.reference_failure_after_first = "schema"
                    provider._reference_refreshed_at -= (
                        provider._reference_ttl_seconds + 1
                    )

                with self.assertRaisesRegex(ProviderError, case["error"]):
                    await self._generate_chat(provider, user_prompt="still?")

    async def test_interactive_attempt_timeout_is_bounded_and_reselected(self) -> None:
        client = FakeHordeClient()
        client.alpha_mode = "slow"
        outcomes: list[dict[str, object]] = []
        provider = HordeProvider(
            client=client,
            router=self._router(),
            trusted_workers=True,
            outcome_recorder=lambda **item: outcomes.append(item),
            interactive_timeout_seconds=0.01,
        )

        result = await self._generate_chat(provider)

        self.assertEqual(result, "routed response")
        self.assertEqual(
            client.generated_models,
            ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
        )
        self.assertEqual(outcomes[0]["error_kind"], "timeout")

    async def test_total_chat_deadline_bounds_metadata_and_all_attempts(self) -> None:
        client = FakeHordeClient()
        client.alpha_mode = "slow_cancel"
        provider = HordeProvider(
            client=client,
            router=self._router(),
            trusted_workers=True,
            interactive_timeout_seconds=1.0,
            total_chat_timeout_seconds=0.01,
        )

        started = asyncio.get_running_loop().time()
        with self.assertRaisesRegex(ProviderError, "chat deadline"):
            await self._generate_chat(provider)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.06)
        self.assertEqual(client.generated_models, ["koboldcpp/Alpha-8B"])
        self.assertEqual(provider.status()["selected_models"], [])

        client.alpha_mode = "ok"
        result = await self._generate_chat(provider, user_prompt="still?")
        self.assertEqual(result, "routed response")
        self.assertEqual(
            client.generated_models,
            ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
        )

    async def test_structured_retry_is_bounded_to_two_models(self) -> None:
        client = FakeHordeClient()
        client.alpha_mode = "malformed"
        client.beta_text = "also malformed"
        outcomes: list[dict[str, object]] = []
        provider = HordeProvider(
            client=client,
            router=self._router(),
            trusted_workers=True,
            outcome_recorder=lambda **item: outcomes.append(item),
        )

        with self.assertRaisesRegex(ProviderError, "invalid structured output"):
            await self._generate_reflection(
                provider,
                user_prompt="reflect",
                validator=lambda text: text == "valid",
            )

        self.assertEqual(
            client.generated_models,
            ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
        )
        self.assertEqual([item["success"] for item in outcomes], [False, False])
        self.assertTrue(all(item["malformed"] for item in outcomes))

        client.alpha_mode = "valid"
        client.beta_text = "valid"
        client.include_alpha_after_refresh = True
        await provider._refresh_metadata(force=True)
        result = await self._generate_reflection(
            provider,
            user_prompt="reflect again",
            validator=lambda text: text == "valid",
        )

        self.assertEqual(result, "valid")
        self.assertEqual(
            client.generated_models,
            [
                "koboldcpp/Alpha-8B",
                "koboldcpp/Beta-8B",
                "koboldcpp/Alpha-8B",
            ],
        )

    async def test_chat_validation_fails_closed_after_two_invalid_outputs(self) -> None:
        client = FakeHordeClient()
        client.alpha_mode = "malformed"
        client.beta_text = "second output is deliverable but still imperfect"
        provider = HordeProvider(
            client=client,
            router=self._router(),
            trusted_workers=True,
        )

        with self.assertRaisesRegex(ProviderError, "invalid structured output"):
            await provider.generate_validated(
                system_prompt="rules",
                user_prompt="chat",
                max_tokens=72,
                temperature=0.7,
                scope="g:1:c:10",
                task="chat",
                context_tokens=8192,
                validator=lambda text: False,
            )
        self.assertEqual(
            client.generated_models,
            ["koboldcpp/Alpha-8B", "koboldcpp/Beta-8B"],
        )


if __name__ == "__main__":
    unittest.main()
