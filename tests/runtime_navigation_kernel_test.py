import subprocess
import sys
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
    confirm_fresh_capability,
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
    assert not state.has_capability(
        "OPEN_ACTION_SUMMARY", current_capture_id=state.capture_id,
        current_frame_hash=state.frame_hash,
    )


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
    assert state.reason == "ambiguous_trusted_page_signatures"


def test_different_priority_conflicting_specific_pages_also_fail_closed():
    kernel = RuntimeNavigationKernel((
        PageSignature(
            "higher", "HIGHER_PAGE", PageKind.TRUSTED,
            required_all=frozenset({"shared"}), priority=50,
        ),
        PageSignature(
            "lower", "LOWER_PAGE", PageKind.TRUSTED,
            required_all=frozenset({"shared"}), priority=5,
        ),
    ))

    state = kernel.classify(PagePerception.from_facts({"shared"}))

    assert state.is_unknown
    assert state.reason == "ambiguous_trusted_page_signatures"


def test_action_contract_is_capability_and_overlay_driven():
    contract = ActionContract(
        "open_action_summary",
        ActionPrimitive.OPEN_ENTRY,
        allowed_pre_pages=frozenset({"GLOBAL_PREP_PAGE"}),
        required_capabilities=frozenset({"OPEN_ACTION_SUMMARY"}),
        allowed_post_pages=frozenset({"ACTION_SUMMARY_VISIBLE"}),
    )
    ready = normalize_legacy_state(
        "GLOBAL_PREP_PAGE", frame_hash="ready", capture_id="capture-2"
    )
    covered = normalize_legacy_state(
        "GLOBAL_PREP_PAGE", overlays=("HELP_OVERLAY",),
        frame_hash="covered", capture_id="capture-3",
    )

    assert contract.authorize(
        ready, current_capture_id="capture-2", current_frame_hash="ready"
    ).allowed
    assert contract.authorize(
        covered, current_capture_id="capture-3", current_frame_hash="covered"
    ).reason == "overlay_blocks_action"
    assert contract.authorize(
        ready, current_capture_id="capture-2", current_frame_hash="ready",
        dispatch_count=1,
    ).reason == (
        "action_dispatch_budget_exhausted"
    )
    assert contract.authorize(
        normalize_legacy_state(
            "UNKNOWN", frame_hash="unknown", capture_id="capture-4"
        ),
        current_capture_id="capture-4",
        current_frame_hash="unknown",
    ).reason == (
        "unknown_pre_state"
    )


def test_capability_is_bound_to_fresh_capture_confidence_and_contract():
    contract = PROVEN_NAVIGATION_CONTRACTS["OPEN_ACTION_SUMMARY"]
    initial = normalize_legacy_state(
        "ACTION_SUMMARY_ENTRY_VISIBLE",
        frame_hash="initial-hash",
        capture_id="capture-1",
    )
    fresh = normalize_legacy_state(
        "ACTION_SUMMARY_ENTRY_VISIBLE",
        frame_hash="fresh-hash",
        capture_id="capture-2",
    )

    stale = contract.authorize(
        initial,
        current_capture_id=fresh.capture_id,
        current_frame_hash=fresh.frame_hash,
    )
    rebound = confirm_fresh_capability(initial, fresh, contract)

    assert stale.reason == "capability_capture_identity_stale"
    assert rebound.allowed
    assert rebound.action_id == "open_action_summary"
    assert rebound.capture_id == "capture-2"
    assert fresh.has_capability(
        "OPEN_ACTION_SUMMARY",
        current_capture_id="capture-2",
        current_frame_hash="fresh-hash",
    )
    assert not initial.has_capability(
        "OPEN_ACTION_SUMMARY",
        current_capture_id="capture-2",
        current_frame_hash="fresh-hash",
    )


def test_medium_confidence_and_missing_capture_identity_do_not_authorize_capability():
    contract = PROVEN_NAVIGATION_CONTRACTS["OPEN_ACTION_SUMMARY"]
    medium = normalize_legacy_state(
        "GLOBAL_PREP_PAGE", confidence=Confidence.MEDIUM,
        frame_hash="medium", capture_id="capture-medium",
    )
    identity_missing = normalize_legacy_state("GLOBAL_PREP_PAGE")

    assert contract.authorize(
        medium,
        current_capture_id="capture-medium",
        current_frame_hash="medium",
    ).reason == "capability_confidence_not_high"
    assert contract.authorize(
        identity_missing,
        current_capture_id="",
        current_frame_hash="",
    ).reason == "capability_capture_identity_missing"


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


