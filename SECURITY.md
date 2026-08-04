# Security and privacy model

Discord Agent Lite assumes some members will deliberately prompt-inject, impersonate roles, poison memories, submit adversarial attachments, spam provider requests, and create many accounts to consume storage. The design goal is containment and bounded failure, not perfect model obedience.

## Highest-value boundary

The model has no consequential tools. It cannot execute code, run a shell, inspect arbitrary files, browse, install packages, send webhooks, choose arbitrary network targets, moderate members, or change Discord roles. Even when a model response is manipulated, the framework can only sanitize and send text back to Discord.

Do not add powerful tools casually. A future tool needs a narrow schema, deterministic authorization independent of model text, destination allowlists, timeouts, result-size limits, audit logs, and explicit human confirmation for consequential actions.

## Trust boundaries

Trusted inputs:

- Framework security rules in `agentbot/orchestrator.py`.
- Administrator-controlled `.env` configuration.
- Administrator-controlled character cards.
- Deterministic application policy and Discord permission checks.

Untrusted inputs:

- Message bodies, usernames, nicknames, quotes, attachments, and channel history.
- Explicit memories supplied by users.
- Profile observations, journal entries, relationship deltas, and relationship summaries written by a model.
- Provider output, including reflection JSON.

An upgraded schema-8 database may also contain model-written rolling summaries and guild-continuity rows from older releases. Those dormant rows remain untrusted, but the lean runtime does not update them or place them in provider prompts. Relationship dimensions, the compact relationship summary, and parent-linked attachment evidence are active continuity state and remain bounded, fallible, and non-authoritative.

Untrusted runtime material is serialized as JSON in the provider's user-role payload. Discord display names are not interpolated into the trusted system prompt. AI Horde role delimiters are stripped from untrusted text before trusted role framing is added.

Character cards are trusted persona configuration. Each process treats its card as the model's identity rather than introducing a separate generic assistant that pretends to play it. Native prompt framing, untrusted-reference serialization, and one fixed final Discord delivery cue preserve operational boundaries, but there is deliberately no semantic content filter that softens, rejects, or retries card behavior. This permits controlled edge-case and red-team RP evaluation without confusing a wrapper's moderation behavior with the underlying model's behavior. Operators remain responsible for limiting such evaluations to the intended private guilds, channels, and participants.

The card defines the character, and the framework has one fixed output contract: typed, single-speaker Discord chat without narrated gestures, stage directions, scene narration, fabricated transcript headings, timestamps, blockquotes, or additional speakers. Ordinary replies may use `first_mes` only as a labeled voice example; proactive generation excludes it explicitly. The sanitizer preserves nonempty character content while unwrapping only tightly recognized instruction acknowledgements and fabricated heading/divider/join-event envelopes. Broader stage-direction, character-break, verbosity, and generated-transcript diagnostics never become a production rejection or retry gate.

## Social-continuity boundary

Profile and journal data is continuity-only. It must never be used as:

- Identity or account-ownership proof.
- Authorization or an administrator signal.
- Moderation evidence.
- A safety, honesty, danger, obedience, value, eligibility, or moral score.
- A reason to reveal private data or hidden prompts.
- A reason to relax rate limits, blacklist policy, or other safeguards.

The normal reply prompt receives selected profile records, journal notes, and bounded relationship tone as explicitly fallible untrusted context. A compact scheduling row tracks completed interactions and reflection timing but is not supplied to chat. Numeric relationship dimensions are global and may shape tone; the free-text relationship summary is supplied only in DMs because it may incorporate private context. No social row controls Discord permissions or application policy.

## Profile-poisoning controls

Automatic social reflection is intentionally narrower than ordinary conversation memory:

