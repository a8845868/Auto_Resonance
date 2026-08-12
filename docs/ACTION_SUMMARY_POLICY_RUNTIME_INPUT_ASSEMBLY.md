# Action Summary Policy Runtime Input Assembly V1

Scope: `ACTION_SUMMARY_POLICY_RUNTIME_INPUT_ASSEMBLY_V1`.

This layer performs one read-only composition step:

```text
fresh ACTION_SUMMARY page model
  + fresh read-only resource observation
  + explicit user policy configuration
  -> one prerequisite model per visible executable card
  -> assembly integrity + independent policy-input readiness
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

The output preserves page and resource capture ids, frame hashes, timestamps,
freshness tokens, their measured age, and one explicit relationship:
`SAME_CAPTURE`, `DIFFERENT_CAPTURE_WITHIN_WINDOW`,
`DIFFERENT_CAPTURE_STALE`, or `UNKNOWN`. A stale resource observation remains
visible as provenance but is never promoted to a known prerequisite fact.

## User policy document

The parser accepts either the section below or a document whose
`ActionSummaryPolicy` property contains it:

```json
{
  "schema_version": "1.0",
  "policy_id": "personal-siege-policy",
  "policy_version": "1",
  "revision": "user-config-rev-1",
  "captured_at": "2026-07-26T11:50:00+08:00",
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

An optional policy target binds by exact semantic task id and, when supplied,
the current title hash. The matching fresh card must be unique; no array index,
substring, or previous bounding box is accepted. The result records the
current `card_match_key` only after that unique match.

## Output contract

`assembly_status=PASS` means the sources were assembled without contradiction.
It is independent from `policy_input_readiness`, which may remain
`BLOCKED_MISSING_FACTS`. A ready assembly contains the complete candidate
prerequisite set expected by the existing advisory evaluator. Ready means only
that evaluation inputs are coherent. Every result fixes these fields:

```text
business_policy_evaluated=false
executor_connected=false
execution_authorized=false
authorization_issued=false
business_dispatches=0
irreversible_actions=0
```

The exact normalized user configuration snapshot, including an incomplete
snapshot, is SHA-256 bound as `policy_config_sha256`. `matches_policy_config()`
rejects later reuse after any snapshot change. Actual policy evaluation and
GUI configuration editing remain outside V1.

The opt-in live gate tool may use the proven V2A product navigator and then
capture at most two ACTION_SUMMARY frames. That orchestration is outside the
assembler: the assembler itself still has no capture, backend, retry, input,
or persistence authority. The gate never sends input on the action-summary
page and never evaluates policy.

## Explicit non-goals

```text
BUSINESS_POLICY_EVALUATION=NO
RUNTIME_EXECUTOR_WIRING=NO
EXECUTION_AUTHORITY=NO
CHALLENGE_EXECUTION=NO
SWEEP_EXECUTION=NO
REWARD_CLAIM=NO
FATIGUE_ITEM_USE=NO
ASSEMBLER_REAL_CAPTURE=NO
REAL_INPUT=NO
```
