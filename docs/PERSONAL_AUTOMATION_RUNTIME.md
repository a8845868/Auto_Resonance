# Personal automation runtime

The default runtime mode is `PERSONAL_AUTOMATION`. `DEBUG` keeps the same
bounded action policy with additional diagnostics. `AUDIT` remains available
and retains its controlled-authority gate; audit infrastructure is not a
dependency of the default personal runtime.

## Episode boundary

A personal episode prepares instance 0 and `com.hermes.goda`, selects exactly
one MuMu V5 game window, observes one classified state at a time, performs only
an action allowed for that proven state, verifies the postcondition, and stops
at `CITY_DETAIL`. `UNKNOWN` is observation-only.

Every reversible action uses one injected `EpisodeActionBudget`. The default
limits are ten total actions, two actions per state, one click per normalized
point, two announcement dismissals, two claimed daily-check-in dismissals, and
two city-entry attempts. Planning does not consume the budget; only a confirmed
dispatch does. Reconstructing a detector, planner, handler, or runtime during
the episode must reuse the same budget object.

## State and action separation

- `StateDetector` converts decoded pixels and OCR into one runtime state.
- `ActionPlanner` selects an action only from that state and the shared budget.
- `ActionExecutor` maps and dispatches the selected point without classifying.
- `PostconditionVerifier` verifies state change without retrying.
- `RecoveryPolicy` permits only bounded, state-specific recovery.
- `RunRecorder` records observations, plans, decisions, dispatches, and results.

Announcements use the startup overlay classifier plus dynamic safe blank-region
selection. OCR boxes, the detected dialog, and high-edge areas are forbidden.
If no safe candidate exists, the announcement state is preserved and the
planner returns `OBSERVE_ONLY`. Claimed daily check-in panels use the same safe
region logic and only select a point outside the panel.

Capture payloads may be PNG or JPEG data URLs. JPEG source bytes and their hash
remain distinct from the deterministic normalized PNG representation; both
formats feed the same decoded-pixel classifier and actual-dimension coordinate
mapping.

## Offline validation

Run the deterministic generated scenarios without an emulator or input backend:

```powershell
.venv\Scripts\python.exe tools\runtime_scenario_replay.py --all
```

The normal development entry is `tools/personal_automation_episode.py`. It
requires an explicit MuMu install path and is dry-run planning unless
`--execute` is supplied. Real execution is a separate user-authorized step and
stops at `CITY_DETAIL`.
