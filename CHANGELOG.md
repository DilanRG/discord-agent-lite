# Changelog

## 1.3.0 - 2026-08-04

- Make supported image/document bytes authoritative when Discord filename or MIME metadata is inaccurate, fixing valid PNGs that previously bypassed Alchemist.
- Add text-bearing PDF and DOCX support through a short-lived isolated parser with strict page/archive/decompression/resource limits, cancellation-safe reaping, and one fail-closed host-wide POSIX lock.
- Advance SQLite additively to schema 8 and retain only bounded structured attachment evidence with eligible message and relationship-event parents--never raw bytes, URLs, hashes, caches, chunks, or FTS rows.
- Return labelled evidence with parent history and lexical recall while keeping it separate from authored Discord text. It can inform inferred social continuity, but cannot act as an instruction, establish identity/direct profile facts, or trigger explicit memory without an outer `remember this attachment` request.
- Update `/privacy`, memory controls, migration/operations guidance, simulator coverage, deterministic intake checks, and Linux-only lock/worker validation for the new lifecycle.

### Validation

- The public release gate covers the full unit suite, deterministic prompt/intake fuzzing, manifest/static/dependency checks, and the under-100 MiB offline runtime ceiling.
- The document worker and real `flock` deadline path are also exercised under Linux before deployment.

## 1.2.1 - 2026-08-02

- Bind stored Discord continuity to immutable account IDs while retaining mutable display names as presentation-only aliases.
- Restore Discord typing visibility for accepted turns.
- Make proactive turns require a current, stored participant message and carry that verified turn through the final prompt, preventing stale-startup and context-free output.

### Validation

- Re-ran the complete public release gate after the focused identity, prompt, simulator, memory, and proactive regressions.
- The public artifact remains code-only: no operator cards, credentials, runtime state, private evidence, or private identifiers are included.

## 1.2.0 - 2026-07-30

- Publish the lean Discord-only agent: card-native single-speaker prompting, adaptive RP-first AI Horde routing, agent-owned profile/journal/relationship continuity, probabilistic ambient conversation, bounded proactivity, current-turn UTF-8/code attachments, and transient Alchemist image captions.
- Treat only explicit Discord mentions, including replies whose `@` toggle is on, as guaranteed direct admission. Replies with `@` off and ordinary messages use the configured ambient probability path.
- Keep conversation-memory controls separate from privately viewable and deletable agent-owned social continuity. Social state has no opt-out and cannot alter permissions, moderation, admission, blacklists, rate limits, or concurrency policy.
- Retain schema compatibility for historical attachment-cache, FTS, and guild-continuity tables while leaving those heavier lanes dormant in the accepted 80/20 runtime.
- Preserve structural output sanitation: transport/control leakage and forged role continuations are removed; tightly recognized acknowledgement or fabricated transcript envelopes are unwrapped without applying a semantic character-quality gate.
- Keep reactive and proactive generation silent in Discord. Proactivity runs hourly by default, requires 12 hours of stored participant idleness, and cannot chain another bot-initiated turn until a participant has spoken again.
- Pin Discord library logging above gateway DEBUG so raw message payloads cannot enter the application log.
- Exclude all operator character cards, credentials, runtime databases, logs, deployment evidence, and private release history from the public repository. Tests and offline resource probes generate short synthetic cards in temporary directories.

### Validation

- The public source tree passes the complete unit suite, deterministic prompt-boundary and attachment fuzzing, manifest verification, compilation/static checks, and the under-100 MiB offline resource gate.
- Live Discord, Horde, reflection, attachment, and proactive behavior were validated privately before publication; private identifiers and operational evidence are intentionally not published.
