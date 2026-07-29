from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import discord
from discord import app_commands
from discord.ext import commands

from .character import CharacterError
from .policy import clean_input, retry_seconds

if TYPE_CHECKING:
    from .app import AgentBot

logger = logging.getLogger(__name__)


class _ProfilePageRecord(Protocol):
    id: int
    kind: str
    topic: str
    status: str
    provenance: str
    text: str


class _JournalPageEntry(Protocol):
    id: int
    created_at: int
    text: str


def _packed_pages(lines: Sequence[str], *, max_chars: int = 1_850) -> tuple[str, ...]:
    """Pack complete record lines into bounded Discord messages without dropping text."""
    pages: list[str] = []
    current: list[str] = []
    current_chars = 0
    for line in lines:
        if len(line) > max_chars:
            if current:
                pages.append("\n".join(current))
                current = []
                current_chars = 0
            pages.extend(line[index : index + max_chars] for index in range(0, len(line), max_chars))
            continue
        added_chars = len(line) + (1 if current else 0)
        if current and current_chars + added_chars > max_chars:
            pages.append("\n".join(current))
            current = [line]
            current_chars = len(line)
        else:
            current.append(line)
            current_chars += added_chars
    if current:
        pages.append("\n".join(current))
    return tuple(pages)


def _profile_pages(records: Sequence[_ProfilePageRecord]) -> tuple[str, ...]:
    lines = [
        (
            f"`profile:{record.id}` - "
            f"{record.kind}/{discord.utils.escape_markdown(record.topic)} - "
            f"{record.status} - {record.provenance} - "
            f"{discord.utils.escape_markdown(record.text)}"
        )
        for record in records
    ]
    return _packed_pages(lines)


def _journal_pages(entries: Sequence[_JournalPageEntry]) -> tuple[str, ...]:
    lines = [
        (
            f"`journal:{entry.id}` - <t:{entry.created_at}:R> - "
            f"{discord.utils.escape_markdown(entry.text)}"
        )
        for entry in entries
    ]
    return _packed_pages(lines)


def _requested_page(pages: Sequence[str], page: int, *, label: str) -> str:
    if not pages:
        return "No pages are available."
    if page < 1 or page > len(pages):
        return f"Page {page} is out of range. Choose a page from 1 to {len(pages)}."
    return f"{label} - page {page}/{len(pages)}\n{pages[page - 1]}"

class CommandRateLimited(app_commands.CheckFailure):
    def __init__(self, retry_after: float) -> None:
        super().__init__("Slash-command rate limit reached")
        self.retry_after = retry_after


class CommandBlocked(app_commands.CheckFailure):
    """Raised when a configured blacklist entry attempts an agent command."""


class CommandShuttingDown(app_commands.CheckFailure):
    """Raised without a reply when shutdown has stopped accepting command work."""


def _current_rss_mb() -> float:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def _peak_rss_mb() -> float:
    try:
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return _current_rss_mb()


