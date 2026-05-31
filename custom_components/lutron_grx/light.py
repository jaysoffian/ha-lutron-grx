"""Light platform for the Lutron GrafikEye GRX integration.

Exposes each of the control unit's four zones as a dimmable light, controlled
directly via zone-intensity commands (independent of scene selection).
"""

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_INTENSITY, ZONE_COUNT
from .convert import brightness_to_intensity, intensity_to_brightness
from .coordinator import LutronGrxCoordinator
from .entity import LutronGrxEntity

_DEFAULT_FADE = 1  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the four GrafikEye zone lights."""
    coordinator: LutronGrxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        LutronGrxZoneLight(coordinator, zone) for zone in range(1, ZONE_COUNT + 1)
    )


class LutronGrxZoneLight(LutronGrxEntity, LightEntity):
    """A single dimmable zone on the GrafikEye control unit."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, coordinator: LutronGrxCoordinator, zone: int) -> None:
        super().__init__(coordinator)
        # Set here (not at class level) to avoid a shared mutable default.
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._zone = zone
        self._index = zone - 1
        self._attr_name = f"Zone {zone}"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_zone_{zone}"

    def _update_attrs(self) -> None:
        data = self.coordinator.data
        self._attr_available = bool(
            self.coordinator.last_update_success
            and data.connected
            and not data.unit_missing
        )
        intensity = data.zone_intensities[self._index]
        if intensity is None:
            self._attr_is_on = None
            self._attr_brightness = None
        else:
            self._attr_is_on = intensity > 0
            self._attr_brightness = intensity_to_brightness(intensity)

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            intensity = MAX_INTENSITY
        else:
            # Ensure turning on never resolves to "off" at the lowest steps.
            intensity = max(1, brightness_to_intensity(brightness))
        fade = int(kwargs.get(ATTR_TRANSITION, _DEFAULT_FADE))
        await self.coordinator.client.set_zone_intensity(self._zone, intensity, fade)

    async def async_turn_off(self, **kwargs: Any) -> None:
        fade = int(kwargs.get(ATTR_TRANSITION, _DEFAULT_FADE))
        await self.coordinator.client.set_zone_intensity(self._zone, 0, fade)
