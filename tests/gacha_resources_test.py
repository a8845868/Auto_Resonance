from auto.gacha_resources import parse_recruit_resource_counts


def box(x1, y1, x2, y2, text):
    return {"text": text, "position": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]}


def test_parse_recruit_top_bar_counts():
    items = [
        box(1040, 29, 1059, 45, "27"),
        box(1191, 26, 1253, 49, "1280+"),
        box(1061, 630, 1099, 649, "×10"),
    ]
    assert parse_recruit_resource_counts(items) == {"tickets": 27, "stones": 1280}


def test_parse_recruit_counts_supports_commas():
    items = [box(1032, 28, 1060, 46, "105"), box(1170, 25, 1250, 50, "12,800+")]
    assert parse_recruit_resource_counts(items) == {"tickets": 105, "stones": 12800}
