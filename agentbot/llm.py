from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Callable, TYPE_CHECKING

import aiohttp

from . import CLIENT_AGENT
from .horde_client import (
    HordeClient,
    HordeClientError,
    HordeGeneration,
    HordeNoWorkerError,
    HordeOutputError,
    HordeRetryableError,
    HordeTimeoutError,
    HordeTransportError,
)
from .horde_router import (
    HordeRouter,
    ModelSelection,
    NoEligibleHordeModel,
    parse_live_models,
    parse_reference_csv,
)
from .prompt_formats import PromptTurn, format_prompt
from .settings import Settings

if TYPE_CHECKING:
    from .memory import MemoryStore

logger = logging.getLogger(__name__)


def _consume_detached_task(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Detached provider cleanup failed", exc_info=True)


class ProviderError(RuntimeError):
    """A recoverable remote inference failure."""


class _HordeValidationError(HordeClientError):
    """A Horde generation that failed a caller-supplied structural check."""

    def __init__(self, message: str, generation: HordeGeneration) -> None:
        super().__init__(message)
        self.generation = generation


class LLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    async def generate_validated(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        validator: Callable[[str], bool],
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
    ) -> str:
        result = await self.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            history=history,
            post_history=post_history,
            scope=scope,
            task=task,
            context_tokens=context_tokens,
        )
        if not validator(result):
            raise ProviderError("Provider returned invalid structured output")
        return result

    @abstractmethod
    def status(self) -> dict[str, object]:
        raise NotImplementedError