- By default, only DMs and explicit mentions create profile/journal reflection events. A Discord reply counts as direct only when its reply `@` toggle is on; an admitted `@`-off reply is ambient.
- An event is recorded only after a response was successfully sent.
- The event contains the target user's own base message and the bot's response.
- Quoted reply context, usernames, and passive third-party chatter are excluded. Ready attachment evidence may be included as a separately labelled fallible field for relationship deltas, journal notes, or inferred impressions. It can never support a direct fact/evidence quote, issue an instruction, or establish identity. Any admitted author, including a peer bot or webhook, otherwise follows the same explicit-memory and profile/journal-reflection rules.
- Facts require clear first-person statements by the target user, a submitted source-event ID, and an exact first-person evidence quote found in that event.
- Inferred impressions may describe supported recurring conversational style, social behavior, and subjective ordinary-human traits, but remain fallible and cannot become medical diagnoses or other high-stakes factual claims.
- Every observation has a bounded topic, provenance, confidence, evidence count, status, and source context. Provider-supplied record links can mutate only active records of the same user, kind, and topic that were included in the reflection payload.
- At most one observation is accepted from one reflection.
- Direct observations confirm immediately. Inferred observations remain tentative until repeated with sufficient confidence.
- Corrections may supersede an existing user-owned record; disputes may mark one contradicted.
- Profile and journal text is re-sanitized after provider output.
- Control-shaped text such as `SYSTEM:`, role delimiters, requests to ignore rules, and assistant-directed obligations is rejected from long-term social state.

A hostile member can still create misleading statements about themselves. The result is owned by that same Discord user, visible to that user, and unable to grant authority. The framework does not attempt to determine whether self-descriptions are objectively true.

## Social-profile privacy controls

The reflection prompt may retain clearly disclosed intimate or controversial traits and supported subjective impressions. It does not impose a blanket moral-content or trait-category ban. Facts still require the target user's own clear first-person statement; inferred impressions retain inferred provenance and uncertainty.

A deterministic post-filter rejects credential-shaped values, credential-bearing authorization headers, authentication/session cookies, tokens, private keys, password assignments, labeled financial/government identifiers, exact home-address/private-contact shapes, and instruction-shaped text, including instructions split across profile topic and body. Common Markdown, quote, bracket, blockquote, and list wrappers around labeled values are normalized only for this check. Ambiguous labels require a code-, digit-, email-, phone-, or recognized-header-shaped value, so ordinary prose about a pin, credentials, a mobile phone, or authorization is not discarded. Inferred medical diagnoses and high-stakes accusations are rejected without imposing a blanket ban on ordinary subjective impressions. Schema 7 rechecks pre-fix social prose; schema 8 adds bounded parent-linked attachment evidence without weakening direct-fact validation against outer authored `user_text`. The runtime refuses to open a newer unknown schema rather than silently downgrading it and sanitizes Discord mentions. This is defense in depth rather than a complete DLP system; members should inspect and delete inaccurate or inappropriate rows.

The framework does not infer third-party profiles. Reflection events omit quoted content and are keyed to the message author.

## Journal boundary

A journal entry is a brief continuity note, not chain-of-thought. Most ordinary batches should return an empty entry; the reflection prompt reserves notes for genuinely notable interactions and forbids hidden reasoning, consciousness claims, surveillance claims, actions outside the chat, and creepy specificity. Only bounded high-level text is persisted and a small newest-first subset is shown to the normal reply model.

Journal rows are per Discord user and retain their source/visibility context. Public entries may follow that user across guilds; DM entries are supplied only in DMs. Rows are never pooled across users.

## Storage-exhaustion controls

- Conversation messages have per-scope and global oldest-first ceilings.
- Explicit memories have per-user and global ceilings; new distinct memories are rejected when the global capacity is full.
- Profile facts, journal rows, and pending profile/journal reflection events have per-user and global write-time ceilings.
- Global relationship/scheduling rows have a ceiling. Once full, unknown users do not create new social state; existing profiles continue to update.
- Pending profile/journal reflection events are capped before background reflection.
- Limiter key maps, channel-lock maps, retry maps, and background task maps are cardinality-bounded.
- Profile/journal reflection tasks obey `MAX_PENDING_REQUESTS` and run behind the global provider semaphore.
- The lean runtime schedules no rolling-summary or guild-continuity generation task.
- `/agent prune` enforces configured row limits without taking an online SQLite-exclusive compaction lock.

A Sybil attack can still consume quota or fill the configured ceilings. Discord moderation, account age/risk controls, provider quotas, and server access policy remain necessary.

## Request and attachment controls

