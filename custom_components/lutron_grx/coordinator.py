"""Data coordinator for the Lutron GrafikEye GRX integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import GrxState, LutronGrxClient, unit_configs_from_data
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class LutronGrxCoordinator(DataUpdateCoordinator[GrxState]):
    """Coordinator for Lutron GrafikEye — pure push, no HA-driven polling.

    The client owns its own 30 s poll (keepalive + scene refresh); HA's
    coordinator interval is disabled and state is delivered via callbacks.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        host: str,
        port: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = LutronGrxClient(host, port, unit_configs_from_data(entry.data))
        self.entry = entry

    async def async_connect(self) -> None:
        """Connect to the device, wiring client callbacks to HA."""
        await self.client.connect(
            on_state_changed=self.on_state_changed,
            on_connection_changed=self.on_connection_changed,
        )

    def on_state_changed(self) -> None:
        """Called (sync) by the client whenever device state changes."""
        self.async_set_updated_data(self.client.state)

    def on_connection_changed(self, connected: bool) -> None:
        """Called (sync) by the client on connect / disconnect."""
        _LOGGER.info("Connection %s", "established" if connected else "lost")
        self.async_set_updated_data(self.client.state)

    async def _async_update_data(self) -> GrxState:
        """Fallback for the first refresh — returns the current snapshot."""
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect the client."""
        _LOGGER.info("Shutting down Lutron GrafikEye coordinator")
        await self.client.disconnect()
