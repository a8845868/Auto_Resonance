# NEMU Navigation Integration Scope Audit V1

## Outcome

The native NEMU capture/touch chain and the HOME backpack entry are live-proven on the frozen code identity below. This audit does not replay either live gate and does not integrate the branch into `main`.

```text
GATE_B_CODE_HEAD=89cf417da4b70b30d090e830f7192ba49d13ff63
GATE_B_CODE_TREE=65b0a6832e6b75f7de4741239d0a7327fa4336aa
LOCAL_MAIN=62020a1578c2f21356c91f0ed90c5cdc47cb3c61
GATE_B_INVENTORY_ENTRY=PASS
MERGE_READINESS=READY_FOR_EXPLICIT_INTEGRATION_PLAN
LOCAL_MAIN_MERGE=NOT_RUN
```

## Live-proof freeze

- Gate B started at `HOME_READY` and stopped at `INVENTORY_PAGE_VISIBLE`.
- The unique cube candidate was `[1081,28,1121,74]`, score `1.0`; the deterministic 20% safe inset was `[1089,37,1113,65]` and the dispatched point was `[1101,51]`.
- The target was rebound to the second fresh HOME frame. The persisted fresh-frame SHA-256 is `6ce7b8393888e430dd61fa01fe76ff81a334da7d30ff2129d76f3de6a66c89e0`.
- Native down/up both returned `ACCEPTED`. In the frozen implementation, `ACCEPTED` maps only from return code `0`; delivery was `NATIVE_ACCEPTED` and release was `CONFIRMED`.
- `CAPTURE_CALLS=4`, `DISPATCH_COUNT=1`, `REAL_UI_ACTIONS=1`, `SAME_ACTION_RETRY=0`, and `ADB_INPUT_FALLBACK_ACTIONS=0`.
- Initial/fresh capture IDs, the initial-frame hash, and the post-frame hash were not emitted by the one-shot console summary and are frozen as `UNKNOWN_NOT_PERSISTED`. They are not reconstructed from later frames.
- Manual diagnosis (`MANUAL_DIAGNOSTIC_INPUTS=2`) and the program gate (`PROGRAM_GATE_B_INPUTS=1`) remain separate.
- The failed launcher invocation occurred before Python started. It initialized no backend, captured no frame, and dispatched no input. `BOOTSTRAP_INVOCATIONS=2`, `BOOTSTRAP_FAILURES_BEFORE_PYTHON=1`, `PHYSICAL_GATE_EXECUTIONS=1`.

The HOME `资产` label is an iron-coin balance display and has no inventory-opening authority. The matched top-right cube itself is the control; expanded toolbar-parent targeting and legacy point `[1138,94]` are prohibited.

## Historical city result

The historical Gate A result remains immutable:

```text
CITY_PARENT_CONTROL_LIVE=PASS
NEMU_CITY_TOUCH_LIVE_PROOF=PASS
CITY_ENTRY_TRUSTED_TRANSITION_LIVE_PROOF=PASS
GATE_A_HISTORICAL_RESULT=BLOCKED
GATE_A_HISTORICAL_POST_STATE=EXCHANGE_NPC_VISIBLE
```

The current `CITY_ENTRY_TRUSTED_CONTEXT_V1` policy accepts both `CITY_DETAIL_VISIBLE` and `EXCHANGE_NPC_VISIBLE` when freshness, frame-change, and evidence-invariant requirements pass. Therefore `CITY_ENTRY_POSTCONDITION_POLICY_CURRENT=PASS`; this interpretation does not rewrite Gate A.

## Commit scope

The creation base `99618e2e4578cc3e21ac7ffb717f5536c0f7091a` is an ancestor of the Gate B HEAD. There are seven single-parent commits after that base and no merge commits:

| Category | Commit | Subject | Integration note |
|---|---|---|---|
| GUI exception boundary | `d5ef94e` | `fix(gui): keep exception boundary non-terminating` | Weakly coupled; may be reviewed independently. |
| BLOCKED safety outcome | `90253b6` | `fix(runtime): classify expected navigation failures as blocked safety` | Depends on shared task/queue outcome contracts; review as a task-outcome group. |
| NEMU ABI and receipt | `652b841` | `fix(nemu): validate touch receipts and navigation effects` | Foundation for native delivery semantics and navigation evidence. |
| NEMU capture fix | `6dc88f8` | `fix(nemu): resolve runtime root and marshal capture buffer` | Must follow the ABI/receipt foundation. |
| City parent control | `c9eefe6` | `fix(runtime): resolve city parent control from live home frame` | Requires fresh capture, coordinate mapping, and receipt semantics. |
| City taxonomy | `8b1a654` | `fix(runtime): align trusted city entry postconditions` | Requires canonical page classification and city navigation evidence. |
| Backpack semantics/runtime binding | `89cf417` | `fix(runtime): target backpack cube control from home` | Semantic correction and runtime binding are inseparable in this commit; requires inventory classification plus NEMU capture/receipt/coordinates. |

```text
FEATURE_COMMITS_SINCE_BASE=7
TOTAL_COMMITS_AHEAD_OF_MAIN=21
COMMITS_BEHIND_MAIN=0
RANGE_CONTAINS_MERGE_COMMITS=NO
UNRELATED_COMMITS_SINCE_BASE=0
UNKNOWN_COMMITS_SINCE_BASE=0
```

