from __future__ import annotations

import cv2 as cv
import numpy as np


class Frame:
    def __init__(self, image, texts, *, metadata=None):
        self.image = image
        self._texts = list(texts)
        self.scenario_metadata = metadata or {}
        self.announcement_dialog_bbox = self.scenario_metadata.get("dialog_bounds")

    def ocr(self):
        return list(self._texts)


def item(text, bbox, score=0.99):
    return {"text": text, "bbox": list(bbox), "score": score}


def announcement_frame(*, with_safe_band=True):
    image = np.full((480, 851, 3), 28, dtype=np.uint8)
    cv.rectangle(image, (68, 47), (778, 422), (180, 180, 180), 2)
    cv.line(image, (68, 93), (778, 93), (190, 190, 190), 2)
    if not with_safe_band:
        for y in range(424, 469, 4):
            cv.line(image, (0, y), (850, y), (255, 255, 255), 1)
    return Frame(
        image,
        [item("公告", (566, 64, 619, 86)), item("触碰空白区域退出", (398, 440, 471, 453))],
    )


def daily_frame():
    image = np.full((720, 1280, 3), 35, dtype=np.uint8)
    cv.rectangle(image, (174, 64), (1105, 644), (160, 160, 160), 2)
    return Frame(
        image,
        [
            item("每日签到奖励", (311, 78, 560, 122)),
            item("已领取", (1012, 584, 1071, 608)),
            item("触碰空白区域退出", (604, 675, 712, 692)),
            item("今日奖励", (847, 486, 933, 504)),
        ],
        metadata={"overlay_bounds": [0, 0, 1280, 720], "dialog_bounds": [174, 64, 1105, 644]},
    )


def home_frame(*, anchor=(1129, 474, 1216, 500)):
    return Frame(
        np.full((720, 1280, 3), 35, dtype=np.uint8),
        [
            item("岚心城", (85, 136, 125, 151)),
            item("作战终端", (1148, 395, 1233, 423)),
            item("访问城市", anchor),
            item("岚心城", (1128, 499, 1175, 518)),
            item("启程", (1173, 652, 1209, 672)),
        ],
    )


def city_frame():
    return Frame(
        np.full((720, 1280, 3), 35, dtype=np.uint8),
        [
            item("岚心城", (176, 535, 279, 572)),
            item("市政厅", (279, 23, 352, 49)),
            item("休息区", (632, 167, 716, 193)),
            item("交易所", (855, 267, 952, 294)),
            item("商会", (448, 281, 492, 300)),
        ],
    )


def unknown_frame():
    return Frame(np.zeros((720, 1280, 3), dtype=np.uint8), [])


def session_entry_frame(*, anchors=1):
    texts = [item("138****1258", (590, 576, 690, 600))]
    texts.extend(
        item("点击屏幕进入游戏", (560, 540 + index * 32, 720, 568 + index * 32))
        for index in range(anchors)
    )
    return Frame(np.full((720, 1280, 3), 20, dtype=np.uint8), texts)


def resource_update_frame(*, progress="0%", confirmations=1, extra_texts=(), size="25.25MB"):
    texts = [
        item(f"需要下载资源包（共{size}）", (468, 345, 795, 376)),
        item(progress, (1211, 601, 1241, 622)),
    ]
    texts.extend(
        item("确认", (643 + index * 80, 491, 691 + index * 80, 521))
        for index in range(confirmations)
    )
    texts.extend(
        item(text, (100, 100 + index * 30, 300, 125 + index * 30))
        for index, text in enumerate(extra_texts)
    )
    return Frame(np.full((720, 1280, 3), 20, dtype=np.uint8), texts)


def resource_update_complete_frame(
    *,
    width=1280,
    height=720,
    download_text="下载已经完成",
    enter_text="点击任意位置进入游戏",
):
    texts = []
    if download_text:
        texts.append(item(download_text, (468, 546, 630, 574)))
    if enter_text:
        texts.append(item(enter_text, (620, 546, 820, 574)))
    return Frame(np.full((height, width, 3), 20, dtype=np.uint8), texts)
