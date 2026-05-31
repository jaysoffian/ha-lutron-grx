"""Config flow for the Lutron GrafikEye GRX integration."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .client import LutronGrxClient
from .const import DEFAULT_PORT, DOMAIN, ERROR_CANNOT_CONNECT

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)


class LutronGrxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lutron GrafikEye GRX."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user configuration — host and port."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            if await self._test_connection(host, port):
                await self.async_set_unique_id(f"{host}_{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"GrafikEye ({host})",
                    data=user_input,
                )
            errors["base"] = ERROR_CANNOT_CONNECT

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of host and port."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            if await self._test_connection(host, port):
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    title=f"GrafikEye ({host})",
                    data={CONF_HOST: host, CONF_PORT: port},
                    unique_id=f"{host}_{port}",
                )
            errors["base"] = ERROR_CANNOT_CONNECT

        entry = self._get_reconfigure_entry()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST, "")): str,
                    vol.Required(
                        CONF_PORT, default=entry.data.get(CONF_PORT, DEFAULT_PORT)
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                }
            ),
            errors=errors,
        )

    @staticmethod
    async def _test_connection(host: str, port: int) -> bool:
        """Connect and verify the device answers the CODE REV LEVEL query."""
        client = LutronGrxClient(host, port)
        try:
            await client.connect()
            if not await client.wait_for(lambda s: s.connected, timeout=5):
                _LOGGER.debug("Connection test: TCP connect failed")
                return False
            await client.query_code_rev()
            if not await client.wait_for(lambda s: s.code_rev is not None, timeout=5):
                _LOGGER.debug("Connection test: device did not respond to :V")
                return False
            return True
        except Exception:
            _LOGGER.debug("Connection test failed for %s:%s", host, port, exc_info=True)
            return False
        finally:
            await client.disconnect()
