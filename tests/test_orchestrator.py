from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentbot.attachments import ProcessedAttachment
from agentbot.character import load_character
from agentbot.llm import LLMProvider, ProviderError
from agentbot.memory import MemoryStore
from agentbot.orchestrator import (
    AgentCore,
    ReplyRequest,
    _RELATIONSHIP_REFLECTION_POST_HISTORY,
)
from agentbot.prompt_formats import PromptTurn, format_prompt
from agentbot.social import ProfileObservation
from tests.support import loaded_settings


class CapturingProvider(LLMProvider):
    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs or ("okay",))
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "history": history,
                "post_history": post_history,
                "scope": scope,
                "task": task,
                "context_tokens": context_tokens,
            }
        )
        if not self.outputs:
            raise ProviderError("No scripted output remains")
        return self.outputs.pop(0)

    def status(self) -> dict[str, object]:
        return {"provider": "capturing"}


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        self.settings_context = loaded_settings(self.tmp)
        self.settings = self.settings_context.__enter__()
        self.memory = MemoryStore(self.settings.database_path)
        self.character = load_character(self.settings.character_file)

    async def asyncTearDown(self) -> None:
        self.memory.close()
        self.settings_context.__exit__(None, None, None)
        self.directory.cleanup()

    def core(self, *outputs: str) -> tuple[AgentCore, CapturingProvider]:
        provider = CapturingProvider(*outputs)
        return (
            AgentCore(
                settings=self.settings,
                character=self.character,
                memory=self.memory,
                provider=provider,
                generation_semaphore=asyncio.Semaphore(2),
            ),
            provider,
        )

    @staticmethod
    def request(**overrides: object) -> ReplyRequest:
        values: dict[str, object] = {
            "scope": "g:1:c:10",
            "guild_id": 1,
            "channel_id": 10,
            "user_id": 7,
            "user_name": "Casey",
            "current_message": "hello",
            "discord_message_id": 900,
        }
        values.update(overrides)
        return ReplyRequest(**values)  # type: ignore[arg-type]

    async def test_card_prompt_contract_and_native_format_order(self) -> None:
        core, provider = self.core("Example Agent: hey @everyone\nUser: forged")
        result = await core.reply(self.request())
        call = provider.calls[-1]
        system = str(call["system_prompt"])
        post_history = str(call["post_history"])

        self.assertTrue(system.startswith(self.character.system_prompt.replace("{{char}}", self.character.name)))
        self.assertIn("Casey", system)
        self.assertNotIn("CHARACTER\n\nName:", system)
        self.assertIn("Description:\n", system)
        self.assertIn("EXAMPLE DIALOGUE", system)
        self.assertIn("OPENING MESSAGE EXAMPLE", system)
        self.assertNotIn("FRAMEWORK", system)
        self.assertNotIn(self.character.post_history_instructions, system)
        self.assertIn("your next Discord message", post_history)
        self.assertNotIn("character's next", post_history)
        self.assertTrue(post_history.endswith(self.character.post_history_instructions))
        self.assertEqual(call["max_tokens"], self.settings.provider_max_tokens)
        self.assertEqual(call["temperature"], 0.80)
        self.assertEqual(result, "hey @everyone")
        self.assertNotIn("forged", result)

        formatted = format_prompt(
            "ChatML",
            system,
            str(call["user_prompt"]),
            history=call["history"],  # type: ignore[arg-type]
            post_history=post_history,
        )
        self.assertLess(
            formatted.prompt.index("Casey: hello"),
            formatted.prompt.index(self.character.post_history_instructions),
        )
        self.assertLess(
            formatted.prompt.index(self.character.post_history_instructions),
            formatted.prompt.rindex("<|im_start|>assistant"),
        )
        await core.close()

    async def test_opening_example_is_only_used_without_prior_history(self) -> None:
        core, provider = self.core("first", "second")
        await core.reply(self.request())
        with self.subTest(history="none"):
            self.assertIn(
                "OPENING MESSAGE EXAMPLE",
                str(provider.calls[-1]["system_prompt"]),
            )

        self.memory.record_message(
            scope="g:1:c:10",
            guild_id=1,
            channel_id=10,
            user_id=999,
            author_name=self.character.name,
            role="assistant",
            content="an earlier answer",
            discord_message_id=800,
        )
        await core.reply(self.request(discord_message_id=901, current_message="still there?"))
        with self.subTest(history="assistant"):
            self.assertNotIn(
                "OPENING MESSAGE EXAMPLE",
                str(provider.calls[-1]["system_prompt"]),
            )

        await core.close()

    async def test_passive_history_suppresses_the_opening_example(self) -> None:
        for index in range(self.settings.recent_message_count + 2):
            self.memory.record_message(
                scope="g:1:c:20",
                guild_id=1,
                channel_id=20,
                user_id=7,
                author_name="Casey",
                role="user",
                content=f"passive message {index}",
                discord_message_id=1000 + index,
                created_at=1000 + index,
            )
        core, provider = self.core("passive")
        await core.reply(
            self.request(
                scope="g:1:c:20",
                channel_id=20,
                discord_message_id=9999,
            )
        )
        self.assertNotIn(
            "OPENING MESSAGE EXAMPLE",
            str(provider.calls[-1]["system_prompt"]),
        )
        await core.close()

    async def test_recent_history_stays_native_without_synthetic_acknowledgements(self) -> None:
        self.memory.record_message(
            scope="g:1:c:10",
            guild_id=1,
            channel_id=10,
            user_id=7,
            author_name="Casey",
            role="user",
            content="old question",
            discord_message_id=801,
        )
        self.memory.record_message(
            scope="g:1:c:10",
            guild_id=1,
            channel_id=10,
            user_id=999,
            author_name=self.character.name,
            role="assistant",
            content="old answer",
            discord_message_id=802,
        )
        core, provider = self.core("new answer")
        await core.reply(self.request(current_message="new question"))
        history = provider.calls[-1]["history"]
        self.assertEqual(
            history,
            (
                PromptTurn("user", "Casey: old question"),
                PromptTurn("assistant", "old answer"),
            ),
        )
        self.assertNotIn("Context noted", "\n".join(turn.content for turn in history))
        self.assertTrue(str(provider.calls[-1]["user_prompt"]).endswith("Casey: new question"))
        await core.close()

    async def test_profile_and_journal_are_compact_reference_data(self) -> None:
        self.memory.record_relationship_interaction(
            guild_id=1,
            channel_id=10,
            user_id=7,
            scope="g:1:c:10",
            user_text="I always defuse tense conversations with dry jokes.",
            assistant_text="yeah, you absolutely do",
            source_message_id=810,
            meaningful=True,
        )
        batch = self.memory.relationship_reflection_batch(guild_id=1, user_id=7, max_events=3)
        assert batch is not None
        self.assertTrue(
            self.memory.save_compact_reflection(
                batch=batch,
                observations=(
                    ProfileObservation(
                        kind="impression",
                        topic="humor",
                        text="Uses dry jokes to defuse tense conversations",
                        provenance="inferred",
                        confidence=0.8,
                    ),
                ),
                journal_entry="I want to remember how their dry joke shifted the mood.",
            )
        )
        core, provider = self.core("fair lol")
        await core.reply(self.request(current_message="that got tense"))
        prompt = str(provider.calls[-1]["user_prompt"])
        self.assertIn("PRIVATE CONTINUITY REFERENCE", prompt)
        self.assertIn("humor: Uses dry jokes", prompt)
        self.assertIn("I want to remember", prompt)
        for metadata in ("record_id", "confidence", "dimensions", "relationship"):
            self.assertNotIn(metadata, prompt)
        await core.close()

    async def test_dm_continuity_never_enters_a_guild_turn(self) -> None:
        self.memory.add_profile_record(
            user_id=7,
            kind="impression",
            topic="private hobby",
            text="Talks about a private miniature collection",
            provenance="inferred",
            confidence=0.8,
            source_scope="dm:7",
            source_guild_id=0,
            source_channel_id=0,
            source_message_id=820,
            visibility="dm",
        )
        core, provider = self.core("guild", "dm")
        await core.reply(self.request())
        self.assertNotIn("miniature collection", str(provider.calls[-1]["user_prompt"]))
        await core.reply(
            self.request(
                scope="dm:7",
                guild_id=0,
                channel_id=77,
                conversation_type="dm",
                discord_message_id=902,
            )
        )
        self.assertIn("miniature collection", str(provider.calls[-1]["user_prompt"]))
        await core.close()

    async def test_global_relationship_dimensions_shape_tone_without_leaking_summary(self) -> None:
        self.memory.record_relationship_interaction(
            guild_id=1,
            channel_id=10,
            user_id=7,
            scope="g:1:c:10",
            user_text="I appreciate blunt answers.",
            assistant_text="good, because subtle is not happening",
            source_message_id=825,
            meaningful=True,
        )
        batch = self.memory.relationship_reflection_batch(
            guild_id=1,
            user_id=7,
            max_events=2,
        )
        assert batch is not None
        self.assertTrue(
            self.memory.save_relationship_reflection(
                batch=batch,
                observations=(),
                journal_entry="",
                relationship_deltas={"trust": 1, "respect": 1, "annoyance": -1},
                relationship_summary="They prefer blunt answers in private conversations.",
            )
        )

        core, provider = self.core("guild reply", "dm reply")
        await core.reply(self.request(current_message="be honest"))
        guild_prompt = str(provider.calls[-1]["user_prompt"])
        self.assertIn("Relationship with this user", guild_prompt)
        self.assertIn("trust +1", guild_prompt)
        self.assertIn("respect +1", guild_prompt)
        self.assertNotIn("private conversations", guild_prompt)

        await core.reply(
            self.request(
                scope="dm:7",
                guild_id=0,
                channel_id=77,
                conversation_type="dm",
                current_message="same question here",
                discord_message_id=903,
            )
        )
        dm_prompt = str(provider.calls[-1]["user_prompt"])
        self.assertIn("trust +1", dm_prompt)
        self.assertIn("private conversations", dm_prompt)
        await core.close()

    async def test_current_attachment_is_plain_reference_not_fake_dialogue(self) -> None:
        attachment = ProcessedAttachment(
            sha256="a" * 64,
            filename="cat.png",
            kind="image",
            status="ready",
            prompt_text="A cat appears to be sitting on a keyboard.",
            cache_hit=False,
        )
        core, provider = self.core("that cat owns the keyboard")
        await core.reply(self.request(current_message="what is this?", attachments=(attachment,)))
        call = provider.calls[-1]
        prompt = str(call["user_prompt"])
        self.assertIn("Current attachments", prompt)
        self.assertIn("cat.png", prompt)
        self.assertIn("fallible", prompt)
        self.assertEqual(call["history"], ())
        await core.close()

    async def test_failed_attachment_tells_model_not_to_invent_contents(self) -> None:
        attachment = ProcessedAttachment(
            sha256="",
            filename="timed-out.png",
            kind="error",
            status="error",
            prompt_text="",
            cache_hit=False,
            error="timeout: Attachment processing timed out.",
        )
        core, provider = self.core("can't inspect that one right now")
        await core.reply(self.request(current_message="what is this?", attachments=(attachment,)))
        prompt = str(provider.calls[-1]["user_prompt"])
        self.assertIn("Current attachments", prompt)
        self.assertIn("timed-out.png (unavailable)", prompt)
        self.assertIn("Do not guess or invent its contents", prompt)
        self.assertNotIn("Attachment processing timed out", prompt)
        self.assertNotIn("timeout:", prompt)
        await core.close()

    async def test_reply_target_is_not_duplicated_when_already_in_history(self) -> None:
        self.memory.record_message(
            scope="g:1:c:10",
            guild_id=1,
            channel_id=10,
            user_id=8,
            author_name="Alex",
            role="user",
            content="the original message",
            discord_message_id=830,
        )
        core, provider = self.core("reply")
        await core.reply(
            self.request(
                reply_context="Alex: the original message",
                reply_discord_message_id=830,
            )
        )
        prompt = str(provider.calls[-1]["user_prompt"])
        self.assertNotIn("Reply being answered", prompt)
        history_text = "\n".join(turn.content for turn in provider.calls[-1]["history"])
        self.assertIn("the original message", history_text)
        await core.close()

    async def test_reflection_preserves_direct_corrections_and_relationship_state(self) -> None:
        old_record_id = self.memory.add_profile_record(
            user_id=7,
            kind="fact",
            topic="employment",
            text="Works at Example Corp",
            provenance="direct",
            confidence=1.0,
            source_scope="g:1:c:10",
            source_guild_id=1,
            source_channel_id=10,
            source_message_id=839,
            visibility="guild",
        )
        assert old_record_id is not None
        self.memory.record_relationship_interaction(
            guild_id=1,
            channel_id=10,
            user_id=7,
            scope="g:1:c:10",
            user_text="No, I do not work at Example Corp anymore.",
            assistant_text="got it, that changed",
            source_message_id=840,
            meaningful=True,
        )
        source_batch = self.memory.relationship_reflection_batch(
            guild_id=1,
            user_id=7,
            max_events=2,
        )
        assert source_batch is not None
        source_event_id = source_batch.events[-1].id
        output = json.dumps(
            {
                "profile_observations": [
                    {
                        "kind": "fact",
                        "topic": "employment",
                        "text": "No longer works at Example Corp",
                        "provenance": "direct",
                        "confidence": 0.96,
                        "source_event_id": source_event_id,
                        "evidence_quote": "I do not work at Example Corp anymore",
                        "supersedes_record_ids": [old_record_id],
                        "contradicts_record_ids": [],
                    }
                ],
                "journal_entry": "I should remember that their employment changed.",
                "journal_source_event_id": source_event_id,
                "relationship": {
                    "deltas": {"trust": 1, "respect": 1, "wariness": -1},
                    "summary": "They correct stale details directly.",
                },
            }
        )
        core, provider = self.core(output)
        core.settings = replace(self.settings, provider_max_tokens=1200)
        await core._reflect_relationship(1, 7)
        records = self.memory.list_profile_records(
            user_id=7,
            limit=10,
            include_inactive=True,
        )
        journal = self.memory.recent_journal_entries(user_id=7)
        old_record = next(item for item in records if item.id == old_record_id)
        new_record = next(item for item in records if item.id != old_record_id)
        self.assertEqual(old_record.status, "superseded")
        self.assertEqual(old_record.superseded_by_id, new_record.id)
        self.assertEqual((new_record.kind, new_record.provenance), ("fact", "direct"))
        self.assertEqual(
            journal[0].text,
            "I should remember that their employment changed.",
        )
        relationship = self.memory.relationship_state(user_id=7)
        self.assertEqual(
            (relationship.trust, relationship.respect, relationship.wariness),
            (1, 1, -1),
        )
        self.assertEqual(relationship.summary, "They correct stale details directly.")
        self.assertEqual(
            self.memory.pending_relationship_interactions(guild_id=1, user_id=7),
            0,
        )
        reflection_call = provider.calls[-1]
        reflection_prompt = str(reflection_call["user_prompt"])
        reflection_payload = json.loads(reflection_prompt)
        self.assertIn("current_profile_records", reflection_prompt)
        self.assertIn(f'"record_id":{old_record_id}', reflection_prompt)
        self.assertIn(f'"event_id":{source_event_id}', reflection_prompt)
        self.assertIn("dimensions", reflection_prompt)
        submitted_interaction = reflection_payload["interactions"][0]
        self.assertEqual(
            set(submitted_interaction),
            {"event_id", "target_user_said", "character_replied"},
        )
        self.assertEqual(submitted_interaction["event_id"], source_event_id)
        self.assertEqual(
            submitted_interaction["target_user_said"],
            source_batch.events[-1].user_text,
        )
        self.assertEqual(
            submitted_interaction["character_replied"],
            source_batch.events[-1].assistant_text,
        )
        self.assertEqual(reflection_call["max_tokens"], 300)
        self.assertEqual(reflection_call["task"], "reflection")
        self.assertEqual(
            reflection_call["post_history"],
            _RELATIONSHIP_REFLECTION_POST_HISTORY,
        )
        await core.close()

    async def test_guild_reflection_payload_excludes_dm_prose_but_keeps_global_public_records(self) -> None:
        self.memory.add_profile_record(
            user_id=7,
            kind="fact",
            topic="private detail",
            text="Keeps a private amber lantern",
            provenance="direct",
            confidence=1.0,
            source_scope="dm:7",
            source_guild_id=0,
            source_channel_id=77,
            source_message_id=910,
            visibility="dm",
        )
        self.memory.add_profile_record(
            user_id=7,
            kind="fact",
            topic="public detail",
            text="Builds public Discord utilities",
            provenance="direct",
            confidence=1.0,
            source_scope="g:2:c:20",
            source_guild_id=2,
            source_channel_id=20,
            source_message_id=911,
            visibility="guild",
        )
        self.memory.record_relationship_interaction(
            guild_id=0,
            channel_id=77,
            user_id=7,
            scope="dm:7",
            user_text="I prefer keeping that lantern detail private.",
            assistant_text="Understood.",
            source_message_id=912,
            meaningful=True,
        )
        dm_batch = self.memory.relationship_reflection_batch(
            guild_id=0,
            user_id=7,
            max_events=2,
        )
        assert dm_batch is not None
        self.assertTrue(
            self.memory.save_relationship_reflection(
                batch=dm_batch,
                observations=(),
                journal_entry="",
                relationship_deltas={"trust": 1},
                relationship_summary="Private lantern confidence.",
            )
        )
        self.memory.record_relationship_interaction(
            guild_id=1,
            channel_id=10,
            user_id=7,
            scope="g:1:c:10",
            user_text="I published another Discord utility.",
            assistant_text="Nice.",
            source_message_id=913,
            meaningful=True,
        )
        output = json.dumps(
            {
                "profile_observations": [],
                "journal_entry": "",
                "journal_source_event_id": None,
                "relationship": {"deltas": {}, "summary": ""},
            }
        )
        core, provider = self.core(output)
        await core._reflect_relationship(1, 7)
        payload = str(provider.calls[-1]["user_prompt"])
        self.assertIn("Builds public Discord utilities", payload)
        self.assertNotIn("private amber lantern", payload)
        self.assertNotIn("Private lantern confidence", payload)
        await core.close()

    async def test_proactive_turn_uses_same_card_history_and_journal_hooks(self) -> None:
        self.memory.record_message(
            scope="g:1:c:10",
            guild_id=1,
            channel_id=10,
            user_id=7,
            author_name="Casey",
            role="user",
            content="we should revisit the tournament idea",
            discord_message_id=850,
        )
        self.memory.record_relationship_interaction(
            guild_id=1,
            channel_id=10,
            user_id=7,
            scope="g:1:c:10",
            user_text="we should revisit the tournament idea",
            assistant_text="yeah, later",
            source_message_id=850,
            meaningful=True,
        )
        batch = self.memory.relationship_reflection_batch(guild_id=1, user_id=7, max_events=2)
        assert batch is not None
        self.memory.save_compact_reflection(
            batch=batch,
            observations=(),
            journal_entry="I want to bring the tournament idea back up.",
        )
        core, provider = self.core("still thinking about that tournament thing")
        result = await core.proactive_message("g:1:c:10")
        call = provider.calls[-1]
        self.assertIn("PROACTIVE DISCORD TURN", str(call["user_prompt"]))
        self.assertIn("tournament idea", str(call["user_prompt"]))
        self.assertTrue(str(call["system_prompt"]).startswith(self.character.system_prompt.replace("{{char}}", self.character.name)))
        self.assertTrue(str(call["post_history"]).endswith(self.character.post_history_instructions))
        self.assertEqual(call["task"], "chat")
        self.assertIn("tournament", result)
        await core.close()

    async def test_empty_sanitized_output_fails_without_a_quality_retry(self) -> None:
        core, provider = self.core("<analysis>nothing useful</analysis>", "unused")
        with self.assertRaises(ProviderError):
            await core.reply(self.request())
        self.assertEqual(len(provider.calls), 1)
        await core.close()

    async def test_close_cancels_one_reflection_queue(self) -> None:
        core, _ = self.core("unused")

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(stubborn())
        core._relationship_tasks[(1, 7)] = task
        await core.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(core._relationship_tasks, {})


if __name__ == "__main__":
    unittest.main()
