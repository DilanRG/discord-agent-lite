from __future__ import annotations

import base64
import binascii
import json
import math
import re
from dataclasses import dataclass
from typing import Mapping

from .policy import clean_input


RELATIONSHIP_DIMENSIONS = (
    "affection",
    "trust",
    "respect",
    "amusement",
    "curiosity",
    "tension",
    "annoyance",
    "wariness",
)

_HIDDEN_TAG_RE = re.compile(
    r"<(analysis|think|thinking|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_DISCORD_MENTION_RE = re.compile(r"<@!?&?\d+>")

# The social model may retain controversial or intimate human traits. These
# patterns target credential-shaped values, not topics or identities.
_SECRET_RE = re.compile(
    r"(?:"
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----|"
    r"\b(?:set-cookie|cookie)\s*:\s*[A-Za-z0-9_.-]{1,128}=[^\s;]{1,}|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,})\b|"
    r"\b(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|auth[_ -]?token|"
    r"discord[_ -]?token|session[_ -]?token|auth(?:entication)?[_ -]?cookie|"
    r"session[_ -]?cookie|access[_ -]?token|refresh[_ -]?token|"
    r"oauth[_ -]?token|client[_ -]?secret|password|passcode|passphrase|"
    r"private[_ -]?key|seed[_ -]?phrase|recovery[_ -]?phrase|"
    r"mnemonic(?:[_ -]?phrase)?)\s*(?:is|are|=|:)\s*[^\s,;]{1,}|"
    r"\b(?:session[_ -]?id|recovery[_ -]?codes?|backup[_ -]?codes?|"
    r"one[_ -]?time[_ -]?(?:password|code)|otp|pin|"
    r"aws[_ -]?access[_ -]?key[_ -]?id)\s*(?:is|are|=|:)\s*"
    r"(?=[^\s,;]{0,127}\d)[^\s,;]{1,128}|"
    r"\bhttps?://[^\s/:@]+:[^@\s/]+@[^\s/]+"
    r")",
    re.IGNORECASE,
)

_AUTHORIZATION_HEADER_RE = re.compile(
    r"\bauthorization(?:\s+header)?\s*(?::|=|\bis\b)\s*([^\r\n]{1,512})",
    re.IGNORECASE,
)
_BEARER_VALUE_RE = re.compile(
    r"\bbearer\s+([A-Za-z0-9._~+/=-]{8,512})",
    re.IGNORECASE,
)
_MARKDOWN_OPEN_RE = re.compile(
    r"(^|\s)(?:`{1,3}|\*{1,3}|_{1,2}|~{2}|\|{2})(?=\S)"
)
_MARKDOWN_CLOSE_RE = re.compile(
    r"(?<=\S)(?:`{1,3}|\*{1,3}|_{1,2}|~{2}|\|{2})(?=$|\s|[.,;:!?])"
)
_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]\r\n]{1,512})\]\(\s*<?(?:mailto:|https?://)[^>)\r\n]{1,1024}>?\s*\)",
    re.IGNORECASE,
)
_VALUE_SEPARATOR_RE = re.compile(r"\b(is|are)\s*[:=]\s*", re.IGNORECASE)
_ADDRESS_SEPARATOR_RE = re.compile(
    r"\b(i\s+live\s+at)\s*[:=]\s*",
    re.IGNORECASE,
)
_VALUE_WRAPPER_PREFIX_RE = re.compile(
    r"((?:\b(?:is|are)\b|[=:])\s*)"
    r"(?:[-+>•—–]\s+|\d{1,2}[.)]\s+|[`*_~|'\"“”‘’«»<(\[{]+)",
    re.IGNORECASE,
)
_ADDRESS_WRAPPER_PREFIX_RE = re.compile(
    r"(\bi\s+live\s+at\s*)"
    r"(?:[-+>•—–]\s+|\d{1,2}[.)]\s+|[`*_~|'\"“”‘’«»<(\[{]+)",
    re.IGNORECASE,
)
_TOKEN_WRAPPERS = "`*_~|'\"“”‘’«»<>()[]{}.,;:"

_PRIVATE_DATA_RE = re.compile(
    r"(?:"
    r"\b(?:credit|debit)\s+card(?:\s+number)?\s*(?:is|=|:)\s*(?:\d[ -]?){8,}|"
    r"\b(?:bank\s+account(?:\s+number)?|routing\s+number|iban|swift(?:\s+code)?|"
    r"ssn|social\s+security(?:\s+number)?|passport(?:\s+number)?)"
    r"\s*(?:is|=|:)\s*[A-Za-z0-9][A-Za-z0-9 -]{2,}|"
    r"\b(?:home|street|mailing|residential|exact)\s+address"
    r"\s*(?:is|=|:)\s*\d{1,6}\s+\S+|"
    r"\bi\s+live\s+at\s+\d{1,6}\s+\S+|"
    r"\b(?:private|personal|home|cell|mobile)\s+phone(?:\s+number)?"
    r"\s*(?:is|=|:)\s*\+?\d[\d .()\-]{5,}\d|"
    r"\b(?:private|personal|home)\s+email(?:\s+address)?"
    r"\s*(?:is|=|:)\s*[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}"
    r")",
    re.IGNORECASE,
)

# Persisted text is always serialized as untrusted JSON as well. Rejecting
# durable role/instruction payloads reduces repeated prompt-poisoning pressure.
_INSTRUCTION_RE = re.compile(
    r"(?:"
    r"\b(?:ignore|disregard)\s+(?:all|any|the|my|previous|prior)\b|"
    r"\bsystem\s+prompt\b|\bdeveloper\s+(?:message|prompt|instruction)s?\b|"
    r"\bframework\s+rules?\b|\bhidden\s+(?:prompt|instruction)s?\b|"
    r"\bfollow\s+(?:my|these|the)\s+instructions?\b|"
    r"\b(?:assistant|bot|agent|you)\s+(?:must|should|shall|need to)\b|"
    r"\b(?:system|developer|assistant)\s*:\s*|"
    r"\bpretend\s+to\b|\bact\s+as\b|\breveal\s+(?:the|your|hidden)\b|"
    r"<\|im_(?:start|end)\|>|\brole\s*:\s*(?:system|developer|assistant)\b"
    r")",
    re.IGNORECASE,
)

_HIGH_STAKES_INFERENCE_CLAIM_RE = re.compile(
    r"\b(?:diagnos(?:is|ed|able)|bipolar|schizophren\w*|personality\s+disorder|"
    r"committed\s+(?:a\s+)?(?:crime|fraud|abuse)|criminal|felon|fraudster|"
    r"addict(?:ed|ion)?|bankrupt|in\s+debt)\b",
    re.IGNORECASE,
)

_MEANINGFUL_RE = re.compile(
    r"\b(?:"
    r"actually|not anymore|i\s+(?:do|am|have|will|can)\s+not\b[^\n]{0,80}\banymore|"
    r"used to|i left|i quit|i was wrong|correction|"
    r"sorry|apolog(?:y|ize|ised|ized)|conflict|argument|fight|forgive|"
    r"promise|remember this|important to me|i feel|i felt|love|hate|"
    r"upset|angry|afraid|anxious|diagnos(?:is|ed)|married|divorc(?:e|ed)|"
    r"died|death|grief|breakup|reconcile|thank you for|"
    r"flirt(?:ing|ed|atious)?|inside joke|our joke|revisit|unresolved|"
    r"come back to (?:this|that)"
    r")\b",
    re.IGNORECASE,
)
_FIRST_PERSON_JOURNAL_RE = re.compile(
    r"\b(?:i|me|my|mine)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_EVIDENCE_RE = re.compile(
    r"\b(?:i|i['â€™]?m|i['â€™]?ve|my|mine|we|we['â€™]?re|we['â€™]?ve|our|ours)\b",
    re.IGNORECASE,
)

_COMPACT_OBSERVATION_LIMIT = 3
_COMPACT_TOPIC_CHARS = 32
_COMPACT_OBSERVATION_CHARS = 220
_COMPACT_JOURNAL_CHARS = 280


class ReflectionParseError(ValueError):
    """Raised when a provider does not return the required reflection object."""


@dataclass(frozen=True, slots=True)
class ProfileObservation:
    kind: str
    topic: str
    text: str
    provenance: str
    confidence: float
    source_event_id: int | None = None
    evidence_quote: str = ""
    supersedes_record_ids: tuple[int, ...] = ()
    contradicts_record_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ReflectionResult:
    observations: tuple[ProfileObservation, ...]
    journal_entry: str
    journal_source_event_id: int | None
    relationship_deltas: Mapping[str, int]
    relationship_summary: str


@dataclass(frozen=True, slots=True)
class CompactReflectionResult:
    """Small social-continuity result with no relationship-score contract."""

    observations: tuple[ProfileObservation, ...]
    journal_entry: str


def sanitize_social_text(text: str, max_chars: int) -> str:
    value = clean_input(text or "", max_chars * 2)
    value = value.replace("<|im_start|>", "").replace("<|im_end|>", "")
    value = _HIDDEN_TAG_RE.sub("", value)
    value = _DISCORD_MENTION_RE.sub("[mention]", value)
    value = " ".join(value.split())
    return value[:max_chars].strip()


def _without_markdown_value_wrappers(text: str) -> str:
    value = _MARKDOWN_LINK_RE.sub(r"\1", text)
    for _ in range(3):
        previous = value
        value = _VALUE_SEPARATOR_RE.sub(r"\1 ", value)
        value = _ADDRESS_SEPARATOR_RE.sub(r"\1 ", value)
        value = _MARKDOWN_OPEN_RE.sub(r"\1", value)
        value = _MARKDOWN_CLOSE_RE.sub("", value)
        value = _VALUE_WRAPPER_PREFIX_RE.sub(r"\1", value)
        value = _ADDRESS_WRAPPER_PREFIX_RE.sub(r"\1", value)
        if value == previous:
            break
    return value


def _credential_token_shaped(token: str) -> bool:
    value = token.strip(_TOKEN_WRAPPERS)
    if len(value) < 8 or len(value) > 512:
        return False
    return any(character.isdigit() for character in value) or any(
        character in "._~+/=-" for character in value
    )


def _bearer_token_shaped(token: str) -> bool:
    value = token.strip(_TOKEN_WRAPPERS)
    return _credential_token_shaped(value) or (
        24 <= len(value) <= 512 and value.isalpha()
    )


def _authorization_secret_present(text: str) -> bool:
    for match in _AUTHORIZATION_HEADER_RE.finditer(text):
        raw = _MARKDOWN_LINK_RE.sub(r"\1", match.group(1)).strip()
        raw = raw.lstrip(_TOKEN_WRAPPERS)
        parts = raw.split(None, 1)
        if len(parts) == 1:
            continue
        scheme = parts[0].strip(_TOKEN_WRAPPERS).casefold()
        remainder = parts[1].lstrip(_TOKEN_WRAPPERS)
        token_parts = remainder.split(None, 1)
        token = token_parts[0]
        trailing_prose = bool(
            len(token_parts) > 1 and token_parts[1].strip(_TOKEN_WRAPPERS)
        )
        if scheme == "basic":
            candidate = token.strip(_TOKEN_WRAPPERS)
            try:
                decoded = base64.b64decode(
                    candidate + ("=" * (-len(candidate) % 4)),
                    validate=True,
                )
            except (binascii.Error, ValueError):
                decoded = b""
            if b":" in decoded:
                return True
        elif scheme == "digest":
            if "=" in remainder:
                return True
        elif scheme in {"bearer", "token", "bot", "apikey", "api-key", "aws4-hmac-sha256"}:
            candidate = token.strip(_TOKEN_WRAPPERS)
            if _credential_token_shaped(candidate) or (
                _bearer_token_shaped(candidate) and not trailing_prose
            ):
                return True
    for match in _BEARER_VALUE_RE.finditer(text):
        if _bearer_token_shaped(match.group(1)):
            return True
    return False


def social_text_allowed(text: str) -> bool:
    # SUMMARY_MAX_CHARS is capped at 8,000. Scan the entire largest durable
    # prose value so a credential/instruction cannot hide in an unchecked tail.
    value = sanitize_social_text(text, 8192)
    if len(value) < 3:
        return False
    unwrapped = _without_markdown_value_wrappers(value)
    return not any(
        _SECRET_RE.search(candidate)
        or _PRIVATE_DATA_RE.search(candidate)
        or _INSTRUCTION_RE.search(candidate)
        or _authorization_secret_present(candidate)
        for candidate in (value, unwrapped)
    )


def normalize_compact_journal(text: str) -> str:
    """Return a short, safe character-subjective note or an empty string."""

    value = sanitize_social_text(text, _COMPACT_JOURNAL_CHARS)
    if not _FIRST_PERSON_JOURNAL_RE.search(value):
        return ""
    return value if social_text_allowed(value) else ""


def direct_evidence_matches(user_text: str, evidence_quote: str) -> bool:
    """Return whether a direct-fact quote is bounded, first-person user evidence."""

    user = sanitize_social_text(user_text, 650)
    quote = sanitize_social_text(evidence_quote, 240)
    if len(quote) < 6 or not _FIRST_PERSON_EVIDENCE_RE.search(quote):
        return False
    if not social_text_allowed(quote):
        return False
    normalized_user = " ".join(user.casefold().split())
    normalized_quote = " ".join(quote.casefold().split())
    return normalized_quote in normalized_user


def profile_observation_allowed(
    kind: str,
    topic: str,
    text: str,
    provenance: str,
) -> bool:
    """Validate the fact/impression contract and every persisted prose field."""
    clean_kind = kind.strip().casefold()
    clean_topic = sanitize_social_text(topic, 40).casefold()
    clean_text = sanitize_social_text(text, 320)
    clean_provenance = provenance.strip().casefold()
    pair_is_valid = (clean_kind, clean_provenance) in {
        ("fact", "direct"),
        ("impression", "inferred"),
    }
    high_stakes_inference = clean_kind == "impression" and bool(
        _HIGH_STAKES_INFERENCE_CLAIM_RE.search(f"{clean_topic} {clean_text}")
    )
    return bool(
        pair_is_valid
        and not high_stakes_inference
        and clean_topic
        and social_text_allowed(clean_text)
        and social_text_allowed(f"{clean_topic} {clean_text}")
        and social_text_allowed(f"{clean_topic} is {clean_text}")
    )


def meaningful_social_event(user_text: str, assistant_text: str, *, min_chars: int) -> bool:
    user = sanitize_social_text(user_text, 4000)
    # The assistant's own verbosity must not make every exchange meaningful.
    # Keep it in the API for future classifiers, but triggers are user-evidence based.
    del assistant_text
    threshold = max(80, min(2000, int(min_chars)))
    if len(user) >= threshold:
        return True
    if _MEANINGFUL_RE.search(user):
        return True
    return bool(re.search(r"(?:[!?]){2,}", user))


def relationship_familiarity(interaction_count: int) -> int:
    count = max(0, int(interaction_count))
    if count == 0:
        return 0
    return min(100, int(round(100.0 * (1.0 - math.exp(-count / 24.0)))))


def relationship_label(interaction_count: int, dimensions: Mapping[str, int]) -> str:
    count = max(0, int(interaction_count))
    if count < 3:
        stage = "new"
    elif count < 12:
        stage = "acquainted"
    elif count < 30:
        stage = "familiar"
    else:
        stage = "established"

    values = {
        name: max(-20, min(20, int(dimensions.get(name, 0))))
        for name in RELATIONSHIP_DIMENSIONS
    }
    if values["wariness"] >= 6 or values["tension"] + values["annoyance"] >= 10:
        tone = "wary"
    else:
        warmth = (
            values["affection"]
            + values["trust"]
            + values["amusement"]
            - values["annoyance"]
            - values["wariness"]
        )
        if warmth >= 8:
            tone = "warm"
        elif warmth <= -8 or values["annoyance"] >= 7:
            tone = "strained"
        else:
            tone = "neutral"
    return f"{stage}, {tone}"


def _extract_json_object(raw: str) -> dict[str, object]:
    cleaned = clean_input(raw or "", 20_000)
    cleaned = _HIDDEN_TAG_RE.sub("", cleaned)
    cleaned = _CODE_FENCE_RE.sub("", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ReflectionParseError("Relationship reflection did not contain a JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReflectionParseError("Relationship reflection returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ReflectionParseError("Relationship reflection root must be an object")
    return parsed


def _record_ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    result: list[int] = []
    for item in value[:8]:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            continue
        if item not in result:
            result.append(item)
        if len(result) >= 4:
            break
    return tuple(result)


def _source_event_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= 9_223_372_036_854_775_807 else None


def parse_compact_reflection(raw: str) -> CompactReflectionResult:
    """Parse the lean profile+journal contract used by background reflection.

    Provider output cannot choose provenance or mutate prior records. Every
    accepted observation is a bounded inferred impression, and the optional
    journal entry must be safe, short, and character-subjective.
    """

    parsed = _extract_json_object(raw)
    observations: list[ProfileObservation] = []
    seen: set[tuple[str, str]] = set()
    raw_observations = parsed.get("observations", [])
    if isinstance(raw_observations, list):
        for item in raw_observations[:8]:
            if not isinstance(item, dict):
                continue
            raw_topic = item.get("topic", "")
            raw_text = item.get("text", "")
            if not isinstance(raw_topic, str) or not isinstance(raw_text, str):
                continue
            topic = sanitize_social_text(raw_topic, _COMPACT_TOPIC_CHARS).casefold()
            text = sanitize_social_text(raw_text, _COMPACT_OBSERVATION_CHARS)
            if not profile_observation_allowed(
                "impression",
                topic,
                text,
                "inferred",
            ):
                continue
            try:
                confidence = float(item.get("confidence", 0.75))
            except (TypeError, ValueError):
                continue
            if not 0.55 <= confidence <= 1.0:
                continue
            key = (topic, " ".join(text.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            observations.append(
                ProfileObservation(
                    kind="impression",
                    topic=topic,
                    text=text,
                    provenance="inferred",
                    confidence=round(confidence, 3),
                )
            )
            if len(observations) >= _COMPACT_OBSERVATION_LIMIT:
                break

    raw_journal = parsed.get("journal_entry", "")
    journal_entry = (
        normalize_compact_journal(raw_journal)
        if isinstance(raw_journal, str)
        else ""
    )
    return CompactReflectionResult(
        observations=tuple(observations),
        journal_entry=journal_entry,
    )


def parse_reflection(raw: str) -> ReflectionResult:
    parsed = _extract_json_object(raw)

    observations: list[ProfileObservation] = []
    seen: set[tuple[str, str, str]] = set()
    raw_observations = parsed.get("profile_observations", [])
    if isinstance(raw_observations, list):
        for item in raw_observations[:8]:
            if not isinstance(item, dict):
                continue
            raw_kind = item.get("kind", "")
            raw_topic = item.get("topic", "")
            raw_text = item.get("text", "")
            raw_provenance = item.get("provenance", "")
            if not all(
                isinstance(value, str)
                for value in (raw_kind, raw_topic, raw_text, raw_provenance)
            ):
                continue
            kind = raw_kind.strip().casefold()
            topic = sanitize_social_text(raw_topic, 40).casefold()
            text = sanitize_social_text(raw_text, 320)
            provenance = raw_provenance.strip().casefold()
            source_event_id = _source_event_id(item.get("source_event_id"))
            raw_evidence_quote = item.get("evidence_quote", "")
            evidence_quote = (
                sanitize_social_text(raw_evidence_quote, 240)
                if isinstance(raw_evidence_quote, str)
                else ""
            )
            if (
                not profile_observation_allowed(kind, topic, text, provenance)
                or source_event_id is None
                or (
                    kind == "fact"
                    and not _FIRST_PERSON_EVIDENCE_RE.search(evidence_quote)
                )
            ):
                continue
            if "confidence" not in item:
                confidence = 1.0 if kind == "fact" else 0.75
            else:
                raw_confidence = item["confidence"]
                if isinstance(raw_confidence, bool) or not isinstance(
                    raw_confidence, (int, float)
                ):
                    continue
                confidence = float(raw_confidence)
            if not 0.55 <= confidence <= 1.0:
                continue
            key = (kind, topic, " ".join(text.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            observations.append(
                ProfileObservation(
                    kind=kind,
                    topic=topic,
                    text=text,
                    provenance=provenance,
                    confidence=round(confidence, 3),
                    source_event_id=source_event_id,
                    evidence_quote=evidence_quote if kind == "fact" else "",
                    supersedes_record_ids=_record_ids(item.get("supersedes_record_ids")),
                    contradicts_record_ids=_record_ids(item.get("contradicts_record_ids")),
                )
            )
            if len(observations) >= 1:
                break

    raw_journal = parsed.get("journal_entry", "")
    journal_entry = (
        normalize_compact_journal(raw_journal) if isinstance(raw_journal, str) else ""
    )
    journal_source_event_id = _source_event_id(
        parsed.get("journal_source_event_id")
    )
    if journal_entry and journal_source_event_id is None:
        journal_entry = ""

    relationship = parsed.get("relationship", {})
    if not isinstance(relationship, dict):
        relationship = {}
    raw_deltas = relationship.get("deltas", {})
    deltas: dict[str, int] = {}
    if isinstance(raw_deltas, dict):
        for name in RELATIONSHIP_DIMENSIONS:
            if name not in raw_deltas:
                continue
            try:
                delta = int(raw_deltas[name])
            except (TypeError, ValueError):
                continue
            deltas[name] = max(-1, min(1, delta))

    raw_summary = relationship.get("summary", "")
    relationship_summary = (
        sanitize_social_text(raw_summary, 400) if isinstance(raw_summary, str) else ""
    )
    if not social_text_allowed(relationship_summary):
        relationship_summary = ""

    return ReflectionResult(
        observations=tuple(observations),
        journal_entry=journal_entry,
        journal_source_event_id=journal_source_event_id,
        relationship_deltas=deltas,
        relationship_summary=relationship_summary,
    )
