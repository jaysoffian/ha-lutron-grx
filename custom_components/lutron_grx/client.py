"""Async TCP client for a Lutron GrafikEye via a GRX-CI-RS232 interface.

The GRX-CI-RS232 speaks the GRAFIK Eye RS232 ASCII protocol.  Commands are
ASCII strings terminated with a carriage return; responses begin with ``~``
and end with ``<CR><LF>``.  When scene-status reporting is enabled (DIP 7),
the interface also emits unsolicited ``:ss`` lines on scene changes.

A config entry may contain several logical control units.  Each logical unit
starts at a MUX address and may span two or three consecutive addresses when it
has 16 or 24 zones.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_ADDRESS,
    CONF_MODEL,
    CONF_UNITS,
    CONF_ZONES,
    DEFAULT_MODEL,
    DEFAULT_UNIT,
    DEFAULT_ZONE_COUNT,
    GRX_ERRORS,
    MAX_INTENSITY,
    MAX_SCENE,
    MAX_UNITS,
    MAX_ZONES,
    MIN_SCENE,
    SCENE_MANUAL,
    ZONES_PER_ADDRESS,
)
from .convert import char_to_scene, scene_to_char

_LOGGER = logging.getLogger(__name__)

# Poll cadence (seconds). The poll loop sends :G + :rzi to refresh scene and
# zone state; the interval adapts to what the device is doing.
_POLL_OFF = 30.0  # unit off (scene 0 / all zones 0): just keep state fresh
_POLL_ON = 3.0  # on and stable
_POLL_FAST = 0.5  # while a scene/zone change is settling
_FAST_WINDOW = 2.0  # stay fast until this long after the last observed change
_CONNECT_TIMEOUT = 10.0
_DETECT_TIMEOUT = 1.0  # per-address timeout while probing for control units

# Connection watchdog: reconnect if nothing is received for this long. Kept
# well above the slowest poll so an idle (off) unit is not flagged.
_WATCHDOG_CHECK = 5.0
_STALE_TIMEOUT = 90.0

# Trailing acknowledgement appended to responses, e.g. " 1 OK" or
# "ERROR #6 0 OK".  Group 1 captures the error code when present.
_ACK_RE = re.compile(r"\s*(?:ERROR #(\d+) )?\d+ OK\s*$")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitConfig:
    """A logical control unit starting at one MUX address."""

    address: int = DEFAULT_UNIT
    zones: int = DEFAULT_ZONE_COUNT
    model: str = DEFAULT_MODEL

    def __post_init__(self) -> None:
        if not 1 <= self.address <= MAX_UNITS:
            raise ValueError(f"Address must be 1-{MAX_UNITS}, got {self.address}")
        if not 1 <= self.zones <= MAX_ZONES:
            raise ValueError(f"Zone count must be 1-{MAX_ZONES}, got {self.zones}")
        if self.address + self.address_count - 1 > MAX_UNITS:
            raise ValueError(
                f"A {self.zones}-zone unit at address {self.address} "
                f"does not fit in addresses 1-{MAX_UNITS}"
            )
        if not self.model.strip():
            raise ValueError("Model must not be empty")

    @property
    def address_count(self) -> int:
        """Number of consecutive MUX addresses occupied by this unit."""
        return (self.zones - 1) // ZONES_PER_ADDRESS + 1

    @property
    def addresses(self) -> tuple[int, ...]:
        """The MUX addresses occupied by this unit."""
        return tuple(range(self.address, self.address + self.address_count))

    def address_for_zone(self, zone: int) -> int:
        """Return the MUX address carrying a logical zone number."""
        if not 1 <= zone <= self.zones:
            raise ValueError(f"Zone must be 1-{self.zones}, got {zone}")
        return self.address + (zone - 1) // ZONES_PER_ADDRESS

    def zone_index(self, zone: int) -> int:
        """Return the zone's index within its address (0-7)."""
        if not 1 <= zone <= self.zones:
            raise ValueError(f"Zone must be 1-{self.zones}, got {zone}")
        return (zone - 1) % ZONES_PER_ADDRESS

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable config-entry representation."""
        return {
            CONF_ADDRESS: self.address,
            CONF_ZONES: self.zones,
            CONF_MODEL: self.model,
        }


@dataclass(frozen=True)
class UnitInfo:
    """Details returned by the ``:rmu`` (read control unit info) command."""

    address: int
    unit_type: str
    zones: int
    code_rev: str
    units_inv: int | None = None
    temp_mode: int | None = None
    pll: int | None = None
    four_q: int | None = None
    ir_addr: int | None = None

    @property
    def multi_address_index(self) -> int | None:
        """Zero-based position within a 16/24-zone unit, if reported."""
        if self.pll is None:
            return None
        return {0x11: 0, 0x12: 1, 0x13: 2}.get(self.pll)


@dataclass
class UnitState:
    """State for one MUX address (at most eight zones)."""

    address: int
    current_scene: int | None = None
    unit_missing: bool = False
    zone_intensities: tuple[int | None, ...] = (None,) * ZONES_PER_ADDRESS

    def __setattr__(self, name: str, value: object) -> None:
        try:
            old = object.__getattribute__(self, name)
        except AttributeError:
            pass
        else:
            if old != value and not name.startswith("_"):
                _LOGGER.debug("unit %s: %s: %r -> %r", self.address, name, old, value)
        object.__setattr__(self, name, value)


@dataclass
class GrxState:
    """Device state, updated in place by the read loop.

    Any assignment that actually changes a value emits a debug log via
    ``__setattr__``.  Legacy properties delegate to the primary unit so the
    original single-unit API keeps working.
    """

    connected: bool = False
    code_rev: str | None = None  # e.g. "5.4"
    model: str | None = None  # model token from the :V response (e.g. "0")
    scene_status: tuple[int | None, ...] | None = None  # eight MUX addresses
    units: dict[int, UnitState] = field(default_factory=dict)
    unit_configs: dict[int, UnitConfig] = field(default_factory=dict)
    unit_info: dict[int, UnitInfo] = field(default_factory=dict)
    primary_address: int = DEFAULT_UNIT

    def __post_init__(self) -> None:
        self.unit_state(self.primary_address)

    def __setattr__(self, name: str, value: object) -> None:
        try:
            old = object.__getattribute__(self, name)
        except AttributeError:
            pass  # Initial assignment during __init__
        else:
            if old != value and not name.startswith("_"):
                _LOGGER.debug("state: %s: %r -> %r", name, old, value)
        object.__setattr__(self, name, value)

    def unit_state(self, address: int) -> UnitState:
        """Return the state for an address, creating it if necessary."""
        unit = self.units.get(address)
        if unit is None:
            unit = UnitState(address)
            self.units[address] = unit
        return unit

    def configure(self, configs: list[UnitConfig]) -> None:
        """Apply the configured logical units to this state."""
        validate_unit_configs(configs)
        self.unit_configs = {config.address: config for config in configs}
        self.primary_address = configs[0].address
        for config in configs:
            for address in config.addresses:
                self.unit_state(address)

    @property
    def current_scene(self) -> int | None:
        """Scene of the primary unit (legacy single-unit API)."""
        return self.unit_state(self.primary_address).current_scene

    @current_scene.setter
    def current_scene(self, value: int | None) -> None:
        self.unit_state(self.primary_address).current_scene = value

    @property
    def unit_missing(self) -> bool:
        """Whether the primary unit is missing from the link."""
        return self.unit_state(self.primary_address).unit_missing

    @unit_missing.setter
    def unit_missing(self, value: bool) -> None:
        self.unit_state(self.primary_address).unit_missing = value

    @property
    def zone_intensities(self) -> tuple[int | None, ...]:
        """Zones of the primary unit (legacy single-unit API)."""
        config = self.unit_configs.get(self.primary_address)
        if config is None:
            return self.unit_state(self.primary_address).zone_intensities[
                :DEFAULT_ZONE_COUNT
            ]
        return self.zone_intensities_for(config)

    @zone_intensities.setter
    def zone_intensities(self, values: tuple[int | None, ...]) -> None:
        raw = list(values[:ZONES_PER_ADDRESS])
        raw.extend([None] * (ZONES_PER_ADDRESS - len(raw)))
        self.unit_state(self.primary_address).zone_intensities = tuple(raw)

    def current_scene_for(self, config: UnitConfig) -> int | None:
        """Scene reported by the first address of a logical unit."""
        unit = self.units.get(config.address)
        return unit.current_scene if unit else None

    def zone_intensities_for(self, config: UnitConfig) -> tuple[int | None, ...]:
        """Aggregate zones across all addresses of a logical unit."""
        values: list[int | None] = []
        for address in config.addresses:
            unit = self.units.get(address)
            raw = unit.zone_intensities if unit else (None,) * ZONES_PER_ADDRESS
            values.extend(raw)
        return tuple(values[: config.zones])

    def unit_missing_for(self, config: UnitConfig) -> bool:
        """Whether any address of a logical unit is missing."""
        return any(
            self.units[address].unit_missing
            for address in config.addresses
            if address in self.units
        )


def validate_unit_configs(configs: list[UnitConfig]) -> None:
    """Reject empty, duplicate, or overlapping unit configurations."""
    if not configs:
        raise ValueError("At least one control unit is required")
    occupied: set[int] = set()
    for config in configs:
        for address in config.addresses:
            if address in occupied:
                raise ValueError(
                    f"MUX address {address} is used by more than one control unit"
                )
            occupied.add(address)


def unit_configs_from_data(data: Mapping[str, Any]) -> list[UnitConfig]:
    """Build unit configs from config-entry data, falling back to legacy."""
    raw = data.get(CONF_UNITS)
    if not raw:
        return [UnitConfig()]
    try:
        configs: list[UnitConfig] = []
        for item in raw:
            if isinstance(item, UnitConfig):
                configs.append(item)
            elif isinstance(item, Mapping):
                configs.append(
                    UnitConfig(
                        address=int(item.get(CONF_ADDRESS, DEFAULT_UNIT)),
                        zones=int(item.get(CONF_ZONES, DEFAULT_ZONE_COUNT)),
                        model=str(item.get(CONF_MODEL, DEFAULT_MODEL)),
                    )
                )
            else:
                raise TypeError(f"Invalid unit entry: {item!r}")
        validate_unit_configs(configs)
    except TypeError, ValueError:
        _LOGGER.warning("Invalid unit configuration; using a single default unit")
        return [UnitConfig()]
    return sorted(configs, key=lambda config: config.address)


def model_for_unit_info(info: UnitInfo) -> str:
    """Best-effort model name from a ``:mu`` response."""
    zones = info.zones
    if info.unit_type == "35":
        return "GRX-3504" if zones == DEFAULT_ZONE_COUNT else f"GRX-35{zones:02d}"
    if info.unit_type == "45":
        return f"GRX-45{zones:02d}"
    return DEFAULT_MODEL if zones == DEFAULT_ZONE_COUNT else f"GRX-{zones:02d}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LutronGrxClient:
    """Async TCP client for a Lutron GrafikEye GRX-CI-RS232."""

    def __init__(
        self,
        host: str,
        port: int,
        units: list[UnitConfig] | None = None,
    ) -> None:
        self.unit_configs = sorted(
            units or [UnitConfig()], key=lambda config: config.address
        )
        validate_unit_configs(self.unit_configs)
        self.state = GrxState()
        self.state.configure(self.unit_configs)
        self._host = host
        self._port = port
        self._on_state_changed: Callable[[], None] | None = None
        self._on_connection_changed: Callable[[bool], None] | None = None
        # Raw-line hook for debugging tools (e.g. the CLI monitor command).
        self.on_raw_line: Callable[[str], None] | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._running = False
        self._disconnecting = False
        self._last_recv: float = 0.0
        self._fast_until: float = 0.0  # poll fast until this monotonic time
        self._suppress_zone_reads_until: float = 0.0  # hold our commanded fade
        self._write_lock = asyncio.Lock()
        self._wake = asyncio.Event()  # interrupts the poll sleep on a change
        self._state_waiters: list[tuple[asyncio.Event, Callable[[GrxState], bool]]] = []

    # -- Connection lifecycle -----------------------------------------------

    async def connect(
        self,
        on_state_changed: Callable[[], None] | None = None,
        on_connection_changed: Callable[[bool], None] | None = None,
    ) -> None:
        """Connect to the device and start background tasks."""
        self._on_state_changed = on_state_changed
        self._on_connection_changed = on_connection_changed
        await self._open_connection()

    async def disconnect(self) -> None:
        """Shut down cleanly."""
        self._running = False
        tasks = [
            t
            for t in (
                self._reconnect_task,
                self._poll_task,
                self._watchdog_task,
                self._read_task,
            )
            if t and not t.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._writer:
            with suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None
        self.state.connected = False

    async def _open_connection(self) -> None:
        """Open the TCP socket and spin up the read + poll loops."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=_CONNECT_TIMEOUT,
            )
        except (TimeoutError, OSError) as err:
            _LOGGER.error("Failed to connect to %s:%s: %s", self._host, self._port, err)
            self.state.connected = False
            if self._on_connection_changed:
                self._on_connection_changed(False)
            return

        _LOGGER.info("Connected to %s:%s", self._host, self._port)
        self._running = True
        self._last_recv = time.monotonic()
        self._wake.clear()
        self.state.connected = True
        self._read_task = asyncio.create_task(self._read_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        if self._on_connection_changed:
            self._on_connection_changed(True)

    async def _on_disconnect(self) -> None:
        """Tear down the current connection and start the reconnect loop."""
        if not self._running or self._disconnecting:
            return
        _LOGGER.info("Disconnected from %s:%s", self._host, self._port)
        self._disconnecting = True

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        if self._writer:
            with suppress(Exception):
                self._writer.close()
        self._reader = None
        self._writer = None

        was_connected = self.state.connected
        self.state.connected = False
        if was_connected:
            if self._on_connection_changed:
                self._on_connection_changed(False)
            self._notify_listeners()

        self._disconnecting = False
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Retry the TCP connection with exponential backoff (1s → 30s max)."""
        delay = 1.0
        while self._running:
            _LOGGER.info(
                "Attempting reconnect to %s:%s in %.0fs", self._host, self._port, delay
            )
            await asyncio.sleep(delay)
            if not self._running:
                return
            await self._open_connection()
            if self.state.connected:
                _LOGGER.info("Reconnected to %s:%s", self._host, self._port)
                await self.query_code_rev()
                await self.request_scene_status()
                await self.request_zone_intensities()
                return
            delay = min(delay * 2, 30.0)

    # -- Read loop ----------------------------------------------------------

    async def _read_loop(self) -> None:
        """Read lines from the device and dispatch them to the parser."""
        while self._running and self._reader:
            try:
                line = await self._reader.readline()
                if not line:  # EOF
                    break
                text = line.decode("ascii", errors="ignore").strip()
                if text:
                    self._last_recv = time.monotonic()
                    _LOGGER.debug("recv: %s", text)
                    if self.on_raw_line:
                        self.on_raw_line(text)
                    self._on_readline(text)
            except asyncio.CancelledError:
                return
            except OSError, ConnectionError:
                break
            except Exception:
                _LOGGER.exception("Read loop error")
                break

        if self._running:
            await self._on_disconnect()

    def _on_readline(self, line: str) -> None:
        """Parse a single line from the device.

        Handles the confirmed line shapes (a leading ``~`` may be present, and
        a trailing ``<N> OK`` acknowledgement may share the line):

          ``~:v 5 4 0 1 OK``  → interface code revision
          ``~:ss 2MMMMMMM 1 OK`` / ``:ss 1MMMMMMM`` (unsolicited) → scene status
          ``~:zi 1 0 7F ... 1 OK`` → zone intensities
          ``~:mu 1 35 4 51 6 1 1 0 0 1 OK`` → control unit info
          ``~1 OK`` / ``~ERROR #6 0 OK`` → bare ack / error
        """
        try:
            payload = line.removeprefix("~")

            error_code: int | None = None
            ack = _ACK_RE.search(payload)
            if ack:
                if ack.group(1) is not None:
                    error_code = int(ack.group(1))
                payload = payload[: ack.start()].strip()

            if payload.startswith(":v"):
                self._parse_code_rev(payload[2:].split())
            elif payload.startswith(":ss"):
                self._parse_scene_status(payload[3:].split())
            elif payload.startswith(":zi"):
                self._parse_zone_intensities(payload[3:].split())
            elif payload.startswith(":mu"):
                self._parse_unit_info(payload[3:].split())
            elif payload:
                _LOGGER.debug("unparsed: %r", line)
                return
            elif error_code is not None:
                _LOGGER.warning(
                    "Device reported error #%d: %s",
                    error_code,
                    GRX_ERRORS.get(error_code, "unknown error"),
                )
                return
            else:
                return  # bare "~N OK" ack — nothing to update
        except Exception:
            _LOGGER.exception("Error parsing line: %r", line)
            return

        self._notify_listeners()

    def _parse_code_rev(self, tokens: list[str]) -> None:
        """Parse ``:v <high> <low> <model>`` → "high.low" + model token."""
        if len(tokens) < 2:
            return
        self.state.code_rev = f"{tokens[0]}.{tokens[1]}"
        self.state.model = tokens[2] if len(tokens) > 2 else None

    def _parse_scene_status(self, tokens: list[str]) -> None:
        """Parse ``:ss <8 chars>`` for every control unit on the link."""
        if not tokens or not tokens[0]:
            return
        status: list[int | None] = []
        changed = False
        for index, char in enumerate(tokens[0][:MAX_UNITS]):
            unit = self.state.unit_state(index + 1)
            if char == "M":
                unit.unit_missing = True
                status.append(None)
                continue
            scene = char_to_scene(char)
            if scene is None:
                _LOGGER.debug("Unrecognized scene char %r for unit %d", char, index + 1)
                status.append(None)
                continue
            unit.unit_missing = False
            if unit.current_scene != scene:
                changed = True
            unit.current_scene = scene
            status.append(scene)
        status.extend([None] * (MAX_UNITS - len(status)))
        self.state.scene_status = tuple(status[:MAX_UNITS])
        if changed:
            # A scene change re-levels the zones; poll fast until they settle.
            self._kick_fast(wake=True)

    def _parse_zone_intensities(self, tokens: list[str]) -> None:
        """Parse ``:zi <unit> <Int1> … <Int8>`` for one MUX address."""
        if time.monotonic() < self._suppress_zone_reads_until:
            # We commanded a fade; hold the target rather than chase mid-fade reads.
            return
        if len(tokens) < 1 + ZONES_PER_ADDRESS:
            return
        try:
            address = int(tokens[0])
        except ValueError:
            return
        if not 1 <= address <= MAX_UNITS:
            return
        values: list[int | None] = []
        for token in tokens[1 : 1 + ZONES_PER_ADDRESS]:
            try:
                values.append(int(token, 16))
            except ValueError:
                values.append(None)  # "*" placeholder or junk
        new = tuple(values)
        unit = self.state.unit_state(address)
        if new != unit.zone_intensities:
            unit.zone_intensities = new
            # Still changing (mid-fade) — keep polling fast until it settles.
            self._kick_fast()

    def _parse_unit_info(self, tokens: list[str]) -> None:
        """Parse ``:mu <addr> <type> <zones> <code_rev> …``."""
        if len(tokens) < 4:
            return
        try:
            address = int(tokens[0])
            zones = int(tokens[2], 16)
        except ValueError:
            return
        if not 1 <= address <= MAX_UNITS or not 1 <= zones <= MAX_ZONES:
            return

        def _hex(index: int) -> int | None:
            if len(tokens) <= index:
                return None
            try:
                return int(tokens[index], 16)
            except ValueError:
                return None

        self.state.unit_info[address] = UnitInfo(
            address=address,
            unit_type=tokens[1],
            zones=zones,
            code_rev=tokens[3],
            units_inv=_hex(4),
            temp_mode=_hex(5),
            pll=_hex(6),
            four_q=_hex(7),
            ir_addr=_hex(8),
        )

    def _kick_fast(self, *, wake: bool = False) -> None:
        """Enter fast polling for a short window after a change.

        *wake* also interrupts the current poll sleep so the change is acted on
        immediately (e.g. a scene selected while the loop was idle/off).
        """
        self._fast_until = time.monotonic() + _FAST_WINDOW
        if wake:
            self._wake.set()

    def _is_off(self) -> bool:
        """True when every configured unit is off: scene 0 or all zones at 0."""
        return all(
            self.state.current_scene_for(config) == 0
            or all(z == 0 for z in self.state.zone_intensities_for(config))
            for config in self.unit_configs
        )

    def _poll_interval(self) -> float:
        """Choose the next poll delay based on current device activity."""
        if time.monotonic() < self._fast_until:
            # Follow a fade (including a fade to off) until it settles, even
            # though scene 0 arrives before the zones finish ramping down.
            return _POLL_FAST
        if self._is_off():
            return _POLL_OFF
        return _POLL_ON

    # -- Poll / watchdog ----------------------------------------------------

    async def _poll_loop(self) -> None:
        """Refresh scene + zone state at an interval that adapts to activity.

        Sends :G + :rzi each cycle. Polls slowly when off, fast while a change
        is settling, and at a moderate rate when on and stable. A change can
        wake the loop immediately via ``_wake``.
        """
        while self._running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval())
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            self._wake.clear()
            if not self._running:
                return
            try:
                await self.request_scene_status()
                await self.request_zone_intensities()
            except asyncio.CancelledError:
                return

    async def _watchdog_loop(self) -> None:
        """Reconnect if nothing is received for a while (decoupled from polling)."""
        while self._running:
            try:
                await asyncio.sleep(_WATCHDOG_CHECK)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            if time.monotonic() - self._last_recv > _STALE_TIMEOUT:
                _LOGGER.warning("No response for %.0fs — reconnecting", _STALE_TIMEOUT)
                await self._on_disconnect()
                return

    # -- Sending ------------------------------------------------------------

    async def send_raw(self, cmd: str) -> None:
        """Send an arbitrary command string (public, for debug tools)."""
        await self._send_command(cmd)

    async def _send_command(self, cmd: str) -> None:
        """Send an ASCII command, appending a carriage return if needed."""
        if not self._writer:
            _LOGGER.warning("Cannot send %r — not connected", cmd)
            return
        if not cmd.endswith("\r"):
            cmd += "\r"
        async with self._write_lock:
            try:
                _LOGGER.debug("send: %s", cmd.rstrip("\r"))
                self._writer.write(cmd.encode("ascii"))
                await self._writer.drain()
            except (OSError, ConnectionError) as err:
                _LOGGER.error("Send error: %s", err)
                await self._on_disconnect()

    def _notify_listeners(self) -> None:
        """Notify the state-change callback and wake any wait_for waiters."""
        if self._on_state_changed:
            self._on_state_changed()
        if self._state_waiters:
            for event, predicate in self._state_waiters:
                if predicate(self.state):
                    event.set()

    async def wait_for(
        self,
        predicate: Callable[[GrxState], bool],
        timeout: float = 5.0,
    ) -> bool:
        """Wait until predicate(state) is true, or timeout. Returns success."""
        if predicate(self.state):
            return True
        event = asyncio.Event()
        waiter = (event, predicate)
        self._state_waiters.append(waiter)
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self._state_waiters.remove(waiter)

    # -- Commands -----------------------------------------------------------

    async def query_code_rev(self) -> None:
        """Request the code revision level (``:V``) and await the response."""
        await self._send_command(":V")
        await self.wait_for(lambda s: s.code_rev is not None)

    async def request_scene_status(self) -> None:
        """Request scene status (``:G``); the reply is parsed asynchronously."""
        await self._send_command(":G")

    async def request_zone_intensities(self) -> None:
        """Request zone intensities for every configured MUX address."""
        addresses = sorted(
            {address for config in self.unit_configs for address in config.addresses}
        )
        for address in addresses:
            await self.request_zone_intensities_for(address)

    async def request_zone_intensities_for(self, address: int) -> None:
        """Request zone intensities (``:rzi``) for one MUX address."""
        if not 1 <= address <= MAX_UNITS:
            raise ValueError(f"Address must be 1-{MAX_UNITS}, got {address}")
        await self._send_command(f":rzi {address}")

    async def detect_units(self, timeout: float = _DETECT_TIMEOUT) -> list[UnitConfig]:
        """Probe the MUX link and return best-effort logical unit configs.

        ``:rmu`` is the authoritative source for model/zones/address count,
        but it is a programming-mode command and may not be supported by a
        GRX-CI-RS232.  In that case infer the zone count from the placeholder
        suffix of the ``:rzi`` response.
        """
        await self.request_scene_status()
        await self.wait_for(
            lambda state: state.scene_status is not None,
            timeout=max(timeout, 2.0),
        )
        status = self.state.scene_status
        if status is None:
            candidates = list(range(1, MAX_UNITS + 1))
        else:
            candidates = [
                index + 1 for index, scene in enumerate(status) if scene is not None
            ]

        detected: list[UnitConfig] = []
        covered: set[int] = set()
        for candidate in candidates:
            if candidate in covered:
                continue
            info = await self._query_unit_info(candidate, timeout)
            start = candidate
            if info is not None:
                index = info.multi_address_index
                if index is not None:
                    start = candidate - index
                if not 1 <= start <= MAX_UNITS:
                    continue
                zones = info.zones
                model = model_for_unit_info(info)
            else:
                zones = await self._infer_zone_count(candidate, timeout)
                if zones is None:
                    continue
                model = (
                    DEFAULT_MODEL if zones == DEFAULT_ZONE_COUNT else f"GRX-{zones:02d}"
                )
            try:
                config = UnitConfig(address=start, zones=zones, model=model)
            except ValueError:
                continue
            if any(address in covered for address in config.addresses):
                continue
            detected.append(config)
            covered.update(config.addresses)

        return sorted(detected, key=lambda config: config.address)

    async def _query_unit_info(self, address: int, timeout: float) -> UnitInfo | None:
        """Request ``:rmu`` for one address and wait for its parsed response."""
        self.state.unit_info.pop(address, None)
        await self._send_command(f":rmu {address}")
        if await self.wait_for(
            lambda state: address in state.unit_info, timeout=timeout
        ):
            return self.state.unit_info[address]
        return None

    async def _infer_zone_count(self, address: int, timeout: float) -> int | None:
        """Infer a unit's zone count from its ``:rzi`` placeholder suffix."""
        unit = self.state.unit_state(address)
        unit.zone_intensities = (None,) * ZONES_PER_ADDRESS
        await self.request_zone_intensities_for(address)
        if not await self.wait_for(
            lambda state: all(
                intensity is not None
                for intensity in state.unit_state(address).zone_intensities
            ),
            timeout=timeout,
        ):
            return None
        values = self.state.unit_state(address).zone_intensities
        for zone_count in range(1, ZONES_PER_ADDRESS):
            if all(values[index] == index for index in range(zone_count, 8)):
                return zone_count
        return ZONES_PER_ADDRESS

    def _resolve_unit(self, unit: UnitConfig | int | None) -> UnitConfig:
        """Resolve an optional unit reference, defaulting to the primary."""
        if unit is None:
            return self.unit_configs[0]
        if isinstance(unit, UnitConfig):
            return unit
        for config in self.unit_configs:
            if config.address == unit:
                return config
        raise ValueError(f"Unknown control unit address {unit}")

    async def select_scene(
        self, scene: int, unit: UnitConfig | int | None = None
    ) -> None:
        """Select a scene (0=Off, 1-16) on a logical control unit."""
        if not MIN_SCENE <= scene <= MAX_SCENE:
            raise ValueError(f"Scene must be {MIN_SCENE}-{MAX_SCENE}, got {scene}")
        config = self._resolve_unit(unit)
        addresses = "".join(str(address) for address in config.addresses)
        await self._send_command(f":A{scene_to_char(scene)}{addresses}")
        # Optimistic update; the unsolicited :ss confirm + next poll reconcile.
        for address in config.addresses:
            address_state = self.state.unit_state(address)
            address_state.unit_missing = False
            address_state.current_scene = scene
        self._notify_listeners()
        # The preset re-levels the zones — poll fast until they settle.
        self._kick_fast(wake=True)

    async def set_zone_intensity(
        self,
        zone: int,
        intensity: int,
        fade: int = 1,
        unit: UnitConfig | int | None = None,
    ) -> None:
        """Set one zone's intensity (0-7Fh) with a fade time in seconds (0-59).

        Zones are numbered across the whole logical unit, so zone 9 on a
        16-zone unit is sent to the second MUX address.  Leaves other zones
        unchanged (``*``). The unit reports the resulting state as the
        "manual" scene (17).
        """
        config = self._resolve_unit(unit)
        address = config.address_for_zone(zone)
        zone_index = config.zone_index(zone)
        if not 0 <= intensity <= MAX_INTENSITY:
            raise ValueError(f"Intensity must be 0-{MAX_INTENSITY}, got {intensity}")
        fade = max(0, min(fade, 59))
        # A single-address unit keeps the original command shape (one value per
        # zone); multi-address units use all eight positions in the block.
        value_count = config.zones if config.address_count == 1 else ZONES_PER_ADDRESS
        values = ["*"] * value_count
        values[zone_index] = format(intensity, "X")
        await self._send_command(f":szi {address} {fade:X} {' '.join(values)}")
        # Optimistic update; hold it through the fade we just commanded so the
        # slider doesn't chase the GRX's mid-fade reads (the poll reconciles
        # once the window passes).
        address_state = self.state.unit_state(address)
        zones = list(address_state.zone_intensities)
        zones[zone_index] = intensity
        address_state.zone_intensities = tuple(zones)
        address_state.current_scene = SCENE_MANUAL
        address_state.unit_missing = False
        self._notify_listeners()
        self._suppress_zone_reads_until = time.monotonic() + fade + 2
