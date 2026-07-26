# Action Summary Advisory Policy Evaluator V1

Scope: `ACTION_SUMMARY_ADVISORY_POLICY_EVALUATOR_V1`.

This layer consumes exactly two immutable inputs:

```text
provenance-bound ActionSummaryRuntimeInputAssembly
  + fingerprint-bound ActionSummaryAdvisoryPolicyProfile
  -> blocked explanation | observe-only completion | advisory ranking
```

It is a pure function. It has no capture, navigation, input, retry,
persistence, executor, authorization issuer, reward claim, fatigue recovery,
challenge, or sweep path.

## Policy profile

The strict profile binds identity, capture time, activity and strategy,
objective, preferred and excluded tasks, reward priorities, cost and reserve
limits, run limits, strict UNKNOWN flags, deterministic tie breaks, and a
human-readable reason. The default factory objective is `OBSERVE_ONLY`; all
`allow_unknown_*` flags default to false.

The canonical SHA-256 fingerprint excludes only the fingerprint field itself.
The evaluator recomputes and validates it before use. Any subsequent profile
change invalidates an older result.

Supported objectives are:

```text
FIXED_TASK
MAXIMIZE_PRIORITY_REWARD
MAXIMIZE_AVAILABLE_ATTEMPTS
MINIMIZE_RESOURCE_COST
BALANCED
OBSERVE_ONLY
```

`BALANCED` is deliberately blocked in V1 because no user-defined weight
contract exists. Costs in different resource identities are not compared
without a conversion contract.

## Strict readiness

For every non-observe objective, UNKNOWN facts are excluded from ranking and
block the result. The evaluator never demotes an unknown candidate to the end
of a ranking. It also never invents a run count.

The suggested run count is the minimum of every known hard bound:

```text
card remaining attempts
policy maximum task runs
policy maximum total cost
resource balance after reserve
fatigue budget after reserve
runtime strategy execution limit
```

If any required bound is unknown, the result remains
`BLOCKED_MISSING_FACTS` with `recommended_run_count=None`.

## Provenance binding

Every result binds the entire runtime input document by SHA-256 and preserves:

```text
assembly schema and scope
assembled_at and evaluated_at
page capture id, frame hash and freshness token
resource observation identity and relationship
runtime policy configuration fingerprint
advisory policy profile fingerprint
```

Stale assemblies, stale resource observations, incomplete page provenance,
candidate binding conflicts, or fingerprint mismatches stop before ranking.

## Missing-fact requests

Blocked results may include `FactAcquisitionRequest` records. These describe
the required scope, preferred read-only source, and whether navigation or page
input may be needed. They are descriptions only; the evaluator never executes
collection. Unknown input requirements remain `None` instead of being guessed.

## Permanent authority boundary

Every result fixes:

```text
execution_authorized=false
authorization_issued=false
business_dispatches=0
irreversible_actions=0
```

An advisory recommendation is not execution authority and is not connected to
the existing legacy or interlocked execution paths.
