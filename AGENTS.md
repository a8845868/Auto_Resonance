# Repository guidance

## Development

- Use `.venv\Scripts\python.exe` for Python and pytest commands on Windows.
- Keep fixes narrow, preserve unrelated working-tree changes, and add a
  regression test for every behavior change.
- Prefer deterministic unit tests. Do not require a live emulator for normal
  test validation.

## Remote repository safety

- Treat this project as local-only. Local branches and local commits are
  allowed, but do not push any ref to an online remote.
- The configured `origin` repository is not owned by the local user, and the
  current GitHub identity has already been rejected with HTTP 403. Do not retry
  `git push`, change credentials, modify remotes, force-push, or attempt another
  route to publish this repository.
- A future task may fetch remote state read-only when explicitly requested, but
  it must still stop before push.
- Change this no-push policy only after the user explicitly revokes it and
  confirms authority to publish to a specific remote repository.

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
