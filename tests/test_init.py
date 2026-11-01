"""Status command, building and caching, and the model endpoint."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.prusa_gcode_viewer import gcode
from custom_components.prusa_gcode_viewer.const import CACHE_KEEP, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from .conftest import HOST

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


async def status(hass_ws_client: WebSocketGenerator, **extra) -> dict:
    ws = await hass_ws_client()
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/status", **extra})
    msg = await ws.receive_json()
    assert msg["success"], msg
    return msg["result"]


async def test_card_is_registered(
    hass: HomeAssistant, viewer: MockConfigEntry, extra_js: MagicMock, hass_client: ClientSessionGenerator
) -> None:
    extra_js.assert_called_once_with(hass, "/prusa_gcode_viewer/prusa-gcode-viewer-card.js?v=1.0.0")
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
    assert ready["url"] == f"/api/prusa_gcode_viewer/model/{viewer.entry_id}/{ready['key']}"
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
