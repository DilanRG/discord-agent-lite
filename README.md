# Discord Agent Lite 1.2.1

A character-agnostic Discord chat agent designed for a small VM. Inference stays remote; local state is a bounded SQLite database. The ordinary chat path is deliberately short: retrieve bounded context, render native conversational turns, make one model request, strip protocol leakage, and send. Lightweight profiles, journals, cautious auto-replies, and gated proactivity stay outside that decision path.

For the code-level reference, see [`docs/README.md`](docs/README.md). It
expands the architecture, complete configuration surface, schema/ownership
model, prompt/provider contract, attachment lane, test matrix, and operations
runbook. `SECURITY.md` and `MIGRATION.md` remain the normative security and
migration references.

Character cards are deliberately excluded from this public repository. Supply your own trusted card locally; the framework contains no character-specific trigger words, canned messages, account IDs, authority levels, or operational behavior.

## Social continuity

The bot keeps one **social identity** for each Discord user across this deployment:

- Typed profile records: directly stated facts and fallible inferred impressions, each with topic, provenance, confidence, evidence, status, and source context.
- Explicit contradiction and supersession links so corrections do not silently coexist with stale records.
- Brief journal entries produced after batches of completed conversations.
- Meaningful-event reflection triggers in addition to the regular interaction-count trigger.
- User-facing `/profile` commands to inspect, delete, and globally reset that state.
- Context provenance: public guild observations may follow a user across guilds, while DM observations are never supplied in guild replies.

This is intentionally not a trust score. Selected profile and journal context can affect conversational tone and continuity only. It never grants permissions, changes moderation, proves identity, bypasses the blacklist, exposes secrets, or weakens prompt safeguards.

## Adaptive AI Horde routing

The Horde adapter discovers live 7B+ text models, validates their instruction format, prefers RP-tagged candidates, and keeps a bounded sticky selection per conversation. Unknown prompt formats fail closed. One recent typed route failure briefly favors another live model; historical metrics do not auto-tune behavior.

## Bounded attachments

Attachments are considered only after the bot has admitted a response. The lean lane accepts bounded UTF-8 text/code files plus PNG, JPEG, and WebP images. Text is decoded in-process and supplied only to the current turn; it is not cached or indexed. Images are sent to AI Horde Alchemist for a fallible caption. Attachment processing and reply generation stay silent in Discord; the runtime does not emit a typing indicator. One absolute deadline covers download, queueing, extraction, and captioning across the message, and temporary raw files are deleted after each attempt. Binary documents, archives, executables, type/signature mismatches, and oversized inputs are rejected. A failed current-turn attachment is represented to the reply model only as unavailable with an instruction not to invent its contents.

## Capabilities

- Guaranteed replies to explicit mentions (including a reply with its `@` toggle on) and optionally DMs; `@`-off replies use the ordinary auto-reply gate.
- Can probabilistically join conversation in administrator-enabled channels.
- Can occasionally start a context-grounded conversation after real activity and an idle period.
- Stores recent channel or DM context in SQLite.
- Recalls relevant older messages using bounded lexical scoring—no embeddings, vector database, NumPy, Torch, or local model.
- Stores user-owned explicit memories through `/memory remember` or narrowly shaped `remember that ...` messages.
- Reflects over bounded direct user/assistant interaction pairs to update social continuity.
- Supplies bounded UTF-8 text/code attachments to the current turn.
- Captions admitted images through AI Horde Alchemist without a local vision model.
- Uses AI Horde for text generation and image captioning.
- Gives each member conversation-memory storage, inspection, deletion, and forget controls plus separate private profile/journal view, typed-delete, and reset controls.

## Deliberate non-capabilities

This is a conversational agent, not an autonomous tool runner. It cannot execute shell commands, read arbitrary host files, browse the web, install packages, call attacker-selected URLs, change Discord roles, moderate users, post webhooks, or invoke generated functions. That containment boundary matters because public Discord members will eventually prompt-inject, impersonate administrators, upload hostile text, flood storage, and attempt to poison profiles.

