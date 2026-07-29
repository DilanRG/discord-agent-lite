from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .prompt_formats import is_supported_instruction_format


class NoEligibleHordeModel(RuntimeError):
    """Raised when live Horde metadata contains no compatible model."""


@dataclass(frozen=True, slots=True)
class LiveModel:
    name: str
    count: int
    performance: float
    queued: float
    jobs: float
    eta: int


@dataclass(frozen=True, slots=True)
class ModelReference:
    name: str
    parameters_bn: float
    instruction_format: str
    tags: frozenset[str]
    style: str
    settings: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelSelection:
    model: str
    instruction_format: str
    parameters_bn: float
    settings: Mapping[str, object]
    score: float
    eligible_worker_count: int
    eligible_threads: int
    eligible_tokens_per_second: float
    estimated_wait_seconds: float
    selected_at: float


@dataclass(slots=True)
class _StickySelection:
    selection: ModelSelection
    last_used_at: float


@dataclass(frozen=True, slots=True)
class _Candidate:
    live: LiveModel
    reference: ModelReference


@dataclass(frozen=True, slots=True)
class _LastFailure:
    model: str
    task: str
    error_kind: str
    recorded_at: float


_BACKEND_PREFIXES = frozenset({"aphrodite", "koboldcpp"})
_QUANT_SUFFIX_RE = re.compile(r"(?i)(?:[-_](?:i?q\d|q\d+k)[a-z0-9_.-]*|\.gguf)$")
_RECENT_FAILURE_SECONDS = 3_600.0
_ROUTE_FAILURE_DEMOTION_SECONDS = 300.0


def _bounded_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _strip_backend_prefix(name: str) -> str:
    clean = name.strip().replace("\\", "/")
    first, separator, remainder = clean.partition("/")
    if separator and first.casefold() in _BACKEND_PREFIXES:
        return remainder
    return clean


def _canonical_model_name(name: str) -> str:
    return _QUANT_SUFFIX_RE.sub("", _strip_backend_prefix(name)).casefold()


def _model_slug(name: str) -> str:
    return _canonical_model_name(name).rsplit("/", 1)[-1]


def parse_live_models(rows: object) -> list[LiveModel]:
    if not isinstance(rows, list):
        return []
    parsed: list[LiveModel] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        model_type = str(item.get("type", "text")).casefold()
        if not isinstance(name, str) or not name.strip() or model_type != "text":
            continue
        parsed.append(
            LiveModel(
                name=name.strip(),
                count=max(0, _bounded_int(item.get("count"))),
                performance=max(0.0, _bounded_float(item.get("performance"))),
                queued=max(0.0, _bounded_float(item.get("queued"))),
                jobs=max(0.0, _bounded_float(item.get("jobs"))),
                eta=max(0, _bounded_int(item.get("eta"))),
            )
        )
    return parsed


def parse_workers(rows: object) -> list[object]:
    """Compatibility shim: worker metadata no longer participates in routing."""

    del rows
    return []


def _split_tags(value: str) -> frozenset[str]:
    return frozenset(
        token.strip().casefold()
        for token in re.split(r"[,;|]", value or "")
        if token.strip()
    )


def _reference_rank(item: ModelReference) -> tuple[bool, bool, int, str]:
    return (
        not is_supported_instruction_format(item.instruction_format),
        not bool(item.instruction_format),
        len(item.name),
        item.name.casefold(),
    )


def parse_reference_csv(text: str) -> list[ModelReference]:
    if not isinstance(text, str) or len(text) > 4_000_000:
        return []
    parsed: list[ModelReference] = []
    for row in csv.DictReader(io.StringIO(text)):
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        raw_settings = row.get("settings") or ""
        settings: Mapping[str, object] = {}
        if raw_settings:
            try:
                value = json.loads(raw_settings)
            except json.JSONDecodeError:
                value = {}
            if isinstance(value, dict):
                settings = value
        parsed.append(
            ModelReference(
                name=name,
                parameters_bn=_bounded_float(row.get("parameters_bn"), -1.0),
                instruction_format=(row.get("instruct_format") or "").strip(),
                tags=_split_tags(row.get("tags") or ""),
                style=(row.get("style") or "").strip().casefold(),
                settings=settings,
            )
        )
    return parsed


