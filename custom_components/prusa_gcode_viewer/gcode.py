"""Turns PrusaSlicer G-code (plain text or binary .bgcode) into a compact toolpath model.

Pure Python with no Home Assistant imports, so it runs in an executor and in tests.
The binary format is described at https://github.com/prusa3d/libbgcode/blob/main/doc/specifications.md
"""

from __future__ import annotations

from array import array
import codecs
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
import json
import math
import re
import struct
import zlib

MODEL_MAGIC = b"PGV1"
MODEL_VERSION = 1

BGCODE_MAGIC = b"GCDE"
BLOCK_GCODE = 1
BLOCK_THUMBNAIL = 5
COMPRESSION_NONE, COMPRESSION_DEFLATE, COMPRESSION_HS_11_4, COMPRESSION_HS_12_4 = range(4)
ENCODING_NONE = 0

# Segments shorter than this are merged into the next one; arcs are split so the chord
# strays at most ARC_TOLERANCE from the true arc.
MIN_SEGMENT = 0.05
ARC_TOLERANCE = 0.02
MAX_ARC_STEPS = 128

DEFAULT_COLORS = ["#FF8000", "#DB5182", "#3EC0FF", "#FF4F4F", "#FBEB7D", "#7ACC00", "#00CC55", "#00BBB0"]

_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class GCodeError(Exception):
    """The file is not G-code this module can read."""


# --- binary G-code -----------------------------------------------------------------


def heatshrink_decode(src: bytes, window_bits: int, lookahead_bits: int) -> bytes:
    """Decompress one heatshrink stream (a bit stream of literals and back-references)."""
    out = bytearray()
    index_mask = (1 << window_bits) - 1
    count_mask = (1 << lookahead_bits) - 1
    acc = nbits = pos = 0
    end = len(src)
    while True:
        if nbits == 0:
            if pos >= end:
                break
            acc, nbits, pos = src[pos], 8, pos + 1
        nbits -= 1
        if (acc >> nbits) & 1:
            while nbits < 8:
                if pos >= end:
                    return bytes(out)
                acc, nbits, pos = (acc << 8) | src[pos], nbits + 8, pos + 1
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
        else:
            need = window_bits + lookahead_bits
            while nbits < need:
                if pos >= end:
                    return bytes(out)
                acc, nbits, pos = (acc << 8) | src[pos], nbits + 8, pos + 1
            nbits -= window_bits
            offset = ((acc >> nbits) & index_mask) + 1
            nbits -= lookahead_bits
            count = ((acc >> nbits) & count_mask) + 1
            start = len(out) - offset
            if start < 0:
                raise GCodeError("Corrupt heatshrink stream")
            if offset >= count:
                out += out[start : start + count]
            else:
                for i in range(count):
                    out.append(out[start + i])
        acc &= (1 << nbits) - 1
    return bytes(out)


_MP_SIGNAL = 0xFF
_MP_ENABLE, _MP_DISABLE, _MP_RESET, _MP_NOSPACE_ON, _MP_NOSPACE_OFF = 251, 250, 249, 247, 246


def _meatpack_table(nospace: bool) -> list[tuple[str, int, str]]:
    """For each packed byte: (characters to emit, full-width bytes to expect, buffered char)."""
    chars = "0123456789." + ("E" if nospace else " ") + "\nGX"
    table = []
    for b in range(256):
        low, high = b & 0xF, b >> 4
        if low == 0xF:
            table.append(("", 2, "") if high == 0xF else ("", 1, chars[high]))
        elif chars[low] == "\n":
            table.append(("\n", 0, ""))
        elif high == 0xF:
            table.append((chars[low], 1, ""))
        else:
            table.append((chars[low] + chars[high], 0, ""))
    return table


_MP_TABLES = (_meatpack_table(False), _meatpack_table(True))


def meatpack_decode(src: bytes) -> str:
    """Undo MeatPack packing, mirroring libbgcode's MeatPack::unbinarize (without re-adding spaces)."""
    out: list[str] = []
    emit = out.append
    packing = nospace = cmd_active = False
    cmd_count = queue = 0
    pending = ""
    table = _MP_TABLES[0]

    def rx(c: int) -> None:
        nonlocal queue, pending
        if not packing:
            emit(chr(c))
        elif queue:
            emit(chr(c))
            if pending:
                emit(pending)
                pending = ""
            queue -= 1
        else:
            text, queue, buffered = table[c]
            if text:
                emit(text)
            if buffered:
                pending = buffered

    for c in src:
        if c == _MP_SIGNAL:
            if cmd_count:
                cmd_active, cmd_count = True, 0
            else:
                cmd_count = 1
        elif cmd_active:
            if c == _MP_ENABLE:
                packing = True
            elif c in (_MP_DISABLE, _MP_RESET):
                packing = False
            elif c == _MP_NOSPACE_ON:
                nospace = True
            elif c == _MP_NOSPACE_OFF:
                nospace = False
            table = _MP_TABLES[nospace]
            cmd_active = False
        else:
            if cmd_count:
                rx(_MP_SIGNAL)
                cmd_count = 0
            rx(c)
    return "".join(out)