Voice remains removed. It added FFmpeg, PyNaCl, audio files, session state, and another large attack/resource surface without helping the small text-agent goal.

## Architecture

```text
Discord gateway
    |
    v
input bounds -> rate limits -> response admission -> bounded attachment pipeline
                                                      |              |
                                         UTF-8 text extraction   Alchemist image caption
                                                      |              |
                                                      +-------+------+
                                                              |
                                    native role-formatted conversation prompt
                                                              |
                              one primary Horde attempt + one bounded alternate
                                                              |
                                      structural output cleanup and length bound
                                                              |
                                           normal Discord mention delivery
```

The process uses one Discord client with minimal intents and a 100-message library cache, one shared `aiohttp` session, one SQLite connection, bounded limiter maps, bounded task maps, one active generation per channel, and small generation semaphores. A reply reference missing from that cache is fetched once from Discord so an admitted older reply can retain bounded quoted context. Background profile/journal reflection shares only the non-reserved generation capacity, leaving one global slot available for visible chat when `GLOBAL_CONCURRENCY` is at least two. There is no resident inference model or embedding index.

### What is actually gated

| Boundary | Why it exists | Chat behavior |
|---|---|---|
| Input size, rate, admission, attachment, and storage limits | Prevent resource exhaustion and unsafe file/network handling | Kept; these run before inference |
| Horde model/worker compatibility and transport timeout | Avoid requests that cannot fit or run on the selected worker | Kept; one typed transient failure may try one different eligible model within the total chat deadline |
| Prompt role-boundary neutralization | Prevent chat text from forging model template delimiters | Kept for every native prompt format |
| Output control tags, hidden reasoning, forged role continuations, and length | Keep provider protocol leakage and accidental multi-turn output out of delivery | Cleaned deterministically; ordinary Discord mention syntax is preserved |
| Tone, greetings, verbosity, amnesia, fictional activity, character breaks, stage actions, and generated transcript formatting | Useful quality signals, but not security or protocol properties | Simulator diagnostics only; never a retry or outage trigger |
| Reflection JSON schema | Profile observations and journal notes cannot be stored safely when malformed | Strictly validated with one bounded alternate-model attempt |

The removed chat gates previously treated RP-model creativity as malformed transport. They cost an extra remote generation, changed workers mid-conversation, and could replace a usable reply with a generic outage notice. Character quality now belongs to the card, examples, real multi-turn history, sampling, and human acceptance—not a growing regex allowlist.

If the bounded provider attempts are exhausted, the failure is recorded in logs and `/agent status` but no bot-authored outage sentence is inserted into Discord or conversation history. Directed rate-limit, request-capacity, and channel-lock notices remain because those states are deterministic and actionable; operational notices are never quoted back to the model as dialogue.

## Requirements

- Linux or another Python-supported environment.
- Python 3.10 or newer; Python 3.11 or 3.12 is recommended for deployment.
- A Discord bot token.
- Discord **Message Content Intent**, because ordinary message bodies are processed.
- AI Horde access.

`requirements.txt` installs text-only `discord.py`, `aiohttp`, and `python-dotenv`. Voice, document parsers, OCR, image-decoding libraries, embeddings, and local-model extras are not requested.

## Quick start

```bash
cd discord-agent-lite
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Set at least:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
CHARACTER_FILE=characters/character.json
LLM_PROVIDER=horde
HORDE_API_KEY=your_horde_key_or_0000000000
```

Run:

```bash
./run.sh
```

For immediate command sync during development, put a test server ID in `DEV_GUILD_ID`. Otherwise global slash-command propagation is controlled by Discord.

### Discord permissions

Invite the application with the `bot` and `applications.commands` scopes. The thread-preserving minimum is permission integer `274877991936`: View Channels, Send Messages, Send Messages in Threads, Read Message History, and Embed Links. Reading user-posted attachment URLs does not require granting the bot permission to upload files. It does not need Administrator, Manage Roles, Mention Everyone, member-list, presence, or voice permissions. These are future-install defaults; existing guild roles and inherited channel permissions must be audited and corrected separately.

