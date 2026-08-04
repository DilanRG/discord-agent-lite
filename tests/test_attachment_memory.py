from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentbot.memory import MemoryStore


class DormantAttachmentSchemaTests(unittest.TestCase):
    def test_schema_v8_attachment_tables_remain_available_but_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                self.assertEqual(
                    int(store._conn.execute("PRAGMA user_version").fetchone()[0]),
                8,
                )
                tables = {
                    str(row[0])
                    for row in store._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    ).fetchall()
                }
                self.assertTrue(
                    {"attachments", "attachment_sources", "attachment_chunks"}
                    <= tables
                )
                self.assertEqual(store.stats()["attachments"], 0)
                self.assertEqual(store.stats()["attachment_chunks"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
