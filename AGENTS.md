# Repository guidance

## Development

- Use `.venv\Scripts\python.exe` for Python and pytest commands on Windows.
- Keep fixes narrow, preserve unrelated working-tree changes, and add a
  regression test for every behavior change.
- Prefer deterministic unit tests. Do not require a live emulator for normal
  test validation.

## Self-healing repair safety

When `HEIYUE_CODEX_REPAIR=1` is present, the task is an isolated repair run:

- Never start `gui.py`, `gui_launcher.pyw`, `debug_runner.py`, the game, ADB,
  NEMU, MuMuManager, or any emulator-control command.
- Never unset or bypass `HEIYUE_CODEX_REPAIR` or `HEIYUE_RUNTIME_DIR`.
- Do not weaken or modify `core/services/repair_safety.py`, its callers, the
  runtime ownership checks, or the self-healing safety policy.
- Do not access account credentials, CDKs, cookies, tokens, or unrelated user
  files. Network access is not part of a repair run.
- Do not commit, merge, push, apply a patch to the main worktree, restart the
  automation, or delete the review worktree.
- Diagnose from the supplied incident evidence, make the smallest code change
  inside the isolated worktree, add a regression test, and leave the diff for
  human review.
