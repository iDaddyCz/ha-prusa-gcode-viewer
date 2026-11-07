"""Status command, building and caching, and the model endpoint."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
import struct
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.prusa_gcode_viewer import gcode
from custom_components.prusa_gcode_viewer.const import CACHE_KEEP, DOMAIN, UPLOAD_URL
from custom_components.prusa_gcode_viewer.viewer import build_upload
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from .conftest import HOST
from .test_threemf import cube_3mf

STAR = gzip.decompress((Path(__file__).parent / "fixtures" / "star.gcode.gz").read_bytes())
JOB = {
    "id": 7,
    "state": "PRINTING",
    "progress": 4.0,
    "file": {
        "name": "STAR~1.GCO",
        "display_name": "star_0.2mm_PLA_COREONE_INDX8T_10m.gcode",
        "path": "/usb",
        "size": len(STAR),
        "m_timestamp": 1761990000,
        "refs": {"download": "/usb/STAR~1.GCO", "thumbnail": "/thumb/l/usb/STAR~1.GCO"},
    },
}


def _header_bytes(model: bytes) -> bytes:
    (length,) = struct.unpack_from("<I", model, 4)
    return model[8 : 8 + length]


async def status(hass_ws_client: WebSocketGenerator, **extra) -> dict:
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/status", **extra})
    msg = await ws.receive_json()
    assert msg["success"], msg
    return msg["result"]


async def test_card_is_registered(
    hass: HomeAssistant, viewer: MockConfigEntry, extra_js: MagicMock, hass_client: ClientSessionGenerator
) -> None:
    version = json.loads((Path(gcode.__file__).parent / "manifest.json").read_text())["version"]
    extra_js.assert_called_once_with(hass, f"/prusa_gcode_viewer/prusa-gcode-viewer-card.js?v={version}")
    client = await hass_client()
    response = await client.get("/prusa_gcode_viewer/prusa-gcode-viewer-card.js")
    assert response.status == 200
    assert "prusa-gcode-viewer-card" in await response.text()


@respx.mock
async def test_builds_once_then_serves_from_cache(
    hass: HomeAssistant,
    viewer: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB)
    download = respx.get(f"{HOST}/usb/STAR~1.GCO").respond(content=STAR)

    first = await status(hass_ws_client)
    assert first["state"] == "building"
    assert first["name"] == "star_0.2mm_PLA_COREONE_INDX8T_10m.gcode"
    assert first["size"] == len(STAR)
    assert first["entities"] == {
        "state": "sensor.core_one_state",
        "z": "sensor.core_one_z",
        "progress": "sensor.core_one_progress",
        "file": "sensor.core_one_file",
    }
    await hass.async_block_till_done(wait_background_tasks=True)

    ready = await status(hass_ws_client)
    assert ready["state"] == "ready"
    assert ready["live"] is True
    assert ready["url"] == f"/api/prusa_gcode_viewer/model/{viewer.entry_id}/{ready['key']}?v={ready['revision']}"
    # Seeing the same print again doesn't download it again.
    await status(hass_ws_client)
    assert download.call_count == 1

    client = await hass_client()
    response = await client.get(ready["url"])
    assert response.status == 200
    assert response.headers["Cache-Control"].endswith("immutable")
    body = await response.read()  # aiohttp undoes the gzip Content-Encoding
    assert body == gcode.build_model(STAR, JOB["file"]["display_name"])


@respx.mock
async def test_after_the_print_the_last_model_stays(
    hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    job = respx.get(f"{HOST}/api/v1/job")
    job.respond(status_code=204)
    assert (await status(hass_ws_client))["state"] == "idle"

    job.respond(json=JOB)
    respx.get(f"{HOST}/usb/STAR~1.GCO").respond(content=STAR)
    await status(hass_ws_client)
    await hass.async_block_till_done(wait_background_tasks=True)
    key = (await status(hass_ws_client))["key"]

    job.respond(status_code=204)
    after = await status(hass_ws_client)
    assert after == after | {"state": "ready", "key": key, "live": False, "name": JOB["file"]["display_name"]}

    job.side_effect = httpx.ConnectError("no route to host")
    offline = await status(hass_ws_client)
    assert offline["state"] == "ready"
    assert offline["key"] == key
    assert "no route to host" in offline["message"]


@respx.mock
async def test_unreadable_file_reports_an_error(
    hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB)
    download = respx.get(f"{HOST}/usb/STAR~1.GCO").respond(content=b"\x89PNG not g-code")
    await status(hass_ws_client)
    await hass.async_block_till_done(wait_background_tasks=True)
    failed = await status(hass_ws_client)
    assert failed["state"] == "error"
    assert failed["message"] == "No G-code moves found"
    # Not retried straight away.
    await status(hass_ws_client)
    assert download.call_count == 1


@respx.mock
async def test_file_still_in_its_transfer_directory(
    hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Firmware 8 lists a Prusa Connect upload with size 0; its data is in the "p" file."""
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB | {"file": JOB["file"] | {"size": 0}})
    respx.get(f"{HOST}/usb/STAR~1.GCO").respond(status_code=404)
    respx.get(f"{HOST}/usb/STAR~1.GCO/p").respond(content=STAR)
    first = await status(hass_ws_client)
    assert first["size"] in (None, len(STAR))  # known once the download's Content-Length arrives
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await status(hass_ws_client))["state"] == "ready"