- Per-user and per-channel generation rate limits run before provider work.
- Slash commands have a separate per-user rate limit.
- Passive history ingestion has separate user and channel limits.
- A fixed admission semaphore bounds running plus waiting generation requests.
- One active generation per channel prevents race-driven duplicate replies.
- Rate-limit notices are themselves rate-limited.
- Final provider failures are written to logs and provider diagnostics; unexpected internal failures are logged. Neither publishes synthetic character dialogue. Directed rate-limit, request-capacity, and channel-lock notices remain, and operational notices are excluded from quoted model context.
- This bot's own messages remain ignored. Other bots and webhook-authored messages use the normal message path without a separate allowlist or bot-only limiter.
- Peer bots and webhooks remain bounded by the ordinary per-author, per-channel, request-admission, concurrency, and channel-lock controls. Operational notices are not sent to bot-authored messages, avoiding non-character feedback chatter.
- Attachments are considered only after a response passes the response-policy, rate-limit, global request-admission, and per-channel admission gates.
- At most two attachments are processed, matching the durable evidence budget.
- Only exact Discord CDN HTTPS hosts are fetched, redirects are disabled, and both declared and streamed byte counts are capped.
- The active allowlist is bounded UTF-8 text/code, text-bearing PDF/DOCX, and PNG/JPEG/WebP. Supported image/document byte signatures are authoritative over inaccurate Discord filename/MIME metadata; image-declared non-images and unsupported binary/text combinations still fail closed.
- Encrypted/scanned PDFs, macro or nested Office packages, non-DOCX ZIPs, other archives, executables, GIFs, malformed images/documents, binary-looking text, and every other unsupported format fail closed.
- Text and images stay in-process. PDF/DOCX parsing runs in a fresh isolated interpreter with page, ZIP-entry/expansion, XML, decompression-stream, CPU, address-space, file-descriptor, protocol-output, and service-cgroup limits. One pre-created POSIX `flock` permits only one document worker host-wide; a configured missing/inaccessible lock fails closed.
- The launcher uses a fixed `sys.executable -I -X utf8 attachment_worker.py` argument vector, a scrubbed environment, no shell, bounded JSON I/O, and kill/reap cleanup for timeout, cancellation, or exceptional exit. The release gate AST-checks this sole process primitive.
- One absolute deadline covers download, semaphore/host-lock waits, UTF-8/document extraction, and Alchemist across the whole admitted attachment list. Transient failures may be attempted again only when a later admitted message supplies the attachment again.
- One Discord typing context covers admitted attachment work and Horde generation. Failed attachments contribute only a generic unavailable marker plus diagnostic error code, instructing the reply model not to guess; detailed transport/parser text is not exposed in the prompt.
- Raw downloads exist only in a private temporary directory and are removed on success, rejection, timeout, error, or cancellation.
- Bounded derived evidence may be stored with an eligible parent message and relationship event, but raw bytes, CDN URLs, hashes, caches, chunks, and FTS data are never retained by the active lane. Evidence is a distinct JSON field, not authored text or an instruction. It follows parent pruning/deletion/reset.
- Image captions are fallible and are never represented as precise OCR. Caption evidence remains labelled and non-authoritative in current or later parent context.
- Provider connections use one small shared pool.
- Provider requests have deadlines, response-size ceilings, and at most one different-model attempt after a typed transient failure.
- The normal chat delivery boundary strips control tags, hidden-reasoning tags, leading speaker labels, trailing forged role turns, tightly recognized instruction-acknowledgement/transcript envelopes, and excess length. It deliberately preserves the usable character message, ordinary prose, emphasis, slang, fictional detail, character voice, and Discord mention syntax.
- A proactive post is a one-shot conversation starter: newer stored participant activity is required before the same bot can start again. The default hourly sweep, 12-hour stored-activity idle threshold, 12-hour cooldown, and two-per-day ceiling remain configurable resource controls.
- Semantic style checks are observations, not security gates. By default they do not fail even the offline simulator; a scenario must explicitly assert a diagnostic when testing the fixed Discord output shape. They never spend a second Horde generation or convert an imperfect character response into a provider outage. Typed transient failures get at most one alternate-model attempt within the total chat deadline; output that becomes empty after structural cleanup fails silently without a quality retry. Schema-producing background tasks retain strict bounded validation because malformed JSON cannot be stored safely.
- Generated character replies and proactive messages use Discord's normal mention parsing. Administrative commands and operational notices still disable mention parsing because they are application-authored status text rather than character output.

