"""Select platform for the Lutron GrafikEye GRX integration.

The scene select doubles as scene status: ``current_option`` reflects the
currently selected scene ("0" … "16", where "0" is off), and selecting an
option sends a SELECT SCENE command.
"""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_SCENE, MIN_SCENE, SCENE_OPTIONS
from .coordinator import LutronGrxCoordinator
from .entity import LutronGrxEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Lutron GrafikEye scene select."""
    coordinator: LutronGrxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LutronGrxSceneSelect(coordinator)])


class LutronGrxSceneSelect(LutronGrxEntity, SelectEntity):
    """Scene select for the GrafikEye control unit."""

    _attr_translation_key = "scene"
    _attr_icon = "mdi:lightbulb-group"
    _attr_options = SCENE_OPTIONS

    def __init__(self, coordinator: LutronGrxCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_scene"
        self._optimistic_option: str | None = None

    def _update_attrs(self) -> None:
        super()._update_attrs()
        data = self.coordinator.data
        # The unit is unavailable for scene control when it is missing.
        self._attr_available = bool(
            self.coordinator.last_update_success
            and data.connected
            and not data.unit_missing
        )
        scene = data.current_scene
        if self._optimistic_option is not None:
            self._attr_current_option = self._optimistic_option
        elif scene is not None and MIN_SCENE <= scene <= MAX_SCENE:
            self._attr_current_option = str(scene)
        else:
            # A manual scene (17) outside our preset list shows no selection.
            self._attr_current_option = None

    def _handle_coordinator_update(self) -> None:
        self._optimistic_option = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        self._optimistic_option = option
        self._attr_current_option = option
        self.async_write_ha_state()
        try:
            await self.coordinator.client.select_scene(int(option))
        except Exception:
            self._optimistic_option = None
            self._update_attrs()
            self.async_write_ha_state()
            raise
