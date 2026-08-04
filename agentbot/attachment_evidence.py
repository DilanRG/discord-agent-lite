"""Small, durable attachment evidence records safe to retain with conversation rows."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from typing import Iterable


_MAX_PARTS = 2
_MAX_ATTACHMENT_ID = 160
_MAX_FILENAME = 160
_MAX_TEXT = 6_000
_MAX_TOTAL_TEXT = 6_000
_MAX_ERROR_CODE = 64
_KINDS = frozenset({"image", "text", "pdf", "docx"})
_STATUSES = frozenset({"ready", "error"})
_ORIGINS = frozenset({"image_caption", "text_extract", "pdf_extract", "docx_extract"})
_ATTACHMENT_REFERENCE_RE = re.compile(
    r"\b(?:this|that|these|those)\s+"
    r"(?:attachment|attachments|file|files|image|images|photo|photos|document|documents)\b|"
    r"\b(?:the\s+)?(?:attached|uploaded)\s+"
    r"(?:attachment|attachments|file|files|image|images|photo|photos|document|documents)\b|"
    r"\b(?:contents?\s+of|what(?:'s|\s+is)\s+in)\s+"
    r"(?:this|that|the)\s+(?:attachment|file|image|photo|document)\b",
    re.IGNORECASE,
)


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def _clean_filename(value: object) -> str:
    # Keep a display name, never a path.  Control characters and separators are
    # discarded so this cannot become an accidental retrieval handle.
    name = _clean_text(value, _MAX_FILENAME)
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip(". _")
    return name or "attachment"


@dataclass(frozen=True, slots=True)
class AttachmentEvidence:
    """Bounded parser output; deliberately excludes raw payloads and locators."""

    attachment_id: str
    ordinal: int
    filename: str
    detected_kind: str
    status: str
    origin: str
    text: str
    confidence: float | None = None
    truncated: bool = False
    error_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachment_id", _clean_text(self.attachment_id, _MAX_ATTACHMENT_ID))
        object.__setattr__(self, "ordinal", max(0, min(int(self.ordinal), 1_000_000)))
        object.__setattr__(self, "filename", _clean_filename(self.filename))
        object.__setattr__(self, "text", _clean_text(self.text, _MAX_TEXT))
        object.__setattr__(self, "error_code", _clean_text(self.error_code, _MAX_ERROR_CODE))
        if self.detected_kind not in _KINDS:
            raise ValueError("unsupported attachment evidence kind")
        if self.status not in _STATUSES:
            raise ValueError("unsupported attachment evidence status")
        if self.origin not in _ORIGINS:
            raise ValueError("unsupported attachment evidence origin")
        if not self.attachment_id:
            raise ValueError("attachment evidence requires an attachment id")
        object.__setattr__(self, "truncated", bool(self.truncated))
        if self.status == "error":
            object.__setattr__(self, "text", "")
            object.__setattr__(self, "confidence", None)
        else:
            object.__setattr__(self, "error_code", "")
        if self.confidence is not None:
            confidence = float(self.confidence)
            if not math.isfinite(confidence):
                raise ValueError("attachment evidence confidence must be finite")
            object.__setattr__(self, "confidence", max(0.0, min(1.0, confidence)))

    def as_json_dict(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "ordinal": self.ordinal,
            "filename": self.filename,
            "detected_kind": self.detected_kind,
            "status": self.status,
            "origin": self.origin,
            "text": self.text,
            "confidence": self.confidence,
            "truncated": self.truncated,
            "error_code": self.error_code,
        }


def bound_attachment_parts(
    parts: Iterable[AttachmentEvidence] = (),
) -> tuple[AttachmentEvidence, ...]:
    """Apply the shared two-part/6,000-character durable evidence budget."""
    candidates = tuple(parts)[:_MAX_PARTS]
    if not all(isinstance(part, AttachmentEvidence) for part in candidates):
        raise TypeError("attachment parts must be AttachmentEvidence values")
    remaining = _MAX_TOTAL_TEXT
    bounded: list[AttachmentEvidence] = []
    for part in candidates:
        if part.status != "ready":
            bounded.append(part)
            continue
        text = part.text[:remaining]
        bounded.append(
            replace(
                part,
                text=text,
                truncated=part.truncated or len(part.text) > len(text),
            )
        )
        remaining -= len(text)
    return tuple(bounded)


def encode_attachment_parts(parts: Iterable[AttachmentEvidence] = ()) -> str:
    """Serialize only valid, bounded parts; callers get an atomic DB-safe value."""
    bounded = bound_attachment_parts(parts)
    return json.dumps([part.as_json_dict() for part in bounded], separators=(",", ":"), ensure_ascii=False)


def decode_attachment_parts(value: object) -> tuple[AttachmentEvidence, ...]:
    """Fail closed for damaged or unexpected historic JSON."""
    try:
        decoded = json.loads(str(value or "[]"))
        if not isinstance(decoded, list):
            return ()
        parts = tuple(
            AttachmentEvidence(
                attachment_id=item["attachment_id"], ordinal=item["ordinal"],
                filename=item["filename"], detected_kind=item["detected_kind"],
                status=item["status"], origin=item["origin"], text=item.get("text", ""),
                confidence=item.get("confidence"), truncated=bool(item.get("truncated", False)),
                error_code=item.get("error_code", ""),
            )
            for item in decoded[:_MAX_PARTS]
            if isinstance(item, dict)
        )
        return bound_attachment_parts(parts)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ()


def explicit_memory_references_attachment(value: str) -> bool:
    """Recognize an outer authored memory request; evidence text is never inspected."""
    return bool(_ATTACHMENT_REFERENCE_RE.search(str(value or "")))


def saved_attachment_memory_text(parts: Iterable[AttachmentEvidence]) -> str:
    ready = tuple(part for part in bound_attachment_parts(parts) if part.status == "ready" and part.text)
    if not ready:
        return ""
    details = " | ".join(
        f"{part.filename} ({part.detected_kind} evidence): {part.text}"
        for part in ready
    )
    return f"Attachment evidence the user asked to remember: {details}"[:500]


def attachment_prompt_items(
    parts: Iterable[AttachmentEvidence],
    *,
    max_text_chars: int,
) -> tuple[dict[str, object], ...]:
    """Render one labelled, non-authoritative evidence shape for every prompt lane."""
    remaining = max(0, int(max_text_chars))
    rendered: list[dict[str, object]] = []
    for part in bound_attachment_parts(parts):
        item: dict[str, object] = {
            "filename": part.filename,
            "kind": part.detected_kind,
            "status": part.status,
            "origin": part.origin,
        }
        if part.status == "ready" and part.text and remaining:
            text = part.text[:remaining]
            item["text"] = text
            item["truncated"] = part.truncated or len(text) < len(part.text)
            if part.confidence is not None:
                item["confidence"] = part.confidence
            remaining -= len(text)
        elif part.status == "error":
            item["error_code"] = part.error_code or "unavailable"
            item["note"] = "unavailable; do not guess or invent contents"
        rendered.append(item)
    return tuple(rendered)
