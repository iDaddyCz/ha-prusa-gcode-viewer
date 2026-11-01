"""Writes the twisted-star STL that tests/fixtures/star.gcode.gz was sliced from.

    python3 scripts/make_fixture_model.py star.stl
    prusa-slicer --export-gcode \
        --printer-profile "Prusa CORE One INDX 8T 0.4 HF, 0.4 HF, 0.4 HF, 0.4 HF, 0.4 HF, 0.4 HF, 0.4 HF, 0.4 HF" \
        --print-profile "0.20mm" --tool-print-profile "0.20mm Balanced ★ @COREONEINDX 0.4HF" \
        --material-profile "Prusament PLA @COREONEINDX HF0.4@COREONEINDX 0.4" -o star.gcode star.stl
    gzip -9 star.gcode

(PrusaSlicer 3.0.0-alpha12.)
"""

from __future__ import annotations

import math
import struct
import sys

POINTS, OUTER, INNER, HEIGHT, LAYERS, TWIST = 5, 14.0, 7.0, 12.0, 24, math.radians(72)


def ring(z: float, twist: float) -> list[tuple[float, float, float]]:
    pts = []
    for i in range(POINTS * 2):
        r = OUTER if i % 2 == 0 else INNER
        a = math.pi * i / POINTS + twist
        pts.append((r * math.cos(a), r * math.sin(a), z))
    return pts


def main(path: str) -> None:
    rings = [ring(HEIGHT * k / LAYERS, TWIST * k / LAYERS) for k in range(LAYERS + 1)]
    tris = []
    n = POINTS * 2
    for lo, hi in zip(rings, rings[1:]):
        for i in range(n):
            j = (i + 1) % n
            tris += [(lo[i], lo[j], hi[j]), (lo[i], hi[j], hi[i])]
    for cap, z, flip in ((rings[0], 0.0, True), (rings[-1], HEIGHT, False)):
        c = (0.0, 0.0, z)
        for i in range(n):
            a, b = cap[i], cap[(i + 1) % n]
            tris.append((c, b, a) if flip else (c, a, b))
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v) for v in t) + b"\0\0")


if __name__ == "__main__":
    main(sys.argv[1])
