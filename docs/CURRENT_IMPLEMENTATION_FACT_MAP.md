# Action Summary Current Implementation Fact Map

Scope: `ACTION_SUMMARY_READ_ONLY_PRODUCT_MODEL_V1`. This map separates existing execution behavior from the new read-only model; it does not authorize any business action.

| Area | Status | Current fact | Product implication |
|---|---|---|---|
| Action-summary page identity | IMPLEMENTED | `observe_action_summary()` requires a multi-cue list layout; the historical `利刃围剿` title alone is insufficient. | Suitable as the trusted base-page predicate. |
| V2A navigation to the page | IMPLEMENTED | `ResidentActivityAutomation.open_action_summary()` defaults to the proven-edge planner and stops at `ACTION_SUMMARY_VISIBLE`. | Navigation is reusable without putting input authority in the page model. |
| Read-only task inventory | IMPLEMENTED | `ActionSummaryPageModel` groups visible semantic titles with nearby card actions and emits hashed IDs, bboxes, states, costs and evidence IDs. | Current page facts can be consumed without clicking a card. |
| Read-only decision | IMPLEMENTED | `ActionSummaryDecision` returns advice and required future authorization only. | A visible button never authorizes input by itself. |
| Hard-coded `利刃围剿` business tasks | PARTIAL | `SIEGE_TASKS` and `SIEGE_REWARDS` are fixed product tables used by the legacy execution flow. | They may be useful policy data, but are not complete discovery of the live page. |
| Task selection | PARTIAL | The legacy flow locates an OCR title and the nearest `进入挑战` label. | It has some semantic binding but is not based on a complete page model or policy decision. |
| Fixed coordinates | UNSAFE | Challenge Y, sweep, start-sweep and swipe coordinates are constants; missing challenge OCR can fall back to a title-derived X and fixed Y. | These paths must not be called by read-only analysis. |
| Carousel traversal | PARTIAL | The legacy siege selector swipes right four times, then scans up to seven pages. | Bounded, but not driven by a proven pagination model. |
| Default sweeping behavior | UNSAFE | `run()` and `run_once()` proceed from navigation into task selection and sweeping. | They remain outside this read-only task and require later business-policy review. |
| Remaining-attempt detection | UNSAFE | `_reward_attempts()` reads an `n/3` OCR value but defaults to `3` when OCR is missing. | Missing evidence can become an optimistic execution count; the new model instead emits `UNKNOWN`. |
| Cost detection | PARTIAL | The new model can record a visible negative numeric amount such as `-40`; the resource identity remains `UNKNOWN`. | A number alone cannot prove affordability or authorize consumption. |
| Resource sufficiency | MISSING | No complete list-page resource balance/cost comparison exists. | Decisions remain `TASK_AVAILABLE_NEEDS_POLICY` or `UNKNOWN`. |
| Reward state | PARTIAL | Legacy detail logic compares known reward icons; the new list-page model only marks reward state when explicit claim/claimed/locked cues exist. | Generic `REWARD` text is preserved as `UNKNOWN`. |
| Wrong-task protection | PARTIAL | Title-to-nearest-button binding reduces ambiguity, but legacy fixed lists/coordinates and incomplete pagination leave residual risk. | A future executor must bind a policy-selected card to fresh model evidence. |
| Detail postcondition | PARTIAL | Legacy code checks region-locked detail, team and reward-result markers. | Better than command-return-only, but not yet governed by the new read-only decision contract. |
| Overlay representation | IMPLEMENTED | The new page model reports overlays independently from the base page, and decisions become `AMBIGUOUS_PAGE`. | No overlay is automatically dismissed by this model. |
| UNKNOWN handling | IMPLEMENTED | Missing card attempts, cost resource, reward state or page identity remain `None`/`UNKNOWN`. | UNKNOWN grants no capability and no input authority. |

## Live sanitized structure used by V1

The 1280×720 read-only capture contained three visible task-card columns, three challenge-entry cues and three visible `-40` amounts. The committed fixture contains only hashes, bboxes, counts and normalized expectations; it contains no screenshot or raw OCR transcript.

The page-level `3/3` and reward-related text were not assigned to individual cards because the current evidence does not prove that relationship.

## Explicitly deferred

- Selecting, challenging, sweeping or claiming from `ActionSummaryDecision`.
- Resource and fatigue consumption policy.
- Pagination/scroll execution.
- UNKNOWN clustering, persisted graphs and learned edges.
- Any irreversible-action planner.
