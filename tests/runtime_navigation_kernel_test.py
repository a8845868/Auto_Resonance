from types import SimpleNamespace

from auto import inventory
from core.services.action_summary_navigation import observe_action_summary
from core.services.city_navigation import observe_city_frame
from core.services.runtime_navigation_kernel import (
    ActionContract,
    ActionPrimitive,
    Confidence,
    InteractionTarget,
    PageKind,
    PagePerception,
    PageSignature,
    PROVEN_NAVIGATION_CONTRACTS,
    RuntimeNavigationKernel,
    TransitionClassification,
    TransitionClassifier,
    UiState,
    confirm_fresh_target,
    normalize_legacy_state,
)


def _item(text, x, y, width=100, height=24):
    return {
        "text": text,
        "position": [
            [x - width // 2, y - height // 2],
            [x + width // 2, y - height // 2],
            [x + width // 2, y + height // 2],
            [x - width // 2, y + height // 2],
        ],
    }


class _Frame:
    def __init__(self, items, capture_id="fixture"):
        self._items = list(items)
        self.image = SimpleNamespace(shape=(720, 1280, 3))
        self.raw_frame_hash = f"hash:{capture_id}"
        self.source_capture_id = capture_id

    def ocr(self):
        return list(self._items)


def _kernel():
    return RuntimeNavigationKernel(
        (
            PageSignature(
                "specific",
                "GLOBAL_PREP_PAGE",
                PageKind.TRUSTED,
                required_all=frozenset({"global_prep_title", "action_summary_entry"}),
                capabilities=frozenset({"OPEN_ACTION_SUMMARY"}),
                priority=100,
            ),
            PageSignature(
                "overlay",
                "CHECKIN_OVERLAY",
                PageKind.OVERLAY,
                required_all=frozenset({"checkin_overlay"}),
                priority=100,
            ),
            PageSignature(
                "foreign",
                "INVENTORY",
                PageKind.FOREIGN,
                required_all=frozenset({"inventory_category_rail"}),
                priority=100,
            ),
        )
    )


def test_specific_signature_precedes_overlay_and_foreign_page():
    state = _kernel().classify(PagePerception.from_facts({
        "global_prep_title",
        "action_summary_entry",
        "checkin_overlay",
        "inventory_category_rail",
    }))

    assert state.base_page == "GLOBAL_PREP_PAGE"
    assert state.overlays == ("CHECKIN_OVERLAY",)
    assert state.capabilities == frozenset({"OPEN_ACTION_SUMMARY"})
    assert not state.has_capability("OPEN_ACTION_SUMMARY")


def test_generic_domain_word_without_layout_fact_stays_unknown():
    state = _kernel().classify(PagePerception.from_facts({"generic_word:材料"}))

    assert state.is_unknown
    assert state.capabilities == frozenset()


def test_equal_priority_conflicting_specific_pages_fail_closed():
    kernel = RuntimeNavigationKernel((
        PageSignature(
            "one", "ONE", PageKind.TRUSTED,
            required_all=frozenset({"shared"}), priority=5,
        ),
        PageSignature(
            "two", "TWO", PageKind.TRUSTED,
            required_all=frozenset({"shared"}), priority=5,
        ),
    ))

    state = kernel.classify(PagePerception.from_facts({"shared"}))

    assert state.is_unknown
    assert state.reason == "ambiguous_or_unknown_page_signature"


def test_action_contract_is_capability_and_overlay_driven():
    contract = ActionContract(
        "open_action_summary",
        ActionPrimitive.OPEN_ENTRY,
        allowed_pre_pages=frozenset({"GLOBAL_PREP_PAGE"}),
        required_capabilities=frozenset({"OPEN_ACTION_SUMMARY"}),
        allowed_post_pages=frozenset({"ACTION_SUMMARY_VISIBLE"}),
    )
    ready = normalize_legacy_state("GLOBAL_PREP_PAGE")
    covered = normalize_legacy_state("GLOBAL_PREP_PAGE", overlays=("HELP_OVERLAY",))

    assert contract.authorize(ready).allowed
    assert contract.authorize(covered).reason == "overlay_blocks_action"
    assert contract.authorize(ready, dispatch_count=1).reason == (
        "action_dispatch_budget_exhausted"
    )
    assert contract.authorize(normalize_legacy_state("UNKNOWN")).reason == (
        "unknown_pre_state"
    )


def _target(point=(112, 290), *, semantic="全域整备", occluded=False):
    return InteractionTarget(
        semantic_id=semantic,
        candidate_count=1,
        label_bbox=(31, 306, 89, 323),
        parent_bbox=(28, 257, 196, 324),
        hit_target_bbox=(62, 270, 162, 311),
        hit_target_point=point,
        capture_size=(1280, 720),
        method="enclosing_high_contrast_left_rail_card_contour",
        occluded=occluded,
    )


def test_fresh_target_confirms_semantics_parent_and_normalized_geometry():
    assert confirm_fresh_target(_target(), _target((113, 291))).allowed
    assert confirm_fresh_target(_target(), _target(semantic="协同终端")).reason == (
        "semantic_identity_changed"
    )
    assert confirm_fresh_target(_target(), _target(occluded=True)).reason == (
        "target_occluded"
    )


def test_transition_classifier_keeps_unknown_pending_then_commits_stable_unknown():
    contract = ActionContract(
        "open_global_prep",
        ActionPrimitive.SELECT_CARD,
        allowed_pre_pages=frozenset({"ACTIVITY_OVERVIEW_VISIBLE"}),
        required_capabilities=frozenset({"OPEN_GLOBAL_PREP"}),
        allowed_post_pages=frozenset({"GLOBAL_PREP_PAGE"}),
        forbidden_post_pages=frozenset({"INVENTORY"}),
    )
    source = UiState(
        "ACTIVITY_OVERVIEW_VISIBLE",
        capabilities=frozenset({"OPEN_GLOBAL_PREP"}),
        confidence=Confidence.HIGH,
        frame_hash="before",
        capture_id="1",
    )
    classifier = TransitionClassifier(contract, source_state=source)

    first = classifier.observe(UiState("UNKNOWN", frame_hash="changed", capture_id="2"))
    second = classifier.observe(UiState("UNKNOWN", frame_hash="changed", capture_id="3"))

    assert first.classification is TransitionClassification.UNKNOWN_RECOVERABLE
    assert not first.terminal
    assert second.classification is TransitionClassification.STABLE_CHANGED_UNKNOWN
    assert second.terminal and not second.success


def test_transition_classifier_accepts_expected_and_stops_on_explicit_foreign():
    contract = ActionContract(
        "open_global_prep",
        ActionPrimitive.SELECT_CARD,
        allowed_pre_pages=frozenset({"ACTIVITY_OVERVIEW_VISIBLE"}),
        allowed_post_pages=frozenset({"GLOBAL_PREP_PAGE"}),
        forbidden_post_pages=frozenset({"INVENTORY"}),
    )
    source = normalize_legacy_state("ACTIVITY_OVERVIEW_VISIBLE")
    expected = TransitionClassifier(contract, source_state=source).observe(
        normalize_legacy_state("GLOBAL_PREP_PAGE")
    )
    foreign = TransitionClassifier(contract, source_state=source).observe(
        normalize_legacy_state("INVENTORY")
    )

    assert expected.classification is TransitionClassification.EXPECTED_POST_STATE
    assert expected.success
    assert foreign.classification is TransitionClassification.KNOWN_FOREIGN_PAGE
    assert foreign.terminal and not foreign.success


def test_city_transition_is_known_but_nonterminal():
    contract = PROVEN_NAVIGATION_CONTRACTS["ENTER_CITY"]
    source = normalize_legacy_state("CITY_ENTRY_VISIBLE")

    decision = TransitionClassifier(contract, source_state=source).observe(
        normalize_legacy_state("CITY_TRANSITION")
    )

    assert decision.classification is TransitionClassification.KNOWN_TRANSITION
    assert not decision.terminal
    assert not decision.success


def test_four_proven_adapters_normalize_to_reusable_capabilities():
    home = _Frame([
        _item("资产", 190, 675),
        _item("访问城市", 1172, 486),
        _item("作战终端", 1191, 410),
    ], "home")
    assets, _ = inventory._assets_runtime_state(home)
    city = observe_city_frame(home).to_ui_state()
    terminal = observe_action_summary(home).to_ui_state()
    global_prep = observe_action_summary(_Frame([
        _item("全域整备", 820, 92),
        _item("行动汇总", 980, 270),
        _item("收集装备、材料等物资", 940, 330, 260),
    ], "global-prep")).to_ui_state()

    assert assets.has_capability("OPEN_INVENTORY")
    assert city.has_capability("ENTER_CITY")
    assert terminal.has_capability("OPEN_ACTION_TERMINAL")
    assert global_prep.has_capability("OPEN_ACTION_SUMMARY")

    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_INVENTORY"].authorize(assets).allowed
    assert PROVEN_NAVIGATION_CONTRACTS["ENTER_CITY"].authorize(city).allowed
    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_ACTION_TERMINAL"].authorize(terminal).allowed
    overview = normalize_legacy_state("ACTIVITY_OVERVIEW_VISIBLE")
    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_GLOBAL_PREP"].authorize(overview).allowed
