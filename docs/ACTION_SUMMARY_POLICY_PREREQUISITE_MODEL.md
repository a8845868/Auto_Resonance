# Action Summary Policy Prerequisite Model V1

Scope: `ACTION_SUMMARY_POLICY_PREREQUISITE_MODEL_V1`.

This is a pure, read-only input contract for a future business-policy
evaluator.  It does not select a task, choose challenge versus sweep, authorize
input, claim rewards, use fatigue items, or dispatch any game action.

## Position in the product flow

```text
ACTION_SUMMARY_VISIBLE read-only page model
  -> candidate task identity binding
  -> prerequisite fact validation
  -> READY_FOR_POLICY_EVALUATION | BLOCKED_*
  -> future business policy (NOT IMPLEMENTED)
  -> future execution authority (NOT IMPLEMENTED)
```

`READY_FOR_POLICY_EVALUATION` means only that the inputs are complete and
internally consistent.  Every result fixes the following fields:

```text
execution_authorized=false
selected_action=null
business_dispatches=0
irreversible_actions=0
```

## Six prerequisite contracts

| Contract | Required facts | Accepted provenance |
|---|---|---|
| Task semantic identity | activity family, semantic task id, card match key, title hash, exact source model/capture/frame/freshness binding | immutable page model |
| Remaining attempts | remaining and total attempt counts | immutable page model or fresh game observation |
| Resource identity and balance | non-`UNKNOWN` resource id, available amount, positive unit cost | fresh game observation, reconciled with visible card cost |
| Reward target | target id, current amount, desired amount, candidate reward per execution | explicit user configuration |
| Fatigue budget | available, reserved, and maximum policy spend | fresh game observation, or the runtime assembler's explicit observed/configured composition |
| Strategy input | schema, strategy id/version, objective, bounded action types and execution count | explicit user configuration |

Known facts require a revision plus an aware `observed_at..valid_until` window
containing the source frame's capture time.  Missing value/provenance produces
`BLOCKED_INCOMPLETE_FACTS`; stale, contradictory, mismatched, unsupported, or
unbounded inputs produce `BLOCKED_CONFLICTING_FACTS`.

## Conservative reconciliation

- A task identity must resolve to exactly one card in the same immutable page
  model and must preserve semantic id, title hash, evidence ids, capture id,
  frame hash, and freshness token.
- A non-available card cannot become a policy candidate.
- Remaining attempts must be non-negative and no larger than the observed
  total; a page-observed count must agree with the supplied fact.
- Resource identity may not be `UNKNOWN`; unit cost must agree with the page
  model when a page cost exists.
- Candidate action types are descriptive intersections derived from the page
  card (`CHALLENGE`/`SWEEP`); they are not execution permissions.
- If the cost resource is `FATIGUE`, the resource balance and fatigue balance
  must reconcile.
- Reserved fatigue cannot be spent, and maximum policy spend cannot exceed
  the unreserved balance.
- Strategy actions are data only and are limited to `CHALLENGE` and `SWEEP`.
  Reward claim is deliberately outside this contract.
- `bounded_candidate_executions` is the minimum of explicit attempts,
  affordability, fatigue spend budget, and the configured strategy cap.  It is
  a bound for later evaluation, not an execution decision.

## Explicit non-goals in V1

```text
TASK_SELECTION_POLICY=NOT_IMPLEMENTED
CHALLENGE_POLICY=NOT_IMPLEMENTED
SWEEP_POLICY=NOT_IMPLEMENTED
REWARD_CLAIM_POLICY=NOT_IMPLEMENTED
FATIGUE_ITEM_POLICY=NOT_IMPLEMENTED
EXECUTION_AUTHORITY=NO
REAL_INPUT=NO
```

The existing `READ_ONLY` product path and execution interlock remain the only
runtime behavior.  This module has no `auto` or control-backend dependency and
is not wired to any executor.
