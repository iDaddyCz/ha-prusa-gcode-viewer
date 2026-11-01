"""Config flow: pick the PrusaLink printer whose prints to show."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig

from .const import CONF_PRINTER, DOMAIN, PRUSALINK_DOMAIN


class PrusaGcodeViewerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        configured = {e.data[CONF_PRINTER] for e in self._async_current_entries()}
        printers = {
            e.entry_id: e.title
            for e in self.hass.config_entries.async_entries(PRUSALINK_DOMAIN)
            if e.entry_id not in configured
        }
        if not printers:
            return self.async_abort(
                reason="already_configured" if configured else "no_printers",
            )
        if user_input is None and len(printers) > 1:
            options = [SelectOptionDict(value=k, label=v) for k, v in printers.items()]
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({vol.Required(CONF_PRINTER): SelectSelector(SelectSelectorConfig(options=options))}),
            )
        printer = user_input[CONF_PRINTER] if user_input else next(iter(printers))
        await self.async_set_unique_id(printer)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=printers[printer], data={CONF_PRINTER: printer})