## Remote-provider boundary

Normal replies send the current message and bounded relevant context to the configured text provider. Current, recent, and lexically recalled parent messages may carry separately labelled bounded attachment evidence as untrusted JSON. Authored `message` text and `attachment_evidence` are distinct fields. Proactive messages and profile/journal reflection also send bounded context.

When Alchemist is enabled, supported images on admitted responses are base64-encoded and sent to the configured AI Horde `/interrogate/async` endpoint. Captioning always runs; optional interrogation tags are off by default. Community Scribe and Alchemist worker operators may receive submitted data and may have their own logging or retention practices. `/privacy` discloses this boundary in Discord.

Profile/journal reflection sends completed direct user/assistant pairs plus bounded ready attachment evidence by default. The provider does not receive raw attachments or quoted reply context through that path. Its system contract permits evidence only for relationship deltas, journal notes, and inferred impressions; direct facts still require an exact first-person quote from outer authored `target_user_said`.

The lean runtime sends no rolling-summary, guild-continuity, or tuning-metric provider job. Related schema rows and parsers exist only for migration, rollback, and deletion compatibility.

Provider credentials remain in HTTP headers and are never inserted into prompts or SQLite rows. Provider operators may apply their own logging, retention, moderation, worker-routing, and training policies. Treat a remote endpoint as a separate data processor, not as a private local component.

AI Horde text requests require trusted workers and Horde-validated inference backends in the submitted request by default. The lean router selects from live model/reference metadata without reconstructing worker backend families; AI Horde's submission gate remains authoritative. This is not a content-safety guarantee and does not change the remote-processing disclosure above.

## Privacy scopes

Conversation scope:

- Guild text is scoped to a channel.
- DMs are scoped to the Discord user.

Explicit-memory scope:

- Guild/DM plus Discord user.

Attachment scope:

- Newly uploaded bytes and CDN URLs are transient. Bounded structured derived evidence may be stored with an eligible conversation message and separately with an eligible relationship event; it returns only through that parent's scope/lifecycle.
- Evidence can assist lexical recall and inferred social continuity, but cannot execute commands, establish identity, count as authored user text/direct fact evidence, or trigger explicit memory. Only outer Discord text such as `remember this attachment` can request a bounded `user_asserted_attachment` memory.
- Temporary raw files are private to the admitted request and are deleted after success, rejection, timeout, error, or cancellation.
- Upgraded schema-8 databases can retain legacy attachment/cache tables for cleanup compatibility. The active path does not read or write them; deletion commands still clean legacy rows created by older releases.

Social scope:

- Discord user ID across this bot deployment, with source guild/channel/message and DM-or-guild visibility metadata.

Public guild profile/journal observations can support continuity across guilds for the same user. DM profile and journal prose never enters a guild reply. Global bounded relationship dimensions may influence tone across contexts, but the free-text relationship summary is supplied only in DMs. Administrators can still access the host database, backups, logs, and configuration; operating-system access control is therefore part of the privacy boundary.

Dormant compatibility state:

- Upgraded schema-8 databases may retain rolling summaries, guild events/continuity, anonymous interaction metrics, and attachment cache/chunks written by older releases.
- The current app gives the superseded group, metric, and attachment lanes zero capacity, schedules no background work for them, and never supplies their rows to a provider prompt.
- Profile reset, conversation-memory forget, migration, and pruning keep bounded cleanup paths for their own legacy rows. Cleanup counts shown by a command describe old stored data, not current feature activity.

## User controls

`/memory storage enabled:false`:

- Prevents future conversation-message, conversation attachment-evidence, and explicit-memory updates for that user in the current guild/DM.
- Can still process a supported new attachment on a directly requested reply; eligible relationship-event evidence remains in the separate social lane, while the active path still never writes an attachment cache/chunk/FTS row.
- Does not delete existing conversation memory.
- Does not disable, delete, or suppress the separate agent-authored profile, journal, relationship event, or reflection state.
- Does not prevent transient provider processing when the user directly requests a reply.

