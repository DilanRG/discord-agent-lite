# Testing, evidence, and release workflow

## Local prerequisites

The runtime requires Python 3.10+ and the three packages in
`requirements.txt`: `discord.py`, `aiohttp`, `python-dotenv`, and pinned
`pypdf==6.14.2`. The test
suite uses the standard library `unittest`; pytest is not required.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The suite is designed to run without Discord credentials, an AI Horde request,
a card, or an external database. Tests that inspect a card use content-free
contract checks and should run only in the private environment that contains
the operator card.

## Test layers

| Layer | What it proves | What it does not prove |
| --- | --- | --- |
| Unit tests | Pure policy, memory, settings, Horde parsing, prompt formatting, shutdown, and command contracts | Live Discord/Horde behavior |
| `DiscordTurnSimulator` | The real `AgentBot.on_message` path with fake Discord objects, including admission, typing, attachments, delivery, identity, peer bots, and proactivity | Provider persona fidelity or gateway behavior |
| `scripts/simulate_discord.py` | Scenario plumbing, prompt bounds, delivery/sanitizer diagnostics, and optional named expectations | A human-quality character response unless a live provider/card is supplied |
| Prompt fuzz | Boundary neutralization and reflection/reference payload limits | Model safety against all prompt injection |
| Attachment fuzz | Deterministic UTF-8/signature/pixel/size cases | Discord CDN availability or Alchemist accuracy |
| RSS measurement | Offline constructed-process steady state under the configured limit | Live Gateway/TLS/cgroup peak under mixed load |
| Live acceptance | Real Discord messages, typing, provider routing, cards, profiles/journal, proactivity, and peer-bot paths | Long-term provider uptime or model quality across all workers |

Important simulator regressions include:

- reply `@` toggle on versus `@`-off admission;
- exact immutable-ID ownership despite forged identity markers or nickname
  changes;
- peer-bot/webhook direct and ambient behavior without a bot allowlist;
- typing spanning attachment work and generation;
- no attachment work on declined or lock-timeout turns;
- structural sanitizer silence on empty output and recovery on the next turn;
- proactive stale-tail, no-chain, cooldown, daily-cap, and in-flight activity
  races;
- profile reset/storage opt-out races and independent revision planes;
- delivery fallback and failure containment.

Orchestrator and social-memory tests cover card/native prompt order, opening
example gating, profile/journal visibility, relationship tone, attachment and
reply deduplication, reflection provenance, bounded records, atomic saves,
deletion races, v1.1-to-schema-8 migration, and future-schema rejection.

## Reproducible release gate

On Linux, WSL, or the deployment host:

```bash
PYTHON_BIN=.venv/bin/python ./scripts/release_check.sh
```

The gate:

1. Verifies `MANIFEST.sha256`, safe paths, hashes, symlink rules, and tracked
   file completeness.
2. Refuses optimized Python because assertions are required.
3. Runs `unittest discover`.
4. Runs bytecode compilation and AST/dependency/secret/runtime-data scans.
5. Runs 400 deterministic prompt-boundary cases.
6. Runs 16 deterministic attachment-extractor cases.
7. Measures constructed-process RSS and enforces the configured ceiling.
8. Checks the version/client-agent string and character-contract invariants.

The current public release candidate records 172 tests, 400 boundary cases,
16 attachment cases, static/dependency checks, and a sub-100 MiB offline RSS
sample. Linux exercises the real host-wide document lock path; other platforms
retain one expected POSIX-only skip.

The release gate is not live acceptance. A live request is required to verify
Discord permissions, real typing, Horde worker routing, card fidelity,
profile/journal reflection, byte-authoritative image plus text/PDF/DOCX handling,
parent-linked evidence recall and authority boundaries, proactive
eligibility, and intended peer-bot behavior.

## Scenario runner

```bash
.venv/bin/python scripts/simulate_discord.py \
  --scenario scripts/scenarios/discord_acceptance.json
```

Scenario files can assert output plumbing and explicitly named expectations.
`*_fidelity_probe.json` scenarios are private edge-case probes and require a
real provider/card when run with `--live-provider`; do not treat a scripted
provider result as persona evidence.

## Branch and artifact policy

- `legacy` is the historical archive and is not a deployment target.
- `experimental` is where implementation and release candidates are validated.
- `master` is promoted only after explicit acceptance and live evidence.
- Versioned deployment releases are immutable; a service symlink selects the
  active release and rollback changes that selection.
- `.env`, databases/WAL files, logs, virtual environments, release work, keys,
  and private cards are not public artifacts.
- The public mirror must be built from a reviewed allowlist. Never use a broad
  `git archive`, recursive copy, or release bundle that could include cards,
  `.env`, databases, evidence, or keys.

The manifest is regenerated only after the final tracked file set is known.
After a release commit, verify the manifest, tag, branch ancestry, exclusions,
and a fresh-clone privacy scan.
