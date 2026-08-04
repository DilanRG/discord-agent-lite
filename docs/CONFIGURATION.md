# Configuration reference

`Settings.load()` reads the process environment and then `.env` through
`python-dotenv` (existing process variables win). Relative paths resolve from
the repository root. `DISCORD_TOKEN` is required. `LLM_PROVIDER` must be
`horde`; there is no local-provider fallback.

Boolean values accept `1/0`, `true/false`, `yes/no`, and `on/off`. Integer and
float values are range-checked. A malformed value or failed cross-setting
relationship raises `ConfigError` during startup.

## Identity, paths, and logging

| Variable | Default | Accepted values / notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | none | Required secret. Never commit or log it. |
| `CHARACTER_FILE` | `characters/example.json` | Plain JSON or `chara_card_v2`; relative to project root. |
| `DATABASE_PATH` | `data/agent.db` | SQLite path; use a persistent absolute path in systemd. |
| `LOG_PATH` | `logs/agent.log` | Rotating application log path; use a persistent absolute path in systemd. |
| `LOG_LEVEL` | `INFO` | Standard logging level; Discord/aiohttp payload logging remains restricted. |
| `BOT_ACTIVITY` | empty | Optional Discord presence text. |
| `DEV_GUILD_ID` | empty | Optional positive Discord guild ID for immediate command sync. |
| `LLM_PROVIDER` | `horde` | Only `horde` is accepted. |

## Horde and provider settings

| Variable | Default | Range / behavior |
| --- | ---: | --- |
| `HORDE_API_KEY` | `0000000000` | Anonymous fallback; a real user key has better priority. |
| `HORDE_BASE_URL` | `https://aihorde.net/api/v2` | Base URL; trailing slash is removed. |
| `HORDE_TRUSTED_WORKERS` | `true` | Boolean passed to Horde routing/submission. |
| `HORDE_POLL_SECONDS` | `2` | Float `1..15`; status polling interval. |
| `HORDE_TIMEOUT_SECONDS` | `120` | Integer `15..600`; total provider deadline. |
| `HORDE_ROUTER_METADATA_TTL_SECONDS` | `90` | Integer `60..120`; live-model metadata refresh. |
| `HORDE_ROUTER_STICKY_SECONDS` | `1800` | Integer `60..7200`; idle lifetime of a `(task, scope)` model selection. |
| `HORDE_MIN_MODEL_PARAMETERS_BN` | `7` | Float `7..200`; hard minimum model size. |
| `HORDE_ROUTER_MAX_SCOPES` | `512` | Integer `8..10000`; maximum sticky selections. |
| `PROVIDER_MAX_TOKENS` | `300` | Integer `32..1200`; visible response budget. |
| `PROVIDER_CONTEXT_TOKENS` | `8192` | Integer `2048..32768`; context budget. Must leave at least 1024 tokens after output. |
| `RELATIONSHIP_CONTEXT_TOKENS` | `4096` | Integer `2048..PROVIDER_CONTEXT_TOKENS`; reflection budget. |
| `PROACTIVE_CONTEXT_TOKENS` | `4096` | Integer `2048..PROVIDER_CONTEXT_TOKENS`; proactive budget. |

The router refreshes live text models and the official reference CSV. It admits
only active models with a matching reference, at least the configured size,
and a supported instruction format. RP-tagged candidates rank ahead of chat
candidates, then ETA/count/size/name break ties. A selection is sticky per
task and scope. One recent route failure is demoted briefly; historical
outcomes are diagnostics, not an automatic circuit or tuning signal.

## Channels and admission

