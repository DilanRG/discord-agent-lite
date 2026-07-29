from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbot.app import configure_logging
from agentbot.settings import ConfigError
from tests.support import loaded_settings


class SettingsTests(unittest.TestCase):
    def test_discord_library_debug_payloads_stay_disabled_at_app_debug_level(self) -> None:
        logger_names = ("discord", "discord.gateway", "discord.http", "aiohttp.access")
        previous_levels = {
            name: logging.getLogger(name).level for name in logger_names
        }
        root_logger = logging.getLogger()
        previous_root_level = root_logger.level
        try:
            root_logger.setLevel(logging.DEBUG)
            logging.getLogger("discord.gateway").setLevel(logging.NOTSET)
            with tempfile.TemporaryDirectory() as directory:
                with loaded_settings(Path(directory), LOG_LEVEL="DEBUG") as settings:
                    with (
                        patch("agentbot.app.logging.basicConfig"),
                        patch(
                            "agentbot.app.logging.handlers.RotatingFileHandler",
                            return_value=logging.NullHandler(),
                        ),
                    ):
                        configure_logging(settings)

            self.assertEqual(logging.getLogger("discord").level, logging.INFO)
            self.assertFalse(logging.getLogger("discord.gateway").isEnabledFor(logging.DEBUG))
            self.assertEqual(logging.getLogger("discord.http").level, logging.WARNING)
            self.assertEqual(logging.getLogger("aiohttp.access").level, logging.WARNING)

            with tempfile.TemporaryDirectory() as directory:
                with loaded_settings(Path(directory), LOG_LEVEL="ERROR") as settings:
                    with (
                        patch("agentbot.app.logging.basicConfig"),
                        patch(
                            "agentbot.app.logging.handlers.RotatingFileHandler",
                            return_value=logging.NullHandler(),
                        ),
                    ):
                        configure_logging(settings)

            self.assertEqual(logging.getLogger("discord").level, logging.ERROR)
            self.assertEqual(logging.getLogger("discord.http").level, logging.ERROR)
            self.assertEqual(logging.getLogger("aiohttp.access").level, logging.ERROR)
        finally:
            root_logger.setLevel(previous_root_level)
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)

    def test_rejects_invalid_setting_relationships(self) -> None:
        invalid_cases = {
            "provider output exceeds context budget": {
                "PROVIDER_CONTEXT_TOKENS": "2048",
                "PROVIDER_MAX_TOKENS": "1200",
            },
            "pending queue smaller than concurrency": {
                "GLOBAL_CONCURRENCY": "4",
                "MAX_PENDING_REQUESTS": "2",
            },
            "non-positive auto-reply channel": {"AUTO_REPLY_CHANNELS": "0"},
            "reflection interval exceeds retained events": {
                "RELATIONSHIP_REFLECT_EVERY": "8",
                "RELATIONSHIP_REFLECT_MAX_EVENTS": "6",
            },
            "retained events exceed pending interactions": {
                "RELATIONSHIP_REFLECT_MAX_EVENTS": "10",
                "MAX_PENDING_INTERACTIONS_PER_USER": "8",
            },
            "profile context exceeds per-user facts": {
                "PROFILE_CONTEXT_FACTS": "12",
                "MAX_PROFILE_FACTS_PER_USER": "8",
            },
            "journal context exceeds per-user entries": {
                "JOURNAL_CONTEXT_ENTRIES": "5",
                "MAX_JOURNAL_ENTRIES_PER_USER": "4",
            },
            "per-user facts exceed total facts": {
                "MAX_PROFILE_FACTS_PER_USER": "100",
                "MAX_TOTAL_PROFILE_FACTS": "99",
            },
            "non-Horde provider": {"LLM_PROVIDER": "openai_compatible"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case, overrides in invalid_cases.items():
                with self.subTest(case=case), self.assertRaises(ConfigError):
                    with loaded_settings(root, **overrides):
                        pass

    def test_explicit_overrides_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with loaded_settings(
                Path(directory),
                HORDE_API_KEY="text-shared-key",
                ALCHEMIST_API_KEY="0000000000",
            ) as settings:
                self.assertEqual(settings.horde_api_key, "text-shared-key")
                self.assertEqual(settings.alchemist_api_key, "0000000000")

    def test_alchemist_does_not_inherit_a_text_shared_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with loaded_settings(
                Path(directory),
                HORDE_API_KEY="text-shared-key",
            ) as settings:
                self.assertEqual(settings.horde_api_key, "text-shared-key")
                self.assertEqual(settings.alchemist_api_key, "0000000000")

            with loaded_settings(
                Path(directory),
                HORDE_API_KEY="text-shared-key",
                ALCHEMIST_API_KEY="registered-user-key",
            ) as settings:
                self.assertEqual(settings.horde_api_key, "text-shared-key")
                self.assertEqual(settings.alchemist_api_key, "registered-user-key")

    def test_defaults_are_bounded_and_lean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with loaded_settings(
                Path(directory),
                SOCIAL_NAMESPACE="must-be-ignored",
                HORDE_MODELS="manual/model,another/model",
            ) as settings:
                with self.subTest(contract="social memory"):
                    self.assertTrue(settings.relationships_enabled)
                    self.assertTrue(settings.relationship_direct_only)
                    self.assertFalse(hasattr(settings, "social_namespace"))
                    self.assertGreaterEqual(settings.relationship_meaningful_chars, 80)
                    self.assertGreaterEqual(
                        settings.relationship_meaningful_event_threshold, 1
                    )
                    self.assertLessEqual(
                        settings.relationship_reflect_every,
                        settings.relationship_reflect_max_events,
                    )
                    self.assertLessEqual(
                        settings.relationship_reflect_max_events,
                        settings.max_pending_interactions_per_user,
                    )
                    self.assertLessEqual(
                        settings.profile_context_facts,
                        settings.max_profile_facts_per_user,
                    )
                    self.assertLessEqual(
                        settings.journal_context_entries,
                        settings.max_journal_entries_per_user,
                    )
                    self.assertGreaterEqual(
                        settings.max_total_relationships,
                        settings.max_pending_requests,
                    )

                with self.subTest(contract="adaptive Horde routing"):
                    self.assertFalse(hasattr(settings, "horde_models"))
                    self.assertGreaterEqual(settings.horde_min_model_parameters_bn, 7)
                    self.assertGreaterEqual(
                        settings.horde_router_metadata_ttl_seconds, 60
                    )
                    self.assertLessEqual(
                        settings.horde_router_metadata_ttl_seconds, 120
                    )
                    self.assertEqual(settings.horde_router_sticky_seconds, 1800)
                    self.assertGreater(settings.horde_router_max_scopes, 0)
                    self.assertFalse(hasattr(settings, "provider"))
                    self.assertEqual(settings.provider_context_tokens, 8192)
                    self.assertLessEqual(
                        settings.relationship_context_tokens,
                        settings.provider_context_tokens,
                    )

                with self.subTest(contract="abuse controls"):
                    self.assertGreaterEqual(
                        settings.max_pending_requests,
                        settings.global_concurrency,
                    )
                    self.assertGreater(settings.command_rate_requests, 0)
                    self.assertGreater(
                        settings.tracking_channel_messages,
                        settings.tracking_user_messages,
                    )
                    self.assertGreater(
                        settings.max_total_messages,
                        settings.max_messages_per_channel,
                    )
                    self.assertGreater(
                        settings.max_total_memories,
                        settings.max_memories_per_user,
                    )

                with self.subTest(contract="legacy-paced proactivity"):
                    self.assertEqual(settings.proactive_interval_seconds, 3_600)
                    self.assertEqual(settings.proactive_min_idle_seconds, 43_200)
                    self.assertEqual(settings.proactive_cooldown_seconds, 43_200)

                with self.subTest(contract="lightweight attachments"):
                    self.assertLessEqual(settings.attachment_concurrency, 2)
                    self.assertLessEqual(settings.attachment_max_count, 3)
                    self.assertGreater(settings.attachment_max_extracted_chars, 0)
                    self.assertGreater(settings.attachment_max_pixels, 0)
                    self.assertEqual(settings.attachment_timeout_seconds, 60.0)
                    self.assertTrue(settings.alchemist_enabled)

    def test_removed_legacy_knobs_are_ignored_and_not_exposed(self) -> None:
        removed_fields = """
            horde_router_max_metrics group_context_tokens summary_context_tokens
            attachment_max_pages attachment_max_archive_entries
            attachment_max_archive_uncompressed_bytes attachment_cache_entries
            attachment_max_chunks attachment_max_chunks_per_file attachment_chunk_chars
            attachment_chunk_overlap attachment_retrieval_chunks attachment_retrieval_chars
            group_continuity_enabled group_reflect_every group_meaningful_event_threshold
            group_reflect_min_seconds group_reflect_max_events group_context_entries
            max_group_events_per_guild max_total_group_events max_group_journal_per_guild
            max_total_group_journal max_group_continuities max_group_members_per_guild
            max_interaction_metrics summary_enabled summary_after_messages
            summary_keep_messages summary_max_chars proactive_max_idle_seconds
            proactive_lookback_seconds proactive_min_human_messages
            proactive_min_unique_users proactive_max_per_sweep proactive_quiet_start_hour
            proactive_quiet_end_hour bot_interaction_channels bot_reply_requests
            bot_reply_period_seconds
        """.split()
        removed = {field.upper(): "invalid" for field in removed_fields}
        with tempfile.TemporaryDirectory() as directory:
            with loaded_settings(Path(directory), **removed) as settings:
                for field in removed_fields:
                    with self.subTest(field=field):
                        self.assertFalse(hasattr(settings, field))


if __name__ == "__main__":
    unittest.main()
