from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord.ext import commands, tasks

from . import CLIENT_AGENT
from .attachment_evidence import (
    AttachmentEvidence,
    explicit_memory_references_attachment,
    saved_attachment_memory_text,
)
from .attachments import (
    AttachmentError,
    AttachmentLimits,
    AttachmentProcessor,
    AttachmentSource,
    ImageAnalysis,
    ProcessedAttachment,
    attachment_processing_admitted,
)
from .character import Character, CharacterError, load_character
from .horde_client import HordeClient, HordeClientError
from .llm import LLMProvider, ProviderError, build_provider
from .memory import MemoryStore
from .orchestrator import AgentCore, ReplyRequest
from .policy import (
    SlidingWindowLimiter,
    clean_input,
    extract_explicit_memory,
    retry_seconds,
    strip_bot_mentions,
)
from .settings import ConfigError, Settings
from .social import meaningful_social_event

logger = logging.getLogger(__name__)
_EVENT_CLOSE_GRACE_SECONDS = 3.0
_DISCORD_CLOSE_GRACE_SECONDS = 3.0
_CORE_CLOSE_GRACE_SECONDS = 3.5
_SESSION_CLOSE_GRACE_SECONDS = 1.0
_MEMORY_CLOSE_EXPECTED_SECONDS = 3.0
_PROCESS_EXIT_GRACE_SECONDS = 15.0
_QUESTION_STARTS = (
    "who ", "what ", "when ", "where ", "why ", "how ", "which ",
    "can ", "could ", "would ", "should ", "do ", "does ", "did ", "is ", "are ",
)
_EVIDENCE_KINDS = frozenset({"image", "text", "pdf", "docx"})


def _attachment_kind_from_metadata(filename: str, declared_mime: str) -> str:
    suffix = "." + filename.casefold().rsplit(".", 1)[-1] if "." in filename else ""
    mime = declared_mime.partition(";")[0].strip().casefold()
    if suffix in {".png", ".jpg", ".jpeg", ".jpe", ".webp"} or mime.startswith("image/"):
        return "image"
    if suffix == ".pdf" or mime == "application/pdf":
        return "pdf"
    if suffix == ".docx" or "wordprocessingml.document" in mime:
        return "docx"
    return "text"


def _attachment_evidence(
    message: discord.Message,
    processed: tuple[ProcessedAttachment, ...],
    *,
    max_count: int,
) -> tuple[AttachmentEvidence, ...]:
    evidence: list[AttachmentEvidence] = []
    uploads = tuple(message.attachments[:max_count])
    for ordinal, (upload, item) in enumerate(zip(uploads, processed)):
        kind = (
            item.kind
            if item.kind in _EVIDENCE_KINDS
            else _attachment_kind_from_metadata(upload.filename, upload.content_type or "")
        )
        origin = {
            "image": "image_caption",
            "text": "text_extract",
            "pdf": "pdf_extract",
            "docx": "docx_extract",
        }[kind]
        evidence.append(
            AttachmentEvidence(
                attachment_id=str(upload.id),
                ordinal=ordinal,
                filename=item.filename or upload.filename,
                detected_kind=kind,
                status=item.status if item.status in {"ready", "error"} else "error",
                origin=origin,
                text=item.prompt_text if item.status == "ready" else "",
                confidence=item.confidence if item.status == "ready" else None,
                truncated=item.truncated,
                error_code=(item.error.partition(":")[0] or "unavailable")
                if item.status != "ready"
                else "",
            )
        )
    return tuple(evidence)


def _consume_shutdown_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Detached shutdown step failed", exc_info=True)


async def _close_step(
    label: str,
    awaitable: Awaitable[Any],
    timeout_seconds: float,
) -> None:
    task = asyncio.create_task(awaitable, name=f"shutdown:{label}")
    try:
        done, _ = await asyncio.wait(
            (task,),
            timeout=max(0.01, float(timeout_seconds)),
        )
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_shutdown_task)
        raise
    if task not in done:
        logger.warning("Shutdown step %s exceeded its %.1fs grace", label, timeout_seconds)
        task.cancel()
        task.add_done_callback(_consume_shutdown_task)
        return
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("Shutdown step %s was cancelled", label)
    except Exception:
        logger.exception("Shutdown step %s failed", label)
_EXACT_OPERATIONAL_NOTICES = frozenset(
    {
        "The agent is at its request capacity. Try again shortly.",
        "Another request is already running in this channel. Try again shortly.",
        "I couldn't get a usable reply right now. Try again in a moment.",
        "The language-model service is unavailable right now. Try again later.",
        "The agent hit an internal error. The details were written to the bot log.",
    }
)


def _is_operational_notice(text: str) -> bool:
    normalized = " ".join(str(text).split())
    return normalized in _EXACT_OPERATIONAL_NOTICES or (
        normalized.startswith("Rate limit reached. Try again in about ")
        and normalized.endswith(" seconds.")
    )


