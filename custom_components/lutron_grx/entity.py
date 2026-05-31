"""Base entity for the Lutron GrafikEye GRX integration."""

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LutronGrxCoordinator


class LutronGrxEntity(CoordinatorEntity[LutronGrxCoordinator]):
    """Base class — shared device_info and availability."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LutronGrxCoordinator) -> None:
        super().__init__(coordinator)
        data = coordinator.data
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.entry.entry_id)},
            "name": "GrafikEye 3504",
            "manufacturer": "Lutron",
            "model": "GrafikEye 3504",
            "sw_version": data.code_rev,
        }

    @property
    def available(self) -> bool:
        """Override CoordinatorEntity which ignores _attr_available."""
        return self._attr_available

    async def async_added_to_hass(self) -> None:
        """Compute initial state when the entity is registered."""
        await super().async_added_to_hass()
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Recompute _attr_ values. Availability = connected. Override as needed."""
        data = self.coordinator.data
        self._attr_available = bool(
            self.coordinator.last_update_success and data.connected
        )

    def _handle_coordinator_update(self) -> None:
        self._update_attrs()
        super()._handle_coordinator_update()
