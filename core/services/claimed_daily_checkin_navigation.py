"""One proven claimed-daily-check-in dismissal adapter.

Only the already-claimed calendar with its explicit blank-area instruction is
eligible.  The adapter owns at most one physical dispatch and never retries it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from core.services.navigation_evidence import CoordinateChain
from core.services.personal_action_budget import EpisodeActionBudget
from core.services.personal_runtime_episode import (
    ActionPlanner,
    RuntimeAction,
    RuntimeState,
    StateDetector,
)
from core.services.read_only_policy import ActionIntent


@dataclass(frozen=True, slots=True)
class ClaimedDailyCheckinResult:
    success: bool
    reason: str
    physical_dispatches: int
    final_state: str
    evidence_ids: tuple[str, ...] = ()


def _inside(point: tuple[int, int], bbox: tuple[int, int, int, int]) -> bool:
    return bbox[0] <= point[0] < bbox[2] and bbox[1] <= point[1] < bbox[3]


def dismiss_claimed_daily_checkin(
    *,
    frame_provider: Callable[[], object],
    tap: Callable[..., object],
    geometry_provider: Callable[[], object],
    action_budget: EpisodeActionBudget,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    cancellation: Callable[[], bool] = lambda: False,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.5,
) -> ClaimedDailyCheckinResult:
    detector = StateDetector()
    planner = ActionPlanner()
    try:
        initial_frame = frame_provider()
        initial = detector.detect(initial_frame)
    except Exception:
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_initial_capture_failed", 0, "UNKNOWN"
        )
    if initial.state is not RuntimeState.DAILY_CHECKIN:
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_precondition_failed", 0, initial.state.value
        )
    first_plan = planner.plan(initial, budget=action_budget)
    if (
        first_plan.action is not RuntimeAction.DISMISS_DAILY_CHECKIN
        or first_plan.capture_point is None
    ):
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_safe_blank_region_unavailable", 0,
            initial.state.value,
        )
    try:
        fresh_frame = frame_provider()
        fresh = detector.detect(fresh_frame)
    except Exception:
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_fresh_capture_failed", 0, initial.state.value
        )
    fresh_plan = planner.plan(fresh, budget=action_budget)
    if (
        fresh.state is not RuntimeState.DAILY_CHECKIN
        or fresh_plan.action is not RuntimeAction.DISMISS_DAILY_CHECKIN
        or fresh_plan.capture_point is None
    ):
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_fresh_confirmation_failed", 0,
            fresh.state.value,
        )
    point = fresh_plan.capture_point
    if (
        fresh.dialog_bbox is None
        or _inside(point, fresh.dialog_bbox)
        or any(_inside(point, bbox) for bbox in fresh.ocr_bboxes)
    ):
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_safe_point_invalid", 0, fresh.state.value
        )
    geometry = geometry_provider()
    dimensions = (
        int(getattr(geometry, "physical_width")),
        int(getattr(geometry, "physical_height")),
    )
    chain = CoordinateChain.from_capture_point(
        point,
        capture_size=fresh.frame_dimensions,
        render_client_size=dimensions,
        device_size=dimensions,
        source_coordinate_space="CAPTURE_PIXELS",
    )
    decision = action_budget.authorize(
        state=RuntimeState.DAILY_CHECKIN.value,
        action_type=RuntimeAction.DISMISS_DAILY_CHECKIN.value,
        normalized_point=chain.render_client_point,
    )
    if not decision.allowed:
        return ClaimedDailyCheckinResult(
            False, decision.reason_code, 0, fresh.state.value
        )
    try:
        acknowledged = tap(
            chain.device_point,
            random_offset=False,
            intent=ActionIntent(
                "dialog_cancel",
                "claimed_daily_checkin_blank_region",
                "PROVEN-EDGE-CAPABILITY-NAVIGATION",
            ),
        )
    except Exception:
        action_budget.record_dispatch(decision)
        action_budget.record_result(decision, "DELIVERY_UNKNOWN")
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_delivery_unknown", 1, fresh.state.value,
            (f"daily_checkin:{fresh.frame_hash}",),
        )
    if acknowledged is False:
        action_budget.record_result(decision, "DISPATCH_REJECTED")
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_dispatch_rejected", 0, fresh.state.value
        )
    action_budget.record_dispatch(decision)
    action_budget.record_result(decision, "DISPATCHED")
    evidence_ids = (f"daily_checkin:{fresh.frame_hash}",)
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    interval = max(0.001, float(interval_seconds))
    while monotonic() < deadline:
        if cancellation():
            return ClaimedDailyCheckinResult(
                False, "cancelled", 1, fresh.state.value, evidence_ids
            )
        sleep(min(interval, max(0.0, deadline - monotonic())))
        if monotonic() >= deadline:
            break
        try:
            observed = detector.detect(frame_provider())
        except Exception:
            return ClaimedDailyCheckinResult(
                False, "daily_checkin_post_capture_failed", 1,
                fresh.state.value, evidence_ids,
            )
        if observed.state is RuntimeState.HOME_READY:
            return ClaimedDailyCheckinResult(
                True, "home_ready_after_daily_checkin", 1,
                observed.state.value, evidence_ids,
            )
        if observed.state in {RuntimeState.DAILY_CHECKIN, RuntimeState.UNKNOWN}:
            continue
        return ClaimedDailyCheckinResult(
            False, "daily_checkin_unexpected_post_state", 1,
            observed.state.value, evidence_ids,
        )
    return ClaimedDailyCheckinResult(
        False, "daily_checkin_postcondition_timeout", 1,
        fresh.state.value, evidence_ids,
    )


__all__ = ["ClaimedDailyCheckinResult", "dismiss_claimed_daily_checkin"]
