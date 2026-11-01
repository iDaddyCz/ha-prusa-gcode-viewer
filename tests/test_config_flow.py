"""Choosing the PrusaLink printer."""

from __future__ import annotations

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.prusa_gcode_viewer.const import CONF_PRINTER, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import make_printer


pytestmark = pytest.mark.usefixtures("deps")


async def start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_no_printer(hass: HomeAssistant) -> None:
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_printers"


async def test_single_printer_is_used_directly(hass: HomeAssistant) -> None:
    make_printer().add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Prusa Core One"
    assert result["data"] == {CONF_PRINTER: "printer1"}
    assert result["result"].unique_id == "printer1"


async def test_choose_between_printers(hass: HomeAssistant) -> None:
    make_printer().add_to_hass(hass)
    make_printer("printer2", "Prusa XL").add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER: "printer2"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Prusa XL"


async def test_every_printer_has_a_viewer(hass: HomeAssistant) -> None:
    make_printer().add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, unique_id="printer1", data={CONF_PRINTER: "printer1"}).add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
