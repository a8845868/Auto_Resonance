# Action Summary Policy Runtime Input Assembly V1

Scope: `ACTION_SUMMARY_POLICY_RUNTIME_INPUT_ASSEMBLY_V1`.

This layer performs one read-only composition step:

```text
fresh ACTION_SUMMARY page model
  + fresh read-only resource observation
  + explicit user policy configuration
  -> one prerequisite model per visible executable card
  -> READY_FOR_POLICY_EVALUATION | BLOCKED_*
```

It does not call the business-policy evaluator, select or execute a
recommendation, issue authorization, initialize a backend, retry, click,
sweep, challenge, claim rewards, or use fatigue items.

## Source boundaries

The assembler does not treat all inputs as interchangeable:

| Output fact | Source |
|---|---|
| Task identity and card action types | immutable page model |
| Remaining and total attempts | exact `remaining/total` page cue |
| Unit cost | exact visible card cost |
| Resource balance and available fatigue | fresh read-only runtime observation bound to its capture id, frame SHA-256 and evidence ids |
| Reward target, per-card reward amount and strategy | explicit user policy configuration |
| Effective fatigue budget | runtime assembly of observed available fatigue plus configured reserve and spend cap |

Missing attempt totals, balances, per-card rewards, provenance, capture
identity, or freshness stay unknown and block evaluation.  No default resource,
reward yield, attempt count, or budget is invented.

## User policy document

The parser accepts either the section below or a document whose
`ActionSummaryPolicy` property contains it:

```json
{
  "schema_version": "1.0",
  "policy_id": "personal-siege-policy",
  "policy_version": "1",
  "revision": "user-config-rev-1",
  "effective_at": "2026-07-26T11:50:00+08:00",
  "valid_until": "2026-07-26T12:10:00+08:00",
  "objective": "MAXIMIZE_TARGET_REWARD",
  "allowed_action_types": ["CHALLENGE", "SWEEP"],
  "max_task_executions": 2,
  "reward_target_id": "SIEGE_PROGRESS",
  "reward_current_amount": 20,
  "reward_target_amount": 100,
  "reserved_fatigue": 20,
  "max_policy_spend": 80,
  "candidate_rewards": [
    {
      "task_semantic_id": "TASK_<PAGE_MODEL_ID>",
      "reward_amount_per_execution": 30
    }
  ]
}
```

Parsing is strict: booleans are not integers, unsupported action types are
rejected, execution count is capped at 10, candidate identities must be
unique, and no fallback defaults are applied.  The file loader reads only an
explicit path and never creates, rewrites, or migrates configuration.

## Output contract

A ready assembly contains the complete candidate prerequisite set expected by
the existing advisory evaluator.  Ready means only that evaluation inputs are
coherent.  Every result fixes these fields:

```text
business_policy_evaluated=false
executor_connected=false
execution_authorized=false
authorization_issued=false
business_dispatches=0
irreversible_actions=0
```

The canonical normalized user configuration is SHA-256 bound as
`policy_config_sha256`.  Actual runtime call-site wiring and GUI configuration
editing remain outside V1.

## Explicit non-goals

```text
BUSINESS_POLICY_EVALUATION=NO
RUNTIME_EXECUTOR_WIRING=NO
EXECUTION_AUTHORITY=NO
CHALLENGE_EXECUTION=NO
SWEEP_EXECUTION=NO
REWARD_CLAIM=NO
FATIGUE_ITEM_USE=NO
REAL_CAPTURE=NO
REAL_INPUT=NO
```