Although `main` is an ancestor of the feature branch, a fast-forward would also import fourteen pre-base action-summary/sidebar commits. Consequently the Git operation is mechanically possible but is not an approved minimal NEMU integration.

## File scope

`CHANGED_FILES_SINCE_BASE=42`; `CHANGED_FILES_VS_MAIN=65`.

Files changed since the creation base are classified as follows:

- GUI: `gui.py`, `core/services/gui_exception_boundary.py`.
- NEMU transport: `core/control/control.py`, `core/control/nemu.py`, `core/control/nemu_dll/nemu_dll.py`, `core/control/nemu_receipt.py`.
- Navigation: `core/preset/presets.py`, `core/services/city_entry_postcondition.py`, `core/services/city_entry_resolver.py`, `core/services/city_navigation.py`, `core/services/navigation_evidence.py`, `core/services/navigation_parent_control.py`, `core/services/navigation_warning_diagnostics.py`, `core/services/runtime_navigation_kernel.py`, `core/services/screen_state.py`, `tools/nemu_city_entry_live_gate.py`.
- Inventory: `auto/inventory.py`, `core/services/home_backpack_cube.py`, `core/services/inventory_page_observer.py`, `resources/inventory/home_backpack_cube_template_v1.json`.
- Task outcomes: `app/common/config.py`, `auto/fatigue_recovery.py`, `auto/run_business/main.py`, `core/services/mailbox_task_outcome.py`, `core/services/task_schedule_state.py`.
- Tests: sixteen files under `tests/` covering receipts, navigation, city, inventory, GUI, and task outcomes.
- Documentation: `docs/NEMU_INPUT_RECEIPT_AND_NAVIGATION_POSTCONDITION_RECOVERY_V1.md`.

Relative to `main`, the extra 23 files are the pre-base action-summary/sidebar resource identity, avatar targeting, clarity balance, close-target, OCR/control, documentation, tests, and live-gate tools. They are non-NEMU dependencies/history and must not be imported silently by a full fast-forward.

```text
RUNTIME_CONFIG_FILES_COMMITTED=0
LOG_OR_DIST_FILES_COMMITTED=0
UNEXPECTED_SCREENSHOT_FILES_COMMITTED=0
PRIVATE_AUDIT_FILES_COMMITTED=0
BINARY_DIFF_ENTRIES_SINCE_BASE=0
UNRELATED_FILES_SINCE_BASE=0
```

## Dependency graph

```text
NEMU transport
  -> ABI signatures and typed return values (652b841)
     -> native touch receipt/effect evidence (652b841)
     -> canonical install root + owned capture buffer (6dc88f8)
        -> city fresh parent control (c9eefe6)
           -> city trusted postcondition taxonomy (8b1a654)
        -> backpack cube semantics + fresh binding (89cf417)

GUI exception boundary (d5ef94e) [weakly coupled]
BLOCKED task outcome (90253b6) -> shared TaskOutcome/queue contracts
```

Recommended grouping:

1. Review `652b841` and `6dc88f8` together as the NEMU transport foundation.
2. Layer `c9eefe6` and `8b1a654` as the city group after the transport foundation.
3. Layer `89cf417` as the backpack group after confirming inventory-classifier dependencies on the integration base.
4. Review `d5ef94e` independently and `90253b6` with task-outcome owners; neither should be smuggled into a NEMU-only change.

## Integration options (not executed)

| Option | Included commits | Included non-NEMU commits | Dependency risk | Conflict risk | Recommended |
|---|---|---:|---|---|---|
| `FULL_DEPENDENCY_CHAIN_INTEGRATION` | All 21 commits in `main..89cf417` | 14 pre-base plus GUI/task-outcome commits | High: imports broader action-summary/sidebar history | Low Git risk, high scope-review risk | No |
| `CLEAN_INTEGRATION_BRANCH` | Re-review/replay the 7 post-base commits in the dependency order above, selecting GUI/task-outcome separately | 0-2 depending explicit scope | Medium and auditable | Medium cherry-pick/reimplementation risk | **Yes** |
| `DEFER_INTEGRATION` | None | 0 | Low immediate risk; leaves proven functions off `main` | None | No, unless no integration window is available |

No merge, cherry-pick, rebase, squash, branch creation, or `main` movement is performed by this audit.

## Offline validation

```text
TARGETED_TESTS=300 passed
FULL_COLLECTION=2086 passed, 2 skipped, 7 missing-private-fixture failures
FULL_RUNNABLE_SUITE=2086 passed, 2 skipped, 7 deselected
CHANGED_CODE_FAILURES=0
COMPILEALL=PASS
GIT_DIFF_CHECK=PASS
DEPENDENCY_FILES_CHANGED=NO
CONFIG_POLLUTION=NO
PRIVACY_SCAN=PASS
```

The seven deselected nodes require historical private audit fixtures under ignored `dist/`; they are unavailable and were not fabricated or committed. The real `config/config.json` SHA-256 was identical before and after both successful test runs.

## Final boundary

```text
REAL_UI_ACTIONS_THIS_ROUND=0
REAL_BUSINESS_ACTIONS_THIS_ROUND=0
LOCAL_MAIN_MERGE=NOT_RUN
PUSH=NO
FETCH=NO
MERGE=NO
TAG=NO
RELEASE=NO
PRODUCT_STATUS=NEMU_NATIVE_CITY_TRUSTED_TRANSITION_AND_BACKPACK_RUNTIME_LIVE_PROVEN_INTEGRATION_PENDING
```
