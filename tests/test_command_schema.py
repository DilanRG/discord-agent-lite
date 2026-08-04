from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from agentbot.commands import AgentCommands


class CommandSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = Path(__file__).resolve().parents[1] / "agentbot" / "commands.py"
        cls.tree = ast.parse(cls.path.read_text(encoding="utf-8"), filename=str(cls.path))

    @staticmethod
    def _literal_string(node: ast.AST | None) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _command_names(self) -> set[tuple[str, str]]:
        command_names: set[tuple[str, str]] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute) or func.attr != "command":
                    continue
                group = func.value.id if isinstance(func.value, ast.Name) else ""
                values = {kw.arg: self._literal_string(kw.value) for kw in decorator.keywords}
                name = values.get("name")
                if name:
                    command_names.add((group, name))
        return command_names

    def _handler_strings(self, name: str) -> str:
        handler = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        return " ".join(
            node.value
            for node in ast.walk(handler)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )

    def test_application_command_metadata_fits_discord_limits(self) -> None:
        command_names: set[tuple[str, str]] = set()
        groups: set[str] = set()
        choice_count = 0

        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "Group":
                    values = {kw.arg: self._literal_string(kw.value) for kw in call.keywords}
                    name = values.get("name")
                    description = values.get("description")
                    self.assertIsNotNone(name)
                    self.assertIsNotNone(description)
                    assert name is not None and description is not None
                    self.assertLessEqual(len(name), 32)
                    self.assertLessEqual(len(description), 100)
                    self.assertNotIn(name, groups)
                    groups.add(name)

            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if isinstance(func, ast.Attribute) and func.attr == "command":
                    group = func.value.id if isinstance(func.value, ast.Name) else ""
                    values = {kw.arg: self._literal_string(kw.value) for kw in decorator.keywords}
                    name = values.get("name")
                    description = values.get("description")
                    self.assertIsNotNone(name, node.name)
                    self.assertIsNotNone(description, node.name)
                    assert name is not None and description is not None
                    self.assertLessEqual(len(name), 32)
                    self.assertLessEqual(len(description), 100)
                    key = (group, name)
                    self.assertNotIn(key, command_names)
                    command_names.add(key)
                elif isinstance(func, ast.Attribute) and func.attr == "describe":
                    for keyword in decorator.keywords:
                        description = self._literal_string(keyword.value)
                        self.assertIsNotNone(description, f"{node.name}.{keyword.arg}")
                        assert description is not None
                        self.assertLessEqual(len(description), 100)

        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "Choice":
                values = {kw.arg: self._literal_string(kw.value) for kw in node.keywords}
                name = values.get("name")
                value = values.get("value")
                self.assertIsNotNone(name)
                self.assertIsNotNone(value)
                assert name is not None and value is not None
                self.assertLessEqual(len(name), 100)
                self.assertLessEqual(len(value), 100)
                choice_count += 1

        self.assertEqual(groups, {"agent", "memory", "profile"})
        self.assertLessEqual(choice_count, 25)
        self.assertGreaterEqual(len(command_names), 10)

    def test_all_slash_command_handlers_are_async(self) -> None:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            has_command_decorator = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
                for decorator in node.decorator_list
            )
            self.assertFalse(has_command_decorator, f"{node.name} must be async")

    def test_removed_command_surfaces_stay_absent(self) -> None:
        command_names = self._command_names()
        function_names: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_names.add(node.name)
        for name in ("add", "storage", "opt-out", "forget"):
            with self.subTest(surface=f"profile {name}"):
                self.assertNotIn(("profile_group", name), command_names)
        with self.subTest(surface="profile add handler"):
            self.assertNotIn("profile_add", function_names)

        prune = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "prune"
        )
        argument_names = [argument.arg for argument in prune.args.args]
        with self.subTest(surface="live prune vacuum"):
            self.assertNotIn("vacuum", argument_names)

    def test_memory_and_agent_continuity_controls_stay_distinct(self) -> None:
        privacy = self._handler_strings("privacy").casefold()
        self.assertIn(
            "conversation memory and agent-authored profile/journal continuity are separate",
            privacy,
        )
        self.assertIn("discord account id and available names", privacy)
        self.assertIn("profile and journal entries have no opt-out", privacy)

        storage = self._handler_strings("storage").casefold()
        self.assertIn("internal profile/journal continuity remains active", storage)

        forget = self._handler_strings("forget").casefold()
        self.assertIn("future conversation-memory storage was not changed", forget)
        self.assertIn("internal profile/journal continuity was not changed", forget)

        for handler_name in ("profile_delete", "profile_reset"):
            with self.subTest(handler=handler_name):
                response = self._handler_strings(handler_name).casefold()
                self.assertIn(
                    "later qualifying interactions may rebuild agent continuity",
                    response,
                )

    def test_private_profile_controls_include_relationship_state(self) -> None:
        profile_view = self._handler_strings("profile_view").casefold()
        self.assertIn("relationship state", profile_view)
        self.assertIn("conversational only", profile_view)

        profile_reset = self._handler_strings("profile_reset").casefold()
        self.assertIn("relationship state", profile_reset)


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_renders_compact_horde_selection_and_last_failure(self) -> None:
        stats = {
            "messages": 0,
            "memories": 0,
            "summaries": 0,
            "profile_facts": 0,
            "relationships": 0,
            "journal_entries": 0,
            "model_outcomes": 3,
            "pending_interactions": 0,
            "group_continuities": 0,
            "group_events": 0,
            "group_journal_entries": 0,
            "interaction_metrics": 0,
            "users": 0,
            "attachments": 0,
            "attachment_chunks": 0,
            "database_bytes": 0,
        }
        provider_status = {
            "provider": "horde",
            "routing": "adaptive",
            "metadata_age_seconds": 8,
            "eligible_candidate_count": 2,
            "selection_count": 1,
            "selected_models": [
                {
                    "task": "chat",
                    "model": "koboldcpp/Only-8B",
                    "format": "ChatML",
                    "selection_age_seconds": 42,
                    "idle_seconds": 7,
                }
            ],
            "recent_model_failures": [
                {
                    "task": "reflection",
                    "model": "koboldcpp/Only-8B",
                    "error_kind": "malformed",
                    "age_seconds": 12,
                }
            ],
        }

        class Response:
            async def defer(self, *, ephemeral: bool) -> None:
                self.ephemeral = ephemeral

        class Followup:
            embed = None

            async def send(self, *, embed, ephemeral: bool) -> None:  # type: ignore[no-untyped-def]
                self.embed = embed
                self.ephemeral = ephemeral

        interaction = SimpleNamespace(response=Response(), followup=Followup())
        bot = SimpleNamespace(
            memory=SimpleNamespace(stats=lambda: stats),
            provider=SimpleNamespace(status=lambda: provider_status),
            character=SimpleNamespace(name="Test Character"),
            attachment_processor=None,
        )

        cog = AgentCommands(bot)
        await AgentCommands.status.callback(cog, interaction)

        fields = {field.name: field.value for field in interaction.followup.embed.fields}
        self.assertNotIn("Rolling summaries", fields)
        self.assertNotIn("Guild continuity", fields)
        self.assertNotIn("Chat provider circuit", fields)
        self.assertEqual(
            fields["Adaptive routing"],
            "metadata=8s, eligible=2, active selections=1",
        )
        self.assertIn(
            "reflection: koboldcpp/Only-8B (malformed, 12s ago)",
            fields["Recent model failures"],
        )
        self.assertIn(
            "chat: koboldcpp/Only-8B (ChatML, selected 42s, idle 7s)",
            fields["Sticky model selections"],
        )


class ProfileCommandPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_callbacks_are_private_and_scoped_to_the_invoking_user(self) -> None:
        user_id = 71

        class Response:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def send_message(self, *args: object, **kwargs: object) -> None:
                self.calls.append(dict(kwargs))

        memory = MagicMock()
        memory.social_profile_counts.return_value = {
            "confirmed_facts": 0,
            "tentative_facts": 0,
            "inactive_facts": 0,
            "journal_entries": 0,
        }
        memory.list_profile_records.return_value = []
        memory.recent_journal_entries.return_value = []
        memory.relationship_state.return_value = SimpleNamespace(
            label="new, neutral", familiarity=0, interaction_count=0, dimensions={}, summary=""
        )
        memory.delete_social_record.return_value = True
        memory.reset_social_profile.return_value = {
            "profile_facts": 0,
            "relationships": 0,
            "journal_entries": 0,
            "pending_interactions": 0,
        }
        core = MagicMock()
        bot = SimpleNamespace(
            memory=memory,
            core=core,
            settings=SimpleNamespace(relationships_enabled=True),
        )
        cog = AgentCommands(bot)

        cases = (
            ("view", AgentCommands.profile_view.callback, (), "social_profile_counts"),
            ("delete", AgentCommands.profile_delete.callback, ("profile:3",), "delete_social_record"),
            ("reset", AgentCommands.profile_reset.callback, (True,), "reset_social_profile"),
        )
        for name, callback, args, memory_method in cases:
            with self.subTest(command=name):
                response = Response()
                interaction = SimpleNamespace(user=SimpleNamespace(id=user_id), response=response)
                await callback(cog, interaction, *args)
                self.assertEqual(len(response.calls), 1)
                call = response.calls[0]
                self.assertTrue(call["ephemeral"])
                allowed = call["allowed_mentions"]
                self.assertIsInstance(allowed, discord.AllowedMentions)
                self.assertFalse(allowed.everyone)
                self.assertFalse(allowed.users)
                self.assertFalse(allowed.roles)
                self.assertFalse(allowed.replied_user)
                self.assertEqual(
                    getattr(memory, memory_method).call_args.kwargs["user_id"],
                    user_id,
                )


if __name__ == "__main__":
    unittest.main()
