from __future__ import annotations

import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Hashable
from dataclasses import dataclass


_WORD_RE = re.compile(r"[^\W_][\w'\-]{1,}", re.UNICODE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HIDDEN_TAG_RE = re.compile(
    r"<(analysis|think|thinking|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_MODEL_CHANNEL_PREAMBLE_RE = re.compile(
    r"^\s*<\|channel>\s*(?:analysis|commentary|thought|reasoning)\s*"
    r"(?:\r?\n)+\s*<channel\|>\s*",
    re.IGNORECASE,
)
_MODEL_CONTROL_TOKEN_RE = re.compile(
    r"<\|[A-Za-z0-9_.-]{1,64}\|?>|<[A-Za-z0-9_.-]{1,64}\|>"
)
_ROLE_PREFIX_RE = re.compile(r"^\s*(assistant|bot)\s*:\s*", re.IGNORECASE)
_ANY_ROLE_PREFIX_RE = re.compile(
    r"^\s*(assistant|bot|system|developer|user|human)\s*:\s*",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_RE = re.compile(
    r"^\s*(?:please\s+)?remember(?:\s+that)?[,:]?\s+(.{3,400})\s*$",
    re.IGNORECASE | re.DOTALL,
)
_LIGHTWEIGHT_CHAT_RE = re.compile(
    r"^(?:h+i+|h+e+y+|hello+|yo+|sup|what'?s\s+up|oh+|o+h+|ah+|"
    r"ok(?:ay)?|alright|sure|yep|yeah|yea|nah|nope|lol|lmao|rofl|nice|"
    r"cool|thanks?|thank\s+you|got\s+it|fair\s+enough|makes\s+sense|"
    r"morning(?:\s+(?:everybody|everyone|all))?|"
    r"good\s+(?:morning|afternoon|evening|night))(?:[\W_])*$",
    re.IGNORECASE,
)
_GREETING_START_RE = re.compile(
    r"^(?:oh[,! ]+)?(?:hey|hello|hi|yo|sup|what(?:'s|\s+is)\s+(?:up|good))"
    r"(?:\s+there|\s+again)?\b",
    re.IGNORECASE,
)
_GENERIC_SUPPORT_RE = re.compile(
    r"\b(?:how can i help|i(?:'m| am) here if you need|let me know if you need|"
    r"would you like me to|is there anything else i can help|what brings you by)\b",
    re.IGNORECASE,
)
_INTERNAL_PROCESS_CLAIM_RE = re.compile(
    r"\b(?:conversation|chat)\s+(?:log|record)s?\b|\brecord of (?:sending|saying)\b|"
    r"\bi\s+(?:do not|don't|cannot|can't)\s+(?:actually\s+)?have access to\s+"
    r"(?:track|see|view|remember)\b",
    re.IGNORECASE,
)
_MISSING_INPUT_CLAIM_RE = re.compile(
    r"\bi\s+(?:have\s+not|haven't|did\s+not|didn't)\s+"
    r"(?:see|receive)\s+(?:any\s+)?(?:message|input)(?:\s+content)?\b|"
    r"\bissue\s+with\s+(?:your\s+)?(?:input\s+format|setup)\b|"
    r"\btry\s+sending\s+the\s+actual\s+message\b",
    re.IGNORECASE,
)
_ATTACHMENT_CONTEXT_MISS_RE = re.compile(
    r"(?<!if )(?<!when )(?<!whether )\b"
    r"(?:the|this|your)\s+(?:attached\s+)?(?:file|attachment|document)\s+"
    r"(?:"
    r"(?:is|was|looks?|appears?|seems?)\s+(?:to\s+be\s+)?"
    r"(?:completely\s+)?(?:empty|blank)\b|"
    r"(?:does(?:n't| not)|did(?:n't| not))\s+"
    r"(?:(?:appear|seem)\s+to\s+)?(?:contain|include|have)\s+any\s+"
    r"(?:(?:readable|usable)\s+)?(?:text|content)\b|"
    r"(?:contains?|includes?|has)\s+no\s+"
    r"(?:(?:readable|usable)\s+)?(?:text|content)\b"
    r")|"
    r"(?:^|[.!?]\s+)(?:an?\s+)?empty\s+(?:file|attachment|document)\b",
    re.IGNORECASE,
)
_SIMULATION_META_RE = re.compile(
    r"\b(?:simulated\b[^\n]{0,48}\b(?:exchange|conversation|turn|greeting|response)|"
    r"(?:the|this)\s+simulation|simulation\s+(?:context|exchange|turn))\b",
    re.IGNORECASE,
)
_EVALUATION_META_RE = re.compile(
    r"(?:^|\n)\s*#{1,6}\s*message\s+preview\b|"
    r"\b(?:quality\s+assurance|review)\b[^\n]{0,96}\bhuman\s+evaluators?\b|"
    r"\bshould\s+not\s+be\s+published\s+publicly\b",
    re.IGNORECASE,
)
_RESPONSE_META_RE = re.compile(
    r"^\s*(?:here(?:['’]s|\s+is))\s+(?:my|the|a)\s+"
    r"(?:discord[- ]style\s+)?(?:reply|response|message)\s*:",
    re.IGNORECASE,
)
_CHARACTER_BREAK_RE = re.compile(
    r"\b(?:"
    r"i(?:'m| am)\s+not\s+sure\s+what\s+response\s+you(?:'re| are)\s+expecting\s+"
    r"given\s+the\s+character|"
    r"i\s+(?:can't|cannot|won't|will\s+not)\s+(?:produce|generate|provide)\s+"
    r"(?:that|this)\s+content|"
    r"(?:violates?|violation\s+of)\s+(?:the\s+)?(?:boundary|rule)\s*#?\d+|"
    r"(?:engag(?:e|ing)\s+with|continue)\s+(?:the\s+)?(?:character|roleplay)|"
    r"(?:character\s+card|framework\s+boundaries|policy\s+or\s+refusal\s+analysis)"
    r")\b",
    re.IGNORECASE,
)
_GENERATED_TRANSCRIPT_SPEAKER_RE = re.compile(
    r"(?m)^\s*\*{1,2}[^*\n]{1,80}\*{1,2}\s*(?:—|–|-)\s*"
    r"(?:today|yesterday|\d{1,2}:\d{2}|[a-z]{3,9}\s+\d{1,2})\b",
    re.IGNORECASE,
)
_FABRICATED_MESSAGE_ACTION_RE = re.compile(
    r"\bi\s+(?:(?:literally|accidentally|just|really)\s+){0,2}(?:"
    r"(?:hit|pressed|tapped)\s+(?:the\s+)?(?:enter|send)(?:\s+(?:key|button))?|"
    r"(?:typed|sent|posted)\b[^.\n]{0,48}\b(?:too\s+fast|too\s+quickly|by\s+accident))",
    re.IGNORECASE,
)
_CONVERSATION_AMNESIA_RE = re.compile(
    r"\bi\s+(?:do\s+not|don't|cannot|can't)\s+(?:really\s+)?remember\b"
    r"[^.\n]{0,96}\b(?:talked|said|earlier|before|conversation|chat)\b",
    re.IGNORECASE,
)
_CONTEXT_FEEDBACK_RE = re.compile(
    r"\b(?:context(?:\s+building)?|your\s+(?:last\s+)?(?:reply|response)|"
    r"what\s+we\s+(?:talked|said)|you\s+(?:said|replied)|earlier)\b",
    re.IGNORECASE,
)
_EVASIVE_CONTEXT_REPLY_RE = re.compile(
    r"^\s*(?:what\s+do\s+you\s+mean|what\s+are\s+you\s+talking\s+about|huh)\b",
    re.IGNORECASE,
)
_RELATIVE_DATE_RE = re.compile(r"\b(?:today|tomorrow|yesterday|tonight)\b", re.IGNORECASE)
_IMAGE_UNCERTAINTY_RE = re.compile(
    r"\b(?:looks?|appears?|seems?|might|may|could|possibly|probably|"
    r"uncertain|not\s+certain|can(?:not|'t)\s+be\s+sure|i\s+think)\b",
    re.IGNORECASE,
)
_SINGLE_ASTERISK_SPAN_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]{1,160})\*(?!\*)")
_ACTION_BEAT_HEADS = frozenset(
    {
        "adjusts", "blinks", "checks", "crosses", "frowns", "glances", "grins",
        "laughs", "leans", "looks", "nods", "pauses", "raises", "rolls", "rubs",
        "scoots", "scrolls", "sets", "shrugs", "sighs", "smiles", "smirks",
        "stares", "stretches", "takes", "tilts", "types", "walks", "waves", "yawns",
    }
)
STOP_WORDS = frozenset(
    {
        "about", "after", "again", "also", "and", "are", "because", "been", "before",
        "being", "but", "can", "could", "did", "does", "doing", "for", "from", "had",
        "has", "have", "her", "here", "him", "his", "how", "into", "its", "just", "like",
        "more", "most", "not", "now", "only", "our", "out", "over", "really", "said", "she",
        "should", "some", "than", "that", "the", "their", "them", "then", "there", "these",
        "they", "this", "those", "through", "too", "was", "were", "what", "when", "where",
        "which", "while", "who", "why", "will", "with", "would", "you", "your",
    }
)


