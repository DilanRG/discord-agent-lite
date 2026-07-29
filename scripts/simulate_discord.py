#!/usr/bin/env python3
"""Run Discord-like turns through the real AgentBot handler without logging in."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentbot.settings import Settings
from agentbot.simulator import DiscordTurnSimulator, ScriptedProvider, SimulatedAttachment


DEFAULT_SCENARIO = ROOT / "scripts" / "scenarios" / "discord_acceptance.json"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Simulate Discord messages, attachments, replies, memory writes, model calls, "
            "sanitization, and outbound sends without connecting to Discord."
        )
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--character", type=Path)
    parser.add_argument(
        "--live-provider",
        action="store_true",
        help="Use the configured Horde/OpenAI-compatible provider instead of scripted outputs.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Keep simulation state at this path; the default is a disposable temporary DB.",
    )
    parser.add_argument(
        "--enforce-rate-limits",
        action="store_true",
        help="Apply production wall-clock rate limits to back-to-back simulated turns.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable results.")
    parser.add_argument(
        "--turn-limit",
        type=int,
        default=0,
        help="Run only the first N scenario turns (0 runs all turns).",
    )
    return parser.parse_args()


def _load_scenario(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("turns"), list):
        raise ValueError("scenario must be an object containing a turns list")
    if not 1 <= len(payload["turns"]) <= 50:
        raise ValueError("scenario must contain between 1 and 50 turns")
    return payload


def _materialize_attachment(
    item: object,
    *,
    scenario_dir: Path,
    temporary_dir: Path,
    index: int,
) -> SimulatedAttachment:
    if not isinstance(item, dict):
        raise ValueError("each attachment must be an object")
    filename = str(item.get("filename") or f"attachment-{index}.txt")[:180]
    content_type = str(item.get("content_type") or "")[:120]
    declared_size_raw = item.get("declared_size")
    declared_size = int(declared_size_raw) if declared_size_raw is not None else None
    supplied = sum(key in item for key in ("path", "text", "base64"))
    if supplied != 1:
        raise ValueError("each attachment needs exactly one of path, text, or base64")
    if "path" in item:
        path = Path(str(item["path"]))
        if not path.is_absolute():
            path = scenario_dir / path
    else:
        path = temporary_dir / f"{index:03d}-{Path(filename).name}"
        if "text" in item:
            path.write_text(str(item["text"]), encoding="utf-8")
        else:
            path.write_bytes(base64.b64decode(str(item["base64"]), validate=True))
    if not path.is_file():
        raise ValueError(f"attachment path does not exist: {path}")
    return SimulatedAttachment(
        path=path,
        filename=filename,
        content_type=content_type,
        declared_size=declared_size,
    )


def _style_expectation_met(turn: dict[str, Any], issues: tuple[str, ...]) -> bool:
    expected_style_issues = turn.get("expected_style_issues")
    if isinstance(expected_style_issues, list):
        expected = tuple(sorted(str(value) for value in expected_style_issues))
        return tuple(sorted(issues)) == expected
    if "expect_style_pass" in turn:
        return (not issues) == bool(turn["expect_style_pass"])
    # Diagnostics are observations unless a scenario explicitly promotes one
    # to an assertion. This mirrors production, where style never gates output.
    return True


def _named_expectation_met(expected_value: object, actual_value: str) -> bool:
    expected = str(expected_value or "").strip()
    return not expected or expected.casefold() == actual_value.casefold()


async def _run(args: argparse.Namespace) -> int:
    if args.turn_limit < 0:
        raise ValueError("turn-limit cannot be negative")
    scenario_path = args.scenario.resolve()
    scenario = _load_scenario(scenario_path)
    scenario_purpose = str(
        scenario.get("purpose", "scripted_handler_plumbing_only")
    ).strip()
    turns = scenario["turns"][: args.turn_limit or None]
    os.environ.setdefault("DISCORD_TOKEN", "discord-simulation-only")
    settings = Settings.load(env_file=args.env_file)
    if args.character:
        settings = replace(settings, character_file=args.character.resolve())
    if not bool(scenario.get("enable_background_tasks", False)):
        # Acceptance scenarios are bounded turn tests. Compact continuity
        # reflection has its own suite and would consume an extra scripted response.
        settings = replace(
            settings,
            relationships_enabled=False,
        )

    with tempfile.TemporaryDirectory(prefix="discord-agent-simulation-") as directory:
        temporary_dir = Path(directory)
        database_path = args.database.resolve() if args.database else temporary_dir / "simulation.db"
        settings = replace(
            settings,
            database_path=database_path,
            log_path=temporary_dir / "simulation.log",
        )
        responses = [
            str(turn.get("provider_response", ""))
            for turn in turns
            if isinstance(turn, dict)
        ]
        if not args.live_provider and any(not response for response in responses):
            raise ValueError("every scripted turn needs a non-empty provider_response")
        provider = None if args.live_provider else ScriptedProvider(responses)
        simulator = await DiscordTurnSimulator.create(
            settings,
            provider=provider,
            enforce_rate_limits=args.enforce_rate_limits,
        )
        character_name = simulator.bot.character.name
        expected_character = str(scenario.get("expected_character", "")).strip()
        character_match = _named_expectation_met(
            expected_character,
            character_name,
        )
        results: list[dict[str, object]] = []
        last_message_id: int | None = None
        failed = not character_match
        try:
            for turn_index, turn in enumerate(turns, start=1):
                if not isinstance(turn, dict):
                    raise ValueError("each turn must be an object")
                message = str(turn.get("message", "")).strip()
                if not message and not turn.get("attachments"):
                    raise ValueError(f"turn {turn_index} has neither a message nor attachments")
                attachments = tuple(
                    _materialize_attachment(
                        item,
                        scenario_dir=scenario_path.parent,
                        temporary_dir=temporary_dir,
                        index=turn_index * 10 + attachment_index,
                    )
                    for attachment_index, item in enumerate(turn.get("attachments", []), start=1)
                )
                reply_to = last_message_id if bool(turn.get("reply_to_last")) else None
                reply = await simulator.send(
                    message,
                    attachments=attachments,
                    reply_to=reply_to,
                )
                expected_statuses = tuple(str(value) for value in turn.get("expected_attachment_statuses", []))
                actual_statuses = tuple(item.status for item in reply.attachments)
                attachment_match = not expected_statuses or expected_statuses == actual_statuses
                style_match = _style_expectation_met(turn, reply.style_issues)
                failed = failed or not reply.generated or not attachment_match or not style_match
                selected_models = reply.provider_status.get("selected_models", [])
                selected_model = (
                    selected_models[0]
                    if isinstance(selected_models, list)
                    and selected_models
                    and isinstance(selected_models[0], dict)
                    else {}
                )
                result: dict[str, object] = {
                        "turn": turn_index,
                        "character": character_name,
                        "user": message,
                        "assistant": reply.content,
                        "delivery": reply.delivery,
                        "generated": reply.generated,
                        "elapsed_seconds": round(reply.elapsed_seconds, 3),
                        "route": {
                            "model": selected_model.get("model", ""),
                            "format": selected_model.get("format", ""),
                            "eligible_tokens_per_second": selected_model.get(
                                "eligible_tokens_per_second", 0
                            ),
                            "estimated_wait_seconds": selected_model.get(
                                "estimated_wait_seconds", 0
                            ),
                        },
                        "style_issues": list(reply.style_issues),
                        "style_expectation_met": style_match,
                        "attachments": [
                            {
                                "filename": item.filename,
                                "kind": item.kind,
                                "status": item.status,
                                "cache_hit": item.cache_hit,
                                "error": item.error,
                            }
                            for item in reply.attachments
                        ],
                        "attachment_expectation_met": attachment_match,
                    }
                results.append(result)
                if not args.json:
                    _print_turn(result)
                last_message_id = reply.message_id
        finally:
            await simulator.close()

    if args.json:
        print(
            json.dumps(
                {
                    "passed": not failed,
                    "purpose": scenario_purpose,
                    "character": character_name,
                    "character_expectation_met": character_match,
                    "turns": results,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"SCOPE: {scenario_purpose}", flush=True)
        if not character_match:
            print(
                f"CHARACTER: FAIL expected={expected_character!r} actual={character_name!r}",
                flush=True,
            )
        print(f"RESULT: {'PASS' if not failed else 'FAIL'}")
    return 1 if failed else 0


def _print_turn(result: dict[str, object]) -> None:
    print(f"USER {result['turn']}> {result['user']}", flush=True)
    print(f"{str(result['character']).upper()} {result['turn']}> {result['assistant']}", flush=True)
    route = result["route"]
    route_model = route["model"]  # type: ignore[index]
    if route_model:
        print(
            f"  route: {route_model} elapsed={result['elapsed_seconds']}s "
            f"metadata_wait={route['estimated_wait_seconds']}s "  # type: ignore[index]
            f"eligible_speed={route['eligible_tokens_per_second']} tok/s",  # type: ignore[index]
            flush=True,
        )
    for attachment in result["attachments"]:  # type: ignore[union-attr]
        print(
            "  attachment: "
            f"{attachment['filename']} {attachment['status']}/{attachment['kind']} "
            f"cache_hit={attachment['cache_hit']}",
            flush=True,
        )
    issues = result["style_issues"]
    audit = "PASS" if result["generated"] and result["style_expectation_met"] else "FAIL"
    details = "" if not issues else " " + ",".join(issues)  # type: ignore[arg-type]
    if not result["generated"]:
        details += " provider_or_delivery_failure"
    print(f"  audit: {audit}{details}", flush=True)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(asyncio.run(_run(_arguments())))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Simulation configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
