"""Value conversions between the GRX wire protocol and Home Assistant.

Scene numbers <-> protocol characters, and Lutron intensities <-> percentages
and Home Assistant brightness.
"""

from contextlib import suppress

from .const import MAX_INTENSITY, MAX_SCENE, MIN_SCENE, SCENE_MANUAL


def scene_to_char(scene: int) -> str:
    """Encode a scene number (0-16) as its protocol character.

    0 → "0" (OFF), 1-9 → "1"-"9", 10-16 → "A"-"G".
    """
    if not MIN_SCENE <= scene <= MAX_SCENE:
        raise ValueError(f"Scene must be {MIN_SCENE}-{MAX_SCENE}, got {scene}")
    return "G" if scene == 16 else f"{scene:X}"


def char_to_scene(char: str) -> int | None:
    """Decode a protocol scene character to a scene number.

    "0" → 0, "1"-"9" → 1-9, "A"-"G" → 10-16, "H" → 17 (manual zone intensities).
    """
    if len(char) != 1:
        return None
    if char == "G":
        return 16
    if char == "H":
        return SCENE_MANUAL
    with suppress(ValueError):
        return int(char, 16)


# 128 intensity steps (0x00-0x7F). The interior reproduces the table on page 3
# of the protocol spec (intensity = ceil(percent * 1.28), e.g. 0x40 = 50%); the
# endpoints are anchored so 0x00 = 0% and 0x7F = 100%. Lutron labels its max
# "99%", but it is full output and HA expects full = 100%.


def percent_to_intensity(percent: int) -> int:
    """Intensity (0x00-0x7F) for a 0-100% level."""
    if percent == 0:
        return 0
    if percent == 100:
        return MAX_INTENSITY
    return (percent * 128 + 99) // 100  # ceil(percent * 1.28)


def intensity_to_percent(intensity: int) -> int:
    """Percent (0-100) for an intensity (0x00-0x7F)."""
    if intensity == 0:
        return 0
    if intensity == MAX_INTENSITY:
        return 100
    return intensity * 100 // 128  # floor(intensity / 1.28)


def intensity_to_brightness(intensity: int) -> int:
    """Intensity (0x00-0x7F) to a Home Assistant brightness (0-255).

    Via the percent step, not a direct 0x7F->255 rescale, so 0x40 (50%) gives
    128 instead of 129 (which HA renders as 51%).
    """
    return round(intensity_to_percent(intensity) * 255 / 100)


def brightness_to_intensity(brightness: int) -> int:
    """Home Assistant brightness (0-255) to an intensity (0x00-0x7F)."""
    return percent_to_intensity(round(brightness * 100 / 255))
