#!/usr/bin/env python3
"""Run deterministic boundary checks for the lean attachment extractor."""

from __future__ import annotations

import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentbot.attachments import (  # noqa: E402
    AttachmentError,
    AttachmentLimits,
    ExtractionResult,
    extract_document,
)


@dataclass(frozen=True, slots=True)
class ExtractorCase:
    name: str
    filename: str
    mime: str
    payload: bytes
    expected_kind: str = ""
    expected_error: str = ""
    exact_text: str | None = None
    truncated: bool | None = None
    dimensions: tuple[int, int] | None = None
    limit_overrides: tuple[tuple[str, int | float], ...] = ()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _limits(
    overrides: tuple[tuple[str, int | float], ...] = (),
) -> AttachmentLimits:
    values: dict[str, int | float] = {
        "max_bytes": 4096,
        "max_extracted_chars": 128,
        "max_pages": 1,
        "max_archive_entries": 1,
        "max_archive_uncompressed_bytes": 4096,
        "max_pixels": 1_000_000,
        "timeout_seconds": 2.0,
    }
    values.update(dict(overrides))
    return AttachmentLimits(
        max_bytes=int(values["max_bytes"]),
        max_extracted_chars=int(values["max_extracted_chars"]),
        max_pages=int(values["max_pages"]),
        max_archive_entries=int(values["max_archive_entries"]),
        max_archive_uncompressed_bytes=int(
            values["max_archive_uncompressed_bytes"]
        ),
        max_pixels=int(values["max_pixels"]),
        timeout_seconds=float(values["timeout_seconds"]),
    )


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _jpeg(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )


def _webp(width: int, height: int) -> bytes:
    return (
        b"RIFF"
        + (22).to_bytes(4, "little")
        + b"WEBPVP8X"
        + (10).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def _cases() -> tuple[ExtractorCase, ...]:
    cases = (
        ExtractorCase(
            "utf8-source-text",
            "example.py",
            "text/x-python",
            "name = 'café U0001f642'\n\tprint(name)".encode(),
            expected_kind="text",
            exact_text="name = 'café U0001f642'\n\tprint(name)",
            truncated=False,
        ),
        ExtractorCase(
            "bounded-markdown",
            "notes.md",
            "text/markdown",
            b"abcdefghijklmnop",
            expected_kind="text",
            exact_text="abcdefgh",
            truncated=True,
            limit_overrides=(("max_extracted_chars", 8),),
        ),
        ExtractorCase(
            "png-dimensions",
            "image.png",
            "image/png",
            _png(2, 3),
            expected_kind="image",
            dimensions=(2, 3),
        ),
        ExtractorCase(
            "jpeg-dimensions",
            "image.jpg",
            "image/jpeg",
            _jpeg(4, 5),
            expected_kind="image",
            dimensions=(4, 5),
        ),
        ExtractorCase(
            "webp-dimensions",
            "image.webp",
            "image/webp",
            _webp(6, 7),
            expected_kind="image",
            dimensions=(6, 7),
        ),
        ExtractorCase("reject-pdf", "notes.pdf", "application/pdf", b"%PDF-1.7", expected_error="unsupported"),
        ExtractorCase("reject-docx", "notes.docx", "application/octet-stream", b"PK\x03\x04docx", expected_error="unsupported"),
        ExtractorCase("reject-html", "page.html", "text/html", b"<p>hello</p>", expected_error="unsupported"),
        ExtractorCase("reject-xml", "data.xml", "application/xml", b"<root>hello</root>", expected_error="unsupported"),
        ExtractorCase("reject-yaml", "data.yaml", "text/yaml", b"name: example", expected_error="unsupported"),
        ExtractorCase("reject-csv", "data.csv", "text/csv", b"name,value", expected_error="unsupported"),
        ExtractorCase("reject-gif", "image.gif", "image/gif", b"GIF89a\x01\x00\x01\x00", expected_error="unsupported"),
        ExtractorCase("reject-binary", "binary.txt", "text/plain", b"alpha\x00beta", expected_error="binary"),
        ExtractorCase("reject-non-utf8", "encoded.txt", "text/plain", b"text \xff", expected_error="encoding"),
        ExtractorCase(
            "metadata-mismatch-png",
            "wrong.jpg",
            "image/jpeg",
            _png(1, 1),
            expected_kind="image",
            dimensions=(1, 1),
        ),
        ExtractorCase(
            "reject-pixel-limit",
            "large.png",
            "image/png",
            _png(2, 2),
            expected_error="pixel_limit",
            limit_overrides=(("max_pixels", 3),),
        ),
    )
    _require(len(cases) == 16, f"expected 16 extractor cases, found {len(cases)}")
    _require(len({case.name for case in cases}) == len(cases), "case names repeat")
    return cases


def _check_result(
    case: ExtractorCase,
    result: ExtractionResult,
    limits: AttachmentLimits,
) -> None:
    _require(result.kind == case.expected_kind, f"{case.name}: kind {result.kind!r}")
    _require(
        len(result.text) <= limits.max_extracted_chars,
        f"{case.name}: text exceeded its bound",
    )
    if case.exact_text is not None:
        _require(
            result.text == case.exact_text,
            f"{case.name}: unexpected text {result.text!r}",
        )
    if case.truncated is not None:
        _require(
            result.truncated is case.truncated,
            f"{case.name}: truncated={result.truncated}",
        )
    if case.dimensions is not None:
        _require(
            (result.width, result.height) == case.dimensions,
            f"{case.name}: dimensions={(result.width, result.height)}",
        )


def main() -> None:
    _require(len(sys.argv) == 1, "this checker takes no arguments")
    cases = _cases()
    passed = 0
    with tempfile.TemporaryDirectory(prefix="agentbot-extractor-check-") as directory:
        root = Path(directory)
        for index, case in enumerate(cases):
            path = root / f"{index:02d}-{case.filename}"
            path.write_bytes(case.payload)
            limits = _limits(case.limit_overrides)
            try:
                result = extract_document(
                    path,
                    filename=case.filename,
                    declared_mime=case.mime,
                    limits=limits,
                )
            except AttachmentError as exc:
                _require(bool(case.expected_error), f"{case.name}: unexpected {exc.code}")
                _require(
                    exc.code == case.expected_error,
                    f"{case.name}: expected {case.expected_error}, got {exc.code}",
                )
            else:
                _require(
                    not case.expected_error,
                    f"{case.name}: expected {case.expected_error}",
                )
                _check_result(case, result, limits)
            passed += 1

    print(f"Lean attachment extractor: {passed} deterministic cases passed")


if __name__ == "__main__":
    main()
