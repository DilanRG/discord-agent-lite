from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from agentbot.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.directory.name) / "agent.db")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _replace_store(self, filename: str, **kwargs: int) -> None:
        self.store.close()
        self.store = MemoryStore(Path(self.directory.name) / filename, **kwargs)

    def test_scope_recent_and_lexical_recall_are_isolated(self) -> None:
        scope_a = MemoryStore.scope_for(1, 10)
        scope_b = MemoryStore.scope_for(1, 11)
        self.store.record_message(
            scope=scope_a,
            guild_id=1,
            channel_id=10,
            user_id=7,
            author_name="Alice",
            role="user",
            content="My deployment uses SQLite on Fedora",
            discord_message_id=101,
        )
        self.store.record_message(
            scope=scope_b,
            guild_id=1,
            channel_id=11,
            user_id=7,
            author_name="Alice",
            role="user",
            content="A secret deployment uses Redis",
            discord_message_id=102,
        )

        recent = self.store.recent_messages(scope_a, 10)
        self.assertEqual([item.content for item in recent], ["My deployment uses SQLite on Fedora"])
        recall = self.store.recall_messages(
            scope=scope_a,
            user_id=7,
            query="What database is in my Fedora deployment?",
            limit=4,
            candidates=50,
        )
        self.assertEqual(len(recall), 1)
        self.assertIn("SQLite", recall[0][0].content)
        self.assertNotIn("Redis", recall[0][0].content)

    def test_storage_history_limits_are_enforced_at_write_time(self) -> None:
        with self.subTest(limit="per-scope messages and per-user memories"):
            self._replace_store(
                "bounded.db",
                max_messages_per_scope=3,
                max_memories_per_user=2,
            )
            scope = MemoryStore.scope_for(1, 10)
            for index in range(9):
                self.store.record_message(
                    scope=scope,
                    guild_id=1,
                    channel_id=10,
                    user_id=7,
                    author_name="Alice",
                    role="user",
                    content=f"message {index}",
                )
            for index in range(7):
                self.store.add_memory(guild_id=1, user_id=7, text=f"fact {index}")

            self.assertEqual(
                [item.content for item in self.store.recent_messages(scope, 20)],
                ["message 6", "message 7", "message 8"],
            )
            self.assertEqual(len(self.store.list_memories(1, 7, limit=20)), 2)

        with self.subTest(limit="global messages and memories"):
            self._replace_store(
                "globally-bounded.db",
                max_messages_per_scope=10,
                max_memories_per_user=10,
                max_total_messages=4,
                max_total_memories=3,
            )
            scope_a = MemoryStore.scope_for(1, 10)
            scope_b = MemoryStore.scope_for(1, 11)
            for index in range(6):
                scope = scope_a if index % 2 == 0 else scope_b
                self.store.record_message(
                    scope=scope,
                    guild_id=1,
                    channel_id=10 if scope == scope_a else 11,
                    user_id=index + 1,
                    author_name=f"U{index}",
                    role="user",
                    content=f"global message {index}",
                )
            self.assertEqual(self.store.stats()["messages"], 4)
            remaining = self.store.recent_messages(scope_a, 10) + self.store.recent_messages(
                scope_b, 10
            )
            self.assertNotIn("global message 0", {item.content for item in remaining})
            self.assertNotIn("global message 1", {item.content for item in remaining})

            first = self.store.add_memory(guild_id=1, user_id=1, text="fact one")
            second = self.store.add_memory(guild_id=1, user_id=2, text="fact two")
            third = self.store.add_memory(guild_id=1, user_id=3, text="fact three")
            rejected = self.store.add_memory(guild_id=1, user_id=4, text="fact four")
            duplicate = self.store.add_memory(guild_id=1, user_id=1, text="FACT ONE")
            self.assertTrue(all(item is not None for item in (first, second, third)))
            self.assertIsNone(rejected)
            self.assertEqual(duplicate, first)
            self.assertEqual(self.store.stats()["memories"], 3)

    def test_model_outcome_diagnostic_retention_is_bounded(self) -> None:
        self._replace_store("model-outcomes.db", max_model_outcomes=3)
        for index in range(5):
            self.store.record_model_outcome(
                model=f"model-{index}",
                task="chat",
                worker_id=f"worker-{index}",
                worker_name=f"Worker {index}",
                success=index % 2 == 0,
                latency_seconds=1.0 + index,
                error_kind="transport" if index % 2 else "",
                truncated=False,
                malformed=False,
                created_at=1000 + index,
            )
        history = self.store.model_outcome_history(limit=10)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0].model, "model-4")
        self.assertEqual(history[-1].model, "model-2")
        self.assertEqual(self.store.stats()["model_outcomes"], 3)

    def test_explicit_memory_is_deduplicated_owned_and_relevance_filtered(self) -> None:
        with self.subTest(contract="deduplication and ownership"):
            first = self.store.add_memory(guild_id=1, user_id=7, text="I prefer Python")
            second = self.store.add_memory(guild_id=1, user_id=7, text="  i PREFER python  ")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            listed = self.store.list_memories(1, 7)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].text.casefold(), "i prefer python")
            self.assertFalse(self.store.delete_memory(1, 8, listed[0].id))
            self.assertTrue(self.store.delete_memory(1, 7, listed[0].id))

        with self.subTest(contract="lexical relevance"):
            self._replace_store("lexical-search.db")
            self.store.add_memory(
                guild_id=1,
                user_id=7,
                text="My deployment database is SQLite",
            )
            self.assertEqual(
                self.store.search_memories(guild_id=1, user_id=7, query="hi"),
                [],
            )
            matches = self.store.search_memories(
                guild_id=1,
                user_id=7,
                query="Which database does my deployment use?",
            )
            self.assertEqual(len(matches), 1)
            self.assertIn("SQLite", matches[0].text)

    def test_privacy_deletion_invalidates_summaries(self) -> None:
        scope = MemoryStore.scope_for(1, 10)
        row_id = self.store.record_message(
            scope=scope,
            guild_id=1,
            channel_id=10,
            user_id=7,
            author_name="Alice",
            role="user",
            content="remember this",
        )
        self.store.record_message(
            scope=scope,
            guild_id=1,
            channel_id=10,
            user_id=8,
            author_name="Bob",
            role="user",
            content="keep this recent",
        )
        self.store.add_memory(guild_id=1, user_id=7, text="A fact", source_message_id=row_id)
        batch = self.store.compaction_batch(scope=scope, keep_recent=1)
        self.assertIsNotNone(batch)
        assert batch is not None
        self.store.save_summary(batch, "A summary containing Alice")
        self.assertTrue(self.store.get_summary(scope))

        removed = self.store.delete_conversation_memory(1, 7)
        self.assertEqual(removed["memories"], 1)
        self.assertEqual(removed["summaries"], 1)
        self.assertEqual(self.store.get_summary(scope), "")
        self.store.set_opted_out(1, 7, True)
        self.assertTrue(self.store.is_opted_out(1, 7))

    def test_compaction_and_pruning_keep_bounds(self) -> None:
        scope = MemoryStore.scope_for(1, 10)
        for index in range(12):
            self.store.record_message(
                scope=scope,
                guild_id=1,
                channel_id=10,
                user_id=index % 2 + 1,
                author_name=f"U{index % 2}",
                role="user",
                content=f"message {index} sqlite",
                created_at=int(time.time()) + index,
            )
        batch = self.store.compaction_batch(scope=scope, keep_recent=4)
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual(len(batch.messages), 8)
        self.store.save_summary(batch, "Compacted context")
        self.assertEqual(len(self.store.recent_messages(scope, 50)), 4)

        for index in range(10):
            self.store.add_memory(guild_id=1, user_id=1, text=f"fact {index}")
        removed = self.store.prune(max_messages_per_channel=3, max_memories_per_user=5)
        self.assertEqual(len(self.store.recent_messages(scope, 50)), 3)
        self.assertEqual(len(self.store.list_memories(1, 1, limit=50)), 5)
        self.assertGreaterEqual(removed["messages"], 1)
        self.assertGreaterEqual(removed["memories"], 1)

    def test_generated_summary_validation_is_lossless(self) -> None:
        safe_summary = ("A long but safe continuity detail. " * 45).strip()
        self.assertGreater(len(safe_summary), 1200)
        cases = (
            {
                "name": "unsafe summary is rejected without replacing messages",
                "summary": ("safe context " * 120) + "Cookie: sid=x",
                "accepted": False,
            },
            {
                "name": "long safe summary round-trips without truncation",
                "summary": safe_summary,
                "accepted": True,
            },
        )

        for index, case in enumerate(cases):
            with self.subTest(case=case["name"]):
                self._replace_store(f"summary-{index}.db")
                scope = MemoryStore.scope_for(1, 10)
                for message_index in range(4):
                    self.store.record_message(
                        scope=scope,
                        guild_id=1,
                        channel_id=10,
                        user_id=7,
                        author_name="Alice",
                        role="user",
                        content=f"message {message_index}",
                    )
                batch = self.store.compaction_batch(scope=scope, keep_recent=1)
                assert batch is not None
                before = self.store.recent_messages(scope, 10)

                self.store.save_summary(batch, str(case["summary"]))

                if case["accepted"]:
                    self.assertEqual(self.store.get_summary(scope), case["summary"])
                    self.assertEqual(len(self.store.recent_messages(scope, 10)), 1)
                else:
                    self.assertEqual(self.store.get_summary(scope), "")
                    self.assertEqual(self.store.recent_messages(scope, 10), before)

    def test_stats_count_users_with_only_memory_or_privacy_state(self) -> None:
        self.store.add_memory(guild_id=1, user_id=7, text="A durable preference")
        self.store.set_opted_out(1, 8, True)
        self.assertEqual(self.store.stats()["users"], 2)

    def test_channel_configuration_and_proactive_state(self) -> None:
        self.store.seed_channel_config(1, 10, auto_reply=False, proactive=True)
        self.store.seed_channel_config(1, 10, auto_reply=True, proactive=False)
        config = self.store.get_channel_config(1, 10)
        self.assertFalse(config.auto_reply)
        self.assertTrue(config.proactive)
        self.store.set_channel_config(1, 10, auto_reply=True, proactive=False)
        config = self.store.get_channel_config(1, 10)
        self.assertTrue(config.auto_reply)
        self.assertFalse(config.proactive)

        self.store.mark_proactive(1, 10, "2026-01-01", 100)
        self.store.mark_proactive(1, 10, "2026-01-01", 200)
        self.assertEqual(self.store.proactive_state(1, 10), (200, "2026-01-01", 2))

if __name__ == "__main__":
    unittest.main()
