# Action Summary Queue Result Semantics

Scope: `ACTION_SUMMARY_INTERLOCK_QUEUE_SEMANTICS_V1`.

This document describes product result propagation only. It does not change
page detection, V2A navigation, execution authorization, task selection, game
input, rewards, or business policy.

## Runtime propagation

```text
run_resident_activity() [READ_ONLY]
  -> ActionSummaryExecutionResult.to_dict()
  -> QueuedTask(name="行动汇总只读评估")
  -> TaskQueueWorker
       -> task_result_outcome()
       -> taskResult(result)
       -> taskFinished(queue_succeeded)
       -> taskCompleted(task, queue_succeeded, result)
  -> DashboardInterface
       -> truthful status text
       -> record_task_execution(completed | deferred | failed)
       -> explicit-progress-only reward recheck
```

The 30-second Dashboard timer remains display-only. It does not build a plan,
start a queue, or retry a deferred action-summary task.

## Pre-fix fact map

| Question | Observed behavior before this round |
|---|---|
| `success=False + business_policy_required` recorded as failure? | YES. `task_result_succeeded()` returned false, and history used `failed_or_stopped`. |
| Could it trigger `halt_on_failure`? | YES, when self-healing supplied the batch halt policy. |
| Could it submit incident/self-healing evidence? | YES, as `unexpected_result`. |
| Did it enter retry planning? | YES, history used the task's failure interval (600 seconds); the 30-second UI timer itself did not retry it. |
| Was `READ_ONLY_COMPLETE` recorded as completed? | YES, because `success=True`. |
| Could read-only completion trigger progress/reward recheck? | YES. Generic integer scanning treated Python `True` as integer `1`. |
| Did Dashboard show both safety-block and sweep-failure text? | NO. In the queue path `taskResult` was emitted only for queue success, so the policy block displayed only the misleading sweep-failure text. |
| Did history retain the sweep task name? | YES, `扫荡与全域整备`. |

## Normalized outcomes

| `task_outcome` | Queue success | History | Incident eligible | Halt eligible | Business progress |
|---|---:|---|---:|---:|---:|
| `COMPLETED_PROGRESS` | YES | completed | NO | NO | explicit only |
| `COMPLETED_NO_PROGRESS` | YES | completed | NO | NO | NO |
| `DEFERRED_EXPECTED` | YES | deferred | NO | NO | NO |
| `BLOCKED_SAFETY` | NO | failed/stopped | NO | NO | NO |
| `FAILED_RUNTIME` | NO | failed/stopped | YES | YES | NO |

Legacy results without `task_outcome` retain their previous success behavior,
but reward recheck no longer scans arbitrary integer values. Attempt history
may retain an explicit `progress_made is True` even for a partially completed
deferred reward task. Downstream business reward recheck additionally requires
the normalized outcome to be `COMPLETED_PROGRESS`.

## Action-summary mappings

```text
business_policy_required
reward_policy_not_started
execution_authorization_required
execution_authority_not_implemented
  -> DEFERRED_EXPECTED
  -> task_terminal=true
  -> task_deferred=true
  -> progress_made=false
  -> business_progress_made=false
  -> incident_eligible=false
  -> halt_eligible=false

read_only_complete / NO_ACTION_REQUIRED
  -> COMPLETED_NO_PROGRESS
  -> progress_made=false

untrusted page / unsupported or ambiguous page
  -> BLOCKED_SAFETY
  -> incident_eligible=false
  -> halt_eligible=false

runtime exception or malformed legacy failure result
  -> FAILED_RUNTIME
  -> incident_eligible=true
  -> halt_eligible=true
```

## Product text

- Running: `正在只读评估行动汇总`
- Policy wait: `行动汇总评估完成，等待业务策略`
- No action: `行动汇总评估完成，当前无需执行`
- Safety stop: `行动汇总因页面安全门禁停止`
- Runtime failure: `行动汇总运行异常`

The default task and history name is `行动汇总只读评估`. The historical
name `扫荡与全域整备` is reserved for an explicitly constructed
`LEGACY_COMPATIBILITY` queue task.
