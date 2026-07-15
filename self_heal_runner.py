"""Command entry point for visible or background Codex self-healing runs."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import time
from pathlib import Path

os.environ.setdefault("HEIYUE_QUIET_LOGGER", "1")

from core.services.codex_repair import (
    CodexRepairConfig,
    CodexRepairExecutor,
    DEFAULT_WORKTREE_ROOT,
    list_run_statuses,
)
from core.services.self_healing import (
    GLOBAL_CLAIM_ENV_VAR,
    GLOBAL_TOKEN_ENV_VAR,
    adopt_global_dispatch,
    claim_global_dispatch,
    pending_dispatches,
    release_global_dispatch,
)


ROOT = Path(__file__).resolve().parent
STORAGE_ENV_VAR = "HEIYUE_SELF_HEALING_STORAGE_ROOT"


def _storage_root() -> Path:
    return Path(
        os.environ.get(
            STORAGE_ENV_VAR,
            str(ROOT / "logs" / "self_healing"),
        )
    ).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex self-healing runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    process = subparsers.add_parser("process", help="process one incident")
    process.add_argument("incident", help="incident JSON path or id")
    process.add_argument(
        "--enabled",
        action="store_true",
        help="explicit safety acknowledgement required to run Codex",
    )
    process.add_argument(
        "--mode",
        choices=("diagnose", "repair"),
        default="diagnose",
    )
    process.add_argument("--codex-bin", default=None)
    process.add_argument("--timeout", type=float, default=900.0)
    process.add_argument(
        "--visible",
        action="store_true",
        help="show operator progress and wait for Enter before closing",
    )

    drain = subparsers.add_parser(
        "drain", help="process the internal incident queue serially"
    )
    drain.add_argument("--enabled", action="store_true")
    drain.add_argument("--codex-bin", default=None)
    drain.add_argument("--timeout", type=float, default=900.0)
    drain.add_argument("--visible", action="store_true")

    status = subparsers.add_parser("status", help="show recent run states")
    status.add_argument("--limit", type=int, default=20)
    return parser


def _execute_incident(
    incident: str,
    *,
    storage_root: Path,
    enabled: bool,
    mode: str,
    codex_bin: str | None,
    timeout: float,
    progress_reporter=None,
):
    config = CodexRepairConfig(
        repository_root=ROOT,
        storage_root=storage_root,
        worktree_root=DEFAULT_WORKTREE_ROOT,
        enabled=enabled,
        mode=mode,
        codex_bin=codex_bin,
        codex_timeout_seconds=timeout,
    )
    executor_kwargs = {}
    if progress_reporter is not None:
        parameters = inspect.signature(CodexRepairExecutor).parameters
        if "progress_reporter" in parameters:
            executor_kwargs["progress_reporter"] = progress_reporter
    return CodexRepairExecutor(config, **executor_kwargs).process(incident)


def _show_start(incident: str, mode: str) -> None:
    print("\n=== 黑月 Codex 自愈 ===", flush=True)
    print(f"incident: {incident}", flush=True)
    print(f"mode: {mode}", flush=True)


def _show_progress(message: str) -> None:
    print(f"progress: {message}", flush=True)


def _show_result(result) -> None:
    document = result.to_dict()
    print("--- result ---", flush=True)
    print(f"status: {document.get('status') or '-'}", flush=True)
    print(
        f"branch: {document.get('branch_name') or document.get('branch') or '(detached)'}",
        flush=True,
    )
    print(f"worktree: {document.get('worktree_path') or '-'}", flush=True)
    print(f"output: {document.get('codex_output_path') or document.get('run_path') or '-'}", flush=True)


def _wait_for_visible_close(visible: bool) -> None:
    if not visible:
        return
    print("\n全局自愈锁已释放。按 Enter 关闭窗口...", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def _drain_pending(
    *,
    storage_root: Path,
    enabled: bool,
    codex_bin: str | None,
    timeout: float,
    visible: bool = False,
) -> int:
    claim_text = os.environ.get(GLOBAL_CLAIM_ENV_VAR, "")
    token = os.environ.get(GLOBAL_TOKEN_ENV_VAR, "")
    claim_path = Path(claim_text).resolve() if claim_text else None
    if claim_path is None or not token:
        claimed = claim_global_dispatch(storage_root)
        if claimed is None:
            return 2
        claim_path, token = claimed
    if not adopt_global_dispatch(claim_path, token):
        return 2

    released = False
    exit_code = 0
    incidents_dir = (storage_root / "incidents").resolve()
    try:
        while True:
            pending = pending_dispatches(storage_root)
            if not pending:
                # A short quiet period closes the enqueue-vs-release race.
                time.sleep(0.25)
                if pending_dispatches(storage_root):
                    continue
                release_global_dispatch(claim_path, token)
                released = True
                if pending_dispatches(storage_root):
                    claimed = claim_global_dispatch(storage_root)
                    if claimed is not None:
                        claim_path, token = claimed
                        if adopt_global_dispatch(claim_path, token):
                            released = False
                            continue
                break

            for queue_path, document in pending:
                mode = str(document.get("mode", "")).casefold()
                incident_path = Path(str(document.get("incident_path", ""))).resolve()
                if mode not in {"diagnose", "repair"} or not incident_path.is_relative_to(
                    incidents_dir
                ):
                    queue_path.unlink(missing_ok=True)
                    exit_code = 1
                    continue
                try:
                    if visible:
                        _show_start(
                            str(document.get("incident_id") or incident_path), mode
                        )
                    result = _execute_incident(
                        str(incident_path),
                        storage_root=storage_root,
                        enabled=enabled,
                        mode=mode,
                        codex_bin=codex_bin,
                        timeout=timeout,
                        progress_reporter=_show_progress if visible else None,
                    )
                    print(json.dumps(result.to_dict(), ensure_ascii=False))
                    if visible:
                        _show_result(result)
                    if not result.succeeded:
                        exit_code = 1
                except Exception as error:
                    print(
                        json.dumps(
                            {
                                "status": "runner_failed",
                                "incident_path": str(incident_path),
                                "error": f"{type(error).__name__}: {error}",
                            },
                            ensure_ascii=False,
                        )
                    )
                    exit_code = 1
                finally:
                    queue_path.unlink(missing_ok=True)
    finally:
        if not released:
            release_global_dispatch(claim_path, token)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    storage_root = _storage_root()
    if arguments.command == "status":
        print(
            json.dumps(
                list_run_statuses(storage_root, limit=arguments.limit),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if arguments.command == "drain":
        exit_code = _drain_pending(
            storage_root=storage_root,
            enabled=arguments.enabled,
            codex_bin=arguments.codex_bin,
            timeout=arguments.timeout,
            visible=arguments.visible,
        )
        _wait_for_visible_close(arguments.visible)
        return exit_code

    claimed = claim_global_dispatch(storage_root)
    if claimed is None:
        print(json.dumps({"status": "blocked", "reason": "runner_busy"}))
        return 2
    claim_path, token = claimed
    if not adopt_global_dispatch(claim_path, token):
        return 2
    try:
        if arguments.visible:
            _show_start(arguments.incident, arguments.mode)
        result = _execute_incident(
            arguments.incident,
            storage_root=storage_root,
            enabled=arguments.enabled,
            mode=arguments.mode,
            codex_bin=arguments.codex_bin,
            timeout=arguments.timeout,
            progress_reporter=_show_progress if arguments.visible else None,
        )
    finally:
        release_global_dispatch(claim_path, token)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if arguments.visible:
        _show_result(result)
    _wait_for_visible_close(arguments.visible)
    if result.succeeded:
        return 0
    if result.status in {"disabled", "blocked"}:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