class HordeRouter:
    """Small RP-first router; Horde remains authoritative about worker capacity."""

    def __init__(
        self,
        *,
        metadata_ttl_seconds: int,
        sticky_seconds: int,
        min_parameters_bn: float,
        max_selections: int,
        max_metrics: int,
        trusted_workers: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.metadata_ttl_seconds = max(1, int(metadata_ttl_seconds))
        self.sticky_seconds = max(1, int(sticky_seconds))
        self.min_parameters_bn = max(7.0, float(min_parameters_bn))
        self.max_selections = max(1, int(max_selections))
        # Accepted for the existing Settings/provider construction boundary. The
        # lean router neither predicts worker capacity nor persists model metrics.
        del max_metrics, trusted_workers
        self._clock = clock
        self._live_models: tuple[LiveModel, ...] = ()
        self._reference_exact: dict[str, ModelReference] = {}
        self._reference_slug: dict[str, ModelReference] = {}
        self._metadata_refreshed_at: float | None = None
        self._eligible_candidate_count = 0
        self._selections: OrderedDict[tuple[str, str], _StickySelection] = OrderedDict()
        self._last_failure: _LastFailure | None = None

    def needs_refresh(self, *, force: bool = False) -> bool:
        if force or self._metadata_refreshed_at is None:
            return True
        return self._clock() - self._metadata_refreshed_at >= self.metadata_ttl_seconds

    def update(
        self,
        live_models: Iterable[LiveModel],
        references: Iterable[ModelReference],
        workers: Iterable[object] = (),
    ) -> None:
        # Worker rows are deliberately ignored. A live model plus a successful
        # Horde submission is more reliable than reconstructing backend capacity
        # from eventually consistent worker metadata.
        del workers
        self._live_models = tuple(live_models)
        exact_groups: dict[str, list[ModelReference]] = {}
        slug_groups: dict[str, list[ModelReference]] = {}
        for reference in references:
            exact_groups.setdefault(_canonical_model_name(reference.name), []).append(reference)
            slug_groups.setdefault(_model_slug(reference.name), []).append(reference)
        self._reference_exact = {
            key: sorted(items, key=_reference_rank)[0]
            for key, items in exact_groups.items()
        }
        self._reference_slug = {
            key: sorted(items, key=_reference_rank)[0]
            for key, items in slug_groups.items()
        }
        self._metadata_refreshed_at = self._clock()

    def update_metadata(
        self,
        live_models: Iterable[LiveModel],
        references: Iterable[ModelReference],
        workers: Iterable[object] = (),
    ) -> None:
        """Compatibility name used by the current Horde provider."""

        self.update(live_models, references, workers)

    def _reference_for(self, live_name: str) -> ModelReference | None:
        return self._reference_exact.get(_canonical_model_name(live_name)) or self._reference_slug.get(
            _model_slug(live_name)
        )

    @staticmethod
    def _preference_tier(reference: ModelReference) -> int:
        tags = set(reference.tags)
        tags.update(_split_tags(reference.style))
        if "roleplay" in tags:
            return 2
        if "chat" in tags:
            return 1
        return 0

    def _candidates(self, excluded_models: frozenset[str]) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for live in self._live_models:
            if live.name.casefold() in excluded_models or live.count <= 0:
                continue
            reference = self._reference_for(live.name)
            if (
                reference is None
                or reference.parameters_bn < self.min_parameters_bn
                or not is_supported_instruction_format(reference.instruction_format)
            ):
                continue
            candidates.append(_Candidate(live=live, reference=reference))
        self._eligible_candidate_count = len(candidates)
        return candidates

    def _candidate_order(
        self,
        candidate: _Candidate,
        now: float,
    ) -> tuple[int, int, int, int, float, str]:
        recently_failed = bool(
            self._last_failure is not None
            and self._last_failure.model.casefold() == candidate.live.name.casefold()
            and now - self._last_failure.recorded_at < _ROUTE_FAILURE_DEMOTION_SECONDS
        )
        # A single recent failure tries another live candidate when one exists.
        # Otherwise semantic suitability dominates and ETA/count/size break ties.
        return (
            int(recently_failed),
            -HordeRouter._preference_tier(candidate.reference),
            candidate.live.eta,
            -candidate.live.count,
            -candidate.reference.parameters_bn,
            candidate.live.name.casefold(),
        )

    @staticmethod
    def _wait_budget(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            budget = float(value)
        except (TypeError, ValueError):
            return None
        return budget if math.isfinite(budget) and budget > 0 else None

    def _selection(self, candidate: _Candidate, *, selected_at: float) -> ModelSelection:
        live = candidate.live
        reference = candidate.reference
        return ModelSelection(
            model=live.name,
            instruction_format=reference.instruction_format,
            parameters_bn=reference.parameters_bn,
            settings=reference.settings,
            score=float(self._preference_tier(reference)),
            # Compatibility fields expose aggregate live metadata only. They are
            # not worker eligibility predictions and do not affect routing.
            eligible_worker_count=live.count,
            eligible_threads=0,
            eligible_tokens_per_second=live.performance,
            estimated_wait_seconds=float(live.eta),
            selected_at=selected_at,
        )

    def _evict_idle_selections(self, now: float) -> None:
        for key, sticky in tuple(self._selections.items()):
            if now - sticky.last_used_at >= self.sticky_seconds:
                self._selections.pop(key, None)

    def select(
        self,
        *,
        scope: str,
        task: str,
        context_tokens: int,
        max_tokens: int,
        excluded_models: Iterable[str] = (),
        max_wait_seconds: float | None = None,
    ) -> ModelSelection:
        del context_tokens, max_tokens
        now = self._clock()
        self._evict_idle_selections(now)
        excluded = frozenset(str(model).casefold() for model in excluded_models)
        candidates = self._candidates(excluded)
        by_name = {candidate.live.name: candidate for candidate in candidates}
        clean_task = str(task)[:32] or "chat"
        clean_scope = str(scope)[:160] or "global"
        key = (clean_task, clean_scope)

        sticky = self._selections.get(key)
        if sticky is not None and sticky.selection.model in by_name:
            sticky.selection = self._selection(
                by_name[sticky.selection.model],
                selected_at=sticky.selection.selected_at,
            )
            sticky.last_used_at = now
            self._selections.move_to_end(key)
            return sticky.selection
        self._selections.pop(key, None)

        if not candidates:
            raise NoEligibleHordeModel(
                "No active 7B+ AI Horde model has a supported prompt format"
            )

        wait_budget = self._wait_budget(max_wait_seconds)
        if wait_budget is not None:
            timely = [candidate for candidate in candidates if candidate.live.eta <= wait_budget]
            if timely:
                candidates = timely
        chosen = sorted(candidates, key=lambda candidate: self._candidate_order(candidate, now))[0]
        selection = self._selection(chosen, selected_at=now)
        self._selections[key] = _StickySelection(selection=selection, last_used_at=now)
        self._selections.move_to_end(key)
        while len(self._selections) > self.max_selections:
            self._selections.popitem(last=False)
        return selection

    def clear_selection(self, *, scope: str, task: str) -> None:
        self._selections.pop((str(task)[:32] or "chat", str(scope)[:160] or "global"), None)

    def record_result(
        self,
        *,
        model: str,
        task: str = "chat",
        success: bool,
        latency_seconds: float,
        error_kind: str = "",
        worker_id: str = "",
        empty: bool = False,
        malformed: bool = False,
        truncated: bool = False,
    ) -> None:
        # Keep one human-readable diagnostic, never a scoring/circuit input.
        del latency_seconds, worker_id, empty, malformed, truncated
        clean_model = str(model)[:200]
        clean_task = str(task)[:32] or "chat"
        if success:
            if (
                self._last_failure is not None
                and self._last_failure.model.casefold() == clean_model.casefold()
                and self._last_failure.task == clean_task
            ):
                self._last_failure = None
            return
        self._last_failure = _LastFailure(
            model=clean_model,
            task=clean_task,
            error_kind=str(error_kind)[:64] or "unknown",
            recorded_at=self._clock(),
        )

    def record_outcome(self, **kwargs: object) -> None:
        """Compatibility name used by the current Horde provider."""

        self.record_result(**kwargs)  # type: ignore[arg-type]

    def seed_outcomes(self, outcomes: Iterable[object]) -> None:
        # Historical metrics must not steer a fresh process or reopen a circuit.
        del outcomes

    def status(self) -> dict[str, object]:
        now = self._clock()
        self._evict_idle_selections(now)
        selected_models = [
            {
                "task": key[0],
                "scope": key[1],
                "model": sticky.selection.model,
                "format": sticky.selection.instruction_format,
                "selection_age_seconds": max(0, int(now - sticky.selection.selected_at)),
                "idle_seconds": max(0, int(now - sticky.last_used_at)),
            }
            for key, sticky in reversed(self._selections.items())
        ]
        recent_failures: list[dict[str, object]] = []
        if self._last_failure is not None:
            age = max(0.0, now - self._last_failure.recorded_at)
            if age <= _RECENT_FAILURE_SECONDS:
                recent_failures.append(
                    {
                        "task": self._last_failure.task,
                        "model": self._last_failure.model,
                        "error_kind": self._last_failure.error_kind,
                        "age_seconds": int(age),
                    }
                )
        return {
            "metadata_age_seconds": (
                None
                if self._metadata_refreshed_at is None
                else max(0, int(now - self._metadata_refreshed_at))
            ),
            "eligible_candidate_count": self._eligible_candidate_count,
            "selection_count": len(self._selections),
            "selected_models": selected_models,
            "recent_model_failures": recent_failures,
        }
