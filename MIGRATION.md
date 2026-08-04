# Migration guide

Discord Agent Lite 1.3.0 retains the 1.2 social-storage migration and additively advances attachment evidence to schema 8. Back up `data/agent.db`, `.env`, and character cards before starting. Do not run old and new bot processes against the same SQLite file.

## From Discord Agent Lite 1.1

Keep the existing database and replace the application files. On first startup, the store detects the v1.1 `agent_namespace` columns and performs one transaction that:

- renames the four v1.1 social tables temporarily;
- creates the v2 profile, relationship, event, and journal tables;
- migrates one selected legacy character identity;
- maps `category` to `topic`, `user_asserted` rows to direct facts, and observed rows to inferred impressions;
- combines the selected identity's per-guild relationship rows into one user relationship, mapping weighted affinity to affection;
- retains guild/DM source context and disclosure visibility where it can be reconstructed;
- re-applies the current credential, authentication-cookie, and durable-instruction persistence filter to legacy profile, relationship, event, and journal text;
- preserves retained profile, event, and journal IDs plus the legacy AUTOINCREMENT high-water marks, including IDs belonging to filtered or unselected rows, so stable private-control IDs are never reused;
- collapses duplicate selected facts deterministically while keeping the canonical ID, earliest creation time, greatest last-seen time, and source metadata from the newest observation;
- drops the temporary v1.1 tables only after all selected rows are copied.

Recent messages, explicit memories, channel settings, proactivity state, conversation-memory storage preferences, and rolling-summary compaction positions are preserved; pre-v6 summary prose is revalidated as described below.

Historical Phase 3 and Phase 4 candidates advanced SQLite to schema versions 4 and 5, adding attachment/cache/FTS and guild-continuity/tuning tables. Version 1.2 kept those additive tables readable for migration, status counts, deletion, and rollback compatibility while leaving them inactive. The current lean attachment design still does not populate or retrieve those cache/chunk/FTS tables; it stores only bounded structured evidence on active parent rows.

The dev8 startup upgrade advanced SQLite to schema version 6 and revalidated prose written before the then-current persistence checks. Unsafe pre-fix profile and journal rows were removed; unsafe relationship, rolling-summary, and guild-continuity summaries were blanked; and unsafe pending relationship/guild event fields were replaced with explicit omission markers. Rolling `through_message_id` remained intact so quarantined content was not reintroduced by replay. Valid rows were retained and normalized. That cleanup also ran immediately after a v1.1 social migration. Version 6 was the dev8 ceiling; dev24's schema-7 rule below supersedes it.

Dev21 remains schema 6 and additively creates a small global `profile_state` revision table. Existing `privacy.opted_out` values are retained strictly as per-guild/DM conversation-memory preferences; they no longer suppress profile/journal observation or reflection. Profile delete/reset advances the independent global profile revision, while memory storage/forget advances only the current-scope conversation revision. Existing profile/journal data is preserved, but social state erased by an older broad forget cannot be reconstructed retroactively.

Dev24 advances SQLite to schema version 7. The existing global relationship row becomes active conversational continuity: its eight numeric dimensions are globally keyed to the Discord user, while its free-text summary is supplied only in DMs. Startup revalidates every retained journal row and removes notes that are unsafe or not short first-person subjective continuity. New reflection output binds every observation and journal note to an actual submitted event; direct facts additionally require an exact first-person quote from that event. A database reporting a schema version newer than 7 is rejected before schema DDL instead of being downgraded.

The attachment-evidence release advances SQLite additively to schema version 8.
It adds `attachment_parts_json TEXT NOT NULL DEFAULT '[]'` to `messages` and
`relationship_events`; existing rows therefore decode as no evidence. The JSON
contains only bounded derived evidence, never raw bytes, CDN URLs, hashes,
caches, chunks, or FTS data. Conversation evidence follows message deletion and
conversation-memory controls; relationship evidence follows social-event
pruning and `/profile reset`. A database reporting a schema version newer than
8 is rejected before schema DDL instead of being downgraded. Restoring an older
binary requires the complete matching pre-upgrade database backup.

### Selecting the legacy identity

New storage has no runtime social namespace. During this one-time migration, the configured `CHARACTER_FILE` filename stem selects the matching legacy identity.

