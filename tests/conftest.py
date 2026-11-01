"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.prusa_gcode_viewer.const import CONF_PRINTER, DOMAIN, PRUSALINK_SENSORS
from pytest_homeassistant_custom_component.common import MockConfigEntry

PRINTER_ID = "printer1"
HOST = "http://printer.local"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def cache_dir(hass: HomeAssistant, tmp_path) -> None:
    """Keep built models out of the shared test config directory."""
    hass.config.config_dir = str(tmp_path)


@pytest.fixture
def extra_js() -> Generator[MagicMock]:
    """The frontend isn't installed for tests: stand in for its card registration."""
    with patch("custom_components.prusa_gcode_viewer.add_extra_js_url") as mock:
        yield mock


@pytest.fixture
async def deps(hass: HomeAssistant, extra_js: MagicMock) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "websocket_api", {})
    hass.config.components.add("frontend")


def make_printer(entry_id: str = PRINTER_ID, title: str = "Prusa Core One") -> MockConfigEntry:
    return MockConfigEntry(
        domain="prusalink",
        entry_id=entry_id,
        title=title,
        version=1,
        minor_version=2,
        data={"host": HOST, "username": "maker", "password": "secret"},
    )


@pytest.fixture
def printer(hass: HomeAssistant) -> MockConfigEntry:
    """A PrusaLink entry (not set up) with the sensors the card follows."""
    entry = make_printer()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for name, key in PRUSALINK_SENSORS.items():
        registry.async_get_or_create(
            "sensor", "prusalink", f"{PRINTER_ID}_{key}", config_entry=entry, suggested_object_id=f"core_one_{name}"
        )
    return entry


@pytest.fixture
async def viewer(hass: HomeAssistant, deps: None, printer: MockConfigEntry) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Prusa Core One", unique_id=PRINTER_ID, data={CONF_PRINTER: PRINTER_ID})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