def test_transition_classifier_rejects_duplicate_non_source_capture_as_stale():
    contract = PROVEN_NAVIGATION_CONTRACTS["OPEN_GLOBAL_PREP"]
    source = normalize_legacy_state(
        "ACTIVITY_OVERVIEW_VISIBLE", frame_hash="source", capture_id="capture-1"
    )
    classifier = TransitionClassifier(contract, source_state=source)
    first = classifier.observe(UiState(
        "UNKNOWN", frame_hash="changing-1", capture_id="capture-2"
    ))
    duplicate = classifier.observe(UiState(
        "UNKNOWN", frame_hash="changing-1", capture_id="capture-2"
    ))

    assert first.classification is TransitionClassification.UNKNOWN_RECOVERABLE
    assert duplicate.classification is TransitionClassification.STALE


def test_optional_overlay_precedes_expected_background_page():
    contract = PROVEN_NAVIGATION_CONTRACTS["OPEN_GLOBAL_PREP"]
    source = normalize_legacy_state(
        "ACTIVITY_OVERVIEW_VISIBLE", frame_hash="source", capture_id="capture-1"
    )
    covered_post = normalize_legacy_state(
        "GLOBAL_PREP_PAGE", overlays=("OPTIONAL_OVERLAY_VISIBLE",),
        frame_hash="covered", capture_id="capture-2",
    )

    decision = TransitionClassifier(contract, source_state=source).observe(covered_post)

    assert decision.classification is TransitionClassification.OPTIONAL_OVERLAY_REACHED
    assert decision.success


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


def test_proven_adapters_normalize_to_reusable_capabilities_without_assets_authority():
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

    assert not assets.has_capability(
        "OPEN_INVENTORY", current_capture_id="home", current_frame_hash=assets.frame_hash
    )
    assert city.has_capability(
        "ENTER_CITY", current_capture_id="home", current_frame_hash=city.frame_hash
    )
    assert terminal.has_capability(
        "OPEN_ACTION_TERMINAL",
        current_capture_id="home",
        current_frame_hash=terminal.frame_hash,
    )
    assert global_prep.has_capability(
        "OPEN_ACTION_SUMMARY",
        current_capture_id="global-prep",
        current_frame_hash=global_prep.frame_hash,
    )

    assert not PROVEN_NAVIGATION_CONTRACTS["OPEN_INVENTORY"].authorize(
        assets, current_capture_id="home", current_frame_hash=assets.frame_hash
    ).allowed
    assert PROVEN_NAVIGATION_CONTRACTS["ENTER_CITY"].authorize(
        city, current_capture_id="home", current_frame_hash=city.frame_hash
    ).allowed
    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_ACTION_TERMINAL"].authorize(
        terminal, current_capture_id="home", current_frame_hash=terminal.frame_hash
    ).allowed
    overview = normalize_legacy_state(
        "ACTIVITY_OVERVIEW_VISIBLE", frame_hash="overview", capture_id="overview"
    )
    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_GLOBAL_PREP"].authorize(
        overview, current_capture_id="overview", current_frame_hash="overview"
    ).allowed
    assert PROVEN_NAVIGATION_CONTRACTS["OPEN_ACTION_SUMMARY"].authorize(
        global_prep,
        current_capture_id="global-prep",
        current_frame_hash=global_prep.frame_hash,
    ).allowed
    assert global_prep.base_page == "GLOBAL_PREP_PAGE"


def test_open_inventory_contract_names_cube_control_not_assets_or_toolbar_parent():
    contract = PROVEN_NAVIGATION_CONTRACTS["OPEN_INVENTORY"]
    assert contract.candidate_policy == "UNIQUE_TOP_RIGHT_BACKPACK_CUBE_CONTROL"
    assert contract.hit_target_policy == "DYNAMIC_CUBE_INSET_SAFE_REGION"
    assert "ASSETS" not in contract.candidate_policy
    assert "PARENT" not in contract.hit_target_policy


def test_kernel_import_does_not_load_capture_input_or_ocr_backends():
    script = (
        "import sys; import core.services.runtime_navigation_kernel; "
        "blocked=('core.control.control','core.control.adb','core.control.nemu',"
        "'core.image.ocr'); print([name for name in blocked if name in sys.modules])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip().splitlines()[-1] == "[]"
