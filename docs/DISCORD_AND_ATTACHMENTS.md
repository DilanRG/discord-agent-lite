# Discord behavior and attachments

## Direct and ambient admission

The handler distinguishes an actual Discord mention from a reply reference.

| Event | Admission |
| --- | --- |
| Explicit bot user mention | Guaranteed direct turn, subject to resource limits |
| Managed bot-role mention | Guaranteed direct turn |
| Guild reply with Discord `@` toggle on | Direct because Discord includes the bot mention |
| Guild reply with `@` toggle off | Ordinary ambient message; auto-reply probability applies |
| DM while `RESPOND_TO_DMS=true` | Direct |
| Ordinary message in an auto-reply channel | Probability/question/recent-agent gate |
| Ordinary message in an unconfigured channel | Not admitted; may be tracked only when configured for tracking |

There are no character-name, topic, or magic-word triggers. The auto-reply
probability is intentionally interruptible: a question receives a bonus,
messages clearly addressed to another person are less likely to be selected,
and a recent assistant turn can modestly increase continuity. The calculated
probability remains capped.

Peer bots and webhook-authored messages use this same path. They may mention
the bot directly, participate in ambient channels, enter bounded recent history,
and become profile/reflection event authors when the normal directness rules
allow it. There is no bot-to-bot allowlist or special bot ban. The bot's own
messages are the only categorical self-loop guard.

Generated character replies and proactive posts use Discord's normal mention
parsing. The runtime does not rewrite or suppress user, role, `@here`, or
`@everyone` syntax; Discord permissions still determine what actually pings.
Administrative commands and operational notices disable mentions because they
are application-authored status text.

## Admission and delivery order

1. Ignore self, configured blacklist authors, and shutdown work.
2. Resolve scope and a same-channel default reply reference. One uncached
   default reference may be fetched from Discord; cross-channel/deleted/non-
   default references are rejected.
3. Decide direct versus ambient admission.
4. Snapshot privacy/profile revisions.
5. Check per-user, per-channel, request-slot, and global concurrency bounds.
6. Acquire one lock per channel, with a bounded wait.
7. Enter one Discord typing context covering attachment processing and Horde
   generation.
8. Persist the user turn and explicit memory only if the current revision still
   allows it.
9. Call `AgentCore`, clean the result, and send one reply with
   `message.reply(mention_author=False)` or a channel-send fallback.
10. Persist the assistant turn only if delivery and privacy revisions permit it;
    then queue an eligible relationship event.

Provider failures and delivery exceptions are logged and do not create fake
character outage dialogue. Directed rate-limit, capacity, and lock notices are
the exception because they are deterministic and actionable.

## Slash commands

Manage Server is required for operator commands:

```text
/agent status
/agent channel channel:#channel auto_reply:true proactive:false
/agent reload_character
/agent prune
```

Member-scoped commands are private where they expose state:

```text
/privacy
/memory remember text:...
/memory search query:...
/memory delete memory_id:...
/memory storage enabled:false
/memory forget confirm:true
/profile view
/profile facts page:1
/profile delete record_id:...
/profile journal page:1
/profile reset confirm:true
```

Blacklist entries cannot use normal agent operations, but privacy, memory, and
profile controls remain available so stored data cannot be trapped behind a
blacklist. Slash commands have a separate rate limiter.

## Attachment admission

Attachments are processed only after a response passes direct/ambient policy,
rate limits, global request admission, and the channel lock. A message can
process at most `ATTACHMENT_MAX_COUNT` files (default 2) under one absolute
deadline and the configured attachment semaphore.

### Supported lane

- UTF-8 text and source/config files from the explicit extension/MIME allowlist;
- PNG, JPEG, and WebP images whose extension, declared MIME, and magic signature
  agree;
- exact `https://cdn.discordapp.com/attachments/...` URLs only, with redirects
  disabled, bounded response size, and no credentials/fragments.

Text is decoded in-process with byte, control-character, and decoded-character
limits. Image headers are checked for valid dimensions and pixel limits before
bytes are sent to Alchemist. The caption is bounded and explicitly fallible.

### Rejected or transient

PDF, Office, archive, executable, GIF, malformed image, binary-looking text,
non-UTF-8 data, mismatched type/signature, empty/oversized content, and
unsupported URLs fail closed. A timeout, transport error, or Alchemist failure
produces an unavailable current-turn marker that tells the reply model not to
guess; it does not trigger a second chat generation.

Temporary raw files are removed on success, rejection, timeout, exception, or
cancellation. The lean constructor still accepts old cache/chunk/source
arguments for compatibility, but the active path does not write an attachment
cache, FTS index, chunk store, or later-recall record.

## Proactivity and no-chain behavior

Enable proactivity with `/agent channel ... proactive:true`. A scheduled post
also requires send permission, stored participant activity, idle threshold,
cooldown, daily quota, a one-post-per-sweep budget, and a current Discord tail
ID present in the exact SQLite scope. It records its own post and waits for a
new participant event before another start.

The loop rechecks activity and tail identity inside the channel lock and again
before sending. Any new activity observed while Horde is generating cancels the
pending post. This applies to peer bots and webhooks too. Cooldown expiry alone
does not permit a chain of bot messages.