| Variable | Default | Range / behavior |
| --- | ---: | --- |
| `AUTO_REPLY_CHANNELS` | empty | Comma-separated positive channel IDs; seeds SQLite only when no saved row exists. |
| `PROACTIVE_CHANNELS` | empty | Comma-separated positive channel IDs; same seed rule. |
| `BLACKLISTED_USER_IDS` | empty | Comma-separated positive Discord IDs; ignored for normal messages/commands. |
| `RESPOND_TO_DMS` | `true` | Direct DMs admitted when enabled. |
| `AUTO_REPLY_PROBABILITY` | `0.10` | Float `0..1`; ambient base probability. |
| `AUTO_REPLY_QUESTION_BONUS` | `0.18` | Float `0..1`; question bonus before the hard admission cap. |
| `USER_RATE_REQUESTS` | `3` | Integer `1..30` per `USER_RATE_PERIOD_SECONDS`. |
| `USER_RATE_PERIOD_SECONDS` | `60` | Integer `5..3600`. |
| `CHANNEL_RATE_REQUESTS` | `8` | Integer `1..100` per channel period. |
| `CHANNEL_RATE_PERIOD_SECONDS` | `60` | Integer `5..3600`. |
| `COMMAND_RATE_REQUESTS` | `12` | Integer `1..100` per command period. |
| `COMMAND_RATE_PERIOD_SECONDS` | `60` | Integer `5..3600`. |
| `TRACKING_USER_MESSAGES` | `30` | Integer `1..300` passive messages per user period. |
| `TRACKING_CHANNEL_MESSAGES` | `120` | Integer `1..2000` passive messages per channel period. |
| `TRACKING_RATE_PERIOD_SECONDS` | `60` | Integer `5..3600`. |
| `GLOBAL_CONCURRENCY` | `2` | Integer `1..8`; active provider work. |
| `MAX_PENDING_REQUESTS` | `6` | Integer `1..32`; must be at least `GLOBAL_CONCURRENCY`. |

`/agent channel` is authoritative after startup. Environment channel lists are
initial seeds and do not overwrite saved SQLite settings.

## Input, output, and attachments

| Variable | Default | Range / behavior |
| --- | ---: | --- |
| `MAX_INPUT_CHARS` | `3500` | Integer `256..16000`; current Discord text bound. |
| `MAX_REPLY_CHARS` | `1800` | Integer `128..1950`; final Discord content bound. |
| `MAX_ATTACHMENT_BYTES` | `5242880` | Integer `1024..20000000`; per attachment byte ceiling (5 MiB default). |
| `MAX_ATTACHMENT_CHARS` | `6000` | Integer `256..16000`; model and durable-evidence text bound. |
| `ATTACHMENT_MAX_COUNT` | `2` | Integer `1..2` per admitted message. |
| `ATTACHMENT_MAX_EXTRACTED_CHARS` | `6000` | Integer `1000..1000000`; extraction-work ceiling before the model/persistence cut. |
| `ATTACHMENT_MAX_PIXELS` | `16777216` | Integer `1024..100000000`; image pixel ceiling. |
| `ATTACHMENT_TIMEOUT_SECONDS` | `60` | Float `1..60`; one deadline shared by the message's attachment work. |
| `ATTACHMENT_CONCURRENCY` | `1` | Integer `1..2`; attachment lane semaphore. |
| `ATTACHMENT_DOCUMENT_LOCK_PATH` | `/run/lock/agent-lite-attachments.lock` | Shared POSIX `flock` path; required for production PDF/DOCX parsing. |
| `ALCHEMIST_API_KEY` | `0000000000` | Anonymous fallback; use a separate user key for higher priority. |
| `ALCHEMIST_ENABLED` | `true` | Boolean; supported images otherwise receive no caption. |

The active attachment lane accepts bounded UTF-8 text/code, text-bearing PDF
and DOCX, and PNG/JPEG/WebP. Raw bytes are temporary and no attachment cache,
chunk table, or FTS index is active. Bounded structured evidence may be stored
with an eligible parent message or relationship event; it follows that
parent's memory/privacy/reset lifecycle. Other Office formats, archives,
executables, scanned/encrypted documents, malformed packages, and arbitrary
URLs fail closed.

## Conversation memory

| Variable | Default | Range / behavior |
| --- | ---: | --- |
| `RECENT_MESSAGE_COUNT` | `14` | Integer `2..40` turns. |
| `RECALL_MESSAGE_COUNT` | `4` | Integer `0..12` lexical recalls. |
| `RECALL_CANDIDATE_COUNT` | `200` | Integer `20..1000` candidate messages. |
| `MAX_MESSAGES_PER_CHANNEL` | `600` | Integer `50..5000`; oldest rows are trimmed. |
| `MAX_MEMORIES_PER_USER` | `50` | Integer `5..500`. |
| `MAX_TOTAL_MESSAGES` | `10000` | Integer `100..200000`. |
| `MAX_TOTAL_MEMORIES` | `5000` | Integer `100..100000`; new distinct memories can be rejected when full. |
| `MAX_MODEL_OUTCOMES` | `1000` | Integer `10..100000`; diagnostic retention. |

