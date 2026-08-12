"""One-shot NEMU HOME -> CITY_DETAIL live gate.

This tool never falls back to ADB input. ADB is used only for package/foreground
health and an optional read-only stale-capture cross-check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2 as cv
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.control.control as control_module
from core.control.nemu import NEMU
from core.control.nemu_receipt import DeliveryStatus
from core.services.city_entry_postcondition import CITY_ENTRY_POSTCONDITION_POLICY
from core.services.city_navigation import CityNavigationAdapter
from core.services.navigation_evidence import classify_native_accepted_no_effect
from core.services.personal_runtime_episode import RuntimeState, StateDetector


def _adb_frame_hash(backend: NEMU) -> str | None:
    adb = getattr(backend, "_health_adb", None)
    if adb is None:
        return None
    try:
        payload = adb.shell("screencap -p", decode=False)
        if isinstance(payload, str):
            return None
        marker = payload.find(b"\x89PNG")
        if marker < 0:
            return None
        image = cv.imdecode(np.frombuffer(payload[marker:], np.uint8), cv.IMREAD_COLOR)
        return hashlib.sha256(image.tobytes()).hexdigest() if image is not None else None
    except Exception:
        return None


def _base(reason: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "gate": "GATE_A_CITY_ENTRY",
        "status": "BLOCKED",
        "reason": reason,
        "initial_state": "UNKNOWN",
        "parent_control_status": "NOT_RUN",
        "nemu_down_status": "NOT_CALLED",
        "nemu_up_status": "NOT_CALLED",
        "delivery_status": "NOT_DISPATCHED",
        "post_state": "UNKNOWN",
        "ui_effect_confirmed": False,
        "evidence_invariant_check": "NOT_RUN",
        "real_ui_actions": 0,
        "same_action_retry": 0,
        "adb_input_fallback_actions": 0,
        "adb_screenshot_crosscheck": "NOT_RUN",
        "business_actions": 0,
        "buy": 0,
        "sell": 0,
        "depart": 0,
        "fatigue_consume": 0,
        "reward_claim": 0,
        "mail_claim": 0,
        "post_canonical_leaf_state": "UNKNOWN",
        "post_context_state": "UNKNOWN",
        "city_entry_verified": False,
        "exact_expected_leaf_match": False,
        "gate_postcondition_policy_id": CITY_ENTRY_POSTCONDITION_POLICY.policy_id,
    }


def _evaluate_navigation_postcondition(navigation):
    evidence = navigation.evidence
    return CITY_ENTRY_POSTCONDITION_POLICY.evaluate(
        navigation.post_canonical_leaf_state or navigation.state,
        frame_is_fresh=bool(
            evidence
            and evidence.post_observations
            and evidence.post_observations[-1].source_capture_id
        ),
        frame_changed=bool(evidence and evidence.post_frame_changed),
        evidence_invariant_check=(
            evidence.evidence_invariant_check if evidence else "NOT_RUN"
        ),
    )


def run(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    result = _base("preflight_not_run")
    try:
        if not control_module.connect():
            result["reason"] = "backend_connect_failed"
            return result
        backend = control_module.control
        if not isinstance(backend, NEMU):
            result["reason"] = "nemu_backend_required_adb_input_fallback_forbidden"
            return result
        if int(getattr(backend.device, "index", -1)) != 0:
            result["reason"] = "instance_index_mismatch"
            return result
        healthy, reasons = backend.input_health()
        if not healthy:
            result["reason"] = "nemu_health_gate_failed:" + ",".join(reasons)
            return result
        initial = control_module.screenshot()
        detected = StateDetector().detect(initial)
        result["initial_state"] = detected.state.value
        if detected.state is not RuntimeState.HOME_READY:
            result["reason"] = "initial_state_not_home_ready"
            return result
        adb_pre_hash = _adb_frame_hash(backend)
        adapter = CityNavigationAdapter(
            frame_provider=control_module.screenshot,
            tap=control_module.input_tap,
            geometry_provider=control_module.current_display_geometry,
            dispatch_backend="NEMU",
            timeout=30.0,
            max_attempts=64,
            require_station_confirmation=False,
        )
        navigation = adapter.enter_city()
        receipt = backend.last_touch_receipt
        result["parent_control_status"] = (
            "PASS" if navigation.dispatch_count == 1 else
            "FAIL" if "parent_control" in navigation.reason else "UNKNOWN"
        )
        result["post_state"] = navigation.state.value
        result["real_ui_actions"] = navigation.dispatch_count
        if receipt is not None:
            result["nemu_down_status"] = receipt.touch_down_status
            result["nemu_up_status"] = receipt.touch_up_status
            result["delivery_status"] = receipt.delivery_status
            result["receipt"] = receipt.to_dict()
        evidence = navigation.evidence
        if evidence is not None:
            result["ui_effect_confirmed"] = evidence.touch_effect_observed
            result["evidence_invariant_check"] = evidence.evidence_invariant_check
            result["evidence_attempt_id"] = evidence.attempt_id
        postcondition = _evaluate_navigation_postcondition(navigation)
        result["post_canonical_leaf_state"] = postcondition.post_canonical_leaf_state
        result["post_context_state"] = postcondition.post_context_state
        result["city_entry_verified"] = postcondition.city_entry_verified
        result["exact_expected_leaf_match"] = postcondition.exact_expected_leaf_match
        result["gate_postcondition_policy_id"] = postcondition.policy_id
        if (
            receipt is not None
            and receipt.delivery_status == DeliveryStatus.NATIVE_ACCEPTED.value
            and not result["ui_effect_confirmed"]
        ):
            adb_post_hash = _adb_frame_hash(backend)
            crosscheck = classify_native_accepted_no_effect(
                native_accepted=True,
                nemu_frame_changed=False,
                adb_crosscheck_available=adb_pre_hash is not None and adb_post_hash is not None,
                adb_frame_changed=(
                    adb_pre_hash != adb_post_hash
                    if adb_pre_hash is not None and adb_post_hash is not None
                    else None
                ),
            )
            result["adb_screenshot_crosscheck"] = crosscheck
        passed = all((
            navigation.status == "PASS",
            postcondition.city_entry_verified,
            navigation.dispatch_count == 1,
            receipt is not None,
            receipt.touch_down_status == "ACCEPTED",
            receipt.touch_up_status == "ACCEPTED",
            receipt.delivery_status == DeliveryStatus.NATIVE_ACCEPTED.value,
            result["ui_effect_confirmed"] is True,
            result["evidence_invariant_check"] == "PASS",
        ))
        result["status"] = "PASS" if passed else "BLOCKED"
        result["reason"] = "city_entry_trusted_context_live_proven" if passed else navigation.reason
        return result
    finally:
        try:
            control_module.kill()
        except Exception:
            pass
        (output / "GATE_A_RESULT.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
