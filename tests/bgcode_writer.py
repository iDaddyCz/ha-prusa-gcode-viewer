"""Minimal binary G-code writer for tests (slow; for small inputs only).

The decoder is also checked against libbgcode's own sample files during development;
this writer lets the tests cover the same paths without shipping those files.
"""

from __future__ import annotations

import struct
import zlib

PACKABLE = "0123456789. \nGX"


def heatshrink_encode(src: bytes, window_bits: int, lookahead_bits: int) -> bytes:
    window, max_len = 1 << window_bits, 1 << lookahead_bits
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> (width - 1 - i)) & 1 for i in range(width))

    i = 0
    while i < len(src):
        best_len = best_off = 0
        lo = max(0, i - window)
        for length in range(2, min(max_len, len(src) - i) + 1):
            # Matches may overlap the bytes being encoded (offset < length).
            pos = src.rfind(src[i : i + length], lo, i + length - 1)
            if pos < 0 or pos >= i:
                break
            best_len, best_off = length, i - pos
        if best_len * 9 > 1 + window_bits + lookahead_bits:
            put(0, 1)
            put(best_off - 1, window_bits)
            put(best_len - 1, lookahead_bits)
            i += best_len
        else:
            put(1, 1)
            put(src[i], 8)
            i += 1
    bits += [0] * (-len(bits) % 8)
    return bytes(int("".join(map(str, bits[k : k + 8])), 2) for k in range(0, len(bits), 8))


def meatpack_encode(text: str, nospace: bool = True) -> bytes:
    """Pack two characters per byte where possible, like PrusaSlicer's MeatPack encoder.

    Like the real encoder this works on whole lines: the text must end with a newline.
    """
    assert text.endswith("\n")
    table = PACKABLE.replace(" ", "E") if nospace else PACKABLE
    code = {c: i for i, c in enumerate(table)}
    out = bytearray(b"\xff\xff\xfb")
    if nospace:
        out += b"\xff\xff\xf7"
    i = 0
    while i < len(text):
        a = text[i]
        if a == "\n":
            out.append(code["\n"])
            i += 1
            continue
        b = text[i + 1] if i + 1 < len(text) else "\n"
        ca, cb = code.get(a), code.get(b)
        if b == "\n":
            cb = None if ca is None else code["\n"]
        byte = (0xF if ca is None else ca) | ((0xF if cb is None else cb) << 4)
        out.append(byte)
        if ca is None:
            out += a.encode()
        if cb is None:
            out += b.encode()
        i += 2
    return bytes(out)


def block(block_type: int, payload: bytes, compression: int, uncompressed: int, params: bytes) -> bytes:
    if compression:
        header = struct.pack("<HHII", block_type, compression, uncompressed, len(payload))
    else:
        header = struct.pack("<HHI", block_type, compression, uncompressed)
    body = header + params + payload
    return body + struct.pack("<I", zlib.crc32(body))


def write_bgcode(gcode: str, meta: dict[str, str], chunk: int = 8000) -> bytes:
    out = bytearray(b"GCDE" + struct.pack("<IH", 1, 1))
    ini = "".join(f"{k}={v}\n" for k, v in meta.items()).encode()
    out += block(0, b"Producer=tests\n", 0, 15, b"\0\0")
    out += block(3, ini, 0, len(ini), b"\0\0")
    out += block(5, b"\x89PNG fake", 0, 9, struct.pack("<HHH", 0, 16, 16))
    deflated = zlib.compress(ini)
    out += block(2, deflated, 1, len(ini), b"\0\0")
    lines = gcode.splitlines(keepends=True)
    blocks, current = [], ""
    for line in lines:  # blocks hold whole lines, as libbgcode writes them
        if current and len(current) + len(line) > chunk:
            blocks.append(current)
            current = ""
        current += line
    blocks.append(current)
    for text in blocks:
        packed = meatpack_encode(text)
        out += block(1, heatshrink_encode(packed, 12, 4), 3, len(packed), b"\2\0")
    return bytes(out)
