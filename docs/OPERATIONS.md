# Operations and maintenance

## Development install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
./run.sh
```

`run.sh` requires an executable `.venv/bin/python`, enables unbuffered output
and bounded malloc arenas, changes to the repository root, and executes
`python -m agentbot`. On Windows, use the virtual-environment Python directly
for unit tests; `run.sh` and the systemd unit are Linux-oriented.

## Systemd deployment

The generic unit is `deploy/discord-agent-lite.service`. A production install
should use a dedicated unprivileged account and versioned immutable releases:

```text
/opt/discord-agent-lite-releases/<release>
/opt/discord-agent-lite -> <release>
/var/lib/discord-agent-lite/data/agent.db
/var/lib/discord-agent-lite/logs/agent.log
/etc/discord-agent-lite.env
```

The environment file should be root-owned and mode `0600`; the service account
needs write access only to persistent `data/` and `logs/`. Use absolute paths
for `CHARACTER_FILE`, `DATABASE_PATH`, and `LOG_PATH` so a symlink switch does
not move state between releases.

The unit runs as `discordbot`, uses a restrictive umask, no Linux capabilities,
no-new-privileges, private temporary storage, protected system/home paths, and
write restrictions. Its default memory pressure/hard ceiling is 90/120 MiB
with a 64-task limit. These are containment limits, not proof that every live
workload fits; observe the service after deployment.

PDF/DOCX parsing also requires the shared lock declared by
`deploy/agent-lite-attachments.conf`. Install it before starting a unit that
uses the supplied `SupplementaryGroups` setting:

```bash
groupadd --system agentliteattachments  # only when the group does not exist
usermod --append --groups agentliteattachments discordbot
install -o root -g root -m 0644 deploy/agent-lite-attachments.conf \
  /etc/tmpfiles.d/agent-lite-attachments.conf
systemd-tmpfiles --create /etc/tmpfiles.d/agent-lite-attachments.conf
stat /run/lock/agent-lite-attachments.lock
```

Add each independently deployed bot account to that same group only when its
unit supports document parsing. The runtime opens the pre-created file and
fails closed when the path/group/lock is unavailable; it never falls back to
ungated document workers.

Inspect health and resource state with:

```bash
systemctl status discord-agent-lite --no-pager
systemctl show discord-agent-lite \
  -p ActiveState -p SubState -p MainPID -p InvocationID \
  -p NRestarts -p ExecMainStatus -p MemoryCurrent -p MemoryPeak
journalctl -u discord-agent-lite -n 100 --no-pager
```

Inside Discord, `/agent status` reports character/provider state, current and
peak RSS, storage counts, pending reflections, router selections/failures, and
active attachment counters. It does not claim that dormant compatibility rows
are active feature state.

## Backups, migration, and rollback

Before upgrades or manual SQLite work, stop the service and back up the
database, WAL sidecar, `.env`, and operator card separately. Never run old and
new processes against one SQLite file concurrently.

The v1.1 migration selects exactly one legacy identity by the configured card
filename stem. It fails closed on mismatch or ambiguity, preserves IDs and
high-water marks, and applies current retention/provenance filters. A v1.1
binary cannot read schema-8 tables; rollback therefore requires restoring the
complete pre-migration database backup, not copying selected tables.

For a versioned release, stage and verify the complete manifest and dependency
environment before activation. Keep the previous symlink target and database
backup until post-start checks pass. If health checks fail, stop the new unit,
restore the prior symlink/database/environment, and verify the old process
identity, schema, permissions, and card hash. Do not edit cards as part of an
application rollback.

## Logging and privacy

Application logs record operational IDs, counts, provider outcomes, and
exceptions but intentionally do not record message bodies, prompt text, profile
text, journal text, or credentials. Keep Discord library logging at `INFO` or
higher even when application logging is `DEBUG`; gateway debug payloads can
contain complete message content.

The remote provider is a separate data processor. A normal admitted reply can
send current text, bounded context, identity metadata, and labelled attachment
evidence. Reflection sends bounded direct pairs plus eligible attachment
evidence under a stricter non-authority cue; quoted reply context is excluded.
Alchemist receives supported image bytes for a fallible caption. `/privacy` is
the user-facing disclosure.

## Troubleshooting

### The bot is online but does not reply

Check that the Discord Message Content Intent is enabled, the bot can view/read
the channel, direct mentions are present, DMs are enabled if expected, and the
channel's saved `/agent channel` flags are correct. Ambient `@`-off replies do
not bypass the probability gate.

### The bot says nothing after a provider failure

This is intentional. Provider failures are logged and appear in status/outcome
diagnostics; the runtime does not publish a generic outage sentence as the
character. Check Horde metadata, eligible models, API key priority, timeout,
and recent model failures.

### Proactive messages stop

Check the saved proactive flag, send permission, idle/cooldown/daily settings,
stored participant activity, and exact tail-ID membership. Missed or unrecorded
new activity deliberately blocks a proactive turn until the bot has a current
stored tail. Cooldown expiration alone cannot create a chain.

### Attachments are unavailable

Confirm the URL is an exact Discord CDN attachment with no redirect, the
actual bytes identify supported UTF-8/PNG/JPEG/WebP/PDF/DOCX, and the
byte/pixel/page/archive/time limits are sufficient. For PDF/DOCX also verify
the shared lock file, service supplementary group, pinned `pypdf`, and worker
exit/resource diagnostics. An unavailable marker is safer than guessing.
Reattach on a later admitted message to retry transient work.

### Profile state looks wrong

Compare the immutable Discord author ID, not a nickname. Use private `/profile`
views and typed deletion/reset. Profile/journal reflection is eventual and can
be fallible; deletion advances a revision so stale background work cannot
resurrect the removed state.

## Public/private artifact boundary

The primary checkout may contain operator cards and private acceptance evidence.
The public mirror must contain code and generic documentation only. Before
publishing, inspect the explicit file allowlist, regenerate the public manifest,
scan tracked files and reachable refs for cards/secrets/private identifiers,
run the public release gate, and verify from a fresh clone. A public README may
describe the card contract but must not include a real card or private server
configuration.
