from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agentbot.horde_client import (
    HordeClient,
    HordeClientError,
    HordeNoWorkerError,
    HordeTransportError,
    _generation_diagnostics,
    _read_json_value,
    parse_interrogation_status,
)


class _ChunkedContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(self, chunks: list[bytes]) -> None:
        self.content = _ChunkedContent(chunks)


class _ContextResponse(_Response):
    def __init__(self, status: int, payload: object) -> None:
        super().__init__([json.dumps(payload).encode()])
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class _RawContextResponse(_Response):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__([body])
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class _GenerationSession:
    def __init__(self, *, submit, status=None, poll_status_code: int = 200) -> None:
        self.submit = submit
        self.status = status
        self.poll_status_code = poll_status_code
        self.events: list[str] = []
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
        del url, headers
        self.events.append("post")
        self.posts.append(json)
        return self.submit

    def get(self, url: str, *, headers: dict[str, str]):
        del url, headers
        self.events.append("get")
        return _ContextResponse(self.poll_status_code, self.status)

    def delete(self, url: str, *, headers: dict[str, str]):
        del url, headers
        self.events.append("delete")
        return _ContextResponse(200, {})


class _InterrogationSession:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
        self.posts.append((url, headers, json))
        return _ContextResponse(202, {"id": "request-1"})

    def get(self, url: str, *, headers: dict[str, str]):
        del url, headers
        return _ContextResponse(
            200,
            {
                "state": "done",
                "forms": [
                    {
                        "form": "caption",
                        "state": "done",
                        "result": {"caption": "A bounded test image"},
                    }
                ],
            },
        )

    def delete(self, url: str, *, headers: dict[str, str]):
        del url, headers
        return _ContextResponse(200, {})


class _StalledDeleteResponse:
    async def __aenter__(self):
        import asyncio

        await asyncio.Event().wait()

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class _StalledDeleteSession:
    def delete(self, url: str, *, headers: dict[str, str]):
        del url, headers
        return _StalledDeleteResponse()


class _TimeoutCleanupResponse:
    def __init__(self, session: "_TimeoutCleanupSession") -> None:
        self.session = session

    async def __aenter__(self):
        self.session.delete_calls += 1
        self.session.delete_started.set()
        await self.session.release_delete.wait()

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class _TimeoutCleanupSession:
    def __init__(self) -> None:
        import asyncio

        self.delete_calls = 0
        self.delete_started = asyncio.Event()
        self.release_delete = asyncio.Event()

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
        del url, headers, json
        return _ContextResponse(202, {"id": "request-1"})

    def delete(self, url: str, *, headers: dict[str, str]):
        del url, headers
        return _TimeoutCleanupResponse(self)


class HordeClientTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _generation_client(session: object) -> HordeClient:
        return HordeClient(
            session=session,  # type: ignore[arg-type]
            api_key="test-key",
            base_url="https://aihorde.example/api/v2",
            poll_seconds=1,
            timeout_seconds=30,
            client_agent="test-client",
        )

    async def test_non_json_server_failure_is_retryable(self) -> None:
        session = _GenerationSession(
            submit=_RawContextResponse(503, b"<html>temporarily unavailable</html>")
        )
        client = self._generation_client(session)
        with self.assertRaises(HordeTransportError):
            await client.generate(
                model="koboldcpp/Test-8B",
                prompt="test",
                stop_sequences=(),
                max_tokens=72,
                context_tokens=8192,
                temperature=0.7,
                trusted_workers=True,
                recommended_settings={},
            )
        self.assertEqual(session.events, ["post"])
        self.assertFalse(session.posts[0]["params"]["use_default_badwordsids"])
        self.assertTrue(session.posts[0]["validated_backends"])

    async def test_retryable_accepted_job_failure_is_cancelled(self) -> None:
        cases = (
            ({"is_possible": False, "done": False}, 200, HordeNoWorkerError),
            ({"faulted": True, "done": False}, 200, HordeTransportError),
            (
                {"rc": "NoValidWorkers", "message": "no compatible worker"},
                400,
                HordeNoWorkerError,
            ),
        )
        for status, status_code, error_type in cases:
            with self.subTest(status=status, status_code=status_code):
                session = _GenerationSession(
                    submit=_ContextResponse(202, {"id": "request-1"}),
                    status=status,
                    poll_status_code=status_code,
                )
                client = self._generation_client(session)
                with patch("agentbot.horde_client.asyncio.sleep", return_value=None):
                    with self.assertRaises(error_type):
                        await client.generate(
                            model="koboldcpp/Test-8B",
                            prompt="test",
                            stop_sequences=(),
                            max_tokens=72,
                            context_tokens=8192,
                            temperature=0.7,
                            trusted_workers=True,
                            recommended_settings={},
                        )
                self.assertEqual(session.events, ["post", "get", "delete"])

    async def test_json_reader_handles_chunking_and_enforces_body_limit(self) -> None:
        cases = (
            {
                "name": "partial_network_chunks",
                "response": _Response([b'{"models":[', b'"one",', b'"two"]}']),
                "expected": {"models": ["one", "two"]},
                "error": None,
            },
            {
                "name": "oversized_body",
                "response": _Response([b"x" * 1_500_000, b"y" * 600_000]),
                "expected": None,
                "error": HordeClientError,
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                if case["error"] is not None:
                    with self.assertRaises(case["error"]):
                        await _read_json_value(case["response"])
                else:
                    self.assertEqual(
                        await _read_json_value(case["response"]),
                        case["expected"],
                    )

    def test_generation_diagnostics_detect_truncation_and_wrong_field_types(self) -> None:
        self.assertEqual(
            _generation_diagnostics(
                {
                    "state": "length",
                    "model": "model",
                    "worker_id": 42,
                    "worker_name": "worker",
                }
            ),
            (True, True),
        )

    def test_interrogation_status_parses_caption_and_optional_interrogation(self) -> None:
        result = parse_interrogation_status(
            {
                "state": "done",
                "forms": [
                    {
                        "form": "caption",
                        "state": "done",
                        "result": {"caption": "A red square"},
                    },
                    {
                        "form": "interrogation",
                        "state": "done",
                        "result": {"interrogation": "red, geometric, square"},
                    },
                ],
            },
            requested_forms=("caption", "interrogation"),
        )
        self.assertEqual(result.caption, "A red square")
        self.assertEqual(result.interrogation, "red, geometric, square")

        with self.assertRaises(HordeClientError):
            parse_interrogation_status(
                {"state": "done", "forms": []},
                requested_forms=("caption",),
            )

    async def test_interrogation_submit_uses_bare_base64_and_parses_poll_result(self) -> None:
        session = _InterrogationSession()
        client = HordeClient(
            session=session,  # type: ignore[arg-type]
            api_key="test-key",
            base_url="https://aihorde.example/api/v2",
            poll_seconds=1,
            timeout_seconds=30,
            client_agent="test-client",
        )
        with patch("agentbot.horde_client.asyncio.sleep", return_value=None):
            result = await client.interrogate_image(
                b"\x89PNG\r\n\x1a\n",
                forms=("caption",),
                trusted_workers=True,
            )
        self.assertEqual(result.caption, "A bounded test image")
        self.assertEqual(len(session.posts), 1)
        url, headers, payload = session.posts[0]
        self.assertTrue(url.endswith("/interrogate/async"))
        self.assertEqual(headers["apikey"], "test-key")
        self.assertEqual(payload["source_image"], "iVBORw0KGgo=")
        self.assertEqual(payload["forms"], [{"name": "caption"}])
        self.assertTrue(payload["trusted_workers"])

    async def test_interrogation_cancellation_cleanup_is_bounded(self) -> None:
        client = HordeClient(
            session=_StalledDeleteSession(),  # type: ignore[arg-type]
            api_key="test-key",
            base_url="https://aihorde.example/api/v2",
            poll_seconds=1,
            timeout_seconds=30,
            client_agent="test-client",
        )
        with patch("agentbot.horde_client._CANCEL_GRACE_SECONDS", 0.01):
            await client._cancel_interrogation_bounded("request-1")

    async def test_interrogation_timeout_cancellation_sends_one_delete(self) -> None:
        import asyncio

        session = _TimeoutCleanupSession()
        client = HordeClient(
            session=session,  # type: ignore[arg-type]
            api_key="test-key",
            base_url="https://aihorde.example/api/v2",
            poll_seconds=1,
            timeout_seconds=30,
            client_agent="test-client",
        )
        client.timeout_seconds = 0
        with patch("agentbot.horde_client._CANCEL_GRACE_SECONDS", 1.0):
            task = asyncio.create_task(
                client.interrogate_image(b"image", forms=("caption",))
            )
            await asyncio.wait_for(session.delete_started.wait(), timeout=1)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
                if task.done() or session.delete_calls > 1:
                    break
            session.release_delete.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(session.delete_calls, 1)


if __name__ == "__main__":
    unittest.main()
