from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentbot.memory import MemoryStore
from scripts.evaluate_phase4_evidence import router_evidence


class RouterEvidenceTests(unittest.TestCase):
    def test_router_evidence_requires_a_real_model_task_sample_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = router_evidence(root / "missing.db")
            self.assertEqual(missing["samples"], 0)
            self.assertEqual(missing["decision"], "hold_current_scores")

            database = root / "agent.db"
            store = MemoryStore(database, max_model_outcomes=100)
            try:
                for index in range(20):
                    store.record_model_outcome(
                        model="roleplay/model",
                        task="chat",
                        success=index < 18,
                        latency_seconds=1.5,
                    )
                for _ in range(19):
                    store.record_model_outcome(
                        model="other/model",
                        task="chat",
                        success=True,
                        latency_seconds=0.5,
                    )
            finally:
                store.close()

            result = router_evidence(database)
            self.assertEqual(result["samples"], 39)
            self.assertEqual(result["eligible_groups"], 1)
            self.assertEqual(result["decision"], "review_calibration")
            roleplay = next(
                item for item in result["groups"] if item["model"] == "roleplay/model"
            )
            self.assertEqual(roleplay["successes"], 18)
            self.assertEqual(roleplay["average_latency_ms"], 1500)


if __name__ == "__main__":
    unittest.main()
