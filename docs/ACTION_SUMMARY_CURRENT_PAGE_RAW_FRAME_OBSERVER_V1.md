# Action Summary current-page raw-frame observer V1

## Scope

`observe_action_summary_current_page_visuals(frame)` consumes exactly one
already captured frame. It may read the frame pixels and call `frame.ocr()`;
it cannot capture, navigate, dispatch input, retry, persist data, evaluate
policy, execute a task, or issue authorization.

The observer reuses the current `ActionSummaryPageModel` contract. The page
model and observer frame must have the same capture ID and SHA-256. A mismatch
returns `BLOCKED_FRAME_MISMATCH` with no facts.

## V1 fact boundary

| Fact | V1 status | Evidence required |
| --- | --- | --- |
| Card resource cost | `OFFLINE_PROVEN` raw callable | Exact negative integer, one card container, fresh `card_match_key`, page-model cost agreement |
| Resource identity | `OFFLINE_PROVEN` normalizer only | Stable catalog icon and resource name; not available in the current raw-frame contract |
| Resource balance | `OFFLINE_PROVEN` normalizer only | Explicit balance plus resolved resource identity |
| Card reward target | `OFFLINE_PROVEN` normalizer only | Unique card-bound reward icon and name |
| Remaining attempts | `NOT_IMPLEMENTED`, disabled Level 3 | Requires a future separately authorized detail-page observer |

The visible `-40` label is normalized to a non-negative cost value of `40`.
Its resource identity remains `UNKNOWN`; it is never inferred to mean fatigue
or stamina. Page-level negative numbers are rejected, and multiple distinct
cost candidates in one card remain ambiguous.

## Authority

```text
RAW_OBSERVER_CAPTURE_AUTHORITY=NO
RAW_OBSERVER_INPUT_AUTHORITY=NO
RAW_OBSERVER_PERSISTENCE_AUTHORITY=NO
RAW_OBSERVER_EXECUTION_AUTHORITY=NO
ACTION_SUMMARY_PAGE_INPUTS=0
BUSINESS_ACTIONS=0
IRREVERSIBLE_ACTIONS=0
```

The serialized observation contains hashes, bounding boxes, semantic IDs,
reason codes, and evidence IDs. It does not retain full OCR text.
