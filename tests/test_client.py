# pyright: reportPrivateUsage=false
"""Tests for the Lutron GrafikEye GRX client parser and scene mapping."""

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from custom_components.lutron_grx.client import (
    _POLL_FAST,
    _POLL_OFF,
    _POLL_ON,
    GrxState,
    LutronGrxClient,
)
from custom_components.lutron_grx.const import (
    MAX_INTENSITY,
    SCENE_MANUAL,
    SCENE_OPTIONS,
    ZONE_COUNT,
)
from custom_components.lutron_grx.convert import (
    brightness_to_intensity,
    char_to_scene,
    intensity_to_brightness,
    intensity_to_percent,
    percent_to_intensity,
    scene_to_char,
)


def _make_client() -> LutronGrxClient:
    return LutronGrxClient("localhost", 0)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Scene mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scene", "char"),
    [(0, "0"), (1, "1"), (9, "9"), (10, "A"), (16, "G")],
)
def test_scene_to_char(scene: int, char: str) -> None:
    assert scene_to_char(scene) == char


@pytest.mark.parametrize("scene", [-1, 17, 100])
def test_scene_to_char_out_of_range(scene: int) -> None:
    with pytest.raises(ValueError, match="Scene must be"):
        scene_to_char(scene)


@pytest.mark.parametrize(
    ("char", "scene"),
    [("0", 0), ("5", 5), ("9", 9), ("A", 10), ("G", 16), ("H", 17)],
)
def test_char_to_scene(char: str, scene: int) -> None:
    assert char_to_scene(char) == scene


@pytest.mark.parametrize("char", ["M", "Z", "", "12"])
def test_char_to_scene_invalid(char: str) -> None:
    assert char_to_scene(char) is None


def test_manual_scene_not_selectable() -> None:
    # The scene select shows no option when the unit is in manual (H) mode.
    assert str(SCENE_MANUAL) not in SCENE_OPTIONS


def test_scene_char_round_trip() -> None:
    for n in range(17):
        assert char_to_scene(scene_to_char(n)) == n


# ---------------------------------------------------------------------------
# select_scene command framing
# ---------------------------------------------------------------------------


def _capture_sends(
    monkeypatch: pytest.MonkeyPatch, client: LutronGrxClient
) -> list[str]:
    sent: list[str] = []

    async def _fake_send(cmd: str) -> None:
        sent.append(cmd)

    monkeypatch.setattr(client, "_send_command", _fake_send)
    return sent


@pytest.mark.parametrize(
    ("scene", "expected"),
    [(0, ":A01"), (1, ":A11"), (5, ":A51"), (16, ":AG1")],
)
def test_select_scene_command(
    monkeypatch: pytest.MonkeyPatch, scene: int, expected: str
) -> None:
    client = _make_client()
    sent = _capture_sends(monkeypatch, client)
    _run(client.select_scene(scene))
    assert sent == [expected]
    assert client.state.current_scene == scene


@pytest.mark.parametrize("scene", [-1, 17])
def test_select_scene_out_of_range(monkeypatch: pytest.MonkeyPatch, scene: int) -> None:
    client = _make_client()
    _capture_sends(monkeypatch, client)
    with pytest.raises(ValueError, match="Scene must be"):
        _run(client.select_scene(scene))


# ---------------------------------------------------------------------------
# Zone intensity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("percent", "intensity"),
    [(0, 0x00), (1, 0x02), (25, 0x20), (50, 0x40), (51, 0x42), (99, 0x7F), (100, 0x7F)],
)
def test_percent_to_intensity_matches_table(percent: int, intensity: int) -> None:
    assert percent_to_intensity(percent) == intensity


@pytest.mark.parametrize(
    ("intensity", "percent"),
    [(0x00, 0), (0x20, 25), (0x40, 50), (0x42, 51), (0x7E, 98), (0x7F, 100)],
)
def test_intensity_to_percent_matches_table(intensity: int, percent: int) -> None:
    assert intensity_to_percent(intensity) == percent


def test_percent_round_trips_through_intensity() -> None:
    # 0-98% map to a table intensity and back; full anchors to 0x7F (100%).
    for pct in range(99):
        assert intensity_to_percent(percent_to_intensity(pct)) == pct
    assert intensity_to_percent(percent_to_intensity(100)) == 100


def test_grafikeye_50_percent_shows_50_in_ha() -> None:
    # Regression: 0x40 (the GrafikEye's 50%) must render as 50% in HA, not 51%.
    assert round(intensity_to_brightness(0x40) / 255 * 100) == 50


