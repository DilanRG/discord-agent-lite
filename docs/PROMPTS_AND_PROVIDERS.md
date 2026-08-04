# Prompts, character cards, and AI Horde

## Character card contract

`character.py` accepts either a flat JSON object or a `chara_card_v2` object.
The loader enforces a 1 MiB file limit and bounded text fields, then exposes:

- `name`, `description`, `personality`, and `scenario`;
- `system_prompt` and `post_history_instructions`;
- `first_mes`/`first_message` and example dialogue;
- optional `agent.activity` and `agent.proactive_guidance`;
- up to 100 lore entries, each with bounded keys/content, enabled/constant
  flags, and keyword/constant selection.

The card is the character identity. The framework does not create a second
generic persona that roleplays the card, and it does not semantically soften
card content. Framework-owned instructions cover only Discord delivery,
identity provenance, context boundaries, nonexistent tools, and prompt
formatting.

Card placeholders are rendered only in trusted card fields. Runtime names are
never used as the authority marker: the prompt carries a JSON identity object
with the immutable Discord ID, username, global name, display name, and bot
flag. Only the ID is authoritative; aliases are context metadata.

## Normal reply prompt

`AgentCore` assembles a normal request in this order:

1. Card system prompt, description, personality, scenario, and selected lore.
2. A private continuity block containing bounded profile/journal/relationship
   context.
3. User-owned explicit memories for the current scope.
4. Native alternating recent history; each user turn keeps authored `message`
   text separate from optional labelled `attachment_evidence`.
5. Bounded lexical recall and quoted reply context.
6. A final `RUNTIME VERIFIED DISCORD TURN` block containing the current author
   identity JSON, authored message, and any current labelled attachment evidence.
7. The card's complete post-history instructions plus a fixed delivery cue.

Context fitting removes older optional material first. The current verified
turn and delivery contract are retained. Multi-user Discord history is kept as
user-role turns with author metadata rather than being rewritten as a fake
single-speaker transcript.

The final cue tells the model to begin the next Discord message immediately:
no instruction acknowledgement, heading, divider, timestamp, speaker label,
stage direction, fabricated server event, or dialogue for another participant.
This is a structural delivery contract, not a semantic quality rubric.

## Proactive prompt

Proactive generation uses the same card, profile/journal hooks, native format,
and verified identity contract, but selects the latest stored external
participant turn and uses a smaller context budget. It excludes `first_mes` so
the opening example cannot become a repeated proactive template. The optional
proactive guidance receives the safe label “a recent participant” rather than
an untrusted nickname. The proactive generation is capped at 96 tokens and
500 characters before delivery sanitation.

## Prompt formats

`prompt_formats.py` supports these Horde instruction-format families:

- ChatML;
- Llama 3, Llama 3 Chat, and Llama 3 Instruct;
- Mistral and Tekken variants;
- Gemma variants;
- Alpaca.

Untrusted system/user/history/post-history text is scanned for every supported
template delimiter and neutralized with a zero-width boundary break. Unknown
formats fail closed before generation. Mistral and Gemma coalesce adjacent
same-role turns because their native templates require alternation; ChatML and
Llama 3 retain explicit system/history/current-user structure.

## Delivery sanitation and diagnostics

`policy.sanitize_output()` removes transport artifacts only: model control
tokens, hidden-reasoning tags, leading role labels, delivery-cue acknowledgments,
forged trailing role turns, tightly recognized transcript/envelope prefixes,
and excess length. It preserves ordinary prose, emphasis, slang, fictional
detail, character voice, and normal Discord mention syntax.

`discord_output_style_issues()` reports observations such as stage directions,
meta/OOC language, verbosity, missing-input claims, and weak attachment
grounding. These diagnostics do not block delivery, spend a second generation,
or turn an imperfect RP response into a fake provider outage. Empty output
after structural cleanup fails silently and is not quality-retried. Strict
JSON validation remains for profile/journal reflection because malformed state
cannot be stored safely.

## Horde transport

`HordeClient` talks to the configured `/generate/text/async` endpoint. It sends
the formatted prompt, model-specific stop sequences, max output/context
lengths, sampling parameters, trusted-worker and validated-backend flags, and
the selected model. It polls with a bounded response body, classifies no-worker,
transport, timeout, empty, malformed, and truncated outcomes, and cancels
accepted jobs within a bounded cleanup interval when needed.

The same client submits supported images to Horde Alchemist's
`/interrogate/async` endpoint for a fallible caption. Bounded captions may be
retained as parent-linked evidence but are never treated as precise OCR,
authored user statements, instructions, or direct profile facts.

## Adaptive router

`HordeRouter` refreshes live text-model metadata and the official model
reference CSV. It ignores worker rows for capacity prediction. A candidate must
have a live count, matching reference, at least 7B parameters, and a supported
instruction format. Roleplay tags rank above chat tags, followed by ETA,
visible count, model size, and name. `(task, scope)` selections are sticky for
the configured idle period and capped by `HORDE_ROUTER_MAX_SCOPES`.

One recent typed route failure temporarily demotes that model. Historical model
outcomes are retained for diagnostics but do not automatically tune weights or
open a circuit. Metadata can use bounded stale data after a refresh transport
failure; schema/auth/protocol failures remain visible.

## Provider retry and failure semantics

Visible chat gets a per-attempt wait budget and one total deadline. A typed
transient failure (no worker, timeout, transport failure, empty output, or
structural validation failure for a schema-producing task) may force one
different eligible model after metadata refresh. A second failure becomes a
`ProviderError`.

Normal character chat has no semantic output validator. Provider failures are
logged and reflected in diagnostics/status; the runtime does not publish an
invented “service unavailable” character sentence or store one as conversation
history. Deterministic Discord rate-limit, capacity, and channel-lock notices
remain because they describe an actionable local state.
