# Developer documentation

This directory is the code-level reference for Discord Agent Lite 1.2.1. The
[root README](../README.md) remains the quick-start and user-behavior guide;
`SECURITY.md` is the normative security/privacy policy; and `MIGRATION.md` is
the normative schema and upgrade guide.

## Read in this order

1. [Architecture](ARCHITECTURE.md) — process boundaries, event flow, prompt
   construction, and active versus dormant runtime lanes.
2. [Configuration](CONFIGURATION.md) — every environment variable read by
   `agentbot.settings.Settings`, including defaults, ranges, and dependencies.
3. [Data model](DATA_MODEL.md) — SQLite scopes, ownership, revisions,
   reflection, migration, and deletion semantics.
4. [Prompts and providers](PROMPTS_AND_PROVIDERS.md) — character cards, native
   Horde formats, model routing, retry behavior, and delivery sanitation.
5. [Discord and attachments](DISCORD_AND_ATTACHMENTS.md) — admission,
   mentions, peer bots, typing, files, images, and proactivity.
6. [Testing and release](TESTING.md) — the test matrix, offline gates, live
   acceptance boundaries, manifests, branches, and public/private artifacts.
7. [Operations](OPERATIONS.md) — installation, systemd, backups, rollback,
   logs, troubleshooting, and safe maintenance.

## Source-of-truth rules

- Runtime behavior is defined by `agentbot/` and its tests. Documentation must
  not invent a trigger, permission, model capability, or data path that is not
  present there.
- `.env.example` and `agentbot/settings.py` define configuration. The settings
  loader rejects invalid values rather than silently coercing them.
- Character cards are operator-controlled private data. These documents use
  only generic card examples and never reproduce a private card.
- `AUDIT_REPORT.md`, `PHASE4_EVIDENCE.md`, and database/release evidence are
  private acceptance records, not a public architecture reference.
- The primary checkout may contain private cards; the public mirror is a
  deliberately sanitized code-only artifact and must be rebuilt from an
  explicit allowlist, never copied with a broad archive command.

## Repository at a glance

```text
agentbot/     Runtime package
characters/   Operator-supplied cards; never assume cards are public
deploy/       Generic systemd unit
scripts/      Release, simulator, fuzz, measurement, and manifest tools
tests/        unittest coverage and migration fixtures
data/         Runtime SQLite location (ignored except .gitkeep)
logs/         Runtime log location (ignored except .gitkeep)
```

The runtime is intentionally a small remote-inference service: Discord and
SQLite are local, AI Horde supplies text/captions, and there is no resident
model, embedding index, tool runner, web browser, or voice subsystem.
