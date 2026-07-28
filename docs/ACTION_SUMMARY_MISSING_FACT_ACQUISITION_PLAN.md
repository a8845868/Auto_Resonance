# Action Summary Missing-Fact Acquisition Plan V1

## Scope

This layer converts the advisory evaluator's missing-fact requests into a
read-only acquisition plan. It registers source contracts and normalizes facts
that are already present in an explicit config snapshot or an already captured
`ACTION_SUMMARY_VISIBLE` frame. It does not capture, navigate, retry, persist,
select a task, evaluate business policy, dispatch input, or issue authority.

## Source levels

| Level | Meaning | V1 behavior |
| --- | --- | --- |
| `LEVEL_0_EXISTING_CONFIG` | Explicit config or existing immutable state | Enabled only for zero-input observers |
| `LEVEL_1_CURRENT_PAGE_READ_ONLY` | Already captured current page | Enabled only for zero-input normalization |
| `LEVEL_2_PROVEN_NAVIGATION_READ_ONLY` | Read-only observation after proven navigation | Described by contract; no navigation is run here |
| `LEVEL_3_REVERSIBLE_DETAIL_OBSERVATION` | A detail page would require reversible page input | Disabled in V1 |
| `LEVEL_4_UNAVAILABLE` | No trustworthy source is registered | Remains blocked |

Every enabled observer has `requires_page_input=false`,
`requires_business_input=false`, `irreversible=false`, and
`maximum_dispatches=0`.

## Explicit default config

`parse_action_summary_acquisition_policy_config()` accepts the
`ActionSummaryAcquisitionPolicy` section. Its conservative product defaults are:

- objective: `OBSERVE_ONLY`;
- strategy: versioned `observe-only` with explicit config provenance;
- preferred tasks and reward priorities: empty;
- cost, reserve, and maximum runs: zero;
- execution and authorization: absent.

The normalized document receives a deterministic SHA-256 fingerprint. The
config observer can resolve only `objective_missing`,
`strategy_identity_missing`, and `strategy_provenance_missing`. It never reads
legacy `SIEGE_TASKS` or infers policy from the page.

## Fact boundaries

- Page-level attempt counters are never copied to a task card. The current
  `remaining_attempts_unknown` fallback is Level 3, disabled, and page-input
  requiring.
- Resource identity requires a stable icon and name pair.
- Resource balance requires an explicit amount; the absence of an
  insufficiency warning proves nothing.
- A displayed `40` cost is a resource cost, not a fatigue fact.
- A reward catalog entry is not a live task-bound reward target.
- `fatigue_cost_per_run` and `resource_cost_per_run` remain separate fields.
- Stale or fingerprint-mismatched observations are not accepted as resolved.

## Planner result semantics

`PLAN_STATUS=PASS` means the requests were classified by valid contracts. It
does not mean every fact was acquired and does not make policy evaluation
ready. The canonical observe-only fixture resolves three explicit config facts;
page facts remain unresolved, so
`POLICY_EVALUATION_STILL_BLOCKED=YES`.

Permanent invariants:

```text
EXECUTION_AUTHORIZED=NO
AUTHORIZATION_ISSUED=NO
CAPTURE_CALLS=0
PAGE_INPUT_DISPATCHES=0
BUSINESS_DISPATCHES=0
IRREVERSIBLE_ACTIONS=0
```
