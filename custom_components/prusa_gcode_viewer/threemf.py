"""Turns a 3MF file into toolpaths: its embedded G-code, or else outlines of its meshes.

Slicer project files hold meshes and settings, not toolpaths, so a mesh is cut at each
layer height into outlines. That shows the shape, colours and layers of the print, but
no infill, supports, brim or wipe tower.

Pure Python with no Home Assistant imports, like gcode.py. The format is described at
https://3mf.io/specification; project settings are PrusaSlicer's own files.
"""

from __future__ import annotations

from array import array
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
import io
import json
import math
import re
from typing import Any
import zipfile

from .gcode import GCodeError, Toolpaths, parse_file

ZIP_MAGIC = b"PK\x03\x04"
ROOT_MODEL = "3D/3dmodel.model"
PROJECT_JSON = "Metadata/PrusaSlicer3_project.json"  # PrusaSlicer 3
PROJECT_CONFIG = "Metadata/Slic3r_PE.config"  # PrusaSlicer 2
MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"
MODEL_PART = "ModelPart"  # other volume types (modifiers, support enforcers...) aren't printed

# Largest file read out of the archive, against zip bombs.
MAX_ENTRY_SIZE = 1024 * 1024 * 1024
OUTLINE_TYPE = "External perimeter"
DEFAULT_LAYER_HEIGHT = 0.2
# Above this many outline segments, layers are drawn further apart to keep the model small.
MAX_SEGMENTS = 8_000_000
SKIP = 255  # marks a triangle as not printed

_TAG = re.compile(rb"<(vertex|triangle|component|object|/object|item)\b([^>]*)>")
_ATTR = re.compile(rb'([\w:.-]+)\s*=\s*"([^"]*)"')
_CONFIG_OBJECT = re.compile(r"<object\b([^>]*)>(.*?)</object>", re.S)
_CONFIG_VOLUME = re.compile(r"<volume\b([^>]*)>(.*?)</volume>", re.S)
_CONFIG_META = re.compile(r'<metadata\b[^>]*\bkey="([^"]*)"[^>]*\bvalue="([^"]*)"')

type Matrix = tuple[float, ...]  # 12 values: 3MF's row-major 3x3 followed by the translation


@dataclass
class _Object:
    vertices: array = field(default_factory=lambda: array("f"))
    triangles: array = field(default_factory=lambda: array("I"))
    paint: bytearray = field(default_factory=bytearray)  # one painted state per triangle
    components: list[tuple[str, str, Matrix | None]] = field(default_factory=list)  # file, id, transform
    ranges: list[tuple[int, int, int]] = field(default_factory=list)  # first, last triangle, tool


@dataclass
class _Volumes:
    """Which tool prints what, from the slicer's project files (extruders count from 1)."""

    object_extruder: dict[str, int] = field(default_factory=dict)
    # PrusaSlicer 3: a volume is a component of its object, under the component's object id.
    volumes: dict[str, tuple[str, int]] = field(default_factory=dict)
    # PrusaSlicer 2: a volume is a range of its object's triangles.
    ranges: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)


@lru_cache(maxsize=4096)
def _paint_state(code: bytes) -> int:
    """The most common state among a painted triangle's pieces.

    The code is a hex dump, read backwards, of the tree PrusaSlicer splits a triangle
    into: a nibble with its low bits set is a split, any other is a piece with its state
    in the high bits, where 3 means the next nibble holds the state minus 3. A triangle
    gets one state here, as tracing the splits would cost far more than it shows.
    """
    try:
        nibbles = [int(chr(c), 16) for c in reversed(code)]
    except ValueError:
        nibbles = []
    counts: Counter[int] = Counter()
    i = 0
    while i < len(nibbles):
        nibble = nibbles[i]
        i += 1
        if nibble & 3:
            continue
        piece = nibble >> 2
        if piece == 3 and i < len(nibbles):
            piece += nibbles[i]
            i += 1
        counts[piece] += 1
    return counts.most_common(1)[0][0] if counts else 0


def _matrix(text: bytes | None) -> Matrix | None:
    if not text:
        return None
    values = tuple(float(v) for v in text.split())
    if len(values) != 12:
        raise GCodeError("Malformed transform in the 3MF file")
    return values


