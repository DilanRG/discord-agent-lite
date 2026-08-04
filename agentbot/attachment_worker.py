"""Narrow disposable parser for text-bearing PDF and DOCX uploads.

It accepts one JSON request on stdin and emits one bounded JSON result.  This
file is executed by path under ``python -I``; do not import application state.
"""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


_MAX_DOCX_XML_BYTES = 4_194_304
_NESTED_ARCHIVE_SUFFIXES = frozenset(
    {
        ".7z", ".docm", ".docx", ".gz", ".jar", ".odt", ".ods", ".odp",
        ".pptm", ".pptx", ".rar", ".tar", ".tgz", ".xlsm", ".xlsx", ".zip",
    }
)


def _clean(value: str, limit: int) -> tuple[str, bool]:
    value = "".join(c for c in value.replace("\r\n", "\n") if c in "\n\t" or ord(c) >= 32).strip()
    return value[:limit], len(value) > limit


def _apply_resource_limits() -> None:
    if os.name != "posix":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_AS, (100_663_296, 100_663_296))


def _pdf(
    path: Path,
    max_pages: int,
    max_chars: int,
    max_stream_bytes: int,
) -> tuple[str, int, bool]:
    from pypdf import PdfReader
    from pypdf import filters as pdf_filters

    stream_limit = max(65_536, min(int(max_stream_bytes), _MAX_DOCX_XML_BYTES))
    for name in (
        "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
        "JBIG2_MAX_OUTPUT_LENGTH",
        "LZW_MAX_OUTPUT_LENGTH",
        "RUN_LENGTH_MAX_OUTPUT_LENGTH",
        "ZLIB_MAX_OUTPUT_LENGTH",
    ):
        setattr(pdf_filters, name, min(int(getattr(pdf_filters, name)), stream_limit))
    pdf_filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH = min(
        int(pdf_filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH), stream_limit
    )

    reader = PdfReader(str(path), strict=True)
    if reader.is_encrypted:
        raise ValueError("encrypted")
    total = len(reader.pages)
    if total > max_pages:
        raise ValueError("pages")
    parts: list[str] = []
    used = 0
    truncated = False
    for index, page in enumerate(reader.pages):
        part = page.extract_text() or ""
        remaining = max(0, max_chars - used)
        parts.append(part[:remaining])
        used += min(len(part), remaining)
        if len(part) > remaining or (used >= max_chars and index + 1 < total):
            truncated = True
            break
    text, cut = _clean("\n".join(parts), max_chars)
    return text, total, truncated or cut


def _docx(path: Path, max_entries: int, max_uncompressed: int, max_chars: int) -> tuple[str, int, bool]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > max_entries:
            raise ValueError("archive")
        names = [item.filename for item in entries]
        if len(set(names)) != len(names):
            raise ValueError("duplicate")
        if any(
            name.startswith(("/", "\\"))
            or ".." in Path(name.replace("\\", "/")).parts
            for name in names
        ):
            raise ValueError("path")
        name_set = set(names)
        if "[Content_Types].xml" not in name_set or "word/document.xml" not in name_set:
            raise ValueError("not-docx")
        if any(
            Path(name.casefold()).suffix in _NESTED_ARCHIVE_SUFFIXES
            or name.casefold().endswith(".bin")
            for name in names
        ):
            raise ValueError("embedded")
        if any(item.flag_bits & 0x1 for item in entries):
            raise ValueError("encrypted")
        total = sum(max(0, item.file_size) for item in entries)
        if total > max_uncompressed:
            raise ValueError("expanded")
        content_types_info = archive.getinfo("[Content_Types].xml")
        document_info = archive.getinfo("word/document.xml")
        if content_types_info.file_size > 262_144 or document_info.file_size > min(
            max_uncompressed, _MAX_DOCX_XML_BYTES
        ):
            raise ValueError("xml-size")
        content_types = archive.read(content_types_info)
        xml = archive.read(document_info)
        for item in entries:
            if item.is_dir() or item.filename in {"[Content_Types].xml", "word/document.xml"}:
                continue
            with archive.open(item) as handle:
                if handle.read(4).startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                    raise ValueError("embedded")
    upper_content_types = content_types.upper()
    upper_xml = xml.upper()
    if (
        b"<!DOCTYPE" in upper_content_types
        or b"<!ENTITY" in upper_content_types
        or b"<!DOCTYPE" in upper_xml
        or b"<!ENTITY" in upper_xml
    ):
        raise ValueError("xml")
    types_root = ET.fromstring(content_types)
    valid_main_type = any(
        node.attrib.get("PartName") == "/word/document.xml"
        and node.attrib.get("ContentType")
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        for node in types_root.iter()
        if node.tag.endswith("}Override") or node.tag == "Override"
    )
    if not valid_main_type:
        raise ValueError("content-type")
    root = ET.fromstring(xml)
    paragraphs = []
    for paragraph in (node for node in root.iter() if node.tag.endswith("}p")):
        value = "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t"))
        if value:
            paragraphs.append(value)
    text, truncated = _clean("\n".join(paragraphs), max_chars)
    return text, 1, truncated


def main() -> int:
    try:
        _apply_resource_limits()
        request = json.loads(sys.stdin.buffer.read(32_768).decode("utf-8"))
        path = Path(request["path"])
        kind = request["kind"]
        if kind == "pdf":
            text, pages, truncated = _pdf(
                path,
                int(request["max_pages"]),
                int(request["max_chars"]),
                int(request["max_pdf_stream_bytes"]),
            )
        elif kind == "docx":
            text, pages, truncated = _docx(path, int(request["max_entries"]), int(request["max_uncompressed"]), int(request["max_chars"]))
        else:
            raise ValueError("kind")
        sys.stdout.write(json.dumps({"text": text, "page_count": pages, "truncated": truncated}, ensure_ascii=False))
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
