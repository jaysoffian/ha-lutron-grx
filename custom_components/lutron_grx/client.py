"""Async TCP client for a Lutron GrafikEye via a GRX-CI-RS232 interface.

The GRX-CI-RS232 speaks the GRAFIK Eye RS232 ASCII protocol.  Commands are
ASCII strings terminated with a carriage return; responses begin with ``~``
and end with ``<CR><LF>``.  When scene-status reporting is enabled (DIP 7),
the interface also emits unsolicited ``:ss`` lines on scene changes.

We model a single control unit hard-coded at MUX address 1.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from .const import (
    CONTROL_UNIT,
    GRX_ERRORS,
    MAX_INTENSITY,
    MAX_SCENE,
    MIN_SCENE,
    SCENE_MANUAL,
    ZONE_COUNT,
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


@dataclass
class GrxState:
    """Flat device state, updated in place by the read loop.

    Any assignment that actually changes a value emits a debug log via
    ``__setattr__``.
    """

    connected: bool = False
    code_rev: str | None = None  # e.g. "5.4"
    model: str | None = None  # model token from the :V response (e.g. "0")
    current_scene: int | None = None  # None=unknown, 0=Off, 1-16=preset, 17=manual
    unit_missing: bool = False  # control unit reported "M" (not on the link)
    # Per-zone intensities (0-7Fh); None = unknown. Index 0 = zone 1.
    zone_intensities: tuple[int | None, ...] = (None,) * ZONE_COUNT

    def __setattr__(self, name: str, value: object) -> None:
        try:
            old = object.__getattribute__(self, name)
        except AttributeError:
            pass  # Initial assignment during __init__
        else:
            if old != value and not name.startswith("_"):
                _LOGGER.debug("state: %s: %r -> %r", name, old, value)
        object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LutronGrxClient:
    """Async TCP client for a Lutron GrafikEye GRX-CI-RS232."""

    def __init__(self, host: str, port: int) -> None:
        self.state = GrxState()
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

          ``~:v 5 4 0 1 OK``  → code revision
          ``~:ss 2MMMMMMM 1 OK`` / ``:ss 1MMMMMMM`` (unsolicited) → scene status
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
        """Parse ``:ss <8 chars>`` and read our control unit (index 0)."""
        if not tokens or not tokens[0]:
            return
        char = tokens[0][0]
        if char == "M":
            self.state.unit_missing = True
            return
        scene = char_to_scene(char)
        if scene is None:
            _LOGGER.debug("Unrecognized scene char %r", char)
            return
        self.state.unit_missing = False
        changed = self.state.current_scene != scene
        self.state.current_scene = scene
        if changed:
            # A scene change re-levels the zones; poll fast until they settle.
            self._kick_fast(wake=True)

    def _parse_zone_intensities(self, tokens: list[str]) -> None:
        """Parse ``:zi <unit> <Int1> … <Int8>`` for our zones (the first four).

        Positions beyond the unit's zone count carry numeric placeholders, so
        we only read ``ZONE_COUNT`` values.
        """
        if time.monotonic() < self._suppress_zone_reads_until:
            # We commanded a fade; hold the target rather than chase mid-fade reads.
            return
        if len(tokens) < 1 + ZONE_COUNT:
            return
        values: list[int | None] = []
        for token in tokens[1 : 1 + ZONE_COUNT]:
            try:
                values.append(int(token, 16))
            except ValueError:
                values.append(None)  # "*" placeholder or junk
        new = tuple(values)
        if new != self.state.zone_intensities:
            self.state.zone_intensities = new
            # Still changing (mid-fade) — keep polling fast until it settles.
            self._kick_fast()

    def _kick_fast(self, *, wake: bool = False) -> None:
        """Enter fast polling for a short window after a change.

        *wake* also interrupts the current poll sleep so the change is acted on
        immediately (e.g. a scene selected while the loop was idle/off).
        """
        self._fast_until = time.monotonic() + _FAST_WINDOW
        if wake:
            self._wake.set()

    def _is_off(self) -> bool:
        """True when the unit is off: scene 0, or all zones at 0."""
        if self.state.current_scene == 0:
            return True
        return all(z == 0 for z in self.state.zone_intensities)

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
        """Request zone intensities (``:rzi``); parsed asynchronously."""
        await self._send_command(f":rzi {CONTROL_UNIT}")

    async def select_scene(self, scene: int) -> None:
        """Select a scene (0=Off, 1-16) on the control unit."""
        if not MIN_SCENE <= scene <= MAX_SCENE:
            raise ValueError(f"Scene must be {MIN_SCENE}-{MAX_SCENE}, got {scene}")
        await self._send_command(f":A{scene_to_char(scene)}{CONTROL_UNIT}")
        # Optimistic update; the unsolicited :ss confirm + next poll reconcile.
        self.state.unit_missing = False
        self.state.current_scene = scene
        self._notify_listeners()
        # The preset re-levels the zones — poll fast until they settle.
        self._kick_fast(wake=True)

    async def set_zone_intensity(
        self, zone: int, intensity: int, fade: int = 1
    ) -> None:
        """Set one zone's intensity (0-7Fh) with a fade time in seconds (0-59).

        Leaves the other zones unchanged (``*``). The unit reports the
        resulting state as the "manual" scene (17).
        """
        if not 1 <= zone <= ZONE_COUNT:
            raise ValueError(f"Zone must be 1-{ZONE_COUNT}, got {zone}")
        if not 0 <= intensity <= MAX_INTENSITY:
            raise ValueError(f"Intensity must be 0-{MAX_INTENSITY}, got {intensity}")
        fade = max(0, min(fade, 59))
        values = ["*"] * ZONE_COUNT
        values[zone - 1] = format(intensity, "X")
        await self._send_command(f":szi {CONTROL_UNIT} {fade:X} {' '.join(values)}")
        # Optimistic update; hold it through the fade we just commanded so the
        # slider doesn't chase the GRX's mid-fade reads (the poll reconciles
        # once the window passes).
        zones = list(self.state.zone_intensities)
        zones[zone - 1] = intensity
        self.state.zone_intensities = tuple(zones)
        self.state.current_scene = SCENE_MANUAL
        self.state.unit_missing = False
        self._notify_listeners()
        self._suppress_zone_reads_until = time.monotonic() + fade + 2
