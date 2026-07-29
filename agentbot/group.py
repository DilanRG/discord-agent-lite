from __future__ import annotations

import re
from dataclasses import dataclass

from .social import _extract_json_object, sanitize_social_text, social_text_allowed


GROUP_OBSERVATION_KINDS = ("culture", "norm", "joke", "dynamic", "event")

_GROUP_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"everyone|everybody|people here|around here|members here|"
    r"our\s+(?:server|guild|channel|community|group|team)|"
    r"this\s+(?:server|guild|channel|community)|"
    r"running joke|inside joke|shared tradition|server tradition|"
    r"community tradition|all agreed|rivalry between|"
    r"server event|community event|guild event|server meetup|"
    r"community meetup|server tournament|guild tournament|server anniversary"
    r")\b",
    re.IGNORECASE,
)
_MULTI_MENTION_RE = re.compile(r"<@!?\d+>")


@dataclass(frozen=True, slots=True)
class GroupObservation:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class GroupReflectionResult:
    summary: str
    observations: tuple[GroupObservation, ...]


def meaningful_group_event(user_text: str) -> bool:
    """Return true only for explicit evidence about a wider guild or group."""
    raw = user_text or ""
    text = sanitize_social_text(raw, 4000)
    if len(_MULTI_MENTION_RE.findall(raw)) >= 2:
        return True
    return bool(_GROUP_SIGNAL_RE.search(text))


def parse_group_reflection(raw: str) -> GroupReflectionResult:
    parsed = _extract_json_object(raw)
    raw_summary = parsed.get("summary", "")
    summary = sanitize_social_text(raw_summary, 600) if isinstance(raw_summary, str) else ""
    if not social_text_allowed(summary):
        summary = ""

    observations: list[GroupObservation] = []
    seen: set[tuple[str, str]] = set()
    raw_observations = parsed.get("observations", [])
    if isinstance(raw_observations, list):
        for item in raw_observations[:12]:
            if not isinstance(item, dict):
                continue
            raw_kind = item.get("kind", "")
            raw_text = item.get("text", "")
            if not isinstance(raw_kind, str) or not isinstance(raw_text, str):
                continue
            kind = raw_kind.strip().casefold()
            text = sanitize_social_text(raw_text, 360)
            key = (kind, " ".join(text.casefold().split()))
            if (
                kind not in GROUP_OBSERVATION_KINDS
                or not social_text_allowed(text)
                or key in seen
            ):
                continue
            seen.add(key)
            observations.append(GroupObservation(kind=kind, text=text))
            if len(observations) >= 5:
                break
    return GroupReflectionResult(summary=summary, observations=tuple(observations))
