"""Sensor platform for the Lutron GrafikEye GRX integration."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .client import GrxState
from .const import DOMAIN
from .coordinator import LutronGrxCoordinator
from .entity import LutronGrxEntity


@dataclass(frozen=True, kw_only=True)
class LutronGrxSensorEntityDescription(SensorEntityDescription):
    """Describes a Lutron GrafikEye sensor entity."""

    value_fn: Callable[[GrxState], StateType]


SENSORS: tuple[LutronGrxSensorEntityDescription, ...] = (
    LutronGrxSensorEntityDescription(
        key="code_rev",
        translation_key="code_rev",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.code_rev,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lutron GrafikEye sensors."""
    coordinator: LutronGrxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(LutronGrxSensorEntity(coordinator, desc) for desc in SENSORS)


class LutronGrxSensorEntity(LutronGrxEntity, SensorEntity):
    """A Lutron GrafikEye sensor entity."""

    entity_description: LutronGrxSensorEntityDescription

    def __init__(
        self,
        coordinator: LutronGrxCoordinator,
        description: LutronGrxSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"

    def _update_attrs(self) -> None:
        super()._update_attrs()
        self._attr_native_value = self.entity_description.value_fn(
            self.coordinator.data
        )
