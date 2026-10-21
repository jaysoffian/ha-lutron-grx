"""Config flow for the Lutron GrafikEye GRX integration."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .client import (
    LutronGrxClient,
    UnitConfig,
    unit_configs_from_data,
    validate_unit_configs,
)
from .const import (
    CONF_MODEL,
    CONF_UNITS,
    CONF_ZONES,
    DEFAULT_PORT,
    DOMAIN,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_UNITS,
    MAX_UNITS,
    MAX_ZONES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated form value, dropping empty entries."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _expand(values: list[str], count: int, field: str) -> list[str]:
    """Broadcast a single value to every unit, or require one per unit."""
    if len(values) == 1:
        return values * count
    if len(values) != count:
        raise ValueError(f"{field} must contain one value or {count} values")
    return values


def _parse_units(units: str, zones: str, models: str) -> list[UnitConfig]:
    """Parse the confirmed unit form values into validated unit configs."""
    addresses = [int(value) for value in _split_csv(units)]
    if not addresses:
        raise ValueError("At least one control unit address is required")
    if len(addresses) > MAX_UNITS:
        raise ValueError(f"At most {MAX_UNITS} control units are supported")

    zone_values = _expand(_split_csv(zones), len(addresses), "Zones")
    model_values = _expand(_split_csv(models), len(addresses), "Models")
    configs: list[UnitConfig] = []
    for address, zone_value, model in zip(
        addresses, zone_values, model_values, strict=True
    ):
        zone_count = int(zone_value)
        if not 1 <= zone_count <= MAX_ZONES:
            raise ValueError(f"Zones must be 1-{MAX_ZONES}")
        configs.append(UnitConfig(address=address, zones=zone_count, model=model))
    validate_unit_configs(configs)
    return sorted(configs, key=lambda config: config.address)


def _unit_form_defaults(configs: list[UnitConfig]) -> tuple[str, str, str]:
    """Return comma-separated defaults for the unit confirmation form."""
    return (
        ",".join(str(config.address) for config in configs),
        ",".join(str(config.zones) for config in configs),
        ",".join(config.model for config in configs),
    )


def _units_schema(units: str, zones: str, models: str) -> vol.Schema:
    """Build the unit confirmation schema with current defaults."""
    return vol.Schema(
        {
            vol.Required(CONF_UNITS, default=units): str,
            vol.Required(CONF_ZONES, default=zones): str,
            vol.Required(CONF_MODEL, default=models): str,
        }
    )


class LutronGrxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lutron GrafikEye GRX."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._host = ""
        self._port = DEFAULT_PORT
        self._detected_units = [UnitConfig()]
        self._reconfigure_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user configuration — host, port, and control units."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            detected = await self._detect_units(host, port)

            if detected is not None:
                await self.async_set_unique_id(f"{host}_{port}")
                self._abort_if_unique_id_configured()
                self._host = host
                self._port = port
                self._detected_units = detected or [UnitConfig()]
                return await self.async_step_units()
            errors["base"] = ERROR_CANNOT_CONNECT

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_units(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the detected (or manually entered) control units."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                configs = _parse_units(
                    user_input[CONF_UNITS],
                    user_input[CONF_ZONES],
                    user_input[CONF_MODEL],
                )
            except ValueError:
                _LOGGER.debug("Invalid unit configuration", exc_info=True)
                errors["base"] = ERROR_INVALID_UNITS
                defaults = (
                    user_input.get(CONF_UNITS, ""),
                    user_input.get(CONF_ZONES, ""),
                    user_input.get(CONF_MODEL, ""),
                )
            else:
                data = {
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_UNITS: [config.as_dict() for config in configs],
                }
                title = f"GrafikEye ({self._host})"
                if self._reconfigure_entry is not None:
                    return self.async_update_reload_and_abort(
                        self._reconfigure_entry,
                        title=title,
                        data=data,
                        unique_id=f"{self._host}_{self._port}",
                    )
                return self.async_create_entry(title=title, data=data)
        else:
            defaults = _unit_form_defaults(self._detected_units)

        return self.async_show_form(
            step_id="units",
            data_schema=_units_schema(*defaults),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of the connection and control units."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            detected = await self._detect_units(host, port)

            if detected is not None:
                self._host = host
                self._port = port
                self._reconfigure_entry = entry
                self._detected_units = detected or unit_configs_from_data(entry.data)
                return await self.async_step_units()
            errors["base"] = ERROR_CANNOT_CONNECT

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
    async def _detect_units(host: str, port: int) -> list[UnitConfig] | None:
        """Connect, verify the interface, and probe for control units.

        Returns ``None`` when the connection test fails and an empty list when
        the interface answers but no units could be identified.
        """
        client = LutronGrxClient(host, port)
        try:
            await client.connect()
            if not await client.wait_for(lambda s: s.connected, timeout=5):
                _LOGGER.debug("Connection test: TCP connect failed")
                return None
            await client.query_code_rev()
            if not await client.wait_for(lambda s: s.code_rev is not None, timeout=5):
                _LOGGER.debug("Connection test: device did not respond to :V")
                return None
            try:
                return await client.detect_units()
            except Exception:
                _LOGGER.debug(
                    "Control unit detection failed for %s:%s",
                    host,
                    port,
                    exc_info=True,
                )
                return []
        except Exception:
            _LOGGER.debug("Connection test failed for %s:%s", host, port, exc_info=True)
            return None
        finally:
            await client.disconnect()