## Profile, journal, and relationship continuity

| Variable | Default | Range / behavior |
| --- | ---: | --- |
| `RELATIONSHIPS_ENABLED` | `true` | Boolean; enables agent-authored social continuity. |
| `RELATIONSHIP_DIRECT_ONLY` | `false` | Boolean; every successfully answered turn becomes a social event by default. Set `true` to restrict events to DMs, explicit mentions, and replies whose Discord `@` toggle is on. |
| `RELATIONSHIP_REFLECT_EVERY` | `6` | Integer `2..50` completed pairs. |
| `RELATIONSHIP_MEANINGFUL_CHARS` | `220` | Integer `80..2000` event significance threshold. |
| `RELATIONSHIP_MEANINGFUL_EVENT_THRESHOLD` | `1` | Integer `1..10` meaningful-event fallback. |
| `RELATIONSHIP_REFLECT_MIN_SECONDS` | `1800` | Integer `60..604800` minimum reflection interval. |
| `RELATIONSHIP_REFLECT_MAX_EVENTS` | `10` | Integer `2..30`; must be at least `RELATIONSHIP_REFLECT_EVERY`. |
| `PROFILE_CONTEXT_FACTS` | `8` | Integer `0..24`; must not exceed per-user profile capacity. |
| `JOURNAL_CONTEXT_ENTRIES` | `2` | Integer `0..5`; must not exceed per-user journal capacity. |
| `MAX_PROFILE_FACTS_PER_USER` | `24` | Integer `4..100`. |
| `MAX_JOURNAL_ENTRIES_PER_USER` | `20` | Integer `2..100`. |
| `MAX_PENDING_INTERACTIONS_PER_USER` | `12` | Integer `2..50`; must be at least reflection batch size. |
| `MAX_TOTAL_PROFILE_FACTS` | `5000` | Integer `100..100000`; must fit the per-user limit. |
| `MAX_TOTAL_JOURNAL_ENTRIES` | `3000` | Integer `100..100000`; must fit the per-user limit. |
| `MAX_TOTAL_PENDING_INTERACTIONS` | `5000` | Integer `100..100000`; must fit the per-user limit. |
| `MAX_TOTAL_RELATIONSHIPS` | `5000` | Integer `100..100000`. |

These settings bound internal state; they do not turn profile/journal state into
an opt-out. Use the private profile commands or stop interacting with the bot
to prevent new qualifying observations.

## Proactivity

| Variable | Default | Range / behavior |
| --- | ---: | --- |
| `PROACTIVE_INTERVAL_SECONDS` | `3600` | Integer `60..3600`; sweep interval. |
| `PROACTIVE_MIN_IDLE_SECONDS` | `43200` | Integer `300..2592000`; participant idle threshold. |
| `PROACTIVE_COOLDOWN_SECONDS` | `43200` | Integer `600..2592000`; cooldown after a post. |
| `PROACTIVE_DAILY_LIMIT` | `2` | Integer `0..20`; zero disables scheduled starts. |
| `PROACTIVE_TIMEZONE` | `UTC` | IANA timezone string used for daily quota boundaries. |

Proactivity also requires a saved channel flag, send permission, a stored
participant tail, exact tail-ID membership, and no newer activity race. A
cooldown by itself never authorizes a chained bot post.

## Configuration checklist

1. Copy `.env.example` to `.env`, set `DISCORD_TOKEN`, card path, Horde key,
   database path, and log path.
2. Use absolute paths in systemd and mode `0600` for the environment file.
3. Set explicit attachment/Alchemist values when migrating from an older
   deployment whose environment may contain retired overrides.
4. Install the document-worker tmpfiles rule/group before starting the supplied
   systemd unit; an inaccessible configured lock intentionally fails closed.
5. Start once, then configure channels with `/agent channel`; saved rows win
   over seed lists.
6. Use `/agent status` to inspect provider selection, current/peak RSS,
   active attachment counters, storage counts, and pending reflections.
