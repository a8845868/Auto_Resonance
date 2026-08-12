# Action Summary Business Policy Evaluator V1

Scope: `ACTION_SUMMARY_BUSINESS_POLICY_EVALUATOR_V1`.

The evaluator consumes a complete set of
`ACTION_SUMMARY_POLICY_PREREQUISITES_V1` candidate models from one fresh page
capture.  It returns one deterministic advisory recommendation or an explicit
blocked/no-action result.  It has no executor, authorization issuer, input
backend, retry loop, or runtime wiring.

## Input gate

Every visible policy candidate must be `READY_FOR_POLICY_EVALUATION`.  All
candidates must share:

- source model scope, capture id, frame hash, and freshness token;
- one reward target id/current/target contract;
- one strategy id/version/objective/action-limit contract;
- one resource balance and fatigue budget;
- unique card match keys.

The number of supplied models must equal the source page's count of available
cards carrying `TASK_EXECUTION_AVAILABLE`; a partial candidate set is rejected.
The evaluator also recomputes every candidate execution bound and rechecks the
prerequisite zero-authority fields, so a caller-forged `READY` flag cannot
cross the advisory boundary.

Any incomplete candidate blocks global ranking.  Cross-capture, duplicate,
stale, or conflicting contracts return `BLOCKED_CONFLICTING_INPUTS`; the
evaluator never ranks a partial view as though it were complete.

## Candidate assessment

For each candidate the evaluator intersects page-observed action types with
the strategy allowlist.  `SWEEP` is preferred over `CHALLENGE` only when both
are already visible and allowed.  It then derives:

```text
bounded executions
recommended executions capped to remaining reward target
estimated resource spend
estimated reward gain
whether this candidate alone can complete the target
```

Zero attempts, zero affordability, reserved-fatigue limits, or no shared
allowed action produce a non-actionable candidate.  They never create an
input attempt.

## Deterministic V1 ranking

| Objective | Ranking |
|---|---|
| `COMPLETE_TARGET` | candidates able to finish, then fewer executions, greater bounded gain, lower spend, stable card key |
| `MAXIMIZE_TARGET_REWARD` | greater bounded gain, lower cost per reward, lower spend, stable card key |
| `CONSERVE_FATIGUE` | lower cost per reward, lower spend, greater bounded gain, stable card key |
| `OBSERVE_ONLY` | `NO_ACTION_REQUIRED`; no candidate is selected |

V1 recommends only one candidate.  It does not combine several cards into a
multi-step plan.  The complete canonical prerequisite documents are hashed as
`policy_input_sha256`; the policy/version, objective, input hash, selected card,
action, and bounded count produce a deterministic `decision_id`.

## Output statuses

```text
RECOMMENDATION_AVAILABLE
NO_ACTION_REQUIRED
BLOCKED_PREREQUISITES
BLOCKED_CONFLICTING_INPUTS
NO_FEASIBLE_CANDIDATE
```

Even `RECOMMENDATION_AVAILABLE` always carries:

```text
advisory_only=true
execution_authorized=false
authorization_issued=false
business_dispatches=0
irreversible_actions=0
required_future_authorization=[EXPLICIT_POLICY_ADOPTION, EXECUTION_AUTHORITY]
```

## Explicit non-goals

```text
RUNTIME_POLICY_WIRING=NO
EXECUTION_AUTHORITY=NO
CHALLENGE_EXECUTION=NO
SWEEP_EXECUTION=NO
REWARD_CLAIM=NO
FATIGUE_ITEM_USE=NO
MULTI_CARD_PLAN=NO
REAL_INPUT=NO
```
