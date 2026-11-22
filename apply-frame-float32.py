#!/usr/bin/env python3
"""Apply a low-memory patch to the BeyondGTAClips engine's frame analysis.

The engine's frame analysis (bgt/frame.py) computes grayscale frames with
`frames.mean(axis=3)`, which produces float64 temporaries: for a 36s clip
sampled at 2 fps / 640 px wide that is ~133 MB per array, and `motion()`
holds two of them (the mean AND the diff) at once — ~264 MB of float64
temporaries during every clip's prepare step.

This patch switches both mean() calls to dtype=np.float32, halving those
temporaries (~132 MB saved) with no meaningful precision loss: the motion
scores are rounded to 2 decimals and the thresholds are ~1.2 on a 0-255
scale, far above float32's precision. Verified locally: identical motion
scores (28.74 / 26.32 / 27.11 / 21.53) and identical clip picks/templates
before and after the patch; Phase-1 peak memory dropped 767 MB -> 611 MB.

Run at Docker build time on the cloned engine (see Dockerfile). Fails
loudly (exit 1) if an expected string is missing or appears more than
once, so a future engine change breaks the build instead of silently
skipping the patch.

Usage: apply-frame-float32.py <path/to/bgt/frame.py>
"""
import sys
from pathlib import Path

PATCHES = [
    # tracks(): the grayscale array used for per-face motion.
    ("    gray = frames.mean(axis=3)\n",
     "    gray = frames.mean(axis=3, dtype=np.float32)\n"),
    # motion(): the grayscale array + its diff (two float64 temporaries).
    ("    g = frames.mean(axis=3)\n",
     "    g = frames.mean(axis=3, dtype=np.float32)\n"),
]


def main():
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    for old, new in PATCHES:
        count = text.count(old)
        if count != 1:
            print(f"FAIL: expected exactly 1 occurrence of {old!r}, found {count} in {path}",
                  file=sys.stderr)
            sys.exit(1)
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"OK: applied {len(PATCHES)} float32 replacements in {path}")


if __name__ == "__main__":
    main()
