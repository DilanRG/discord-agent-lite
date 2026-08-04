from __future__ import annotations

import asyncio
import mimetypes
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable, Sequence

import aiohttp
import discord

from . import CLIENT_AGENT
from .app import AgentBot, create_memory_store
from .attachments import (
    AttachmentLimits,
    AttachmentProcessor,
    ProcessedAttachment,
)
from .character import load_character
from .horde_client import HordeClient
from .llm import LLMProvider, ProviderError, build_provider
from .orchestrator import AgentCore, ReplyRequest
from .prompt_formats import PromptTurn
from .policy import discord_output_style_issues
from .settings import Settings


@dataclass(frozen=True, slots=True)
class SimulatedAttachment:
    path: Path
    filename: str = ""
    content_type: str = ""
    declared_size: int | None = None

    def resolved_filename(self) -> str:
        return (self.filename or self.path.name)[:180]

    def resolved_content_type(self) -> str:
        if self.content_type:
            return self.content_type
        guessed, _ = mimetypes.guess_type(self.resolved_filename())
        return guessed or "application/octet-stream"


@dataclass(frozen=True, slots=True)
class SimulatedReply:
    message_id: int
    content: str
    attachments: tuple[ProcessedAttachment, ...]
    delivery: str
    generated: bool
    elapsed_seconds: float
    provider_status: dict[str, object]
    style_issues: tuple[str, ...]


class ScriptedProvider(LLMProvider):
    """Deterministic provider for regression scenarios and hostile-output tests."""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "history": history,
                "post_history": post_history,
                "scope": scope,
                "task": task,
                "context_tokens": context_tokens,
            }
        )
        if not self._responses:
            raise ProviderError("The scripted simulation provider has no response left")
        return self._responses.pop(0)

    def status(self) -> dict[str, object]:
        return {"provider": "scripted", "responses_remaining": len(self._responses)}


class _RecordingAgentCore(AgentCore):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.requests: list[ReplyRequest] = []
        self.responses: list[str] = []

    async def reply(self, request: ReplyRequest) -> str:
        self.requests.append(request)
        response = await super().reply(request)
        self.responses.append(response)
        return response


@dataclass(slots=True)
class _SimulatedUser:
    id: int
    display_name: str
    bot: bool
    name: str = ""
    global_name: str | None = None


@dataclass(slots=True)
class _SimulatedGuild:
    id: int


@dataclass(slots=True)
class _SimulatedRoleTags:
    bot_id: int | None = None


@dataclass(slots=True)
class _SimulatedRole:
    id: int
    tags: _SimulatedRoleTags


@dataclass(slots=True)
class _SimulatedReference:
    message_id: int
    resolved: object | None
    cached_message: object | None
    channel_id: int
    type: discord.MessageReferenceType = discord.MessageReferenceType.default


@dataclass(slots=True)
class _SimulatedDiscordAttachment:
    id: int
    url: str
    filename: str
    content_type: str
    size: int


class _Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class _ByteStream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.data), max(1, size)):
            yield self.data[offset : offset + size]


class _DownloadResponse:
    def __init__(self, data: bytes) -> None:
        self.status = 200
        self.content_length = len(data)
        self.content = _ByteStream(data)

    async def __aenter__(self) -> "_DownloadResponse":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class _DownloadSession:
    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}
        self.closed = False

    def register(self, url: str, data: bytes) -> None:
        self._payloads[url] = data

    def get(
        self,
        url: str,
        *,
        allow_redirects: bool,
        timeout: object,
    ) -> _DownloadResponse:
        del timeout
        if allow_redirects:
            raise AssertionError("The production attachment path must disable redirects")
        if url not in self._payloads:
            raise aiohttp.ClientError("simulation attachment URL was not registered")
        return _DownloadResponse(self._payloads[url])

    async def close(self) -> None:
        self.closed = True
        self._payloads.clear()


class _FailedHTTPResponse:
    status = 403
    reason = "simulated delivery failure"


