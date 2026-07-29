-- Exact SQLite schema created by the stable v1.1 baseline (master 77ba03a).
-- v1.1 did not set PRAGMA user_version, so the historical value is 0.
CREATE TABLE messages (
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
CREATE INDEX idx_messages_scope_time
    ON messages(scope, id DESC);
CREATE INDEX idx_messages_user_time
    ON messages(guild_id, user_id, id DESC);
CREATE INDEX idx_messages_channel_human
    ON messages(guild_id, channel_id, role, created_at DESC);

CREATE TABLE memories (
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
CREATE INDEX idx_memories_user_time
    ON memories(guild_id, user_id, last_used_at DESC);

CREATE TABLE profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_namespace TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('user_asserted', 'observed')),
    confidence REAL NOT NULL DEFAULT 0.75,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'tentative'
        CHECK(status IN ('tentative', 'confirmed')),
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(agent_namespace, guild_id, user_id, category, text_hash)
);
CREATE INDEX idx_profile_facts_user_rank
    ON profile_facts(
        agent_namespace, guild_id, user_id, status,
        evidence_count DESC, confidence DESC, last_seen_at DESC
    );

CREATE TABLE relationships (
    agent_namespace TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    affinity INTEGER NOT NULL DEFAULT 0 CHECK(affinity BETWEEN -20 AND 20),
    summary TEXT NOT NULL DEFAULT '',
    last_interaction_at INTEGER NOT NULL DEFAULT 0,
    last_reflected_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(agent_namespace, guild_id, user_id)
);
CREATE INDEX idx_relationships_recent
    ON relationships(agent_namespace, last_interaction_at DESC);

CREATE TABLE relationship_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_namespace TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_relationship_events_user
    ON relationship_events(agent_namespace, guild_id, user_id, id ASC);

CREATE TABLE agent_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_namespace TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    source_through_event_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(agent_namespace, guild_id, user_id, source_through_event_id)
);
CREATE INDEX idx_agent_journal_user
    ON agent_journal(agent_namespace, guild_id, user_id, id DESC);

CREATE TABLE summaries (
    scope TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    through_message_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE channel_config (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    auto_reply INTEGER NOT NULL DEFAULT 0,
    proactive INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(guild_id, channel_id)
);

CREATE TABLE channel_state (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    last_proactive_at INTEGER NOT NULL DEFAULT 0,
    proactive_day TEXT NOT NULL DEFAULT '',
    proactive_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(guild_id, channel_id)
);

CREATE TABLE privacy (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    opted_out INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(guild_id, user_id)
);