class HordeProvider(LLMProvider):
    def __init__(
        self,
        *,
        client: HordeClient,
        router: HordeRouter,
        trusted_workers: bool,
        outcome_recorder: Callable[..., object] | None = None,
        interactive_timeout_seconds: float = 45.0,
        total_chat_timeout_seconds: float = 120.0,
    ) -> None:
        self.client = client
        self.router = router
        self.trusted_workers = bool(trusted_workers)
        self.outcome_recorder = outcome_recorder
        self.interactive_timeout_seconds = max(
            0.01, min(600.0, float(interactive_timeout_seconds))
        )
        self.total_chat_timeout_seconds = max(
            0.01, min(600.0, float(total_chat_timeout_seconds))
        )
        self._refresh_lock = asyncio.Lock()
        self._references = ()
        self._reference_refreshed_at = 0.0
        self._reference_ttl_seconds = 21_600.0
        self._max_stale_reference_seconds = 86_400.0
        self._has_metadata = False
        self._metadata_refreshed_at = 0.0
        self._max_stale_metadata_seconds = max(
            120.0,
            min(600.0, float(self.router.metadata_ttl_seconds) * 4.0),
        )
        self._stale_metadata_uses = 0

    async def _refresh_metadata(self, *, force: bool = False) -> None:
        if not self.router.needs_refresh(force=force):
            return
        async with self._refresh_lock:
            if not force and not self.router.needs_refresh():
                return
            try:
                live_rows = await self.client.fetch_live_models()
            except HordeTransportError:
                metadata_age = time.monotonic() - self._metadata_refreshed_at
                if (
                    not self._has_metadata
                    or metadata_age > self._max_stale_metadata_seconds
                ):
                    raise
                self._stale_metadata_uses += 1
                logger.warning("Could not refresh AI Horde metadata; using bounded stale metadata")
                return
            now = time.monotonic()
            if (
                not self._references
                or now - self._reference_refreshed_at >= self._reference_ttl_seconds
            ):
                try:
                    references = parse_reference_csv(
                        await self.client.fetch_reference_csv()
                    )
                    if not references:
                        raise HordeClientError(
                            "AI Horde model reference contained no usable rows"
                        )
                except HordeTransportError:
                    reference_age = now - self._reference_refreshed_at
                    if (
                        not self._references
                        or reference_age > self._max_stale_reference_seconds
                    ):
                        raise
                    logger.warning(
                        "Could not refresh AI Horde model reference; using bounded cached metadata"
                    )
                else:
                    self._references = tuple(references)
                    self._reference_refreshed_at = now
            self.router.update_metadata(
                parse_live_models(live_rows),
                self._references,
            )
            self._has_metadata = True
            self._metadata_refreshed_at = now

    def _record_outcome(
        self,
        *,
        selected_model: str,
        task: str,
        success: bool,
        latency_seconds: float,
        error_kind: str = "",
        generation: HordeGeneration | None = None,
        empty: bool = False,
        malformed: bool = False,
        truncated: bool = False,
    ) -> None:
        actual_model = generation.model if generation is not None else selected_model
        worker_id = generation.worker_id if generation is not None else ""
        worker_name = generation.worker_name if generation is not None else ""
        actual_latency = (
            generation.latency_seconds if generation is not None else latency_seconds
        )
        actual_malformed = malformed or bool(
            generation is not None and generation.malformed
        )
        actual_truncated = truncated or bool(
            generation is not None and generation.truncated
        )
        self.router.record_outcome(
            model=selected_model,
            task=task,
            success=success,
            latency_seconds=actual_latency,
            error_kind=error_kind,
            worker_id=worker_id,
            empty=empty,
            malformed=actual_malformed,
            truncated=actual_truncated,
        )
        if self.outcome_recorder is None:
            return
        try:
            self.outcome_recorder(
                model=actual_model,
                task=task,
                worker_id=worker_id,
                worker_name=worker_name,
                success=success,
                latency_seconds=actual_latency,
                error_kind=error_kind,
                empty=empty,
                malformed=actual_malformed,
                truncated=actual_truncated,
            )
        except Exception:
            logger.exception("Could not persist AI Horde model outcome")

    async def _generate_once(
        self,
        *,
        selection: ModelSelection,
        system_prompt: str,
        user_prompt: str,
        history: tuple[PromptTurn, ...],
        post_history: str,
        max_tokens: int,
        context_tokens: int,
        temperature: float,
        task: str,
        validator: Callable[[str], bool] | None = None,
    ) -> HordeGeneration:
        formatted = format_prompt(
            selection.instruction_format,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history=history,
            post_history=post_history,
        )
        started = time.monotonic()
        try:
            request = self.client.generate(
                model=selection.model,
                prompt=formatted.prompt,
                stop_sequences=formatted.stop_sequences,
                max_tokens=max_tokens,
                context_tokens=context_tokens,
                temperature=temperature,
                trusted_workers=self.trusted_workers,
                recommended_settings=selection.settings,
            )
            if task == "chat":
                generation = await asyncio.wait_for(
                    request,
                    timeout=self.interactive_timeout_seconds,
                )
            else:
                generation = await request
        except asyncio.TimeoutError as exc:
            self._record_outcome(
                selected_model=selection.model,
                task=task,
                success=False,
                latency_seconds=time.monotonic() - started,
                error_kind="timeout",
            )
            raise HordeTimeoutError("AI Horde interactive generation timed out") from exc
        except HordeNoWorkerError:
            self._record_outcome(
                selected_model=selection.model,
                task=task,
                success=False,
                latency_seconds=time.monotonic() - started,
                error_kind="no_worker",
            )
            raise
        except HordeOutputError as exc:
            self._record_outcome(
                selected_model=selection.model,
                task=task,
                success=False,
                latency_seconds=time.monotonic() - started,
                error_kind="output",
                empty=exc.empty,
                malformed=exc.malformed,
            )
            raise
        except HordeTimeoutError:
            self._record_outcome(
                selected_model=selection.model,
                task=task,
                success=False,
                latency_seconds=time.monotonic() - started,
                error_kind="timeout",
            )
            raise
        except HordeTransportError:
            self._record_outcome(
                selected_model=selection.model,
                task=task,
                success=False,
                latency_seconds=time.monotonic() - started,
                error_kind="transport",
            )
            raise
        except HordeClientError:
            # Authentication, request configuration, and protocol/schema errors
            # are not evidence that this particular model route is unhealthy.
            raise
        if validator is not None and not validator(generation.text):
            self._record_outcome(
                selected_model=selection.model,
                task=task,
                success=False,
                latency_seconds=generation.latency_seconds,
                error_kind="malformed",
                generation=generation,
                malformed=True,
            )
            raise _HordeValidationError(
                "Provider returned invalid structured output",
                generation,
            )
        self._record_outcome(
            selected_model=selection.model,
            task=task,
            success=True,
            latency_seconds=generation.latency_seconds,
            generation=generation,
        )
        return generation

    async def _generate_routed_inner(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
        validator: Callable[[str], bool] | None = None,
        attempted_models: list[str] | None = None,
    ) -> str:
        effective_context = 8192 if context_tokens is None else max(1, int(context_tokens))
        clean_scope = str(scope)[:160] or "global"
        clean_task = str(task)[:32] or "chat"
        max_wait_seconds = (
            self.interactive_timeout_seconds if clean_task == "chat" else None
        )
        try:
            await self._refresh_metadata()
            selection = self.router.select(
                scope=clean_scope,
                task=clean_task,
                context_tokens=effective_context,
                max_tokens=max_tokens,
                max_wait_seconds=max_wait_seconds,
            )
            try:
                if attempted_models is not None:
                    attempted_models.append(selection.model)
                generation = await self._generate_once(
                    selection=selection,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    history=history,
                    post_history=post_history,
                    max_tokens=max_tokens,
                    context_tokens=effective_context,
                    temperature=temperature,
                    task=clean_task,
                    validator=validator,
                )
            except (HordeRetryableError, _HordeValidationError):
                self.router.clear_selection(scope=clean_scope, task=clean_task)
                await self._refresh_metadata(force=True)
                retry_selection = self.router.select(
                    scope=clean_scope,
                    task=clean_task,
                    context_tokens=effective_context,
                    max_tokens=max_tokens,
                    excluded_models=(selection.model,),
                    max_wait_seconds=max_wait_seconds,
                )
                try:
                    if attempted_models is not None:
                        attempted_models.append(retry_selection.model)
                    generation = await self._generate_once(
                        selection=retry_selection,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        history=history,
                        post_history=post_history,
                        max_tokens=max_tokens,
                        context_tokens=effective_context,
                        temperature=temperature,
                        task=clean_task,
                        validator=validator,
                    )
                except _HordeValidationError:
                    self.router.clear_selection(scope=clean_scope, task=clean_task)
                    raise
                except HordeRetryableError:
                    self.router.clear_selection(scope=clean_scope, task=clean_task)
                    raise
            return generation.text
        except asyncio.CancelledError:
            raise
        except (HordeClientError, NoEligibleHordeModel) as exc:
            raise ProviderError(str(exc)) from exc

    async def _generate_routed(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
        validator: Callable[[str], bool] | None = None,
    ) -> str:
        attempted_models: list[str] = []
        request = self._generate_routed_inner(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            history=history,
            post_history=post_history,
            scope=scope,
            task=task,
            context_tokens=context_tokens,
            validator=validator,
            attempted_models=attempted_models,
        )
        clean_task = str(task)[:32] or "chat"
        if clean_task != "chat":
            return await request
        started = time.monotonic()
        generation_task = asyncio.create_task(request)
        try:
            done, _ = await asyncio.wait(
                (generation_task,),
                timeout=self.total_chat_timeout_seconds,
            )
        except asyncio.CancelledError:
            generation_task.cancel()
            generation_task.add_done_callback(_consume_detached_task)
            raise
        if generation_task in done:
            return generation_task.result()
        clean_scope = str(scope)[:160] or "global"
        selected_model = attempted_models[-1] if attempted_models else ""
        if selected_model:
            self._record_outcome(
                selected_model=selected_model,
                task=clean_task,
                success=False,
                latency_seconds=time.monotonic() - started,
                error_kind="timeout",
            )
        self.router.clear_selection(scope=clean_scope, task=clean_task)
        generation_task.cancel()
        generation_task.add_done_callback(_consume_detached_task)
        raise ProviderError("AI Horde chat deadline expired")

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
    ) -> str:
        return await self._generate_routed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            history=history,
            post_history=post_history,
            scope=scope,
            task=task,
            context_tokens=context_tokens,
        )

    async def generate_validated(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        validator: Callable[[str], bool],
        history: tuple[PromptTurn, ...] = (),
        post_history: str = "",
        scope: str = "global",
        task: str = "chat",
        context_tokens: int | None = None,
    ) -> str:
        return await self._generate_routed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            history=history,
            post_history=post_history,
            scope=scope,
            task=task,
            context_tokens=context_tokens,
            validator=validator,
        )

    def status(self) -> dict[str, object]:
        data: dict[str, object] = {
            "provider": "horde",
            "routing": "adaptive",
            "base_url": getattr(self.client, "base_url", ""),
            "interactive_timeout_seconds": self.interactive_timeout_seconds,
            "total_chat_timeout_seconds": self.total_chat_timeout_seconds,
            "max_stale_metadata_seconds": self._max_stale_metadata_seconds,
            "max_stale_reference_seconds": self._max_stale_reference_seconds,
            "stale_metadata_uses": self._stale_metadata_uses,
        }
        data.update(self.router.status())
        return data


def build_provider(
    settings: Settings,
    session: aiohttp.ClientSession,
    memory: "MemoryStore | None" = None,
) -> LLMProvider:
    client = HordeClient(
        session=session,
        api_key=settings.horde_api_key,
        base_url=settings.horde_base_url,
        poll_seconds=settings.horde_poll_seconds,
        timeout_seconds=settings.horde_timeout_seconds,
        client_agent=CLIENT_AGENT,
    )
    router = HordeRouter(
        metadata_ttl_seconds=settings.horde_router_metadata_ttl_seconds,
        sticky_seconds=settings.horde_router_sticky_seconds,
        min_parameters_bn=settings.horde_min_model_parameters_bn,
        max_selections=settings.horde_router_max_scopes,
        max_metrics=1,
        trusted_workers=settings.horde_trusted_workers,
    )
    return HordeProvider(
        client=client,
        router=router,
        trusted_workers=settings.horde_trusted_workers,
        outcome_recorder=(memory.record_model_outcome if memory is not None else None),
        total_chat_timeout_seconds=settings.horde_timeout_seconds,
    )