class _SimulatedChannel:
    def __init__(self, channel_id: int, bot_user: _SimulatedUser) -> None:
        self.id = channel_id
        self._bot_user = bot_user
        self._next_id = 10_000
        self.messages: dict[int, _SimulatedMessage] = {}
        self.remote_messages: dict[int, _SimulatedMessage] = {}
        self.fetch_requests: list[int] = []
        self.outbound: list[tuple[str, _SimulatedMessage]] = []
        self.fail_message_reply = False
        self.fail_channel_send = False

    def typing(self) -> _Typing:
        return _Typing()

    def add(self, message: "_SimulatedMessage") -> None:
        self.messages[message.id] = message
        self.remote_messages[message.id] = message

    def evict_cached_message(self, message_id: int) -> None:
        self.messages.pop(message_id, None)

    async def fetch_message(self, message_id: int) -> "_SimulatedMessage":
        self.fetch_requests.append(message_id)
        return self.remote_messages[message_id]

    async def send(self, content: str, **kwargs: object) -> "_SimulatedMessage":
        del kwargs
        if self.fail_channel_send:
            raise discord.HTTPException(_FailedHTTPResponse(), "simulated channel.send failure")  # type: ignore[arg-type]
        return self._emit(content, "channel.send")

    def _emit(self, content: str, delivery: str) -> "_SimulatedMessage":
        self._next_id += 1
        message = _SimulatedMessage(
            message_id=self._next_id,
            content=content,
            author=self._bot_user,
            channel=self,
            guild=_SimulatedGuild(1),
            attachments=(),
            mentions=(),
            reference=None,
        )
        self.add(message)
        self.outbound.append((delivery, message))
        return message


class _SimulatedMessage:
    def __init__(
        self,
        *,
        message_id: int,
        content: str,
        author: _SimulatedUser,
        channel: _SimulatedChannel,
        guild: _SimulatedGuild | None,
        attachments: Iterable[_SimulatedDiscordAttachment],
        mentions: Iterable[_SimulatedUser],
        reference: _SimulatedReference | None,
        webhook_id: int | None = None,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.attachments = list(attachments)
        self.mentions = list(mentions)
        self.role_mentions: list[object] = []
        self.reference = reference
        self.webhook_id = webhook_id
        self.created_at = datetime.now(timezone.utc)

    async def reply(self, content: str, **kwargs: object) -> "_SimulatedMessage":
        del kwargs
        if self.channel.fail_message_reply:
            raise discord.HTTPException(_FailedHTTPResponse(), "simulated message.reply failure")  # type: ignore[arg-type]
        return self.channel._emit(content, "message.reply")


class _SimulationAgentBot(AgentBot):
    def _reply_message(self, message: object) -> object | None:
        reference = getattr(message, "reference", None)
        if reference is None:
            return None
        return getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)


def _style_issues(
    *,
    current_message: str,
    reply: str,
    previous_reply: str,
    grounding_text: str = "",
) -> tuple[str, ...]:
    return discord_output_style_issues(
        current_message=current_message,
        reply=reply,
        previous_reply=previous_reply,
        grounding_text=grounding_text,
    )


