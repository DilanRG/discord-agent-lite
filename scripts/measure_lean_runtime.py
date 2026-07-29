#!/usr/bin/env python3
"""Measure the constructed lean runtime without Discord or Horde network I/O."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def rss_mib() -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            return counters.WorkingSetSize / (1024 * 1024)
        return 0.0

    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


async def measure(settle_seconds: float) -> list[float]:
    from agentbot.app import AgentBot, create_memory_store
    from agentbot.character import load_character
    from agentbot.settings import Settings

    with tempfile.TemporaryDirectory(prefix="discord-agent-lean-rss-") as directory:
        temporary_root = Path(directory)
        character_path = temporary_root / "resource-character.json"
        character_path.write_text(
            json.dumps(
                {
                    "name": "ResourceProbe",
                    "system_prompt": "Reply concisely as the configured character.",
                }
            ),
            encoding="utf-8",
        )
        environment = {
            "DISCORD_TOKEN": "offline-resource-probe",
            "CHARACTER_FILE": str(character_path),
            "DATABASE_PATH": str(temporary_root / "agent.db"),
            "LOG_PATH": str(temporary_root / "agent.log"),
            "LLM_PROVIDER": "horde",
            "HORDE_API_KEY": "0000000000",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.load(temporary_root / "missing.env")

        character = load_character(settings.character_file)
        memory = create_memory_store(settings)
        bot = AgentBot(settings, character, memory)
        bot._ready = asyncio.Event()

        async def offline_sync(*args: object, **kwargs: object) -> list[object]:
            del args, kwargs
            return []

        try:
            with (
                patch.object(bot.tree, "sync", side_effect=offline_sync),
                patch(
                    "aiohttp.ClientSession._request",
                    side_effect=AssertionError(
                        "The offline resource probe attempted external HTTP I/O"
                    ),
                ),
            ):
                await bot.setup_hook()
                assert bot.session is not None
                assert bot.provider is not None
                assert bot.alchemist_client is not None
                assert bot.attachment_processor is not None
                assert bot.core is not None
                assert bot.get_cog("AgentCommands") is not None
                assert bot.proactive_loop.is_running()
                assert bot.maintenance_loop.is_running()

                gc.collect()
                sample_count = max(1, round(settle_seconds * 10))
                samples: list[float] = []
                for _ in range(sample_count):
                    samples.append(rss_mib())
                    await asyncio.sleep(0.1)
                return samples
        finally:
            await bot.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--limit-mib", type=float, default=100.0)
    args = parser.parse_args()
    if not 0.1 <= args.settle_seconds <= 60.0:
        parser.error("--settle-seconds must be between 0.1 and 60")
    if args.limit_mib <= 0:
        parser.error("--limit-mib must be positive")

    samples = asyncio.run(measure(args.settle_seconds))
    if not samples or max(samples) <= 0:
        raise SystemExit("Could not measure process RSS on this platform")
    print("Offline lean-runtime steady-state RSS")
    print(f"samples={len(samples)}")
    print(f"minimum={min(samples):.1f} MiB")
    print(f"median={statistics.median(samples):.1f} MiB")
    print(f"maximum={max(samples):.1f} MiB")
    print("External Discord and Horde network I/O were not exercised.")
    if max(samples) >= args.limit_mib:
        raise SystemExit(
            f"Lean-runtime RSS reached {max(samples):.1f} MiB "
            f"(limit: {args.limit_mib:.1f} MiB)"
        )


if __name__ == "__main__":
    main()