def _compose(child: Matrix | None, parent: Matrix) -> Matrix:
    """The transform applying child first, then parent."""
    if child is None:
        return parent
    c, p = child, parent
    return (
        c[0] * p[0] + c[1] * p[3] + c[2] * p[6],
        c[0] * p[1] + c[1] * p[4] + c[2] * p[7],
        c[0] * p[2] + c[1] * p[5] + c[2] * p[8],
        c[3] * p[0] + c[4] * p[3] + c[5] * p[6],
        c[3] * p[1] + c[4] * p[4] + c[5] * p[7],
        c[3] * p[2] + c[4] * p[5] + c[5] * p[8],
        c[6] * p[0] + c[7] * p[3] + c[8] * p[6],
        c[6] * p[1] + c[7] * p[4] + c[8] * p[7],
        c[6] * p[2] + c[7] * p[5] + c[8] * p[8],
        c[9] * p[0] + c[10] * p[3] + c[11] * p[6] + p[9],
        c[9] * p[1] + c[10] * p[4] + c[11] * p[7] + p[10],
        c[9] * p[2] + c[10] * p[5] + c[11] * p[8] + p[11],
    )


IDENTITY: Matrix = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise GCodeError(f"The 3MF file has no {name}") from None
    if info.file_size > MAX_ENTRY_SIZE:
        raise GCodeError("The 3MF file is too large")
    return archive.read(info)


def _parse_model(
    data: bytes, file: str, objects: dict[tuple[str, str], _Object]
) -> Iterator[tuple[str, Matrix | None]]:
    """Fills objects from one model file; yields its build items (object id, transform)."""
    current: _Object | None = None
    pattern = _ATTR.findall
    for match in _TAG.finditer(data):
        tag, body = match.groups()
        if tag == b"vertex":
            if current is not None:
                a = dict(pattern(body))
                current.vertices.extend((float(a[b"x"]), float(a[b"y"]), float(a[b"z"])))
        elif tag == b"triangle":
            if current is not None:
                a = dict(pattern(body))
                current.triangles.extend((int(a[b"v1"]), int(a[b"v2"]), int(a[b"v3"])))
                code = a.get(b"slic3rpe:mmu_segmentation") or a.get(b"paint_color")
                current.paint.append(_paint_state(code) if code else 0)
        elif tag == b"object":
            a = dict(pattern(body))
            current = _Object()
            objects[file, a[b"id"].decode()] = current
            if body.endswith(b"/"):
                current = None
        elif tag == b"/object":
            current = None
        elif tag == b"component":
            if current is not None:
                a = dict(pattern(body))
                path = next((v for k, v in a.items() if k == b"path" or k.endswith(b":path")), None)
                target = path.decode().lstrip("/") if path else file
                current.components.append((target, a[b"objectid"].decode(), _matrix(a.get(b"transform"))))
        else:  # item
            a = dict(pattern(body))
            if a.get(b"printable", b"1") not in (b"0", b"false"):
                yield a[b"objectid"].decode(), _matrix(a.get(b"transform"))


def _read_objects(archive: zipfile.ZipFile) -> tuple[dict[tuple[str, str], _Object], list[tuple[str, Matrix | None]]]:
    """Every object of the root model and the model files its components point to."""
    objects: dict[tuple[str, str], _Object] = {}
    items = list(_parse_model(_read(archive, ROOT_MODEL), ROOT_MODEL, objects))
    seen = {ROOT_MODEL}
    while files := {f for obj in objects.values() for f, _, _ in obj.components} - seen:
        for file in files:
            seen.add(file)
            for _ in _parse_model(_read(archive, file), file, objects):
                pass
    return objects, items


def _height(value: Any, layer_height: float) -> float | None:
    """A length from the settings: a number, "0.2", "50%" or {"value": 0.2, "is_percent": false}."""
    percent = False
    if isinstance(value, dict):
        value, percent = value.get("value"), value.get("is_percent", False)
    elif isinstance(value, str) and value.endswith("%"):
        value, percent = value[:-1], True
    try:
        height = float(value)
    except (TypeError, ValueError):
        return None
    if percent:
        height = layer_height * height / 100
    return height if height > 0 else None


def _read_project(archive: zipfile.ZipFile) -> tuple[dict[str, str], _Volumes]:
    """Settings (under the names G-code files use) and tool assignments of a slicer project."""
    names = set(archive.namelist())
    meta: dict[str, str] = {}
    volumes = _Volumes()
    if PROJECT_JSON in names:
        try:
            _read_project_json(json.loads(_read(archive, PROJECT_JSON)), meta, volumes)
        except (KeyError, TypeError, ValueError, AttributeError):
            pass  # an unknown project format: the meshes can still be shown, in default colours
    if PROJECT_CONFIG in names:
        for line in _read(archive, PROJECT_CONFIG).decode("utf-8", "replace").splitlines():
            key, sep, value = line.removeprefix("; ").partition(" = ")
            if sep:
                meta.setdefault(key.strip(), value.strip())
    if MODEL_CONFIG in names:
        _read_model_config(_read(archive, MODEL_CONFIG).decode("utf-8", "replace"), volumes)
    return meta, volumes