def _managed_bot_role_mentions(message: discord.Message, bot_user_id: int) -> tuple[int, ...]:
    """Return role mentions managed by this bot's Discord application."""
    role_ids: list[int] = []
    for role in getattr(message, "role_mentions", ()):
        tags = getattr(role, "tags", None)
        if getattr(tags, "bot_id", None) == bot_user_id:
            role_ids.append(int(role.id))
    return tuple(role_ids)


class AgentBot(commands.Bot):
    def __init__(self, settings: Settings, character: Character, memory: MemoryStore) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            description="A lightweight character-agnostic Discord AI agent",
            max_messages=100,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
        )
        self.settings = settings
        self.character = character
        self.memory = memory
        self.session: aiohttp.ClientSession | None = None
        self.provider: LLMProvider | None = None
        self.alchemist_client: HordeClient | None = None
        self.attachment_processor: AttachmentProcessor | None = None
        self.core: AgentCore | None = None
        self.generation_semaphore = asyncio.Semaphore(settings.global_concurrency)
        self.request_slots = asyncio.BoundedSemaphore(settings.max_pending_requests)
        self.user_limiter = SlidingWindowLimiter(max_keys=4096)
        self.channel_limiter = SlidingWindowLimiter(max_keys=2048)
        self.command_limiter = SlidingWindowLimiter(max_keys=4096)
        self.tracking_user_limiter = SlidingWindowLimiter(max_keys=4096)
        self.tracking_channel_limiter = SlidingWindowLimiter(max_keys=2048)
        self.notice_limiter = SlidingWindowLimiter(max_keys=4096)
        self._channel_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
        self._seeded_channels = False
        self._closed_resources = False
        self._shutdown_started = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._active_event_tasks: set[asyncio.Task[Any]] = set()
        self._process_watchdog_enabled = False
        self._shutdown_watchdog_event: threading.Event | None = None
        self._shutdown_watchdog_thread: threading.Thread | None = None
        self._event_close_grace_seconds = _EVENT_CLOSE_GRACE_SECONDS
        self._discord_close_grace_seconds = _DISCORD_CLOSE_GRACE_SECONDS
        self._core_close_grace_seconds = _CORE_CLOSE_GRACE_SECONDS
        self._session_close_grace_seconds = _SESSION_CLOSE_GRACE_SECONDS
        self._process_exit_grace_seconds = _PROCESS_EXIT_GRACE_SECONDS
        if settings.proactive_timezone.casefold() in {"utc", "etc/utc", "gmt"}:
            self.proactive_timezone = timezone.utc
        else:
            try:
                self.proactive_timezone = ZoneInfo(settings.proactive_timezone)
            except ZoneInfoNotFoundError:
                logger.warning("Unknown proactive timezone %s; using UTC", settings.proactive_timezone)
                self.proactive_timezone = timezone.utc

    async def setup_hook(self) -> None:
        connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=30)
        self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        self.provider = build_provider(self.settings, self.session, self.memory)
        if self.settings.alchemist_enabled:
            self.alchemist_client = HordeClient(
                session=self.session,
                api_key=self.settings.alchemist_api_key,
                base_url=self.settings.horde_base_url,
                poll_seconds=self.settings.horde_poll_seconds,
                timeout_seconds=self.settings.horde_timeout_seconds,
                client_agent=CLIENT_AGENT,
            )
        self.attachment_processor = AttachmentProcessor(
            memory=self.memory,
            limits=AttachmentLimits(
                max_bytes=self.settings.max_attachment_bytes,
                max_extracted_chars=min(
                    self.settings.attachment_max_extracted_chars,
                    self.settings.max_attachment_chars,
                ),
                max_pages=32,
                max_archive_entries=128,
                max_archive_uncompressed_bytes=16_777_216,
                max_pixels=self.settings.attachment_max_pixels,
                timeout_seconds=self.settings.attachment_timeout_seconds,
            ),
            # Constructor compatibility only: cache and chunk retrieval are gone.
            max_cache_entries=1,
            max_chunks_per_attachment=1,
            chunk_chars=1,
            chunk_overlap=0,
            prompt_chars=self.settings.max_attachment_chars,
            concurrency=self.settings.attachment_concurrency,
            document_lock_path=self.settings.attachment_document_lock_path,
            image_analyzer=(
                self._analyze_image if self.alchemist_client is not None else None
            ),
        )
        self.core = AgentCore(
            settings=self.settings,
            character=self.character,
            memory=self.memory,
            provider=self.provider,
            generation_semaphore=self.generation_semaphore,
        )

        from .commands import AgentCommands

        await self.add_cog(AgentCommands(self))
        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d application commands to development guild", len(synced))
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global application commands", len(synced))

        self.proactive_loop.change_interval(seconds=self.settings.proactive_interval_seconds)
        self.proactive_loop.start()
        self.maintenance_loop.start()

    async def on_ready(self) -> None:
        if self.user is None:
            return
        logger.info("Connected as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))
        if not self._seeded_channels:
            self._seed_environment_channels()
            self._seeded_channels = True
        await self.apply_presence()
        if self.core:
            scheduled = self.core.schedule_due_relationship_reflections()
            if scheduled:
                logger.info("Scheduled %d pending profile/journal reflections", scheduled)

    async def apply_presence(self) -> None:
        activity_name = self.settings.bot_activity or self.character.activity
        activity = discord.Game(name=activity_name[:128]) if activity_name else None
        await self.change_presence(activity=activity)

    def _seed_environment_channels(self) -> None:
        channel_ids = self.settings.initial_auto_channels | self.settings.initial_proactive_channels
        for channel_id in channel_ids:
            channel = self.get_channel(channel_id)
            guild = getattr(channel, "guild", None)
            if channel is None or guild is None:
                logger.warning("Configured channel %s is not visible to the bot", channel_id)
                continue
            self.memory.seed_channel_config(
                guild.id,
                channel_id,
                auto_reply=channel_id in self.settings.initial_auto_channels,
                proactive=channel_id in self.settings.initial_proactive_channels,
            )

    @staticmethod
    def _scope(message: discord.Message) -> tuple[str, int, int]:
        if message.guild is None:
            return MemoryStore.scope_for(0, message.channel.id, message.author.id), 0, message.channel.id
        return (
            MemoryStore.scope_for(message.guild.id, message.channel.id),
            message.guild.id,
            message.channel.id,
        )

    def _reply_message(self, message: discord.Message) -> discord.Message | None:
        reference = message.reference
        if not reference:
            return None
        resolved = reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved
        cached = getattr(reference, "cached_message", None)
        return cached if isinstance(cached, discord.Message) else None

    async def _resolve_reply_message(
        self,
        message: discord.Message,
    ) -> discord.Message | None:
        reference = message.reference
        if reference is None:
            return None
        resolved = getattr(reference, "resolved", None) if reference else None
        reference_type = (
            getattr(reference, "type", discord.MessageReferenceType.default)
            if reference
            else None
        )
        reference_channel_id = (
            getattr(reference, "channel_id", message.channel.id)
            if reference
            else None
        )
        if (
            isinstance(resolved, discord.DeletedReferencedMessage)
            or reference_type is not discord.MessageReferenceType.default
            or reference_channel_id != message.channel.id
        ):
            return None

        cached = self._reply_message(message)
        if cached is not None:
            return cached

        message_id = getattr(reference, "message_id", None)
        fetch_message = getattr(message.channel, "fetch_message", None)
        if not isinstance(message_id, int) or fetch_message is None:
            return None
        try:
            return await asyncio.wait_for(fetch_message(message_id), timeout=3.0)
        except (asyncio.TimeoutError, discord.HTTPException):
            logger.debug(
                "Could not fetch referenced message %s in channel %s",
                message_id,
                message.channel.id,
            )
            return None

    def _get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            while len(self._channel_locks) >= 512:
                removable = next(
                    (
                        old_id
                        for old_id, old_lock in self._channel_locks.items()
                        if not old_lock.locked()
                    ),
                    None,
                )
                if removable is None:
                    # The admission semaphore keeps this practically unreachable.
                    # An uncached lock is safer than growing the map without bound.
                    return asyncio.Lock()
                self._channel_locks.pop(removable, None)
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        else:
            self._channel_locks.move_to_end(channel_id)
        return lock

    def _auto_should_reply(
        self,
        message: discord.Message,
        scope: str,
    ) -> bool:
        content = (message.content or "").strip()
        if len(content) < (1 if message.author.bot else 3):
            return False

        probability = self.settings.auto_reply_probability
        lowered = content.casefold()
        if "?" in content or lowered.startswith(_QUESTION_STARTS):
            probability += self.settings.auto_reply_question_bonus
        if self.user is not None:
            other_mentions = [user for user in message.mentions if user.id != self.user.id]
            if not message.author.bot and (other_mentions or message.role_mentions):
                probability *= 0.35

        recent = self.memory.recent_messages(scope, 2)
        if recent and recent[-1].role == "assistant" and int(time.time()) - recent[-1].created_at <= 180:
            probability += 0.20
        return random.random() < min(0.75, max(0.0, probability))

    async def _analyze_image(self, image: bytes) -> ImageAnalysis:
        if self.alchemist_client is None:
            raise AttachmentError(
                "Remote image analysis is unavailable.",
                code="alchemy_unavailable",
            )
        try:
            result = await self.alchemist_client.interrogate_image(
                image,
                forms=("caption",),
                trusted_workers=self.settings.horde_trusted_workers,
            )
        except HordeClientError as exc:
            raise AttachmentError(
                "Remote image analysis failed.",
                code="alchemy",
            ) from exc
        return ImageAnalysis(
            caption=result.caption,
            interrogation=result.interrogation,
            worker_id=result.worker_id,
            worker_name=result.worker_name,
            confidence=0.5,
        )

    async def _process_attachments(
        self,
        *,
        message: discord.Message,
        scope: str,
        guild_id: int,
        channel_id: int,
        persist: bool,
        privacy_revision: int,
    ) -> tuple[ProcessedAttachment, ...]:
        if (
            self.session is None
            or self.attachment_processor is None
            or not attachment_processing_admitted(True, len(message.attachments))
        ):
            return ()
        source = AttachmentSource(
            scope=scope,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message.id,
            user_id=message.author.id,
            privacy_revision=privacy_revision,
        )
        results: list[ProcessedAttachment] = []
        deadline = time.monotonic() + self.settings.attachment_timeout_seconds
        for attachment in message.attachments[: self.settings.attachment_max_count]:
            try:
                item = await self.attachment_processor.process_url(
                    self.session,
                    attachment.url,
                    filename=attachment.filename,
                    declared_mime=attachment.content_type or "",
                    declared_size=attachment.size,
                    source=source,
                    persist=persist,
                    _deadline=deadline,
                )
            except AttachmentError as exc:
                logger.info(
                    "Attachment %s was not processed (%s)",
                    attachment.id,
                    exc.code,
                )
                item = ProcessedAttachment(
                    sha256="",
                    filename=attachment.filename[:180],
                    kind="error",
                    status="error",
                    prompt_text="",
                    cache_hit=False,
                    error=f"{exc.code}: {str(exc)}"[:300],
                )
            results.append(item)
        return tuple(results)

    async def _send_operational_notice(self, message: discord.Message, text: str) -> None:
        result = self.notice_limiter.check((message.channel.id, message.author.id), 1, 30)
        if not result.allowed:
            return
        try:
            await message.reply(
                text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.debug("Could not send operational notice in channel %s", message.channel.id)

    def _allow_passive_tracking(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        user_result = self.tracking_user_limiter.check(
            (guild_id, user_id),
            self.settings.tracking_user_messages,
            self.settings.tracking_rate_period_seconds,
        )
        channel_result = self.tracking_channel_limiter.check(
            (guild_id, channel_id),
            self.settings.tracking_channel_messages,
            self.settings.tracking_rate_period_seconds,
        )
        return user_result.allowed and channel_result.allowed

    def _track_current_event_task(self) -> bool:
        """Register Discord-dispatched work so shutdown can drain it first."""
        if self._shutdown_started:
            return False
        task = asyncio.current_task()
        if task is not None and task not in self._active_event_tasks:
            self._active_event_tasks.add(task)
            task.add_done_callback(self._active_event_tasks.discard)
        return True

    async def on_message(self, message: discord.Message) -> None:
        if not self._track_current_event_task():
            return
        task = asyncio.current_task()
        try:
            if not self._shutdown_started:
                await self._on_message(message)
        finally:
            if task is not None:
                self._active_event_tasks.discard(task)

    async def _on_message(self, message: discord.Message) -> None:
        if self.user is None or self.core is None:
            return
        if message.author.id == self.user.id:
            return
        if message.author.id in self.settings.blacklisted_users:
            return

        scope, guild_id, channel_id = self._scope(message)
        reply_target = await self._resolve_reply_message(message)
        managed_role_mentions = _managed_bot_role_mentions(message, self.user.id)
        mentioned = (
            self.user in message.mentions
            or f"<@{self.user.id}>" in (message.content or "")
            or f"<@!{self.user.id}>" in (message.content or "")
            or bool(managed_role_mentions)
        )
        # Discord's reply-author toggle is represented by the bot appearing in
        # ``message.mentions``. A reply reference alone is context, not a
        # guaranteed request: with that toggle off it follows the same ambient
        # admission probability as an ordinary channel message.
        directed = mentioned or message.guild is None

        if message.guild is None:
            if not self.settings.respond_to_dms:
                return
            auto_reply = False
            proactive_tracking = False
            should_respond = True
        else:
            config = self.memory.get_channel_config(
                guild_id,
                channel_id,
                default_auto=channel_id in self.settings.initial_auto_channels,
                default_proactive=channel_id in self.settings.initial_proactive_channels,
            )
            auto_reply = config.auto_reply
            proactive_tracking = config.proactive
            if not (directed or auto_reply or proactive_tracking):
                return
            should_respond = directed or (auto_reply and self._auto_should_reply(message, scope))

        privacy = self.memory.privacy_state(guild_id, message.author.id)
        opted_out = privacy.opted_out
        profile_revision = self.memory.profile_revision(message.author.id)
        base_content = message.content or ""
        if mentioned:
            base_content = strip_bot_mentions(base_content, self.user.id)
            for role_id in managed_role_mentions:
                base_content = base_content.replace(f"<@&{role_id}>", "").strip()
        base_content = clean_input(base_content, self.settings.max_input_chars)

        if not should_respond:
            if (
                not opted_out
                and base_content
                and self._allow_passive_tracking(guild_id, channel_id, message.author.id)
            ):
                self.memory.record_message(
                    scope=scope,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=message.author.id,
                    author_name=message.author.display_name,
                    role="user",
                    content=base_content,
                    discord_message_id=message.id,
                    created_at=int(message.created_at.timestamp()),
                )
            return

        user_limit = self.user_limiter.check(
            (guild_id, message.author.id),
            self.settings.user_rate_requests,
            self.settings.user_rate_period_seconds,
        )
        channel_limit = self.channel_limiter.check(
            (guild_id, channel_id),
            self.settings.channel_rate_requests,
            self.settings.channel_rate_period_seconds,
        )
        if not user_limit.allowed or not channel_limit.allowed:
            retry = max(user_limit.retry_after, channel_limit.retry_after)
            if directed and not message.author.bot:
                await self._send_operational_notice(
                    message,
                    f"Rate limit reached. Try again in about {retry_seconds(retry)} seconds.",
                )
            return

        if self.request_slots.locked():
            if directed and not message.author.bot:
                await self._send_operational_notice(
                    message,
                    "The agent is at its request capacity. Try again shortly.",
                )
            return
        await self.request_slots.acquire()

        try:
            await self._handle_generation(
                message=message,
                scope=scope,
                guild_id=guild_id,
                channel_id=channel_id,
                directed=directed,
                opted_out=opted_out,
                privacy_revision=privacy.revision,
                profile_revision=profile_revision,
                base_content=base_content,
                reply_target=reply_target,
            )
        finally:
            self.request_slots.release()

    async def _handle_generation(
        self,
        *,
        message: discord.Message,
        scope: str,
        guild_id: int,
        channel_id: int,
        directed: bool,
        opted_out: bool,
        privacy_revision: int,
        profile_revision: int,
        base_content: str,
        reply_target: discord.Message | None,
    ) -> None:
        assert self.user is not None
        assert self.core is not None

        lock = self._get_channel_lock(channel_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            if directed and not message.author.bot:
                await self._send_operational_notice(
                    message,
                    "Another request is already running in this channel. Try again shortly.",
                )
            return

        try:
            async with message.channel.typing():
                attachments = await self._process_attachments(
                    message=message,
                    scope=scope,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    persist=not opted_out,
                    privacy_revision=privacy_revision,
                )
                attachment_parts = _attachment_evidence(
                    message,
                    attachments,
                    max_count=self.settings.attachment_max_count,
                )
                current_input = clean_input(
                    base_content
                    or (
                        "(The user attached one or more files without additional text.)"
                        if attachments
                        else "(The user addressed the character without additional text.)"
                    ),
                    self.settings.max_input_chars,
                )
                reply_context = ""
                reply_author_id: int | None = None
                reply_author_name = ""
                reply_author_username = ""
                reply_author_global_name = ""
                reply_author_is_bot: bool | None = None
                if reply_target is not None:
                    quoted = clean_input(reply_target.content or "", 600)
                    if not (
                        reply_target.author.id == self.user.id
                        and _is_operational_notice(quoted)
                    ):
                        reply_context = quoted
                        reply_author_id = reply_target.author.id
                        reply_author_name = reply_target.author.display_name
                        reply_author_username = str(
                            getattr(reply_target.author, "name", "") or ""
                        )
                        reply_author_global_name = str(
                            getattr(reply_target.author, "global_name", "") or ""
                        )
                        reply_author_is_bot = bool(reply_target.author.bot)

                current_privacy = self.memory.privacy_state(guild_id, message.author.id)
                persist_turn = (
                    not opted_out
                    and not current_privacy.opted_out
                    and current_privacy.revision == privacy_revision
                )
                if persist_turn:
                    stored_id = self.memory.record_message(
                        scope=scope,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=message.author.id,
                        author_name=message.author.display_name,
                        role="user",
                        content=current_input,
                        discord_message_id=message.id,
                        created_at=int(message.created_at.timestamp()),
                        attachment_parts=attachment_parts,
                    )
                    explicit_memory = extract_explicit_memory(base_content)
                    if explicit_memory:
                        if explicit_memory_references_attachment(explicit_memory):
                            attachment_memory = saved_attachment_memory_text(attachment_parts)
                            if attachment_memory:
                                self.memory.add_memory(
                                    guild_id=guild_id,
                                    user_id=message.author.id,
                                    text=attachment_memory,
                                    kind="user_asserted_attachment",
                                    source_message_id=stored_id,
                                )
                        else:
                            self.memory.add_memory(
                                guild_id=guild_id,
                                user_id=message.author.id,
                                text=explicit_memory,
                                source_message_id=stored_id,
                            )

                request = ReplyRequest(
                    scope=scope,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=message.author.id,
                    user_name=message.author.display_name,
                    user_username=str(getattr(message.author, "name", "") or ""),
                    user_global_name=str(
                        getattr(message.author, "global_name", "") or ""
                    ),
                    user_is_bot=bool(message.author.bot),
                    current_message=current_input,
                    discord_message_id=message.id,
                    reply_context=reply_context,
                    reply_discord_message_id=(
                        reply_target.id if reply_target is not None else None
                    ),
                    reply_author_id=reply_author_id,
                    reply_author_name=reply_author_name,
                    reply_author_username=reply_author_username,
                    reply_author_global_name=reply_author_global_name,
                    reply_author_is_bot=reply_author_is_bot,
                    conversation_type="dm" if message.guild is None else "guild",
                    attachments=attachments,
                    attachment_parts=attachment_parts,
                )
                try:
                    response = await self.core.reply(request)
                except ProviderError as exc:
                    logger.warning("Provider request failed in channel %s: %s", channel_id, exc)
                    # A transient model failure is operational state, not something
                    # the character said. Keep it in bounded diagnostics instead of
                    # publishing a generic outage line into the conversation.
                    return
                except Exception:
                    logger.exception("Unhandled response failure in channel %s", channel_id)
                    return

            try:
                sent = await message.reply(
                    response,
                    mention_author=False,
                )
            except discord.HTTPException:
                try:
                    sent = await message.channel.send(response)
                except discord.HTTPException:
                    logger.warning(
                        "Could not deliver generated response in channel %s",
                        channel_id,
                    )
                    return

            current_privacy = self.memory.privacy_state(
                guild_id,
                message.author.id,
            )
            persist_turn = (
                persist_turn
                and not current_privacy.opted_out
                and current_privacy.revision == privacy_revision
            )
            sent_at = int(sent.created_at.timestamp())
            if persist_turn:
                self.memory.record_message(
                    scope=scope,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=self.user.id,
                    author_name=self.character.name,
                    role="assistant",
                    content=response,
                    discord_message_id=sent.id,
                    created_at=sent_at,
                )

            # Conversation-memory storage and agent-owned social continuity use
            # independent epochs. A storage-disabled turn is still an
            # interaction the agent may learn from, while a racing profile
            # delete/reset prevents older work from recreating social state.
            profile_epoch_current = (
                self.memory.profile_revision(message.author.id) == profile_revision
            )
            relationship_input = base_content or (
                "(shared attachment evidence without authored message text)"
                if attachment_parts
                else "(addressed the agent without text)"
            )
            social_meaningful = meaningful_social_event(
                relationship_input,
                response,
                min_chars=self.settings.relationship_meaningful_chars,
            )
            if (
                profile_epoch_current
                and self.settings.relationships_enabled
                and (directed or not self.settings.relationship_direct_only)
            ):
                # Only pair the target user's own message with the response.
                # Attachment contents, quoted reply context, and third-party
                # passive messages stay out.
                self.memory.record_relationship_interaction(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=message.author.id,
                    scope=scope,
                    user_text=relationship_input,
                    assistant_text=response,
                    source_message_id=message.id,
                    meaningful=social_meaningful,
                    created_at=sent_at,
                    attachment_parts=attachment_parts,
                )
                self.core.maybe_schedule_relationship_reflection(
                    guild_id,
                    message.author.id,
                )
        finally:
            lock.release()

    @tasks.loop(seconds=300)
    async def proactive_loop(self) -> None:
        if self.core is None or self.user is None or self.settings.proactive_daily_limit == 0:
            return
        now = int(time.time())
        local_now = datetime.now(self.proactive_timezone)
        day_key = local_now.date().isoformat()
        posted = False

        for config in self.memory.proactive_channels():
            # One post per sweep is enough for a private Discord agent and
            # prevents a single provider recovery from waking several channels.
            if posted:
                break
            channel = self.get_channel(config.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            member = channel.guild.me
            if member is None or not channel.permissions_for(member).send_messages:
                continue
            last_activity, _, _ = self.memory.channel_participant_stats(
                config.guild_id,
                config.channel_id,
                0,
            )
            if (
                last_activity is None
                or now - last_activity < self.settings.proactive_min_idle_seconds
            ):
                continue

            last_proactive, state_day, state_count = self.memory.proactive_state(
                config.guild_id, config.channel_id
            )
            if last_proactive and last_activity <= last_proactive:
                # A proactive post is a one-shot conversation starter. Wait for
                # another participant before this bot can start again.
                continue
            if now - last_proactive < self.settings.proactive_cooldown_seconds:
                continue
            if state_day == day_key and state_count >= self.settings.proactive_daily_limit:
                continue

            lock = self._get_channel_lock(config.channel_id)
            if lock.locked():
                continue
            scope = MemoryStore.scope_for(config.guild_id, config.channel_id)
            async with lock:
                current_activity, _, _ = self.memory.channel_participant_stats(
                    config.guild_id,
                    config.channel_id,
                    0,
                )
                last_seen_message_id = channel.last_message_id
                if (
                    current_activity != last_activity
                    or last_seen_message_id is None
                    or not self.memory.has_discord_message_id(scope, last_seen_message_id)
                ):
                    # The process may have missed Discord traffic while offline,
                    # or a queued turn may have changed context while this lock
                    # was acquired. Never generate proactively from stale data.
                    continue
                try:
                    async with channel.typing():
                        text = await self.core.proactive_message(scope)
                except ProviderError as exc:
                    logger.info("Proactive generation skipped: %s", exc)
                    break
                except Exception:
                    logger.exception("Proactive generation failed for channel %s", config.channel_id)
                    continue
                if not text:
                    continue
                latest_activity, _, _ = self.memory.channel_participant_stats(
                    config.guild_id,
                    config.channel_id,
                    0,
                )
                if latest_activity != last_activity:
                    # A participant resumed the conversation while generation was running.
                    continue
                if channel.last_message_id != last_seen_message_id:
                    # Also catches opted-out members whose messages are intentionally
                    # absent from SQLite, plus any other intervening channel activity.
                    continue
                try:
                    sent = await channel.send(text)
                except discord.HTTPException:
                    logger.warning("Could not send proactive message to channel %s", config.channel_id)
                    continue
                sent_at = int(sent.created_at.timestamp())
                self.memory.record_message(
                    scope=scope,
                    guild_id=config.guild_id,
                    channel_id=config.channel_id,
                    user_id=self.user.id,
                    author_name=self.character.name,
                    role="assistant",
                    content=text,
                    discord_message_id=sent.id,
                    created_at=sent_at,
                    is_proactive=True,
                )
                self.memory.mark_proactive(
                    config.guild_id,
                    config.channel_id,
                    day_key,
                    sent_at,
                )
                posted = True

    @proactive_loop.before_loop
    async def before_proactive_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(hours=1)
    async def maintenance_loop(self) -> None:
        try:
            removed = self.memory.prune(
                self.settings.max_messages_per_channel,
                self.settings.max_memories_per_user,
                self.settings.max_profile_facts_per_user,
                self.settings.max_journal_entries_per_user,
                self.settings.max_pending_interactions_per_user,
            )
            scheduled_reflections = (
                self.core.schedule_due_relationship_reflections()
                if self.core is not None
                else 0
            )
            self.user_limiter.cleanup()
            self.channel_limiter.cleanup()
            self.command_limiter.cleanup()
            self.tracking_user_limiter.cleanup()
            self.tracking_channel_limiter.cleanup()
            self.notice_limiter.cleanup()
            logger.info(
                "Maintenance complete; pruned messages=%d memories=%d profile_facts=%d "
                "journal=%d pending_social=%d; scheduled_reflections=%d",
                removed["messages"],
                removed["memories"],
                removed["profile_facts"],
                removed["journal_entries"],
                removed["pending_interactions"],
                scheduled_reflections,
            )
        except Exception:
            logger.exception("Maintenance loop failed")

    @maintenance_loop.before_loop
    async def before_maintenance_loop(self) -> None:
        await self.wait_until_ready()

    def _arm_shutdown_watchdog(self) -> None:
        if not getattr(self, "_process_watchdog_enabled", False):
            return
        thread = getattr(self, "_shutdown_watchdog_thread", None)
        if thread is not None and thread.is_alive():
            return
        completed = threading.Event()
        self._shutdown_watchdog_event = completed
        timeout_seconds = max(
            0.1,
            float(
                getattr(
                    self,
                    "_process_exit_grace_seconds",
                    _PROCESS_EXIT_GRACE_SECONDS,
                )
            ),
        )

        def force_exit() -> None:
            if completed.wait(timeout_seconds):
                return
            os._exit(1)

        thread = threading.Thread(
            target=force_exit,
            name="agent-shutdown-watchdog",
            daemon=True,
        )
        self._shutdown_watchdog_thread = thread
        thread.start()

    def _disarm_shutdown_watchdog(self) -> None:
        event = getattr(self, "_shutdown_watchdog_event", None)
        if event is not None:
            event.set()

    async def _close_active_events(self) -> None:
        tasks_to_close = set(getattr(self, "_active_event_tasks", set()))
        current = asyncio.current_task()
        if current is not None:
            tasks_to_close.discard(current)
        for loop in (self.proactive_loop, self.maintenance_loop):
            if loop.is_running():
                loop_task = loop.get_task()
                loop.cancel()
                if loop_task is not None and loop_task is not current:
                    tasks_to_close.add(loop_task)
        for task in tasks_to_close:
            task.cancel()
        if not tasks_to_close:
            return
        done, pending = await asyncio.wait(
            tasks_to_close,
            timeout=max(
                0.01,
                float(
                    getattr(
                        self,
                        "_event_close_grace_seconds",
                        _EVENT_CLOSE_GRACE_SECONDS,
                    )
                ),
            ),
        )
        for task in done:
            _consume_shutdown_task(task)
        for task in pending:
            task.cancel()
            task.add_done_callback(_consume_shutdown_task)
        if pending:
            logger.warning(
                "Shutdown detached %d cancellation-resistant event task(s)",
                len(pending),
            )

    async def _shutdown_resources(self) -> None:
        logger.info("Shutdown started")
        self._shutdown_started = True
        await self._close_active_events()
        await _close_step(
            "discord",
            super().close(),
            self._discord_close_grace_seconds,
        )
        if self.core:
            await _close_step(
                "agent-core",
                self.core.close(),
                self._core_close_grace_seconds,
            )
        if self.session and not self.session.closed:
            await _close_step(
                "provider-session",
                self.session.close(),
                self._session_close_grace_seconds,
            )
        try:
            self.memory.close()
        except Exception:
            logger.exception("Shutdown step memory failed")
        self._closed_resources = True
        logger.info("Shutdown complete")

    async def close(self) -> None:
        self._arm_shutdown_watchdog()
        task = getattr(self, "_shutdown_task", None)
        if task is None:
            task = asyncio.create_task(
                self._shutdown_resources(),
                name="agentbot-shutdown",
            )
            self._shutdown_task = task
        await asyncio.shield(task)

    def run(self, *args: Any, **kwargs: Any) -> None:
        self._process_watchdog_enabled = True
        try:
            super().run(*args, **kwargs)
        finally:
            self._disarm_shutdown_watchdog()

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        del args, kwargs
        logger.exception("Unhandled Discord event error in %s", event_method)


def configure_logging(settings: Settings) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_path,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    application_level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(
        level=application_level,
        handlers=[console, file_handler],
        force=True,
    )
    # discord.py DEBUG gateway dispatches contain complete message payloads.
    logging.getLogger("discord").setLevel(max(application_level, logging.INFO))
    logging.getLogger("discord.http").setLevel(
        max(application_level, logging.WARNING)
    )
    logging.getLogger("aiohttp.access").setLevel(
        max(application_level, logging.WARNING)
    )


def create_memory_store(settings: Settings) -> MemoryStore:
    """Construct the bounded persistent store used by both Discord and simulations."""
    return MemoryStore(
        settings.database_path,
        max_messages_per_scope=settings.max_messages_per_channel,
        max_memories_per_user=settings.max_memories_per_user,
        max_total_messages=settings.max_total_messages,
        max_total_memories=settings.max_total_memories,
        max_model_outcomes=settings.max_model_outcomes,
        max_profile_facts_per_user=settings.max_profile_facts_per_user,
        max_journal_entries_per_user=settings.max_journal_entries_per_user,
        max_pending_interactions_per_user=settings.max_pending_interactions_per_user,
        max_total_profile_facts=settings.max_total_profile_facts,
        max_total_journal_entries=settings.max_total_journal_entries,
        max_total_pending_interactions=settings.max_total_pending_interactions,
        max_total_relationships=settings.max_total_relationships,
        # Schema-6 tables remain readable, but the lean runtime no longer writes
        # or maintains these superseded continuity/cache features.
        max_group_events_per_guild=0,
        max_total_group_events=0,
        max_group_journal_per_guild=0,
        max_total_group_journal=0,
        max_group_continuities=0,
        max_group_members_per_guild=0,
        max_interaction_metrics=0,
        max_attachments=0,
        max_attachment_chunks=0,
        # The card filename is a deterministic one-time legacy migration selector;
        # new storage has one process identity and is globally keyed by user.
        legacy_social_namespace=settings.character_file.stem[:64] or "default",
    )


def run() -> None:
    memory: MemoryStore | None = None
    try:
        settings = Settings.load()
        configure_logging(settings)
        character = load_character(settings.character_file)
        memory = create_memory_store(settings)
        bot = AgentBot(settings, character, memory)
        bot.run(settings.discord_token, log_handler=None)
    except (ConfigError, CharacterError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    except KeyboardInterrupt:
        return
    except Exception:
        if memory is not None:
            try:
                memory.close()
            except Exception:
                pass
        raise