def test_brightness_intensity_endpoints() -> None:
    assert intensity_to_brightness(0) == 0
    assert intensity_to_brightness(MAX_INTENSITY) == 255  # full -> 100%
    assert brightness_to_intensity(0) == 0
    assert brightness_to_intensity(255) == MAX_INTENSITY


@pytest.mark.parametrize(
    ("zone", "intensity", "expected"),
    [
        (1, 0x00, ":szi 1 1 0 * * *"),
        (2, 0x40, ":szi 1 1 * 40 * *"),
        (4, MAX_INTENSITY, ":szi 1 1 * * * 7F"),
    ],
)
def test_set_zone_intensity_command(
    monkeypatch: pytest.MonkeyPatch, zone: int, intensity: int, expected: str
) -> None:
    client = _make_client()
    sent = _capture_sends(monkeypatch, client)
    _run(client.set_zone_intensity(zone, intensity, fade=1))
    assert sent == [expected]
    assert client.state.zone_intensities[zone - 1] == intensity
    assert client.state.current_scene == SCENE_MANUAL


@pytest.mark.parametrize(("zone", "intensity"), [(0, 10), (5, 10), (1, -1), (1, 128)])
def test_set_zone_intensity_out_of_range(
    monkeypatch: pytest.MonkeyPatch, zone: int, intensity: int
) -> None:
    client = _make_client()
    _capture_sends(monkeypatch, client)
    with pytest.raises(ValueError, match="must be"):
        _run(client.set_zone_intensity(zone, intensity))


def test_commanded_zone_holds_through_fade(monkeypatch: pytest.MonkeyPatch) -> None:
    # After we set a zone, mid-fade reads are ignored so the slider holds.
    client = _make_client()
    _capture_sends(monkeypatch, client)
    _run(client.set_zone_intensity(1, 0x40, fade=1))
    client._on_readline("~:zi 1 20 7F 7F 7F 4 5 6 7 1 OK")  # mid-fade 0x20
    assert client.state.zone_intensities[0] == 0x40  # held, not 0x20


def test_zone_reads_resume_after_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client()
    _capture_sends(monkeypatch, client)
    _run(client.set_zone_intensity(1, 0x40, fade=1))
    client._suppress_zone_reads_until = 0.0  # window elapsed
    client._on_readline("~:zi 1 7F 7F 7F 7F 4 5 6 7 1 OK")
    assert client.state.zone_intensities[0] == 0x7F


