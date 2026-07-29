# Changelog

## 1.2.0 - 2026-07-30

- Publish the lean Discord-only agent: card-native single-speaker prompting, adaptive RP-first AI Horde routing, agent-owned profile/journal/relationship continuity, probabilistic ambient conversation, bounded proactivity, current-turn UTF-8/code attachments, and transient Alchemist image captions.
- Treat only explicit Discord mentions, including replies whose `@` toggle is on, as guaranteed direct admission. Replies with `@` off and ordinary messages use the configured ambient probability path.
- Keep conversation-memory controls separate from privately viewable and deletable agent-owned social continuity. Social state has no opt-out and cannot alter permissions, moderation, admission, blacklists, rate limits, or concurrency policy.
- Retain schema compatibility for historical attachment-cache, FTS, and guild-continuity tables while leaving those heavier lanes dormant in the accepted 80/20 runtime.
- Preserve structural output sanitation only: transport/control leakage and forged role continuations are removed, while character prose, actions, mentions, and style remain model/card-owned.
- Exclude all operator character cards, credentials, runtime databases, logs, deployment evidence, and private release history from the public repository. Tests and offline resource probes generate short synthetic cards in temporary directories.

### Validation

- The public source tree passes the complete unit suite, deterministic prompt-boundary and attachment fuzzing, manifest verification, compilation/static checks, and the under-100 MiB offline resource gate.
- Live Discord, Horde, reflection, attachment, typing, and proactive behavior were validated privately before publication; private identifiers and operational evidence are intentionally not published.
