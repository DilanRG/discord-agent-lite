from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .policy import tokenize
from .social import (
    RELATIONSHIP_DIMENSIONS,
    ProfileObservation,
    direct_evidence_matches,
    normalize_compact_journal,
    profile_observation_allowed,
    relationship_familiarity,
    relationship_label,
    sanitize_social_text,
    social_text_allowed,
)
from .group import GroupObservation


logger = logging.getLogger(__name__)
_MAX_PERSISTED_SUMMARY_CHARS = 8_000

_SOCIAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profile_state (
    user_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('fact', 'impression')),
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('direct', 'inferred')),
    confidence REAL NOT NULL DEFAULT 0.75,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'tentative'
        CHECK(status IN ('tentative', 'confirmed', 'contradicted', 'superseded')),
    superseded_by_id INTEGER,
    source_scope TEXT NOT NULL,
    source_guild_id INTEGER NOT NULL,
    source_channel_id INTEGER NOT NULL DEFAULT 0,
    source_message_id INTEGER,
    visibility TEXT NOT NULL CHECK(visibility IN ('dm', 'guild')),
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(user_id, kind, topic, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_profile_facts_user_rank
    ON profile_facts(
        user_id, status, evidence_count DESC, confidence DESC, last_seen_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_profile_facts_context
    ON profile_facts(user_id, visibility, source_guild_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS relationships (
    user_id INTEGER PRIMARY KEY,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    affection INTEGER NOT NULL DEFAULT 0 CHECK(affection BETWEEN -20 AND 20),
    trust INTEGER NOT NULL DEFAULT 0 CHECK(trust BETWEEN -20 AND 20),
    respect INTEGER NOT NULL DEFAULT 0 CHECK(respect BETWEEN -20 AND 20),
    amusement INTEGER NOT NULL DEFAULT 0 CHECK(amusement BETWEEN -20 AND 20),
    curiosity INTEGER NOT NULL DEFAULT 0 CHECK(curiosity BETWEEN -20 AND 20),
    tension INTEGER NOT NULL DEFAULT 0 CHECK(tension BETWEEN -20 AND 20),
    annoyance INTEGER NOT NULL DEFAULT 0 CHECK(annoyance BETWEEN -20 AND 20),
    wariness INTEGER NOT NULL DEFAULT 0 CHECK(wariness BETWEEN -20 AND 20),
    summary TEXT NOT NULL DEFAULT '',
    last_interaction_at INTEGER NOT NULL DEFAULT 0,
    last_reflected_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationships_recent
    ON relationships(last_interaction_at DESC);

CREATE TABLE IF NOT EXISTS relationship_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    source_message_id INTEGER,
    meaningful INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_events_user
    ON relationship_events(guild_id, user_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_relationship_events_global_user
    ON relationship_events(user_id, id ASC);

CREATE TABLE IF NOT EXISTS agent_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    source_guild_id INTEGER NOT NULL,
    source_channel_id INTEGER NOT NULL DEFAULT 0,
    source_message_id INTEGER,
    visibility TEXT NOT NULL CHECK(visibility IN ('dm', 'guild')),
    source_through_event_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, source_through_event_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_journal_user
    ON agent_journal(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_journal_context
    ON agent_journal(user_id, visibility, source_guild_id, id DESC);

CREATE TABLE IF NOT EXISTS guild_continuity (
    guild_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    last_interaction_at INTEGER NOT NULL DEFAULT 0,
    last_reflected_at INTEGER NOT NULL DEFAULT 0,
    source_through_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_continuity_recent
    ON guild_continuity(last_interaction_at DESC);

CREATE TABLE IF NOT EXISTS guild_group_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    source_message_id INTEGER,
    meaningful INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_group_events_guild
    ON guild_group_events(guild_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_guild_group_events_user
    ON guild_group_events(user_id, guild_id, id ASC);

CREATE TABLE IF NOT EXISTS guild_group_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('culture', 'norm', 'joke', 'dynamic', 'event')),
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    source_through_event_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(guild_id, kind, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_guild_group_journal_context
    ON guild_group_journal(guild_id, id DESC);

CREATE TABLE IF NOT EXISTS guild_continuity_members (
    guild_id INTEGER NOT NULL REFERENCES guild_continuity(guild_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    last_contributed_at INTEGER NOT NULL,
    PRIMARY KEY(guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_guild_continuity_members_user
    ON guild_continuity_members(user_id, guild_id);

CREATE TABLE IF NOT EXISTS interaction_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_type TEXT NOT NULL CHECK(conversation_type IN ('dm', 'guild')),
    user_chars_bucket INTEGER NOT NULL,
    assistant_chars_bucket INTEGER NOT NULL,
    directed INTEGER NOT NULL,
    meaningful_social INTEGER NOT NULL,
    group_signal INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interaction_metrics_time
    ON interaction_metrics(id DESC);
"""


class SocialMigrationError(RuntimeError):
    """Raised when legacy social identities cannot be migrated without guessing."""


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    scope: str
    guild_id: int
    channel_id: int
    user_id: int
    author_name: str
    role: str
    content: str
    created_at: int
    discord_message_id: int | None
    is_proactive: bool


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: int
    guild_id: int
    user_id: int
    kind: str
    text: str
    importance: int
    created_at: int
    last_used_at: int
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    guild_id: int
    channel_id: int
    auto_reply: bool
    proactive: bool


@dataclass(frozen=True, slots=True)
class CompactionBatch:
    scope: str
    guild_id: int
    channel_id: int
    previous_summary: str
    messages: tuple[MessageRecord, ...]
    through_message_id: int


@dataclass(frozen=True, slots=True)
class PrivacyState:
    opted_out: bool
    revision: int


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    id: int
    user_id: int
    kind: str
    topic: str
    text: str
    provenance: str
    confidence: float
    evidence_count: int
    status: str
    superseded_by_id: int | None
    source_scope: str
    source_guild_id: int
    source_channel_id: int
    source_message_id: int | None
    visibility: str
    created_at: int
    last_seen_at: int


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: int
    user_id: int
    text: str
    source_scope: str
    source_guild_id: int
    source_channel_id: int
    source_message_id: int | None
    visibility: str
    created_at: int


@dataclass(frozen=True, slots=True)
class RelationshipState:
    user_id: int
    interaction_count: int
    affection: int
    trust: int
    respect: int
    amusement: int
    curiosity: int
    tension: int
    annoyance: int
    wariness: int
    summary: str
    last_interaction_at: int
    last_reflected_at: int

    @property
    def familiarity(self) -> int:
        return relationship_familiarity(self.interaction_count)

    @property
    def dimensions(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in RELATIONSHIP_DIMENSIONS}

    @property
    def label(self) -> str:
        return relationship_label(self.interaction_count, self.dimensions)


@dataclass(frozen=True, slots=True)
class InteractionEvent:
    id: int
    guild_id: int
    channel_id: int
    scope: str
    user_text: str
    assistant_text: str
    source_message_id: int | None
    meaningful: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class RelationshipReflectionBatch:
    guild_id: int
    user_id: int
    profile_revision: int
    relationship: RelationshipState
    events: tuple[InteractionEvent, ...]
    through_event_id: int


@dataclass(frozen=True, slots=True)
class GroupContinuityState:
    guild_id: int
    summary: str
    interaction_count: int
    last_interaction_at: int
    last_reflected_at: int
    source_through_event_id: int


@dataclass(frozen=True, slots=True)
class GroupInteractionEvent:
    id: int
    guild_id: int
    channel_id: int
    user_id: int
    scope: str
    user_text: str
    assistant_text: str
    source_message_id: int | None
    meaningful: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class GroupJournalEntry:
    id: int
    guild_id: int
    kind: str
    text: str
    created_at: int


@dataclass(frozen=True, slots=True)
class GroupReflectionBatch:
    guild_id: int
    participant_revisions: tuple[tuple[int, int], ...]
    continuity: GroupContinuityState
    journal: tuple[GroupJournalEntry, ...]
    events: tuple[GroupInteractionEvent, ...]
    through_event_id: int


@dataclass(frozen=True, slots=True)
class ModelOutcomeRecord:
    id: int
    model: str
    task: str
    worker_id: str
    worker_name: str
    success: bool
    latency_ms: int
    error_kind: str
    empty: bool
    malformed: bool
    truncated: bool
    created_at: int

    @property
    def latency_seconds(self) -> float:
        return self.latency_ms / 1000.0


@dataclass(frozen=True, slots=True)
class AttachmentChunkInput:
    chunk_index: int
    text: str
    page_number: int | None
    heading: str
    keywords: str


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    sha256: str
    filename: str
    detected_type: str
    size_bytes: int
    status: str
    text: str
    parser_version: str
    confidence: float
    error: str
    page_count: int
    width: int
    height: int
    truncated: bool
    created_at: int
    last_used_at: int


@dataclass(frozen=True, slots=True)
class AttachmentChunkRecord:
    id: int
    sha256: str
    filename: str
    chunk_index: int
    text: str
    page_number: int | None
    heading: str
    score: float


class MemoryStore:
    """Bounded SQLite-backed conversation memory with no embedding dependencies."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_messages_per_scope: int = 0,
        max_memories_per_user: int = 0,
        max_total_messages: int = 0,
        max_total_memories: int = 0,
        max_model_outcomes: int = 0,
        max_attachments: int = 0,
        max_attachment_chunks: int = 0,
        max_profile_facts_per_user: int = 0,
        max_journal_entries_per_user: int = 0,
        max_pending_interactions_per_user: int = 0,
        max_total_profile_facts: int = 0,
        max_total_journal_entries: int = 0,
        max_total_pending_interactions: int = 0,
        max_total_relationships: int = 0,
        max_group_events_per_guild: int = 0,
        max_total_group_events: int = 0,
        max_group_journal_per_guild: int = 0,
        max_total_group_journal: int = 0,
        max_group_continuities: int = 0,
        max_group_members_per_guild: int = 0,
        max_interaction_metrics: int = 0,
        legacy_social_namespace: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_messages_per_scope = max(0, int(max_messages_per_scope))
        self.max_memories_per_user = max(0, int(max_memories_per_user))
        self.max_total_messages = max(0, int(max_total_messages))
        self.max_total_memories = max(0, int(max_total_memories))
        self.max_model_outcomes = max(0, int(max_model_outcomes))
        self.max_attachments = max(0, int(max_attachments))
        self.max_attachment_chunks = max(0, int(max_attachment_chunks))
        self.max_profile_facts_per_user = max(0, int(max_profile_facts_per_user))
        self.max_journal_entries_per_user = max(0, int(max_journal_entries_per_user))
        self.max_pending_interactions_per_user = max(
            0, int(max_pending_interactions_per_user)
        )
        self.max_total_profile_facts = max(0, int(max_total_profile_facts))
        self.max_total_journal_entries = max(0, int(max_total_journal_entries))
        self.max_total_pending_interactions = max(
            0, int(max_total_pending_interactions)
        )
        self.max_total_relationships = max(0, int(max_total_relationships))
        self.max_group_events_per_guild = max(0, int(max_group_events_per_guild))
        self.max_total_group_events = max(0, int(max_total_group_events))
        self.max_group_journal_per_guild = max(0, int(max_group_journal_per_guild))
        self.max_total_group_journal = max(0, int(max_total_group_journal))
        self.max_group_continuities = max(0, int(max_group_continuities))
        self.max_group_members_per_guild = max(0, int(max_group_members_per_guild))
        self.max_interaction_metrics = max(0, int(max_interaction_metrics))
        self.legacy_social_namespace = (
            sanitize_social_text(legacy_social_namespace, 64)
            if legacy_social_namespace
            else None
        )
        if self.max_total_messages >= 128:
            self._global_message_check_interval = min(64, self.max_total_messages // 10)
            self._global_message_trim_target = (
                self.max_total_messages - self._global_message_check_interval
            )
        else:
            self._global_message_check_interval = 1
            self._global_message_trim_target = self.max_total_messages
        self._global_message_checks_remaining = 1
        self._conn = sqlite3.connect(self.path, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        try:
            schema_preflight = self._schema_preflight()
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=3000")
            self._conn.execute("PRAGMA cache_size=-2048")
            self._conn.execute("PRAGMA wal_autocheckpoint=500")
            self._conn.execute("PRAGMA journal_size_limit=2097152")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._init_schema(*schema_preflight)
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    def _schema_preflight(self) -> tuple[int, bool, str]:
        schema_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if schema_version > 7:
            raise SocialMigrationError(
                f"Database schema version {schema_version} is newer than supported version 7"
            )
        legacy_social_schema = self._table_has_column("profile_facts", "agent_namespace")
        legacy_social_identity = (
            self._legacy_social_migration_selection()
            if legacy_social_schema
            else ""
        )
        return schema_version, legacy_social_schema, legacy_social_identity

    def _init_schema(
        self,
        schema_version: int,
        legacy_social_schema: bool,
        legacy_social_identity: str,
    ) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                discord_message_id INTEGER UNIQUE,
                is_proactive INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_messages_scope_time
                ON messages(scope, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_user_time
                ON messages(guild_id, user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_channel_human
                ON messages(guild_id, channel_id, role, created_at DESC);

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                importance INTEGER NOT NULL DEFAULT 5,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                source_message_id INTEGER,
                UNIQUE(guild_id, user_id, text_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_memories_user_time
                ON memories(guild_id, user_id, last_used_at DESC);

            CREATE TABLE IF NOT EXISTS model_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                task TEXT NOT NULL,
                worker_id TEXT NOT NULL DEFAULT '',
                worker_name TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                error_kind TEXT NOT NULL DEFAULT '',
                empty INTEGER NOT NULL DEFAULT 0,
                malformed INTEGER NOT NULL DEFAULT 0,
                truncated INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_outcomes_model_task_time
                ON model_outcomes(model, task, id DESC);

            CREATE TABLE IF NOT EXISTS attachments (
                sha256 TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                detected_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'error')),
                text TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                page_count INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                truncated INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_attachments_lru
                ON attachments(last_used_at ASC, sha256);

            CREATE TABLE IF NOT EXISTS attachment_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL REFERENCES attachments(sha256) ON DELETE CASCADE,
                filename TEXT NOT NULL DEFAULT 'attachment',
                source_scope TEXT NOT NULL,
                source_guild_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL DEFAULT 0,
                source_message_id INTEGER NOT NULL DEFAULT 0,
                uploader_user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                UNIQUE(sha256, source_scope, source_message_id, uploader_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attachment_sources_scope
                ON attachment_sources(source_scope, sha256);
            CREATE INDEX IF NOT EXISTS idx_attachment_sources_user
                ON attachment_sources(source_guild_id, uploader_user_id, id);

            CREATE TABLE IF NOT EXISTS attachment_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL REFERENCES attachments(sha256) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                page_number INTEGER,
                heading TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                UNIQUE(sha256, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_attachment_chunks_hash
                ON attachment_chunks(sha256, chunk_index);

            CREATE VIRTUAL TABLE IF NOT EXISTS attachment_chunks_fts USING fts5(
                text,
                heading,
                keywords,
                content='attachment_chunks',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TRIGGER IF NOT EXISTS attachment_chunks_ai AFTER INSERT ON attachment_chunks BEGIN
                INSERT INTO attachment_chunks_fts(rowid, text, heading, keywords)
                VALUES (new.id, new.text, new.heading, new.keywords);
            END;
            CREATE TRIGGER IF NOT EXISTS attachment_chunks_ad AFTER DELETE ON attachment_chunks BEGIN
                INSERT INTO attachment_chunks_fts(attachment_chunks_fts, rowid, text, heading, keywords)
                VALUES ('delete', old.id, old.text, old.heading, old.keywords);
            END;
            CREATE TRIGGER IF NOT EXISTS attachment_chunks_au AFTER UPDATE ON attachment_chunks BEGIN
                INSERT INTO attachment_chunks_fts(attachment_chunks_fts, rowid, text, heading, keywords)
                VALUES ('delete', old.id, old.text, old.heading, old.keywords);
                INSERT INTO attachment_chunks_fts(rowid, text, heading, keywords)
                VALUES (new.id, new.text, new.heading, new.keywords);
            END;

            CREATE TABLE IF NOT EXISTS summaries (
                scope TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                through_message_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_config (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                auto_reply INTEGER NOT NULL DEFAULT 0,
                proactive INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS channel_state (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                last_proactive_at INTEGER NOT NULL DEFAULT 0,
                proactive_day TEXT NOT NULL DEFAULT '',
                proactive_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS privacy (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                opted_out INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(guild_id, user_id)
            );
            """
        )
        # Version 1.0 databases predate the revision field. A monotonically
        # increasing revision prevents an in-flight reflection task from
        # recreating data after a user has reset or deleted it.
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(privacy)").fetchall()
        }
        if "revision" not in columns:
            self._conn.execute(
                "ALTER TABLE privacy ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        attachment_source_columns = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(attachment_sources)"
            ).fetchall()
        }
        if "filename" not in attachment_source_columns:
            self._conn.execute(
                "ALTER TABLE attachment_sources "
                "ADD COLUMN filename TEXT NOT NULL DEFAULT 'attachment'"
            )
        # Filenames are source-scoped metadata. Never retain an uploader's name
        # on the hash-global extraction row, including from an earlier dev3 DB.
        self._conn.execute("UPDATE attachments SET filename = 'attachment'")
        if legacy_social_schema:
            self._migrate_v1_social_schema(legacy_social_identity)
        else:
            self._create_social_schema()
        if schema_version < 7 or legacy_social_schema:
            self._quarantine_unsafe_social_rows()
        self._conn.execute("PRAGMA user_version = 7")
        self._conn.commit()

    def _table_has_column(self, table: str, column: str) -> bool:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            return False
        return column in {
            str(row["name"])
            for row in self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }

    def _create_social_schema(self) -> None:
        self._conn.executescript(_SOCIAL_SCHEMA_SQL)

    def _quarantine_unsafe_social_rows(self) -> None:
        """One-time v6 cleanup for rows written before durable-policy checks."""
        for row in self._conn.execute(
            "SELECT scope, text FROM summaries"
        ).fetchall():
            clean = sanitize_social_text(
                str(row["text"]),
                _MAX_PERSISTED_SUMMARY_CHARS,
            )
            self._conn.execute(
                "UPDATE summaries SET text = ? WHERE scope = ?",
                (clean if social_text_allowed(clean) else "", str(row["scope"])),
            )

        for row in self._conn.execute(
            "SELECT user_id, summary FROM relationships"
        ).fetchall():
            clean = sanitize_social_text(str(row["summary"]), 400)
            self._conn.execute(
                "UPDATE relationships SET summary = ? WHERE user_id = ?",
                (clean if social_text_allowed(clean) else "", int(row["user_id"])),
            )

        for row in self._conn.execute(
            "SELECT id, kind, topic, text, provenance FROM profile_facts"
        ).fetchall():
            clean_topic = sanitize_social_text(str(row["topic"]), 40).casefold()
            clean_text = sanitize_social_text(str(row["text"]), 320)
            if not profile_observation_allowed(
                str(row["kind"]),
                clean_topic,
                clean_text,
                str(row["provenance"]),
            ):
                self._conn.execute(
                    "DELETE FROM profile_facts WHERE id = ?",
                    (int(row["id"]),),
                )
                continue
            try:
                self._conn.execute(
                    "UPDATE profile_facts SET topic = ?, text = ?, text_hash = ? WHERE id = ?",
                    (
                        clean_topic,
                        clean_text,
                        self._memory_hash(clean_text),
                        int(row["id"]),
                    ),
                )
            except sqlite3.IntegrityError:
                self._conn.execute(
                    "DELETE FROM profile_facts WHERE id = ?",
                    (int(row["id"]),),
                )

        for row in self._conn.execute(
            "SELECT id, text FROM agent_journal"
        ).fetchall():
            clean = normalize_compact_journal(str(row["text"]))
            if clean:
                self._conn.execute(
                    "UPDATE agent_journal SET text = ? WHERE id = ?",
                    (clean, int(row["id"])),
                )
            else:
                self._conn.execute(
                    "DELETE FROM agent_journal WHERE id = ?",
                    (int(row["id"]),),
                )

        for row in self._conn.execute(
            "SELECT id, user_text, assistant_text FROM relationship_events"
        ).fetchall():
            clean_user = sanitize_social_text(str(row["user_text"]), 650)
            clean_assistant = sanitize_social_text(str(row["assistant_text"]), 650)
            self._conn.execute(
                "UPDATE relationship_events SET user_text = ?, assistant_text = ? WHERE id = ?",
                (
                    clean_user
                    if social_text_allowed(clean_user)
                    else "[content omitted from social reflection]",
                    clean_assistant
                    if social_text_allowed(clean_assistant)
                    else "[response omitted from social reflection]",
                    int(row["id"]),
                ),
            )

        for row in self._conn.execute(
            "SELECT guild_id, summary FROM guild_continuity"
        ).fetchall():
            clean = sanitize_social_text(str(row["summary"]), 600)
            self._conn.execute(
                "UPDATE guild_continuity SET summary = ? WHERE guild_id = ?",
                (
                    clean if social_text_allowed(clean) else "",
                    int(row["guild_id"]),
                ),
            )

        for row in self._conn.execute(
            "SELECT id, user_text, assistant_text FROM guild_group_events"
        ).fetchall():
            clean_user = sanitize_social_text(str(row["user_text"]), 650)
            clean_assistant = sanitize_social_text(str(row["assistant_text"]), 650)
            self._conn.execute(
                "UPDATE guild_group_events SET user_text = ?, assistant_text = ? WHERE id = ?",
                (
                    clean_user
                    if social_text_allowed(clean_user)
                    else "[content omitted from group reflection]",
                    clean_assistant
                    if social_text_allowed(clean_assistant)
                    else "[response omitted from group reflection]",
                    int(row["id"]),
                ),
            )

        for row in self._conn.execute(
            "SELECT id, text FROM guild_group_journal"
        ).fetchall():
            clean = sanitize_social_text(str(row["text"]), 360)
            if not social_text_allowed(clean):
                self._conn.execute(
                    "DELETE FROM guild_group_journal WHERE id = ?",
                    (int(row["id"]),),
                )
                continue
            try:
                self._conn.execute(
                    "UPDATE guild_group_journal SET text = ?, text_hash = ? WHERE id = ?",
                    (clean, self._memory_hash(clean), int(row["id"])),
                )
            except sqlite3.IntegrityError:
                self._conn.execute(
                    "DELETE FROM guild_group_journal WHERE id = ?",
                    (int(row["id"]),),
                )

    @staticmethod
    def _channel_id_from_scope(scope: str) -> int:
        parts = scope.split(":")
        if len(parts) == 4 and parts[0] == "g" and parts[2] == "c":
            try:
                return max(0, int(parts[3]))
            except ValueError:
                return 0
        return 0

    def _legacy_social_migration_selection(self) -> str:
        legacy_tables = (
            "profile_facts",
            "relationships",
            "relationship_events",
            "agent_journal",
        )
        namespaces: set[str] = set()
        for table in legacy_tables:
            if not self._table_has_column(table, "agent_namespace"):
                continue
            namespaces.update(
                str(row[0])
                for row in self._conn.execute(
                    f'SELECT DISTINCT agent_namespace FROM "{table}"'
                ).fetchall()
                if str(row[0])
            )

        requested = self.legacy_social_namespace
        if namespaces and requested and requested not in namespaces:
            available = ", ".join(sorted(namespaces))
            raise SocialMigrationError(
                "The configured legacy social identity does not match the v1.1 database "
                f"({requested!r} not in {available}). No social history was migrated."
            )
        if len(namespaces) > 1 and not requested:
            available = ", ".join(sorted(namespaces))
            raise SocialMigrationError(
                "The v1.1 database contains multiple social identities "
                f"({available}). Set the one-time legacy migration identity explicitly; "
                "histories will not be merged."
            )
        if requested and requested in namespaces:
            selected = requested
        elif len(namespaces) == 1:
            selected = next(iter(namespaces))
        else:
            selected = requested or ""

        for table in legacy_tables:
            backup_exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (f"{table}_v1",),
            ).fetchone()
            if backup_exists:
                raise SocialMigrationError(
                    f"Cannot migrate while stale temporary table {table}_v1 exists"
                )
        return selected

    def _migrate_v1_social_schema(self, selected: str) -> None:
        legacy_tables = (
            "profile_facts",
            "relationships",
            "relationship_events",
            "agent_journal",
        )

        logger.info(
            "Migrating v1.1 social schema using identity %r; other identities are not merged",
            selected or "(empty database)",
        )
        rename_sql = ["BEGIN IMMEDIATE;"]
        for index in (
            "idx_profile_facts_user_rank",
            "idx_relationships_recent",
            "idx_relationship_events_user",
            "idx_agent_journal_user",
        ):
            rename_sql.append(f'DROP INDEX IF EXISTS "{index}";')
        for table in legacy_tables:
            rename_sql.append(f'ALTER TABLE "{table}" RENAME TO "{table}_v1";')
        rename_sql.append(_SOCIAL_SCHEMA_SQL)

        try:
            self._conn.executescript("\n".join(rename_sql))

            sequence_floors: dict[str, int] = {}
            for table in (
                "profile_facts",
                "relationship_events",
                "agent_journal",
            ):
                max_id = int(
                    self._conn.execute(
                        f'SELECT COALESCE(MAX(id), 0) FROM "{table}_v1"'
                    ).fetchone()[0]
                )
                sequence_rows = self._conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name IN (?, ?)",
                    (table, f"{table}_v1"),
                ).fetchall()
                sequence_floors[table] = max(
                    (max_id, *(int(row[0]) for row in sequence_rows)),
                )

            if selected:
                old_facts = self._conn.execute(
                    """
                    SELECT * FROM profile_facts_v1
                    WHERE agent_namespace = ?
                    ORDER BY created_at ASC, id ASC, last_seen_at ASC
                    """,
                    (selected,),
                ).fetchall()
                for row in old_facts:
                    self._upsert_profile_record_no_commit(
                        user_id=int(row["user_id"]),
                        kind=(
                            "fact" if str(row["source"]) == "user_asserted" else "impression"
                        ),
                        topic=str(row["category"]),
                        text=str(row["text"]),
                        provenance=(
                            "direct" if str(row["source"]) == "user_asserted" else "inferred"
                        ),
                        confidence=float(row["confidence"]),
                        source_scope=(
                            f"dm:{int(row['user_id'])}"
                            if int(row["guild_id"]) == 0
                            else f"g:{int(row['guild_id'])}:legacy"
                        ),
                        source_guild_id=int(row["guild_id"]),
                        source_channel_id=0,
                        source_message_id=None,
                        visibility="dm" if int(row["guild_id"]) == 0 else "guild",
                        now=int(row["last_seen_at"]),
                        record_id=int(row["id"]),
                        created_at=int(row["created_at"]),
                        evidence_increment=max(1, int(row["evidence_count"])),
                        force_confirmed=str(row["status"]) == "confirmed",
                    )

                relationship_rows = self._conn.execute(
                    """
                    SELECT * FROM relationships_v1
                    WHERE agent_namespace = ?
                    ORDER BY last_interaction_at ASC, guild_id ASC
                    """,
                    (selected,),
                ).fetchall()
                combined: dict[int, dict[str, object]] = {}
                for row in relationship_rows:
                    user_id = int(row["user_id"])
                    item = combined.setdefault(
                        user_id,
                        {
                            "interaction_count": 0,
                            "weighted_affinity": 0,
                            "weight": 0,
                            "summary": "",
                            "last_interaction_at": 0,
                            "last_reflected_at": 0,
                            "updated_at": 0,
                        },
                    )
                    count = max(0, int(row["interaction_count"]))
                    weight = max(1, count)
                    item["interaction_count"] = min(
                        2_147_483_647, int(item["interaction_count"]) + count
                    )
                    item["weighted_affinity"] = int(item["weighted_affinity"]) + (
                        int(row["affinity"]) * weight
                    )
                    item["weight"] = int(item["weight"]) + weight
                    if int(row["last_interaction_at"]) >= int(item["last_interaction_at"]):
                        candidate_summary = sanitize_social_text(str(row["summary"]), 400)
                        if social_text_allowed(candidate_summary):
                            item["summary"] = candidate_summary
                    item["last_interaction_at"] = max(
                        int(item["last_interaction_at"]), int(row["last_interaction_at"])
                    )
                    item["last_reflected_at"] = max(
                        int(item["last_reflected_at"]), int(row["last_reflected_at"])
                    )
                    item["updated_at"] = max(
                        int(item["updated_at"]), int(row["updated_at"])
                    )
                for user_id, item in combined.items():
                    affection = round(
                        int(item["weighted_affinity"]) / max(1, int(item["weight"]))
                    )
                    clean_summary = sanitize_social_text(str(item["summary"]), 400)
                    if not social_text_allowed(clean_summary):
                        clean_summary = ""
                    self._conn.execute(
                        """
                        INSERT INTO relationships(
                            user_id, interaction_count, affection, trust, respect,
                            amusement, curiosity, tension, annoyance, wariness,
                            summary, last_interaction_at, last_reflected_at, updated_at
                        ) VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            int(item["interaction_count"]),
                            max(-20, min(20, affection)),
                            clean_summary,
                            int(item["last_interaction_at"]),
                            int(item["last_reflected_at"]),
                            int(item["updated_at"]),
                        ),
                    )

                for row in self._conn.execute(
                    "SELECT * FROM relationship_events_v1 WHERE agent_namespace = ? ORDER BY id",
                    (selected,),
                ).fetchall():
                    scope = str(row["scope"])
                    clean_user = sanitize_social_text(str(row["user_text"]), 650)
                    clean_assistant = sanitize_social_text(str(row["assistant_text"]), 650)
                    if not social_text_allowed(clean_user):
                        clean_user = "[content omitted from social reflection]"
                    if not social_text_allowed(clean_assistant):
                        clean_assistant = "[content omitted from social reflection]"
                    self._conn.execute(
                        """
                        INSERT INTO relationship_events(
                            id, guild_id, channel_id, user_id, scope, user_text,
                            assistant_text, source_message_id, meaningful, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                        """,
                        (
                            int(row["id"]),
                            int(row["guild_id"]),
                            self._channel_id_from_scope(scope),
                            int(row["user_id"]),
                            scope,
                            clean_user,
                            clean_assistant,
                            int(row["created_at"]),
                        ),
                    )

                for row in self._conn.execute(
                    "SELECT * FROM agent_journal_v1 WHERE agent_namespace = ? ORDER BY id",
                    (selected,),
                ).fetchall():
                    scope = str(row["scope"])
                    guild_id = int(row["guild_id"])
                    clean_journal = sanitize_social_text(str(row["text"]), 600)
                    if not social_text_allowed(clean_journal):
                        continue
                    self._conn.execute(
                        """
                        INSERT INTO agent_journal(
                            id, user_id, text, source_scope, source_guild_id,
                            source_channel_id, source_message_id, visibility,
                            source_through_event_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            int(row["id"]),
                            int(row["user_id"]),
                            clean_journal,
                            scope,
                            guild_id,
                            self._channel_id_from_scope(scope),
                            "dm" if guild_id == 0 else "guild",
                            int(row["source_through_event_id"]),
                            int(row["created_at"]),
                        ),
                    )

            for table in legacy_tables:
                self._conn.execute(f'DROP TABLE "{table}_v1"')
            for table, floor in sequence_floors.items():
                if floor <= 0:
                    continue
                current = self._conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = ?",
                    (table,),
                ).fetchone()
                if current is None:
                    self._conn.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (table, floor),
                    )
                elif int(current[0]) < floor:
                    self._conn.execute(
                        "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                        (floor, table),
                    )
            self._conn.execute("PRAGMA user_version = 5")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def scope_for(guild_id: int, channel_id: int, user_id: int | None = None) -> str:
        if guild_id == 0:
            if user_id is None:
                raise ValueError("user_id is required for a DM scope")
            return f"dm:{user_id}"
        return f"g:{guild_id}:c:{channel_id}"

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self._conn.close()
            self._closed = True

    def record_model_outcome(
        self,
        *,
        model: str,
        task: str,
        worker_id: str = "",
        worker_name: str = "",
        success: bool,
        latency_seconds: float = 0.0,
        error_kind: str = "",
        empty: bool = False,
        malformed: bool = False,
        truncated: bool = False,
        created_at: int | None = None,
    ) -> None:
        clean_model = str(model).strip()[:240]
        if not clean_model:
            return
        self._conn.execute(
            """
            INSERT INTO model_outcomes(
                model, task, worker_id, worker_name, success, latency_ms,
                error_kind, empty, malformed, truncated, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_model,
                str(task).strip()[:32] or "chat",
                str(worker_id).strip()[:120],
                str(worker_name).strip()[:160],
                int(bool(success)),
                max(0, min(2_147_483_647, round(float(latency_seconds) * 1000))),
                str(error_kind).strip()[:80],
                int(bool(empty)),
                int(bool(malformed)),
                int(bool(truncated)),
                int(time.time()) if created_at is None else int(created_at),
            ),
        )
        if self.max_model_outcomes:
            self._conn.execute(
                """
                DELETE FROM model_outcomes
                WHERE id NOT IN (
                    SELECT id FROM model_outcomes ORDER BY id DESC LIMIT ?
                )
                """,
                (self.max_model_outcomes,),
            )
        self._conn.commit()

    def model_outcome_history(self, *, limit: int = 250) -> list[ModelOutcomeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM model_outcomes ORDER BY id DESC LIMIT ?",
            (max(0, min(10_000, int(limit))),),
        ).fetchall()
        return [
            ModelOutcomeRecord(
                id=int(row["id"]),
                model=str(row["model"]),
                task=str(row["task"]),
                worker_id=str(row["worker_id"]),
                worker_name=str(row["worker_name"]),
                success=bool(row["success"]),
                latency_ms=int(row["latency_ms"]),
                error_kind=str(row["error_kind"]),
                empty=bool(row["empty"]),
                malformed=bool(row["malformed"]),
                truncated=bool(row["truncated"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _attachment_from_row(row: sqlite3.Row) -> AttachmentRecord:
        return AttachmentRecord(
            sha256=str(row["sha256"]),
            filename=str(row["filename"]),
            detected_type=str(row["detected_type"]),
            size_bytes=int(row["size_bytes"]),
            status=str(row["status"]),
            text=str(row["text"]),
            parser_version=str(row["parser_version"]),
            confidence=float(row["confidence"]),
            error=str(row["error"]),
            page_count=int(row["page_count"]),
            width=int(row["width"]),
            height=int(row["height"]),
            truncated=bool(row["truncated"]),
            created_at=int(row["created_at"]),
            last_used_at=int(row["last_used_at"]),
        )

    @staticmethod
    def _validate_attachment_hash(sha256: str) -> str:
        clean = str(sha256).strip().casefold()
        if len(clean) != 64 or any(character not in "0123456789abcdef" for character in clean):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        return clean

    def attachment_by_hash(self, sha256: str) -> AttachmentRecord | None:
        digest = self._validate_attachment_hash(sha256)
        row = self._conn.execute(
            "SELECT * FROM attachments WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        return self._attachment_from_row(row) if row else None

    def touch_attachment(self, sha256: str, *, now: int | None = None) -> None:
        digest = self._validate_attachment_hash(sha256)
        timestamp = int(time.time()) if now is None else int(now)
        self._conn.execute(
            "UPDATE attachments SET last_used_at = ? WHERE sha256 = ?",
            (timestamp, digest),
        )
        self._conn.commit()

    def link_attachment_source(
        self,
        *,
        sha256: str,
        filename: str,
        source_scope: str,
        source_guild_id: int,
        source_channel_id: int,
        source_message_id: int | None,
        uploader_user_id: int,
        expected_privacy_revision: int | None = None,
        now: int | None = None,
    ) -> bool:
        digest = self._validate_attachment_hash(sha256)
        timestamp = int(time.time()) if now is None else int(now)
        clean_filename = self._clean_attachment_filename(filename)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if not self._privacy_allows_attachment_write_no_commit(
                source_guild_id,
                uploader_user_id,
                expected_privacy_revision,
            ):
                self._conn.rollback()
                return False
            self._conn.execute(
                """
                INSERT INTO attachment_sources(
                    sha256, filename, source_scope, source_guild_id,
                    source_channel_id, source_message_id, uploader_user_id,
                    created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256, source_scope, source_message_id, uploader_user_id)
                DO UPDATE SET
                    filename = excluded.filename,
                    last_used_at = excluded.last_used_at
                """,
                (
                    digest,
                    clean_filename,
                    str(source_scope)[:160],
                    max(0, int(source_guild_id)),
                    max(0, int(source_channel_id)),
                    max(0, int(source_message_id or 0)),
                    max(0, int(uploader_user_id)),
                    timestamp,
                    timestamp,
                ),
            )
            self._conn.execute(
                "UPDATE attachments SET last_used_at = ? WHERE sha256 = ?",
                (timestamp, digest),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _clean_attachment_filename(filename: str) -> str:
        return (
            "".join(
                character for character in str(filename) if ord(character) >= 32
            ).strip()[:180]
            or "attachment"
        )

    def _privacy_allows_attachment_write_no_commit(
        self,
        guild_id: int,
        user_id: int,
        expected_revision: int | None,
    ) -> bool:
        if expected_revision is None:
            return True
        row = self._conn.execute(
            "SELECT opted_out, revision FROM privacy WHERE guild_id = ? AND user_id = ?",
            (max(0, int(guild_id)), max(0, int(user_id))),
        ).fetchone()
        opted_out = bool(row["opted_out"]) if row else False
        revision = int(row["revision"]) if row else 0
        return not opted_out and revision == max(0, int(expected_revision))

    def _trim_attachment_cache_no_commit(self) -> None:
        if self.max_attachments:
            self._conn.execute(
                """
                DELETE FROM attachments
                WHERE sha256 IN (
                    SELECT sha256 FROM attachments
                    ORDER BY last_used_at DESC, sha256 DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_attachments,),
            )
        if self.max_attachment_chunks:
            count = int(
                self._conn.execute("SELECT COUNT(*) FROM attachment_chunks").fetchone()[0]
            )
            excess = count - self.max_attachment_chunks
            if excess > 0:
                self._conn.execute(
                    """
                    DELETE FROM attachment_chunks
                    WHERE id IN (
                        SELECT chunks.id
                        FROM attachment_chunks AS chunks
                        JOIN attachments AS attachment
                            ON attachment.sha256 = chunks.sha256
                        ORDER BY attachment.last_used_at ASC, chunks.id ASC
                        LIMIT ?
                    )
                    """,
                    (excess,),
                )

    def save_attachment_result(
        self,
        *,
        sha256: str,
        filename: str,
        detected_type: str,
        size_bytes: int,
        source_scope: str,
        source_guild_id: int,
        source_channel_id: int,
        source_message_id: int | None,
        uploader_user_id: int,
        status: str,
        text: str,
        parser_version: str,
        confidence: float,
        error: str,
        chunks: Iterable[AttachmentChunkInput],
        page_count: int = 0,
        width: int = 0,
        height: int = 0,
        truncated: bool = False,
        expected_privacy_revision: int | None = None,
        created_at: int | None = None,
    ) -> bool:
        digest = self._validate_attachment_hash(sha256)
        if status not in {"ready", "error"}:
            raise ValueError("status must be ready or error")
        timestamp = int(time.time()) if created_at is None else int(created_at)
        clean_filename = self._clean_attachment_filename(filename)
        bounded_text = str(text)[:1_000_000]
        bounded_chunks = tuple(chunks)[:10_000]
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if not self._privacy_allows_attachment_write_no_commit(
                source_guild_id,
                uploader_user_id,
                expected_privacy_revision,
            ):
                self._conn.rollback()
                return False
            previous = self._conn.execute(
                "SELECT status FROM attachments WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if status == "error" or (
                previous is not None and str(previous["status"]) == "error"
            ):
                self._conn.execute(
                    "DELETE FROM attachment_sources WHERE sha256 = ?",
                    (digest,),
                )
            self._conn.execute(
                """
                INSERT INTO attachments(
                    sha256, filename, detected_type, size_bytes, status, text,
                    parser_version, confidence, error, page_count, width, height,
                    truncated, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    filename = excluded.filename,
                    detected_type = excluded.detected_type,
                    size_bytes = excluded.size_bytes,
                    status = excluded.status,
                    text = excluded.text,
                    parser_version = excluded.parser_version,
                    confidence = excluded.confidence,
                    error = excluded.error,
                    page_count = excluded.page_count,
                    width = excluded.width,
                    height = excluded.height,
                    truncated = excluded.truncated,
                    last_used_at = excluded.last_used_at
                """,
                (
                    digest,
                    "attachment",
                    str(detected_type)[:32],
                    max(0, int(size_bytes)),
                    status,
                    bounded_text,
                    str(parser_version)[:80],
                    max(0.0, min(1.0, float(confidence))),
                    str(error)[:300],
                    max(0, int(page_count)),
                    max(0, int(width)),
                    max(0, int(height)),
                    int(bool(truncated)),
                    timestamp,
                    timestamp,
                ),
            )
            self._conn.execute("DELETE FROM attachment_chunks WHERE sha256 = ?", (digest,))
            for chunk in bounded_chunks:
                clean_text = str(chunk.text).strip()[:100_000]
                if not clean_text:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO attachment_chunks(
                        sha256, chunk_index, text, page_number, heading,
                        keywords, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        max(0, int(chunk.chunk_index)),
                        clean_text,
                        (
                            max(1, int(chunk.page_number))
                            if chunk.page_number is not None
                            else None
                        ),
                        str(chunk.heading).strip()[:300],
                        str(chunk.keywords).strip()[:500],
                        timestamp,
                    ),
                )
            if status == "ready":
                self._conn.execute(
                    """
                    INSERT INTO attachment_sources(
                        sha256, filename, source_scope, source_guild_id,
                        source_channel_id, source_message_id, uploader_user_id,
                        created_at, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha256, source_scope, source_message_id, uploader_user_id)
                    DO UPDATE SET
                        filename = excluded.filename,
                        last_used_at = excluded.last_used_at
                    """,
                    (
                        digest,
                        clean_filename,
                        str(source_scope)[:160],
                        max(0, int(source_guild_id)),
                        max(0, int(source_channel_id)),
                        max(0, int(source_message_id or 0)),
                        max(0, int(uploader_user_id)),
                        timestamp,
                        timestamp,
                    ),
                )
            self._trim_attachment_cache_no_commit()
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _attachment_query_terms(query: str) -> tuple[str, ...]:
        stop_words = {
            "about",
            "attachment",
            "did",
            "does",
            "from",
            "have",
            "into",
            "or",
            "said",
            "say",
            "the",
            "this",
            "what",
            "with",
        }
        terms: list[str] = []
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,63}", query):
            normalized = token.casefold().strip("._+-")
            if len(normalized) < 2 or normalized in stop_words or normalized in terms:
                continue
            terms.append(normalized)
            if len(terms) >= 12:
                break
        return tuple(terms)

    def search_attachment_chunks(
        self,
        *,
        scope: str,
        query: str,
        limit: int,
        max_chars: int,
    ) -> list[AttachmentChunkRecord]:
        terms = self._attachment_query_terms(query)
        bounded_limit = max(0, min(8, int(limit)))
        remaining = max(0, int(max_chars))
        if not terms or bounded_limit <= 0 or remaining <= 0:
            return []
        match_query = " OR ".join(f'"{term}"' for term in terms)
        rows = self._conn.execute(
            """
            SELECT chunks.*,
                   COALESCE(
                       (
                           SELECT source.filename
                           FROM attachment_sources AS source
                           WHERE source.sha256 = chunks.sha256
                             AND source.source_scope = ?
                           ORDER BY source.last_used_at DESC, source.id DESC
                           LIMIT 1
                       ),
                       'attachment'
                   ) AS filename,
                   bm25(attachment_chunks_fts, 1.0, 0.6, 0.8) AS rank
            FROM attachment_chunks_fts
            JOIN attachment_chunks AS chunks
                ON chunks.id = attachment_chunks_fts.rowid
            JOIN attachments AS attachment
                ON attachment.sha256 = chunks.sha256
            WHERE attachment_chunks_fts MATCH ?
              AND attachment.status = 'ready'
              AND EXISTS (
                  SELECT 1 FROM attachment_sources AS source
                  WHERE source.sha256 = chunks.sha256
                    AND source.source_scope = ?
              )
            ORDER BY rank ASC, attachment.last_used_at DESC, chunks.id ASC
            LIMIT ?
            """,
            (
                str(scope)[:160],
                match_query,
                str(scope)[:160],
                bounded_limit * 3,
            ),
        ).fetchall()
        results: list[AttachmentChunkRecord] = []
        seen: set[int] = set()
        for row in rows:
            if len(results) >= bounded_limit or remaining <= 0:
                break
            row_id = int(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            text = str(row["text"])[:remaining]
            if not text:
                continue
            remaining -= len(text)
            results.append(
                AttachmentChunkRecord(
                    id=row_id,
                    sha256=str(row["sha256"]),
                    filename=str(row["filename"]),
                    chunk_index=int(row["chunk_index"]),
                    text=text,
                    page_number=(
                        int(row["page_number"])
                        if row["page_number"] is not None
                        else None
                    ),
                    heading=str(row["heading"]),
                    score=float(row["rank"]),
                )
            )
        if results:
            now = int(time.time())
            self._conn.executemany(
                "UPDATE attachments SET last_used_at = ? WHERE sha256 = ?",
                ((now, result.sha256) for result in results),
            )
            self._conn.commit()
        return results

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=int(row["id"]),
            scope=str(row["scope"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            user_id=int(row["user_id"]),
            author_name=str(row["author_name"]),
            role=str(row["role"]),
            content=str(row["content"]),
            created_at=int(row["created_at"]),
            discord_message_id=(
                int(row["discord_message_id"]) if row["discord_message_id"] is not None else None
            ),
            is_proactive=bool(row["is_proactive"]),
        )

    @staticmethod
    def _profile_record_from_row(row: sqlite3.Row) -> ProfileRecord:
        return ProfileRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            kind=str(row["kind"]),
            topic=str(row["topic"]),
            text=str(row["text"]),
            provenance=str(row["provenance"]),
            confidence=float(row["confidence"]),
            evidence_count=int(row["evidence_count"]),
            status=str(row["status"]),
            superseded_by_id=(
                int(row["superseded_by_id"])
                if row["superseded_by_id"] is not None
                else None
            ),
            source_scope=str(row["source_scope"]),
            source_guild_id=int(row["source_guild_id"]),
            source_channel_id=int(row["source_channel_id"]),
            source_message_id=(
                int(row["source_message_id"])
                if row["source_message_id"] is not None
                else None
            ),
            visibility=str(row["visibility"]),
            created_at=int(row["created_at"]),
            last_seen_at=int(row["last_seen_at"]),
        )

    @staticmethod
    def _journal_from_row(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            text=str(row["text"]),
            source_scope=str(row["source_scope"]),
            source_guild_id=int(row["source_guild_id"]),
            source_channel_id=int(row["source_channel_id"]),
            source_message_id=(
                int(row["source_message_id"])
                if row["source_message_id"] is not None
                else None
            ),
            visibility=str(row["visibility"]),
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def _relationship_from_row(row: sqlite3.Row) -> RelationshipState:
        return RelationshipState(
            user_id=int(row["user_id"]),
            interaction_count=int(row["interaction_count"]),
            affection=int(row["affection"]),
            trust=int(row["trust"]),
            respect=int(row["respect"]),
            amusement=int(row["amusement"]),
            curiosity=int(row["curiosity"]),
            tension=int(row["tension"]),
            annoyance=int(row["annoyance"]),
            wariness=int(row["wariness"]),
            summary=str(row["summary"]),
            last_interaction_at=int(row["last_interaction_at"]),
            last_reflected_at=int(row["last_reflected_at"]),
        )

    def record_message(
        self,
        *,
        scope: str,
        guild_id: int,
        channel_id: int,
        user_id: int,
        author_name: str,
        role: str,
        content: str,
        discord_message_id: int | None = None,
        created_at: int | None = None,
        is_proactive: bool = False,
    ) -> int | None:
        clean_content = (content or "").replace("\x00", "").strip()[:16_000]
        if not clean_content or role not in {"user", "assistant"}:
            return None
        now = int(created_at or time.time())
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO messages(
                scope, guild_id, channel_id, user_id, author_name, role,
                content, created_at, discord_message_id, is_proactive
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                guild_id,
                channel_id,
                user_id,
                author_name[:100],
                role,
                clean_content,
                now,
                discord_message_id,
                int(is_proactive),
            ),
        )
        if cursor.rowcount and self.max_messages_per_scope:
            self._trim_scope(scope, self.max_messages_per_scope)
        if cursor.rowcount and self.max_total_messages:
            self._global_message_checks_remaining -= 1
            if self._global_message_checks_remaining <= 0:
                self._trim_total_messages(self._global_message_trim_target)
                self._global_message_checks_remaining = self._global_message_check_interval
        self._conn.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None

    def _trim_scope(self, scope: str, limit: int) -> int:
        if limit <= 0:
            return 0
        cutoff = self._conn.execute(
            "SELECT id FROM messages WHERE scope = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (scope, limit),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = self._conn.execute(
            "DELETE FROM messages WHERE scope = ? AND id <= ?",
            (scope, int(cutoff["id"])),
        )
        return max(0, int(cursor.rowcount))

    def _trim_total_messages(self, limit: int) -> int:
        if limit <= 0:
            return 0
        cutoff = self._conn.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?",
            (limit,),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = self._conn.execute(
            "DELETE FROM messages WHERE id <= ?",
            (int(cutoff["id"]),),
        )
        return max(0, int(cursor.rowcount))

    def _trim_user_memories(self, guild_id: int, user_id: int, limit: int) -> int:
        if limit <= 0:
            return 0
        rows = self._conn.execute(
            """
            SELECT id FROM memories
            WHERE guild_id = ? AND user_id = ?
            ORDER BY importance DESC, last_used_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        ).fetchall()
        if len(rows) < limit:
            return 0
        keep_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in keep_ids)
        cursor = self._conn.execute(
            f"DELETE FROM memories WHERE guild_id = ? AND user_id = ? "
            f"AND id NOT IN ({placeholders})",
            [guild_id, user_id, *keep_ids],
        )
        return max(0, int(cursor.rowcount))

    def _trim_user_profile_facts(
        self,
        user_id: int,
        limit: int,
    ) -> int:
        if limit <= 0:
            return 0
        rows = self._conn.execute(
            """
            SELECT id FROM profile_facts
            WHERE user_id = ?
            ORDER BY
                CASE status WHEN 'confirmed' THEN 2 WHEN 'tentative' THEN 1 ELSE 0 END DESC,
                CASE provenance WHEN 'direct' THEN 1 ELSE 0 END DESC,
                evidence_count DESC, confidence DESC, last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        if len(rows) < limit:
            return 0
        keep_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in keep_ids)
        cursor = self._conn.execute(
            f"DELETE FROM profile_facts WHERE user_id = ? AND id NOT IN ({placeholders})",
            [user_id, *keep_ids],
        )
        return max(0, int(cursor.rowcount))

    def _trim_total_profile_facts(self, limit: int) -> int:
        if limit <= 0:
            return 0
        rows = self._conn.execute(
            """
            SELECT id FROM profile_facts
            ORDER BY
                CASE status WHEN 'confirmed' THEN 1 ELSE 0 END DESC,
                CASE provenance WHEN 'direct' THEN 1 ELSE 0 END DESC,
                evidence_count DESC, confidence DESC, last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if len(rows) < limit:
            return 0
        keep_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in keep_ids)
        cursor = self._conn.execute(
            f"DELETE FROM profile_facts WHERE id NOT IN ({placeholders})",
            keep_ids,
        )
        return max(0, int(cursor.rowcount))

    def _trim_user_journal(
        self,
        user_id: int,
        limit: int,
    ) -> int:
        if limit <= 0:
            return 0
        cutoff = self._conn.execute(
            """
            SELECT id FROM agent_journal
            WHERE user_id = ?
            ORDER BY id DESC LIMIT 1 OFFSET ?
            """,
            (user_id, limit),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = self._conn.execute(
            """
            DELETE FROM agent_journal
            WHERE user_id = ? AND id <= ?
            """,
            (user_id, int(cutoff["id"])),
        )
        return max(0, int(cursor.rowcount))

    def _trim_total_journal(self, limit: int) -> int:
        if limit <= 0:
            return 0
        cutoff = self._conn.execute(
            "SELECT id FROM agent_journal ORDER BY id DESC LIMIT 1 OFFSET ?",
            (limit,),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = self._conn.execute(
            "DELETE FROM agent_journal WHERE id <= ?",
            (int(cutoff["id"]),),
        )
        return max(0, int(cursor.rowcount))

    def _trim_user_relationship_events(
        self,
        guild_id: int,
        user_id: int,
        limit: int,
    ) -> int:
        if limit <= 0:
            return 0
        cutoff = self._conn.execute(
            """
            SELECT id FROM relationship_events
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 1 OFFSET ?
            """,
            (guild_id, user_id, limit),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = self._conn.execute(
            """
            DELETE FROM relationship_events
            WHERE guild_id = ? AND user_id = ? AND id <= ?
            """,
            (guild_id, user_id, int(cutoff["id"])),
        )
        return max(0, int(cursor.rowcount))

    def _trim_total_relationship_events(self, limit: int) -> int:
        if limit <= 0:
            return 0
        cutoff = self._conn.execute(
            "SELECT id FROM relationship_events ORDER BY id DESC LIMIT 1 OFFSET ?",
            (limit,),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = self._conn.execute(
            "DELETE FROM relationship_events WHERE id <= ?",
            (int(cutoff["id"]),),
        )
        return max(0, int(cursor.rowcount))

    def recent_messages(
        self,
        scope: str,
        limit: int,
        *,
        exclude_discord_message_id: int | None = None,
    ) -> list[MessageRecord]:
        if exclude_discord_message_id is None:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE scope = ? ORDER BY id DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE scope = ? AND (discord_message_id IS NULL OR discord_message_id != ?)
                ORDER BY id DESC LIMIT ?
                """,
                (scope, exclude_discord_message_id, limit),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def has_discord_message_id(self, scope: str, discord_message_id: int) -> bool:
        if discord_message_id <= 0:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE scope = ? AND discord_message_id = ? LIMIT 1",
            (scope, discord_message_id),
        ).fetchone()
        return row is not None

    def recall_messages(
        self,
        *,
        scope: str,
        user_id: int,
        query: str,
        limit: int,
        candidates: int,
        exclude_discord_message_id: int | None = None,
    ) -> list[tuple[MessageRecord, float]]:
        """Recall only the user's messages from the current channel/DM scope."""
        query_tokens = set(tokenize(query))
        if not query_tokens or limit <= 0:
            return []
        params: list[object] = [scope, user_id]
        exclusion = ""
        if exclude_discord_message_id is not None:
            exclusion = " AND (discord_message_id IS NULL OR discord_message_id != ?)"
            params.append(exclude_discord_message_id)
        params.append(candidates)
        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            WHERE scope = ? AND user_id = ? AND role = 'user'{exclusion}
            ORDER BY id DESC LIMIT ?
            """,
            params,
        ).fetchall()

        now = int(time.time())
        scored: list[tuple[MessageRecord, float]] = []
        for row in rows:
            message = self._message_from_row(row)
            message_tokens = set(tokenize(message.content, limit=40))
            overlap = len(query_tokens & message_tokens)
            if overlap == 0:
                continue
            coverage = overlap / max(len(query_tokens), 1)
            density = overlap / max(len(message_tokens), 1)
            age_days = max(0.0, (now - message.created_at) / 86_400)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = coverage * 0.65 + density * 0.20 + recency * 0.15
            scored.append((message, score))
        scored.sort(key=lambda pair: (pair[1], pair[0].id), reverse=True)
        return scored[:limit]

    @staticmethod
    def _memory_hash(text: str) -> str:
        normalized = " ".join(text.casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    def add_memory(
        self,
        *,
        guild_id: int,
        user_id: int,
        text: str,
        kind: str = "user_asserted",
        importance: int = 5,
        source_message_id: int | None = None,
    ) -> int | None:
        clean_text = " ".join((text or "").replace("\x00", "").split())[:500]
        if not clean_text:
            return None
        now = int(time.time())
        text_hash = self._memory_hash(clean_text)
        existing = self._conn.execute(
            "SELECT id FROM memories WHERE guild_id = ? AND user_id = ? AND text_hash = ?",
            (guild_id, user_id, text_hash),
        ).fetchone()
        if existing is None and self.max_total_memories:
            total = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            if total >= self.max_total_memories:
                return None
        cursor = self._conn.execute(
            """
            INSERT INTO memories(
                guild_id, user_id, kind, text, text_hash, importance,
                created_at, last_used_at, source_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, text_hash) DO UPDATE SET
                text = excluded.text,
                kind = excluded.kind,
                importance = MAX(memories.importance, excluded.importance),
                last_used_at = excluded.last_used_at,
                source_message_id = COALESCE(excluded.source_message_id, memories.source_message_id)
            """,
            (
                guild_id,
                user_id,
                kind[:40],
                clean_text,
                text_hash,
                max(1, min(10, importance)),
                now,
                now,
                source_message_id,
            ),
        )
        if self.max_memories_per_user:
            self._trim_user_memories(guild_id, user_id, self.max_memories_per_user)
        row = self._conn.execute(
            "SELECT id FROM memories WHERE guild_id = ? AND user_id = ? AND text_hash = ?",
            (guild_id, user_id, text_hash),
        ).fetchone()
        self._conn.commit()
        return int(row["id"]) if row else None

    def search_memories(
        self,
        *,
        guild_id: int,
        user_id: int,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        query_tokens = set(tokenize(query))
        if not query_tokens or limit <= 0:
            return []
        rows = self._conn.execute(
            """
            SELECT * FROM memories
            WHERE guild_id = ? AND user_id = ?
            ORDER BY importance DESC, last_used_at DESC
            LIMIT 100
            """,
            (guild_id, user_id),
        ).fetchall()
        now = int(time.time())
        scored: list[MemoryRecord] = []
        for row in rows:
            text = str(row["text"])
            memory_tokens = set(tokenize(text, limit=40))
            overlap = len(query_tokens & memory_tokens)
            if overlap == 0:
                continue
            coverage = overlap / max(len(query_tokens), 1)
            age_days = max(0.0, (now - int(row["last_used_at"])) / 86_400)
            recency = 1.0 / (1.0 + age_days / 90.0)
            importance = int(row["importance"]) / 10.0
            score = coverage * 0.65 + importance * 0.20 + recency * 0.15
            scored.append(
                MemoryRecord(
                    id=int(row["id"]),
                    guild_id=int(row["guild_id"]),
                    user_id=int(row["user_id"]),
                    kind=str(row["kind"]),
                    text=text,
                    importance=int(row["importance"]),
                    created_at=int(row["created_at"]),
                    last_used_at=int(row["last_used_at"]),
                    score=score,
                )
            )
        scored.sort(key=lambda memory: (memory.score, memory.id), reverse=True)
        chosen = scored[:limit]
        if chosen:
            self._conn.executemany(
                "UPDATE memories SET last_used_at = ? WHERE id = ?",
                [(now, memory.id) for memory in chosen],
            )
            self._conn.commit()
        return chosen

    def list_memories(self, guild_id: int, user_id: int, limit: int = 10) -> list[MemoryRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM memories
            WHERE guild_id = ? AND user_id = ?
            ORDER BY importance DESC, last_used_at DESC LIMIT ?
            """,
            (guild_id, user_id, limit),
        ).fetchall()
        return [
            MemoryRecord(
                id=int(row["id"]),
                guild_id=int(row["guild_id"]),
                user_id=int(row["user_id"]),
                kind=str(row["kind"]),
                text=str(row["text"]),
                importance=int(row["importance"]),
                created_at=int(row["created_at"]),
                last_used_at=int(row["last_used_at"]),
            )
            for row in rows
        ]

    def delete_memory(self, guild_id: int, user_id: int, memory_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM memories WHERE id = ? AND guild_id = ? AND user_id = ?",
            (memory_id, guild_id, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def _upsert_profile_record_no_commit(
        self,
        *,
        user_id: int,
        kind: str,
        topic: str,
        text: str,
        provenance: str,
        confidence: float,
        source_scope: str,
        source_guild_id: int,
        source_channel_id: int,
        source_message_id: int | None,
        visibility: str,
        now: int,
        evidence_increment: int = 1,
        force_confirmed: bool = False,
        supersedes_record_ids: Iterable[int] = (),
        contradicts_record_ids: Iterable[int] = (),
        record_id: int | None = None,
        created_at: int | None = None,
    ) -> int | None:
        clean_kind = kind.strip().casefold()
        clean_topic = sanitize_social_text(topic, 40).casefold()
        clean_text = sanitize_social_text(text, 320)
        clean_provenance = provenance.strip().casefold()
        clean_visibility = visibility.strip().casefold()
        if (
            user_id <= 0
            or not profile_observation_allowed(
                clean_kind,
                clean_topic,
                clean_text,
                clean_provenance,
            )
            or clean_visibility not in {"dm", "guild"}
        ):
            return None
        bounded_confidence = max(0.55, min(1.0, float(confidence)))
        evidence_increment = max(1, min(20, int(evidence_increment)))
        preferred_record_id = (
            int(record_id)
            if record_id is not None and int(record_id) > 0
            else None
        )
        created_timestamp = int(now if created_at is None else created_at)
        text_hash = self._memory_hash(clean_text)
        existing = self._conn.execute(
            """
            SELECT * FROM profile_facts
            WHERE user_id = ? AND kind = ? AND topic = ? AND text_hash = ?
            """,
            (user_id, clean_kind, clean_topic, text_hash),
        ).fetchone()
        if existing is None:
            if self.max_total_profile_facts:
                total = int(self._conn.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0])
                if total >= self.max_total_profile_facts:
                    return None
            status = (
                "confirmed"
                if force_confirmed or clean_provenance == "direct" or evidence_increment >= 2
                else "tentative"
            )
            cursor = self._conn.execute(
                """
                INSERT INTO profile_facts(
                    id, user_id, kind, topic, text, text_hash, provenance, confidence,
                    evidence_count, status, superseded_by_id, source_scope,
                    source_guild_id, source_channel_id, source_message_id,
                    visibility, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preferred_record_id,
                    user_id,
                    clean_kind,
                    clean_topic,
                    clean_text,
                    text_hash,
                    clean_provenance,
                    bounded_confidence,
                    evidence_increment,
                    status,
                    source_scope[:120],
                    source_guild_id,
                    max(0, int(source_channel_id)),
                    source_message_id,
                    clean_visibility,
                    created_timestamp,
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
        else:
            previous_provenance = str(existing["provenance"])
            merged_provenance = (
                "direct"
                if clean_provenance == "direct" or previous_provenance == "direct"
                else "inferred"
            )
            evidence_count = min(
                20, int(existing["evidence_count"]) + evidence_increment
            )
            confidence_out = max(float(existing["confidence"]), bounded_confidence)
            status = "tentative"
            if force_confirmed or merged_provenance == "direct" or (
                evidence_count >= 2 and confidence_out >= 0.65
            ):
                status = "confirmed"
            record_id = int(existing["id"])
            created_timestamp = min(
                int(existing["created_at"]),
                created_timestamp,
            )
            previous_last_seen = int(existing["last_seen_at"])
            incoming_is_latest = int(now) >= previous_last_seen
            source_scope_out = (
                source_scope[:120]
                if incoming_is_latest
                else str(existing["source_scope"])
            )
            source_guild_id_out = (
                source_guild_id
                if incoming_is_latest
                else int(existing["source_guild_id"])
            )
            source_channel_id_out = (
                max(0, int(source_channel_id))
                if incoming_is_latest
                else int(existing["source_channel_id"])
            )
            source_message_id_out = (
                source_message_id
                if incoming_is_latest
                else (
                    int(existing["source_message_id"])
                    if existing["source_message_id"] is not None
                    else None
                )
            )
            visibility_out = (
                clean_visibility
                if incoming_is_latest
                else str(existing["visibility"])
            )
            self._conn.execute(
                """
                UPDATE profile_facts
                SET text = ?, provenance = ?, confidence = ?, evidence_count = ?,
                    status = ?, superseded_by_id = NULL, source_scope = ?,
                    source_guild_id = ?, source_channel_id = ?, source_message_id = ?,
                    visibility = ?, created_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    clean_text,
                    merged_provenance,
                    confidence_out,
                    evidence_count,
                    status,
                    source_scope_out,
                    source_guild_id_out,
                    source_channel_id_out,
                    source_message_id_out,
                    visibility_out,
                    created_timestamp,
                    max(previous_last_seen, int(now)),
                    record_id,
                ),
            )

        def bounded_record_ids(values: Iterable[int], excluded: set[int]) -> tuple[int, ...]:
            result: list[int] = []
            for item in values:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value <= 0 or value in excluded or value in result:
                    continue
                result.append(value)
                if len(result) == 4:
                    break
            return tuple(result)

        supersedes = bounded_record_ids(supersedes_record_ids, {record_id})
        contradicts = bounded_record_ids(
            contradicts_record_ids, {record_id, *supersedes}
        )
        if supersedes:
            placeholders = ",".join("?" for _ in supersedes)
            self._conn.execute(
                f"UPDATE profile_facts SET status = 'superseded', superseded_by_id = ? "
                f"WHERE user_id = ? AND kind = ? AND topic = ? "
                f"AND status IN ('tentative', 'confirmed') "
                f"AND id IN ({placeholders})",
                (record_id, user_id, clean_kind, clean_topic, *supersedes),
            )
        if contradicts:
            placeholders = ",".join("?" for _ in contradicts)
            self._conn.execute(
                f"UPDATE profile_facts SET status = 'contradicted', superseded_by_id = NULL "
                f"WHERE user_id = ? AND kind = ? AND topic = ? "
                f"AND status IN ('tentative', 'confirmed') "
                f"AND id IN ({placeholders})",
                (user_id, clean_kind, clean_topic, *contradicts),
            )
        if self.max_profile_facts_per_user:
            self._trim_user_profile_facts(user_id, self.max_profile_facts_per_user)
        return record_id

    def add_profile_record(
        self,
        *,
        user_id: int,
        kind: str,
        topic: str,
        text: str,
        provenance: str,
        confidence: float,
        source_scope: str,
        source_guild_id: int,
        source_channel_id: int = 0,
        source_message_id: int | None = None,
        visibility: str | None = None,
        supersedes_record_ids: Iterable[int] = (),
        contradicts_record_ids: Iterable[int] = (),
    ) -> int | None:
        record_id = self._upsert_profile_record_no_commit(
            user_id=user_id,
            kind=kind,
            topic=topic,
            text=text,
            provenance=provenance,
            confidence=confidence,
            source_scope=source_scope,
            source_guild_id=source_guild_id,
            source_channel_id=source_channel_id,
            source_message_id=source_message_id,
            visibility=visibility or ("dm" if source_guild_id == 0 else "guild"),
            now=int(time.time()),
            supersedes_record_ids=supersedes_record_ids,
            contradicts_record_ids=contradicts_record_ids,
        )
        self._conn.commit()
        return record_id

    def list_profile_records(
        self,
        *,
        user_id: int,
        limit: int = 10,
        include_inactive: bool = False,
        visibility: str | None = None,
    ) -> list[ProfileRecord]:
        if limit <= 0:
            return []
        status_filter = "" if include_inactive else " AND status IN ('tentative', 'confirmed')"
        if visibility is None:
            visibility_filter = ""
            params: tuple[object, ...] = (user_id, limit)
        else:
            clean_visibility = visibility.strip().casefold()
            if clean_visibility not in {"dm", "guild"}:
                return []
            visibility_filter = " AND visibility = ?"
            params = (user_id, clean_visibility, limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM profile_facts
            WHERE user_id = ?{status_filter}{visibility_filter}
            ORDER BY
                CASE status WHEN 'confirmed' THEN 2 WHEN 'tentative' THEN 1 ELSE 0 END DESC,
                CASE provenance WHEN 'direct' THEN 1 ELSE 0 END DESC,
                evidence_count DESC, confidence DESC, last_seen_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._profile_record_from_row(row) for row in rows]

    def profile_records_for_context(
        self,
        *,
        guild_id: int,
        user_id: int,
        is_dm: bool,
        limit: int = 10,
    ) -> list[ProfileRecord]:
        if limit <= 0:
            return []
        del guild_id
        if is_dm:
            context_clause = ""
        else:
            # Public observations follow the user's global identity across guilds.
            # DM observations never cross the DM boundary.
            context_clause = " AND visibility = 'guild'"
        params: tuple[object, ...] = (user_id, limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM profile_facts
            WHERE user_id = ? AND status IN ('tentative', 'confirmed'){context_clause}
            ORDER BY CASE status WHEN 'confirmed' THEN 1 ELSE 0 END DESC,
                     evidence_count DESC, confidence DESC, last_seen_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._profile_record_from_row(row) for row in rows]

    def profile_revision(self, user_id: int) -> int:
        row = self._conn.execute(
            "SELECT revision FROM profile_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["revision"]) if row else 0

    def _bump_profile_revision_no_commit(self, user_id: int) -> int:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO profile_state(user_id, revision, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                revision = profile_state.revision + 1,
                updated_at = excluded.updated_at
            """,
            (user_id, now),
        )
        return self.profile_revision(user_id)

    def delete_social_record(self, *, user_id: int, record_id: str) -> bool:
        kind, separator, raw_id = record_id.strip().partition(":")
        if not separator or kind not in {"profile", "journal"}:
            return False
        try:
            numeric_id = int(raw_id)
        except ValueError:
            return False
        if numeric_id <= 0:
            return False
        table = "profile_facts" if kind == "profile" else "agent_journal"
        cursor = self._conn.execute(
            f'DELETE FROM "{table}" WHERE id = ? AND user_id = ?',
            (numeric_id, user_id),
        )
        if cursor.rowcount:
            if kind == "profile":
                self._conn.execute(
                    """
                    UPDATE profile_facts
                    SET superseded_by_id = NULL
                    WHERE user_id = ? AND superseded_by_id = ?
                    """,
                    (user_id, numeric_id),
                )
            self._bump_profile_revision_no_commit(user_id)
        self._conn.commit()
        return cursor.rowcount > 0

    def relationship_state(
        self,
        *,
        user_id: int,
    ) -> RelationshipState:
        row = self._conn.execute(
            "SELECT * FROM relationships WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return self._relationship_from_row(row)
        return RelationshipState(
            user_id=user_id,
            interaction_count=0,
            affection=0,
            trust=0,
            respect=0,
            amusement=0,
            curiosity=0,
            tension=0,
            annoyance=0,
            wariness=0,
            summary="",
            last_interaction_at=0,
            last_reflected_at=0,
        )

    def record_relationship_interaction(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        scope: str,
        user_text: str,
        assistant_text: str,
        source_message_id: int | None = None,
        meaningful: bool = False,
        created_at: int | None = None,
    ) -> RelationshipState:
        existing = self._conn.execute(
            "SELECT 1 FROM relationships WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not existing and self.max_total_relationships:
            relationship_count = int(
                self._conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
            )
            if relationship_count >= self.max_total_relationships:
                return self.relationship_state(user_id=user_id)
        now = int(created_at or time.time())
        clean_user = sanitize_social_text(user_text, 650)
        clean_assistant = sanitize_social_text(assistant_text, 650)
        if not social_text_allowed(clean_user):
            clean_user = "[content omitted from social reflection]"
        if not social_text_allowed(clean_assistant):
            clean_assistant = "[response omitted from social reflection]"

        self._conn.execute(
            """
            INSERT INTO relationships(
                user_id, interaction_count, affection, trust, respect, amusement,
                curiosity, tension, annoyance, wariness, summary,
                last_interaction_at, last_reflected_at, updated_at
            ) VALUES (?, 1, 0, 0, 0, 0, 0, 0, 0, 0, '', ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                interaction_count = MIN(2147483647, relationships.interaction_count + 1),
                last_interaction_at = excluded.last_interaction_at,
                updated_at = excluded.updated_at
            """,
            (user_id, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO relationship_events(
                guild_id, channel_id, user_id, scope, user_text, assistant_text,
                source_message_id, meaningful, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                max(0, int(channel_id)),
                user_id,
                scope[:120],
                clean_user,
                clean_assistant,
                source_message_id,
                int(bool(meaningful)),
                now,
            ),
        )
        if self.max_pending_interactions_per_user:
            self._trim_user_relationship_events(
                guild_id,
                user_id,
                self.max_pending_interactions_per_user,
            )
        if self.max_total_pending_interactions:
            self._trim_total_relationship_events(self.max_total_pending_interactions)
        self._conn.commit()
        return self.relationship_state(user_id=user_id)

    def pending_relationship_interactions(
        self,
        *,
        guild_id: int,
        user_id: int,
    ) -> int:
        return int(
            self._conn.execute(
                """
                SELECT COUNT(*) FROM relationship_events
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()[0]
        )

    def relationship_reflection_due(
        self,
        *,
        guild_id: int,
        user_id: int,
        reflect_every: int,
        meaningful_event_threshold: int,
        min_seconds: int,
        now: int | None = None,
    ) -> bool:
        state = self.relationship_state(user_id=user_id)
        if state.interaction_count == 0:
            return False
        counts = self._conn.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(meaningful), 0) AS meaningful
            FROM relationship_events WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        pending = int(counts["total"])
        meaningful = int(counts["meaningful"])
        current = int(now or time.time())
        threshold_reached = pending >= max(2, reflect_every) or meaningful >= max(
            1, meaningful_event_threshold
        )
        return threshold_reached and (
            state.last_reflected_at == 0 or current - state.last_reflected_at >= max(0, min_seconds)
        )

    def due_relationship_users(
        self,
        *,
        reflect_every: int,
        meaningful_event_threshold: int,
        min_seconds: int,
        limit: int = 10,
        now: int | None = None,
    ) -> list[tuple[int, int]]:
        if limit <= 0:
            return []
        cutoff = int(now or time.time()) - max(0, min_seconds)
        rows = self._conn.execute(
            """
            SELECT e.guild_id, e.user_id
            FROM relationship_events AS e
            JOIN relationships AS r
              ON r.user_id = e.user_id
            WHERE (r.last_reflected_at = 0 OR r.last_reflected_at <= ?)
            GROUP BY e.guild_id, e.user_id
            HAVING COUNT(*) >= ? OR SUM(e.meaningful) >= ?
            ORDER BY MIN(e.id) ASC
            LIMIT ?
            """,
            (
                cutoff,
                max(2, reflect_every),
                max(1, meaningful_event_threshold),
                limit,
            ),
        ).fetchall()
        return [(int(row["guild_id"]), int(row["user_id"])) for row in rows]

    def relationship_reflection_batch(
        self,
        *,
        guild_id: int,
        user_id: int,
        max_events: int,
    ) -> RelationshipReflectionBatch | None:
        if max_events <= 0:
            return None
        rows = self._conn.execute(
            """
            SELECT * FROM relationship_events
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id ASC LIMIT ?
            """,
            (guild_id, user_id, max_events),
        ).fetchall()
        if not rows:
            return None
        events = tuple(
            InteractionEvent(
                id=int(row["id"]),
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                scope=str(row["scope"]),
                user_text=str(row["user_text"]),
                assistant_text=str(row["assistant_text"]),
                source_message_id=(
                    int(row["source_message_id"])
                    if row["source_message_id"] is not None
                    else None
                ),
                meaningful=bool(row["meaningful"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
        )
        return RelationshipReflectionBatch(
            guild_id=guild_id,
            user_id=user_id,
            profile_revision=self.profile_revision(user_id),
            relationship=self.relationship_state(user_id=user_id),
            events=events,
            through_event_id=events[-1].id,
        )

    def recent_journal_entries(
        self,
        *,
        user_id: int,
        limit: int = 3,
        guild_id: int | None = None,
        is_dm: bool | None = None,
    ) -> list[JournalEntry]:
        if limit <= 0:
            return []
        if guild_id is not None and is_dm is False:
            context_clause = " AND visibility = 'guild'"
            params: tuple[object, ...] = (user_id, limit)
        else:
            context_clause = ""
            params = (user_id, limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM agent_journal
            WHERE user_id = ?{context_clause}
            ORDER BY id DESC LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._journal_from_row(row) for row in rows]

    def save_compact_reflection(
        self,
        *,
        batch: RelationshipReflectionBatch,
        observations: Iterable[ProfileObservation],
        journal_entry: str,
    ) -> bool:
        """Atomically save lean inferred continuity and consume its event batch.

        The compact path intentionally leaves legacy relationship dimensions and
        summary text unchanged. Existing schema-6 rows therefore remain readable
        by rollback builds while new reflection output has no score contract.
        """

        if not batch.events:
            return False

        compact_observations: list[ProfileObservation] = []
        seen: set[tuple[str, str]] = set()
        for index, observation in enumerate(observations):
            if index >= 8 or len(compact_observations) >= 3:
                break
            if (
                observation.kind.strip().casefold() != "impression"
                or observation.provenance.strip().casefold() != "inferred"
            ):
                continue
            topic = sanitize_social_text(observation.topic, 32).casefold()
            text = sanitize_social_text(observation.text, 220)
            if not profile_observation_allowed(
                "impression",
                topic,
                text,
                "inferred",
            ):
                continue
            try:
                confidence = float(observation.confidence)
            except (TypeError, ValueError):
                continue
            if not 0.55 <= confidence <= 1.0:
                continue
            key = (topic, " ".join(text.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            compact_observations.append(
                ProfileObservation(
                    kind="impression",
                    topic=topic,
                    text=text,
                    provenance="inferred",
                    confidence=round(confidence, 3),
                )
            )

        clean_journal = normalize_compact_journal(journal_entry)
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if self.profile_revision(batch.user_id) != batch.profile_revision:
                self._conn.rollback()
                return False
            remaining = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM relationship_events
                    WHERE guild_id = ? AND user_id = ? AND id <= ?
                    """,
                    (
                        batch.guild_id,
                        batch.user_id,
                        batch.through_event_id,
                    ),
                ).fetchone()[0]
            )
            if remaining == 0:
                self._conn.rollback()
                return False

            source_event = batch.events[-1]
            visibility = "dm" if source_event.guild_id == 0 else "guild"
            for observation in compact_observations:
                self._upsert_profile_record_no_commit(
                    user_id=batch.user_id,
                    kind="impression",
                    topic=observation.topic,
                    text=observation.text,
                    provenance="inferred",
                    confidence=observation.confidence,
                    source_scope=source_event.scope,
                    source_guild_id=source_event.guild_id,
                    source_channel_id=source_event.channel_id,
                    source_message_id=source_event.source_message_id,
                    visibility=visibility,
                    now=now,
                )

            # Keep the legacy row current without letting compact reflection
            # create or mutate any relationship dimensions or summary prose.
            self._conn.execute(
                """
                INSERT INTO relationships(
                    user_id, interaction_count, affection, trust, respect,
                    amusement, curiosity, tension, annoyance, wariness, summary,
                    last_interaction_at, last_reflected_at, updated_at
                ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_reflected_at = excluded.last_reflected_at,
                    updated_at = excluded.updated_at
                """,
                (batch.user_id, now, now),
            )
            if clean_journal:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_journal(
                        user_id, text, source_scope, source_guild_id,
                        source_channel_id, source_message_id, visibility,
                        source_through_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.user_id,
                        clean_journal,
                        source_event.scope[:120],
                        source_event.guild_id,
                        source_event.channel_id,
                        source_event.source_message_id,
                        visibility,
                        batch.through_event_id,
                        now,
                    ),
                )
            self._conn.execute(
                """
                DELETE FROM relationship_events
                WHERE guild_id = ? AND user_id = ? AND id <= ?
                """,
                (
                    batch.guild_id,
                    batch.user_id,
                    batch.through_event_id,
                ),
            )
            if self.max_profile_facts_per_user:
                self._trim_user_profile_facts(
                    batch.user_id,
                    self.max_profile_facts_per_user,
                )
            if self.max_journal_entries_per_user:
                self._trim_user_journal(
                    batch.user_id,
                    self.max_journal_entries_per_user,
                )
            if self.max_total_profile_facts:
                self._trim_total_profile_facts(self.max_total_profile_facts)
            if self.max_total_journal_entries:
                self._trim_total_journal(self.max_total_journal_entries)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def save_relationship_reflection(
        self,
        *,
        batch: RelationshipReflectionBatch,
        observations: Iterable[ProfileObservation],
        journal_entry: str,
        journal_source_event_id: int | None = None,
        relationship_deltas: dict[str, int],
        relationship_summary: str,
        mutable_record_ids: Iterable[int] = (),
    ) -> bool:
        now = int(time.time())
        event_by_id = {event.id: event for event in batch.events}
        journal_event = event_by_id.get(
            journal_source_event_id
            if isinstance(journal_source_event_id, int)
            and not isinstance(journal_source_event_id, bool)
            else -1
        )
        clean_journal = (
            normalize_compact_journal(journal_entry) if journal_event else ""
        )
        clean_summary = sanitize_social_text(relationship_summary, 400)
        if not social_text_allowed(clean_summary):
            clean_summary = ""
        bounded_deltas = {
            name: max(-1, min(1, int(relationship_deltas.get(name, 0))))
            for name in RELATIONSHIP_DIMENSIONS
        }
        mutable_ids = {
            int(item)
            for item in mutable_record_ids
            if not isinstance(item, bool) and isinstance(item, int) and int(item) > 0
        }

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if self.profile_revision(batch.user_id) != batch.profile_revision:
                self._conn.rollback()
                return False
            remaining = self._conn.execute(
                """
                SELECT COUNT(*) FROM relationship_events
                WHERE guild_id = ? AND user_id = ? AND id <= ?
                """,
                (
                    batch.guild_id,
                    batch.user_id,
                    batch.through_event_id,
                ),
            ).fetchone()[0]
            if int(remaining) == 0:
                self._conn.rollback()
                return False

            for observation in observations:
                source_event = event_by_id.get(observation.source_event_id or -1)
                if source_event is None:
                    continue
                if observation.kind == "fact" and not direct_evidence_matches(
                    source_event.user_text,
                    observation.evidence_quote,
                ):
                    continue
                visibility = "dm" if source_event.guild_id == 0 else "guild"
                self._upsert_profile_record_no_commit(
                    user_id=batch.user_id,
                    kind=observation.kind,
                    topic=observation.topic,
                    text=observation.text,
                    provenance=observation.provenance,
                    confidence=observation.confidence,
                    source_scope=source_event.scope,
                    source_guild_id=source_event.guild_id,
                    source_channel_id=source_event.channel_id,
                    source_message_id=source_event.source_message_id,
                    visibility=visibility,
                    now=now,
                    supersedes_record_ids=(
                        item for item in observation.supersedes_record_ids if item in mutable_ids
                    ),
                    contradicts_record_ids=(
                        item for item in observation.contradicts_record_ids if item in mutable_ids
                    ),
                )

            self._conn.execute(
                """
                INSERT INTO relationships(
                    user_id, interaction_count, affection, trust, respect, amusement,
                    curiosity, tension, annoyance, wariness, summary,
                    last_interaction_at, last_reflected_at, updated_at
                ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    affection = MAX(-20, MIN(20, relationships.affection + ?)),
                    trust = MAX(-20, MIN(20, relationships.trust + ?)),
                    respect = MAX(-20, MIN(20, relationships.respect + ?)),
                    amusement = MAX(-20, MIN(20, relationships.amusement + ?)),
                    curiosity = MAX(-20, MIN(20, relationships.curiosity + ?)),
                    tension = MAX(-20, MIN(20, relationships.tension + ?)),
                    annoyance = MAX(-20, MIN(20, relationships.annoyance + ?)),
                    wariness = MAX(-20, MIN(20, relationships.wariness + ?)),
                    summary = CASE WHEN ? != '' THEN ? ELSE relationships.summary END,
                    last_reflected_at = excluded.last_reflected_at,
                    updated_at = excluded.updated_at
                """,
                (
                    batch.user_id,
                    *(bounded_deltas[name] for name in RELATIONSHIP_DIMENSIONS),
                    clean_summary,
                    now,
                    now,
                    *(bounded_deltas[name] for name in RELATIONSHIP_DIMENSIONS),
                    clean_summary,
                    clean_summary,
                ),
            )
            if clean_journal:
                assert journal_event is not None
                journal_visibility = "dm" if journal_event.guild_id == 0 else "guild"
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_journal(
                        user_id, text, source_scope, source_guild_id,
                        source_channel_id, source_message_id, visibility,
                        source_through_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.user_id,
                        clean_journal,
                        journal_event.scope[:120],
                        journal_event.guild_id,
                        journal_event.channel_id,
                        journal_event.source_message_id,
                        journal_visibility,
                        batch.through_event_id,
                        now,
                    ),
                )
            self._conn.execute(
                """
                DELETE FROM relationship_events
                WHERE guild_id = ? AND user_id = ? AND id <= ?
                """,
                (
                    batch.guild_id,
                    batch.user_id,
                    batch.through_event_id,
                ),
            )
            if self.max_profile_facts_per_user:
                self._trim_user_profile_facts(batch.user_id, self.max_profile_facts_per_user)
            if self.max_journal_entries_per_user:
                self._trim_user_journal(batch.user_id, self.max_journal_entries_per_user)
            if self.max_total_profile_facts:
                self._trim_total_profile_facts(self.max_total_profile_facts)
            if self.max_total_journal_entries:
                self._trim_total_journal(self.max_total_journal_entries)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _length_bucket(length: int) -> int:
        value = max(0, int(length))
        for boundary in (0, 40, 120, 240, 500, 1000, 2000, 4000):
            if value <= boundary:
                return boundary
        return 8000

    def record_interaction_metric(
        self,
        *,
        conversation_type: str,
        user_chars: int,
        assistant_chars: int,
        directed: bool,
        meaningful_social: bool,
        group_signal: bool,
    ) -> None:
        """Record identifier-free outcome shape for threshold tuning."""
        kind = "dm" if conversation_type == "dm" else "guild"
        self._conn.execute(
            """
            INSERT INTO interaction_metrics(
                conversation_type, user_chars_bucket, assistant_chars_bucket,
                directed, meaningful_social, group_signal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                self._length_bucket(user_chars),
                self._length_bucket(assistant_chars),
                int(bool(directed)),
                int(bool(meaningful_social)),
                int(bool(group_signal)),
            ),
        )
        if self.max_interaction_metrics:
            self._conn.execute(
                """
                DELETE FROM interaction_metrics
                WHERE id <= COALESCE((
                    SELECT id FROM interaction_metrics
                    ORDER BY id DESC LIMIT 1 OFFSET ?
                ), 0)
                """,
                (self.max_interaction_metrics,),
            )
        self._conn.commit()

    def interaction_metric_summary(self) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS samples,
                   COALESCE(SUM(directed), 0) AS directed,
                   COALESCE(SUM(meaningful_social), 0) AS meaningful_social,
                   COALESCE(SUM(group_signal), 0) AS group_signal,
                   COALESCE(SUM(CASE WHEN conversation_type = 'guild' THEN 1 ELSE 0 END), 0)
                       AS guild
            FROM interaction_metrics
            """
        ).fetchone()
        return {name: int(row[name]) for name in (
            "samples", "directed", "meaningful_social", "group_signal", "guild"
        )}

    def group_continuity_state(self, *, guild_id: int) -> GroupContinuityState:
        row = self._conn.execute(
            "SELECT * FROM guild_continuity WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row:
            return GroupContinuityState(
                guild_id=int(row["guild_id"]),
                summary=str(row["summary"]),
                interaction_count=int(row["interaction_count"]),
                last_interaction_at=int(row["last_interaction_at"]),
                last_reflected_at=int(row["last_reflected_at"]),
                source_through_event_id=int(row["source_through_event_id"]),
            )
        return GroupContinuityState(guild_id, "", 0, 0, 0, 0)

    def recent_group_journal(
        self, *, guild_id: int, limit: int = 3
    ) -> list[GroupJournalEntry]:
        if guild_id <= 0 or limit <= 0:
            return []
        rows = self._conn.execute(
            """
            SELECT id, guild_id, kind, text, created_at
            FROM guild_group_journal
            WHERE guild_id = ? ORDER BY id DESC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return [
            GroupJournalEntry(
                id=int(row["id"]),
                guild_id=int(row["guild_id"]),
                kind=str(row["kind"]),
                text=str(row["text"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
        ]

    def group_guilds_for_user(self, *, user_id: int) -> list[int]:
        """Guilds whose pending or derived group continuity can contain this user."""
        rows = self._conn.execute(
            """
            SELECT guild_id FROM guild_continuity_members WHERE user_id = ?
            UNION
            SELECT guild_id FROM guild_group_events WHERE user_id = ?
            ORDER BY guild_id
            """,
            (user_id, user_id),
        ).fetchall()
        return [int(row["guild_id"]) for row in rows]

    def _trim_group_events(self, guild_id: int, limit: int) -> int:
        if limit <= 0:
            return 0
        cursor = self._conn.execute(
            """
            DELETE FROM guild_group_events
            WHERE guild_id = ? AND id NOT IN (
                SELECT id FROM guild_group_events
                WHERE guild_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (guild_id, guild_id, limit),
        )
        return int(cursor.rowcount)

    def _trim_total_group_events(self, limit: int) -> int:
        if limit <= 0:
            return 0
        cursor = self._conn.execute(
            """
            DELETE FROM guild_group_events
            WHERE id <= COALESCE((
                SELECT id FROM guild_group_events ORDER BY id DESC LIMIT 1 OFFSET ?
            ), 0)
            """,
            (limit,),
        )
        return int(cursor.rowcount)

    def _trim_group_journal(self, guild_id: int, limit: int) -> int:
        if limit <= 0:
            return 0
        cursor = self._conn.execute(
            """
            DELETE FROM guild_group_journal
            WHERE guild_id = ? AND id NOT IN (
                SELECT id FROM guild_group_journal
                WHERE guild_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (guild_id, guild_id, limit),
        )
        return int(cursor.rowcount)

    def _trim_total_group_journal(self, limit: int) -> int:
        if limit <= 0:
            return 0
        cursor = self._conn.execute(
            """
            DELETE FROM guild_group_journal
            WHERE id <= COALESCE((
                SELECT id FROM guild_group_journal ORDER BY id DESC LIMIT 1 OFFSET ?
            ), 0)
            """,
            (limit,),
        )
        return int(cursor.rowcount)

    def record_group_interaction(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        scope: str,
        user_text: str,
        assistant_text: str,
        source_message_id: int | None = None,
        meaningful: bool = False,
        created_at: int | None = None,
    ) -> bool:
        """Queue a successful public interaction for guild-only reflection."""
        if guild_id <= 0:
            return False
        existing = self._conn.execute(
            "SELECT 1 FROM guild_continuity WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if not existing and self.max_group_continuities:
            count = int(
                self._conn.execute("SELECT COUNT(*) FROM guild_continuity").fetchone()[0]
            )
            if count >= self.max_group_continuities:
                return False
        clean_user = sanitize_social_text(user_text, 650)
        clean_assistant = sanitize_social_text(assistant_text, 650)
        if not social_text_allowed(clean_user):
            clean_user = "[content omitted from group reflection]"
        if not social_text_allowed(clean_assistant):
            clean_assistant = "[response omitted from group reflection]"
        now = int(created_at or time.time())
        self._conn.execute(
            """
            INSERT INTO guild_continuity(
                guild_id, summary, interaction_count, last_interaction_at,
                last_reflected_at, source_through_event_id, updated_at
            ) VALUES (?, '', 1, ?, 0, 0, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                interaction_count = MIN(2147483647, guild_continuity.interaction_count + 1),
                last_interaction_at = excluded.last_interaction_at,
                updated_at = excluded.updated_at
            """,
            (guild_id, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO guild_group_events(
                guild_id, channel_id, user_id, scope, user_text, assistant_text,
                source_message_id, meaningful, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                max(0, int(channel_id)),
                user_id,
                scope[:120],
                clean_user,
                clean_assistant,
                source_message_id,
                int(bool(meaningful)),
                now,
            ),
        )
        if self.max_group_events_per_guild:
            self._trim_group_events(guild_id, self.max_group_events_per_guild)
        if self.max_total_group_events:
            self._trim_total_group_events(self.max_total_group_events)
        self._conn.commit()
        return True

    def group_reflection_due(
        self,
        *,
        guild_id: int,
        reflect_every: int,
        meaningful_event_threshold: int,
        min_seconds: int,
        now: int | None = None,
    ) -> bool:
        if guild_id <= 0:
            return False
        state = self.group_continuity_state(guild_id=guild_id)
        counts = self._conn.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(meaningful), 0) AS meaningful
            FROM guild_group_events WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        threshold = int(counts["total"]) >= max(2, reflect_every) or int(
            counts["meaningful"]
        ) >= max(1, meaningful_event_threshold)
        current = int(now or time.time())
        return threshold and (
            state.last_reflected_at == 0
            or current - state.last_reflected_at >= max(0, min_seconds)
        )

    def due_group_guilds(
        self,
        *,
        reflect_every: int,
        meaningful_event_threshold: int,
        min_seconds: int,
        limit: int = 10,
        now: int | None = None,
    ) -> list[int]:
        if limit <= 0:
            return []
        cutoff = int(now or time.time()) - max(0, min_seconds)
        rows = self._conn.execute(
            """
            SELECT e.guild_id
            FROM guild_group_events AS e
            JOIN guild_continuity AS c ON c.guild_id = e.guild_id
            WHERE c.last_reflected_at = 0 OR c.last_reflected_at <= ?
            GROUP BY e.guild_id
            HAVING COUNT(*) >= ? OR SUM(e.meaningful) >= ?
            ORDER BY MIN(e.id) ASC LIMIT ?
            """,
            (cutoff, max(2, reflect_every), max(1, meaningful_event_threshold), limit),
        ).fetchall()
        return [int(row["guild_id"]) for row in rows]

    def group_reflection_batch(
        self, *, guild_id: int, max_events: int, journal_limit: int = 6
    ) -> GroupReflectionBatch | None:
        if guild_id <= 0 or max_events <= 0:
            return None
        rows = self._conn.execute(
            """
            SELECT * FROM guild_group_events
            WHERE guild_id = ? ORDER BY id ASC LIMIT ?
            """,
            (guild_id, max_events),
        ).fetchall()
        if not rows:
            return None
        events = tuple(
            GroupInteractionEvent(
                id=int(row["id"]),
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                user_id=int(row["user_id"]),
                scope=str(row["scope"]),
                user_text=str(row["user_text"]),
                assistant_text=str(row["assistant_text"]),
                source_message_id=(
                    int(row["source_message_id"])
                    if row["source_message_id"] is not None else None
                ),
                meaningful=bool(row["meaningful"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
        )
        revisions: list[tuple[int, int]] = []
        for user_id in sorted({event.user_id for event in events}):
            revisions.append((user_id, self.profile_revision(user_id)))
        return GroupReflectionBatch(
            guild_id=guild_id,
            participant_revisions=tuple(revisions),
            continuity=self.group_continuity_state(guild_id=guild_id),
            journal=tuple(self.recent_group_journal(guild_id=guild_id, limit=journal_limit)),
            events=events,
            through_event_id=events[-1].id,
        )

    def save_group_reflection(
        self,
        *,
        batch: GroupReflectionBatch,
        summary: str,
        observations: Iterable[GroupObservation],
    ) -> bool:
        clean_summary = sanitize_social_text(summary, 600)
        if not social_text_allowed(clean_summary):
            clean_summary = ""
        clean_observations: list[GroupObservation] = []
        for observation in observations:
            kind = observation.kind.strip().casefold()
            text = sanitize_social_text(observation.text, 360)
            if kind in {"culture", "norm", "joke", "dynamic", "event"} and social_text_allowed(text):
                clean_observations.append(GroupObservation(kind, text))
            if len(clean_observations) >= 5:
                break
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for user_id, revision in batch.participant_revisions:
                if self.profile_revision(user_id) != revision:
                    self._conn.rollback()
                    return False
            remaining = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM guild_group_events
                    WHERE guild_id = ? AND id <= ?
                    """,
                    (batch.guild_id, batch.through_event_id),
                ).fetchone()[0]
            )
            if remaining == 0:
                self._conn.rollback()
                return False
            existing_members = {
                int(row[0]) for row in self._conn.execute(
                    "SELECT user_id FROM guild_continuity_members WHERE guild_id = ?",
                    (batch.guild_id,),
                ).fetchall()
            }
            participants = {user_id for user_id, _ in batch.participant_revisions}
            if (
                self.max_group_members_per_guild
                and len(existing_members | participants) > self.max_group_members_per_guild
            ):
                self._invalidate_group_guild_no_commit(batch.guild_id)
                self._conn.commit()
                return False
            self._conn.execute(
                """
                UPDATE guild_continuity
                SET summary = CASE WHEN ? != '' THEN ? ELSE summary END,
                    last_reflected_at = ?, source_through_event_id = ?, updated_at = ?
                WHERE guild_id = ?
                """,
                (clean_summary, clean_summary, now, batch.through_event_id, now, batch.guild_id),
            )
            for user_id in participants:
                self._conn.execute(
                    """
                    INSERT INTO guild_continuity_members(guild_id, user_id, last_contributed_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        last_contributed_at = excluded.last_contributed_at
                    """,
                    (batch.guild_id, user_id, now),
                )
            for observation in clean_observations:
                digest = hashlib.sha256(
                    " ".join(observation.text.casefold().split()).encode("utf-8")
                ).hexdigest()
                self._conn.execute(
                    """
                    INSERT INTO guild_group_journal(
                        guild_id, kind, text, text_hash, source_through_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, kind, text_hash) DO UPDATE SET
                        source_through_event_id = excluded.source_through_event_id,
                        created_at = excluded.created_at
                    """,
                    (
                        batch.guild_id, observation.kind, observation.text, digest,
                        batch.through_event_id, now,
                    ),
                )
            self._conn.execute(
                "DELETE FROM guild_group_events WHERE guild_id = ? AND id <= ?",
                (batch.guild_id, batch.through_event_id),
            )
            if self.max_group_journal_per_guild:
                self._trim_group_journal(batch.guild_id, self.max_group_journal_per_guild)
            if self.max_total_group_journal:
                self._trim_total_group_journal(self.max_total_group_journal)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def _invalidate_group_guild_no_commit(self, guild_id: int) -> tuple[int, int, int]:
        journal = self._conn.execute(
            "DELETE FROM guild_group_journal WHERE guild_id = ?", (guild_id,)
        ).rowcount
        events = self._conn.execute(
            "DELETE FROM guild_group_events WHERE guild_id = ?", (guild_id,)
        ).rowcount
        state = self._conn.execute(
            "DELETE FROM guild_continuity WHERE guild_id = ?", (guild_id,)
        ).rowcount
        return int(state), int(journal), int(events)

    def _invalidate_group_state_for_user_no_commit(
        self, user_id: int
    ) -> tuple[int, int, int]:
        guild_ids = [
            int(row[0]) for row in self._conn.execute(
                """
                SELECT guild_id FROM guild_continuity_members WHERE user_id = ?
                UNION
                SELECT guild_id FROM guild_group_events WHERE user_id = ?
                """,
                (user_id, user_id),
            ).fetchall()
        ]
        totals = [0, 0, 0]
        for guild_id in guild_ids:
            removed = self._invalidate_group_guild_no_commit(guild_id)
            totals = [left + right for left, right in zip(totals, removed)]
        return tuple(totals)  # type: ignore[return-value]

    def social_profile_counts(
        self,
        *,
        user_id: int,
    ) -> dict[str, int]:
        confirmed = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM profile_facts WHERE user_id = ? AND status = 'confirmed'",
                (user_id,),
            ).fetchone()[0]
        )
        tentative = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM profile_facts WHERE user_id = ? AND status = 'tentative'",
                (user_id,),
            ).fetchone()[0]
        )
        inactive = int(
            self._conn.execute(
                """
                SELECT COUNT(*) FROM profile_facts
                WHERE user_id = ? AND status IN ('contradicted', 'superseded')
                """,
                (user_id,),
            ).fetchone()[0]
        )
        journal = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM agent_journal WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        )
        pending = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        )
        relationship = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM relationships WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        )
        return {
            "confirmed_facts": confirmed,
            "tentative_facts": tentative,
            "inactive_facts": inactive,
            "journal_entries": journal,
            "pending_interactions": pending,
            "relationships": relationship,
        }

    def reset_social_profile(
        self,
        *,
        user_id: int,
    ) -> dict[str, int]:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            fact_cursor = self._conn.execute(
                "DELETE FROM profile_facts WHERE user_id = ?", (user_id,)
            )
            journal_cursor = self._conn.execute(
                "DELETE FROM agent_journal WHERE user_id = ?", (user_id,)
            )
            event_cursor = self._conn.execute(
                "DELETE FROM relationship_events WHERE user_id = ?", (user_id,)
            )
            relationship_cursor = self._conn.execute(
                "DELETE FROM relationships WHERE user_id = ?", (user_id,)
            )
            group_states, group_journal, invalidated_group_events = (
                self._invalidate_group_state_for_user_no_commit(user_id)
            )
            group_event_cursor = self._conn.execute(
                "DELETE FROM guild_group_events WHERE user_id = ?", (user_id,)
            )
            self._bump_profile_revision_no_commit(user_id)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {
            "profile_facts": int(fact_cursor.rowcount),
            "journal_entries": int(journal_cursor.rowcount),
            "pending_interactions": int(event_cursor.rowcount),
            "relationships": int(relationship_cursor.rowcount),
            "group_continuities": group_states,
            "group_journal_entries": group_journal,
            "group_events": invalidated_group_events + int(group_event_cursor.rowcount),
        }

    def privacy_state(self, guild_id: int, user_id: int) -> PrivacyState:
        row = self._conn.execute(
            "SELECT opted_out, revision FROM privacy WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if not row:
            return PrivacyState(opted_out=False, revision=0)
        return PrivacyState(opted_out=bool(row["opted_out"]), revision=int(row["revision"]))

    def is_opted_out(self, guild_id: int, user_id: int) -> bool:
        return self.privacy_state(guild_id, user_id).opted_out

    def _bump_privacy_revision_no_commit(self, guild_id: int, user_id: int) -> int:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO privacy(guild_id, user_id, opted_out, revision, updated_at)
            VALUES (?, ?, 0, 1, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                revision = privacy.revision + 1,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, now),
        )
        row = self._conn.execute(
            "SELECT revision FROM privacy WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return int(row["revision"]) if row else 0

    def set_opted_out(self, guild_id: int, user_id: int, opted_out: bool) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO privacy(guild_id, user_id, opted_out, revision, updated_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                opted_out = excluded.opted_out,
                revision = privacy.revision + 1,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, int(opted_out), now),
        )
        self._conn.commit()

    def delete_conversation_memory(self, guild_id: int, user_id: int) -> dict[str, int]:
        """Delete user-controlled conversation memory in one guild or DM scope.

        Inferred profiles, the agent journal, relationship events, and shared
        continuity are agent-owned state with their own private delete/reset
        controls.  They are deliberately not part of `/memory forget`.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            message_cursor = self._conn.execute(
                "DELETE FROM messages WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            memory_cursor = self._conn.execute(
                "DELETE FROM memories WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            attachment_cursor = self._conn.execute(
                """
                DELETE FROM attachment_sources
                WHERE source_guild_id = ? AND uploader_user_id = ?
                """,
                (guild_id, user_id),
            )
            self._conn.execute(
                """
                DELETE FROM attachments
                WHERE NOT EXISTS (
                    SELECT 1 FROM attachment_sources
                    WHERE attachment_sources.sha256 = attachments.sha256
                )
                """
            )
            # A summary may have incorporated the user's content, so invalidate
            # summaries in the affected DM or guild. This intentionally errs on
            # the side of deleting too much context rather than retaining a trace.
            if guild_id == 0:
                summary_cursor = self._conn.execute(
                    "DELETE FROM summaries WHERE scope = ?",
                    (f"dm:{user_id}",),
                )
            else:
                summary_cursor = self._conn.execute(
                    "DELETE FROM summaries WHERE guild_id = ?",
                    (guild_id,),
                )
            # Invalidate a concurrent conversation/attachment write in this
            # scope without changing the future-storage preference.
            self._bump_privacy_revision_no_commit(guild_id, user_id)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {
            "messages": int(message_cursor.rowcount),
            "memories": int(memory_cursor.rowcount),
            "attachments": int(attachment_cursor.rowcount),
            "summaries": int(summary_cursor.rowcount),
        }

    def get_summary(self, scope: str) -> str:
        row = self._conn.execute("SELECT text FROM summaries WHERE scope = ?", (scope,)).fetchone()
        if not row:
            return ""
        value = sanitize_social_text(
            str(row["text"]),
            _MAX_PERSISTED_SUMMARY_CHARS,
        )
        return value if social_text_allowed(value) else ""

    def messages_since_summary(self, scope: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM messages
            WHERE scope = ? AND id > COALESCE(
                (SELECT through_message_id FROM summaries WHERE scope = ?), 0
            )
            """,
            (scope, scope),
        ).fetchone()
        return int(row["count"])

    def compaction_batch(
        self,
        *,
        scope: str,
        keep_recent: int,
        max_batch: int = 80,
    ) -> CompactionBatch | None:
        if keep_recent < 1:
            raise ValueError("keep_recent must be at least 1")
        summary_row = self._conn.execute(
            "SELECT * FROM summaries WHERE scope = ?", (scope,)
        ).fetchone()
        through = int(summary_row["through_message_id"]) if summary_row else 0
        previous = ""
        if summary_row:
            candidate = sanitize_social_text(
                str(summary_row["text"]),
                _MAX_PERSISTED_SUMMARY_CHARS,
            )
            if social_text_allowed(candidate):
                previous = candidate

        recent_rows = self._conn.execute(
            "SELECT id FROM messages WHERE scope = ? AND id > ? ORDER BY id DESC LIMIT ?",
            (scope, through, keep_recent),
        ).fetchall()
        if len(recent_rows) < keep_recent:
            return None
        recent_cutoff = min(int(row["id"]) for row in recent_rows)
        rows = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE scope = ? AND id > ? AND id < ?
            ORDER BY id ASC LIMIT ?
            """,
            (scope, through, recent_cutoff, max_batch),
        ).fetchall()
        if not rows:
            return None
        messages = tuple(self._message_from_row(row) for row in rows)
        first = messages[0]
        return CompactionBatch(
            scope=scope,
            guild_id=first.guild_id,
            channel_id=first.channel_id,
            previous_summary=previous,
            messages=messages,
            through_message_id=messages[-1].id,
        )

    def save_summary(self, batch: CompactionBatch, text: str) -> None:
        summary = sanitize_social_text(
            text,
            _MAX_PERSISTED_SUMMARY_CHARS,
        )
        if not summary or not social_text_allowed(summary):
            return
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO summaries(scope, guild_id, channel_id, text, through_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                text = excluded.text,
                through_message_id = excluded.through_message_id,
                updated_at = excluded.updated_at
            """,
            (
                batch.scope,
                batch.guild_id,
                batch.channel_id,
                summary,
                batch.through_message_id,
                now,
            ),
        )
        self._conn.execute(
            "DELETE FROM messages WHERE scope = ? AND id <= ?",
            (batch.scope, batch.through_message_id),
        )
        self._conn.commit()

    def seed_channel_config(
        self,
        guild_id: int,
        channel_id: int,
        *,
        auto_reply: bool,
        proactive: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_config(
                guild_id, channel_id, auto_reply, proactive, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, int(auto_reply), int(proactive), int(time.time())),
        )
        self._conn.commit()

    def set_channel_config(
        self,
        guild_id: int,
        channel_id: int,
        *,
        auto_reply: bool,
        proactive: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO channel_config(guild_id, channel_id, auto_reply, proactive, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                auto_reply = excluded.auto_reply,
                proactive = excluded.proactive,
                updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, int(auto_reply), int(proactive), int(time.time())),
        )
        self._conn.commit()

    def get_channel_config(
        self,
        guild_id: int,
        channel_id: int,
        *,
        default_auto: bool = False,
        default_proactive: bool = False,
    ) -> ChannelConfig:
        row = self._conn.execute(
            "SELECT * FROM channel_config WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        if row:
            return ChannelConfig(
                guild_id=guild_id,
                channel_id=channel_id,
                auto_reply=bool(row["auto_reply"]),
                proactive=bool(row["proactive"]),
            )
        return ChannelConfig(guild_id, channel_id, default_auto, default_proactive)

    def proactive_channels(self) -> list[ChannelConfig]:
        rows = self._conn.execute(
            "SELECT * FROM channel_config WHERE proactive = 1 ORDER BY guild_id, channel_id"
        ).fetchall()
        return [
            ChannelConfig(
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                auto_reply=bool(row["auto_reply"]),
                proactive=bool(row["proactive"]),
            )
            for row in rows
        ]

    def channel_participant_stats(
        self,
        guild_id: int,
        channel_id: int,
        since_timestamp: int,
    ) -> tuple[int | None, int, int]:
        """Return stored external activity; peer bots are participants."""
        row = self._conn.execute(
            """
            SELECT MAX(created_at) AS last_at,
                   COUNT(*) AS message_count,
                   COUNT(DISTINCT user_id) AS unique_users
            FROM messages
            WHERE guild_id = ? AND channel_id = ? AND role = 'user' AND created_at >= ?
            """,
            (guild_id, channel_id, since_timestamp),
        ).fetchone()
        return (
            int(row["last_at"]) if row["last_at"] is not None else None,
            int(row["message_count"]),
            int(row["unique_users"]),
        )

    def channel_human_stats(
        self,
        guild_id: int,
        channel_id: int,
        since_timestamp: int,
    ) -> tuple[int | None, int, int]:
        """Compatibility alias for pre-dev14 callers."""
        return self.channel_participant_stats(guild_id, channel_id, since_timestamp)

    def proactive_state(self, guild_id: int, channel_id: int) -> tuple[int, str, int]:
        row = self._conn.execute(
            "SELECT * FROM channel_state WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        if not row:
            return 0, "", 0
        return int(row["last_proactive_at"]), str(row["proactive_day"]), int(row["proactive_count"])

    def mark_proactive(self, guild_id: int, channel_id: int, day_key: str, timestamp: int) -> None:
        last_at, previous_day, previous_count = self.proactive_state(guild_id, channel_id)
        del last_at
        count = previous_count + 1 if previous_day == day_key else 1
        self._conn.execute(
            """
            INSERT INTO channel_state(
                guild_id, channel_id, last_proactive_at, proactive_day, proactive_count
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                last_proactive_at = excluded.last_proactive_at,
                proactive_day = excluded.proactive_day,
                proactive_count = excluded.proactive_count
            """,
            (guild_id, channel_id, timestamp, day_key, count),
        )
        self._conn.commit()

    def prune(
        self,
        max_messages_per_channel: int,
        max_memories_per_user: int,
        max_profile_facts_per_user: int | None = None,
        max_journal_entries_per_user: int | None = None,
        max_pending_interactions_per_user: int | None = None,
    ) -> dict[str, int]:
        removed_messages = 0
        scopes = [
            str(row["scope"])
            for row in self._conn.execute("SELECT DISTINCT scope FROM messages")
        ]
        for scope in scopes:
            removed_messages += self._trim_scope(scope, max_messages_per_channel)
        if self.max_total_messages:
            removed_messages += self._trim_total_messages(self.max_total_messages)
            self._global_message_checks_remaining = 1

        removed_memories = 0
        users = self._conn.execute(
            "SELECT DISTINCT guild_id, user_id FROM memories"
        ).fetchall()
        for row in users:
            removed_memories += self._trim_user_memories(
                int(row["guild_id"]),
                int(row["user_id"]),
                max_memories_per_user,
            )

        fact_limit = (
            self.max_profile_facts_per_user
            if max_profile_facts_per_user is None
            else max(0, int(max_profile_facts_per_user))
        )
        removed_profile_facts = 0
        if fact_limit:
            fact_users = self._conn.execute(
                "SELECT DISTINCT user_id FROM profile_facts"
            ).fetchall()
            for row in fact_users:
                removed_profile_facts += self._trim_user_profile_facts(
                    int(row["user_id"]),
                    fact_limit,
                )
        if self.max_total_profile_facts:
            removed_profile_facts += self._trim_total_profile_facts(
                self.max_total_profile_facts
            )

        journal_limit = (
            self.max_journal_entries_per_user
            if max_journal_entries_per_user is None
            else max(0, int(max_journal_entries_per_user))
        )
        removed_journal_entries = 0
        if journal_limit:
            journal_users = self._conn.execute(
                "SELECT DISTINCT user_id FROM agent_journal"
            ).fetchall()
            for row in journal_users:
                removed_journal_entries += self._trim_user_journal(
                    int(row["user_id"]),
                    journal_limit,
                )
        if self.max_total_journal_entries:
            removed_journal_entries += self._trim_total_journal(
                self.max_total_journal_entries
            )

        event_limit = (
            self.max_pending_interactions_per_user
            if max_pending_interactions_per_user is None
            else max(0, int(max_pending_interactions_per_user))
        )
        removed_pending_interactions = 0
        if event_limit:
            event_users = self._conn.execute(
                """
                SELECT DISTINCT guild_id, user_id
                FROM relationship_events
                """
            ).fetchall()
            for row in event_users:
                removed_pending_interactions += self._trim_user_relationship_events(
                    int(row["guild_id"]),
                    int(row["user_id"]),
                    event_limit,
                )
        if self.max_total_pending_interactions:
            removed_pending_interactions += self._trim_total_relationship_events(
                self.max_total_pending_interactions
            )

        removed_group_events = 0
        if self.max_group_events_per_guild:
            guild_rows = self._conn.execute(
                "SELECT DISTINCT guild_id FROM guild_group_events"
            ).fetchall()
            for row in guild_rows:
                removed_group_events += self._trim_group_events(
                    int(row["guild_id"]), self.max_group_events_per_guild
                )
        if self.max_total_group_events:
            removed_group_events += self._trim_total_group_events(
                self.max_total_group_events
            )

        removed_group_journal = 0
        if self.max_group_journal_per_guild:
            guild_rows = self._conn.execute(
                "SELECT DISTINCT guild_id FROM guild_group_journal"
            ).fetchall()
            for row in guild_rows:
                removed_group_journal += self._trim_group_journal(
                    int(row["guild_id"]), self.max_group_journal_per_guild
                )
        if self.max_total_group_journal:
            removed_group_journal += self._trim_total_group_journal(
                self.max_total_group_journal
            )

        self._conn.commit()
        return {
            "messages": removed_messages,
            "memories": removed_memories,
            "profile_facts": removed_profile_facts,
            "journal_entries": removed_journal_entries,
            "pending_interactions": removed_pending_interactions,
            "group_events": removed_group_events,
            "group_journal_entries": removed_group_journal,
        }

    def stats(self) -> dict[str, int]:
        tables = {
            "messages": "messages",
            "memories": "memories",
            "model_outcomes": "model_outcomes",
            "attachments": "attachments",
            "attachment_chunks": "attachment_chunks",
            "profile_facts": "profile_facts",
            "relationships": "relationships",
            "pending_interactions": "relationship_events",
            "journal_entries": "agent_journal",
            "group_continuities": "guild_continuity",
            "group_events": "guild_group_events",
            "group_journal_entries": "guild_group_journal",
            "group_memberships": "guild_continuity_members",
            "interaction_metrics": "interaction_metrics",
            "summaries": "summaries",
        }
        counts = {
            key: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for key, table in tables.items()
        }
        counts["users"] = int(
            self._conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT user_id FROM messages
                    UNION
                    SELECT user_id FROM memories
                    UNION
                    SELECT user_id FROM profile_facts
                    UNION
                    SELECT user_id FROM relationships
                    UNION
                    SELECT user_id FROM relationship_events
                    UNION
                    SELECT user_id FROM agent_journal
                    UNION
                    SELECT user_id FROM guild_group_events
                    UNION
                    SELECT user_id FROM guild_continuity_members
                    UNION
                    SELECT user_id FROM privacy
                    UNION
                    SELECT uploader_user_id AS user_id FROM attachment_sources
                )
                """
            ).fetchone()[0]
        )
        counts["database_bytes"] = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            )
            if candidate.exists()
        )
        return counts