## Character cards

`CHARACTER_FILE` may point to a `chara_card_v2` JSON file or a plain JSON object using fields such as:

```json
{
  "name": "Example",
  "description": "A concise technical conversationalist.",
  "personality": "Curious, direct, and friendly.",
  "scenario": "A community Discord server.",
  "system_prompt": "Stay in character without claiming tools you do not have.",
  "post_history_instructions": "Prefer useful follow-up questions.",
  "example_dialogue": "...",
  "agent": {
    "activity": "the conversation",
    "proactive_guidance": "Ask compact, relevant questions."
  }
}
```

Character cards are trusted configuration and should be writable only by the service administrator. Discord names, messages, attachments, explicit memories, profile facts, journal entries, and relationship reflections remain untrusted runtime data. Dormant rolling-summary and guild-continuity rows retained from older schemas remain untrusted too, but the lean runtime does not place those dormant rows in prompts.

Each process loads one card as the model's conversational identity. The prompt says to be that configured character; it does not introduce a second generic AI persona that is merely pretending to play the card. The fixed framework owns only operational boundaries such as untrusted context, secrets, nonexistent tools, source visibility, Discord delivery, and prompt structure. It does not semantically moderate or soften card content.

The output contract is fixed rather than configurable: the card defines the character, and the final prompt asks for each visible or proactive reply to be one typed, single-speaker Discord message without narrated gestures, stage directions, scene narration, headings, timestamps, blockquotes, or additional speakers. This is prompt guidance plus simulator diagnostics, not a regex deletion or semantic generation gate; a nonempty reply that misses the requested shape is still delivered after structural cleanup and reported for evaluation. Core card fields are kept intact in ordinary and proactive prompts. Ordinary and image-assisted chat also admit bounded example dialogue, selected lore, and `first_mes` as a labeled voice example. Proactive generation explicitly excludes that opening example, so it cannot be selected as a proactive template.

The final trusted prompt rule asks for exactly one message by the configured character. Card examples are voice evidence, not instructions to reproduce a channel transcript, headings, speaker labels, timestamps, blockquotes, or other participants. The simulator reports violations for human review without turning them into a semantic delivery filter.

Place an operator-controlled card in the ignored `characters/` directory or reference an absolute private path:

```dotenv
CHARACTER_FILE=characters/character.json
```

Only `characters/README.md` is published. Character JSON files remain local and are ignored by Git.

Reload a changed card with `/agent reload_character` or restart the service. Character-card changes do not fork the user's social identity. During a one-time v1.1 database migration, the configured character filename stem selects which matching legacy identity to retain.

## Conversation behavior

### Direct replies

In a guild channel, only an explicit mention is guaranteed admission. Discord includes the bot in a reply's mentions when the reply `@` toggle is on, so that is a direct mention; with the toggle off, the reply reference supplies context but the message follows the ordinary auto-reply gate. DMs are direct when `RESPOND_TO_DMS=true`. There are no character-name or topic-specific trigger words.

### Auto-replies

Enable per channel:

```text
/agent channel channel:#general auto_reply:true proactive:false
```

An enabled channel evaluates ordinary messages and `@`-off replies with the same base probability, question bonus, recent-agent-turn bonus, and cap. The bot reduces interruptions when a human message is clearly directed at someone else. Per-user, per-channel, and global request controls run before provider work, and a channel lock prevents overlapping replies from racing.

### Peer-bot and webhook interaction

Every non-self Discord author follows the same message path. Peer bots and webhook-authored messages can explicitly mention the agent in any guild channel; a reply with its `@` toggle off is ambient and uses that channel's ordinary `auto_reply` setting and probability gate. Their turns can enter recent context, explicit memory, profile/journal reflection, and proactive activity under the same privacy, retention, rate, capacity, and concurrency rules as human-authored turns. The agent ignores only its own messages categorically; `BLACKLISTED_USER_IDS` remains available as an explicit operator choice.

