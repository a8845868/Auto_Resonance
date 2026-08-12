# Action Summary Execution Call Graph

Scope: `ACTION_SUMMARY_EXECUTION_AUTHORITY_INTERLOCK_V1`.

The graph distinguishes navigation/read-only observation from legacy business
execution. `READ_ONLY` is the product default. No path below creates execution
authorization, chooses a task, consumes resources, claims a reward, or falls
back automatically to legacy behavior.

## Formal runtime graph

```text
Dashboard 30-second schedule timer
  -> DashboardInterface._refreshDisplayStatus()
  -> display refresh only; no task plan and no execution

Explicit Start Queue action
  -> DashboardInterface._allEnabledTasks()
  -> QueuedTask(key="resident_activity")
  -> TaskQueueWorker.run()
  -> run_resident_activity()                    [default READ_ONLY]
  -> ResidentActivityAutomation.run()
  -> connect_resonance()
  -> read_action_summary_product_model()
  -> V2A open_action_summary()
  -> capture one current page
  -> observe_action_summary_page()
  -> decide_action_summary()
  -> evaluate_execution_interlock()
  -> structured terminal result

Headless debug scheduler
  -> core.services.debug_tasks._resident_activity()
  -> run_resident_activity()                    [same default READ_ONLY path]

Explicit one-shot API
  -> run_resident_activity_once()
  -> ResidentActivityAutomation.run_once()      [same default READ_ONLY path]

Explicit compatibility-only API construction
  -> ResidentActivityAutomation(
         execution_mode=LEGACY_COMPATIBILITY)
  -> run()/run_once()
  -> legacy selection/challenge/sweep helpers
```

## Entrypoint assessment

| ENTRYPOINT | DEFAULT_REACHABLE | READ_ONLY_MODEL_USED | BUSINESS_POLICY_REQUIRED | EXECUTION_AUTHORIZATION_REQUIRED | CAN_REACH_LEGACY_SELECTION | CAN_REACH_CHALLENGE | CAN_REACH_SWEEP | CAN_REACH_REWARD | CURRENT_DEFAULT_SAFE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Dashboard `_allEnabledTasks()` resident task | YES | YES | YES | YES | NO | NO | NO | NO | YES |
| Dashboard 30-second schedule timer | NO | NO | N/A | N/A | NO | NO | NO | NO | YES |
| `TaskQueueWorker.run()` resident closure | YES | YES | YES | YES | NO | NO | NO | NO | YES |
| Headless `debug_tasks._resident_activity()` | YES, when enabled and due | YES | YES | YES | NO | NO | NO | NO | YES |
| `run_resident_activity()` | YES | YES | YES | YES | NO | NO | NO | NO | YES |
| `run_resident_activity_once()` | YES | YES | YES | YES | NO | NO | NO | NO | YES |
| `ResidentActivityAutomation.run()` | YES | YES | YES | YES | NO | NO | NO | NO | YES |
| `ResidentActivityAutomation.run_once()` | YES | YES | YES | YES | NO | NO | NO | NO | YES |
| `read_action_summary_product_model()` | YES | YES | N/A | N/A | NO | NO | NO | NO | YES |
| `select_siege_task()` | NO | NO | YES | YES | Explicit compatibility only | Explicit compatibility only | NO | NO | YES by default denial |
| `enter_first_visible_challenge()` | NO | NO | YES | YES | NO | Explicit compatibility only | NO | NO | YES by default denial |
| `select_activity_stage()` | NO | NO | YES | YES | Explicit compatibility only | Explicit compatibility only | NO | NO | YES by default denial |
| `sweep_current_activity()` | NO | NO | YES | YES | NO | NO | Explicit compatibility only | Result dialog only in compatibility mode | YES by default denial |
| `_reward_attempts()` optimistic fallback | NO | NO | YES | N/A | Explicit compatibility only | NO | NO | NO | YES by default denial |
| Separate scheduled reward collector | YES, separate task key | No action-summary model | Separate reward policy | Separate reward authority | NO | NO | NO | YES, outside this interlock scope | OUT OF SCOPE |

## Default result contract

When current facts produce `TASK_AVAILABLE_NEEDS_POLICY`, the product entry
returns a terminal structured result:

```text
execution_mode=READ_ONLY
execution_status=BLOCKED
reason=business_policy_required
execution_authorized=false
authorization_valid=false
business_dispatches=0
irreversible_actions=0
```

The TaskQueue only invokes the returned callable. It has no alternate action-
summary executor and cannot convert this result into legacy selection.

## Explicit legacy boundary

The following historical behavior remains for compatibility tests only:

- optimistic `_reward_attempts(..., fallback=3)`;
- fixed challenge Y coordinate;
- fixed sweep and start-sweep coordinates;
- bounded fixed carousel swipes;
- title-derived challenge fallback coordinate.

Every public or helper route that can reach those behaviors requires an
explicit `LEGACY_COMPATIBILITY` constructor mode. Failure of the read-only or
policy-gated path never changes the mode and never invokes legacy fallback.
