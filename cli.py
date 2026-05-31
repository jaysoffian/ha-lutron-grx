#!/usr/bin/env -S uv run --group cli python
"""CLI for testing the Lutron GrafikEye GRX client.

Reads connection settings from ``GRX_HOST`` / ``GRX_PORT`` (or --host/--port).

    ./cli.py coderev
    ./cli.py status
    ./cli.py scene 3
    ./cli.py monitor
"""

import asyncio
import contextlib
import os

import typer

from custom_components.lutron_grx.client import LutronGrxClient
from custom_components.lutron_grx.const import DEFAULT_PORT, ZONE_COUNT
from custom_components.lutron_grx.convert import (
    intensity_to_brightness,
    intensity_to_percent,
    percent_to_intensity,
)

cli = typer.Typer(help="Test client for the Lutron GrafikEye GRX-CI-RS232.")

_HOST_OPT = typer.Option(None, "--host", "-h", help="Adapter host (or $GRX_HOST).")
_PORT_OPT = typer.Option(None, "--port", "-p", help="Adapter port (or $GRX_PORT).")


def _make_client(host: str | None, port: int | None) -> LutronGrxClient:
    host = host or os.environ.get("GRX_HOST")
    if not host:
        typer.echo("No host. Set $GRX_HOST or pass --host.", err=True)
        raise SystemExit(1)
    if port is None:
        port = int(os.environ.get("GRX_PORT", str(DEFAULT_PORT)))
    return LutronGrxClient(host, port)


async def _connected(client: LutronGrxClient) -> bool:
    await client.connect()
    if not await client.wait_for(lambda s: s.connected, timeout=5):
        typer.echo("Could not connect.", err=True)
        return False
    return True


@cli.command()
def coderev(host: str | None = _HOST_OPT, port: int | None = _PORT_OPT) -> None:
    """Query the CODE REV LEVEL."""

    async def _run() -> None:
        client = _make_client(host, port)
        try:
            if not await _connected(client):
                raise SystemExit(1)
            await client.query_code_rev()
            typer.echo(f"code_rev={client.state.code_rev} model={client.state.model}")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@cli.command()
def status(host: str | None = _HOST_OPT, port: int | None = _PORT_OPT) -> None:
    """Query SCENE STATUS for the control unit."""

    async def _run() -> None:
        client = _make_client(host, port)
        try:
            if not await _connected(client):
                raise SystemExit(1)
            await client.request_scene_status()
            await client.wait_for(
                lambda s: s.current_scene is not None or s.unit_missing, timeout=5
            )
            typer.echo(
                f"scene={client.state.current_scene} "
                f"unit_missing={client.state.unit_missing}"
            )
        finally:
            await client.disconnect()

    asyncio.run(_run())


@cli.command()
def scene(
    number: int = typer.Argument(..., help="Scene 0 (Off) to 16."),
    host: str | None = _HOST_OPT,
    port: int | None = _PORT_OPT,
) -> None:
    """SELECT a scene (0=Off, 1-16)."""

    async def _run() -> None:
        client = _make_client(host, port)
        try:
            if not await _connected(client):
                raise SystemExit(1)
            await client.select_scene(number)
            # Give the device a moment to confirm via its :ss report.
            await asyncio.sleep(1)
            typer.echo(
                f"selected scene {number} (current={client.state.current_scene})"
            )
        finally:
            await client.disconnect()

    asyncio.run(_run())


@cli.command()
def zones(host: str | None = _HOST_OPT, port: int | None = _PORT_OPT) -> None:
    """Read each zone's intensity (rzi)."""

    async def _run() -> None:
        client = _make_client(host, port)
        try:
            if not await _connected(client):
                raise SystemExit(1)
            await client.request_zone_intensities()
            await client.wait_for(
                lambda s: any(i is not None for i in s.zone_intensities), timeout=5
            )
            for n, intensity in enumerate(client.state.zone_intensities, start=1):
                if intensity is None:
                    typer.echo(f"zone {n}: unknown")
                else:
                    pct = intensity_to_percent(intensity)
                    bri = intensity_to_brightness(intensity)
                    typer.echo(f"zone {n}: 0x{intensity:02X} {pct}% (brightness {bri})")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@cli.command()
def setzone(
    zone: int = typer.Argument(..., help=f"Zone 1-{ZONE_COUNT}."),
    percent: int = typer.Argument(..., help="Level 0-100 (0 = off)."),
    fade: int = typer.Option(1, help="Fade time in seconds (0-59)."),
    host: str | None = _HOST_OPT,
    port: int | None = _PORT_OPT,
) -> None:
    """Set a single zone's intensity directly (szi)."""

    async def _run() -> None:
        client = _make_client(host, port)
        try:
            if not await _connected(client):
                raise SystemExit(1)
            intensity = percent_to_intensity(max(0, min(percent, 100)))
            await client.set_zone_intensity(zone, intensity, fade)
            await asyncio.sleep(1)
            typer.echo(f"set zone {zone} to {percent}% (0x{intensity:02X})")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@cli.command()
def monitor(host: str | None = _HOST_OPT, port: int | None = _PORT_OPT) -> None:
    """Stream every line received from the device. Ctrl-C to stop."""

    async def _run() -> None:
        client = _make_client(host, port)
        client.on_raw_line = typer.echo
        try:
            if not await _connected(client):
                raise SystemExit(1)
            typer.echo("Monitoring (Ctrl-C to stop)...", err=True)
            while True:
                await asyncio.sleep(3600)
        finally:
            await client.disconnect()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    cli()
