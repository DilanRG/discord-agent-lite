from __future__ import annotations

import asyncio
import struct
import tempfile
import unittest
from pathlib import Path

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
    ) -> AttachmentProcessor:
        return AttachmentProcessor(
            memory=self.memory,  # type: ignore[arg-type]
            limits=limits or _limits(),
            max_cache_entries=10,
            max_chunks_per_attachment=10,
            chunk_chars=120,
            chunk_overlap=10,
            prompt_chars=60,
            concurrency=concurrency,
            image_analyzer=analyzer,
        )

    async def test_response_gate_and_utf8_source_text_are_bounded_current_turn_only(self) -> None:
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

    async def test_document_data_formats_binary_and_non_utf8_are_rejected(self) -> None:
        processor = self.processor()
        cases = (
            (b"%PDF-1.7", "notes.pdf", "application/pdf", "unsupported"),
            (b"PK\x03\x04docx", "notes.docx", "application/octet-stream", "unsupported"),
            (b"<p>hello</p>", "page.html", "text/html", "unsupported"),
            (b"<root>hello</root>", "data.xml", "application/xml", "unsupported"),
            (b"name: example", "data.yaml", "text/yaml", "unsupported"),
            (b"name,value", "data.csv", "text/csv", "unsupported"),
            (b"%PDF-1.7", "disguised.txt", "text/plain", "binary"),
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
                _png(2, 2),
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

        session = Session((b"Discord ", b"attachment"))
        processor = self.processor(limits=_limits(max_bytes=32))
        result = await processor.process_url(
            session,  # type: ignore[arg-type]
            "https://cdn.discordapp.com/attachments/1/2/notes.txt?ex=signed",
            filename="notes.txt",
            declared_mime="text/plain",
            declared_size=18,
            source=self.source,
        )
        self.assertEqual(result.prompt_text, "Discord attachment")
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
