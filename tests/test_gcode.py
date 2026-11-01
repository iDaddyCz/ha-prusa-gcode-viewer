"""G-code decoding, toolpath extraction and the model format."""

from __future__ import annotations

from array import array
import gzip
import json
import math
from pathlib import Path
import struct

import pytest

from custom_components.prusa_gcode_viewer import gcode

from .bgcode_writer import heatshrink_encode, meatpack_encode, write_bgcode

STAR = gzip.decompress((Path(__file__).parent / "fixtures" / "star.gcode.gz").read_bytes())


def read_model(blob: bytes) -> tuple[dict, array, array, bytes, bytes]:
    assert blob[:4] == gcode.MODEL_MAGIC
    (length,) = struct.unpack_from("<I", blob, 4)
    header = json.loads(blob[8 : 8 + length])
    pos = 8 + length
    points = array("H", blob[pos : pos + header["points"] * 6])
    pos += header["points"] * 6
    pos += -pos % 4
    starts = array("I", blob[pos : pos + (header["polylines"] + 1) * 4])
    pos += len(starts) * 4
    n = header["polylines"]
    return header, points, starts, blob[pos : pos + n], blob[pos + n : pos + 2 * n]


@pytest.mark.parametrize("bits", [(11, 4), (12, 4)])
def test_heatshrink_round_trip(bits: tuple[int, int]) -> None:
    data = b"G1 X10 Y10 E.5\n" * 40 + b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" + bytes(range(256))
    assert gcode.heatshrink_decode(heatshrink_encode(data, *bits), *bits) == data


@pytest.mark.parametrize("nospace", [True, False])
def test_meatpack_round_trip(nospace: bool) -> None:
    text = "G1 X117.584 Y102.025 E.00926\n;TYPE:External perimeter\nM73 P4 R11\n\nT1\nG28 W ; home\n"
    assert gcode.meatpack_decode(meatpack_encode(text, nospace)) == text


def test_bgcode_round_trip() -> None:
    text = STAR[:30_000].decode()
    text = text[: text.rindex("\n") + 1]
    data = write_bgcode(text, {"filament_colour": "#123456", "bed_shape": "0x0,250x0,250x220,0x220"})
    chunks, meta = gcode.read_bgcode(data)
    assert meta["filament_colour"] == "#123456"
    assert meta["Producer"] == "tests"
    assert list(gcode.iter_lines(chunks)) == text.split("\n")[:-1]


def test_bgcode_checksum_mismatch() -> None:
    data = bytearray(write_bgcode("G1 X1 Y1 E1\n", {}))
    data[-10] ^= 0xFF
    with pytest.raises(gcode.GCodeError, match="checksum"):
        gcode.read_bgcode(bytes(data))


def test_not_gcode() -> None:
    with pytest.raises(gcode.GCodeError):
        gcode.parse_file(b"\x89PNG not a print")


def test_text_and_binary_give_the_same_model() -> None:
    text = STAR.decode()
    head = text[: text.index(";LAYER_CHANGE\n;Z:0.6")]  # the test encoder is too slow for all of it
    via_text = gcode.serialize(gcode.parse_file(head.encode()))
    via_binary = gcode.serialize(gcode.parse_file(write_bgcode(head, {})))
    assert via_text == via_binary


def test_star_model() -> None:
    header, points, starts, types, tools = read_model(gcode.build_model(STAR, "star.gcode"))
    assert header["name"] == "star.gcode"
    assert header["bed"] == [[0, 0], [248, 0], [248, 205], [0, 205]]
    assert header["tools"] == ["#FF8000"]
    assert header["estimated_time"] == "10m 24s"
    assert {"External perimeter", "Perimeter", "Internal infill"} <= set(header["types"])
    zs = [layer["z"] for layer in header["layers"]]
    assert zs == sorted(zs)
    assert zs[-1] == 12.0
    assert len(zs) == 60
    # Layer starts index the (sorted) polylines, and every polyline's points stay in bounds.
    assert [layer["start"] for layer in header["layers"]] == sorted(layer["start"] for layer in header["layers"])
    assert starts[-1] == header["points"] == len(points) // 3
    lo, hi = header["bounds"]
    s = header["scale"]
    for axis in range(3):
        values = points[axis::3]
        assert min(values) * s + lo[axis] == pytest.approx(lo[axis], abs=s)
        assert max(values) * s + lo[axis] == pytest.approx(hi[axis], abs=s)
    assert max(tools) == 0
    assert max(types) < len(header["types"])


SYNTHETIC = """\
; filament_colour = #AA0000;;#00BB00
; extruder_colour = #000001;#000002;#000003
M83
G28
G1 Z0.2
T0
;TYPE:External perimeter
G1 X10 Y10
G1 X20 Y10 E1
G1 F3000
G1 X20 Y20 E1
G1 E-0.8
G1 X20 Y20.02 E0.001
G1 X20 Y30 E1
G1 X30 Y30
G1 X40 Y30 E1
T2
G3 X50 Y40 I0 J10 E2
M82
G92 E0
G1 X60 Y40 E1
G1 X70 Y40 E1
G1 X80 Y40 E0.5
"""


def test_synthetic_paths() -> None:
    paths = gcode.parse_file(SYNTHETIC.encode())
    starts = [*paths.starts, len(paths.points) // 3]
    polylines = [
        [tuple(round(v, 3) for v in paths.points[j * 3 : j * 3 + 3]) for j in range(a, b)]
        for a, b in zip(starts, starts[1:])
    ]
    # The feed-rate line and the retraction don't break the path; the tiny move folds into the next.
    assert polylines[0] == [(10, 10, 0.2), (20, 10, 0.2), (20, 20, 0.2), (20, 30, 0.2)]
    assert polylines[1] == [(30, 30, 0.2), (40, 30, 0.2)]
    # Tool change starts a new polyline; the counter-clockwise quarter arc stays on its circle.
    arc, tail = polylines[2][:-1], polylines[2][-1]
    assert arc[0] == (40, 30, 0.2)
    assert arc[-1] == (50, 40, 0.2)
    assert len(arc) > 10
    assert all(math.hypot(x - 40, y - 40) == pytest.approx(10, abs=0.01) for x, y, _ in arc)
    assert all(x >= 40 - 1e-6 and y <= 40 + 1e-6 for x, y, _ in arc)
    # Absolute E after G92: the first move extrudes and continues the path, the rest don't.
    assert tail == (60, 40, 0.2)
    assert len(polylines) == 3
    assert list(paths.tools) == [0, 0, 2]
    header, *_ = read_model(gcode.serialize(paths))
    # filament_colour wins, empty entries fall back to extruder_colour.
    assert header["tools"] == ["#AA0000", "#000002", "#00BB00"]
