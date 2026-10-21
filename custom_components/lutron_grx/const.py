"""Constants for the Lutron GrafikEye GRX integration."""

DOMAIN = "lutron_grx"

DEFAULT_PORT = 4196

# Defaults preserve the original single-GRX-3504 behaviour.  A config entry may
# override these with one or more control units on the MUX link.
DEFAULT_UNIT = 1
DEFAULT_MODEL = "GRX-3504"
DEFAULT_ZONE_COUNT = 4

# The MUX link supports up to eight control unit addresses.  Each address
# exposes at most eight zones; a 16- or 24-zone unit occupies two or three
# consecutive addresses.
MAX_UNITS = 8
MAX_ZONES = 24
ZONES_PER_ADDRESS = 8

# Kept for backwards compatibility with the original single-unit integration.
CONTROL_UNIT = str(DEFAULT_UNIT)
ZONE_COUNT = DEFAULT_ZONE_COUNT

CONF_ADDRESS = "address"
CONF_MODEL = "model"
CONF_UNITS = "units"
CONF_ZONES = "zones"

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_UNITS = "invalid_units"

MIN_SCENE = 0
MAX_SCENE = 16

# Setting zone intensities directly puts the unit in a "manual" state that the
# interface reports as scene 17 ("H"), distinct from the 16 presets.
SCENE_MANUAL = 17

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
