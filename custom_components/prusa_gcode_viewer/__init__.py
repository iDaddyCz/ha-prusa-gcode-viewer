"""Prusa G-code Viewer: a 3D view of what a PrusaLink printer is printing."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.http import KEY_HASS
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import CARD_FILE, DOMAIN, MAX_FILE_SIZE, MODEL_URL, UPLOAD_URL, URL_BASE
from .viewer import ModelCache

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type PrusaGcodeViewerConfigEntry = ConfigEntry[ModelCache]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "www"), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={integration.version}")
    hass.http.register_view(ModelView())
    hass.http.register_view(UploadView())
    websocket_api.async_register_command(hass, ws_status)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PrusaGcodeViewerConfigEntry) -> bool:
    entry.runtime_data = ModelCache(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PrusaGcodeViewerConfigEntry) -> bool:
    return True


@callback
def _cache(hass: HomeAssistant, entry_id: str | None) -> ModelCache | None:
    """The viewer for entry_id, or the only one set up."""
    entries: list[PrusaGcodeViewerConfigEntry] = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and entry_id in (None, e.entry_id)
    ]
    return entries[0].runtime_data if len(entries) == 1 else None


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/status", vol.Optional("entry_id"): str})
@websocket_api.async_response
async def ws_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The model to show; starts building it the first time a print is viewed."""
    cache = _cache(hass, msg.get("entry_id"))
    if cache is None:
        several = "entry_id" not in msg and len(hass.config_entries.async_loaded_entries(DOMAIN)) > 1
        message = "Several printers are set up: set entry_id" if several else "Prusa G-code Viewer is not set up"
        connection.send_error(msg["id"], "not_found", message)
        return
    connection.send_result(msg["id"], await cache.async_status())


class ModelView(HomeAssistantView):
    """A built model; its key changes with the print, so it can be cached for good."""

    url = MODEL_URL
    name = f"api:{DOMAIN}:model"

    async def get(self, request: web.Request, entry_id: str, key: str) -> web.Response:
        hass = request.app[KEY_HASS]
        cache = _cache(hass, entry_id)
        body = await hass.async_add_executor_job(cache.read, key) if cache else None
        if body is None:
            return web.Response(status=404)
        return web.Response(
            body=body,
            content_type="application/octet-stream",
            headers={"Content-Encoding": "gzip", "Cache-Control": "private, max-age=31536000, immutable"},
        )


class UploadView(HomeAssistantView):
    """A G-code or 3MF file dropped on the card, built in the background.

    Answers once the file is here: a large one can take longer to build than a reverse
    proxy waits. The card follows the build through the status command.
    """

    url = UPLOAD_URL
    name = f"api:{DOMAIN}:upload"

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        cache = _cache(hass, request.query.get("entry_id"))
        if cache is None:
            return self.json_message("Prusa G-code Viewer is not set up", HTTPStatus.NOT_FOUND)
        too_large = self.json_message("The file is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if (request.content_length or 0) > MAX_FILE_SIZE:
            return too_large
        chunks = []
        received = 0
        # Read as a stream: Home Assistant limits bodies read in one go to 16 MB.
        async for chunk in request.content.iter_any():
            received += len(chunk)
            if received > MAX_FILE_SIZE:
                return too_large
            chunks.append(chunk)
        if not received:
            return self.json_message("The file is empty", HTTPStatus.BAD_REQUEST)
        name = request.query.get("name", "").replace("\\", "/").rsplit("/", 1)[-1][:200] or "Dropped file"
        cache.async_upload(name, b"".join(chunks))
        return self.json({"name": name}, HTTPStatus.ACCEPTED)