def _read_project_json(project: dict[str, Any], meta: dict[str, str], volumes: _Volumes) -> None:
    for obj in project.get("objects") or []:
        extruder = (obj.get("object_settings") or {}).get("extruder")
        if isinstance(extruder, int):
            volumes.object_extruder[str(obj["id"])] = extruder
        for volume in obj.get("volumes") or []:
            settings = volume.get("volume_settings") or {}
            extruder = settings.get("extruder")
            volumes.volumes[str(volume["id"])] = (volume.get("type", MODEL_PART), extruder if isinstance(extruder, int) else 0)
    containers = project.get("config_containers") or [{}]
    container = containers[0]
    sections = container.get("configuration") or {}
    merged = {k: v for section in sections.values() if isinstance(section, dict) for k, v in section.items()}
    filament = list(merged.get("filament_colour") or [])
    # Mixed filaments are extruders of their own, with their own colour.
    for virtual in container.get("virtual_extruders") or []:
        index = virtual["id"] - 1
        filament += [""] * (index + 1 - len(filament))
        filament[index] = virtual.get("color", "")
    if filament:
        meta["filament_colour"] = ";".join(filament)
    if colours := merged.get("extruder_colour"):
        meta["extruder_colour"] = ";".join(colours)
    if bed := merged.get("bed_shape"):
        meta["bed_shape"] = ",".join(f"{x:g}x{y:g}" for x, y in bed)
    printing = sections.get("print_settings") or {}
    for key in ("layer_height", "first_layer_height"):
        if key in printing:
            value = printing[key]
            percent = isinstance(value, dict) and value.get("is_percent")
            meta[key] = f"{value['value']}%" if percent else str(value["value"] if isinstance(value, dict) else value)


def _read_model_config(text: str, volumes: _Volumes) -> None:
    for attrs, body in _CONFIG_OBJECT.findall(text):
        object_id = re.search(r'\bid="([^"]*)"', attrs)
        if object_id is None:
            continue
        head = body.split("<volume", 1)[0]
        extruder = dict(_CONFIG_META.findall(head)).get("extruder", "")
        object_extruder = int(extruder) if extruder.isdigit() else 0
        volumes.object_extruder[object_id[1]] = object_extruder
        ranges = []
        for volume_attrs, volume_body in _CONFIG_VOLUME.findall(body):
            first, last = re.search(r'firstid="(\d+)"', volume_attrs), re.search(r'lastid="(\d+)"', volume_attrs)
            if first is None or last is None:
                continue
            settings = dict(_CONFIG_META.findall(volume_body))
            extruder = settings.get("extruder", "")
            number = int(extruder) if extruder.isdigit() else 0
            printed = settings.get("volume_type", MODEL_PART) == MODEL_PART
            ranges.append((int(first[1]), int(last[1]), (number or object_extruder or 1) - 1 if printed else SKIP))
        volumes.ranges[object_id[1]] = ranges


def _instances(
    objects: dict[tuple[str, str], _Object], items: list[tuple[str, Matrix | None]], volumes: _Volumes
) -> Iterator[tuple[_Object, Matrix, int]]:
    """Each mesh to print with its place on the bed and its tool."""

    def walk(file: str, object_id: str, matrix: Matrix, tool: int, depth: int) -> Iterator[tuple[_Object, Matrix, int]]:
        obj = objects.get((file, object_id))
        if obj is None or depth > 16:
            return
        if len(obj.triangles):
            if object_id in volumes.ranges:
                obj.ranges = volumes.ranges[object_id]
            yield obj, matrix, tool
        for target, child, transform in obj.components:
            kind, extruder = volumes.volumes.get(child, (MODEL_PART, 0))
            if kind == MODEL_PART:
                yield from walk(target, child, _compose(transform, matrix), extruder - 1 if extruder else tool, depth + 1)

    for object_id, transform in items:
        tool = max(volumes.object_extruder.get(object_id, 0) - 1, 0)
        yield from walk(ROOT_MODEL, object_id, transform or IDENTITY, tool, 0)


class _Planes:
    """The heights to cut at: the middle of each layer, so that outlines fall inside it."""

    def __init__(self, first: float, height: float) -> None:
        self.first, self.height = first, height

    def at_or_above(self, z: float) -> int:
        """The lowest layer cut at or above z."""
        k = math.ceil((z - self.first) / self.height + 0.5)
        return k if k >= 1 else (0 if z <= self.first / 2 else 1)

    def cut(self, k: int) -> float:
        return self.first / 2 if k == 0 else self.first + (k - 0.5) * self.height

    def top(self, k: int) -> float:
        """The nozzle height when printing layer k, which is what the printer reports."""
        return self.first + k * self.height


