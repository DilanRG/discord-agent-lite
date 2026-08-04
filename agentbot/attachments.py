from __future__ import annotations

import asyncio
import hashlib
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Sequence
from urllib.parse import urlparse

import aiohttp

if TYPE_CHECKING:
    from .memory import MemoryStore


_DISCORD_CDN_HOST = "cdn.discordapp.com"
_GENERIC_MIMES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".sh",
        ".ps1",
        ".sql",
        ".json",
        ".css",
        ".log",
        ".ini",
        ".cfg",
        ".conf",
        ".toml",
    }
)
_TEXT_APPLICATION_MIMES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/x-javascript",
    }
)
_IMAGE_EXTENSIONS = {
    "png": frozenset({".png"}),
    "jpeg": frozenset({".jpg", ".jpeg", ".jpe"}),
    "webp": frozenset({".webp"}),
}
_IMAGE_MIMES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class AttachmentError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AttachmentLimits:
    # The document-era fields stay in this public shape so existing settings and
    # deployments can move to the lean lane without a coordinated migration.
    max_bytes: int
    max_extracted_chars: int
    max_pages: int
    max_archive_entries: int
    max_archive_uncompressed_bytes: int
    max_pixels: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    kind: str
    text: str = ""
    page_texts: tuple[str, ...] = ()
    page_count: int = 0
    width: int = 0
    height: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ImageAnalysis:
    caption: str
    interrogation: str = ""
    worker_id: str = ""
    worker_name: str = ""
    confidence: float = 0.5


@dataclass(frozen=True, slots=True)
class AttachmentSource:
    scope: str
    guild_id: int
    channel_id: int
    message_id: int | None
    user_id: int
    privacy_revision: int = 0


@dataclass(frozen=True, slots=True)
class ProcessedAttachment:
    sha256: str
    filename: str
    kind: str
    status: str
    prompt_text: str
    cache_hit: bool
    error: str = ""
    page_count: int = 0
    width: int = 0
    height: int = 0
    truncated: bool = False


def attachment_processing_admitted(will_respond: bool, attachment_count: int) -> bool:
    """Keep attachment work behind the caller's response decision."""

    return bool(will_respond and attachment_count > 0)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _timeout_error() -> AttachmentError:
    return AttachmentError(
        "Attachment processing exceeded the configured time limit.",
        code="timeout",
    )


def _declared_mime(value: str) -> str:
    return value.partition(";")[0].strip().casefold()


def _image_kind(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _looks_binary_or_document(data: bytes) -> bool:
    return data.startswith(
        (
            b"MZ",
            b"\x7fELF",
            b"\xca\xfe\xba\xbe",
            b"\xfe\xed\xfa",
            b"\xcf\xfa\xed\xfe",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            b"%PDF-",
            b"PK\x03\x04",
            b"PK\x05\x06",
            b"PK\x07\x08",
            b"GIF87a",
            b"GIF89a",
        )
    )


def _validate_declared_type(filename: str, declared_mime: str) -> str:
    suffix = Path(filename).suffix.casefold()
    mime = _declared_mime(declared_mime)
    for kind, extensions in _IMAGE_EXTENSIONS.items():
        if suffix in extensions:
            if mime not in (_GENERIC_MIMES | {_IMAGE_MIMES[kind]}):
                raise AttachmentError(
                    "The image type does not match its MIME declaration.",
                    code="signature_mismatch",
                )
            return kind
    if suffix not in _TEXT_EXTENSIONS:
        raise AttachmentError("This attachment type is not supported.", code="unsupported")
    if mime not in _GENERIC_MIMES and not (
        mime.startswith("text/") or mime in _TEXT_APPLICATION_MIMES
    ):
        raise AttachmentError(
            "The attachment type does not match its MIME declaration.",
            code="signature_mismatch",
        )
    return "text"


def _image_dimensions(data: bytes, kind: str) -> tuple[int, int]:
    if kind == "png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise AttachmentError("The PNG header is malformed.", code="malformed_image")
        return struct.unpack(">II", data[16:24])
    if kind == "jpeg":
        index = 2
        while index + 9 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = struct.unpack(">H", data[index : index + 2])[0]
            if length < 2 or index + length > len(data):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height, width = struct.unpack(">HH", data[index + 3 : index + 7])
                return width, height
            index += length
        raise AttachmentError("The JPEG dimensions could not be read.", code="malformed_image")
    if kind == "webp":
        if len(data) < 25:
            raise AttachmentError("The WebP header is malformed.", code="malformed_image")
        flavor = data[12:16]
        if flavor == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if flavor == b"VP8 ":
            marker = data.find(b"\x9d\x01\x2a", 20)
            if marker >= 0 and marker + 7 <= len(data):
                width, height = struct.unpack("<HH", data[marker + 3 : marker + 7])
                return width & 0x3FFF, height & 0x3FFF
        if flavor == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        raise AttachmentError("The WebP dimensions could not be read.", code="malformed_image")
    raise AttachmentError("Unsupported image signature.", code="unsupported")


def _clean_text(text: str, limit: int) -> tuple[str, bool]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        character
        for character in normalized
        if character in "\n\t" or ord(character) >= 32
    ).strip()
    bounded_limit = max(0, int(limit))
    return cleaned[:bounded_limit], len(cleaned) > bounded_limit


