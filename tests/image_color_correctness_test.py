import cv2 as cv
import numpy as np
import pytest

from core.image.image import Image
from core.image.utils import get_bgrs


def _synthetic_bgr_image(height=6, width=7):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        for x in range(width):
            image[y, x] = (y * 10 + x, 100 + y, 200 + x)
    return image


def test_single_and_multiple_bgr_reads_use_absolute_xy_after_crop():
    source = _synthetic_bgr_image()
    image = Image(source.copy()).crop_image((2, 1), (6, 5))

    assert tuple(image.get_bgr((3, 2))) == tuple(source[2, 3])
    assert [tuple(color) for color in image.get_bgrs([(2, 1), (3, 2)])] == [
        tuple(source[1, 2]),
        tuple(source[2, 3]),
    ]


def test_get_bgrs_empty_positions_returns_empty_list():
    source = _synthetic_bgr_image()
    image = Image(source).crop_image((2, 1), (6, 5))

    assert image.get_bgrs([]) == []


def test_color_reads_reject_positive_and_negative_out_of_bounds_positions():
    source = _synthetic_bgr_image()

    with pytest.raises(IndexError):
        get_bgrs(source, [(99, 99)])
    with pytest.raises(IndexError):
        get_bgrs(source, [(-1, 0)])
    with pytest.raises(IndexError):
        Image(source).get_bgr((0, -1))


def test_get_bgrs_supports_non_contiguous_images():
    source = _synthetic_bgr_image()
    view = source[::2, ::2]
    assert not view.flags.c_contiguous

    colors = get_bgrs(view, [(0, 0), (2, 1)])

    assert [tuple(color) for color in colors] == [
        tuple(view[0, 0]),
        tuple(view[1, 2]),
    ]


def test_get_hsv_interprets_source_pixel_as_bgr():
    source = np.array([[[10, 80, 200]]], dtype=np.uint8)
    expected = cv.cvtColor(source, cv.COLOR_BGR2HSV)[0, 0]

    assert tuple(Image(source).get_hsv((0, 0))) == tuple(expected)
