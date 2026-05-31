"""Constants for the Lutron GrafikEye GRX integration."""

DOMAIN = "lutron_grx"

DEFAULT_PORT = 4196

# This integration controls a single GRAFIK Eye control unit, hard-coded to
# MUX address 1.  The address appears both in the SELECT SCENE command
# (``:A<scene>1``) and as index 0 of the 8-character scene-status field.
CONTROL_UNIT = "1"

ERROR_CANNOT_CONNECT = "cannot_connect"

MIN_SCENE = 0
MAX_SCENE = 16

# Setting zone intensities directly puts the unit in a "manual" state that the
# interface reports as scene 17 ("H"), distinct from the 16 presets.
SCENE_MANUAL = 17

# The GRX-3504 has four zones. Intensities are hex 00-7F (0-99%).
ZONE_COUNT = 4
MAX_INTENSITY = 0x7F

# Select options are the bare scene numbers "0" … "16" (where "0" is off);
# they convert to/from the scene int with plain str()/int().
SCENE_OPTIONS: list[str] = [str(n) for n in range(MIN_SCENE, MAX_SCENE + 1)]

# RS232 error codes from the GRAFIK Eye integration protocol (Appendix A).
GRX_ERRORS: dict[int, str] = {
    1: "control unit raise/lower error",
    2: "invalid scene selected",
    6: "bad command was sent",
    13: "not a timeclock unit",
    14: "illegal time was entered",
    15: "invalid schedule",
    16: "no super sequence has been loaded",
    20: "command was missing control units",
    21: "command was missing data",
    22: "error in command argument (improper hex value)",
    24: "invalid control unit",
    25: "invalid value, outside range of acceptable values",
    26: "invalid accessory control",
    31: "network address illegally formatted",
    80: "time-out error, no response received",
    103: "invalid number of arguments",
}
