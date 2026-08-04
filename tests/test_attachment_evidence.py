from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentbot.attachment_evidence import (
    AttachmentEvidence,
    decode_attachment_parts,
    encode_attachment_parts,
    explicit_memory_references_attachment,
)
from agentbot.memory import MemoryStore


def _part(**changes: object) -> AttachmentEvidence:
    values: dict[str, object] = {
        "attachment_id": "discord-42", "ordinal": 0, "filename": "notes.txt",
        "detected_kind": "text", "status": "ready", "origin": "text_extract",
        "text": "the project is called cobalt", "confidence": 0.8,
    }
    values.update(changes)
    return AttachmentEvidence(**values)  # type: ignore[arg-type]


class AttachmentEvidenceTests(unittest.TestCase):
    def test_only_outer_deictic_attachment_requests_select_evidence_memory(self) -> None:
        self.assertTrue(explicit_memory_references_attachment("this attachment"))
        self.assertTrue(explicit_memory_references_attachment("what is in the document"))
        self.assertFalse(explicit_memory_references_attachment("my attachment broke yesterday"))
        self.assertFalse(explicit_memory_references_attachment("I use Fedora"))

    def test_codec_is_bounded_and_corruption_fails_closed(self) -> None:
        part = _part(filename="C:/unsafe\\name?.txt", text="x" * 2_000)
        decoded = decode_attachment_parts(encode_attachment_parts([part] * 20))
        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[0].filename, "C_unsafe_name_.txt")
        self.assertEqual(len(decoded[0].text), 2_000)
        self.assertEqual(sum(len(item.text) for item in decoded), 4_000)
        self.assertEqual(decode_attachment_parts("{bad json"), ())
        self.assertEqual(decode_attachment_parts('[{"unexpected": true}]'), ())

        large = _part(text="y" * 6_000)
        total_bounded = decode_attachment_parts(encode_attachment_parts([large, large]))
        self.assertEqual(sum(len(item.text) for item in total_bounded), 6_000)
        self.assertTrue(total_bounded[1].truncated)

        error = _part(status="error", text="untrusted error body", error_code="timeout")
        self.assertEqual(error.text, "")
        self.assertEqual(error.error_code, "timeout")

    def test_message_and_relationship_round_trip_and_recall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                part = _part()
                store.record_message(
                    scope="dm:7", guild_id=0, channel_id=0, user_id=7,
                    author_name="user", role="user", content="please see this", attachment_parts=[part],
                )
                message, _ = store.recall_messages(
                    scope="dm:7", user_id=7, query="cobalt", limit=1, candidates=4,
                )[0]
                self.assertEqual(message.attachment_parts, (part,))
                store.record_relationship_interaction(
                    guild_id=0, channel_id=0, user_id=7, scope="dm:7", user_text="hello",
                    assistant_text="hi", attachment_parts=[part],
                )
                batch = store.relationship_reflection_batch(guild_id=0, user_id=7, max_events=1)
                self.assertIsNotNone(batch)
                self.assertEqual(batch.events[0].attachment_parts, (part,))  # type: ignore[union-attr]
                store.reset_social_profile(user_id=7)
                self.assertIsNone(
                    store.relationship_reflection_batch(guild_id=0, user_id=7, max_events=1)
                )
                store.delete_conversation_memory(0, 7)
                self.assertEqual(store.stats()["messages"], 0)
            finally:
                store.close()

    def test_v7_database_migrates_additively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            store = MemoryStore(path)
            store.close()
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA user_version = 7")
                connection.execute("ALTER TABLE messages DROP COLUMN attachment_parts_json")
                connection.execute("ALTER TABLE relationship_events DROP COLUMN attachment_parts_json")
                connection.commit()
            finally:
                connection.close()
            migrated = MemoryStore(path)
            try:
                self.assertEqual(migrated._conn.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertTrue(migrated._table_has_column("messages", "attachment_parts_json"))
                self.assertTrue(migrated._table_has_column("relationship_events", "attachment_parts_json"))
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