class AgentCommands(commands.Cog):
    agent = app_commands.Group(name="agent", description="Configure and inspect the AI agent")
    memory_group = app_commands.Group(name="memory", description="Manage your stored conversation memory")
    profile_group = app_commands.Group(
        name="profile",
        description="Inspect and manage the agent's social profile about you",
    )

    def __init__(self, bot: "AgentBot") -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.bot._track_current_event_task():
            raise CommandShuttingDown("Agent shutdown is already in progress")
        qualified_name = str(getattr(interaction.command, "qualified_name", ""))
        privacy_commands = {
            "privacy",
            "memory search",
            "memory delete",
            "memory storage",
            "memory forget",
            "profile view",
            "profile facts",
            "profile delete",
            "profile journal",
            "profile reset",
        }
        if (
            interaction.user.id in self.bot.settings.blacklisted_users
            and qualified_name not in privacy_commands
        ):
            raise CommandBlocked("This user is blocked from agent commands")
        result = self.bot.command_limiter.check(
            (interaction.guild_id or 0, interaction.user.id),
            self.bot.settings.command_rate_requests,
            self.bot.settings.command_rate_period_seconds,
        )
        if not result.allowed:
            raise CommandRateLimited(result.retry_after)
        return True

    @agent.command(name="status", description="Show agent runtime and memory status")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        stats = self.bot.memory.stats()
        provider = self.bot.provider.status() if self.bot.provider else {"provider": "not ready"}
        embed = discord.Embed(title="Discord Agent Lite", color=discord.Color.blurple())
        embed.add_field(name="Character", value=self.bot.character.name[:100], inline=True)
        embed.add_field(name="Provider", value=str(provider.get("provider", "unknown")), inline=True)
        embed.add_field(
            name="RSS current / peak",
            value=f"{_current_rss_mb():.1f} / {_peak_rss_mb():.1f} MiB",
            inline=True,
        )
        embed.add_field(name="Stored messages", value=f"{stats['messages']:,}", inline=True)
        embed.add_field(name="Explicit memories", value=f"{stats['memories']:,}", inline=True)
        embed.add_field(name="Profile facts", value=f"{stats['profile_facts']:,}", inline=True)
        embed.add_field(name="Journal entries", value=f"{stats['journal_entries']:,}", inline=True)
        embed.add_field(
            name="Pending reflections",
            value=f"{stats['pending_interactions']:,}",
            inline=True,
        )
        embed.add_field(name="Known users", value=f"{stats['users']:,}", inline=True)
        embed.add_field(
            name="Database",
            value=f"{stats['database_bytes'] / (1024 * 1024):.2f} MiB",
            inline=True,
        )
        if provider.get("routing") == "adaptive":
            metadata_age = provider.get("metadata_age_seconds")
            age_text = "not loaded" if metadata_age is None else f"{metadata_age}s"
            embed.add_field(
                name="Adaptive routing",
                value=(
                    f"metadata={age_text}, "
                    f"eligible={provider.get('eligible_candidate_count', 0)}, "
                    f"active selections={provider.get('selection_count', 0)}"
                ),
                inline=False,
            )
            recent_failures = provider.get("recent_model_failures")
            if isinstance(recent_failures, list) and recent_failures:
                rows = []
                for item in recent_failures[:5]:
                    if isinstance(item, dict):
                        rows.append(
                            f"{item.get('task', '?')}: {item.get('model', '?')} "
                            f"({item.get('error_kind', 'unknown')}, "
                            f"{item.get('age_seconds', 0)}s ago)"
                        )
                if rows:
                    embed.add_field(
                        name="Recent model failures",
                        value="\n".join(rows)[:1024],
                        inline=False,
                    )
            selected = provider.get("selected_models")
            if isinstance(selected, list) and selected:
                rows = []
                for item in selected[:5]:
                    if isinstance(item, dict):
                        rows.append(
                            f"{item.get('task', '?')}: {item.get('model', '?')} "
                            f"({item.get('format', '?')}, "
                            f"selected {item.get('selection_age_seconds', 0)}s, "
                            f"idle {item.get('idle_seconds', 0)}s)"
                        )
                if rows:
                    embed.add_field(
                        name="Sticky model selections",
                        value="\n".join(rows)[:1024],
                        inline=False,
                    )
        if self.bot.attachment_processor is not None:
            attachment_status = self.bot.attachment_processor.status()
            embed.add_field(
                name="Attachment jobs",
                value=(
                    f"active={attachment_status['active_jobs']}, "
                    f"peak={attachment_status['peak_active_jobs']}, "
                    f"processed={attachment_status['processed_jobs']}, "
                    f"failed={attachment_status['failed_jobs']}"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="privacy",
        description="Explain remote processing, storage, and your privacy controls",
    )
    async def privacy(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Privacy and remote processing",
            description=(
                "This agent uses AI Horde. Community worker operators may receive the "
                "data needed to answer an admitted request."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Text generation",
            value=(
                "Your current message, character card, bounded recent conversation, relevant "
                "profile/journal context, and supported current attachments are sent to an "
                "AI Horde Scribe worker. Compact profile/journal reflections use the same service."
            ),
            inline=False,
        )
        embed.add_field(
            name="Images and files",
            value=(
                "Attachments are processed only when the bot has admitted a response. "
                "Supported images are sent to a community Alchemist worker for a fallible "
                "caption; small UTF-8 text, Markdown, and common source files are read locally. "
                "Raw temporary files are deleted after the current turn and attachment content "
                "is not cached."
            ),
            inline=False,
        )
        embed.add_field(
            name="Credentials",
            value=(
                "Discord and provider credentials are configuration secrets and are not "
                "intentionally placed in model prompts. Do not upload secrets in messages "
                "or files."
            ),
            inline=False,
        )
        embed.add_field(
            name="Controls",
            value=(
                "Conversation memory and agent-authored profile/journal continuity are "
                "separate. Use `/memory search`, `/memory storage`, and `/memory forget` "
                "for conversation memory. Profile and journal entries have no opt-out; "
                "use private `/profile view`, `/profile facts`, `/profile journal`, "
                "`/profile delete`, or `/profile reset` controls. Continued qualifying "
                "interactions can create new observations after deletion or reset."
            ),
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @agent.command(name="channel", description="Set auto-reply and proactive behavior for a channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to configure",
        auto_reply="Allow probabilistic replies without a direct mention",
        proactive="Allow bounded context-aware conversation starters",
    )
    async def channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        auto_reply: bool,
        proactive: bool,
    ) -> None:
        assert interaction.guild_id is not None
        if channel.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "The selected channel must belong to this server.",
                ephemeral=True,
            )
            return
        self.bot.memory.set_channel_config(
            interaction.guild_id,
            channel.id,
            auto_reply=auto_reply,
            proactive=proactive,
        )
        await interaction.response.send_message(
            f"Updated {channel.mention}: auto-reply={auto_reply}, proactive={proactive}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @agent.command(name="reload_character", description="Reload the configured character card")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reload_character(self, interaction: discord.Interaction) -> None:
        try:
            character = self.bot.core.reload_character() if self.bot.core else None
            if character is None:
                raise CharacterError("Agent core is not ready")
            self.bot.character = character
            await self.bot.apply_presence()
        except CharacterError as exc:
            await interaction.response.send_message(
                f"Character reload failed: {exc}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            f"Reloaded character card: **{discord.utils.escape_markdown(character.name)}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @agent.command(name="prune", description="Prune bounded agent storage")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def prune(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = self.bot.memory.prune(
            self.bot.settings.max_messages_per_channel,
            self.bot.settings.max_memories_per_user,
        )
        await interaction.followup.send(
            f"Pruned {removed['messages']} messages, {removed['memories']} explicit memories, "
            f"{removed['profile_facts']} profile facts, {removed['journal_entries']} journal entries, "
            f"{removed['pending_interactions']} pending social events, "
            f"{removed['group_events']} pending guild events, and "
            f"{removed['group_journal_entries']} guild notes.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @memory_group.command(name="remember", description="Store one explicit memory about yourself")
    @app_commands.describe(text="A fact or preference to remember; stored only in this server or DM")
    async def remember(self, interaction: discord.Interaction, text: str) -> None:
        guild_id = interaction.guild_id or 0
        user_id = interaction.user.id
        if self.bot.memory.is_opted_out(guild_id, user_id):
            await interaction.response.send_message(
                "Memory storage is disabled for you here. Use `/memory storage enabled:true` first.",
                ephemeral=True,
            )
            return
        clean = " ".join(clean_input(text, 500).split())
        if len(clean) < 3:
            await interaction.response.send_message("That memory is too short.", ephemeral=True)
            return
        memory_id = self.bot.memory.add_memory(guild_id=guild_id, user_id=user_id, text=clean)
        await interaction.response.send_message(
            "Stored that memory."
            if memory_id is not None
            else "The configured global memory capacity has been reached; nothing was stored.",
            ephemeral=True,
        )

    @memory_group.command(name="search", description="Search or list your explicit memories")
    @app_commands.describe(query="Optional words to search for")
    async def search(self, interaction: discord.Interaction, query: str = "") -> None:
        guild_id = interaction.guild_id or 0
        memories = (
            self.bot.memory.search_memories(
                guild_id=guild_id,
                user_id=interaction.user.id,
                query=query,
                limit=8,
            )
            if query.strip()
            else self.bot.memory.list_memories(guild_id, interaction.user.id, limit=8)
        )
        if not memories:
            await interaction.response.send_message("No matching explicit memories.", ephemeral=True)
            return
        lines: list[str] = []
        remaining = 1800
        for item in memories:
            prefix = f"`{item.id}` — "
            escaped = discord.utils.escape_markdown(item.text)
            available = remaining - len(prefix) - (1 if lines else 0)
            if available < 2:
                break
            body = escaped if len(escaped) <= available else escaped[: available - 1].rstrip() + "…"
            line = prefix + body
            lines.append(line)
            remaining -= len(line) + 1
            if remaining < 16:
                break
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @memory_group.command(name="delete", description="Delete one explicit memory by its ID")
    async def delete(self, interaction: discord.Interaction, memory_id: int) -> None:
        deleted = self.bot.memory.delete_memory(
            interaction.guild_id or 0,
            interaction.user.id,
            memory_id,
        )
        await interaction.response.send_message(
            "Deleted that memory." if deleted else "No matching memory owned by you.",
            ephemeral=True,
        )

    @memory_group.command(
        name="storage",
        description="Enable or disable future conversation-memory storage for you",
    )
    @app_commands.describe(
        enabled="When false, conversation messages and explicit memories are not stored"
    )
    async def storage(self, interaction: discord.Interaction, enabled: bool) -> None:
        guild_id = interaction.guild_id or 0
        self.bot.memory.set_opted_out(guild_id, interaction.user.id, not enabled)
        await interaction.response.send_message(
            (
                "Conversation-memory storage enabled."
                if enabled
                else "Conversation-memory storage disabled; existing conversation memory "
                "was not deleted. Internal profile/journal continuity remains active."
            ),
            ephemeral=True,
        )

    @memory_group.command(
        name="forget",
        description="Delete your stored conversation memory in this server or DM",
    )
    @app_commands.describe(
        confirm="Must be true to delete messages, explicit memories, and affected summaries"
    )
    async def forget(self, interaction: discord.Interaction, confirm: bool) -> None:
        if not confirm:
            await interaction.response.send_message(
                "Nothing was deleted. Run the command with `confirm:true` to proceed.",
                ephemeral=True,
            )
            return
        guild_id = interaction.guild_id or 0
        removed = self.bot.memory.delete_conversation_memory(
            guild_id,
            interaction.user.id,
        )
        await interaction.response.send_message(
            "Deleted your stored conversation memory here. Future conversation-memory "
            "storage was not changed. "
            f"Removed {removed['messages']} messages, {removed['memories']} explicit memories, "
            f"{removed['attachments']} attachment source links, and invalidated "
            f"{removed['summaries']} summaries. Internal profile/journal continuity was "
            "not changed.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @profile_group.command(name="view", description="Show the agent's compact continuity about you")
    async def profile_view(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        counts = self.bot.memory.social_profile_counts(user_id=user_id)
        records = self.bot.memory.list_profile_records(
            user_id=user_id,
            limit=5,
            include_inactive=False,
        )
        journal = self.bot.memory.recent_journal_entries(
            user_id=user_id,
            limit=1,
        )
        relationship = self.bot.memory.relationship_state(user_id=user_id)

        description = (
            "Automatic agent-owned profile, relationship, and journal reflection is enabled."
            if self.bot.settings.relationships_enabled
            else "Automatic reflection is disabled; this shows any stored continuity."
        )
        embed = discord.Embed(
            title="Your agent continuity",
            description=description,
            color=discord.Color.blurple(),
        )
        if records:
            fact_lines: list[str] = []
            for record in records:
                fact_lines.append(
                    f"• **{record.kind}/{record.topic}** "
                    f"({record.status}, {record.provenance}): "
                    f"{discord.utils.escape_markdown(record.text[:220])}"
                )
            facts_value = "\n".join(fact_lines)[:1024]
        else:
            facts_value = "No profile facts stored."
        embed.add_field(
            name=(
                f"Agent profile ({counts['confirmed_facts'] + counts['tentative_facts']} active, "
                f"{counts['inactive_facts']} inactive)"
            ),
            value=facts_value,
            inline=False,
        )
        active_dimensions = ", ".join(
            f"{name} {value:+d}"
            for name, value in relationship.dimensions.items()
            if value
        )
        relationship_value = (
            f"{relationship.label}; familiarity {relationship.familiarity}%; "
            f"{relationship.interaction_count} interactions"
        )
        if active_dimensions:
            relationship_value += f"\n{active_dimensions}"
        if relationship.summary:
            relationship_value += "\n" + discord.utils.escape_markdown(
                relationship.summary[:700]
            )
        embed.add_field(
            name="Relationship state (conversational only)",
            value=relationship_value[:1024],
            inline=False,
        )
        latest_journal = (
            discord.utils.escape_markdown(journal[0].text[:900])
            if journal
            else "No journal entries yet."
        )
        embed.add_field(
            name=f"Latest journal entry ({counts['journal_entries']} total)",
            value=latest_journal,
            inline=False,
        )
        embed.set_footer(
            text=(
                "Profile observations and relationship state are fallible and conversational only. "
                "They never grant authority, change moderation, or weaken safeguards."
            )
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @profile_group.command(name="facts", description="List the social profile records stored about you")
    @app_commands.describe(page="Page number to show, starting at 1")
    async def profile_facts(self, interaction: discord.Interaction, page: int = 1) -> None:
        records = self.bot.memory.list_profile_records(
            user_id=interaction.user.id,
            limit=self.bot.settings.max_profile_facts_per_user,
            include_inactive=True,
        )
        if not records:
            await interaction.response.send_message(
                "No social profile records are stored about you.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            _requested_page(_profile_pages(records), page, label="Profile records"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @profile_group.command(name="delete", description="Delete one profile or journal record by typed ID")
    @app_commands.describe(record_id="An ID shown by /profile facts or /profile journal")
    async def profile_delete(self, interaction: discord.Interaction, record_id: str) -> None:
        if self.bot.core:
            self.bot.core.cancel_relationship_reflections_for_user(interaction.user.id)
        deleted = self.bot.memory.delete_social_record(
            user_id=interaction.user.id,
            record_id=record_id,
        )
        await interaction.response.send_message(
            (
                "Deleted that social record. Later qualifying interactions may rebuild "
                "agent continuity."
                if deleted
                else "No matching social record owned by you."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @profile_group.command(name="journal", description="Show character journal entries stored about you")
    @app_commands.describe(page="Page number to show, starting at 1")
    async def profile_journal(self, interaction: discord.Interaction, page: int = 1) -> None:
        entries = self.bot.memory.recent_journal_entries(
            user_id=interaction.user.id,
            limit=self.bot.settings.max_journal_entries_per_user,
        )
        if not entries:
            await interaction.response.send_message(
                "No character journal entries are stored about you here.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            _requested_page(_journal_pages(entries), page, label="Journal entries"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @profile_group.command(
        name="reset",
        description="Reset your agent profile, relationship state, and character journal",
    )
    @app_commands.describe(confirm="Must be true to delete profile, relationship, and journal data")
    async def profile_reset(self, interaction: discord.Interaction, confirm: bool) -> None:
        if not confirm:
            await interaction.response.send_message(
                "Nothing was deleted. Run the command with `confirm:true` to reset the social profile.",
                ephemeral=True,
            )
            return
        if self.bot.core:
            self.bot.core.cancel_relationship_reflections_for_user(interaction.user.id)
        removed = self.bot.memory.reset_social_profile(
            user_id=interaction.user.id,
        )
        await interaction.response.send_message(
            "Reset your agent profile, relationship state, and character journal. "
            f"Removed {removed['profile_facts']} profile records, {removed['relationships']} "
            f"relationship state, {removed['journal_entries']} journal entries, and "
            f"{removed['pending_interactions']} pending reflections. Later qualifying "
            "interactions may rebuild agent continuity.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, CommandShuttingDown):
            return
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the server permission required by this command."
        elif isinstance(error, CommandRateLimited):
            message = (
                "Slash-command rate limit reached. Try again in about "
                f"{retry_seconds(error.retry_after)} seconds."
            )
        elif isinstance(error, CommandBlocked):
            message = "Agent commands are disabled for this account."
        elif isinstance(error, app_commands.CheckFailure):
            message = "This command is not available to you."
        else:
            logger.error(
                "Slash command failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "The command failed internally. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
