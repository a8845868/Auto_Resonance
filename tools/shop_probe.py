"""Capture ADB/OCR evidence while reverse engineering shop pages.

The tool deliberately talks to the configured TCP ADB endpoint instead of the
global automation controller.  This keeps discovery captures reproducible even
when the GUI owns the normal runtime lease.  It never confirms a purchase.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import cv2 as cv
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.control.adb import ADB  # noqa: E402
DEFAULT_OUTPUT = ROOT / "artifacts" / "shop_discovery" / date.today().isoformat()
DEFAULT_CONTENT_REGION = (580, 140, 1260, 665)


@dataclass(frozen=True)
class CaptureRecord:
    image: str
    ocr: str
    difference: float | None
    texts: list[str]


def _safe_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value.strip()
    ).strip("-")
    if not cleaned:
        raise ValueError("证据名称不能为空")
    return cleaned


def _content_difference(
    previous: np.ndarray,
    current: np.ndarray,
    region: tuple[int, int, int, int] = DEFAULT_CONTENT_REGION,
) -> float:
    """Return mean grayscale difference for the scrollable product region."""
    x1, y1, x2, y2 = region
    before = cv.cvtColor(previous[y1:y2, x1:x2], cv.COLOR_BGR2GRAY)
    after = cv.cvtColor(current[y1:y2, x1:x2], cv.COLOR_BGR2GRAY)
    return float(np.mean(cv.absdiff(before, after)))


def save_evidence(
    image: np.ndarray,
    output: Path,
    stem: str,
    difference: float | None = None,
    with_ocr: bool = True,
) -> CaptureRecord:
    output.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(stem)
    image_path = output / f"{stem}.png"
    if not cv.imwrite(str(image_path), image):
        raise OSError(f"无法保存截图: {image_path}")

    ocr_path = output / f"{stem}.ocr.json"
    if with_ocr:
        # Keep --no-ocr genuinely lightweight; loading the OCR runtime is slow
        # and otherwise emits provider warnings even for screenshot-only work.
        from core.image.ocr import predict

        ocr_data = predict(image)
    else:
        ocr_data = []
    ocr_path.write_text(
        json.dumps(ocr_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return CaptureRecord(
        image=str(image_path.relative_to(ROOT)),
        ocr=str(ocr_path.relative_to(ROOT)),
        difference=difference,
        texts=[str(item.get("text", "")) for item in ocr_data],
    )


def scan_to_bottom(
    device: ADB,
    output: Path,
    prefix: str,
    swipe: tuple[int, int, int, int],
    region: tuple[int, int, int, int],
    max_pages: int,
    stable_pages: int,
    threshold: float,
    wait_seconds: float,
    with_ocr: bool,
) -> list[CaptureRecord]:
    """Capture every scroll position until consecutive swipes stop changing it."""
    records: list[CaptureRecord] = []
    previous = device.screenshot()
    records.append(save_evidence(previous, output, f"{prefix}-00", with_ocr=with_ocr))
    stable_count = 0
    for index in range(1, max_pages + 1):
        device.input_swipe(*swipe, millisecond=650)
        time.sleep(wait_seconds)
        current = device.screenshot()
        difference = _content_difference(previous, current, region)
        records.append(
            save_evidence(
                current,
                output,
                f"{prefix}-{index:02d}",
                difference=difference,
                with_ocr=with_ocr,
            )
        )
        stable_count = stable_count + 1 if difference <= threshold else 0
        previous = current
        if stable_count >= stable_pages:
            break
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="商店页面 ADB/OCR 证据采集")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置中的 ADB 端口")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-ocr", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="保存当前画面")
    capture.add_argument("name")

    tap = commands.add_parser("tap", help="点击后保存画面")
    tap.add_argument("name")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    tap.add_argument("--wait", type=float, default=1.5)

    scan = commands.add_parser("scan", help="逐页上滑，直到商品区域稳定")
    scan.add_argument("prefix")
    # Short swipes keep the two-row shop grid overlapped between captures, so a
    # middle row cannot be skipped between the first and second screenshots.
    scan.add_argument("--swipe", nargs=4, type=int, default=(900, 585, 900, 365))
    scan.add_argument("--region", nargs=4, type=int, default=DEFAULT_CONTENT_REGION)
    scan.add_argument("--max-pages", type=int, default=12)
    scan.add_argument("--stable-pages", type=int, default=2)
    scan.add_argument("--threshold", type=float, default=4.0)
    scan.add_argument("--wait", type=float, default=1.2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    device = ADB()
    if not device.connect(args.port):
        raise RuntimeError("ADB 连接失败")
    try:
        if args.command == "capture":
            records = [
                save_evidence(
                    device.screenshot(), output, args.name, with_ocr=not args.no_ocr
                )
            ]
        elif args.command == "tap":
            device.input_tap(args.x, args.y)
            time.sleep(args.wait)
            records = [
                save_evidence(
                    device.screenshot(), output, args.name, with_ocr=not args.no_ocr
                )
            ]
        else:
            records = scan_to_bottom(
                device,
                output,
                _safe_name(args.prefix),
                tuple(args.swipe),
                tuple(args.region),
                args.max_pages,
                args.stable_pages,
                args.threshold,
                args.wait,
                not args.no_ocr,
            )
    finally:
        device.kill()

    manifest = output / f"{_safe_name(args.name if args.command != 'scan' else args.prefix)}.manifest.json"
    manifest.write_text(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
