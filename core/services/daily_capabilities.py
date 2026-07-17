"""Production registry for safe automations that can advance daily objectives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from core.services.daily_rewards import DailyProgressSnapshot, RewardStrategy


@dataclass(frozen=True)
class DailyCapability:
    task_key: str
    enabled: bool
    automation_available: bool
    activity_contribution: int
    handbook_contribution: int
    premium_currency_risk: bool
    prerequisites: tuple[str, ...]
    run_factory: Callable[[], object]


def select_daily_capabilities(
    capabilities: Iterable[DailyCapability],
    snapshot: DailyProgressSnapshot | None,
    strategy: RewardStrategy,
    *,
    satisfied_prerequisites: frozenset[str] = frozenset(),
) -> list[DailyCapability]:
    if strategy is RewardStrategy.CLAIM_ONLY:
        return []
    activity_incomplete = snapshot is None or (
        snapshot.daily_activity_current is None
        or snapshot.daily_activity_max is None
        or snapshot.daily_activity_current < snapshot.daily_activity_max
    )
    handbook_incomplete = snapshot is None or (
        snapshot.handbook_daily_tasks_completed is None
        or snapshot.handbook_daily_tasks_total is None
        or snapshot.handbook_daily_tasks_completed
        < snapshot.handbook_daily_tasks_total
    )
    selected = []
    for capability in capabilities:
        if not capability.enabled or not capability.automation_available:
            continue
        if capability.premium_currency_risk:
            continue
        if any(
            prerequisite not in satisfied_prerequisites
            for prerequisite in capability.prerequisites
        ):
            continue
        contributes = (
            activity_incomplete and capability.activity_contribution > 0
        ) or (handbook_incomplete and capability.handbook_contribution > 0)
        if contributes:
            selected.append(capability)
    return selected
