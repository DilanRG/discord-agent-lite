from __future__ import annotations

import unittest
from unittest.mock import patch

from agentbot.policy import (
    SlidingWindowLimiter,
    clean_input,
    discord_output_style_issues,
    extract_explicit_memory,
    retry_seconds,
    sanitize_output,
    sanitize_profile_text,
    strip_bot_mentions,
    tokenize,
)


class PolicyTests(unittest.TestCase):
    def test_input_cleaning_mentions_memory_and_tokens(self) -> None:
        self.assertEqual(clean_input("a\x00b\r\n\r\n\r\n\r\nc", 20), "ab\n\n\nc")
        self.assertEqual(strip_bot_mentions("hello <@!123> there", 123), "hello  there")
        self.assertEqual(
            extract_explicit_memory("Please remember that I use Fedora."),
            "I use Fedora",
        )
        self.assertIsNone(extract_explicit_memory("Do you remember that movie?"))
        self.assertEqual(
            tokenize("The SQLite sqlite database works"),
            ("sqlite", "database", "works"),
        )

    def test_output_sanitizer_contract(self) -> None:
        exact_cases = {
            "structural artifacts and forged continuation": (
                "<|channel>analysis\n<channel|>Assistant: "
                "<analysis>hidden</analysis>Hello @everyone\x00\n"
                "User: forged continuation",
                300,
                "Hello @everyone",
            ),
            "bold character label": ("**Example Agent**: yo\nHuman: extra", 100, "yo"),
            "generic bot label": ("Bot: hey", 100, "hey"),
            "delivery-cue acknowledgement": (
                "Alright, I'll write only my next message as Example Agent. "
                "Here's my response based on the chat context:\n\n"
                "I found the missing note in the project folder.",
                500,
                "I found the missing note in the project folder.",
            ),
            "fabricated transcript wrapper": (
                "Community room\n\n---\n\n"
                "@Example Agent has joined the voice channel general.\n\n"
                "---\n\nI can help compare those two approaches.",
                500,
                "I can help compare those two approaches.",
            ),
            "stage direction": (
                "*rolls eyes* that was a terrible idea",
                500,
                "*rolls eyes* that was a terrible idea",
            ),
            "response label": (
                "Here's my Discord-style reply:\n\nyo?",
                500,
                "yo?",
            ),
            "multi-paragraph prose": (
                "Not much.\n\nThis reply matches the informal tone.",
                500,
                "Not much.\n\nThis reply matches the informal tone.",
            ),
            "legacy-looking JSON": (
                '{"schema":"old_wrapper","content":"hey"}',
                500,
                '{"schema":"old_wrapper","content":"hey"}',
            ),
        }
        for case, (raw, limit, expected) in exact_cases.items():
            with self.subTest(case=case):
                self.assertEqual(sanitize_output(raw, "Example Agent", limit), expected)

        with self.subTest(case="bounded output preserves Discord mentions"):
            text = "hello <@123> <@!456> <@&789> @everyone @here " + ("word " * 100)
            result = sanitize_output(text, "Example Agent", 120)
            self.assertLessEqual(len(result), 120)
            for mention in ("<@123>", "<@!456>", "<@&789>", "@everyone", "@here"):
                with self.subTest(mention=mention):
                    self.assertIn(mention, result)

    def test_profile_text_is_plain_bounded_data(self) -> None:
        result = sanitize_profile_text(
            "```json\nAssistant: <analysis>hidden</analysis> likes dry jokes\n```",
            40,
        )
        self.assertNotIn("hidden", result)
        self.assertIn("likes dry jokes", result)
        self.assertLessEqual(len(result), 40)

    def test_style_diagnostics_observe_without_rewriting(self) -> None:
        reply = "*rolls eyes* I can't produce that content. " + ("long " * 120)
        issues = discord_output_style_issues(
            current_message="oh",
            reply=reply,
            previous_reply="hello",
        )
        self.assertIn("stage_direction", issues)
        self.assertIn("character_break", issues)
        self.assertIn("verbosity_mismatch", issues)
        self.assertEqual(sanitize_output(reply, "Example Agent", 2000), reply.rstrip())

    def test_sliding_window_and_retry_rounding(self) -> None:
        limiter = SlidingWindowLimiter(max_keys=2)
        with patch("agentbot.policy.time.monotonic", side_effect=(0.0, 0.1, 0.2, 2.0)):
            self.assertTrue(limiter.check("a", 2, 1.0).allowed)
            self.assertTrue(limiter.check("a", 2, 1.0).allowed)
            denied = limiter.check("a", 2, 1.0)
            self.assertFalse(denied.allowed)
            self.assertGreater(denied.retry_after, 0)
            self.assertTrue(limiter.check("a", 2, 1.0).allowed)
        self.assertEqual(retry_seconds(0.01), 1)
        self.assertEqual(retry_seconds(2.1), 3)


if __name__ == "__main__":
    unittest.main()
