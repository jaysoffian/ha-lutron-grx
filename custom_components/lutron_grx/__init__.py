"""The Lutron GrafikEye GRX integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import LutronGrxCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SELECT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lutron GrafikEye from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    coordinator = LutronGrxCoordinator(hass, entry, host, port)

    try:
        await coordinator.async_connect()
        if not await coordinator.client.wait_for(lambda s: s.connected, timeout=5):
            raise ConfigEntryNotReady("Device not connected")

        # Code revision is diagnostic — query best-effort, don't fail setup.
        await coordinator.client.query_code_rev()
        await coordinator.client.request_scene_status()
        await coordinator.client.request_zone_intensities()
    except ConfigEntryNotReady:
        raise
    except Exception as err:
        _LOGGER.error("Failed to set up GrafikEye at %s:%s: %s", host, port, err)
        raise ConfigEntryNotReady(f"Failed to set up: {err}") from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("Lutron GrafikEye integration ready (%s:%s)", host, port)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: LutronGrxCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
