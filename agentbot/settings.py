from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true/false, yes/no, on/off, or 1/0")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _csv_ints(name: str) -> FrozenSet[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-integer value: {item!r}") from exc
        if value <= 0:
            raise ConfigError(f"{name} contains a non-positive Discord ID: {item!r}")
        values.add(value)
    return frozenset(values)


def _optional_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive Discord ID")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    discord_token: str
    character_file: Path
    database_path: Path
    log_path: Path
    log_level: str
    bot_activity: str
    dev_guild_id: Optional[int]

    horde_api_key: str
    horde_base_url: str
    horde_trusted_workers: bool
    horde_poll_seconds: float
    horde_timeout_seconds: int
    horde_router_metadata_ttl_seconds: int
    horde_router_sticky_seconds: int
    horde_min_model_parameters_bn: float
    horde_router_max_scopes: int
    provider_max_tokens: int
    provider_context_tokens: int
    relationship_context_tokens: int
    proactive_context_tokens: int

    initial_auto_channels: FrozenSet[int]
    initial_proactive_channels: FrozenSet[int]
    blacklisted_users: FrozenSet[int]
    respond_to_dms: bool
    auto_reply_probability: float
    auto_reply_question_bonus: float

    max_input_chars: int
    max_attachment_bytes: int
    max_attachment_chars: int
    attachment_max_count: int
    attachment_max_extracted_chars: int
    attachment_max_pixels: int
    attachment_timeout_seconds: float
    attachment_concurrency: int
    alchemist_api_key: str
    alchemist_enabled: bool
    max_reply_chars: int
    recent_message_count: int
    recall_message_count: int
    recall_candidate_count: int
    max_messages_per_channel: int
    max_memories_per_user: int
    max_total_messages: int
    max_total_memories: int
    max_model_outcomes: int

    relationships_enabled: bool
    relationship_direct_only: bool
    relationship_reflect_every: int
    relationship_meaningful_chars: int
    relationship_meaningful_event_threshold: int
    relationship_reflect_min_seconds: int
    relationship_reflect_max_events: int
    profile_context_facts: int
    journal_context_entries: int
    max_profile_facts_per_user: int
    max_journal_entries_per_user: int
    max_pending_interactions_per_user: int
    max_total_profile_facts: int
    max_total_journal_entries: int
    max_total_pending_interactions: int
    max_total_relationships: int

    user_rate_requests: int
    user_rate_period_seconds: int
    channel_rate_requests: int
    channel_rate_period_seconds: int
    command_rate_requests: int
    command_rate_period_seconds: int
    tracking_user_messages: int
    tracking_channel_messages: int
    tracking_rate_period_seconds: int
    global_concurrency: int
    max_pending_requests: int

    proactive_interval_seconds: int
    proactive_min_idle_seconds: int
    proactive_cooldown_seconds: int
    proactive_daily_limit: int
    proactive_timezone: str

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        load_dotenv(env_file or root / ".env")

        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        if not discord_token:
            raise ConfigError("DISCORD_TOKEN is required")

        character_raw = os.getenv("CHARACTER_FILE", "characters/character.json").strip()
        character_file = Path(character_raw)
        if not character_file.is_absolute():
            character_file = root / character_file

        database_raw = os.getenv("DATABASE_PATH", "data/agent.db").strip()
        database_path = Path(database_raw)
        if not database_path.is_absolute():
            database_path = root / database_path

        log_raw = os.getenv("LOG_PATH", "logs/agent.log").strip()
        log_path = Path(log_raw)
        if not log_path.is_absolute():
            log_path = root / log_path

        provider = os.getenv("LLM_PROVIDER", "horde").strip().lower()
        if provider != "horde":
            raise ConfigError("LLM_PROVIDER must be horde")

        min_idle = _env_int("PROACTIVE_MIN_IDLE_SECONDS", 21_600, 300, 2_592_000)

        provider_max_tokens = _env_int("PROVIDER_MAX_TOKENS", 300, 32, 1200)
        provider_context_tokens = _env_int("PROVIDER_CONTEXT_TOKENS", 8192, 2048, 32768)
        if provider_context_tokens - provider_max_tokens < 1024:
            raise ConfigError(
                "PROVIDER_CONTEXT_TOKENS must leave at least 1024 tokens after PROVIDER_MAX_TOKENS"
            )
        relationship_context_tokens = _env_int(
            "RELATIONSHIP_CONTEXT_TOKENS", 4096, 2048, provider_context_tokens
        )
        proactive_context_tokens = _env_int(
            "PROACTIVE_CONTEXT_TOKENS", 4096, 2048, provider_context_tokens
        )

        global_concurrency = _env_int("GLOBAL_CONCURRENCY", 2, 1, 8)
        max_pending_requests = _env_int("MAX_PENDING_REQUESTS", 6, 1, 32)
        if max_pending_requests < global_concurrency:
            raise ConfigError("MAX_PENDING_REQUESTS must be at least GLOBAL_CONCURRENCY")

        relationship_reflect_every = _env_int("RELATIONSHIP_REFLECT_EVERY", 6, 2, 50)
        relationship_reflect_max_events = _env_int(
            "RELATIONSHIP_REFLECT_MAX_EVENTS", 10, 2, 30
        )
        profile_context_facts = _env_int("PROFILE_CONTEXT_FACTS", 8, 0, 24)
        journal_context_entries = _env_int("JOURNAL_CONTEXT_ENTRIES", 2, 0, 5)
        max_profile_facts_per_user = _env_int(
            "MAX_PROFILE_FACTS_PER_USER", 24, 4, 100
        )
        max_journal_entries_per_user = _env_int(
            "MAX_JOURNAL_ENTRIES_PER_USER", 20, 2, 100
        )
        max_pending_interactions_per_user = _env_int(
            "MAX_PENDING_INTERACTIONS_PER_USER", 12, 2, 50
        )
        max_total_profile_facts = _env_int(
            "MAX_TOTAL_PROFILE_FACTS", 5_000, 100, 100_000
        )
        max_total_journal_entries = _env_int(
            "MAX_TOTAL_JOURNAL_ENTRIES", 3_000, 100, 100_000
        )
        max_total_pending_interactions = _env_int(
            "MAX_TOTAL_PENDING_INTERACTIONS", 5_000, 100, 100_000
        )
        max_total_relationships = _env_int(
            "MAX_TOTAL_RELATIONSHIPS", 5_000, 100, 100_000
        )
        if relationship_reflect_max_events < relationship_reflect_every:
            raise ConfigError(
                "RELATIONSHIP_REFLECT_MAX_EVENTS must be at least RELATIONSHIP_REFLECT_EVERY"
            )
        if max_pending_interactions_per_user < relationship_reflect_max_events:
            raise ConfigError(
                "MAX_PENDING_INTERACTIONS_PER_USER must be at least RELATIONSHIP_REFLECT_MAX_EVENTS"
            )
        if profile_context_facts > max_profile_facts_per_user:
            raise ConfigError(
                "PROFILE_CONTEXT_FACTS cannot exceed MAX_PROFILE_FACTS_PER_USER"
            )
        if journal_context_entries > max_journal_entries_per_user:
            raise ConfigError(
                "JOURNAL_CONTEXT_ENTRIES cannot exceed MAX_JOURNAL_ENTRIES_PER_USER"
            )
        if max_total_profile_facts < max_profile_facts_per_user:
            raise ConfigError(
                "MAX_TOTAL_PROFILE_FACTS must be at least MAX_PROFILE_FACTS_PER_USER"
            )
        if max_total_journal_entries < max_journal_entries_per_user:
            raise ConfigError(
                "MAX_TOTAL_JOURNAL_ENTRIES must be at least MAX_JOURNAL_ENTRIES_PER_USER"
            )
        if max_total_pending_interactions < max_pending_interactions_per_user:
            raise ConfigError(
                "MAX_TOTAL_PENDING_INTERACTIONS must be at least MAX_PENDING_INTERACTIONS_PER_USER"
            )
        horde_api_key = os.getenv("HORDE_API_KEY", "0000000000").strip() or "0000000000"
        alchemist_api_key = (
            os.getenv("ALCHEMIST_API_KEY", "0000000000").strip() or "0000000000"
        )

        return cls(
            project_root=root,
            discord_token=discord_token,
            character_file=character_file,
            database_path=database_path,
            log_path=log_path,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            bot_activity=os.getenv("BOT_ACTIVITY", "").strip(),
            dev_guild_id=_optional_int("DEV_GUILD_ID"),
            horde_api_key=horde_api_key,
            horde_base_url=os.getenv("HORDE_BASE_URL", "https://aihorde.net/api/v2").rstrip("/"),
            horde_trusted_workers=_env_bool("HORDE_TRUSTED_WORKERS", True),
            horde_poll_seconds=_env_float("HORDE_POLL_SECONDS", 2.0, 1.0, 15.0),
            horde_timeout_seconds=_env_int("HORDE_TIMEOUT_SECONDS", 120, 15, 600),
            horde_router_metadata_ttl_seconds=_env_int(
                "HORDE_ROUTER_METADATA_TTL_SECONDS", 90, 60, 120
            ),
            horde_router_sticky_seconds=_env_int(
                "HORDE_ROUTER_STICKY_SECONDS", 1800, 60, 7200
            ),
            horde_min_model_parameters_bn=_env_float(
                "HORDE_MIN_MODEL_PARAMETERS_BN", 7.0, 7.0, 200.0
            ),
            horde_router_max_scopes=_env_int(
                "HORDE_ROUTER_MAX_SCOPES", 512, 8, 10_000
            ),
            provider_max_tokens=provider_max_tokens,
            provider_context_tokens=provider_context_tokens,
            relationship_context_tokens=relationship_context_tokens,
            proactive_context_tokens=proactive_context_tokens,
            initial_auto_channels=_csv_ints("AUTO_REPLY_CHANNELS"),
            initial_proactive_channels=_csv_ints("PROACTIVE_CHANNELS"),
            blacklisted_users=_csv_ints("BLACKLISTED_USER_IDS"),
            respond_to_dms=_env_bool("RESPOND_TO_DMS", True),
            auto_reply_probability=_env_float("AUTO_REPLY_PROBABILITY", 0.10, 0.0, 1.0),
            auto_reply_question_bonus=_env_float("AUTO_REPLY_QUESTION_BONUS", 0.18, 0.0, 1.0),
            max_input_chars=_env_int("MAX_INPUT_CHARS", 3500, 256, 16000),
            max_attachment_bytes=_env_int(
                "MAX_ATTACHMENT_BYTES", 5_242_880, 1024, 20_000_000
            ),
            max_attachment_chars=_env_int("MAX_ATTACHMENT_CHARS", 6000, 256, 16000),
            attachment_max_count=_env_int("ATTACHMENT_MAX_COUNT", 2, 1, 3),
            attachment_max_extracted_chars=_env_int(
                "ATTACHMENT_MAX_EXTRACTED_CHARS", 100_000, 1000, 1_000_000
            ),
            attachment_max_pixels=_env_int(
                "ATTACHMENT_MAX_PIXELS", 16_777_216, 1024, 100_000_000
            ),
            attachment_timeout_seconds=_env_float(
                "ATTACHMENT_TIMEOUT_SECONDS", 60.0, 1.0, 60.0
            ),
            attachment_concurrency=_env_int("ATTACHMENT_CONCURRENCY", 1, 1, 2),
            alchemist_api_key=alchemist_api_key,
            alchemist_enabled=_env_bool("ALCHEMIST_ENABLED", True),
            max_reply_chars=_env_int("MAX_REPLY_CHARS", 1800, 128, 1950),
            recent_message_count=_env_int("RECENT_MESSAGE_COUNT", 14, 2, 40),
            recall_message_count=_env_int("RECALL_MESSAGE_COUNT", 4, 0, 12),
            recall_candidate_count=_env_int("RECALL_CANDIDATE_COUNT", 200, 20, 1000),
            max_messages_per_channel=_env_int("MAX_MESSAGES_PER_CHANNEL", 600, 50, 5000),
            max_memories_per_user=_env_int("MAX_MEMORIES_PER_USER", 50, 5, 500),
            max_total_messages=_env_int("MAX_TOTAL_MESSAGES", 10_000, 100, 200_000),
            max_total_memories=_env_int("MAX_TOTAL_MEMORIES", 5_000, 100, 100_000),
            max_model_outcomes=_env_int("MAX_MODEL_OUTCOMES", 1000, 10, 100_000),
            relationships_enabled=_env_bool("RELATIONSHIPS_ENABLED", True),
            relationship_direct_only=_env_bool("RELATIONSHIP_DIRECT_ONLY", True),
            relationship_reflect_every=relationship_reflect_every,
            relationship_meaningful_chars=_env_int(
                "RELATIONSHIP_MEANINGFUL_CHARS", 220, 80, 2000
            ),
            relationship_meaningful_event_threshold=_env_int(
                "RELATIONSHIP_MEANINGFUL_EVENT_THRESHOLD", 1, 1, 10
            ),
            relationship_reflect_min_seconds=_env_int(
                "RELATIONSHIP_REFLECT_MIN_SECONDS", 1800, 60, 604_800
            ),
            relationship_reflect_max_events=relationship_reflect_max_events,
            profile_context_facts=profile_context_facts,
            journal_context_entries=journal_context_entries,
            max_profile_facts_per_user=max_profile_facts_per_user,
            max_journal_entries_per_user=max_journal_entries_per_user,
            max_pending_interactions_per_user=max_pending_interactions_per_user,
            max_total_profile_facts=max_total_profile_facts,
            max_total_journal_entries=max_total_journal_entries,
            max_total_pending_interactions=max_total_pending_interactions,
            max_total_relationships=max_total_relationships,
            user_rate_requests=_env_int("USER_RATE_REQUESTS", 3, 1, 30),
            user_rate_period_seconds=_env_int("USER_RATE_PERIOD_SECONDS", 60, 5, 3600),
            channel_rate_requests=_env_int("CHANNEL_RATE_REQUESTS", 8, 1, 100),
            channel_rate_period_seconds=_env_int("CHANNEL_RATE_PERIOD_SECONDS", 60, 5, 3600),
            command_rate_requests=_env_int("COMMAND_RATE_REQUESTS", 12, 1, 100),
            command_rate_period_seconds=_env_int("COMMAND_RATE_PERIOD_SECONDS", 60, 5, 3600),
            tracking_user_messages=_env_int("TRACKING_USER_MESSAGES", 30, 1, 300),
            tracking_channel_messages=_env_int("TRACKING_CHANNEL_MESSAGES", 120, 1, 2000),
            tracking_rate_period_seconds=_env_int(
                "TRACKING_RATE_PERIOD_SECONDS", 60, 5, 3600
            ),
            global_concurrency=global_concurrency,
            max_pending_requests=max_pending_requests,
            proactive_interval_seconds=_env_int("PROACTIVE_INTERVAL_SECONDS", 300, 60, 3600),
            proactive_min_idle_seconds=min_idle,
            proactive_cooldown_seconds=_env_int("PROACTIVE_COOLDOWN_SECONDS", 43_200, 600, 2_592_000),
            proactive_daily_limit=_env_int("PROACTIVE_DAILY_LIMIT", 2, 0, 20),
            proactive_timezone=os.getenv("PROACTIVE_TIMEZONE", "UTC").strip() or "UTC",
        )
