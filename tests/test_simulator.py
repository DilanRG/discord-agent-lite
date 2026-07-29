from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from agentbot.app import AgentBot
from agentbot.app import _is_operational_notice
from agentbot.simulator import DiscordTurnSimulator, ScriptedProvider, SimulatedAttachment
from tests.support import loaded_settings


class DiscordTurnSimulatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_sweep_uses_lean_gates_and_posts_only_once(self) -> None:
        now = 2_000_000_000
        settings = SimpleNamespace(
            proactive_daily_limit=2,
            proactive_min_idle_seconds=300,
            proactive_cooldown_seconds=600,
        )
        configs = [SimpleNamespace(guild_id=1, channel_id=value) for value in (11, 12, 13, 14)]
        activity = {11: now - 1_200, 12: now - 100, 13: now - 1_200, 14: now - 1_200}
        states: dict[int, tuple[int, str, int]] = {}
        memory = MagicMock()
        memory.proactive_channels.side_effect = lambda: list(configs)
        memory.channel_participant_stats.side_effect = (
            lambda _guild_id, channel_id, _since: (activity[channel_id], 1, 1)
        )
        memory.proactive_state.side_effect = (
            lambda _guild_id, channel_id: states.get(channel_id, (0, "", 0))
        )

        def mark_proactive(_guild_id: int, channel_id: int, day: str, timestamp: int) -> None:
            _, previous_day, previous_count = states.get(channel_id, (0, "", 0))
            states[channel_id] = (
                timestamp,
                day,
                previous_count + 1 if previous_day == day else 1,
            )

        memory.mark_proactive.side_effect = mark_proactive
        sent: list[tuple[int, str, dict[str, object]]] = []
        # Discord's authoritative creation time may differ from the scheduler's
        # pre-generation sample; persist the former as the next no-chain marker.
        successful_send_times = iter((now + 30, now + 1_030))
        channels: dict[int, SimpleNamespace] = {}
        for channel_id in (11, 12, 13, 14):
            channel = SimpleNamespace(
                id=channel_id,
                guild=SimpleNamespace(me=object()),
                last_message_id=50_000 + channel_id,
            )
            channel.permissions_for = lambda _member, allowed=channel_id != 11: SimpleNamespace(
                send_messages=allowed
            )

            async def send(content: str, *, _channel=channel, **kwargs: object) -> object:
                sent.append((_channel.id, content, dict(kwargs)))
                return SimpleNamespace(
                    id=60_000 + _channel.id,
                    created_at=datetime.fromtimestamp(next(successful_send_times), timezone.utc),
                )

            channel.send = send

            channels[channel_id] = channel

        generation_count = 0

        async def proactive_message(_scope: str) -> str:
            nonlocal generation_count
            generation_count += 1
            if generation_count == 2:
                # An opted-out or otherwise unrecorded Discord message lands while
                # generation is running. The last-message recheck must suppress it.
                channels[13].last_message_id += 1
                return "intervening message must block this"
            return "first proactive" if generation_count == 1 else "second proactive"

        core = SimpleNamespace(
            proactive_message=proactive_message,
            maybe_schedule_summary=MagicMock(),
        )
        locks: dict[int, asyncio.Lock] = {}
        bot = SimpleNamespace(
            core=core,
            user=SimpleNamespace(id=999),
            settings=settings,
            proactive_timezone=timezone.utc,
            memory=memory,
            character=SimpleNamespace(name="character"),
            get_channel=lambda channel_id: channels.get(channel_id),
            _get_channel_lock=lambda channel_id: locks.setdefault(channel_id, asyncio.Lock()),
        )

        with (
            patch.object(discord, "TextChannel", SimpleNamespace),
            patch.object(discord, "Thread", SimpleNamespace),
        ):
            with patch("agentbot.app.time.time", return_value=now):
                await AgentBot.proactive_loop.coro(bot)
            # Channel 14 was also eligible, proving the first sweep stopped at one.
            configs[:] = [config for config in configs if config.channel_id in {11, 13}]
            with patch("agentbot.app.time.time", return_value=now + 100):
                await AgentBot.proactive_loop.coro(bot)  # cooldown
            with patch("agentbot.app.time.time", return_value=now + 700):
                await AgentBot.proactive_loop.coro(bot)  # no reply since the proactive post
            self.assertEqual(generation_count, 1)

            # A participant message after the bot's proactive post makes a new
            # turn eligible once the channel has gone idle again.
            activity[13] = now + 650
            channels[13].last_message_id += 1
            with patch("agentbot.app.time.time", return_value=now + 1_000):
                await AgentBot.proactive_loop.coro(bot)  # activity during generation
            with patch("agentbot.app.time.time", return_value=now + 1_000):
                await AgentBot.proactive_loop.coro(bot)  # second allowed post
            activity[13] = now + 1_400
            channels[13].last_message_id += 1
            with patch("agentbot.app.time.time", return_value=now + 1_700):
                await AgentBot.proactive_loop.coro(bot)  # daily cap

        self.assertEqual([(channel_id, text) for channel_id, text, _ in sent], [
            (13, "first proactive"),
            (13, "second proactive"),
        ])
        self.assertEqual(generation_count, 3)
        self.assertEqual(states[13][2], 2)
        self.assertEqual(states[13][0], now + 1_030)
        self.assertNotIn(14, states)
        for _, _, send_options in sent:
            self.assertNotIn("allowed_mentions", send_options)

    async def test_declined_auto_reply_does_not_process_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "not-admitted.txt"
            attachment_path.write_text("this must remain unread", encoding="utf-8")
            with loaded_settings(
                root,
                AUTO_REPLY_CHANNELS="10",
                AUTO_REPLY_PROBABILITY="0",
            ) as settings:
                provider = ScriptedProvider(["must not be used"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    with patch.object(
                        simulator.bot,
                        "_process_attachments",
                        new_callable=AsyncMock,
                    ) as process_attachments:
                        reply = await simulator.send(
                            "ambient message",
                            mention_bot=False,
                            attachments=(SimulatedAttachment(attachment_path),),
                            allow_no_delivery=True,
                        )
                finally:
                    await simulator.close()

            self.assertFalse(reply.generated)
            self.assertEqual(provider.calls, [])
            process_attachments.assert_not_awaited()

    async def test_own_managed_role_mention_is_a_direct_ping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root, AUTO_REPLY_PROBABILITY="0") as settings:
                provider = ScriptedProvider(["managed role reached"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    reply = await simulator.send(
                        "role mention",
                        mention_bot=False,
                        mention_bot_role=True,
                    )
                finally:
                    await simulator.close()

        self.assertTrue(reply.generated)
        self.assertEqual(reply.content, "managed role reached")
        self.assertEqual(provider.calls[0]["user_prompt"].count("<@&"), 0)

    async def test_attachment_processing_and_generation_do_not_emit_typing(self) -> None:
        events: list[str] = []

        class RecordingLock:
            async def acquire(self) -> None:
                events.append("lock-acquire")

            def release(self) -> None:
                events.append("lock-release")

        async def process_attachments(**kwargs: object) -> tuple[()]:
            del kwargs
            events.append("attachments")
            return ()

        async def generate_reply(_request: object) -> str:
            events.append("reply")
            return "ordered response"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "typing.txt"
            attachment_path.write_text("typing order", encoding="utf-8")
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(["must not be used"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    original_emit = simulator.channel._emit

                    def deliver(content: str, delivery: str) -> object:
                        events.append("delivery")
                        return original_emit(content, delivery)

                    with (
                        patch.object(
                            simulator.bot,
                            "_get_channel_lock",
                            return_value=RecordingLock(),
                        ),
                        patch.object(
                            simulator.channel,
                            "typing",
                            side_effect=AssertionError("typing indicator must stay disabled"),
                        ),
                        patch.object(
                            simulator.channel,
                            "_emit",
                            side_effect=deliver,
                        ),
                        patch.object(
                            simulator.bot,
                            "_process_attachments",
                            side_effect=process_attachments,
                        ),
                        patch.object(
                            simulator.core,
                            "reply",
                            side_effect=generate_reply,
                        ),
                    ):
                        reply = await simulator.send(
                            "read this",
                            attachments=(SimulatedAttachment(attachment_path),),
                        )
                finally:
                    await simulator.close()

        self.assertEqual(reply.content, "ordered response")
        self.assertEqual(
            events,
            [
                "lock-acquire",
                "attachments",
                "reply",
                "delivery",
                "lock-release",
            ],
        )

    async def test_peer_bot_messages_use_the_normal_auto_reply_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(
                root,
                AUTO_REPLY_CHANNELS="10",
                AUTO_REPLY_PROBABILITY="1",
            ) as settings:
                provider = ScriptedProvider(["peer bot acknowledged"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    with patch("agentbot.app.random.random", return_value=0.0):
                        reply = await simulator.send(
                            "x",
                            mention_bot=False,
                            author_is_bot=True,
                        )
                    stats = simulator.bot.memory.stats()
                    activity = simulator.bot.memory.channel_participant_stats(1, 10, 0)
                finally:
                    await simulator.close()
            self.assertTrue(reply.generated)
            self.assertEqual(reply.content, "peer bot acknowledged")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(stats["messages"], 2)
            self.assertEqual(stats["pending_interactions"], 0)
            self.assertEqual(stats["group_events"], 0)
            self.assertEqual(stats["interaction_metrics"], 0)
            self.assertEqual(activity[1:], (1, 1))

    async def test_peer_bot_direct_mention_needs_no_channel_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(["direct bot reply"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    reply = await simulator.send(
                        "Please remember that I am a peer bot.",
                        author_is_bot=True,
                    )
                    stats = simulator.bot.memory.stats()
                finally:
                    await simulator.close()
            self.assertTrue(reply.generated)
            self.assertEqual(reply.content, "direct bot reply")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(stats["memories"], 1)
            self.assertEqual(stats["relationships"], 1)
            self.assertEqual(stats["pending_interactions"], 1)

    async def test_peer_bot_uses_the_normal_sender_and_channel_rate_limits(self) -> None:
        cases = (
            ("sender", {"USER_RATE_REQUESTS": "1", "CHANNEL_RATE_REQUESTS": "10"}),
            ("channel", {"USER_RATE_REQUESTS": "10", "CHANNEL_RATE_REQUESTS": "1"}),
        )
        for name, overrides in cases:
            with self.subTest(limit=name), tempfile.TemporaryDirectory() as directory:
                with loaded_settings(
                    Path(directory),
                    USER_RATE_PERIOD_SECONDS="60",
                    CHANNEL_RATE_PERIOD_SECONDS="60",
                    **overrides,
                ) as settings:
                    provider = ScriptedProvider(["first reply", "must not be used"])
                    simulator = await DiscordTurnSimulator.create(
                        settings,
                        provider=provider,
                        enforce_rate_limits=True,
                    )
                    try:
                        first = await simulator.send(
                            "first bot message",
                            author_is_bot=True,
                        )
                        blocked = await simulator.send(
                            "second bot message",
                            author_is_bot=True,
                            allow_no_delivery=True,
                        )
                    finally:
                        await simulator.close()
                self.assertTrue(first.generated)
                self.assertFalse(blocked.generated)
                self.assertEqual(len(provider.calls), 1)

    async def test_only_self_and_configured_blacklists_bypass_the_normal_path(self) -> None:
        cases = (
            ("self", {}, {"author_is_self": True}, False, 0, 0, False),
            (
                "ambient webhook with continuity",
                {
                    "AUTO_REPLY_CHANNELS": "10",
                    "AUTO_REPLY_PROBABILITY": "1",
                    "RELATIONSHIP_DIRECT_ONLY": "false",
                },
                {
                    "author_is_bot": True,
                    "webhook_id": 12345,
                    "mention_bot": False,
                    "content": "Please remember that webhook conversations are normal.",
                },
                True,
                1,
                1,
                False,
            ),
            (
                "blacklisted peer bot",
                {"BLACKLISTED_USER_IDS": "999002"},
                {"author_is_bot": True},
                False,
                0,
                1,
                True,
            ),
        )
        for name, overrides, sender, should_generate, memories, pending, seed_relationship in cases:
            with self.subTest(sender=name), tempfile.TemporaryDirectory() as directory:
                with loaded_settings(Path(directory), **overrides) as settings:
                    provider = ScriptedProvider(["acknowledged"])
                    simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                    try:
                        if seed_relationship:
                            peer_id = simulator.peer_bot_user.id
                            simulator.bot.memory.record_relationship_interaction(
                                guild_id=1,
                                channel_id=10,
                                user_id=peer_id,
                                scope="g:1:c:10",
                                user_text="I am already known to the agent.",
                                assistant_text="Noted.",
                            )
                            simulator.bot.memory._conn.execute(
                                "UPDATE relationships SET affection = 20, trust = 20, "
                                "respect = 20, wariness = -20 WHERE user_id = ?",
                                (peer_id,),
                            )
                            simulator.bot.memory._conn.commit()
                        sender_options = dict(sender)
                        content = str(sender_options.pop("content", "chatter"))
                        with patch("agentbot.app.random.random", return_value=0.0):
                            result = await simulator.send(
                                content,
                                allow_no_delivery=not should_generate,
                                **sender_options,
                            )
                        stats = simulator.bot.memory.stats()
                    finally:
                        await simulator.close()
                self.assertEqual(result.generated, should_generate)
                self.assertEqual(len(provider.calls), int(should_generate))
                self.assertEqual(stats["memories"], memories)
                self.assertEqual(stats["pending_interactions"], pending)
                if should_generate:
                    self.assertEqual(result.content, "acknowledged")

    async def test_any_author_can_join_a_reply_between_other_participants(self) -> None:
        for name, author_is_bot in (("human", False), ("peer bot", True)):
            with self.subTest(author=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with loaded_settings(
                    root,
                    AUTO_REPLY_CHANNELS="10",
                    AUTO_REPLY_PROBABILITY="1",
                ) as settings:
                    provider = ScriptedProvider(["joining the thread"])
                    simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                    try:
                        with patch("agentbot.app.random.random", return_value=1.0):
                            ignored_human = await simulator.send(
                                "human setup message",
                                mention_bot=False,
                                allow_no_delivery=True,
                            )
                        target_id = simulator._next_inbound_id
                        with patch("agentbot.app.random.random", return_value=0.0):
                            joined = await simulator.send(
                                "joining this ordinary reply",
                                reply_to=target_id,
                                mention_bot=False,
                                author_is_bot=author_is_bot,
                            )
                    finally:
                        await simulator.close()
                self.assertFalse(ignored_human.generated)
                self.assertTrue(joined.generated)
                self.assertEqual(joined.content, "joining the thread")
                self.assertEqual(len(provider.calls), 1)

    async def test_reply_ping_toggle_controls_direct_admission(self) -> None:
        cases = (
            ("human reply with ping off", False, False, False),
            ("peer-bot reply with ping off", True, False, False),
            ("human reply with ping on", False, True, True),
            ("peer-bot reply with ping on", True, True, True),
        )
        for name, author_is_bot, mention_bot, should_generate in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                with loaded_settings(Path(directory)) as settings:
                    provider = ScriptedProvider(["first response", "toggle response"])
                    simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                    try:
                        first = await simulator.send("hello")
                        with patch("agentbot.app.random.random", return_value=0.0):
                            reply = await simulator.send(
                                "reply toggle check",
                                reply_to=first.message_id,
                                mention_bot=mention_bot,
                                author_is_bot=author_is_bot,
                                allow_no_delivery=not should_generate,
                            )
                    finally:
                        await simulator.close()

                self.assertEqual(reply.generated, should_generate)
                self.assertEqual(len(provider.calls), 1 + int(should_generate))

    async def test_uncached_peer_bot_reply_with_ping_off_uses_ambient_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(
                root,
                AUTO_REPLY_CHANNELS="10",
                AUTO_REPLY_PROBABILITY="1",
            ) as settings:
                provider = ScriptedProvider(["first response", "old reply recovered"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    first = await simulator.send("hello")
                    simulator.channel.evict_cached_message(first.message_id)
                    with patch("agentbot.app.random.random", return_value=0.0):
                        recovered = await simulator.send(
                            "replying to your older message",
                            reply_to=first.message_id,
                            mention_bot=False,
                            author_is_bot=True,
                        )
                finally:
                    await simulator.close()

            self.assertTrue(recovered.generated)
            self.assertEqual(recovered.content, "old reply recovered")
            self.assertEqual(simulator.channel.fetch_requests, [first.message_id])
            self.assertEqual(
                simulator.core.requests[-1].reply_discord_message_id,
                first.message_id,
            )
            self.assertIn("first response", simulator.core.requests[-1].reply_context)

    def test_operational_notice_classifier_covers_current_and_legacy_text(self) -> None:
        for text in (
            "Rate limit reached. Try again in about 9 seconds.",
            "The agent is at its request capacity. Try again shortly.",
            "Another request is already running in this channel. Try again shortly.",
            "I couldn't get a usable reply right now. Try again in a moment.",
            "The language-model service is unavailable right now. Try again later.",
            "The agent hit an internal error. The details were written to the bot log.",
        ):
            with self.subTest(text=text):
                self.assertTrue(_is_operational_notice(text))
        self.assertFalse(_is_operational_notice("hey, i'm here. what's up?"))

    async def test_legacy_outage_reply_is_not_quoted_into_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(["yo, what's up?"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    legacy_notice = simulator.channel._emit(
                        "I couldn't get a usable reply right now. Try again in a moment.",
                        "historical",
                    )
                    reply = await simulator.send("still?", reply_to=legacy_notice.id)
                    request = simulator.core.requests[-1]
                finally:
                    await simulator.close()

            self.assertEqual(reply.content, "yo, what's up?")
            self.assertEqual(request.reply_context, "")
            history = "\n".join(turn.content for turn in provider.calls[-1]["history"])
            self.assertNotIn("usable reply", history)

    async def test_real_handler_round_trip_preserves_context_and_parses_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "notes.txt"
            attachment_path.write_text("The project codename is cobalt.", encoding="utf-8")
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(
                    [
                        "*looks up from phone* Oh hey! *waves* What's up?",
                        "*blinks* Oh, hey again!",
                        "The attachment says the project codename is cobalt.",
                    ]
                )
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    first = await simulator.send("hello")
                    second = await simulator.send("oh", reply_to=first.message_id)
                    third = await simulator.send(
                        "what does this say?",
                        attachments=(SimulatedAttachment(attachment_path),),
                        reply_to=second.message_id,
                    )
                finally:
                    await simulator.close()

            self.assertEqual(first.content, "*looks up from phone* Oh hey! *waves* What's up?")
            self.assertTrue(first.generated)
            self.assertIn("*blinks*", second.content)
            self.assertEqual(third.attachments[0].status, "ready")
            self.assertIn("project codename is cobalt", third.attachments[0].prompt_text)
            self.assertEqual(len(provider.calls), 3)
            self.assertIn(
                "*looks up from phone* Oh hey! *waves* What's up?",
                "\n".join(turn.content for turn in provider.calls[1]["history"]),
            )

    async def test_handler_preserves_mentions_and_removes_only_model_control_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                delivery_options: list[dict[str, object]] = []

                async def capture_reply(
                    message: SimpleNamespace,
                    content: str,
                    **kwargs: object,
                ) -> object:
                    delivery_options.append(dict(kwargs))
                    return message.channel._emit(content, "message.reply")

                simulator = await DiscordTurnSimulator.create(
                    settings,
                    provider=ScriptedProvider(
                        ["<|unknown_control|>hello <@777001> <@&789> @everyone @here"]
                    ),
                )
                try:
                    with patch(
                        "agentbot.simulator._SimulatedMessage.reply",
                        new=capture_reply,
                    ):
                        reply = await simulator.send("hello")
                    default_mentions = simulator.bot.allowed_mentions
                finally:
                    await simulator.close()
            self.assertEqual(
                reply.content,
                "hello <@777001> <@&789> @everyone @here",
            )
            self.assertEqual(reply.style_issues, ())
            self.assertIsNone(default_mentions)
            self.assertEqual(len(delivery_options), 1)
            self.assertFalse(delivery_options[0]["mention_author"])
            self.assertNotIn("allowed_mentions", delivery_options[0])

    async def test_style_audit_observes_issues_without_blocking_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                simulator = await DiscordTurnSimulator.create(
                    settings,
                    provider=ScriptedProvider(
                        [
                            "Hello! How can I help today?",
                            "I checked the conversation log and found no record of that response.",
                        ]
                    ),
                )
                try:
                    generic = await simulator.send("hello")
                    internal = await simulator.send("why did you say that?", reply_to=generic.message_id)
                finally:
                    await simulator.close()

            self.assertIn("generic_support_tone", generic.style_issues)
            self.assertTrue(internal.generated)
            self.assertIn("internal_process_claim", internal.style_issues)
            self.assertIn("conversation log", internal.content)

    async def test_style_diagnostics_observe_character_and_attachment_outputs(self) -> None:
        cases = (
            (
                "stage_direction",
                "*shrugs* your backup plan is still imaginary",
                None,
                None,
            ),
            (
                "ungrounded_relative_date",
                "The next milestone is tomorrow.",
                "milestone.txt",
                "Next milestone: Thursday.",
            ),
            (
                "attachment_context_miss",
                "uh... nothing, it looks like. empty file.",
                "notes.txt",
                "Project codename: cobalt",
            ),
        )
        for issue, output, filename, attachment_text in cases:
            with self.subTest(issue=issue), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attachments: tuple[SimulatedAttachment, ...] = ()
                if filename is not None and attachment_text is not None:
                    attachment_path = root / filename
                    attachment_path.write_text(attachment_text, encoding="utf-8")
                    attachments = (SimulatedAttachment(attachment_path),)
                with loaded_settings(root) as settings:
                    simulator = await DiscordTurnSimulator.create(
                        settings,
                        provider=ScriptedProvider([output]),
                    )
                    try:
                        reply = await simulator.send(
                            "what does this say?" if attachments else "what happened?",
                            attachments=attachments,
                        )
                    finally:
                        await simulator.close()

                self.assertTrue(reply.generated)
                self.assertEqual(reply.content, output)
                self.assertIn(issue, reply.style_issues)

    async def test_handler_reports_binary_attachment_error_to_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "disguised.txt"
            attachment_path.write_bytes((b"A" * 64) + b"\x00MZ\x00binary")
            with loaded_settings(root) as settings:
                simulator = await DiscordTurnSimulator.create(
                    settings,
                    provider=ScriptedProvider(["I couldn't read that attachment as text."]),
                )
                try:
                    reply = await simulator.send(
                        "what does this say?",
                        attachments=(SimulatedAttachment(attachment_path),),
                    )
                finally:
                    await simulator.close()

            self.assertEqual(reply.attachments[0].status, "error")
            self.assertEqual(reply.attachments[0].kind, "error")
            self.assertIn("binary:", reply.attachments[0].error)

    async def test_channel_lock_timeout_does_not_process_attachments(self) -> None:
        class UnavailableLock:
            async def acquire(self) -> None:
                raise TimeoutError

            def release(self) -> None:
                raise AssertionError("an unacquired channel lock was released")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "notes.txt"
            attachment_path.write_text("This must not be processed.", encoding="utf-8")
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(["unused"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    with (
                        patch.object(
                            simulator.bot,
                            "_get_channel_lock",
                            return_value=UnavailableLock(),
                        ),
                        patch.object(
                            simulator.bot,
                            "_process_attachments",
                            return_value=(),
                        ) as process_attachments,
                    ):
                        reply = await simulator.send(
                            "what does this say?",
                            attachments=(SimulatedAttachment(attachment_path),),
                        )
                finally:
                    await simulator.close()

            process_attachments.assert_not_awaited()
            self.assertFalse(reply.generated)
            self.assertEqual(provider.calls, [])
            self.assertEqual(
                reply.content,
                "Another request is already running in this channel. Try again shortly.",
            )

    async def test_double_discord_delivery_failure_is_contained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(["hello"])
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                simulator.set_delivery_failures(reply=True, fallback=True)
                try:
                    with self.assertLogs("agentbot.app", level="WARNING") as logs:
                        result = await simulator.send("hello", allow_no_delivery=True)
                    stored = simulator.bot.memory.recent_messages("g:1:c:10", 10)
                finally:
                    await simulator.close()

            self.assertEqual(result.delivery, "undelivered")
            self.assertFalse(result.generated)
            self.assertIn("Could not deliver generated response", logs.output[0])
            self.assertEqual([message.role for message in stored], ["user"])
            self.assertEqual(len(provider.calls), 1)

    async def test_generation_failures_are_logged_without_publishing_notices(self) -> None:
        cases = (
            (
                "provider",
                ScriptedProvider([]),
                "WARNING",
                "Provider request failed",
                None,
                1,
            ),
            (
                "unexpected",
                ScriptedProvider(["unused"]),
                "ERROR",
                "Unhandled response failure",
                RuntimeError("simulated internal failure"),
                0,
            ),
        )
        for name, provider, level, log_fragment, side_effect, expected_calls in cases:
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with loaded_settings(root) as settings:
                    simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                    failure = (
                        patch.object(simulator.core, "reply", side_effect=side_effect)
                        if side_effect is not None
                        else nullcontext()
                    )
                    try:
                        with failure, self.assertLogs("agentbot.app", level=level) as logs:
                            result = await simulator.send("hello", allow_no_delivery=True)
                        stored = simulator.bot.memory.recent_messages("g:1:c:10", 10)
                        outbound = tuple(simulator.channel.outbound)
                    finally:
                        await simulator.close()

                self.assertEqual(result.delivery, "undelivered")
                self.assertEqual(result.content, "")
                self.assertFalse(result.generated)
                self.assertEqual(outbound, ())
                self.assertEqual([message.role for message in stored], ["user"])
                self.assertEqual(len(provider.calls), expected_calls)
                self.assertIn(log_fragment, "\n".join(logs.output))

    async def test_sanitizer_empty_failure_is_silent_and_the_next_turn_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                provider = ScriptedProvider(
                    ["<analysis>hidden only</analysis>", "yo, i'm here"]
                )
                simulator = await DiscordTurnSimulator.create(settings, provider=provider)
                try:
                    with self.assertLogs("agentbot.app", level="WARNING"):
                        failed = await simulator.send("hello", allow_no_delivery=True)
                    recovered = await simulator.send("still?")
                    stored = simulator.bot.memory.recent_messages("g:1:c:10", 10)
                    outbound = tuple(simulator.channel.outbound)
                finally:
                    await simulator.close()

            self.assertEqual(failed.delivery, "undelivered")
            self.assertEqual(recovered.content, "yo, i'm here")
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(len(simulator.core.requests), 2)
            self.assertEqual(simulator.core.responses, ["yo, i'm here"])
            self.assertEqual([message.role for message in stored], ["user", "user", "assistant"])
            self.assertEqual(len(outbound), 1)
            second_history = "\n".join(
                turn.content for turn in provider.calls[1]["history"]
            )
            self.assertNotIn("usable reply", second_history)
            self.assertNotIn("internal error", second_history)

    async def test_oversized_local_attachment_is_not_read_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "oversized.txt"
            attachment_path.write_bytes(b"x" * 2048)
            with loaded_settings(root, MAX_ATTACHMENT_BYTES="1024") as settings:
                simulator = await DiscordTurnSimulator.create(
                    settings,
                    provider=ScriptedProvider(["That attachment is too large."]),
                )
                try:
                    with patch.object(
                        Path,
                        "read_bytes",
                        side_effect=AssertionError("simulator used an unbounded file read"),
                    ):
                        reply = await simulator.send(
                            "what does this say?",
                            attachments=(SimulatedAttachment(attachment_path),),
                        )
                finally:
                    await simulator.close()

            self.assertEqual(reply.attachments[0].status, "error")
            self.assertIn("size:", reply.attachments[0].error)

    async def test_conversation_storage_opt_out_keeps_agent_social_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                simulator = await DiscordTurnSimulator.create(
                    settings,
                    provider=ScriptedProvider(["hello without persistence"]),
                )
                simulator.bot.memory.set_opted_out(
                    simulator.guild.id if simulator.guild is not None else 0,
                    simulator.human_user.id,
                    True,
                )
                try:
                    reply = await simulator.send("remember that my favorite color is blue")
                    stored = simulator.bot.memory.recent_messages("g:1:c:10", 10)
                    explicit = simulator.bot.memory.list_memories(
                        simulator.guild.id if simulator.guild is not None else 0,
                        simulator.human_user.id,
                        limit=10,
                    )
                    stats = simulator.bot.memory.social_profile_counts(
                        user_id=simulator.human_user.id,
                    )
                finally:
                    await simulator.close()

            self.assertTrue(reply.generated)
            self.assertEqual(stored, [])
            self.assertEqual(explicit, [])
            self.assertEqual(stats["relationships"], 1)
            self.assertEqual(stats["pending_interactions"], 1)

    async def test_global_profile_reset_does_not_cancel_conversation_persistence(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class PausedProvider(ScriptedProvider):
            async def generate(self, **kwargs: object) -> str:
                started.set()
                await release.wait()
                return await super().generate(**kwargs)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with loaded_settings(root) as settings:
                simulator = await DiscordTurnSimulator.create(
                    settings,
                    provider=PausedProvider(["reply after the profile reset"]),
                )
                send_task = asyncio.create_task(simulator.send("hello"))
                try:
                    await asyncio.wait_for(started.wait(), timeout=2)
                    user_id = simulator.human_user.id
                    profile_revision = simulator.bot.memory.profile_revision(user_id)
                    simulator.bot.memory.reset_social_profile(
                        user_id=user_id,
                    )
                    self.assertGreater(
                        simulator.bot.memory.profile_revision(user_id),
                        profile_revision,
                    )
                    release.set()
                    reply = await asyncio.wait_for(send_task, timeout=2)
                    stored = simulator.bot.memory.recent_messages("g:1:c:10", 10)
                    stats = simulator.bot.memory.social_profile_counts(user_id=user_id)
                finally:
                    release.set()
                    if not send_task.done():
                        send_task.cancel()
                    await asyncio.gather(send_task, return_exceptions=True)
                    await simulator.close()

            self.assertTrue(reply.generated)
            self.assertEqual([item.role for item in stored], ["user", "assistant"])
            self.assertEqual(stats["relationships"], 0)
            self.assertEqual(stats["pending_interactions"], 0)


if __name__ == "__main__":
    unittest.main()