def _estimate_segments(instances: list[tuple[_Object, Matrix, int]], height: float) -> float:
    """Roughly how many outline segments a layer height gives (from a sample of triangles)."""
    total = 0.0
    for obj, m, _ in instances:
        count = len(obj.triangles) // 3
        step = max(1, count // 20000)
        tris, verts = obj.triangles, obj.vertices
        spans = 0.0
        for t in range(0, count, step):
            zs = [
                verts[i * 3] * m[2] + verts[i * 3 + 1] * m[5] + verts[i * 3 + 2] * m[8]
                for i in tris[t * 3 : t * 3 + 3]
            ]
            spans += max(zs) - min(zs)
        total += spans * step / height
    return total


def _slice(instances: list[tuple[_Object, Matrix, int]], planes: _Planes) -> Toolpaths:
    paths = Toolpaths(type_names=[OUTLINE_TYPE])
    out, starts, types, tools = paths.points, paths.starts, paths.types, paths.tools
    count = 0
    for obj, m, base_tool in instances:
        v = obj.vertices
        m0, m1, m2, m3, m4, m5, m6, m7, m8, m9, m10, m11 = m
        xs = [v[i] * m0 + v[i + 1] * m3 + v[i + 2] * m6 + m9 for i in range(0, len(v), 3)]
        ys = [v[i] * m1 + v[i + 1] * m4 + v[i + 2] * m7 + m10 for i in range(0, len(v), 3)]
        zs = [v[i] * m2 + v[i + 1] * m5 + v[i + 2] * m8 + m11 for i in range(0, len(v), 3)]
        tris = obj.triangles
        n = len(tris) // 3
        chosen = bytearray([base_tool]) * n
        for first, last, tool in obj.ranges:
            if first <= last:
                chosen[first : last + 1] = bytes([tool]) * (min(last, n - 1) - first + 1)
        paint = obj.paint
        for t in range(n):
            tool = chosen[t]
            if tool == SKIP:
                continue
            if paint[t]:
                tool = paint[t] - 1
            a, b, c = tris[t * 3], tris[t * 3 + 1], tris[t * 3 + 2]
            # Lowest, middle and highest corner.
            if zs[a] > zs[b]:
                a, b = b, a
            if zs[b] > zs[c]:
                b, c = c, b
                if zs[a] > zs[b]:
                    a, b = b, a
            zl, zm, zh = zs[a], zs[b], zs[c]
            if zl == zh:
                continue
            k = planes.at_or_above(zl)
            cut = planes.cut(k)
            if cut >= zh:
                continue
            xl, yl, xm, ym, xh, yh = xs[a], ys[a], xs[b], ys[b], xs[c], ys[c]
            while cut < zh:
                # The cut crosses the long edge and one of the short ones.
                r = (cut - zl) / (zh - zl)
                x1, y1 = xl + (xh - xl) * r, yl + (yh - yl) * r
                if cut < zm:
                    r = (cut - zl) / (zm - zl)
                    x2, y2 = xl + (xm - xl) * r, yl + (ym - yl) * r
                else:
                    r = (cut - zm) / (zh - zm)
                    x2, y2 = xm + (xh - xm) * r, ym + (yh - ym) * r
                top = planes.top(k)
                starts.append(count)
                types.append(0)
                tools.append(tool)
                out.extend((x1, y1, top, x2, y2, top))
                count += 2
                k += 1
                cut = planes.cut(k)
    return paths


def parse(data: bytes) -> Toolpaths:
    """Toolpaths of a 3MF file: its sliced G-code if it has any, otherwise mesh outlines."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise GCodeError("Not a valid 3MF file") from None
    with archive:
        # Bambu Studio and OrcaSlicer keep the sliced plates in the 3MF.
        sliced = sorted(n for n in archive.namelist() if n.lower().endswith((".gcode", ".bgcode")))
        if sliced:
            return parse_file(_read(archive, sliced[0]))
        meta, volumes = _read_project(archive)
        try:
            objects, items = _read_objects(archive)
        except (KeyError, ValueError) as err:
            raise GCodeError("Malformed model in the 3MF file") from err
    instances = list(_instances(objects, items, volumes))
    if not instances:
        raise GCodeError("The 3MF file has no printable objects")
    height = _height(meta.get("layer_height"), 1) or DEFAULT_LAYER_HEIGHT
    first = _height(meta.get("first_layer_height"), height) or height
    estimate = _estimate_segments(instances, height)
    if estimate > MAX_SEGMENTS:
        height *= estimate / MAX_SEGMENTS
    paths = _slice(instances, _Planes(first, height))
    if not paths.polyline_count:
        raise GCodeError("The 3MF file has nothing to print")
    paths.meta = meta
    paths.outline = True
    return paths