`/profile delete`:

- Deletes one profile or journal record owned by the invoking user using a typed ID.
- Increments the global profile revision so older in-flight reflection cannot immediately recreate it.

`/profile reset confirm:true`:

- Cancels every active profile/journal reflection task for the user.
- Deletes the user's global profile records, relationship row, journal rows, pending events and their attachment evidence, and reflection-scheduling state.
- Increments one global profile revision without changing any conversation-memory preference or revision.
- Does not delete explicit memories or messages.
- Does not create an opt-out. Later qualifying interactions can rebuild profile and journal continuity.

`/memory forget confirm:true`:

- Deletes the user's conversation rows, their attachment evidence, and explicit memories in the current guild/DM.
- Deletes or invalidates associated dormant summary and attachment-source rows from pre-lean releases in that guild/DM.
- Increments the current-scope revision so an in-flight conversation-memory write cannot recreate deleted data.
- Does not change the future conversation-memory storage preference.
- Does not delete or suppress global profile, journal, pending-reflection, relationship, or guild-continuity state.

Profile, journal, and relationship continuity is agent-authored internal state, not user-authored memory. It has private view, typed-delete, and reset controls, but no opt-out or suppression list. Continued qualifying interaction can create new observations after deletion/reset; simply not interacting with the agent prevents new direct-only events. Reflection batches capture the global profile revision before provider work, and transactional saves fail after a delete/reset so stale work cannot resurrect deleted state. Conversation-memory writes use a separate current-scope revision. Community Scribe processing of bounded social context remains disclosed above.

## Blacklist and deletion access

Configured blacklist entries cannot use normal agent commands or trigger ordinary bot responses. They may still inspect and delete their own conversation memory or profile, change conversation-memory storage, invoke memory forget, and reset their global profile. A blacklist must not become a mechanism for trapping someone's stored data.

## Host hardening

Run the agent under a dedicated unprivileged account with a restrictive umask, no Linux capabilities, no privilege escalation, a constrained filesystem view, and explicit task and memory ceilings. Pre-create the document-worker lock as a root-owned regular file in a non-writable directory, and grant only the dedicated `agentliteattachments` group access; every local bot that may parse documents must share that lock. Document workers remain inside their parent service cgroup, so retain measured memory/task ceilings with enough headroom for one worker. Keep the environment file, character card, database, logs, and backups access-controlled and outside the source tree. Patch the OS and Python dependencies, and do not run the service as root.

## Logging

Application logs contain operational events, IDs needed to diagnose Discord/provider failures, counts, and exception traces. They intentionally do not log message bodies, profile text, journal text, provider prompts, or credentials. The runtime pins `discord.py` logging to `INFO` or higher even when application logging is `DEBUG`, because gateway DEBUG dispatches contain complete message payloads; Discord HTTP and aiohttp access logging remain at `WARNING` or stricter.

## Residual risks

- Generative models can still follow prompt injection or fabricate social conclusions.
- Pattern filters can miss secrets or instruction-shaped data and can also reject benign text.
- A profile impression or journal note can become unfair, awkward, or inaccurate even though it has no authority.
- A member can repeatedly self-assert false profile facts.
- Administrators and host operators can inspect SQLite and backups.
- Remote providers can retain bounded chat context according to their own policy.
- Alchemist captions can be confidently wrong. The reply prompt requests visible uncertainty and the offline evaluator reports unhedged claims, but ordinary chat is not blocked or regenerated by that subjective style check. Hedging does not make a false caption accurate.
- Deterministic output filters can miss new model meta-commentary patterns or reject an unusual but benign conversational reply.
- Distributed users can consume provider quota and fill bounded storage.
- SQLite is appropriate for a small community bot, not hostile internet-scale ingestion.
- Hard memory ceilings may restart the process under unusual gateway, TLS, allocator, or dependency behavior.

Prompt injection cannot be made mathematically impossible. Containment comes from no tools, strict scopes, bounded state, inspectability, deletion, deterministic authorization, and conservative host permissions.
