from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp


_MAX_JSON_BYTES = 2_000_000
_MAX_REFERENCE_BYTES = 4_000_000
_CANCEL_GRACE_SECONDS = 2.0
_MODEL_REFERENCE_URL = (
    "https://raw.githubusercontent.com/Haidra-Org/"
    "AI-Horde-text-model-reference/main/models.csv"
)
_NO_WORKER_CODES = frozenset(
    {"NoValidWorkers", "UnsupportedModel", "UnexpectedModelName"}
)


def _consume_cancel_task(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


class HordeClientError(RuntimeError):
    """A recoverable AI Horde transport or response failure."""


class HordeRetryableError(HordeClientError):
    """A transient failure that may use one different model attempt."""


class HordeTransportError(HordeRetryableError):
    """A transient network, server, or accepted-generation fault."""


class HordeNoWorkerError(HordeRetryableError):
    def __init__(self, message: str, *, error_code: str = "NoValidWorkers") -> None:
        super().__init__(message)
        self.error_code = error_code


class HordeTimeoutError(HordeRetryableError):
    """Raised when an accepted Horde generation does not finish in time."""


class HordeOutputError(HordeRetryableError):
    def __init__(self, message: str, *, empty: bool, malformed: bool) -> None:
        super().__init__(message)
        self.empty = empty
        self.malformed = malformed


@dataclass(frozen=True, slots=True)
class HordeGeneration:
    text: str
    model: str
    worker_id: str
    worker_name: str
    latency_seconds: float
    truncated: bool
    malformed: bool


@dataclass(frozen=True, slots=True)
class HordeInterrogation:
    caption: str
    interrogation: str
    worker_id: str
    worker_name: str
    latency_seconds: float


async def _read_bounded_body(
    response: aiohttp.ClientResponse,
    *,
    limit: int,
    label: str,
) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(65_536):
        body.extend(chunk)
        if len(body) > limit:
            raise HordeClientError(f"{label} was unexpectedly large")
    return bytes(body)


async def _read_json_value(response: aiohttp.ClientResponse) -> object:
    body = await _read_bounded_body(
        response,
        limit=_MAX_JSON_BYTES,
        label="AI Horde response",
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HordeClientError("AI Horde returned invalid JSON") from exc


def _error_details(payload: object, status: int) -> tuple[str, str]:
    if isinstance(payload, dict):
        code = payload.get("rc")
        message = payload.get("message")
        clean_code = code if isinstance(code, str) else ""
        clean_message = message if isinstance(message, str) else ""
        if clean_code or clean_message:
            return clean_code, clean_message[:240]
    return "", f"AI Horde HTTP error {status}"


def _recommended_params(settings: Mapping[str, object]) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    bounds: dict[str, tuple[float, float]] = {
        "top_p": (0.05, 1.0),
        "top_k": (0.0, 200.0),
        "rep_pen": (0.8, 1.5),
        "rep_pen_range": (0.0, 8192.0),
        "tfs": (0.0, 1.0),
        "typical": (0.0, 1.0),
        "min_p": (0.0, 1.0),
    }
    aliases = {"repetition_penalty": "rep_pen"}
    for raw_name, raw_value in settings.items():
        name = aliases.get(str(raw_name), str(raw_name))
        if name not in bounds or isinstance(raw_value, bool):
            continue
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            continue
        minimum, maximum = bounds[name]
        if not minimum <= number <= maximum:
            continue
        result[name] = int(number) if name in {"top_k", "rep_pen_range"} else number
    return result


def _generation_diagnostics(generation: Mapping[str, object]) -> tuple[bool, bool]:
    state = str(generation.get("state", "")).strip().casefold()
    finish_reason = str(generation.get("finish_reason", "")).strip().casefold()
    truncated = state in {"length", "max_length", "truncated"} or finish_reason in {
        "length",
        "max_length",
        "max_tokens",
    }
    malformed = any(
        value is not None and not isinstance(value, str)
        for value in (
            generation.get("model"),
            generation.get("worker_id"),
            generation.get("worker_name"),
        )
    )
    return truncated, malformed


def parse_interrogation_status(
    payload: object,
    *,
    requested_forms: tuple[str, ...],
) -> HordeInterrogation:
    if not isinstance(payload, dict):
        raise HordeClientError("AI Horde Alchemist returned an unexpected payload")
    state = str(payload.get("state", "")).strip().casefold()
    if state != "done":
        raise HordeClientError(
            f"AI Horde Alchemist finished in unexpected state {state or 'unknown'}"
        )
    raw_forms = payload.get("forms")
    if not isinstance(raw_forms, list):
        raise HordeClientError("AI Horde Alchemist returned no form results")
    wanted = {form.casefold() for form in requested_forms}
    values: dict[str, str] = {}
    worker_id = ""
    worker_name = ""
    for item in raw_forms:
        if not isinstance(item, dict):
            continue
        form = str(item.get("form", "")).strip().casefold()
        if form not in wanted or str(item.get("state", "")).casefold() != "done":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        value = result.get(form)
        if isinstance(value, str) and value.strip():
            values[form] = value.strip()[:4000]
        if not worker_id and isinstance(item.get("worker_id"), str):
            worker_id = str(item["worker_id"])[:120]
        if not worker_name and isinstance(item.get("worker_name"), str):
            worker_name = str(item["worker_name"])[:160]
    caption = values.get("caption", "")
    if "caption" in wanted and not caption:
        raise HordeClientError("AI Horde Alchemist returned no usable caption")
    return HordeInterrogation(
        caption=caption,
        interrogation=values.get("interrogation", ""),
        worker_id=worker_id,
        worker_name=worker_name,
        latency_seconds=0.0,
    )


class HordeClient:
    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str,
        poll_seconds: float,
        timeout_seconds: int,
        client_agent: str,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.timeout_seconds = max(15, int(timeout_seconds))
        self.client_agent = client_agent

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "apikey": self.api_key,
            "Client-Agent": self.client_agent,
        }

    async def _get_list(self, url: str) -> list[object]:
        try:
            async with self.session.get(url, headers=self._headers) as response:
                try:
                    payload = await _read_json_value(response)
                except HordeClientError as exc:
                    if response.status == 429 or response.status >= 500:
                        raise HordeTransportError(
                            f"AI Horde metadata HTTP error {response.status}"
                        ) from exc
                    raise
                if response.status != 200:
                    code, message = _error_details(payload, response.status)
                    if response.status == 429 or response.status >= 500:
                        raise HordeTransportError(message or code)
                    raise HordeClientError(f"{code}: {message}".strip(": "))
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HordeTransportError("Could not reach AI Horde metadata endpoints") from exc
        if not isinstance(payload, list):
            raise HordeClientError("AI Horde metadata returned an unexpected payload")
        return payload

    async def fetch_live_models(self) -> list[object]:
        return await self._get_list(
            f"{self.base_url}/status/models?type=text&model_state=known"
        )

    async def fetch_workers(self) -> list[object]:
        return await self._get_list(f"{self.base_url}/workers?type=text")

    async def fetch_reference_csv(self) -> str:
        headers = {
            "User-Agent": self.client_agent,
            "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1",
        }
        try:
            async with self.session.get(_MODEL_REFERENCE_URL, headers=headers) as response:
                body = await _read_bounded_body(
                    response,
                    limit=_MAX_REFERENCE_BYTES,
                    label="AI Horde model reference",
                )
                if response.status != 200:
                    if response.status == 429 or response.status >= 500:
                        raise HordeTransportError(
                            f"AI Horde model reference HTTP error {response.status}"
                        )
                    raise HordeClientError(
                        f"AI Horde model reference HTTP error {response.status}"
                    )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HordeTransportError("Could not fetch AI Horde model reference") from exc
        try:
            return body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HordeClientError("AI Horde model reference was not UTF-8") from exc

    async def _cancel(self, request_id: str) -> None:
        try:
            async with self.session.delete(
                f"{self.base_url}/generate/text/status/{request_id}",
                headers=self._headers,
            ):
                return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return

    async def _cancel_bounded(self, request_id: str) -> None:
        task = asyncio.create_task(self._cancel(request_id))
        try:
            done, _ = await asyncio.wait((task,), timeout=_CANCEL_GRACE_SECONDS)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_consume_cancel_task)
            raise
        if task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                return
            return
        task.cancel()
        task.add_done_callback(_consume_cancel_task)

    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        stop_sequences: tuple[str, ...],
        max_tokens: int,
        context_tokens: int,
        temperature: float,
        trusted_workers: bool,
        recommended_settings: Mapping[str, object],
    ) -> HordeGeneration:
        params: dict[str, Any] = {
            "max_length": max(16, min(4096, int(max_tokens))),
            "max_context_length": max(80, int(context_tokens)),
            "temperature": max(0.0, min(5.0, float(temperature))),
            "top_p": 0.92,
            "top_k": 0,
            "rep_pen": 1.08,
            "rep_pen_range": 512,
            # Preserve EOS and the model's natural vocabulary. Kobold backends
            # otherwise may apply their built-in bad-token ID list.
            "use_default_badwordsids": False,
            "stop_sequence": [item[:80] for item in stop_sequences[:10] if item],
        }
        params.update(_recommended_params(recommended_settings))
        payload: dict[str, Any] = {
            "prompt": prompt,
            "params": params,
            "trusted_workers": bool(trusted_workers),
            "validated_backends": True,
            "models": [model],
        }
        started = time.monotonic()
        request_id = ""
        try:
            try:
                async with self.session.post(
                    f"{self.base_url}/generate/text/async",
                    headers=self._headers,
                    json=payload,
                ) as response:
                    try:
                        submitted = await _read_json_value(response)
                    except HordeClientError as exc:
                        if response.status == 429 or response.status >= 500:
                            raise HordeTransportError(
                                f"AI Horde HTTP error {response.status}"
                            ) from exc
                        raise
                    if response.status != 202:
                        code, message = _error_details(submitted, response.status)
                        if code in _NO_WORKER_CODES:
                            raise HordeNoWorkerError(message or code, error_code=code)
                        if response.status == 429 or response.status >= 500:
                            raise HordeTransportError(message or code or "AI Horde is temporarily unavailable")
                        raise HordeClientError(f"{code}: {message}".strip(": "))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise HordeTransportError("Could not submit generation to AI Horde") from exc

            if not isinstance(submitted, dict):
                raise HordeClientError("AI Horde submit returned an unexpected payload")
            raw_id = submitted.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                raise HordeClientError("AI Horde returned no request id")
            request_id = raw_id

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(self.poll_seconds)
                try:
                    async with self.session.get(
                        f"{self.base_url}/generate/text/status/{request_id}",
                        headers=self._headers,
                    ) as response:
                        if response.status == 429 or response.status >= 500:
                            continue
                        status = await _read_json_value(response)
                        if response.status != 200:
                            code, message = _error_details(status, response.status)
                            if code in _NO_WORKER_CODES:
                                accepted_id = request_id
                                request_id = ""
                                await self._cancel_bounded(accepted_id)
                                raise HordeNoWorkerError(message or code, error_code=code)
                            raise HordeClientError(f"{code}: {message}".strip(": "))
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue

                if not isinstance(status, dict):
                    raise HordeClientError("AI Horde status returned an unexpected payload")
                if status.get("is_possible") is False:
                    accepted_id = request_id
                    request_id = ""
                    await self._cancel_bounded(accepted_id)
                    raise HordeNoWorkerError(
                        "No compatible worker can fulfil this request",
                        error_code="NoValidWorkers",
                    )
                if status.get("faulted"):
                    accepted_id = request_id
                    request_id = ""
                    await self._cancel_bounded(accepted_id)
                    raise HordeTransportError("AI Horde generation faulted")
                if not status.get("done"):
                    continue
                generations = status.get("generations")
                if not isinstance(generations, list) or not generations:
                    raise HordeOutputError(
                        "AI Horde returned no generation", empty=True, malformed=True
                    )
                first = generations[0] if isinstance(generations[0], dict) else {}
                text = first.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise HordeOutputError(
                        "AI Horde returned an empty generation",
                        empty=True,
                        malformed=not isinstance(text, str),
                    )
                actual_model = first.get("model")
                worker_id = first.get("worker_id")
                worker_name = first.get("worker_name")
                truncated, malformed = _generation_diagnostics(first)
                return HordeGeneration(
                    text=text.strip(),
                    model=(
                        actual_model.strip()
                        if isinstance(actual_model, str) and actual_model.strip()
                        else model
                    ),
                    worker_id=worker_id if isinstance(worker_id, str) else "",
                    worker_name=worker_name if isinstance(worker_name, str) else "",
                    latency_seconds=max(0.0, time.monotonic() - started),
                    truncated=truncated,
                    malformed=malformed,
                )

            accepted_id = request_id
            request_id = ""
            await self._cancel_bounded(accepted_id)
            raise HordeTimeoutError("AI Horde generation timed out")
        except asyncio.CancelledError:
            if request_id:
                accepted_id = request_id
                request_id = ""
                try:
                    await asyncio.shield(self._cancel_bounded(accepted_id))
                except asyncio.CancelledError:
                    pass
            raise

    async def interrogate_image(
        self,
        image: bytes,
        *,
        forms: tuple[str, ...] = ("caption",),
        trusted_workers: bool = True,
    ) -> HordeInterrogation:
        clean_forms = tuple(
            dict.fromkeys(
                form.strip().casefold()
                for form in forms
                if form.strip().casefold() in {"caption", "interrogation"}
            )
        )
        if "caption" not in clean_forms:
            clean_forms = ("caption", *clean_forms)
        if not image:
            raise HordeClientError("Cannot submit an empty image to AI Horde Alchemist")
        payload = {
            "forms": [{"name": form} for form in clean_forms],
            "source_image": base64.b64encode(image).decode("ascii"),
            "trusted_workers": bool(trusted_workers),
            "slow_workers": True,
        }
        started = time.monotonic()
        request_id = ""
        try:
            try:
                async with self.session.post(
                    f"{self.base_url}/interrogate/async",
                    headers=self._headers,
                    json=payload,
                ) as response:
                    submitted = await _read_json_value(response)
                    if response.status != 202:
                        code, message = _error_details(submitted, response.status)
                        raise HordeClientError(f"{code}: {message}".strip(": "))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise HordeClientError("Could not submit image to AI Horde Alchemist") from exc
            if not isinstance(submitted, dict):
                raise HordeClientError("AI Horde Alchemist submit returned an unexpected payload")
            raw_id = submitted.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                raise HordeClientError("AI Horde Alchemist returned no request id")
            request_id = raw_id

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(self.poll_seconds)
                try:
                    async with self.session.get(
                        f"{self.base_url}/interrogate/status/{request_id}",
                        headers=self._headers,
                    ) as response:
                        if response.status == 429 or response.status >= 500:
                            continue
                        status = await _read_json_value(response)
                        if response.status != 200:
                            code, message = _error_details(status, response.status)
                            raise HordeClientError(f"{code}: {message}".strip(": "))
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
                if not isinstance(status, dict):
                    raise HordeClientError(
                        "AI Horde Alchemist status returned an unexpected payload"
                    )
                state = str(status.get("state", "")).strip().casefold()
                if state == "done":
                    parsed = parse_interrogation_status(
                        status,
                        requested_forms=clean_forms,
                    )
                    return HordeInterrogation(
                        caption=parsed.caption,
                        interrogation=parsed.interrogation,
                        worker_id=parsed.worker_id,
                        worker_name=parsed.worker_name,
                        latency_seconds=max(0.0, time.monotonic() - started),
                    )
                if state in {"faulted", "cancelled", "expired"}:
                    raise HordeClientError(
                        f"AI Horde Alchemist request ended in state {state}"
                    )
            accepted_id = request_id
            request_id = ""
            await self._cancel_interrogation_bounded(accepted_id)
            raise HordeClientError("AI Horde Alchemist request timed out")
        except asyncio.CancelledError:
            if request_id:
                try:
                    await asyncio.shield(self._cancel_interrogation_bounded(request_id))
                except asyncio.CancelledError:
                    pass
            raise

    async def _cancel_interrogation(self, request_id: str) -> None:
        try:
            async with self.session.delete(
                f"{self.base_url}/interrogate/status/{request_id}",
                headers=self._headers,
            ):
                return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return

    async def _cancel_interrogation_bounded(self, request_id: str) -> None:
        task = asyncio.create_task(self._cancel_interrogation(request_id))
        try:
            done, _ = await asyncio.wait((task,), timeout=_CANCEL_GRACE_SECONDS)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_consume_cancel_task)
            raise
        if task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                return
            return
        task.cancel()
        task.add_done_callback(_consume_cancel_task)