def test_request_zone_intensities_command(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client()
    sent = _capture_sends(monkeypatch, client)
    _run(client.request_zone_intensities())
    assert sent == [":rzi 1"]


def test_parse_zone_intensities() -> None:
    client = _make_client()
    # Zones 1-4 are 0/7F/7F/7F; positions 5-8 are existence placeholders.
    client._on_readline("~:zi 1 0 7F 7F 7F 4 5 6 7 1 OK")
    assert client.state.zone_intensities == (0, 127, 127, 127)
    assert len(client.state.zone_intensities) == ZONE_COUNT


def test_parse_zone_intensities_truncated() -> None:
    client = _make_client()
    client._on_readline("~:zi 1 20")  # too few — must not raise
    assert client.state.zone_intensities == (None,) * ZONE_COUNT


def test_parse_manual_scene() -> None:
    client = _make_client()
    client._on_readline("~:ss HMMMMMMM 1 OK")
    assert client.state.current_scene == SCENE_MANUAL


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_code_rev() -> None:
    client = _make_client()
    client._on_readline("~:v 5 4 0 1 OK")
    assert client.state.code_rev == "5.4"
    assert client.state.model == "0"


def test_parse_code_rev_no_model() -> None:
    client = _make_client()
    client._on_readline("~:v 5 4")
    assert client.state.code_rev == "5.4"
    assert client.state.model is None


@pytest.mark.parametrize(
    ("line", "scene"),
    [
        ("~:ss 2MMMMMMM 1 OK", 2),  # solicited (:G response) with ack
        (":ss 1MMMMMMM", 1),  # unsolicited (DIP 7), no ~, no ack
        (":ss 0MMMMMMM", 0),  # Off
        ("~:ss GMMMMMMM 1 OK", 16),
    ],
)
def test_parse_scene_status(line: str, scene: int) -> None:
    client = _make_client()
    client._on_readline(line)
    assert client.state.current_scene == scene
    assert client.state.unit_missing is False


def test_parse_scene_status_unit_missing() -> None:
    client = _make_client()
    client._on_readline("~:ss MMMMMMMM 1 OK")
    assert client.state.unit_missing is True
    assert client.state.current_scene is None


def test_parse_scene_status_truncated() -> None:
    client = _make_client()
    client._on_readline("~:ss")  # no field — must not raise
    assert client.state.current_scene is None


@pytest.mark.parametrize("line", ["~1 OK", "~ 3 OK"])
def test_parse_bare_ack(line: str) -> None:
    client = _make_client()
    client._on_readline(line)
    assert client.state.current_scene is None
    assert client.state.code_rev is None


@pytest.mark.parametrize("line", ["~ERROR #2 1 OK", "~ERROR #24 0 OK"])
def test_parse_error(line: str, caplog: pytest.LogCaptureFixture) -> None:
    client = _make_client()
    with caplog.at_level("WARNING"):
        client._on_readline(line)
    assert "error" in caplog.text.lower()
    assert client.state.current_scene is None


@pytest.mark.parametrize("line", ["xyz", "~~~", ":A11", ""])
def test_parse_junk_ignored(line: str) -> None:
    client = _make_client()
    client._on_readline(line)  # must not raise
    assert client.state.current_scene is None


def test_state_change_fires_callback() -> None:
    client = _make_client()
    calls: list[int] = []
    client._on_state_changed = lambda: calls.append(1)
    client._on_readline(":ss 3MMMMMMM")
    assert calls == [1]
    # An unparsed line should not notify listeners.
    calls.clear()
    client._on_readline("garbage")
    assert calls == []


# ---------------------------------------------------------------------------
# wait_for
# ---------------------------------------------------------------------------


def test_wait_for_resolves_on_parse() -> None:
    client = _make_client()

    async def _scenario() -> bool:
        async def _feed() -> None:
            await asyncio.sleep(0.01)
            client._on_readline(":ss 7MMMMMMM")

        task = asyncio.create_task(_feed())
        result = await client.wait_for(lambda s: s.current_scene == 7, timeout=1)
        await task
        return result

    assert _run(_scenario()) is True


def test_wait_for_times_out() -> None:
    client = _make_client()
    result = _run(client.wait_for(lambda s: s.code_rev is not None, timeout=0.05))
    assert result is False


def test_wait_for_immediate() -> None:
    client = _make_client()
    client.state.connected = True
    result = _run(client.wait_for(lambda s: s.connected, timeout=0.05))
    assert result is True


def test_grxstate_defaults() -> None:
    state = GrxState()
    assert state.connected is False
    assert state.current_scene is None
    assert state.unit_missing is False


# ---------------------------------------------------------------------------
# Adaptive polling
# ---------------------------------------------------------------------------


def test_is_off_scene_zero() -> None:
    client = _make_client()
    client.state.current_scene = 0
    assert client._is_off() is True


def test_is_off_all_zones_zero() -> None:
    client = _make_client()
    client.state.current_scene = 5  # scene says on, but every zone is 0
    client.state.zone_intensities = (0, 0, 0, 0)
    assert client._is_off() is True


def test_not_off_when_a_zone_is_lit() -> None:
    client = _make_client()
    client.state.current_scene = 5
    client.state.zone_intensities = (0, 127, 0, 0)
    assert client._is_off() is False


def test_poll_interval_off() -> None:
    client = _make_client()
    client.state.current_scene = 0
    assert client._poll_interval() == _POLL_OFF


def test_poll_interval_fast_while_settling() -> None:
    client = _make_client()
    client.state.current_scene = 5
    client.state.zone_intensities = (127, 127, 127, 127)
    client._kick_fast()
    assert client._poll_interval() == _POLL_FAST


def test_poll_interval_on_stable() -> None:
    client = _make_client()
    client.state.current_scene = 5
    client.state.zone_intensities = (127, 127, 127, 127)
    client._fast_until = 0.0  # window expired
    assert client._poll_interval() == _POLL_ON


def test_poll_interval_fast_when_fading_to_off() -> None:
    # Turning off emits scene 0 while the zones are still fading; the fast
    # window must win over the off cadence so we follow them down to 0.
    client = _make_client()
    client.state.current_scene = 0
    client._kick_fast()
    assert client._poll_interval() == _POLL_FAST


def test_kick_fast_wake_sets_event() -> None:
    client = _make_client()
    client._kick_fast(wake=True)
    assert client._wake.is_set()


def test_kick_fast_no_wake() -> None:
    client = _make_client()
    client._kick_fast()
    assert client._wake.is_set() is False
