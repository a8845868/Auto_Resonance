# Runtime Navigation Kernel V2A

V2A adds a minimal capability coordinator over four explicitly proven,
reversible navigation edges. It does not learn pages or edges and does not move
physical input into the kernel.

## Evidence schema 2.0

`NavigationAttemptEvidence` keeps every V1 field and now aggregates the entire
post-dispatch observation window. `post_frame_changed` compares the pre-frame
against every effective post-frame. `target_page_changed` compares normalized
base pages. `touch_effect_observed` becomes true when a frame or page changes,
the target control disappears, or another trusted equivalent postcondition is
explicitly supplied. A `PASS` with all effect evidence false is rejected.

Stage points remain separate: `actual_dispatched_point` is generic,
`action_terminal_device_point` is populated only for the terminal edge, and
`action_summary_entry_device_point` is populated only for the summary edge.

## Source-declared graph

`ProvenNavigationGraph` contains only:

1. `claimed_daily_checkin_to_home`
2. `home_to_activity_overview`
3. `activity_overview_to_global_prep`
4. `global_prep_to_action_summary`

Every edge names its source page, required capability, existing action
contract, expected target, allowed overlays, one-dispatch cap, reversible flag,
and live proof reference. Construction rejects missing contracts,
contract mismatches, irreversible edges, invalid limits, missing proof, and
cycles. The graph is immutable and in-memory only.

## Coordinator contract

`ensure_capability()` uses BFS because all edge costs are one. It captures and
classifies before every edge, reauthorizes the existing action contract against
that fresh capture, invokes exactly one injected adapter, captures again, and
verifies the postcondition before replanning. UNKNOWN, an unregistered overlay,
a stale capture, a failed edge, cancellation, or exhausted budget ends the run
without a fallback path or restart.

The coordinator has no capture or input backend of its own. Existing claimed
check-in and action-summary adapters retain candidate resolution, precise input,
bounded transition observation, and evidence ownership.

## Product integration

`ResidentActivityAutomation.open_action_summary()` uses V2A by default and
targets `ACTION_SUMMARY_VISIBLE`. The former sequential navigator remains only
behind `use_proven_edge_planner=False`; a V2A failure never automatically falls
back to it.

## Deferred

- UNKNOWN clustering
- persisted navigation graphs
- automatic edge registration or learning
- graph search through assets, city, exchange, or NPC pages
- irreversible reward, purchase, sale, fatigue, sweep, challenge, or battle
  planning