If a configured filename stem does not match an identity in the legacy database, startup fails with `SocialMigrationError`, even when the database contains only one different identity. With no selector, multiple legacy identities also fail closed. This preflight occurs before persistent SQLite journal settings or schema/data changes. The migration never guesses and never merges unrelated character identities. Point `CHARACTER_FILE` at a card whose filename stem matches the identity to retain, then start once and confirm the migration:

```dotenv
CHARACTER_FILE=characters/character.json
```

Startup logs the selected legacy identity before migration. Only that identity is retained. Back up the database first if another legacy identity may need a separate export.

### Recommended Phase 1 settings

Version 1.3 makes every successfully answered turn eligible for social continuity by default. Existing deployments that explicitly set `RELATIONSHIP_DIRECT_ONLY=true` keep the older restrictive behavior; change that value to `false` if admitted auto-replies should also become relationship/profile/journal events.

```dotenv
RELATIONSHIPS_ENABLED=true
RELATIONSHIP_DIRECT_ONLY=false
RELATIONSHIP_REFLECT_EVERY=6
RELATIONSHIP_MEANINGFUL_CHARS=220
RELATIONSHIP_MEANINGFUL_EVENT_THRESHOLD=1
RELATIONSHIP_REFLECT_MIN_SECONDS=1800
RELATIONSHIP_REFLECT_MAX_EVENTS=10
PROFILE_CONTEXT_FACTS=8
JOURNAL_CONTEXT_ENTRIES=2
MAX_PROFILE_FACTS_PER_USER=24
MAX_JOURNAL_ENTRIES_PER_USER=20
MAX_PENDING_INTERACTIONS_PER_USER=12
MAX_TOTAL_PROFILE_FACTS=5000
MAX_TOTAL_JOURNAL_ENTRIES=3000
MAX_TOTAL_PENDING_INTERACTIONS=5000
MAX_TOTAL_RELATIONSHIPS=5000
```

Run `/agent status`, `/profile view`, and `/profile facts page:1` after startup. Continue through the reported pages to inspect every record. Profile IDs are typed, such as `profile:12`; journal IDs are `journal:7`.

For the active attachment lane, run `/privacy` and inspect `/agent status`. The service environment may retain old `MAX_ATTACHMENT_BYTES`, `MAX_ATTACHMENT_CHARS`, or 15-second `ATTACHMENT_TIMEOUT_SECONDS` overrides; compare it with `.env.example` before expecting the current 5 MiB/6,000-character/60-second defaults. Add the active `ATTACHMENT_*` and `ALCHEMIST_*` settings explicitly when stable deployment behavior should not depend on defaults. Retired cache/FTS and `GROUP_*` settings should not be copied into a new 1.3.0 environment.

### New identity and disclosure behavior

- Profile records and relationship state are owned globally by one Discord user for this bot deployment.
- Public guild observations may be used when that user appears in another guild.
- DM-derived profile and journal records are supplied only in DMs.
- Global numeric relationship dimensions may shape tone across guild and DM contexts; the free-text relationship summary is supplied only in DMs.
- Normal prompts receive active records only; contradicted and superseded records remain inspectable.
- Character-card changes no longer fork relationship state.

### Rollback

Discord Agent Lite 1.1 cannot read the v2 social tables or schema-v4/v5/v6/v7/v8 tables. Restore the complete pre-migration database backup before running v1.1. Do not attempt a partial table copy while either process is active.

## From an older custom bot

Treat the old runtime as migration input, not as code to patch or state to import automatically.

Keep:

- the original character card as private, trusted operator-controlled configuration outside Git;
- compatible `.env` values, copied manually into the new template with file mode `0600`;
- a restricted backup of the old SQLite database and profile JSON for audit or a later consent-based importer.

Do not automatically import:

- the old `user_profiles.json`, because it mixes hardcoded authority, roles, and free-form notes without the new provenance and deletion semantics;
- the old `interactions` rows, because channel IDs alone cannot reliably reconstruct guild/privacy scope and the rows lack disclosure provenance;
- old logs, virtual environments, caches, or manually launched process state.

Build a clean virtual environment and keep credentials, character cards, databases, and logs outside the source tree. Any future legacy-data importer should require explicit user re-consent and produce v2 provenance rather than copying rows blindly.
