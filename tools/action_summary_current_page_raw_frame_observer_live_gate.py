"""One bounded live read-only gate for the Action Summary raw observer.

V2A may navigate to ACTION_SUMMARY_VISIBLE.  After navigation this tool takes
exactly one fresh capture, performs one cached OCR read, observes current-page
facts, normalizes them, builds an acquisition plan, and stops.  It never clicks
inside Action Summary and never evaluates or executes business policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Mapping

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.action_summary_advisory_policy import FactAcquisitionRequest
from core.services.action_summary_missing_fact_acquisition import (
    build_missing_fact_acquisition_plan,
)
from core.services.action_summary_product_model import observe_action_summary_page
from core.services.action_summary_raw_frame_observer import (
    normalize_raw_action_summary_visual_observation,
    observe_action_summary_current_page_visuals,
    raw_observation_fingerprint,
)


DEFAULT_OUTPUT_DIR = (
    ROOT
    / "dist"
    / "debug_private"
    / "action-summary-current-page-raw-frame-observer-live-gate-v1"
)
_POLICY_FINGERPRINT = hashlib.sha256(
    b"ACTION_SUMMARY_CURRENT_PAGE_RAW_FRAME_OBSERVER_V1_OBSERVE_ONLY"
).hexdigest()
_FACT_IDS = (
    "resource_cost_unknown",
    "resource_identity_unknown",
    "resource_balance_unknown",
    "reward_target_unknown",
)


class _SingleOcrFrame:
    """Reuse one captured frame and one underlying OCR result."""

    def __init__(self, source: object) -> None:
        self.image = getattr(source, "image", None)
        self.source_capture_id = getattr(source, "source_capture_id", None)
        self.raw_frame_hash = getattr(source, "raw_frame_hash", "")
        self.captured_at = getattr(source, "captured_at", None)
        self._source = source
        self._items: tuple[object, ...] | None = None
        self.underlying_ocr_calls = 0

    def ocr(self) -> list[object]:
        if self._items is None:
            self._items = tuple(self._source.ocr())
            self.underlying_ocr_calls += 1
        return list(self._items)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _request(fact_id: str, priority: int) -> FactAcquisitionRequest:
    return FactAcquisitionRequest(
        missing_fact=fact_id,
        required_scope="CURRENT_ACTION_SUMMARY_PAGE",
        preferred_source="raw_frame_observer",
        requires_navigation=None,
        requires_page_input=False,
        requires_business_input=False,
        priority=priority,
        reason="bounded live read-only observer gate",
    )


def _blocked(reason: str) -> dict[str, object]:
    return {
        "LIVE_OBSERVER_GATE": "BLOCKED",
        "BLOCK_REASON_CODES": [reason],
        "FRAME_BINDING": "NOT_RUN",
        "NO_FALSE_FACTS_EMITTED": "NOT_RUN",
        "LIVE_RESOLVED_FACTS": [],
        "LIVE_UNRESOLVED_FACTS": list(_FACT_IDS),
        "LIVE_AMBIGUOUS_FACTS": [],
        "LIVE_READ_ONLY_OBSERVATIONS": 0,
        "NAVIGATION_ACTIONS": 0,
        "ACTION_SUMMARY_PAGE_INPUTS": 0,
        "BUSINESS_ACTIONS": 0,
        "IRREVERSIBLE_ACTIONS": 0,
        "POLICY_EVALUATOR_CALLED": "NO",
        "EXECUTOR_CONNECTED": "NO",
        "AUTHORIZATION_ISSUED": "NO",
    }


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def run(*, adb_port: int = 16384) -> dict[str, object]:
    from auto.resident_activity import ResidentActivityAutomation, ScreenDriver
    from core.control.control import connect_adb, get_runtime_device
    from core.services.emulator_lifecycle import GAME_PACKAGE, MuMuManagerClient

    device = get_runtime_device()
    if int(device.index) != 0:
        return _blocked("instance_index_mismatch")
    manager = MuMuManagerClient(
        device, correlation_id="ACTION-SUMMARY-RAW-OBSERVER-LIVE-GATE-V1"
    )
    info = manager.info()
    game = manager.game_info(GAME_PACKAGE)
    if not bool(info.get("is_process_started")) or not bool(
        info.get("is_android_started")
    ):
        return _blocked("instance_zero_not_running")
    if str(game.get("state", "")).casefold() != "running":
        return _blocked("target_package_not_running")
    if not connect_adb(adb_port):
        return _blocked("instance_zero_backend_connect_failed")

    driver = ScreenDriver()
    automation = ResidentActivityAutomation(driver, use_proven_edge_planner=True)
    navigation_success = automation.open_action_summary()
    navigation = automation.last_capability_navigation_result
    if navigation is None:
        return _blocked("capability_navigation_result_missing")
    navigation_actions = int(navigation.physical_dispatches)
    if not navigation_success or navigation.final_state != "ACTION_SUMMARY_VISIBLE":
        document = _blocked(f"navigation_blocked:{navigation.reason}")
        document.update(
            NAVIGATION_ACTIONS=navigation_actions,
            NAVIGATION_RESULT=navigation.to_dict(),
        )
        return document

    captured = _SingleOcrFrame(driver.capture_frame())
    model = observe_action_summary_page(captured)
    observation = observe_action_summary_current_page_visuals(
        captured, page_model=model
    )
    runtime_fingerprint = raw_observation_fingerprint(observation)
    facts = normalize_raw_action_summary_visual_observation(
        observation, runtime_input_fingerprint=runtime_fingerprint
    )
    generated_at = _now()
    plan = build_missing_fact_acquisition_plan(
        tuple(_request(fact_id, index) for index, fact_id in enumerate(_FACT_IDS, 1)),
        observations=facts,
        policy_fingerprint=_POLICY_FINGERPRINT,
        runtime_input_fingerprint=runtime_fingerprint,
        generated_at=generated_at,
    )

    frame_binding = bool(
        observation.source_capture_id == model.source_capture_id
        and observation.source_frame_sha256 == model.source_frame_sha256
    )
    false_fact = any(
        fact.fact_id != "resource_cost_unknown"
        or fact.value().get("resource_id") != "UNKNOWN"
        or type(fact.value().get("resource_cost_per_run")) is not int
        or fact.value().get("resource_cost_per_run", -1) < 0
        for fact in facts
    )
    ambiguous = sorted({
        value.fact_type for value in observation.ambiguous_candidates
    })
    cost_resolved = bool(observation.resource_cost_observations) and not ambiguous
    resolved = ["resource_cost_unknown"] if cost_resolved else []
    unresolved = [fact_id for fact_id in _FACT_IDS if fact_id not in resolved]
    gate_pass = bool(
        observation.observation_status.startswith("PASS")
        and frame_binding
        and not false_fact
        and captured.underlying_ocr_calls == 1
        and observation.capture_calls == 0
        and observation.page_input_dispatches == 0
        and observation.business_dispatches == 0
        and observation.irreversible_actions == 0
    )
    return {
        "LIVE_OBSERVER_GATE": "PASS" if gate_pass else "FAIL",
        "BLOCK_REASON_CODES": [] if gate_pass else ["observer_gate_invariant_failed"],
        "FRAME_BINDING": "PASS" if frame_binding else "FAIL",
        "NO_FALSE_FACTS_EMITTED": "YES" if not false_fact else "NO",
        "OBSERVATION_STATUS": observation.observation_status,
        "LIVE_RESOLVED_FACTS": resolved,
        "LIVE_UNRESOLVED_FACTS": unresolved,
        "LIVE_AMBIGUOUS_FACTS": ambiguous,
        "LIVE_READ_ONLY_OBSERVATIONS": 1,
        "UNDERLYING_OCR_CALLS": captured.underlying_ocr_calls,
        "NAVIGATION_ACTIONS": navigation_actions,
        "ACTION_SUMMARY_PAGE_INPUTS": 0,
        "BUSINESS_ACTIONS": 0,
        "IRREVERSIBLE_ACTIONS": 0,
        "POLICY_EVALUATOR_CALLED": "NO",
        "EXECUTOR_CONNECTED": "NO",
        "AUTHORIZATION_ISSUED": "NO",
        "POLICY_EVALUATION_STILL_BLOCKED": (
            "YES" if plan.policy_evaluation_still_blocked else "NO"
        ),
        "BLOCKING_FACTS_AFTER_OBSERVATION": list(plan.unresolved_facts),
        "CONFLICTING_FACTS": list(plan.conflicting_facts),
        "ACQUIRED_FACTS": [fact.to_dict() for fact in facts],
        "RAW_OBSERVATION": observation.to_dict(),
        "ACQUISITION_PLAN": plan.to_dict(),
        "NAVIGATION_RESULT": navigation.to_dict(),
        "OBSERVED_AT": generated_at,
    }


def _write_outputs(output: Path, document: dict[str, object]) -> None:
    _write_json(output / "RUN_RESULT.json", document)
    _write_json(output / "FINAL_RESULT.json", document)
    report = "# Action Summary Raw Frame Observer Live Gate\n\n" + "\n".join(
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in document.items()
        if key not in {
            "ACQUIRED_FACTS",
            "RAW_OBSERVATION",
            "ACQUISITION_PLAN",
            "NAVIGATION_RESULT",
        }
    ) + "\n"
    (output / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
    checksums = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "SHA256SUMS.json"
    }
    _write_json(output / "SHA256SUMS.json", checksums)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--adb-port", type=int, default=16384)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        document = _blocked("explicit_execute_flag_required")
    else:
        logger.disable("core.image.ocr")
        try:
            document = run(adb_port=args.adb_port)
        except Exception as error:
            document = _blocked(f"runtime_error:{type(error).__name__}")
        finally:
            try:
                import core.control.control as control_module

                control_module.control.kill()
            except Exception:
                pass
    _write_outputs(args.output.resolve(), document)
    return 0 if document.get("LIVE_OBSERVER_GATE") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
