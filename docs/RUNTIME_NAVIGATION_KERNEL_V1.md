# Runtime Navigation Kernel V1

## Purpose

`RuntimeNavigationKernel` is the shared, side-effect-free contract layer for
routine game UI navigation. It replaces page-specific keyword precedence with
composable page signatures and gives adapters one state/action/transition
vocabulary without moving physical input authority into the kernel.

The normalized state is deliberately factorized:

```text
base_page + overlays + phase + capabilities + confidence + evidence
```

Large UI state spaces are represented by combinations of these dimensions,
not by a flat enumeration of every possible screenshot.

## Five-layer boundary

1. Perception adapters derive OCR, CV and layout facts from a frame.
2. Page signatures normalize facts into `UiState`.
3. Existing semantic resolvers bind an anchor to one parent control and safe
   hit target; `confirm_fresh_target()` verifies identity and geometry.
4. `ActionContract` and `TransitionClassifier` enforce preconditions, bounded
   dispatch count, postconditions, recoverable UNKNOWN frames and explicit
   foreign-page stops.
5. Task planners may consume capabilities after the lower layers are migrated;
   V1 does not yet execute graph paths automatically.

## Classification order

The kernel evaluates categories in this order:

```text
specific trusted page signatures
known overlays
strongly committed foreign-page signatures
UNKNOWN
```

An overlay is represented independently from its base page and blocks action
authorization by default. Common domain words such as `材料`, `奖励`, `活动` or
`任务` are not page signatures. A foreign page requires a layout fact or
multiple independent cues.

The first production migration is Global Prep. The page containing a unique
`行动汇总` entry is classified as `GLOBAL_PREP_PAGE` even when its descriptive
copy also contains `材料`. The existing adapter continues to expose
`ACTION_SUMMARY_ENTRY_VISIBLE` to legacy callers.

## Reusable contracts

- `PagePerception`: privacy-safe cue identifiers and capture freshness.
- `PageSignature`: trusted, overlay or foreign page prototype.
- `UiState`: normalized composable state and capabilities.
- `ActionContract`: allowed pre-state/capability, candidate and target policy,
  at-most-N dispatch, allowed/forbidden post-state and irreversibility.
- `InteractionTarget` / `confirm_fresh_target`: semantic and normalized
  geometry confirmation across initial/fresh captures.
- `TransitionClassifier`: stale-frame rejection, retained source state,
  known loading/transition states, recoverable UNKNOWN, stable changed UNKNOWN,
  optional overlay, expected page, explicit foreign page and timeout/no-effect
  distinction.

The kernel never captures a frame, dispatches input, retries, or persists
evidence. Existing adapters retain those responsibilities and continue using
`NavigationAttemptEvidence`, `CoordinateChain`, `EpisodeActionBudget` and the
read-only input policy.

## Regression exemplars

V1 bridges four proven adapters into the shared state/capability model:

| Existing exemplar | Normalized capability |
| --- | --- |
| Assets entry on `HOME_READY` | `OPEN_INVENTORY` |
| Unique city entry | `ENTER_CITY` |
| Action terminal on `HOME_READY` | `OPEN_ACTION_TERMINAL` |
| Global Prep page | `OPEN_ACTION_SUMMARY` |

Assets and city keep their proven OCR/CV and dispatch implementations. Action
terminal and Global Prep use the shared page-signature classifier; the Global
Prep post-dispatch loop also uses the shared transition classifier.

## Explicitly deferred

V1 does not:

- perform real input or live validation;
- auto-approve a newly observed page;
- cluster and persist UNKNOWN frames;
- auto-build or execute a navigation graph;
- generalize irreversible reward, purchase, sale, fatigue or battle logic;
- remove legacy state enums before all callers have migrated.

Those capabilities can be added incrementally after their evidence and privacy
formats are specified. UNKNOWN remains zero-action by default.
