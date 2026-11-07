"""3MF files: embedded G-code, mesh outlines, tools and placement."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from custom_components.prusa_gcode_viewer import gcode, threemf

# A 10 mm cube, standing on the origin.
VERTICES = [(x, y, z) for z in (0, 10) for y in (0, 10) for x in (0, 10)]
TRIANGLES = [
    (0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6),  # bottom, top
    (0, 1, 4), (1, 5, 4), (2, 6, 3), (3, 6, 7),  # front, back
    (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5),  # left, right
]  # fmt: skip


def mesh(paint: dict[int, str] | None = None) -> str:
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in VERTICES)
    triangles = ""
    for i, (a, b, c) in enumerate(TRIANGLES):
        code = f' slic3rpe:mmu_segmentation="{paint[i]}"' if paint and i in paint else ""
        triangles += f'<triangle v1="{a}" v2="{b}" v3="{c}"{code}/>'
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def archive(files: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return out.getvalue()


def model(resources: str, build: str) -> str:
    return (
        '<?xml version="1.0"?><model unit="millimeter" xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">'
        f"<resources>{resources}</resources><build>{build}</build></model>"
    )


def cube_3mf(transform: str = "1 0 0 0 1 0 0 0 1 100 50 0", paint: dict[int, str] | None = None) -> bytes:
    return archive(
        {
            threemf.ROOT_MODEL: model(
                f'<object id="1">{mesh(paint)}</object>', f'<item objectid="1" transform="{transform}"/>'
            ),
        }
    )


def polylines(paths: gcode.Toolpaths) -> list[list[tuple[float, float, float]]]:
    starts = [*paths.starts, len(paths.points) // 3]
    return [
        [tuple(round(v, 3) for v in paths.points[j * 3 : j * 3 + 3]) for j in range(a, b)]
        for a, b in zip(starts, starts[1:])
    ]


def test_cube_outlines() -> None:
    paths = threemf.parse(cube_3mf())
    assert paths.outline is True
    assert paths.type_names == ["External perimeter"]
    lines = polylines(paths)
    # Default 0.2 mm layers: nozzle heights 0.2 ... 10, with the flat bottom and top uncut.
    heights = sorted({p[2] for line in lines for p in line})
    assert len(heights) == 50
    assert heights[0] == 0.2
    assert heights[-1] == 10
    # Four sides of two triangles each: every layer's outline is at most 8 pieces,
    # and stays on the shell of the cube, moved by its transform.
    assert len(lines) <= 50 * 8
    for x, y, _ in (p for line in lines for p in line):
        assert 100 <= x <= 110
        assert 50 <= y <= 60
        assert x in (100, 110) or y in (50, 60)
    layer = [line for line in lines if line[0][2] == 5]
    length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in layer)
    assert length == pytest.approx(40)


def test_rotated_and_nested_transforms() -> None:
    data = archive(
        {
            threemf.ROOT_MODEL: model(
                f'<object id="1">{mesh()}</object>'
                '<object id="2"><components><component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 5"/></components></object>',
                # A quarter turn about Z, then moved: x' = -y + 20, y' = x + 30.
                '<item objectid="2" transform="0 1 0 -1 0 0 0 0 1 20 30 0"/>',
            ),
        }
    )
    points = [p for line in polylines(threemf.parse(data)) for p in line]
    assert min(p[0] for p in points) == pytest.approx(10)
    assert max(p[0] for p in points) == pytest.approx(20)
    assert min(p[1] for p in points) == pytest.approx(30)
    assert max(p[1] for p in points) == pytest.approx(40)
    assert min(p[2] for p in points) > 5


def test_component_in_another_model_file() -> None:
    data = archive(
        {
            threemf.ROOT_MODEL: model(
                '<object id="9"><components><component p:path="/3D/Objects/part.model" objectid="1"/></components></object>',
                '<item objectid="9"/>',
            ),
            "3D/Objects/part.model": model(f'<object id="1">{mesh()}</object>', ""),
        }
    )
    assert threemf.parse(data).polyline_count > 0


def project_json(volumes: list[dict], **settings) -> str:
    return json.dumps(
        {
            "objects": [{"id": 3, "object_settings": {"extruder": 2}, "volumes": volumes}],
            "config_containers": [
                {
                    "configuration": {
                        "filament_settings": {"filament_colour": ["", "#00FF00"]},
                        "project_settings": {"extruder_colour": ["#FF0000", ""]},
                        "print_settings": {
                            "layer_height": 0.5,
                            "first_layer_height": {"is_percent": False, "value": 1.0},
                        },
                        "printer_settings": {"bed_shape": [[0.0, 0.0], [248.0, 0.0], [248.0, 205.0], [0.0, 205.0]]},
                    },
                    "virtual_extruders": [{"id": 3, "kind": "fullspectrum", "color": "#123456", "components": []}],
                    **settings,
                }
            ],
        }
    )


def prusaslicer3(paint: dict[int, str] | None = None) -> bytes:
    resources = (
        f'<object id="1">{mesh(paint)}</object><object id="2">{mesh()}</object>'
        '<object id="3"><components><component objectid="1"/><component objectid="2"/></components></object>'
    )
    return archive(
        {
            threemf.ROOT_MODEL: model(resources, '<item objectid="3"/>'),
            threemf.PROJECT_JSON: project_json(
                [
                    {"id": 1, "type": "ModelPart", "volume_settings": {}},
                    {"id": 2, "type": "ParameterModifier", "volume_settings": {"extruder": 1}},
                ]
            ),
        }
    )


def test_prusaslicer_3_project() -> None:
    paths = threemf.parse(prusaslicer3())
    # The modifier isn't printed; the part inherits the object's extruder 2.
    assert set(paths.tools) == {1}
    lines = polylines(paths)
    heights = sorted({line[0][2] for line in lines})
    assert heights[:3] == [1, 1.5, 2]
    assert heights[-1] == 10
    header = read_header(gcode.serialize(paths))
    assert header["bed"] == [[0, 0], [248, 0], [248, 205], [0, 205]]
    assert header["tools"] == ["#FF0000", "#00FF00"]


def test_painted_triangles_use_their_tool() -> None:
    # Front side: 8 = state 2, 3C = state 6. Back: the mixed extruder 3. Left: 0 = unpainted.
    paths = threemf.parse(prusaslicer3({4: "8", 5: "3C", 6: "0C", 7: "0C", 8: "0"}))
    assert {1, 5, 2} == set(paths.tools)
    header = read_header(gcode.serialize(paths))
    assert header["tools"][2] == "#123456"


@pytest.mark.parametrize(
    ("code", "state"),
    [
        ("0", 0),
        ("4", 1),
        ("8", 2),
        ("0C", 3),
        ("3C", 6),
        ("7C", 10),
        ("884", 2),  # read backwards: a leaf of state 1, then two of state 2
        ("ZZ", 0),
    ],
)
def test_paint_state(code: str, state: int) -> None:
    assert threemf._paint_state(code.encode()) == state


def test_prusaslicer_2_project() -> None:
    config = (
        '<config><object id="1"><metadata type="object" key="extruder" value="2"/>'
        '<volume firstid="0" lastid="5"><metadata type="volume" key="volume_type" value="ModelPart"/>'
        '<metadata type="volume" key="extruder" value="3"/></volume>'
        '<volume firstid="6" lastid="11"><metadata type="volume" key="volume_type" value="ParameterModifier"/></volume>'
        "</object></config>"
    )
    data = archive(
        {
            threemf.ROOT_MODEL: model(f'<object id="1">{mesh()}</object>', '<item objectid="1"/>'),
            threemf.MODEL_CONFIG: config,
            threemf.PROJECT_CONFIG: "; layer_height = 1\n; first_layer_height = 50%\n; extruder_colour = #000001;#000002;#000003\n",
        }
    )
    paths = threemf.parse(data)
    assert set(paths.tools) == {2}
    heights = sorted({line[0][2] for line in polylines(paths)})
    assert heights[:2] == [0.5, 1.5]
    assert read_header(gcode.serialize(paths))["tools"][2] == "#000003"


def test_embedded_gcode_wins() -> None:
    text = b"G1 Z0.2\nG1 X10 Y10\nG1 X20 Y10 E1\n"
    paths = threemf.parse(archive({threemf.ROOT_MODEL: model("", ""), "Metadata/plate_1.gcode": text}))
    assert paths.outline is False
    assert polylines(paths) == [[(10, 10, 0.2), (20, 10, 0.2)]]


def test_too_many_segments_coarsen_the_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    full = threemf.parse(cube_3mf()).polyline_count
    monkeypatch.setattr(threemf, "MAX_SEGMENTS", full // 4)
    assert threemf.parse(cube_3mf()).polyline_count < full / 2


FLAT = (
    '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
    '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"PK\x03\x04 not really a zip", "Not a valid 3MF"),
        (archive({"other.txt": "hello"}), "has no 3D/3dmodel.model"),
        (archive({threemf.ROOT_MODEL: model("", "")}), "no printable objects"),
        (archive({threemf.ROOT_MODEL: model('<object id="1"/>', '<item objectid="1"/>')}), "no printable objects"),
        (
            # A flat sheet, lying in the bottom of the first layer's cut.
            archive({threemf.ROOT_MODEL: model(FLAT, '<item objectid="1"/>')}),
            "nothing to print",
        ),
        (
            archive({threemf.ROOT_MODEL: model('<object id="1"><vertex x="a" y="0" z="0"/></object>', "")}),
            "Malformed",
        ),
    ],
)
def test_unusable_files(data: bytes, message: str) -> None:
    with pytest.raises(gcode.GCodeError, match=message):
        threemf.parse(data)


def test_a_project_format_we_do_not_know_still_shows_the_mesh() -> None:
    data = archive(
        {
            threemf.ROOT_MODEL: model(f'<object id="1">{mesh()}</object>', '<item objectid="1"/>'),
            threemf.PROJECT_JSON: '{"objects": [{"volumes": [{"no_id": true}]}]}',
        }
    )
    assert threemf.parse(data).polyline_count > 0


def read_header(blob: bytes) -> dict:
    length = int.from_bytes(blob[4:8], "little")
    return json.loads(blob[8 : 8 + length])
