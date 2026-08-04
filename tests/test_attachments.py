from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from agentbot.attachments import (
    AttachmentError,
    AttachmentLimits,
    AttachmentProcessor,
    AttachmentSource,
    ImageAnalysis,
    attachment_processing_admitted,
)


class _NoPersistenceMemory:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"lean attachment lane touched memory.{name}")


def _limits(**overrides: int | float) -> AttachmentLimits:
    values: dict[str, int | float] = {
        "max_bytes": 1024,
        "max_extracted_chars": 80,
        "max_pages": 1,
        "max_archive_entries": 1,
        "max_archive_uncompressed_bytes": 1024,
        "max_pixels": 10_000,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return AttachmentLimits(**values)  # type: ignore[arg-type]


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


def _docx(
    path: Path,
    text: str = "cobalt plan",
    *,
    extra: dict[str, bytes] | None = None,
) -> None:
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)


def _text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream",
    )
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(payload)


class AttachmentProcessorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.source = AttachmentSource(
            scope="g:1:c:2",
            guild_id=1,
            channel_id=2,
            message_id=3,
            user_id=4,
        )
        self.memory = _NoPersistenceMemory()

    def processor(
        self,
        *,
        limits: AttachmentLimits | None = None,
        analyzer=None,
        concurrency: int = 1,
        prompt_chars: int = 60,
    ) -> AttachmentProcessor:
        return AttachmentProcessor(
            memory=self.memory,  # type: ignore[arg-type]
            limits=limits or _limits(),
            max_cache_entries=10,
            max_chunks_per_attachment=10,
            chunk_chars=120,
            chunk_overlap=10,
            prompt_chars=prompt_chars,
            concurrency=concurrency,
            image_analyzer=analyzer,
        )

    async def test_response_gate_and_utf8_source_text_are_bounded_without_cache_writes(self) -> None:
        self.assertFalse(attachment_processing_admitted(False, 1))
        self.assertFalse(attachment_processing_admitted(True, 0))
        self.assertTrue(attachment_processing_admitted(True, 1))

        processor = self.processor(limits=_limits(max_extracted_chars=24))
        result = await processor.process_bytes(
            "café\n\tprint('hello')\ntrailing text".encode(),
            filename="example.py",
            declared_mime="text/x-python",
            source=self.source,
        )

        self.assertEqual(result.kind, "text")
        self.assertEqual(result.status, "ready")
        self.assertIn("café\n\tprint", result.prompt_text)
        self.assertTrue(result.truncated)
        self.assertFalse(result.cache_hit)
        self.assertEqual(len(result.sha256), 64)
        self.assertEqual(processor.status()["cache_hits"], 0)

    async def test_unsupported_and_malformed_documents_are_rejected(self) -> None:
        processor = self.processor()
        cases = (
            (b"%PDF-1.7", "notes.pdf", "application/pdf", "document_parse"),
            (b"PK\x03\x04docx", "notes.docx", "application/octet-stream", "document_parse"),
            (b"<p>hello</p>", "page.html", "text/html", "unsupported"),
            (b"<root>hello</root>", "data.xml", "application/xml", "unsupported"),
            (b"name: example", "data.yaml", "text/yaml", "unsupported"),
            (b"name,value", "data.csv", "text/csv", "unsupported"),
            (b"%PDF-1.7", "disguised.txt", "text/plain", "document_parse"),
            (b"not utf8: \xff", "notes.txt", "text/plain", "encoding"),
        )
        for payload, filename, mime, expected_code in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(AttachmentError) as caught:
                    await processor.process_bytes(
                        payload,
                        filename=filename,
                        declared_mime=mime,
                        source=self.source,
                    )
                self.assertEqual(caught.exception.code, expected_code)

    async def test_docx_runs_in_disposable_worker_without_a_production_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "notes.docx"
            _docx(document)
            result = await self.processor(
                limits=_limits(max_archive_entries=8, max_archive_uncompressed_bytes=4096),
            ).process_path(
                document, filename="notes.docx",
                declared_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                source=self.source,
            )
        self.assertEqual(result.kind, "docx")
        self.assertEqual(result.prompt_text, "cobalt plan")

    async def test_text_pdf_runs_in_disposable_worker_and_bytes_are_authoritative(self) -> None:
        result = await self.processor(
            limits=_limits(
                max_bytes=4096,
                max_pages=2,
                max_archive_uncompressed_bytes=4096,
            ),
        ).process_bytes(
            _text_pdf("violet lighthouse"),
            filename="misleading.txt",
            declared_mime="text/plain",
            source=self.source,
        )
        self.assertEqual(result.kind, "pdf")
        self.assertIn("violet lighthouse", result.prompt_text)

    async def test_docx_rejects_nested_packages_macros_entities_and_bounds(self) -> None:
        cases = (
            ({"word/embeddings/inner.docx": b"PK\x03\x04nested"}, "safe text", 8, 4096, "nested"),
            ({"word/vbaProject.bin": b"macro"}, "safe text", 8, 4096, "macro"),
            ({}, "<!DOCTYPE x><w:t>x</w:t>", 8, 4096, "entity"),
            ({"extra.txt": b"x"}, "safe text", 2, 4096, "entries"),
            ({"extra.txt": b"x" * 2048}, "safe text", 8, 1024, "expanded"),
        )
        for extra, text, max_entries, max_expanded, label in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                document = Path(directory) / "notes.docx"
                _docx(document, text, extra=extra)
                with self.assertRaises(AttachmentError) as caught:
                    await self.processor(
                        limits=_limits(
                            max_bytes=4096,
                            max_archive_entries=max_entries,
                            max_archive_uncompressed_bytes=max_expanded,
                        ),
                    ).process_path(
                        document,
                        filename="notes.docx",
                        declared_mime="application/octet-stream",
                        source=self.source,
                    )
                self.assertEqual(caught.exception.code, "document_parse")

    async def test_configured_document_lock_fails_closed_when_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "notes.docx"
            _docx(document, "x")
            processor = AttachmentProcessor(
                memory=self.memory,  # type: ignore[arg-type]
                limits=_limits(max_archive_entries=8),
                max_cache_entries=1, max_chunks_per_attachment=1,
                chunk_chars=1, chunk_overlap=0, prompt_chars=60, concurrency=1,
                document_lock_path=str(Path(directory) / "missing" / "gate.lock"),
            )
            with self.assertRaises(AttachmentError) as caught:
                await processor.process_path(
                    document, filename="notes.docx", declared_mime="application/octet-stream", source=self.source,
                )
        self.assertEqual(caught.exception.code, "document_lock")

    @unittest.skipUnless(os.name == "posix", "POSIX flock is Linux-only")
    async def test_document_lock_wait_uses_the_existing_message_deadline(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "notes.docx"
            lock_path = Path(directory) / "attachments.lock"
            _docx(document, "x")
            lock_path.touch()
            with lock_path.open("r+b") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                processor = AttachmentProcessor(
                    memory=self.memory,  # type: ignore[arg-type]
                    limits=_limits(
                        max_archive_entries=8,
                        max_archive_uncompressed_bytes=4096,
                        timeout_seconds=0.05,
                    ),
                    max_cache_entries=1,
                    max_chunks_per_attachment=1,
                    chunk_chars=1,
                    chunk_overlap=0,
                    prompt_chars=60,
                    concurrency=1,
                    document_lock_path=str(lock_path),
                )
                with self.assertRaises(AttachmentError) as caught:
                    await processor.process_path(
                        document,
                        filename="notes.docx",
                        declared_mime="application/octet-stream",
                        source=self.source,
                    )
        self.assertEqual(caught.exception.code, "timeout")

    async def test_cancellation_kills_and_reaps_the_disposable_worker(self) -> None:
        started = asyncio.Event()

        class HangingProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
                self.payload = payload
                started.set()
                await asyncio.Event().wait()
                return b"", b""

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> int:
                self.waited = True
                return int(self.returncode or 0)

        fake = HangingProcess()
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "notes.docx"
            _docx(document, "x")
            processor = self.processor(
                limits=_limits(max_archive_entries=8, max_archive_uncompressed_bytes=4096),
            )
            with patch(
                "agentbot.attachments.asyncio.create_subprocess_exec",
                return_value=fake,
            ):
                task = asyncio.create_task(
                    processor.process_path(
                        document,
                        filename="notes.docx",
                        declared_mime="application/octet-stream",
                        source=self.source,
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(fake.killed)
        self.assertTrue(fake.waited)

    async def test_cancellation_during_spawn_reaps_late_worker_before_return(self) -> None:
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()

        class LateProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> int:
                self.waited = True
                return int(self.returncode or 0)

        fake = LateProcess()

        async def delayed_spawn(*args, **kwargs):
            del args, kwargs
            spawn_started.set()
            await release_spawn.wait()
            return fake

        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "notes.docx"
            _docx(document, "x")
            processor = self.processor(
                limits=_limits(max_archive_entries=8, max_archive_uncompressed_bytes=4096),
            )
            with patch(
                "agentbot.attachments.asyncio.create_subprocess_exec",
                side_effect=delayed_spawn,
            ):
                task = asyncio.create_task(
                    processor.process_path(
                        document,
                        filename="notes.docx",
                        declared_mime="application/octet-stream",
                        source=self.source,
                    )
                )
                await asyncio.wait_for(spawn_started.wait(), timeout=1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release_spawn.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(fake.killed)
        self.assertTrue(fake.waited)

    async def test_document_worker_protocol_accepts_maximum_unicode_and_escaping(self) -> None:
        fixtures = (
            "\U0001f642" * 16_000,
            ((chr(34) + chr(92)) * 8_000),
        )
        for expected in fixtures:
            with self.subTest(prefix=expected[:2]):
                with tempfile.TemporaryDirectory() as directory:
                    document = Path(directory) / "notes.docx"
                    _docx(document, expected)
                    processor = self.processor(
                        limits=_limits(
                            max_bytes=200_000,
                            max_extracted_chars=16_000,
                            max_archive_entries=8,
                            max_archive_uncompressed_bytes=200_000,
                            timeout_seconds=5,
                        ),
                        prompt_chars=16_000,
                    )
                    result = await processor.process_path(
                        document,
                        filename="notes.docx",
                        declared_mime="application/octet-stream",
                        source=self.source,
                    )
                self.assertEqual(result.prompt_text, expected)
                self.assertFalse(result.truncated)

    async def test_png_jpeg_and_webp_use_caption_only(self) -> None:
        calls: list[bytes] = []

        async def analyze(data: bytes) -> ImageAnalysis:
            calls.append(data)
            return ImageAnalysis(
                caption=f"a compact caption for image {len(calls)}",
                interrogation="ignored, legacy, tags",
            )

        processor = self.processor(analyzer=analyze)
        fixtures = (
            (_png(2, 3), "image.png", "image/png", (2, 3)),
            (_jpeg(4, 5), "image.jpg", "image/jpeg", (4, 5)),
            (_webp(6, 7), "image.webp", "image/webp", (6, 7)),
        )
        for payload, filename, mime, dimensions in fixtures:
            result = await processor.process_bytes(
                payload,
                filename=filename,
                declared_mime=mime,
                source=self.source,
            )
            self.assertEqual(result.kind, "image")
            self.assertEqual((result.width, result.height), dimensions)
            self.assertIn("compact caption", result.prompt_text)
            self.assertNotIn("legacy", result.prompt_text)
        self.assertEqual(len(calls), 3)

    async def test_image_signature_is_authoritative_over_discord_metadata(self) -> None:
        calls: list[bytes] = []

        async def analyze(data: bytes) -> ImageAnalysis:
            calls.append(data)
            return ImageAnalysis(caption="the uploaded screenshot shows a bot profile")

        payload = _png(2, 2)
        result = await self.processor(analyzer=analyze).process_bytes(
            payload,
            filename="image.png",
            declared_mime="image/webp",
            source=self.source,
        )

        self.assertEqual(result.kind, "image")
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.prompt_text, "the uploaded screenshot shows a bot profile")
        self.assertEqual(calls, [payload])

    async def test_image_signature_pixel_limit_and_analyzer_availability_are_enforced(self) -> None:
        async def analyze(data: bytes) -> ImageAnalysis:
            del data
            return ImageAnalysis(caption="caption")

        with self.assertRaises(AttachmentError) as mismatch:
            await self.processor(analyzer=analyze).process_bytes(
                b"this is not image data",
                filename="image.jpg",
                declared_mime="image/jpeg",
                source=self.source,
            )
        self.assertEqual(mismatch.exception.code, "signature_mismatch")

        with self.assertRaises(AttachmentError) as pixels:
            await self.processor(
                limits=_limits(max_pixels=3),
                analyzer=analyze,
            ).process_bytes(
                _png(2, 2),
                filename="image.png",
                declared_mime="image/png",
                source=self.source,
            )
        self.assertEqual(pixels.exception.code, "pixel_limit")

        with self.assertRaises(AttachmentError) as unavailable:
            await self.processor().process_bytes(
                _png(2, 2),
                filename="image.png",
                declared_mime="image/png",
                source=self.source,
            )
        self.assertEqual(unavailable.exception.code, "alchemy_unavailable")

    async def test_download_requires_exact_discord_cdn_and_streams_without_redirects(self) -> None:
        class Content:
            def __init__(self, blocks: tuple[bytes, ...]) -> None:
                self.blocks = blocks

            async def iter_chunked(self, size: int):
                del size
                for block in self.blocks:
                    yield block

        class Response:
            status = 200
            content_length = None

            def __init__(self, blocks: tuple[bytes, ...]) -> None:
                self.content = Content(blocks)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback

        class Session:
            def __init__(self, blocks: tuple[bytes, ...]) -> None:
                self.blocks = blocks
                self.calls: list[tuple[str, bool, object]] = []

            def get(self, url: str, *, allow_redirects: bool, timeout: object):
                self.calls.append((url, allow_redirects, timeout))
                return Response(self.blocks)

        analyzer_calls: list[bytes] = []

        async def analyze(data: bytes) -> ImageAnalysis:
            analyzer_calls.append(data)
            return ImageAnalysis(caption="downloaded screenshot")

        payload = _png(2, 2)
        session = Session((payload[:12], payload[12:]))
        processor = self.processor(limits=_limits(max_bytes=64), analyzer=analyze)
        result = await processor.process_url(
            session,  # type: ignore[arg-type]
            "https://cdn.discordapp.com/attachments/1/2/image.png?ex=signed",
            filename="image.png",
            declared_mime="image/webp",
            declared_size=len(payload),
            source=self.source,
        )
        self.assertEqual(result.prompt_text, "downloaded screenshot")
        self.assertEqual(analyzer_calls, [payload])
        self.assertFalse(session.calls[0][1])

        for invalid in (
            "http://cdn.discordapp.com/attachments/1/2/notes.txt",
            "https://media.discordapp.net/attachments/1/2/notes.txt",
            "https://cdn.discordapp.com.evil.test/attachments/1/2/notes.txt",
            "https://cdn.discordapp.com/not-attachments/notes.txt",
        ):
            with self.subTest(url=invalid):
                with self.assertRaises(AttachmentError) as caught:
                    await processor.process_url(
                        session,  # type: ignore[arg-type]
                        invalid,
                        filename="notes.txt",
                        declared_mime="text/plain",
                        declared_size=1,
                        source=self.source,
                    )
                self.assertEqual(caught.exception.code, "download_url")
        self.assertEqual(len(session.calls), 1)

        oversized = Session((b"12345678", b"9"))
        with self.assertRaises(AttachmentError) as caught:
            await self.processor(limits=_limits(max_bytes=8)).process_url(
                oversized,  # type: ignore[arg-type]
                "https://cdn.discordapp.com/attachments/1/2/notes.txt",
                filename="notes.txt",
                declared_mime="text/plain",
                declared_size=1,
                source=self.source,
            )
        self.assertEqual(caught.exception.code, "size")

    async def test_alchemist_failures_are_transient_and_not_cached(self) -> None:
        calls = 0

        async def analyze(data: bytes) -> ImageAnalysis:
            nonlocal calls
            del data
            calls += 1
            if calls == 1:
                raise AttachmentError("temporary Horde failure", code="alchemy")
            return ImageAnalysis(caption="recovered caption")

        processor = self.processor(analyzer=analyze)
        payload = _png(2, 2)
        with self.assertRaises(AttachmentError) as caught:
            await processor.process_bytes(
                payload,
                filename="image.png",
                declared_mime="image/png",
                source=self.source,
            )
        self.assertEqual(caught.exception.code, "alchemy")

        recovered = await processor.process_bytes(
            payload,
            filename="image.png",
            declared_mime="image/png",
            source=self.source,
        )
        self.assertEqual(recovered.prompt_text, "recovered caption")
        self.assertFalse(recovered.cache_hit)
        self.assertEqual(calls, 2)
        self.assertEqual(processor.status()["failed_jobs"], 1)
        self.assertEqual(processor.status()["processed_jobs"], 1)

    async def test_one_deadline_and_global_concurrency_bound_the_lane(self) -> None:
        active = 0
        peak = 0

        async def analyze(data: bytes) -> ImageAnalysis:
            nonlocal active, peak
            del data
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ImageAnalysis(caption="bounded caption")

        processor = self.processor(analyzer=analyze, concurrency=1)
        await asyncio.gather(
            processor.process_bytes(
                _png(2, 2),
                filename="one.png",
                declared_mime="image/png",
                source=self.source,
            ),
            processor.process_bytes(
                _png(3, 3),
                filename="two.png",
                declared_mime="image/png",
                source=self.source,
            ),
        )
        self.assertEqual(peak, 1)
        self.assertEqual(processor.status()["peak_active_jobs"], 1)

        async def too_slow(data: bytes) -> ImageAnalysis:
            del data
            await asyncio.sleep(0.2)
            return ImageAnalysis(caption="too late")

        timed = self.processor(
            limits=_limits(timeout_seconds=0.05),
            analyzer=too_slow,
        )
        with self.assertRaises(AttachmentError) as caught:
            await timed.process_bytes(
                _png(2, 2),
                filename="slow.png",
                declared_mime="image/png",
                source=self.source,
            )
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(timed.status()["active_jobs"], 0)


if __name__ == "__main__":
    unittest.main()