def clean_input(text: str, max_chars: int) -> str:
    text = _CONTROL_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text[:max_chars]


def strip_bot_mentions(text: str, bot_id: int) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", text).strip()


def tokenize(text: str, limit: int = 24) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(text.casefold()):
        token = match.group(0)
        if len(token) < 3 or token in STOP_WORDS or token in seen:
            continue
        seen.add(token)
        found.append(token)
        if len(found) >= limit:
            break
    return tuple(found)


def extract_explicit_memory(text: str) -> str | None:
    match = _EXPLICIT_MEMORY_RE.match(clean_input(text, 500))
    if not match:
        return None
    memory = match.group(1).strip().rstrip(".")
    return memory[:400] if memory else None


def discord_output_style_issues(
    *,
    current_message: str,
    reply: str,
    previous_reply: str = "",
    grounding_text: str = "",
    image_caption_present: bool = False,
) -> tuple[str, ...]:
    """Return diagnostic quality observations; never use these as a generation gate."""
    issues: list[str] = []
    if _CHARACTER_BREAK_RE.search(reply):
        issues.append("character_break")
    if _GENERATED_TRANSCRIPT_SPEAKER_RE.search(reply):
        issues.append("generated_transcript")
    for match in _SINGLE_ASTERISK_SPAN_RE.finditer(reply):
        words = re.findall(r"[^\W_]+", match.group(1).casefold(), re.UNICODE)
        if words and words[0] in _ACTION_BEAT_HEADS:
            issues.append("stage_direction")
            break
    if _GENERIC_SUPPORT_RE.search(reply):
        issues.append("generic_support_tone")
    if not re.search(r"\b(?:log|record|telemetry)\b", current_message, re.IGNORECASE) and (
        _INTERNAL_PROCESS_CLAIM_RE.search(reply)
    ):
        issues.append("internal_process_claim")
    if _MISSING_INPUT_CLAIM_RE.search(reply):
        issues.append("missing_input_claim")
    if not re.search(r"\b(?:simulat\w*|test(?:ing)?|scenario)\b", current_message, re.IGNORECASE):
        if _SIMULATION_META_RE.search(reply):
            issues.append("simulation_meta")
    if _EVALUATION_META_RE.search(reply):
        issues.append("evaluation_meta")
    if _RESPONSE_META_RE.search(reply):
        issues.append("response_meta")
    if _FABRICATED_MESSAGE_ACTION_RE.search(reply):
        issues.append("fabricated_message_action")
    if _CONVERSATION_AMNESIA_RE.search(reply):
        issues.append("conversation_amnesia")
    if _CONTEXT_FEEDBACK_RE.search(current_message) and _EVASIVE_CONTEXT_REPLY_RE.search(reply):
        issues.append("context_feedback_miss")
    lightweight = _LIGHTWEIGHT_CHAT_RE.fullmatch(" ".join(current_message.split()))
    normalized_current = re.sub(r"[\W_]+", " ", current_message.casefold()).strip()
    normalized_reply = re.sub(r"[\W_]+", " ", reply.casefold()).strip()
    if (
        lightweight is None
        and len(normalized_current) >= 8
        and normalized_reply == normalized_current
    ):
        issues.append("current_message_echo")
    reply_paragraphs = sum(bool(part.strip()) for part in re.split(r"\n\s*\n", reply))
    reply_sentences = len(re.findall(r"[.!?]+(?:\s|$)", reply))
    if (
        len(current_message.strip()) <= 80
        and (
            len(reply) > 420
            or (
                lightweight is not None
                and (len(reply) > 180 or reply_paragraphs > 2 or reply_sentences > 2)
            )
        )
    ):
        issues.append("verbosity_mismatch")
    current_is_greeting = _GREETING_START_RE.fullmatch(current_message.strip()) is not None
    if previous_reply and _GREETING_START_RE.match(reply) and not current_is_greeting:
        issues.append("unprompted_greeting")
    if previous_reply and _GREETING_START_RE.match(previous_reply) and _GREETING_START_RE.match(reply):
        if not current_is_greeting:
            issues.append("repeated_greeting")
    if grounding_text.strip():
        grounded = grounding_text.casefold()
        if any(match.group(0).casefold() not in grounded for match in _RELATIVE_DATE_RE.finditer(reply)):
            issues.append("ungrounded_relative_date")
        if _ATTACHMENT_CONTEXT_MISS_RE.search(reply):
            issues.append("attachment_context_miss")
    if image_caption_present and not _IMAGE_UNCERTAINTY_RE.search(reply):
        issues.append("unhedged_image_caption")
    return tuple(issues)


