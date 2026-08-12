import pytest

from core.services.personal_city_target import PersonalCityTarget, resolve_personal_city_anchor


def test_default_target_is_exact_instance_package_and_city():
    target = PersonalCityTarget()
    target.validate()
    assert (target.instance_index, target.package_id, target.city_id, target.selector_type) == (
        0, "com.hermes.goda", "岚心城", "RESOLVER_BOUND_TARGET"
    )


def test_unique_visit_anchor_and_target_label_in_bound_region_resolve():
    items = [
        {"text": "岚心城", "bbox": [85, 136, 125, 151]},
        {"text": "访问城市", "bbox": [1129, 474, 1216, 500]},
        {"text": "岚心城", "bbox": [1128, 499, 1175, 518]},
    ]
    assert resolve_personal_city_anchor(items, (1280, 720)) == (1129, 474, 1216, 500)


def test_ambiguous_anchor_or_missing_city_stops():
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_personal_city_anchor(
            [
                {"text": "岚心城", "bbox": [1128, 499, 1175, 518]},
                {"text": "访问城市", "bbox": [1129, 474, 1216, 500]},
                {"text": "访问城市", "bbox": [1132, 476, 1218, 502]},
            ],
            (1280, 720),
        )
    with pytest.raises(ValueError, match="label_missing"):
        resolve_personal_city_anchor(
            [{"text": "访问城市", "bbox": [1129, 474, 1216, 500]}],
            (1280, 720),
        )


@pytest.mark.parametrize("dimensions", [(851, 480), (853, 480), (1280, 720)])
def test_spatial_binding_is_resolution_independent(dimensions):
    width, height = dimensions
    box = lambda left, top, right, bottom: [
        round(left * width), round(top * height), round(right * width), round(bottom * height)
    ]
    items = [
        {"text": "访问城市", "bbox": box(0.885, 0.655, 0.95, 0.69)},
        {"text": "岚心城", "bbox": box(0.89, 0.695, 0.94, 0.72)},
    ]
    assert resolve_personal_city_anchor(items, dimensions) == tuple(items[0]["bbox"])


def test_label_or_anchor_outside_target_region_fails_closed():
    with pytest.raises(ValueError, match="label_missing"):
        resolve_personal_city_anchor(
            [
                {"text": "访问城市", "bbox": [1129, 474, 1216, 500]},
                {"text": "岚心城", "bbox": [85, 136, 125, 151]},
            ],
            (1280, 720),
        )
    with pytest.raises(ValueError, match="anchor_missing"):
        resolve_personal_city_anchor(
            [
                {"text": "访问城市", "bbox": [100, 100, 200, 130]},
                {"text": "岚心城", "bbox": [1128, 499, 1175, 518]},
            ],
            (1280, 720),
        )


def test_multiple_target_labels_or_invalid_spatial_relation_fail_closed():
    with pytest.raises(ValueError, match="label_ambiguous"):
        resolve_personal_city_anchor(
            [
                {"text": "访问城市", "bbox": [1129, 474, 1216, 500]},
                {"text": "岚心城", "bbox": [1128, 499, 1175, 518]},
                {"text": "岚心城", "bbox": [1180, 500, 1220, 520]},
            ],
            (1280, 720),
        )
    with pytest.raises(ValueError, match="spatial_relation"):
        resolve_personal_city_anchor(
            [
                {"text": "访问城市", "bbox": [1129, 510, 1216, 530]},
                {"text": "岚心城", "bbox": [1128, 480, 1175, 500]},
            ],
            (1280, 720),
        )
