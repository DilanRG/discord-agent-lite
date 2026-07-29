#!/usr/bin/env python3
"""Measure the character + SQLite baseline without Discord or network I/O."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def rss_mib() -> float:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def main() -> None:
    startup_rss = rss_mib()
    sitecustomize_loaded = "sitecustomize" in sys.modules

    # Keep these imports inside main so the incremental cost can be reported
    # even in instrumented Python environments with a large startup footprint.
    from agentbot.character import load_character
    from agentbot.memory import MemoryStore

    with tempfile.TemporaryDirectory() as directory:
        character_path = Path(directory) / "resource-character.json"
        character_path.write_text(
            json.dumps(
                {
                    "name": "ResourceProbe",
                    "system_prompt": "Reply concisely as the configured character.",
                }
            ),
            encoding="utf-8",
        )
        character = load_character(character_path)
        memory = MemoryStore(Path(directory) / "bench.db")
        try:
            measured_rss = rss_mib()
            print(f"Character: {character.name}")
            print(f"Python startup RSS: {startup_rss:.1f} MiB")
            print(f"Process RSS after character + SQLite: {measured_rss:.1f} MiB")
            print(f"Increment over Python startup: {max(0.0, measured_rss - startup_rss):.1f} MiB")
            print("This excludes discord.py imports, the live gateway, aiohttp, and TLS buffers.")
            if sitecustomize_loaded:
                print(
                    "Note: sitecustomize was loaded, so host instrumentation or global startup "
                    "packages are included. Run with Python -S for an isolated core baseline."
                )
        finally:
            memory.close()


if __name__ == "__main__":
    main()