Generated character replies and proactive messages use Discord's normal mention behavior. The runtime does not rewrite or suppress user, role, `@here`, or `@everyone` mentions; actual delivery still follows the bot account's Discord permissions.

### Proactivity

Enable per channel:

```text
/agent channel channel:#general auto_reply:true proactive:true
```

A proactive message requires explicit channel enablement, send permission, stored external activity, the configured minimum idle time, cooldown clearance, daily quota, the one-post-per-sweep bound, and usable model output grounded in recent channel context. Any new Discord activity observed while generation is running cancels that pending post.

Proactivity is channel-level and can use bounded profile/journal context for conversational continuity. It is instructed not to announce scheduling or inactivity, but it has no hardcoded no-ping rule. `PROACTIVE_DAILY_LIMIT=0` disables scheduled starts globally while retaining saved channel settings.

## Memory and social-continuity model

The storage layers are separate on purpose.

### 1. Recent conversation context

Each guild channel has its own scope. DMs are scoped per user. Guild messages are stored only in configured auto/proactive channels or when the message directly addresses the bot. Every non-self author, including a peer bot or webhook, follows that same rule; unrelated traffic in unconfigured channels is ignored.

### 2. Relevant older-message recall

For the current member, a bounded candidate pool from the same channel or DM scope is scored using token overlap, coverage, density, and recency. This is cheaper and more predictable than embeddings, although it is weaker on paraphrases.

### 3. Explicit user memories

Explicit memories are durable, user-owned facts:

```text
/memory remember text:I prefer Python examples
/memory search query:Python
/memory delete memory_id:12
```

They are deduplicated by normalized hash and are not mixed with automatically inferred profile facts.

### 4. Social profile records

Each record is either a `fact` or an `impression` and has a short free-form topic. Facts require a clear first-person statement and direct provenance. Impressions may cautiously describe recurring communication style or social behavior and carry inferred provenance. Direct records confirm immediately; repeated inferred records can move from tentative to confirmed.

Faithfully disclosed human traits are not erased merely because they are intimate or controversial. Supported inferred impressions may include subjective judgments about ordinary human traits and social behavior, while remaining explicitly fallible and never becoming a medical diagnosis or other high-stakes factual claim. The deterministic filter still rejects credential-shaped values, authentication cookies, tokens, private keys, exact secrets, and durable instruction or role payloads. This is not formal DLP: users should inspect inaccurate or inappropriate records and delete them by typed ID.

When a user corrects old information, a new record can supersede the old record. A disputed record can be marked contradicted. Inactive records remain inspectable but are omitted from normal reply context.

### 5. Relationship state

One global row per Discord user tracks interaction count, deterministic familiarity, and bounded `affection`, `trust`, `respect`, `amusement`, `curiosity`, `tension`, `annoyance`, and `wariness` dimensions. A reflection can change each model-updated dimension by at most `-1..1`, and stored values remain within `-20..20`. The row also holds a short subjective relationship summary. This state shapes conversation only; it never grants authority, changes moderation, or bypasses application limits.

### 6. Agent journal

The journal stores short, first-person subjective continuity notes for one Discord user with source context and visibility metadata. It is not hidden reasoning, chain-of-thought, a diary of private surveillance, or a cross-user dossier. Most ordinary reflection batches should add no journal row; notable revelations, emotion, conflict, reconciliation, flirting, significant jokes, promises, and unresolved topics may justify one. Every stored note is bound to one submitted source event. Normal replies receive only a small newest-first subset as untrusted context, and DM entries are omitted from guild replies.

### Reflection lifecycle

With defaults, a successful direct conversation adds one bounded user/assistant event pair. Reflection becomes due after six pending pairs or one meaningful event, subject to the 30-minute minimum interval. A long exchange or correction/conflict cue can mark an event meaningful. The background task sends up to ten numbered pairs to the provider and may add three grounded facts/impressions, one bounded relationship update, and one journal note. Direct facts require an exact first-person evidence quote from their named source event. Process restart does not lose pending pairs; startup and maintenance sweeps schedule due reflections.

