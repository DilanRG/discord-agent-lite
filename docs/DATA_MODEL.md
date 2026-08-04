# Data model, ownership, and privacy scopes

`MemoryStore` opens SQLite in WAL mode with foreign keys, a bounded journal
size, a busy timeout, and `PRAGMA user_version` schema checks. The current
schema ceiling is 8. A database reporting a newer schema is rejected rather
than downgraded.

## Scope and ownership

| State | Key/scope | Who can change it | Prompt visibility |
| --- | --- | --- | --- |
| Conversation messages | Guild channel; DM plus user | Runtime, subject to storage preference | Same scope, bounded recent/lexical recall |
| Conversation attachment evidence | Parent message row | Runtime, subject to the parent's storage preference | Same parent scope, labelled separately from authored text |
| Explicit memories | Guild/DM scope plus author user ID | The author via `/memory` or explicit `remember that` | Author's current scope |
| Profile facts/impressions | Global Discord user ID | Agent reflection, with private typed delete/reset controls | Guild-visible records may cross guilds; DM records stay in DMs |
| Journal entries | Global Discord user ID plus source visibility | Agent reflection, with private typed delete/reset controls | Same public/DM visibility rules as profile records |
| Relationship state | Global Discord user ID | Agent reflection | Numeric dimensions can shape tone; free-text summary is DM-only |
| Relationship attachment evidence | Parent relationship event | Runtime; removed/pruned/reset with the parent | Reflection only; inferred continuity, never direct facts |
| Channel configuration | Guild ID plus channel ID | Manage Server via `/agent channel` | Operational state, not prompt prose |
| Proactive state | Guild/channel state | Runtime | Eligibility bookkeeping only |
| Model outcomes | Bounded diagnostic rows | Provider | Never supplied as conversation context |

The numeric Discord author ID is authoritative. Username, global name, display
name, and nickname are mutable metadata used for presentation and prompt
context; copying a nickname cannot impersonate the account that owns state.
Peer bots and webhooks have the same ownership and storage rules as other
authors. This bot's own messages are the only authors ignored categorically.

## Active tables and records

The active lean path uses these families of records:

- `messages`: bounded user/assistant turns, scope, Discord message ID, author
  identity, proactive marker, and structured `attachment_parts_json`. The
  unique `(scope, discord_message_id)`
  membership check is also the proactive stale-tail guard.
- `memories`: explicit, user-owned memories deduplicated by normalized hash.
- `channel_config` and channel state: auto-reply/proactive flags, last
  proactive time, local-day count, and participant activity.
- `privacy`: per-user conversation-memory storage preference and revision for
  the current guild, or the user's DM scope.
- `profile_facts`, `agent_journal`, `relationships`, and reflection events:
  separate global social continuity keyed by immutable user ID. Reflection
  events may carry their own parent-controlled `attachment_parts_json` copy.
- `model_outcomes`: bounded operational diagnostics for selected model,
  latency, success/error kind, empty/malformed/truncated output.

Schema-8 databases can also contain compatibility tables from older releases:
rolling summaries, guild/group continuity, attachment cache/chunks/FTS, and
historical interaction metrics. The lean runtime gives those lanes zero
capacity, schedules no generation for them, does not retrieve them for prompts,
and does not treat their rows as current feature activity. Deletion and
migration paths retain selected compatibility cleanup so older databases can
be upgraded or rolled back safely.

## Conversation memory controls

`/memory storage enabled:false` disables future conversation-message,
conversation attachment-evidence, and explicit-memory writes for that user
throughout the current guild, or in that user's DM scope. It does not erase
existing rows and does not opt out of the separate agent-owned social
continuity or its relationship-event evidence.

`/memory forget confirm:true` deletes conversation messages, their attachment
evidence, and explicit memories belonging to that user throughout the current
guild, or in that user's DM scope. It advances the conversation revision and invalidates
in-flight writes. It does not delete profile, journal, relationship, or pending
reflection state. In a guild, legacy aggregate summaries are invalidated
conservatively for the guild because they may contain multiple users; active
lean code does not create new summaries.

`/memory delete` removes one explicit memory only when it is owned by the
invoking user. `/memory search` lists only that user's memories in the current
guild or DM scope.

## Social continuity controls

`/profile view`, `/profile facts`, and `/profile journal` are private views.
`/profile delete` removes one typed profile or journal record owned by the
invoking user. `/profile reset confirm:true` deletes that user's global profile,
journal, relationship, pending-event, and reflection scheduling state and bumps
the profile revision. It does not change conversation-memory storage, delete
explicit memories, or create a permanent social opt-out. Future qualifying
direct interactions may rebuild state.

Reflection is eventual. A completed direct user/assistant pair is queued only
when relationships are enabled and the directness policy allows it. The
provider receives bounded pair records and separately labelled ready attachment
evidence, never raw attachments, quoted reply context, or unrelated third-party
chatter. Attachment evidence may support relationship deltas, journal notes,
and inferred impressions; only outer authored `user_text` can support a direct
fact/evidence quote. The save boundary validates provenance,
first-person direct evidence, source-event IDs, bounded text, journal format,
and relationship deltas before atomically applying changes. Profile and memory
revisions prevent stale in-flight work from resurrecting deleted data.

Profile and journal data is agent-authored working continuity, not user-authored
memory. It is still fallible model-facing reference data, is re-sanitized at
the storage boundary, can be corrected or deleted, and never grants authority,
changes moderation, or becomes an instruction to the character card.

## Bounds and pruning

Messages and explicit memories are trimmed oldest-first or rejected according
to their per-scope/global ceilings. Profile facts, journal entries, pending
events, relationships, limiter maps, channel locks, model selections, and
background tasks are bounded at write/admission time. `/agent prune` applies the
configured active limits; it is not a promise to remove every dormant legacy
group row from an upgraded database.

Back up the database and any SQLite WAL sidecar before upgrades, migration, or
manual surgery. Never run two releases against the same database concurrently.
See [MIGRATION.md](../MIGRATION.md) for the v1.1-to-schema-8 procedure and
rollback requirements.