def _read_path_bounded(path: Path, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(max(0, int(max_bytes)) + 1)
    except OSError as exc:
        raise AttachmentError("The attachment could not be read.", code="io") from exc
    if len(data) > max_bytes:
        raise AttachmentError(
            "The attachment exceeds the configured size limit.",
            code="size",
        )
    return data


def _extract_bytes(
    data: bytes,
    *,
    filename: str,
    declared_mime: str,
    limits: AttachmentLimits,
) -> ExtractionResult:
    if not data:
        raise AttachmentError("The attachment is empty.", code="empty")
    if len(data) > limits.max_bytes:
        raise AttachmentError("The attachment exceeds the configured size limit.", code="size")
    detected_image = _image_kind(data)
    if detected_image:
        width, height = _image_dimensions(data, detected_image)
        if width <= 0 or height <= 0 or width * height > limits.max_pixels:
            raise AttachmentError(
                "The image exceeds the configured pixel limit.",
                code="pixel_limit",
            )
        return ExtractionResult(kind="image", width=width, height=height)

    declared_kind = _validate_declared_type(filename, declared_mime)
    if declared_kind != "text":
        raise AttachmentError(
            "The image signature does not match its filename or MIME type.",
            code="signature_mismatch",
        )
    if _looks_binary_or_document(data) or b"\x00" in data:
        raise AttachmentError("Binary data cannot be processed as text.", code="binary")
    try:
        decoded = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise AttachmentError(
            "Text attachments must use UTF-8 encoding.",
            code="encoding",
        ) from exc
    text, truncated = _clean_text(decoded, limits.max_extracted_chars)
    if not text:
        raise AttachmentError(
            "The text attachment has no usable text.",
            code="empty_text",
        )
    return ExtractionResult(kind="text", text=text, truncated=truncated)


def extract_document(
    path: Path,
    *,
    filename: str,
    declared_mime: str,
    limits: AttachmentLimits,
    clock: Callable[[], float] = time.monotonic,
) -> ExtractionResult:
    """Compatibility entry point for the lean, non-document extractor."""

    started = clock()
    data = _read_path_bounded(path, limits.max_bytes)
    if clock() - started > limits.timeout_seconds:
        raise _timeout_error()
    return _extract_bytes(
        data,
        filename=filename,
        declared_mime=declared_mime,
        limits=limits,
    )


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AttachmentError(
            "Only Discord CDN attachment URLs are accepted.",
            code="download_url",
        ) from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != _DISCORD_CDN_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/attachments/")
        or parsed.fragment
    ):
        raise AttachmentError(
            "Only Discord CDN attachment URLs are accepted.",
            code="download_url",
        )


