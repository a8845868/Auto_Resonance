"""Offline OCR backend comparison — PP-OCRv4 vs PP-OCRv6 Medium.

Reads pre-captured screenshots, runs both backends on each, and prints a
comparison table.  No emulator, no input, no network after model load.

Usage:  python tools/ocr_benchmark.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _init_backend(env, label):
    import core.image.ocr as ocr

    ocr._reset_ocr_backend_for_tests()
    os.environ["AUTO_RESONANCE_OCR_BACKEND"] = env
    t0 = time.perf_counter()
    backend = ocr._get_backend()
    init_ms = (time.perf_counter() - t0) * 1000
    print(f"  {label}: {backend.name} provider={backend.provider}, init={init_ms:.0f}ms")
    return backend


def _pixel_size(frame_path):
    import cv2

    img = cv2.imread(str(frame_path))
    if img is None:
        return 0, 0
    return img.shape[0], img.shape[1]


def _extract_ocr_artifacts(ocr_items):
    texts = [item.get("text", "") for item in ocr_items]
    scores = [item.get("score", 0) for item in ocr_items]
    return {
        "count": len(texts),
        "texts": texts,
        "mean_score": float(sum(scores) / len(scores)) if scores else 0.0,
        "digits": [t for t in texts if _is_digit_like(t)],
        "chinese": [t for t in texts if _is_chinese_like(t)],
        "empty_count": sum(1 for t in texts if not t.strip()),
    }


def _is_digit_like(text):
    return bool(text.strip()) and all(c in "0123456789,./kKmM万 " for c in text.strip())


def _is_chinese_like(text):
    return any("一" <= c <= "鿿" for c in text)


def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _digit_overlap(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return True, 0, 0
    missing = sa - sb
    extra = sb - sa
    return len(missing) == 0 and len(extra) == 0, len(missing), len(extra)


def main():
    import cv2

    artifact_dir = Path("artifacts/shop_discovery/2026-07-13")
    candidates = sorted(
        {
            p.name: p
            for p in artifact_dir.rglob("*.png")
            if "bureau-full" in p.name
            or "headquarters-full" in p.name
            or "dialog" in p.name.lower()
            or "page-0" in p.name
            or "single-item" in p.name
        }.values(),
    )

    if not candidates:
        print("No offline frames found.  Skipping benchmark.")
        return

    print(f"Comparing PP-OCRv4 vs PP-OCRv6 Medium on {len(candidates)} frames\n")

    # Warm up both backends (first-run penalty is one-time)
    v4 = _init_backend("ppocr-v4", "V4 ")
    v6 = _init_backend("ppocr-v6-medium", "V6 ")

    results = []
    for path in candidates:
        h, w = _pixel_size(path)
        label = f"{path.name} ({w}x{h})"
        img = cv2.imread(str(path))
        if img is None:
            print(f"  SKIP {label} — unreadable")
            continue

        row = {"frame": path.name, "size": f"{w}x{h}"}
        for tag, backend in [("v4", v4), ("v6", v6)]:
            t0 = time.perf_counter()
            try:
                raw = backend.predict(img, no_crop=True)
            except Exception as exc:
                row[f"{tag}_error"] = f"{type(exc).__name__}: {exc}"
                row[f"{tag}_ms"] = -1
                continue
            elapsed = (time.perf_counter() - t0) * 1000
            art = _extract_ocr_artifacts(raw)
            row[f"{tag}_ms"] = round(elapsed, 1)
            row[f"{tag}_items"] = art["count"]
            row[f"{tag}_texts"] = art["texts"]
            row[f"{tag}_digits"] = art["digits"]
        results.append(row)

    # --- Summary ---
    print(f"\n{'Frame':<48} {'V4 items':>8} {'V6 items':>8} {'V4 ms':>8} {'V6 ms':>8} {'Jaccard':>8} {'Digits ok':>8}")
    print("-" * 104)
    total_v4_texts = []
    total_v6_texts = []
    total_v4_ms = 0
    total_v6_ms = 0
    digit_broken = 0
    for r in results:
        v4t = r.get("v4_texts", [])
        v6t = r.get("v6_texts", [])
        jac = _jaccard(v4t, v6t)
        d_ok, d_miss, d_extra = _digit_overlap(r.get("v4_digits", []), r.get("v6_digits", []))
        if not d_ok:
            digit_broken += 1
        d_flag = "OK" if d_ok else f"MISS{d_miss}/EXTRA{d_extra}"
        v4ms = r.get("v4_ms", -1)
        v6ms = r.get("v6_ms", -1)
        total_v4_ms += max(0, v4ms)
        total_v6_ms += max(0, v6ms)
        total_v4_texts.extend(v4t)
        total_v6_texts.extend(v6t)
        print(
            f"{r['frame'][:47]:<48} "
            f"{r.get('v4_items', 0):>8} "
            f"{r.get('v6_items', 0):>8} "
            f"{v4ms:>7.1f} "
            f"{v6ms:>7.1f} "
            f"{jac:>8.3f} "
            f"{d_flag:>8}"
        )
        if r.get("v4_error"):
            print(f"  V4 ERROR: {r['v4_error']}")
        if r.get("v6_error"):
            print(f"  V6 ERROR: {r['v6_error']}")

    count = len(results)
    overall_j = _jaccard(total_v4_texts, total_v6_texts)
    print(f"\n{count} frames total")
    print(f"V4 unique texts: {len(set(total_v4_texts))}, V6 unique texts: {len(set(total_v6_texts))}")
    print(f"Overall Jaccard: {overall_j:.4f}")
    print(f"Digit mismatches: {digit_broken}/{count}")
    print(f"V4 total time: {total_v4_ms:.0f}ms  V6 total time: {total_v6_ms:.0f}ms")
    if total_v4_ms > 0 and total_v6_ms > 0:
        ratio = total_v6_ms / total_v4_ms
        print(f"V6/V4 speed ratio: {ratio:.2f}x  ({'slower' if ratio > 1 else 'faster'})")

    # Show text differences on frames where Jaccard < 0.7
    low_j = [r for r in results if _jaccard(r.get("v4_texts", []), r.get("v6_texts", [])) < 0.7]
    if low_j:
        print(f"\n--- Low-overlap frames ({len(low_j)}) ---")
        for r in low_j:
            v4t = set(r.get("v4_texts", []))
            v6t = set(r.get("v6_texts", []))
            print(f"\n{r['frame']}")
            print(f"  V4-only: {sorted(v4t - v6t)[:30]}")
            print(f"  V6-only: {sorted(v6t - v4t)[:30]}")


if __name__ == "__main__":
    main()
