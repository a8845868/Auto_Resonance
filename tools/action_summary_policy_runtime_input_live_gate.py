"""One bounded read-only live gate for action-summary runtime input assembly.

The only physical inputs this tool can cause are proven V2A navigation edges
needed to reach ACTION_SUMMARY_VISIBLE.  Once that page is reached it captures
at most two frames, assembles provenance-bound policy inputs, and stops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys
import time
from typing import Mapping

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.action_summary_policy_runtime_inputs import (
    AssemblyIntegrityStatus,
    PolicyInputReadiness,
    assemble_action_summary_policy_runtime_inputs,
)
from core.services.action_summary_product_model import (
    ActionSummaryPageModel,
    action_summary_title_hash,
    observe_action_summary_page,
)


DEFAULT_OUTPUT_DIR = (
    ROOT
    / "dist"
    / "debug_private"
    / "action-summary-policy-runtime-input-assembly-live-gate-v1"
)
_ALLOWED_READINESS = {
    PolicyInputReadiness.READY_FOR_POLICY_EVALUATION,
    PolicyInputReadiness.BLOCKED_MISSING_FACTS,
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _blocked(reason: str) -> dict[str, object]:
    return {
        "LIVE_ASSEMBLY_GATE": "BLOCKED",
        "BLOCK_REASON_CODES": [reason],
        "ASSEMBLY_STATUS": "BLOCKED",
        "POLICY_INPUT_READINESS": "NOT_RUN",
        "MISSING_RUNTIME_INPUTS": [],
        "LIVE_READ_ONLY_OBSERVATIONS": 0,
        "NAVIGATION_ACTIONS": 0,
        "ACTION_SUMMARY_PAGE_INPUTS": 0,
        "REAL_UI_ACTIONS": 0,
        "EXECUTION_AUTHORIZED": "NO",
        "AUTHORIZATION_ISSUED": "NO",
        "BUSINESS_ACTIONS": 0,
        "IRREVERSIBLE_ACTIONS": 0,
        "POLICY_EVALUATOR_CALLED": "NO",
        "EXECUTOR_CONNECTED": "NO",
    }


def _load_policy_snapshot(
    path: Path,
    captured_at: str,
    siege_tasks: tuple[str, ...],
) -> dict[str, object]:
    payload = path.read_bytes()
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("app_config_not_mapping")
    resident = document.get("ResidentActivity")
    resident = resident if isinstance(resident, Mapping) else {}
    selected_task = resident.get("Task")
    selected_task = (
        selected_task.strip()
        if isinstance(selected_task, str) and selected_task.strip()
        else None
    )
    source_hash = _sha256(payload)
    policy: dict[str, object] = {
        "schema_version": "1.0",
        "policy_id": "resident-activity-app-config",
        "policy_version": source_hash[:16],
        "revision": source_hash,
        "captured_at": captured_at,
        "source_config_sha256": source_hash,
    }
    if selected_task in siege_tasks:
        title_hash = action_summary_title_hash(selected_task)
        policy.update(
            requested_known_task_id=f"TASK_{title_hash[:16].upper()}",
            requested_task_title_hash=title_hash,
        )
    return {"ActionSummaryPolicy": policy}


def _trusted_page(model: ActionSummaryPageModel) -> bool:
    return bool(
        model.page_state == "ACTION_SUMMARY_VISIBLE"
        and not model.overlay_states
        and model.source_capture_id
        and model.source_frame_sha256
        and model.captured_at
        and model.model_freshness_token
    )


def _page_summary(
    model: ActionSummaryPageModel,
    siege_tasks: tuple[str, ...],
) -> dict[str, object]:
    known_hashes = {action_summary_title_hash(title) for title in siege_tasks}
    title_counts: dict[str, int] = {}
    for card in model.task_cards:
        title_counts[card.title_hash] = title_counts.get(card.title_hash, 0) + 1
    known_count = sum(card.title_hash in known_hashes for card in model.task_cards)
    resolved_attempts = sum(
        card.remaining_attempts is not None and card.total_attempts is not None
        for card in model.task_cards
    )
    return {
        "ACTIVITY_FAMILY": model.activity_family,
        "VISIBLE_TASK_CARD_COUNT": model.visible_task_cards,
        "KNOWN_TASK_COUNT": known_count,
        "OBSERVED_ONLY_TASK_COUNT": len(model.task_cards) - known_count,
        "AMBIGUOUS_TASK_COUNT": sum(
            count for count in title_counts.values() if count > 1
        ),
        "PAGE_ATTEMPTS": None,
        "PAGE_ATTEMPTS_SCOPE": "PAGE_ONLY_NOT_COPIED_TO_CARDS",
        "CARD_ATTEMPTS_RESOLVED": resolved_attempts,
        "CARD_ATTEMPTS_UNKNOWN": len(model.task_cards) - resolved_attempts,
        "COST_AMOUNTS": [card.cost for card in model.task_cards],
        "COST_RESOURCE_IDS": [card.cost_resource_id for card in model.task_cards],
        "RESOURCE_BALANCES": [None for _card in model.task_cards],
        "RESOURCE_SUFFICIENCY": ["UNKNOWN" for _card in model.task_cards],
        "REWARD_TARGETS": [None for _card in model.task_cards],
        "REWARD_STATES": [card.reward_state.value for card in model.task_cards],
        "FATIGUE_CURRENT": None,
        "FATIGUE_BUDGET": None,
    }


def run(*, adb_port: int = 16384) -> dict[str, object]:
    from auto.resident_activity import (
        ResidentActivityAutomation,
        SIEGE_TASKS,
        ScreenDriver,
    )
    from core.control.control import connect_adb, get_runtime_device
    from core.services.emulator_lifecycle import GAME_PACKAGE, MuMuManagerClient

    device = get_runtime_device()
    if int(device.index) != 0:
        return _blocked("instance_index_mismatch")
    manager = MuMuManagerClient(device, correlation_id="POLICY-INPUT-LIVE-GATE-V1")
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

    config_captured_at = _now()
    policy_snapshot = _load_policy_snapshot(
        ROOT / "config" / "app.json",
        config_captured_at,
        SIEGE_TASKS,
    )
    driver = ScreenDriver()
    automation = ResidentActivityAutomation(driver, use_proven_edge_planner=True)
    navigation_success = automation.open_action_summary()
    navigation = automation.last_capability_navigation_result
    if navigation is None:
        return _blocked("capability_navigation_result_missing")
    navigation_actions = int(navigation.physical_dispatches)
    if not navigation_success or navigation.final_state != "ACTION_SUMMARY_VISIBLE":
        result = _blocked(f"navigation_blocked:{navigation.reason}")
        result.update(
            NAVIGATION_ACTIONS=navigation_actions,
            REAL_UI_ACTIONS=navigation_actions,
            NAVIGATION_RESULT=navigation.to_dict(),
        )
        return result

    observations = 0
    model: ActionSummaryPageModel | None = None
    for index in range(2):
        observations += 1
        model = observe_action_summary_page(driver.capture_frame())
        if _trusted_page(model):
            break
        if index == 0:
            time.sleep(1.0)
    assert model is not None
    if not _trusted_page(model):
        result = _blocked("fresh_action_summary_page_not_confirmed")
        result.update(
            LIVE_READ_ONLY_OBSERVATIONS=observations,
            NAVIGATION_ACTIONS=navigation_actions,
            REAL_UI_ACTIONS=navigation_actions,
            FINAL_PAGE_STATE=model.page_state,
        )
        return result

    assembly = assemble_action_summary_policy_runtime_inputs(
        model,
        policy_snapshot,
        resource_observation=None,
        assembled_at=_now(),
    )
    gate_pass = (
        assembly.assembly_status is AssemblyIntegrityStatus.PASS
        and assembly.policy_input_readiness in _ALLOWED_READINESS
    )
    document = {
        "LIVE_ASSEMBLY_GATE": "PASS" if gate_pass else "FAIL",
        **_page_summary(model, SIEGE_TASKS),
        "LIVE_READ_ONLY_OBSERVATIONS": observations,
        "NAVIGATION_ACTIONS": navigation_actions,
        "ACTION_SUMMARY_PAGE_INPUTS": 0,
        "REAL_UI_ACTIONS": navigation_actions,
        "ASSEMBLY_STATUS": assembly.assembly_status.value,
        "POLICY_INPUT_READINESS": assembly.policy_input_readiness.value,
        "MISSING_RUNTIME_INPUTS": list(assembly.missing_runtime_inputs),
        "BLOCK_REASON_CODES": list(assembly.reason_codes),
        "POLICY_CONFIG_ID": assembly.policy_config_id,
        "POLICY_CONFIG_VERSION": assembly.policy_config_version,
        "POLICY_CONFIG_FINGERPRINT": assembly.policy_config_sha256,
        "POLICY_CONFIG_FINGERPRINT_BOUND": (
            "YES" if assembly.policy_config_fingerprint_bound else "NO"
        ),
        "POLICY_TARGET_MATCH_STATUS": assembly.policy_target_match_status.value,
        "POLICY_TARGET_CARD_MATCH_KEY": assembly.policy_target_card_match_key,
        "PROVENANCE": {
            "assembly_schema_version": assembly.schema_version,
            "assembled_at": assembly.assembled_at,
            "page_model_capture_id": assembly.source_capture_id,
            "page_model_frame_sha256": assembly.source_frame_sha256,
            "page_model_captured_at": assembly.page_model_captured_at,
            "page_model_freshness_token": assembly.source_model_freshness_token,
            "resource_observation_capture_id": assembly.resource_observation_capture_id,
            "resource_observation_frame_sha256": assembly.resource_observation_frame_sha256,
            "resource_observation_captured_at": assembly.resource_observation_captured_at,
            "resource_observation_freshness_token": (
                assembly.resource_observation_freshness_token
            ),
            "source_relationship": assembly.source_relationship.value,
            "source_age_seconds": assembly.source_age_seconds,
            "maximum_allowed_age_seconds": assembly.maximum_allowed_age_seconds,
            "policy_config_captured_at": assembly.policy_config_captured_at,
        },
        "STALE_RESOURCE_INPUT_ACCEPTED": (
            "YES" if assembly.stale_resource_input_accepted else "NO"
        ),
        "EXECUTION_AUTHORIZED": "NO",
        "AUTHORIZATION_ISSUED": "NO",
        "BUSINESS_ACTIONS": 0,
        "IRREVERSIBLE_ACTIONS": 0,
        "POLICY_EVALUATOR_CALLED": "NO",
        "EXECUTOR_CONNECTED": "NO",
        "NAVIGATION_RESULT": navigation.to_dict(),
    }
    return document


def _write_outputs(output: Path, document: dict[str, object]) -> None:
    _write_json(output / "RUN_RESULT.json", document)
    _write_json(output / "FINAL_RESULT.json", document)
    report = "# Action Summary Policy Runtime Input Live Gate\n\n" + "\n".join(
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in document.items()
        if key != "NAVIGATION_RESULT"
    ) + "\n"
    (output / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
    checksums = {
        path.name: _sha256(path.read_bytes())
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
    return 0 if document.get("LIVE_ASSEMBLY_GATE") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