class DiscordTurnSimulator:
    """Run fake Discord messages through AgentBot.on_message without Discord I/O."""

    def __init__(
        self,
        *,
        bot: _SimulationAgentBot,
        core: _RecordingAgentCore,
        download_session: _DownloadSession,
        provider_session: aiohttp.ClientSession | None,
        channel: _SimulatedChannel,
        human_user: _SimulatedUser,
        peer_bot_user: _SimulatedUser,
        guild: _SimulatedGuild | None,
    ) -> None:
        self.bot = bot
        self.core = core
        self.download_session = download_session
        self.provider_session = provider_session
        self.channel = channel
        self.human_user = human_user
        self.peer_bot_user = peer_bot_user
        self.guild = guild
        self._next_inbound_id = 1_000
        self._next_attachment_id = 20_000
        self._previous_reply = ""
        self._closed = False

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        provider: LLMProvider | None = None,
        enforce_rate_limits: bool = False,
        bot_user_id: int = 999_001,
        human_user_id: int = 777_001,
        guild_id: int = 1,
        channel_id: int = 10,
    ) -> "DiscordTurnSimulator":
        if not enforce_rate_limits:
            # Scenario turns run back-to-back instead of at human Discord speed.
            settings = replace(
                settings,
                user_rate_requests=max(settings.user_rate_requests, 30),
                channel_rate_requests=max(settings.channel_rate_requests, 100),
            )
        character = load_character(settings.character_file)
        memory = create_memory_store(settings)
        bot = _SimulationAgentBot(settings, character, memory)
        bot_user = _SimulatedUser(
            bot_user_id,
            character.name,
            True,
            character.name,
            character.name,
        )
        human_user = _SimulatedUser(
            human_user_id,
            "simulated-user",
            False,
            "simulated-user",
            "Simulated User",
        )
        peer_bot_user = _SimulatedUser(
            bot_user_id + 1,
            "simulated-peer-bot",
            True,
            "simulated-peer-bot",
            "Simulated Peer Bot",
        )
        bot._connection.user = bot_user  # The simulator never logs in to Discord.
        channel = _SimulatedChannel(channel_id, bot_user)
        guild = _SimulatedGuild(guild_id) if guild_id else None
        download_session = _DownloadSession()
        provider_session: aiohttp.ClientSession | None = None

        if provider is None:
            connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=30)
            provider_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
            provider = build_provider(settings, provider_session, memory)
            if settings.alchemist_enabled:
                bot.alchemist_client = HordeClient(
                    session=provider_session,
                    api_key=settings.alchemist_api_key,
                    base_url=settings.horde_base_url,
                    poll_seconds=settings.horde_poll_seconds,
                    timeout_seconds=settings.horde_timeout_seconds,
                    client_agent=CLIENT_AGENT,
                )

        bot.session = download_session  # type: ignore[assignment]
        bot.provider = provider
        bot.attachment_processor = AttachmentProcessor(
            memory=memory,
            limits=AttachmentLimits(
                max_bytes=settings.max_attachment_bytes,
                max_extracted_chars=min(
                    settings.attachment_max_extracted_chars,
                    settings.max_attachment_chars,
                ),
                max_pages=32,
                max_archive_entries=128,
                max_archive_uncompressed_bytes=16_777_216,
                max_pixels=settings.attachment_max_pixels,
                timeout_seconds=settings.attachment_timeout_seconds,
            ),
            # Cache/chunk retrieval is absent from the lean attachment path.
            max_cache_entries=1,
            max_chunks_per_attachment=1,
            chunk_chars=1,
            chunk_overlap=0,
            prompt_chars=settings.max_attachment_chars,
            concurrency=settings.attachment_concurrency,
            # The simulator deliberately does not depend on the host POSIX gate.
            document_lock_path=None,
            image_analyzer=bot._analyze_image if bot.alchemist_client is not None else None,
        )
        core = _RecordingAgentCore(
            settings=settings,
            character=character,
            memory=memory,
            provider=provider,
            generation_semaphore=bot.generation_semaphore,
        )
        bot.core = core
        return cls(
            bot=bot,
            core=core,
            download_session=download_session,
            provider_session=provider_session,
            channel=channel,
            human_user=human_user,
            peer_bot_user=peer_bot_user,
            guild=guild,
        )

    async def send(
        self,
        content: str,
        *,
        attachments: Sequence[SimulatedAttachment] = (),
        reply_to: int | None = None,
        mention_bot: bool = True,
        mention_bot_role: bool = False,
        author_is_bot: bool = False,
        author_is_self: bool = False,
        webhook_id: int | None = None,
        allow_no_delivery: bool = False,
    ) -> SimulatedReply:
        if self._closed:
            raise RuntimeError("The Discord simulator is closed")
        self._next_inbound_id += 1
        resolved = self.channel.messages.get(reply_to) if reply_to is not None else None
        reference = (
            _SimulatedReference(reply_to, resolved, resolved, self.channel.id)
            if reply_to is not None
            else None
        )
        mention = mention_bot and self.guild is not None and not mention_bot_role
        managed_role = (
            _SimulatedRole(
                id=self.bot.user.id + 1_000_000,
                tags=_SimulatedRoleTags(bot_id=self.bot.user.id),
            )
            if mention_bot_role and self.guild is not None
            else None
        )
        # A normal channel mention appears in content. Discord's reply-author
        # toggle instead supplies the replied-to user in the mentions payload
        # without inserting mention text into the reply body.
        message_content = (
            f"<@{self.bot.user.id}> {content}"
            if mention and reference is None
            else f"<@&{managed_role.id}> {content}"
            if managed_role is not None and reference is None
            else content
        )

        discord_attachments: list[_SimulatedDiscordAttachment] = []
        for item in attachments:
            actual_size = item.path.stat().st_size
            reported_size = actual_size if item.declared_size is None else item.declared_size
            data = b""
            if reported_size <= self.bot.settings.max_attachment_bytes:
                with item.path.open("rb") as handle:
                    data = handle.read(self.bot.settings.max_attachment_bytes + 1)
            self._next_attachment_id += 1
            filename = item.resolved_filename()
            url = (
                "https://cdn.discordapp.com/attachments/1/"
                f"{self._next_attachment_id}/{filename}"
            )
            self.download_session.register(url, data)
            discord_attachments.append(
                _SimulatedDiscordAttachment(
                    id=self._next_attachment_id,
                    url=url,
                    filename=filename,
                    content_type=item.resolved_content_type(),
                    size=reported_size,
                )
            )

        author = (
            self.bot.user
            if author_is_self
            else self.peer_bot_user if author_is_bot else self.human_user
        )
        inbound = _SimulatedMessage(
            message_id=self._next_inbound_id,
            content=message_content,
            author=author,
            channel=self.channel,
            guild=self.guild,
            attachments=discord_attachments,
            mentions=(self.bot.user,) if mention else (),
            reference=reference,
            webhook_id=webhook_id,
        )
        if managed_role is not None:
            inbound.role_mentions = [managed_role]
        self.channel.add(inbound)
        outbound_before = len(self.channel.outbound)
        request_before = len(self.core.requests)
        response_before = len(self.core.responses)
        started = time.monotonic()
        await self.bot.on_message(inbound)  # Exercises the real Discord event handler.
        elapsed_seconds = max(0.0, time.monotonic() - started)
        provider_status = self.bot.provider.status() if self.bot.provider is not None else {}
        if len(self.channel.outbound) <= outbound_before:
            if allow_no_delivery:
                return SimulatedReply(
                    message_id=0,
                    content="",
                    attachments=(
                        self.core.requests[-1].attachments
                        if len(self.core.requests) > request_before
                        else ()
                    ),
                    delivery="undelivered",
                    generated=False,
                    elapsed_seconds=elapsed_seconds,
                    provider_status=provider_status,
                    style_issues=(),
                )
            raise RuntimeError("The simulated Discord turn produced no outbound message")
        delivery, outbound = self.channel.outbound[-1]
        request_attachments: tuple[ProcessedAttachment, ...] = ()
        if len(self.core.requests) > request_before:
            request_attachments = self.core.requests[-1].attachments
        issues = _style_issues(
            current_message=content,
            reply=outbound.content,
            previous_reply=self._previous_reply,
            grounding_text="\n".join(item.prompt_text for item in request_attachments),
        )
        generated = (
            len(self.core.responses) > response_before
            and self.core.responses[-1] == outbound.content
        )
        self._previous_reply = outbound.content
        return SimulatedReply(
            message_id=outbound.id,
            content=outbound.content,
            attachments=request_attachments,
            delivery=delivery,
            generated=generated,
            elapsed_seconds=elapsed_seconds,
            provider_status=provider_status,
            style_issues=issues,
        )

    def set_delivery_failures(self, *, reply: bool, fallback: bool) -> None:
        self.channel.fail_message_reply = reply
        self.channel.fail_channel_send = fallback

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.bot.close()
        if self.provider_session is not None and not self.provider_session.closed:
            await self.provider_session.close()