def _decompress(data: bytes, compression: int, size: int) -> bytes:
    if compression == COMPRESSION_NONE:
        return data
    if compression == COMPRESSION_DEFLATE:
        return zlib.decompress(data)
    if compression == COMPRESSION_HS_11_4:
        out = heatshrink_decode(data, 11, 4)
    elif compression == COMPRESSION_HS_12_4:
        out = heatshrink_decode(data, 12, 4)
    else:
        raise GCodeError(f"Unknown compression {compression}")
    if len(out) != size:
        raise GCodeError("Decompressed block has the wrong size")
    return out


def read_bgcode(data: bytes) -> tuple[Iterator[str], dict[str, str]]:
    """G-code text chunks of a .bgcode file, and its metadata key/values.

    Metadata blocks come before the G-code, so the dict is complete before the first
    chunk is read; chunks are decoded lazily to keep memory low.
    """
    if data[:4] != BGCODE_MAGIC:
        raise GCodeError("Not a binary G-code file")
    _version, checksum_type = struct.unpack_from("<IH", data, 4)
    checksum_size = 4 if checksum_type == 1 else 0
    blocks: list[tuple[int, int, int, int, int]] = []
    meta: dict[str, str] = {}
    pos = 10
    while pos < len(data):
        block_type, compression, size = struct.unpack_from("<HHI", data, pos)
        header_len = 8 if compression == COMPRESSION_NONE else 12
        stored = size if compression == COMPRESSION_NONE else struct.unpack_from("<I", data, pos + 8)[0]
        params_len = 6 if block_type == BLOCK_THUMBNAIL else 2
        payload = pos + header_len + params_len
        end = payload + stored
        if end + checksum_size > len(data):
            raise GCodeError("Truncated binary G-code file")
        if checksum_size:
            (expected,) = struct.unpack_from("<I", data, end)
            if zlib.crc32(data[pos:end]) != expected:
                raise GCodeError("Binary G-code checksum mismatch")
        (encoding,) = struct.unpack_from("<H", data, pos + header_len)
        if block_type == BLOCK_GCODE:
            blocks.append((payload, end, compression, size, encoding))
        elif block_type != BLOCK_THUMBNAIL:
            text = _decompress(data[payload:end], compression, size).decode("utf-8", "replace")
            if encoding == ENCODING_NONE:  # INI; JSON metadata (PrusaSlicer 3) is skipped
                for line in text.splitlines():
                    key, sep, value = line.partition("=")
                    if sep:
                        meta.setdefault(key.strip(), value.strip())
        pos = end + checksum_size

    def chunks() -> Iterator[str]:
        for start, stop, compression, size, encoding in blocks:
            raw = _decompress(data[start:stop], compression, size)
            yield raw.decode("ascii", "replace") if encoding == ENCODING_NONE else meatpack_decode(raw)

    return chunks(), meta


def iter_lines(chunks: Iterable[str]) -> Iterator[str]:
    """Lines across chunks whose boundaries may fall mid-line."""
    rest = ""
    for chunk in chunks:
        lines = (rest + chunk).split("\n")
        rest = lines.pop()
        yield from lines
    if rest:
        yield rest


# --- toolpaths ----------------------------------------------------------------------


@dataclass
class Toolpaths:
    """Extrusion polylines: points are x, y, z triples; starts[i] is polyline i's first point."""

    points: array = field(default_factory=lambda: array("f"))
    starts: array = field(default_factory=lambda: array("I"))
    types: bytearray = field(default_factory=bytearray)
    tools: bytearray = field(default_factory=bytearray)
    type_names: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    outline: bool = False  # mesh outlines, not the toolpaths a slicer made

    @property
    def polyline_count(self) -> int:
        return len(self.types)


