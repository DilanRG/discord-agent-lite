from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from discord import app_commands
from discord.ext import commands

from agentbot.app import AgentBot
from agentbot.commands import AgentCommands
from agentbot.orchestrator import AgentCore


ROOT = Path(__file__).resolve().parents[1]


class ShutdownLifecycleTests(unittest.TestCase):
    def test_sigint_exits_when_agent_cleanup_stalls(self) -> None:
        probe = textwrap.dedent(
            """
            import asyncio
            import _thread
            import os
            import signal
            import threading

            import discord
            from discord.ext import commands

            from agentbot.app import AgentBot


            class StalledCore:
                async def close(self) -> None:
                    await asyncio.Event().wait()


            class Memory:
                def close(self) -> None:
                    return None


            class ProbeBot(AgentBot):
                def __init__(self) -> None:
                    commands.Bot.__init__(
                        self,
                        command_prefix="!",
                        intents=discord.Intents.none(),
                    )
                    self._closed_resources = False
                    self.core = StalledCore()
                    self.session = None
                    self.memory = Memory()
                    self._discord_close_grace_seconds = 0.05
                    self._core_close_grace_seconds = 0.05
                    self._session_close_grace_seconds = 0.05

                async def start(self, token: str, *, reconnect: bool = True) -> None:
                    del token, reconnect
                    print("probe_ready", flush=True)
                    await asyncio.Event().wait()


            bot = ProbeBot()
            timer = threading.Timer(
                0.2,
                (
                    _thread.interrupt_main
                    if os.name == "nt"
                    else lambda: os.kill(os.getpid(), signal.SIGINT)
                ),
            )
            timer.daemon = True
            timer.start()
            bot.run("probe-token", log_handler=None)
            print("probe_exited", flush=True)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            self.fail(
                "SIGINT did not stop the bot within four seconds; "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("probe_ready", stdout)
        self.assertIn("probe_exited", stdout)

    def test_cancellation_resistant_work_hits_controlled_process_watchdog(self) -> None:
        probe = textwrap.dedent(
            """
            import asyncio
            import _thread
            import os
            import signal
            import threading

            import discord
            from discord.ext import commands

            from agentbot.app import AgentBot


            async def resist_cancellation() -> None:
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        continue


            class ResistantCore:
                async def close(self) -> None:
                    task = asyncio.create_task(
                        resist_cancellation(),
                        name="cancellation-resistant-probe",
                    )
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


            class Memory:
                def close(self) -> None:
                    return None


            class ProbeBot(AgentBot):
                def __init__(self) -> None:
                    commands.Bot.__init__(
                        self,
                        command_prefix="!",
                        intents=discord.Intents.none(),
                    )
                    self._closed_resources = False
                    self.core = ResistantCore()
                    self.session = None
                    self.memory = Memory()
                    self._discord_close_grace_seconds = 0.05
                    self._core_close_grace_seconds = 0.05
                    self._session_close_grace_seconds = 0.05
                    # The budget test below covers the production 15s/20s contract.
                    self._process_exit_grace_seconds = 0.5

                async def start(self, token: str, *, reconnect: bool = True) -> None:
                    del token, reconnect
                    print("resistant_probe_ready", flush=True)
                    timer = threading.Timer(
                        0.2,
                        (
                            _thread.interrupt_main
                            if os.name == "nt"
                            else lambda: os.kill(os.getpid(), signal.SIGINT)
                        ),
                    )
                    timer.daemon = True
                    timer.start()
                    await asyncio.Event().wait()


            ProbeBot().run("probe-token", log_handler=None)
            """
        )
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            self.fail(
                "The controlled shutdown watchdog did not force process exit; "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 1, stderr)
        self.assertIn("resistant_probe_ready", stdout)
        self.assertIn("Shutdown step agent-core exceeded", stderr)
        self.assertLess(elapsed, 5.0)

    def test_watchdog_forces_exit_when_stderr_pipe_is_full(self) -> None:
        probe = textwrap.dedent(
            """
            import os
            import time

            from agentbot.app import AgentBot


            os.set_blocking(2, False)
            while True:
                try:
                    os.write(2, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(2, True)
            print("stderr_full", flush=True)

            bot = object.__new__(AgentBot)
            bot._process_watchdog_enabled = True
            bot._shutdown_watchdog_event = None
            bot._shutdown_watchdog_thread = None
            bot._process_exit_grace_seconds = 0.2
            bot._arm_shutdown_watchdog()
            time.sleep(10)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _stderr = process.communicate(timeout=2)
            self.fail(
                "The shutdown watchdog blocked on a full stderr pipe; "
                f"stdout={stdout!r}"
            )
        stdout, stderr = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 1, stderr[-1_000:])
        self.assertIn("stderr_full", stdout)

    def test_systemd_stop_budget_exceeds_runtime_cleanup_budget(self) -> None:
        from agentbot.app import (
            _CORE_CLOSE_GRACE_SECONDS,
            _DISCORD_CLOSE_GRACE_SECONDS,
            _EVENT_CLOSE_GRACE_SECONDS,
            _MEMORY_CLOSE_EXPECTED_SECONDS,
            _PROCESS_EXIT_GRACE_SECONDS,
            _SESSION_CLOSE_GRACE_SECONDS,
        )

        unit = (ROOT / "deploy" / "discord-agent-lite.service").read_text(
            encoding="utf-8"
        )
        timeout_line = next(
            line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")
        )
        systemd_seconds = float(timeout_line.partition("=")[2])
        runtime_seconds = (
            _EVENT_CLOSE_GRACE_SECONDS
            + _DISCORD_CLOSE_GRACE_SECONDS
            + _CORE_CLOSE_GRACE_SECONDS
            + _SESSION_CLOSE_GRACE_SECONDS
            + _MEMORY_CLOSE_EXPECTED_SECONDS
        )
        self.assertLessEqual(runtime_seconds, _PROCESS_EXIT_GRACE_SECONDS)
        self.assertGreaterEqual(systemd_seconds - _PROCESS_EXIT_GRACE_SECONDS, 5.0)


class _FakeLoop:
    def is_running(self) -> bool:
        return False

    def get_task(self):
        return None


class ConcurrentCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_close_callers_share_one_ordered_cleanup(self) -> None:
        import asyncio

        events: list[str] = []

        class Core:
            async def close(self) -> None:
                events.append("core")
                await asyncio.sleep(0)

        class Session:
            closed = False

            async def close(self) -> None:
                events.append("session")
                self.closed = True

        class Memory:
            def close(self) -> None:
                events.append("memory")

        async def discord_close(_self) -> None:
            events.append("discord")
            await asyncio.sleep(0)

        bot = object.__new__(AgentBot)
        bot._closed_resources = False
        bot._shutdown_started = False
        bot._shutdown_task = None
        bot._active_event_tasks = set()
        bot._process_watchdog_enabled = False
        bot._discord_close_grace_seconds = 0.05
        bot._core_close_grace_seconds = 0.05
        bot._session_close_grace_seconds = 0.05
        bot._event_close_grace_seconds = 0.05
        bot.core = Core()
        bot.session = Session()
        bot.memory = Memory()
        with (
            patch.object(commands.Bot, "close", new=discord_close),
            patch.object(AgentBot, "proactive_loop", new=_FakeLoop()),
            patch.object(AgentBot, "maintenance_loop", new=_FakeLoop()),
        ):
            await asyncio.gather(bot.close(), bot.close())
            await bot.close()
        self.assertEqual(events, ["discord", "core", "session", "memory"])
        self.assertTrue(bot._closed_resources)

    async def test_cancelled_first_close_caller_does_not_cancel_shared_cleanup(self) -> None:
        import asyncio

        events: list[str] = []
        discord_started = asyncio.Event()
        release_discord = asyncio.Event()

        class Core:
            async def close(self) -> None:
                events.append("core")

        class Session:
            closed = False

            async def close(self) -> None:
                events.append("session")
                self.closed = True

        class Memory:
            def close(self) -> None:
                events.append("memory")

        async def discord_close(_self) -> None:
            events.append("discord-start")
            discord_started.set()
            await release_discord.wait()
            events.append("discord-finish")

        bot = object.__new__(AgentBot)
        bot._closed_resources = False
        bot._shutdown_started = False
        bot._shutdown_task = None
        bot._active_event_tasks = set()
        bot._process_watchdog_enabled = False
        bot._discord_close_grace_seconds = 0.5
        bot._core_close_grace_seconds = 0.05
        bot._session_close_grace_seconds = 0.05
        bot._event_close_grace_seconds = 0.05
        bot.core = Core()
        bot.session = Session()
        bot.memory = Memory()
        with (
            patch.object(commands.Bot, "close", new=discord_close),
            patch.object(AgentBot, "proactive_loop", new=_FakeLoop()),
            patch.object(AgentBot, "maintenance_loop", new=_FakeLoop()),
        ):
            first_caller = asyncio.create_task(bot.close())
            await asyncio.wait_for(discord_started.wait(), timeout=0.2)
            first_caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_caller
            self.assertIsNotNone(bot._shutdown_task)
            self.assertFalse(bot._shutdown_task.done())
            release_discord.set()
            await bot.close()

        self.assertEqual(
            events,
            ["discord-start", "discord-finish", "core", "session", "memory"],
        )
        self.assertTrue(bot._closed_resources)

    async def test_active_slash_command_drains_before_session_and_memory_close(self) -> None:
        import asyncio

        events: list[str] = []
        command_started = asyncio.Event()

        class Core:
            async def close(self) -> None:
                events.append("core")

        class Session:
            closed = False

            async def close(self) -> None:
                events.append("session")
                self.closed = True

        class Memory:
            def close(self) -> None:
                events.append("memory")

        class Limiter:
            def check(self, *_args, **_kwargs):
                return SimpleNamespace(allowed=True, retry_after=0.0)

        async def discord_close(_self) -> None:
            events.append("discord")

        bot = object.__new__(AgentBot)
        bot._closed_resources = False
        bot._shutdown_started = False
        bot._shutdown_task = None
        bot._active_event_tasks = set()
        bot._process_watchdog_enabled = False
        bot._discord_close_grace_seconds = 0.05
        bot._core_close_grace_seconds = 0.05
        bot._session_close_grace_seconds = 0.05
        bot._event_close_grace_seconds = 0.05
        bot.settings = SimpleNamespace(
            blacklisted_users=frozenset(),
            command_rate_requests=5,
            command_rate_period_seconds=60,
        )
        bot.command_limiter = Limiter()
        bot.core = Core()
        bot.session = Session()
        bot.memory = Memory()
        cog = AgentCommands(bot)
        interaction = SimpleNamespace(
            command=SimpleNamespace(qualified_name="agent status"),
            guild_id=1,
            user=SimpleNamespace(id=2),
        )

        async def slash_command() -> None:
            await cog.interaction_check(interaction)
            events.append("command-start")
            command_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("command-finish")

        command_task = asyncio.create_task(
            slash_command(),
            name="CommandTree-invoker",
        )
        await asyncio.wait_for(command_started.wait(), timeout=0.2)
        try:
            with (
                patch.object(commands.Bot, "close", new=discord_close),
                patch.object(AgentBot, "proactive_loop", new=_FakeLoop()),
                patch.object(AgentBot, "maintenance_loop", new=_FakeLoop()),
            ):
                await bot.close()
            self.assertTrue(command_task.done())
            self.assertLess(events.index("command-finish"), events.index("session"))
            self.assertLess(events.index("command-finish"), events.index("memory"))
        finally:
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)

    async def test_slash_commands_are_rejected_after_shutdown_starts(self) -> None:
        class Limiter:
            def check(self, *_args, **_kwargs):
                return SimpleNamespace(allowed=True, retry_after=0.0)

        bot = object.__new__(AgentBot)
        bot._shutdown_started = True
        bot._active_event_tasks = set()
        bot.settings = SimpleNamespace(
            blacklisted_users=frozenset(),
            command_rate_requests=5,
            command_rate_period_seconds=60,
        )
        bot.command_limiter = Limiter()
        cog = AgentCommands(bot)
        interaction = SimpleNamespace(
            command=SimpleNamespace(qualified_name="agent status"),
            guild_id=1,
            user=SimpleNamespace(id=2),
        )

        with self.assertRaises(app_commands.CheckFailure):
            await cog.interaction_check(interaction)


class BackgroundShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_core_close_does_not_gather_forever(self) -> None:
        import asyncio

        release = asyncio.Event()

        async def cancellation_resistant_task() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(cancellation_resistant_task(), name="stalled-reflection")
        await asyncio.sleep(0)
        core = object.__new__(AgentCore)
        core._relationship_tasks = {(1, 2): task}
        core._relationship_retry_after = {}
        core._background_close_grace_seconds = 0.02
        try:
            await asyncio.wait_for(core.close(), timeout=0.2)
            self.assertFalse(task.done())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