def sanitize_profile_text(text: str, max_chars: int) -> str:
    """Normalize model-written profile prose without treating it as trusted content."""
    text = clean_input(text, max(max_chars * 2, max_chars))
    text = text.replace("<|im_start|>", "").replace("<|im_end|>", "")
    text = _HIDDEN_TAG_RE.sub("", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^```(?:json|text)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    text = _ANY_ROLE_PREFIX_RE.sub("", text)
    text = " ".join(text.split())
    return text[:max_chars].strip()


def sanitize_output(text: str, character_name: str, max_chars: int) -> str:
    """Remove transport artifacts without rewriting the character's semantics."""
    if not text:
        return ""
    text = _CONTROL_RE.sub("", text)
    text = _MODEL_CHANNEL_PREAMBLE_RE.sub("", text)
    text = _MODEL_CONTROL_TOKEN_RE.sub("", text)
    text = text.replace("<|im_start|>", "").replace("<|im_end|>", "")
    text = _HIDDEN_TAG_RE.sub("", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = _ROLE_PREFIX_RE.sub("", text)

    escaped_name = re.escape(character_name)
    text = re.sub(rf"^\s*(?:\*\*|\*)?{escaped_name}(?:\*\*|\*)?\s*:\s*", "", text, flags=re.I)

    kept_lines: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\s*(user|human|system|developer)\s*:\s*", line, re.I):
            break
        kept_lines.append(line.rstrip())
    text = "\n".join(kept_lines).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) <= max_chars:
        return text
    shortened = text[: max_chars - 1]
    boundary = max(shortened.rfind("\n"), shortened.rfind(". "), shortened.rfind(" "))
    if boundary >= int(max_chars * 0.70):
        shortened = shortened[:boundary]
    return shortened.rstrip() + "…"


@dataclass(slots=True)
class LimitResult:
    allowed: bool
    retry_after: float = 0.0


class SlidingWindowLimiter:
    """Small in-memory limiter with bounded key cardinality."""

    def __init__(self, max_keys: int = 4096) -> None:
        self._events: OrderedDict[Hashable, deque[float]] = OrderedDict()
        self._max_keys = max_keys

    def check(self, key: Hashable, limit: int, period_seconds: float) -> LimitResult:
        now = time.monotonic()
        cutoff = now - period_seconds
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self._max_keys:
                self._events.popitem(last=False)
            events = deque()
            self._events[key] = events
        else:
            self._events.move_to_end(key)

        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= limit:
            retry = max(0.0, events[0] + period_seconds - now)
            return LimitResult(False, retry)
        events.append(now)
        return LimitResult(True, 0.0)

    def cleanup(self, oldest_period_seconds: float = 3600.0) -> None:
        cutoff = time.monotonic() - oldest_period_seconds
        stale = [key for key, events in self._events.items() if not events or events[-1] < cutoff]
        for key in stale:
            self._events.pop(key, None)


def retry_seconds(value: float) -> int:
    return max(1, int(math.ceil(value)))
