#!/usr/bin/env python3
"""Report whether retained model outcomes justify manual router review."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_ROUTER_SAMPLES_PER_MODEL_TASK = 20


def router_evidence(database: Path) -> dict[str, object]:
    if not database.exists():
        return {
            "samples": 0,
            "groups": [],
            "decision": "hold_current_scores",
            "reason": "no local outcome database",
        }

    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_outcomes'"
        ).fetchone()
        rows = (
            connection.execute(
                """
                SELECT model, task, COUNT(*) AS samples, SUM(success) AS successes,
                       CAST(AVG(latency_ms) AS INTEGER) AS average_latency_ms
                FROM model_outcomes GROUP BY model, task ORDER BY samples DESC
                """
            ).fetchall()
            if exists
            else []
        )
    finally:
        connection.close()

    groups = [
        {
            "model": str(row[0]),
            "task": str(row[1]),
            "samples": int(row[2]),
            "successes": int(row[3] or 0),
            "average_latency_ms": int(row[4] or 0),
        }
        for row in rows
    ]
    eligible_groups = sum(
        int(item["samples"]) >= MIN_ROUTER_SAMPLES_PER_MODEL_TASK
        for item in groups
    )
    return {
        "samples": sum(int(item["samples"]) for item in groups),
        "minimum_per_model_task": MIN_ROUTER_SAMPLES_PER_MODEL_TASK,
        "groups": groups,
        "eligible_groups": eligible_groups,
        "decision": "review_calibration" if eligible_groups else "hold_current_scores",
        "reason": (
            "at least one model/task group meets the manual review threshold"
            if eligible_groups
            else "insufficient actual outcomes per model/task"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "agent.db")
    args = parser.parse_args()
    print(json.dumps({"router": router_evidence(args.database)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