class AttachmentProcessor:
    def __init__(
        self,
        *,
        memory: MemoryStore,
        limits: AttachmentLimits,
        max_cache_entries: int,
        max_chunks_per_attachment: int,
        chunk_chars: int,
        chunk_overlap: int,
        prompt_chars: int,
        concurrency: int,
        image_analyzer: Callable[[bytes], Awaitable[ImageAnalysis]] | None = None,
        image_cache_namespace: str = "image-analyzer-v1",
        extractor: Callable[..., ExtractionResult] = extract_document,
        isolate_extractor: bool | None = None,
        parser_command: Sequence[str] | None = None,
    ) -> None:
        # Keep constructor compatibility while deliberately dropping cache,
        # chunking, FTS, parser-process and image-cache behavior.
        self.memory = memory
        self.limits = limits
        self.max_cache_entries = max(1, int(max_cache_entries))
        self.max_chunks_per_attachment = max(1, int(max_chunks_per_attachment))
        self.chunk_chars = max(1, int(chunk_chars))
        self.chunk_overlap = max(0, int(chunk_overlap))
        self.prompt_chars = max(1, int(prompt_chars))
        self.image_analyzer = image_analyzer
        self.extractor = extractor
        del image_cache_namespace, isolate_extractor, parser_command
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._active_jobs = 0
        self._peak_active_jobs = 0
        self._processed_jobs = 0
        self._failed_jobs = 0

    def status(self) -> dict[str, int]:
        return {
            "active_jobs": self._active_jobs,
            "peak_active_jobs": self._peak_active_jobs,
            "processed_jobs": self._processed_jobs,
            "failed_jobs": self._failed_jobs,
            "cache_hits": 0,
            "hash_locks": 0,
        }

    def _new_deadline(self) -> float:
        return time.monotonic() + max(0.001, float(self.limits.timeout_seconds))

    async def _extract(
        self,
        path: Path,
        data: bytes,
        *,
        filename: str,
        declared_mime: str,
        deadline: float,
    ) -> ExtractionResult:
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise _timeout_error()
        try:
            if self.extractor is extract_document:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        _extract_bytes,
                        data,
                        filename=filename,
                        declared_mime=declared_mime,
                        limits=self.limits,
                    ),
                    timeout=remaining,
                )
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.extractor,
                    path,
                    filename=filename,
                    declared_mime=declared_mime,
                    limits=self.limits,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise _timeout_error() from exc

    async def process_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        declared_mime: str,
        source: AttachmentSource,
        persist: bool = True,
    ) -> ProcessedAttachment:
        deadline = self._new_deadline()
        if len(data) > self.limits.max_bytes:
            raise AttachmentError(
                "The attachment exceeds the configured size limit.",
                code="size",
            )
        with tempfile.TemporaryDirectory(prefix="discord-agent-attachment-") as directory:
            path = Path(directory) / "upload.bin"
            try:
                path.write_bytes(data)
            except OSError as exc:
                raise AttachmentError(
                    "The attachment could not be buffered.",
                    code="io",
                ) from exc
            return await self.process_path(
                path,
                filename=filename,
                declared_mime=declared_mime,
                source=source,
                persist=persist,
                _deadline=deadline,
            )

    async def process_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        filename: str,
        declared_mime: str,
        declared_size: int,
        source: AttachmentSource,
        persist: bool = True,
        _deadline: float | None = None,
    ) -> ProcessedAttachment:
        deadline = self._new_deadline() if _deadline is None else float(_deadline)
        _validate_download_url(url)
        if declared_size < 0 or declared_size > self.limits.max_bytes:
            raise AttachmentError(
                "The attachment exceeds the configured size limit.",
                code="size",
            )
        with tempfile.TemporaryDirectory(prefix="discord-agent-attachment-") as directory:
            path = Path(directory) / "download.bin"
            downloaded = 0
            try:
                remaining = _remaining(deadline)
                if remaining <= 0:
                    raise _timeout_error()
                async with session.get(
                    url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=remaining),
                ) as response:
                    if response.status != 200:
                        raise AttachmentError(
                            f"Discord CDN returned HTTP {response.status}.",
                            code="download",
                        )
                    if (
                        response.content_length is not None
                        and response.content_length > self.limits.max_bytes
                    ):
                        raise AttachmentError(
                            "The downloaded attachment exceeds the configured size limit.",
                            code="size",
                        )
                    with path.open("wb") as handle:
                        async for block in response.content.iter_chunked(65_536):
                            if _remaining(deadline) <= 0:
                                raise _timeout_error()
                            downloaded += len(block)
                            if downloaded > self.limits.max_bytes:
                                raise AttachmentError(
                                    "The downloaded attachment exceeds the configured size limit.",
                                    code="size",
                                )
                            handle.write(block)
            except asyncio.CancelledError:
                raise
            except AttachmentError:
                raise
            except asyncio.TimeoutError as exc:
                raise _timeout_error() from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise AttachmentError(
                    "The Discord attachment could not be downloaded.",
                    code="download",
                ) from exc
            return await self.process_path(
                path,
                filename=filename,
                declared_mime=declared_mime,
                source=source,
                persist=persist,
                _deadline=deadline,
            )

    async def process_path(
        self,
        path: Path,
        *,
        filename: str,
        declared_mime: str,
        source: AttachmentSource,
        persist: bool = True,
        _deadline: float | None = None,
    ) -> ProcessedAttachment:
        del source, persist
        deadline = self._new_deadline() if _deadline is None else float(_deadline)
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise _timeout_error()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise _timeout_error() from exc
        self._active_jobs += 1
        self._peak_active_jobs = max(self._peak_active_jobs, self._active_jobs)
        try:
            try:
                remaining = _remaining(deadline)
                if remaining <= 0:
                    raise _timeout_error()
                try:
                    data = await asyncio.wait_for(
                        asyncio.to_thread(
                            _read_path_bounded,
                            path,
                            self.limits.max_bytes,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError as exc:
                    raise _timeout_error() from exc
                result = await self._extract(
                    path,
                    data,
                    filename=filename,
                    declared_mime=declared_mime,
                    deadline=deadline,
                )
                if result.kind == "image":
                    if self.image_analyzer is None:
                        raise AttachmentError(
                            "Remote image analysis is unavailable.",
                            code="alchemy_unavailable",
                        )
                    remaining = _remaining(deadline)
                    if remaining <= 0:
                        raise _timeout_error()
                    try:
                        analysis = await asyncio.wait_for(
                            self.image_analyzer(data),
                            timeout=remaining,
                        )
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError as exc:
                        raise _timeout_error() from exc
                    except AttachmentError:
                        raise
                    except Exception as exc:
                        raise AttachmentError(
                            "Remote image analysis failed.",
                            code="alchemy",
                        ) from exc
                    caption, truncated = _clean_text(
                        analysis.caption,
                        self.limits.max_extracted_chars,
                    )
                    if not caption:
                        raise AttachmentError(
                            "Remote image analysis returned no caption.",
                            code="alchemy",
                        )
                    result = ExtractionResult(
                        kind="image",
                        text=caption,
                        width=result.width,
                        height=result.height,
                        truncated=truncated,
                    )
            except AttachmentError:
                self._failed_jobs += 1
                raise
            self._processed_jobs += 1
            return ProcessedAttachment(
                sha256=hashlib.sha256(data).hexdigest(),
                filename=filename[:180],
                kind=result.kind,
                status="ready",
                prompt_text=result.text[: self.prompt_chars],
                cache_hit=False,
                page_count=result.page_count,
                width=result.width,
                height=result.height,
                truncated=result.truncated or len(result.text) > self.prompt_chars,
            )
        finally:
            self._active_jobs -= 1
            self._semaphore.release()