@respx.mock
async def test_printer_refuses_the_download(
    hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB)
    respx.get(f"{HOST}/usb/STAR~1.GCO").respond(status_code=401)
    await status(hass_ws_client)
    await hass.async_block_till_done(wait_background_tasks=True)
    failed = await status(hass_ws_client)
    assert failed["state"] == "error"
    assert "401" in failed["message"]


@respx.mock
async def test_file_missing(hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB)
    respx.get(url__startswith=f"{HOST}/usb/").respond(status_code=404)
    await status(hass_ws_client)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await status(hass_ws_client))["message"] == "Printer answered 404 for the file"


@respx.mock
async def test_a_new_job_of_the_same_file_is_a_new_print(
    hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    job = respx.get(f"{HOST}/api/v1/job")
    job.respond(json=JOB)
    respx.get(f"{HOST}/usb/STAR~1.GCO").respond(content=STAR)
    await status(hass_ws_client)
    await hass.async_block_till_done(wait_background_tasks=True)
    first = await status(hass_ws_client)
    # The printer finishing the transfer changes size and timestamp: same print.
    job.respond(json=JOB | {"file": JOB["file"] | {"size": 1, "m_timestamp": 1}})
    assert (await status(hass_ws_client))["key"] == first["key"]
    job.respond(json=JOB | {"id": 8})
    assert (await status(hass_ws_client))["state"] == "building"


async def test_old_models_are_deleted(hass: HomeAssistant, viewer: MockConfigEntry) -> None:
    from custom_components.prusa_gcode_viewer.viewer import Job

    cache = viewer.runtime_data
    for i in range(CACHE_KEEP + 2):
        job = Job(key=f"{i:016x}", name=f"print {i}", download="/x", size=1)
        await hass.async_add_executor_job(cache._store, job, b"model")
        os.utime(cache.path(job.key), (1000 + i, 1000 + i))
    kept = sorted(p.name for p in cache.directory.glob("*.pgv.gz"))
    assert kept == [f"{i:016x}.pgv.gz" for i in range(2, CACHE_KEEP + 2)]
    assert await hass.async_add_executor_job(cache._newest_cached) == (f"{CACHE_KEEP + 1:016x}", f"print {CACHE_KEEP + 1}")


async def upload(client, body: bytes, name: str = "star.gcode", **query) -> None:
    response = await client.post(UPLOAD_URL, params={"name": name, **query}, data=body)
    assert response.status == 202, await response.text()


@respx.mock
async def test_dropped_file_replaces_the_download(
    hass: HomeAssistant,
    viewer: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB)
    stalled = asyncio.Event()

    async def never_finishes(build):
        stalled.set()
        await asyncio.Event().wait()

    cache = viewer.runtime_data
    with patch.object(cache, "_async_download", never_finishes):
        assert (await status(hass_ws_client))["state"] == "building"
        await stalled.wait()
        task = next(iter(cache._builds.values())).task

        client = await hass_client()
        release = threading.Event()

        def slow_build(data: bytes, name: str) -> tuple[bytes, str]:
            release.wait(10)
            return build_upload(data, name)

        with patch("custom_components.prusa_gcode_viewer.viewer.build_upload", slow_build):
            await upload(client, STAR, "dropped.gcode")
            building = await status(hass_ws_client)
            assert building["upload"] == {"name": "dropped.gcode", "error": None}
            assert not task.done()  # the download goes on until the file is known to be good
            release.set()
            await hass.async_block_till_done(wait_background_tasks=True)

    assert task.cancelled()
    ready = await status(hass_ws_client)
    assert ready["state"] == "ready"
    assert ready["live"] is True
    assert "upload" not in ready
    response = await client.get(ready["url"])
    header = json.loads(_header_bytes(await response.read()))
    assert header["name"] == "dropped.gcode"
    assert header["uploaded"] is True
    assert header["outline"] is False
    # Still the same print, so a second file takes its place under a new URL.
    await upload(client, STAR, "second.gcode")
    await hass.async_block_till_done(wait_background_tasks=True)
    again = await status(hass_ws_client)
    assert again["key"] == ready["key"]
    assert again["url"] != ready["url"]


@respx.mock
async def test_dropped_file_while_nothing_prints(
    hass: HomeAssistant,
    viewer: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(status_code=204)
    assert (await status(hass_ws_client))["state"] == "idle"
    client = await hass_client()
    # Larger than the 16 MB Home Assistant allows for bodies read in one go.
    await upload(client, STAR + b"; padding\n" * 2_000_000, "large.gcode")
    await hass.async_block_till_done(wait_background_tasks=True)
    ready = await status(hass_ws_client)
    assert ready == ready | {"state": "ready", "live": False, "name": "large.gcode"}


@respx.mock
async def test_dropped_3mf_is_outlined(
    hass: HomeAssistant,
    viewer: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(status_code=204)
    client = await hass_client()
    await upload(client, cube_3mf(), "cube.3mf")
    await hass.async_block_till_done(wait_background_tasks=True)
    ready = await status(hass_ws_client)
    response = await client.get(ready["url"])
    header = json.loads(_header_bytes(await response.read()))
    assert header["outline"] is True
    assert header["uploaded"] is True


@respx.mock
async def test_unreadable_dropped_file_keeps_the_current_model(
    hass: HomeAssistant, viewer: MockConfigEntry, hass_ws_client: WebSocketGenerator, hass_client: ClientSessionGenerator
) -> None:
    respx.get(f"{HOST}/api/v1/job").respond(json=JOB)
    download = respx.get(f"{HOST}/usb/STAR~1.GCO").respond(content=STAR)
    await status(hass_ws_client)
    await hass.async_block_till_done(wait_background_tasks=True)
    before = await status(hass_ws_client)

    await upload(await hass_client(), b"\x89PNG not a print", "picture.gcode")
    await hass.async_block_till_done(wait_background_tasks=True)
    after = await status(hass_ws_client)
    assert after["upload"] == {"name": "picture.gcode", "error": "No G-code moves found"}
    assert after["url"] == before["url"]
    assert download.call_count == 1


async def test_upload_refusals(viewer: MockConfigEntry, hass_client: ClientSessionGenerator) -> None:
    client = await hass_client()
    assert (await client.post(UPLOAD_URL, params={"name": "x.gcode"}, data=b"")).status == 400
    assert (await client.post(UPLOAD_URL, params={"entry_id": "nope"}, data=b"G1")).status == 404
    with patch("custom_components.prusa_gcode_viewer.MAX_FILE_SIZE", 4):
        assert (await client.post(UPLOAD_URL, data=b"too much")).status == 413


async def test_upload_needs_auth(viewer: MockConfigEntry, hass_client_no_auth: ClientSessionGenerator) -> None:
    client = await hass_client_no_auth()
    assert (await client.post(UPLOAD_URL, data=b"G1")).status == 401


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/model/{entry}/0123456789abcdef", 404),
        ("/model/{entry}/..%2F..%2Fsecrets", 400),  # refused by Home Assistant's own filter
        ("/model/{entry}/..secrets", 404),
        ("/model/nope/0123456789abcdef", 404),
    ],
)
async def test_model_view_not_found(
    viewer: MockConfigEntry, hass_client: ClientSessionGenerator, path: str, code: int
) -> None:
    client = await hass_client()
    response = await client.get("/api/prusa_gcode_viewer" + path.format(entry=viewer.entry_id))
    assert response.status == code


async def test_model_view_needs_auth(viewer: MockConfigEntry, hass_client_no_auth: ClientSessionGenerator) -> None:
    client = await hass_client_no_auth()
    response = await client.get(f"/api/prusa_gcode_viewer/model/{viewer.entry_id}/0123456789abcdef")
    assert response.status == 401


async def test_status_without_a_viewer(hass: HomeAssistant, deps: None, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/status"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["message"] == "Prusa G-code Viewer is not set up"
