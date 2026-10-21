"""Base entity for the Lutron GrafikEye GRX integration."""

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import UnitConfig
from .const import DEFAULT_MODEL, DOMAIN
from .coordinator import LutronGrxCoordinator


class LutronGrxEntity(CoordinatorEntity[LutronGrxCoordinator]):
    """Base class — shared device_info and availability."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LutronGrxCoordinator,
        unit: UnitConfig | None = None,
    ) -> None:
        super().__init__(coordinator)
        configs = coordinator.client.unit_configs
        self.unit = unit or configs[0]
        self._single_unit = len(configs) == 1
        legacy = (
            self._single_unit
            and self.unit.address == 1
            and self.unit.zones == 4
            and self.unit.model == DEFAULT_MODEL
        )
        data = coordinator.data
        if self._single_unit:
            identifier = coordinator.entry.entry_id
        else:
            identifier = f"{coordinator.entry.entry_id}_{self.unit.address}"
        if legacy:
            name = "GrafikEye 3504"
            model = "GrafikEye 3504"
        else:
            name = f"GrafikEye {self.unit.model}"
            model = self.unit.model
            if not self._single_unit:
                name = f"{name} (A{self.unit.address})"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, identifier)},
            "name": name,
            "manufacturer": "Lutron",
            "model": model,
            "sw_version": data.code_rev,
        }

    def _unit_unique_id(self, suffix: str) -> str:
        """Build a stable unique id, preserving single-unit compatibility."""
        if self._single_unit:
            return f"{self.coordinator.entry.entry_id}_{suffix}"
        return f"{self.coordinator.entry.entry_id}_{self.unit.address}_{suffix}"

    def _unit_available(self) -> bool:
        """Whether this logical unit is connected and present on the link."""
        data = self.coordinator.data
        return bool(
            self.coordinator.last_update_success
            and data.connected
            and not data.unit_missing_for(self.unit)
        )

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