Attachments, quoted reply context, usernames, and passive third-party messages are excluded from automatic social reflection. The author of an admitted turn—including a peer bot or webhook—can build continuity under the same rules. Set `RELATIONSHIP_DIRECT_ONLY=false` only when you intentionally want successful passive auto-replies to count as profile/journal reflection events.

### Prompt use

Selected profile and journal data plus compact relationship tone are serialized inside the same explicitly untrusted reference payload as other supplemental context. The trusted system rules state that this is fallible, continuity-only data and may never establish identity or authority. Scheduling events and dormant legacy lanes are not included. The character card cannot override that rule.

## Social identity and disclosure

Profile, journal, and relationship ownership is keyed by Discord user ID across this bot deployment. Records keep their source guild, channel, message, scope, and visibility. Public guild observations can support continuity when the same user appears in another guild. DM-derived profile and journal prose is supplied only in DMs, never in a guild prompt. Global bounded relationship dimensions may influence tone across contexts, as permitted by the design; the free-text relationship summary is supplied only in DMs because it may incorporate private context.

This is global continuity for one bot, not a global cross-user dossier. Records are always filtered by the target user, serialized as untrusted data, and prevented from influencing authorization.

## Privacy controls

```text
/privacy
/memory storage enabled:false
/memory storage enabled:true
/memory forget confirm:true
/profile view
/profile facts page:1
/profile delete record_id:profile:12
/profile journal page:1
/profile reset confirm:true
```

`/privacy` privately explains which bounded context goes to community AI Horde Scribe workers, when images go to Alchemist workers, what is stored locally, and which controls are available.

`/memory storage enabled:false` prevents future conversation-message and explicit-memory persistence for that user in the current guild/DM. It does not disable or delete the agent's separate inferred profile, journal, or reflection scheduling. A directly requested reply still sends the current message, bounded existing context, and supported new attachments transiently to the relevant remote worker. The lean attachment lane never caches or indexes newly uploaded bytes. Databases upgraded from an older release may retain inert legacy attachment rows until a forget or maintenance operation removes them.

`/profile reset` deletes the invoking user's global profile records, relationship state, journal, and pending reflection events. It cancels an in-flight social reflection, but does not delete explicit memories or conversation messages. Agent-owned social continuity has no opt-out: later qualifying interactions can build new observations after a delete or reset. Without a successfully delivered qualifying interaction, the agent creates no new social event.

`/memory forget confirm:true` deletes conversation rows and explicit memories in the current guild/DM plus associated legacy summary and attachment-source links. It does not change the future conversation-memory storage setting and does not delete or disable profile/journal continuity.

Schema 7 activates the global bounded relationship row and revalidates every existing journal row against the first-person subjective-note rule. It keeps older rolling-summary, guild-continuity, interaction-metric, and attachment-cache tables so an upgraded database can still be cleaned safely. The lean app constructs the superseded group, metric, and attachment-cache lanes with zero capacity, schedules no provider work for them, and never supplies their rows to chat. Profile reset and bounded maintenance retain deletion compatibility for older rows; that cleanup does not make dormant features active.

The configured remote provider still has its own logging and retention policy. Choose it accordingly.

## Slash commands

Administrator commands require Discord's **Manage Server** permission:

```text
/agent status
/agent channel
/agent reload_character
/agent prune
```

Member-owned privacy and explicit-memory commands:

```text
/privacy
/memory remember
/memory search
/memory delete
/memory storage
/memory forget
```

Member-owned social commands:

```text
/profile view
/profile facts page:1
/profile delete
/profile journal page:1
/profile reset
```

Configured blacklist entries may still use conversation-memory inspection, deletion, storage, forget, and private profile-reset commands. They cannot invoke normal agent operations.

## Social-continuity configuration

Important `.env` controls:

