"""Config flow: pick up the bike by Bluetooth discovery (or choose it manually)."""

from __future__ import annotations

from typing import Any, override

import probatio

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import CONF_RIDER, CONF_USER_MAP, DEFAULT_RIDER, DEFAULT_USER_MAP, DOMAIN
from .protocol import SERVICE_UUID


class SchwinnConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._devices: dict[str, BluetoothServiceInfoBleak] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> SchwinnOptionsFlow:
        return SchwinnOptionsFlow()

    @override
    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name or "Schwinn 170/270"}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._discovery is not None
        if user_input is not None:
            return self._create(self._discovery, user_input)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=probatio.Schema({probatio.Optional(CONF_RIDER, default=DEFAULT_RIDER): str, probatio.Optional(CONF_USER_MAP, default=DEFAULT_USER_MAP): str}),
            description_placeholders={"name": self._discovery.name or "Schwinn 170/270"},
        )

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            info = self._devices[user_input[CONF_ADDRESS]]
            await self.async_set_unique_id(info.address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create(info, user_input)

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if SERVICE_UUID in [u.lower() for u in info.service_uuids] and info.address not in current:
                self._devices[info.address] = info
        if not self._devices:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_ADDRESS): probatio.In({a: f"{i.name} ({a})" for a, i in self._devices.items()}),
                    probatio.Optional(CONF_RIDER, default=DEFAULT_RIDER): str,
                    probatio.Optional(CONF_USER_MAP, default=DEFAULT_USER_MAP): str,
                }
            ),
        )

    def _create(self, info: BluetoothServiceInfoBleak, user_input: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=info.name or "Schwinn 170/270",
            data={CONF_ADDRESS: info.address},
            options={CONF_RIDER: user_input.get(CONF_RIDER, DEFAULT_RIDER), CONF_USER_MAP: user_input.get(CONF_USER_MAP, DEFAULT_USER_MAP)},
        )


class SchwinnOptionsFlow(OptionsFlowWithReload):
    """Who rides: the console user number maps to a person id, a ride is logged to that person (their max HR/zones come from /config/weight/people.json)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_RIDER, DEFAULT_RIDER)
        umap = self.config_entry.options.get(CONF_USER_MAP, DEFAULT_USER_MAP)
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema({probatio.Optional(CONF_RIDER, default=current): str, probatio.Optional(CONF_USER_MAP, default=umap): str}),
        )
