import pytest

from core.services.personal_city_target import PersonalCityTarget, resolve_personal_city_anchor


def test_default_target_is_exact_instance_package_and_city():
    target = PersonalCityTarget()
    target.validate()
    assert (target.instance_index, target.package_id, target.city_id, target.selector_type) == (
        0, "com.hermes.goda", "岚心城", "RESOLVER_BOUND_TARGET"
    )


def test_unique_visit_anchor_with_one_or_more_city_labels_resolves():
    items = [
        {"text": "岚心城", "bbox": [1, 1, 10, 10]},
        {"text": "访问城市", "bbox": [100, 100, 180, 140]},
        {"text": "岚心城", "bbox": [100, 145, 160, 170]},
    ]
    assert resolve_personal_city_anchor(items) == (100, 100, 180, 140)


def test_ambiguous_anchor_or_missing_city_stops():
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_personal_city_anchor([
            {"text": "岚心城", "bbox": [1, 1, 10, 10]},
            {"text": "访问城市", "bbox": [1, 1, 2, 2]},
            {"text": "访问城市", "bbox": [3, 3, 4, 4]},
        ])
    with pytest.raises(ValueError, match="label_missing"):
        resolve_personal_city_anchor([{"text": "访问城市", "bbox": [1, 1, 2, 2]}])