class _Parser:
    def __init__(self) -> None:
        self.paths = Toolpaths()
        self.x = self.y = self.z = self.e = 0.0
        self.relative = False
        self.relative_e = False
        self.tool = 0
        self.type = self._type_index("Custom")
        self.open = False  # the last polyline can be extended by the next extrusion
        self.config: dict[str, str] = {}

    def _type_index(self, name: str) -> int:
        names = self.paths.type_names
        if name not in names:
            if len(names) == 255:
                return len(names) - 1
            names.append(name)
        return names.index(name)

    def feed(self, line: str) -> None:
        if not line:
            return
        first = line[0]
        if first == ";":
            self._comment(line)
            return
        if first not in "GTM":
            return
        if (semicolon := line.find(";")) >= 0:
            line = line[:semicolon]
        words = _WORD.findall(line)
        if not words:
            return
        letter, number = words[0]
        if letter == "G":
            if number in ("1", "0", "01", "00"):
                self._move(words)
            elif number in ("2", "3", "02", "03"):
                self._arc(words, clockwise=number in ("2", "02"))
            elif number == "90":
                self.relative = self.relative_e = False
            elif number == "91":
                self.relative = self.relative_e = True
            elif number == "92":
                for k, v in words[1:]:
                    if k in "XYZE":
                        setattr(self, k.lower(), float(v))
            elif number == "28":
                axes = {k for k, _ in words[1:]} & set("XYZ") or set("XYZ")
                for k in axes:
                    setattr(self, k.lower(), 0.0)
                self.open = False
        elif letter == "M":
            if number == "82":
                self.relative_e = False
            elif number == "83":
                self.relative_e = True
        elif letter == "T" and number.isdigit():
            tool = int(number)
            if tool < 64 and tool != self.tool:
                self.tool = tool
                self.open = False

    def _comment(self, line: str) -> None:
        if line.startswith(";TYPE:"):
            kind = self._type_index(line[6:].strip())
            if kind != self.type:
                self.type = kind
                self.open = False
        elif line.startswith("; ") and "=" in line:
            key, _, value = line[2:].partition("=")
            self.config.setdefault(key.strip(), value.strip())

    def _target(self, words: list[tuple[str, str]]) -> tuple[float, float, float, float]:
        x, y, z, de = self.x, self.y, self.z, 0.0
        for k, v in words[1:]:
            if k == "X":
                x = x + float(v) if self.relative else float(v)
            elif k == "Y":
                y = y + float(v) if self.relative else float(v)
            elif k == "Z":
                z = z + float(v) if self.relative else float(v)
            elif k == "E":
                de = float(v) if self.relative_e else float(v) - self.e
        return x, y, z, de

    def _move(self, words: list[tuple[str, str]]) -> None:
        x, y, z, de = self._target(words)
        moved = x != self.x or y != self.y or z != self.z
        if moved and de > 1e-6:
            self._extrude_to(x, y, z)
        elif moved:  # travel; feed-rate-only lines and retractions keep the path open
            self.open = False
        self.x, self.y, self.z = x, y, z
        self.e += de

    def _arc(self, words: list[tuple[str, str]], clockwise: bool) -> None:
        x, y, z, de = self._target(words)
        params = dict(words[1:])
        if "I" not in params and "J" not in params:  # R-form arcs are drawn straight
            self._move(words)
            return
        cx = self.x + float(params.get("I", 0))
        cy = self.y + float(params.get("J", 0))
        r = math.hypot(self.x - cx, self.y - cy)
        a0 = math.atan2(self.y - cy, self.x - cx)
        sweep = math.atan2(y - cy, x - cx) - a0
        if clockwise and sweep >= 0:
            sweep -= 2 * math.pi
        elif not clockwise and sweep <= 0:
            sweep += 2 * math.pi
        if de <= 1e-6:
            self.open = False
        else:
            step = 2 * math.acos(max(-1.0, 1 - ARC_TOLERANCE / r)) if r > ARC_TOLERANCE else math.pi
            n = max(1, min(MAX_ARC_STEPS, math.ceil(abs(sweep) / step)))
            z0 = self.z
            for i in range(1, n):
                t = i / n
                a = a0 + sweep * t
                self._extrude_to(cx + r * math.cos(a), cy + r * math.sin(a), z0 + (z - z0) * t)
                self.x, self.y, self.z = self.paths.points[-3:]
            self._extrude_to(x, y, z)
        self.x, self.y, self.z = x, y, z
        self.e += de

    def _extrude_to(self, x: float, y: float, z: float) -> None:
        p = self.paths
        if self.open:
            pts = p.points
            # Fold a too-short last segment into this one instead of keeping both.
            if len(pts) - 3 > p.starts[-1] * 3:
                px, py, pz = pts[-6], pts[-5], pts[-4]
                lx, ly, lz = pts[-3], pts[-2], pts[-1]
                if abs(lx - px) + abs(ly - py) + abs(lz - pz) < MIN_SEGMENT:
                    pts[-3], pts[-2], pts[-1] = x, y, z
                    return
            pts.extend((x, y, z))
            return
        p.starts.append(len(p.points) // 3)
        p.types.append(self.type)
        p.tools.append(self.tool)
        p.points.extend((self.x, self.y, self.z, x, y, z))
        self.open = True


def parse_lines(lines: Iterable[str], meta: dict[str, str] | None = None) -> Toolpaths:
    parser = _Parser()
    for line in lines:
        parser.feed(line.rstrip("\r"))
    paths = parser.paths
    paths.meta = {**parser.config, **(meta or {})}
    return paths


def parse_file(data: bytes) -> Toolpaths:
    """Toolpaths of a plain-text or binary G-code file."""
    if data[:4] == BGCODE_MAGIC:
        chunks, meta = read_bgcode(data)
        return parse_lines(iter_lines(chunks), meta)
    if b"\nG1 " not in data and b"\nG0 " not in data:
        raise GCodeError("No G-code moves found")
    return parse_lines(iter_lines(_text_chunks(data)))


def _text_chunks(data: bytes, size: int = 1 << 20) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    for start in range(0, len(data), size):
        yield decoder.decode(data[start : start + size])
    yield decoder.decode(b"", final=True)


# --- serialisation --------------------------------------------------------------------


def _tool_colors(meta: dict[str, str], count: int) -> list[str]:
    """As in PrusaSlicer's preview: the extruder's colour if it has one, else the filament's."""
    filament = meta.get("filament_colour", "").split(";")
    extruder = meta.get("extruder_colour", "").split(";")
    colors = []
    for i in range(count):
        for source in (extruder, filament):
            if i < len(source) and _HEX_COLOR.match(source[i].strip()):
                colors.append(source[i].strip().upper())
                break
        else:
            colors.append(DEFAULT_COLORS[i % len(DEFAULT_COLORS)])
    return colors


def _bed(meta: dict[str, str]) -> list[list[float]] | None:
    try:
        return [[float(c) for c in p.split("x")] for p in meta["bed_shape"].split(",")]
    except (KeyError, ValueError):
        return None


def serialize(paths: Toolpaths, name: str = "", uploaded: bool = False) -> bytes:
    """The model as sent to the card: a JSON header followed by typed arrays.

    PGV1 | u32 header length | header JSON (space-padded to 4 bytes)
    | u16 points (x, y, z quantised to `scale` mm from `origin`, padded to 4 bytes)
    | u32 polyline starts (+ end sentinel) | u8 types | u8 tools
    Polylines are sorted by height so the card can split printed and unprinted with a
    draw range; `layers` lists each height and its first polyline.
    """
    n = paths.polyline_count
    pts = paths.points
    starts = list(paths.starts) + [len(pts) // 3]
    if n:
        xs, ys, zs = pts[0::3], pts[1::3], pts[2::3]
        lo = [min(xs), min(ys), min(zs)]
        hi = [max(xs), max(ys), max(zs)]
    else:
        lo = hi = [0.0, 0.0, 0.0]
    scale = max(0.01, max(h - l for h, l in zip(hi, lo)) / 65000)

    def layer_z(i: int) -> float:
        return round(min(pts[starts[i] * 3 + 2], pts[(starts[i + 1] - 1) * 3 + 2]), 3)

    order = sorted(range(n), key=layer_z)
    out_pts = array("H")
    out_starts = array("I")
    types, tools = bytearray(), bytearray()
    layers: list[dict[str, float | int]] = []
    ox, oy, oz = lo
    inv = 1 / scale
    for rank, i in enumerate(order):
        z = layer_z(i)
        if not layers or layers[-1]["z"] != z:
            layers.append({"z": z, "start": rank})
        out_starts.append(len(out_pts) // 3)
        types.append(paths.types[i])
        tools.append(paths.tools[i])
        for j in range(starts[i] * 3, starts[i + 1] * 3, 3):
            out_pts.append(round((pts[j] - ox) * inv))
            out_pts.append(round((pts[j + 1] - oy) * inv))
            out_pts.append(round((pts[j + 2] - oz) * inv))
    out_starts.append(len(out_pts) // 3)

    meta = paths.meta
    header = {
        "version": MODEL_VERSION,
        "name": name,
        "points": len(out_pts) // 3,
        "polylines": n,
        "origin": lo,
        "scale": scale,
        "bounds": [lo, hi],
        "layers": layers,
        "types": paths.type_names,
        "tools": _tool_colors(meta, max(paths.tools, default=0) + 1),
        "bed": _bed(meta),
        "estimated_time": meta.get("estimated printing time (normal mode)"),
        "layer_height": meta.get("layer_height"),
        "uploaded": uploaded,
        "outline": paths.outline,
    }
    blob = json.dumps(header, separators=(",", ":")).encode()
    blob += b" " * (-len(blob) % 4)
    point_bytes = out_pts.tobytes()
    point_bytes += b"\0" * (-len(point_bytes) % 4)
    return b"".join(
        (
            MODEL_MAGIC,
            struct.pack("<I", len(blob)),
            blob,
            point_bytes,
            out_starts.tobytes(),
            bytes(types),
            bytes(tools),
        )
    )


def build_model(data: bytes, name: str = "") -> bytes:
    """Parse a G-code file and serialise its toolpaths in one go (run in an executor)."""
    return serialize(parse_file(data), name)