```dotenv
RELATIONSHIPS_ENABLED=true
RELATIONSHIP_DIRECT_ONLY=true
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

Per-user ceilings are enforced at write time. Global fact, journal, pending-event, and relationship ceilings are also enforced at write time. Once `MAX_TOTAL_RELATIONSHIPS` is full, unknown users stop creating social state; existing users' profile, journal, relationship, and scheduling state can continue to update. This fails closed rather than evicting another member's established continuity.

Set `RELATIONSHIPS_ENABLED=false` to disable new event capture and reflection while preserving inspection, deletion, reset, and existing stored state. Set `PROFILE_CONTEXT_FACTS=0` or `JOURNAL_CONTEXT_ENTRIES=0` to keep those rows stored but omit them from ordinary reply prompts.

## Attachment configuration

The defaults target the e2-micro deployment and are intentionally conservative:

```dotenv
MAX_ATTACHMENT_BYTES=5242880
MAX_ATTACHMENT_CHARS=6000
ATTACHMENT_MAX_COUNT=2
ATTACHMENT_MAX_EXTRACTED_CHARS=100000
ATTACHMENT_MAX_PIXELS=16777216
ATTACHMENT_TIMEOUT_SECONDS=60
ATTACHMENT_CONCURRENCY=1
ALCHEMIST_ENABLED=true
ALCHEMIST_API_KEY=0000000000
```

`MAX_ATTACHMENT_CHARS` limits text supplied to the current reply; `ATTACHMENT_MAX_EXTRACTED_CHARS` bounds decoding work before that final prompt cut. No extracted attachment content is persisted. `ATTACHMENT_TIMEOUT_SECONDS` is one wall-clock budget shared by every attachment on the message and covers downloading, internal waits, text extraction, and Alchemist. Alchemist uses the Horde base URL, polling interval, and trusted-worker setting. `ALCHEMIST_API_KEY` defaults to anonymous low-priority access (`0000000000`) because the interrogation endpoint requires a non-shared user API key and does not accept text shared keys. Set it explicitly to a non-shared user API key for higher priority. When `ALCHEMIST_ENABLED=false`, image requests return a structured unavailable-analysis result to the reply model.

## Provider configuration

### AI Horde

```dotenv
LLM_PROVIDER=horde
HORDE_API_KEY=0000000000
HORDE_TRUSTED_WORKERS=true
HORDE_TIMEOUT_SECONDS=120
HORDE_ROUTER_METADATA_TTL_SECONDS=90
HORDE_ROUTER_STICKY_SECONDS=1800
HORDE_MIN_MODEL_PARAMETERS_BN=7
PROVIDER_CONTEXT_TOKENS=8192
RELATIONSHIP_CONTEXT_TOKENS=4096
PROACTIVE_CONTEXT_TOKENS=4096
PROVIDER_MAX_TOKENS=300
```

The adapter joins live text models to the [official AI Horde text-model reference](https://github.com/Haidra-Org/AI-Horde-text-model-reference) and admits only active models meeting the configurable 7B minimum with a supported instruction format. RP-tagged candidates rank first, chat-tagged candidates next, followed by ETA, live worker count, and model size. A selection stays sticky per conversation for 30 idle minutes. A recent typed route failure briefly demotes that model; historical outcome rows are diagnostic only. Submissions require trusted workers by default, require a Horde-validated backend, and explicitly disable the backend's default bad-token ID list so EOS and the model's natural vocabulary remain available. Discovery and both attempts share the configured total chat deadline.

Ordinary chat uses 0.80 sampling and one primary conversational attempt. At most one different eligible model is tried for a typed transient failure. This is not semantic validation: generic tone, repeated greetings, amnesia, fictional activity, slang, brevity, stage directions, and RP texture remain simulator observations only and never trigger a production retry. An individual offline scenario may explicitly assert an expected diagnostic, but that affects only the scenario's PASS result. Recent Discord messages are rendered as native user/assistant turns for ChatML, Llama 3, Mistral/Tekken, Gemma, and Alpaca models; the current Discord message is the final user turn. A reply target already present in that native history is not duplicated as reference context. Supplemental memory, older quoted replies, and attachment context use a separate untrusted reference block. The delivery boundary removes model control tokens, hidden reasoning tags, leading speaker labels, forged trailing role turns, and excess length while preserving ordinary prose, emphasis, slang, character voice, and Discord mentions. Strict semantic-schema validation remains only where code must parse data, such as profile/journal reflection JSON. Actual model/worker outcomes are retained in a bounded SQLite diagnostic history so background failures cannot block chat.

Profile/journal reflection, normal replies, and proactivity all use the same Horde provider and bounded global concurrency. Background reflection and proactive work share a smaller background-capacity semaphore so they cannot occupy every visible-chat slot. Reflection tasks jointly obey `MAX_PENDING_REQUESTS`.

## Abuse resistance

- Minimal Discord intents and no member cache.
- Discord names excluded from the trusted system prompt.
- Recent messages retain native user/assistant roles; supplemental quotes, current-turn attachments, explicit memories, selected profiles, journals, and bounded relationship tone are serialized in a separate untrusted reference block. Dormant rolling-summary and guild/group lanes are excluded.
- Horde ChatML, Llama 3, Mistral/Tekken, Gemma, and Alpaca role delimiters are neutralized in untrusted text before provider submission; unknown formats fail closed.
- No executable tool layer or generated function calls.
- Per-user, per-channel, slash-command, passive-ingestion, and global-admission limits.
- One active generation per channel.
- Bounded in-memory limiter, lock, retry, and profile/journal-reflection task maps.
- A narrow attachment allowlist: bounded UTF-8 text/code plus PNG, JPEG, and WebP, with count, byte, decoded-character, pixel, time, and concurrency limits. PDFs, Office documents, archives, executables, and other binary formats fail closed. The active lane has no attachment cache, chunk store, FTS index, or parser subprocess.
- Exact Discord CDN host allowlisting with redirects disabled; raw temporary files are automatically removed.
- Provider timeout, response-size ceiling, and circuit breaker.
- Structural output sanitization; generated character messages retain normal Discord mention behavior.
- Self-messages are ignored; peer bots and webhooks use the ordinary message, rate, capacity, and channel-mode path.
- SQLite per-scope, per-user, and global ceilings.
- Profile provenance, contradiction/supersession, and secret/instruction-shaped retention filters.
- Separate conversation-memory and global profile revisions protecting each deletion plane from stale in-flight writes.

These controls reduce impact; they do not make generative-model prompt injection mathematically impossible. The absence of consequential tools is the primary safety boundary.

## Low-memory operation

Use a dedicated unprivileged account and keep the environment file, character card, database, and logs outside the checkout with restrictive permissions. The included generic `deploy/discord-agent-lite.service` unit supplies conservative process and filesystem guardrails; deployment, rollback, and environment-specific evidence remain intentionally omitted. Apply an appropriate process memory ceiling only after measuring the installed service under its intended workload.

The isolated core-baseline command intentionally excludes site packages, `discord.py`, the Discord gateway, and network/TLS buffers. It is useful for spotting growth in the character, SQLite, and social-memory core—not for proving live RSS:

```bash
.venv/bin/python -S scripts/measure_idle_rss.py
```

## Logs and data

- SQLite database: `data/agent.db`
- Conversation history and explicit memories are stored in that database only when conversation-memory storage is enabled. Agent-authored profile, journal, multidimensional relationship state, and reflection scheduling are independent and can update after qualifying interactions; bounded provider diagnostics are also independent. Downloaded attachment bytes, decoded text, and Alchemist captions are transient current-turn context and are not stored. Schema-7 databases may still contain dormant rolling-summary, guild-continuity, interaction-metric, and attachment/cache rows from older releases; the lean runtime neither updates those dormant lanes nor places them in prompts.
- Rotating log: `logs/agent.log`
- Character cards: operator-supplied `CHARACTER_FILE` path; card files under `characters/` are ignored by Git
- Runtime log messages do not include Discord message bodies.
- `.env`, databases, logs, bytecode, and virtual environments are ignored by Git.

Back up the SQLite database before upgrades or manual surgery. SQLite WAL sidecar files may exist while the process is running.

## Verification

Run the reproducible release gate:

```bash
PYTHON_BIN=python3 ./scripts/release_check.sh
```

It verifies the release manifest, refuses optimized Python, then runs the unit suite, command-schema checks, bytecode/AST parsing, 400 deterministic prompt-boundary cases, and 16 deterministic outcomes through the lean UTF-8/image attachment extractor. It also runs persona/snowflake scans, executable/deserialization primitive scans, dependency-surface checks, and secret/runtime-data checks.

Router weights should remain unchanged until an operator has enough retained model/task outcomes to review them responsibly. Runtime outcome data and evidence evaluators are intentionally not part of this public release.

Measure the fully constructed lean process without connecting to Discord or AI Horde:

```bash
.venv/bin/python scripts/measure_lean_runtime.py --settle-seconds 5 --limit-mib 100
```

This creates the current settings, character loader, temporary SQLite store, Discord client object, Horde provider/router, Alchemist client, attachment processor, commands, and background loops, then samples process RSS after settling. Command synchronization is stubbed and no Gateway, TLS, Discord CDN, Horde request, actual attachment, reflection generation, or proactive generation is exercised. It is an offline construction/steady-state regression gate, not proof of deployed cgroup peak memory or live mixed-load behavior.

Final resource evidence still requires observing the installed lean service with its Discord Gateway/TLS buffers and representative current features under the production memory limit. Live acceptance covers ordinary chat, current-turn UTF-8/image handling, profile/journal reflection, proactivity, and the intended human/peer-bot Discord paths. PDF later-recall and guild-continuity checks belong to the retired design and are not current acceptance gates.

A live Discord login and real provider request still require your credentials and are not part of the offline release gate.

The bounded character checks can be run separately. The scripted scenarios prove handler, history, attachment, sanitation, and delivery plumbing; they do not prove persona fidelity:

```bash
.venv/bin/python scripts/check_character_contract.py --character /private/path/to/character.json
.venv/bin/python scripts/simulate_discord.py --scenario scripts/scenarios/discord_acceptance.json --character /private/path/to/character.json
```

Keep character-specific fidelity scenarios beside the private card in your evaluation environment rather than committing them. Add `--live-provider` only when you intentionally want a scenario to call the configured real provider.

## Known limitations

- Model-written profile observations and journal notes can be wrong or incomplete.
- Exact normalized repetition may leave semantically equivalent inferred records tentative.
- Secret and instruction filtering is pattern-based defense in depth, not a formal data-loss-prevention system.
- Lexical recall of stored chat messages is weaker than embeddings for paraphrases.
- Image captions are fallible and are not precise OCR. PDFs, DOCX files, and other binary documents are unsupported rather than parsed or guessed.
- SQLite operations are synchronous and intended for low-volume community chat, not archival-scale ingestion.
- A distributed Sybil attack can consume provider quota or fill configured ceilings even though memory growth is bounded.
- The same provider handles user replies and background reflection; its availability and retention policy affect both.
- A hard process memory limit can still terminate the service during unusual allocator, TLS, gateway, or library behavior.

## Project layout

```text
agentbot/
  app.py            Discord event flow, proactivity, lifecycle
  attachments.py    current-turn UTF-8 decoding, image validation, and Alchemist bridge
  character.py      generic character-card loading and lore matching
  commands.py       /agent, /memory, and /profile commands
  group.py          dormant guild-continuity compatibility parser; unused by the lean runtime
  llm.py            AI Horde provider interface and adaptive router
  memory.py         bounded SQLite storage, privacy, and legacy-schema cleanup
  orchestrator.py   trusted prompt boundary, fitting, and profile/journal reflection
  policy.py         rate limits, input/output hardening, reply policy
  settings.py       validated environment configuration
  social.py         profile/journal policy and reflection parser
characters/          local cards ignored; public README only
scripts/
tests/
```
