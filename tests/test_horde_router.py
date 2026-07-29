from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentbot.horde_router import (
    HordeRouter,
    LiveModel,
    ModelReference,
    NoEligibleHordeModel,
    parse_live_models,
    parse_reference_csv,
    parse_workers,
)


def live(name: str, *, count: int = 1, eta: int = 0) -> LiveModel:
    return LiveModel(
        name=name,
        count=count,
        performance=10.0,
        queued=0.0,
        jobs=0.0,
        eta=eta,
    )


def reference(
    name: str,
    *,
    parameters: float = 8.0,
    instruction_format: str = "ChatML",
    tags: tuple[str, ...] = (),
    style: str = "",
    settings: dict[str, object] | None = None,
) -> ModelReference:
    return ModelReference(
        name=name,
        parameters_bn=parameters,
        instruction_format=instruction_format,
        tags=frozenset(tags),
        style=style,
        settings={} if settings is None else settings,
    )


class HordeRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.router = HordeRouter(
            metadata_ttl_seconds=90,
            sticky_seconds=120,
            min_parameters_bn=7,
            max_selections=2,
            max_metrics=3,
            trusted_workers=True,
            clock=lambda: self.now,
        )

    def update(
        self,
        live_models: list[LiveModel],
        references: list[ModelReference],
    ) -> None:
        self.router.update(live_models, references, ())

    def select(
        self,
        scope: str = "g:1:c:10",
        *,
        excluded_models: tuple[str, ...] = (),
        max_wait_seconds: float | None = None,
    ):
        return self.router.select(
            scope=scope,
            task="chat",
            context_tokens=8192,
            max_tokens=300,
            excluded_models=excluded_models,
            max_wait_seconds=max_wait_seconds,
        )

    def test_parsers_keep_bounded_live_reference_and_ignore_worker_metadata(self) -> None:
        live_rows = parse_live_models(
            [
                {"name": "koboldcpp/Good-8B", "count": "2", "eta": "4"},
                {"name": "image-only", "count": 1, "type": "image"},
                {"name": "", "count": 1},
            ]
        )
        self.assertEqual([(item.name, item.count, item.eta) for item in live_rows], [
            ("koboldcpp/Good-8B", 2, 4)
        ])

        references = parse_reference_csv(
            "name,parameters_bn,style,tags,instruct_format,settings\n"
            'org/Good-8B,8,roleplay,"chat, popular",ChatML,"{""top_p"":0.8}"\n'
        )
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].settings, {"top_p": 0.8})
        self.assertIn("chat", references[0].tags)

        self.assertEqual(
            parse_workers([{"id": "worker-1", "models": ["koboldcpp/Good-8B"]}]),
            [],
        )

    def test_filters_only_active_supported_minimum_size_references(self) -> None:
        self.update(
            [
                live("koboldcpp/Small-3B"),
                live("koboldcpp/Inactive-8B", count=0),
                live("koboldcpp/Unknown-8B"),
                live("koboldcpp/Good-8B"),
            ],
            [
                reference("org/Small-3B", parameters=3),
                reference("org/Inactive-8B"),
                reference("org/Unknown-8B", instruction_format="FutureFormat"),
                reference("org/Good-8B", tags=("chat",)),
            ],
        )
        self.assertEqual(self.select().model, "koboldcpp/Good-8B")
        self.assertEqual(self.router.status()["eligible_candidate_count"], 1)

        self.update([live("koboldcpp/Small-3B")], [reference("org/Small-3B", parameters=3)])
        with self.assertRaisesRegex(NoEligibleHordeModel, "active 7B"):
            self.select()

    def test_selection_ranking_prefers_roleplay_then_chat_within_wait_budget(self) -> None:
        slow = "koboldcpp/A-Slow-RP-8B"
        fast = "koboldcpp/B-Fast-RP-8B"
        cases = (
            {
                "name": "roleplay_beats_larger_generic_and_chat_models",
                "live_models": [
                    live("koboldcpp/Z-Generic-70B"),
                    live("koboldcpp/Y-Chat-8B"),
                    live("koboldcpp/A-Roleplay-8B"),
                ],
                "references": [
                    reference("org/Z-Generic-70B", parameters=70, tags=("popular",)),
                    reference("org/Y-Chat-8B", tags=("chat",)),
                    reference("org/A-Roleplay-8B", style="roleplay"),
                ],
                "max_wait_seconds": None,
                "expected": "koboldcpp/A-Roleplay-8B",
            },
            {
                "name": "chat_beats_larger_generic_model",
                "live_models": [
                    live("koboldcpp/Z-Generic-70B"),
                    live("koboldcpp/Y-Chat-8B"),
                ],
                "references": [
                    reference("org/Z-Generic-70B", parameters=70, tags=("popular",)),
                    reference("org/Y-Chat-8B", tags=("chat",)),
                ],
                "max_wait_seconds": None,
                "expected": "koboldcpp/Y-Chat-8B",
            },
            {
                "name": "eta_breaks_tie_between_roleplay_models",
                "live_models": [
                    live(slow, count=5, eta=120),
                    live(fast, count=1, eta=5),
                ],
                "references": [
                    reference("org/A-Slow-RP-8B", tags=("roleplay",)),
                    reference("org/B-Fast-RP-8B", tags=("roleplay",)),
                ],
                "max_wait_seconds": 45,
                "expected": fast,
            },
            {
                "name": "wait_budget_prefers_timely_chat_model",
                "live_models": [
                    live(slow, count=5, eta=120),
                    live("koboldcpp/C-Chat-8B", eta=0),
                ],
                "references": [
                    reference("org/A-Slow-RP-8B", tags=("roleplay",)),
                    reference("org/C-Chat-8B", tags=("chat",)),
                ],
                "max_wait_seconds": 45,
                "expected": "koboldcpp/C-Chat-8B",
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                self.router.clear_selection(scope="g:1:c:10", task="chat")
                self.update(case["live_models"], case["references"])
                self.assertEqual(
                    self.select(max_wait_seconds=case["max_wait_seconds"]).model,
                    case["expected"],
                )

    def test_selection_is_sticky_until_idle_expiry(self) -> None:
        first = "koboldcpp/A-Roleplay-8B"
        second = "koboldcpp/B-Roleplay-8B"
        self.update(
            [live(first, count=2), live(second)],
            [reference("org/A-Roleplay-8B", tags=("roleplay",)), reference("org/B-Roleplay-8B", tags=("roleplay",))],
        )
        self.assertEqual(self.select().model, first)

        self.now = 60
        self.update(
            [live(first), live(second, count=20)],
            [reference("org/A-Roleplay-8B", tags=("roleplay",)), reference("org/B-Roleplay-8B", tags=("roleplay",))],
        )
        self.assertEqual(self.select().model, first)

        self.now = 181
        self.assertEqual(self.select().model, second)

    def test_exclusion_and_clear_force_one_alternate(self) -> None:
        first = "koboldcpp/A-Roleplay-8B"
        second = "koboldcpp/B-Chat-8B"
        self.update(
            [live(first), live(second)],
            [reference("org/A-Roleplay-8B", tags=("roleplay",)), reference("org/B-Chat-8B", tags=("chat",))],
        )
        self.assertEqual(self.select().model, first)
        self.assertEqual(self.select(excluded_models=(first,)).model, second)
        self.router.clear_selection(scope="g:1:c:10", task="chat")
        self.assertEqual(self.select().model, first)

    def test_metadata_refresh_updates_sticky_format_and_settings(self) -> None:
        model = "koboldcpp/Roleplay-8B"
        self.update(
            [live(model)],
            [reference("org/Roleplay-8B", tags=("roleplay",), settings={"top_p": 0.8})],
        )
        initial = self.select()
        self.now = 20
        self.router.update_metadata(
            [live(model, count=3)],
            [reference("org/Roleplay-8B", instruction_format="Mistral", tags=("roleplay",), settings={"top_p": 0.9})],
            (),
        )
        refreshed = self.select()
        self.assertEqual(refreshed.model, initial.model)
        self.assertEqual(refreshed.instruction_format, "Mistral")
        self.assertEqual(refreshed.settings, {"top_p": 0.9})
        self.assertEqual(refreshed.selected_at, initial.selected_at)

    def test_one_recent_failure_prefers_an_alternate_without_a_metric_circuit(self) -> None:
        roleplay = "koboldcpp/A-Roleplay-8B"
        chat = "koboldcpp/B-Chat-8B"
        self.update(
            [live(roleplay), live(chat)],
            [reference("org/A-Roleplay-8B", tags=("roleplay",)), reference("org/B-Chat-8B", tags=("chat",))],
        )
        self.router.record_result(
            model=roleplay,
            task="chat",
            success=False,
            latency_seconds=60,
            error_kind="timeout",
        )
        self.router.seed_outcomes([SimpleNamespace(model=roleplay, success=False)])
        self.router.clear_selection(scope="g:1:c:10", task="chat")
        self.assertEqual(self.select().model, chat)
        self.assertEqual(self.router.status()["recent_model_failures"][0]["model"], roleplay)

        self.now = 301
        self.router.clear_selection(scope="g:1:c:10", task="chat")
        self.assertEqual(self.select().model, roleplay)
        self.router.record_result(
            model=roleplay,
            task="chat",
            success=True,
            latency_seconds=2,
        )
        self.assertEqual(self.router.status()["recent_model_failures"], [])

    def test_selection_map_is_bounded_and_status_is_compact(self) -> None:
        model = "koboldcpp/Roleplay-8B"
        self.update([live(model)], [reference("org/Roleplay-8B", tags=("roleplay",))])
        for index in range(4):
            self.select(scope=f"scope:{index}")
        status = self.router.status()
        self.assertEqual(status["selection_count"], 2)
        self.assertEqual(len(status["selected_models"]), 2)
        self.assertEqual(status["recent_model_failures"], [])
        self.assertEqual(
            set(status["selected_models"][0]),
            {"task", "scope", "model", "format", "selection_age_seconds", "idle_seconds"},
        )


if __name__ == "__main__":
    unittest.main()
