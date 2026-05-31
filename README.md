# Lutron GrafikEye GRX Integration for Home Assistant

This integration controls a **Lutron GrafikEye 3504** via a **GRX-CI-RS232** control
interface bridged to the network by a TCP-to-RS232 adapter.

## Features

- Select/report scene (0-16).
- Set/report zone intensity (zones 1-4).
- Report GRX-CI-RS232 code revision.

## Requirements

- A GRX-CI-RS232 connected to a single GRAFIK Eye 3000/4000-series control unit
  at MUX address **1**.
- The interface reachable over TCP via a serial-to-network adapter
  (9600 8N1 on the serial side; the adapter handles framing).
- Recommended DIP switches: **7 ON** (report scene changes).

## Installation

### HACS

1. Install [HACS](https://hacs.xyz), then either:
   1. [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jaysoffian&repository=ha-lutron-grx&category=integration)

   Or:

   1. Open HACS → Integrations
   2. Open triple-dot menu ( ⠇) → Custom repositories
   3. Add this repository's URL (`https://github.com/jaysoffian/ha-lutron-grx`), category "Integration"
2. Install this integration and restart Home Assistant

### Manual

Copy `custom_components/lutron_grx` into your Home Assistant `custom_components` directory and restart.

## CLI

```console
$ ./cli.py coderev      # CODE REV LEVEL
$ ./cli.py status       # SCENE STATUS
$ ./cli.py scene 3      # SELECT SCENE 3
$ ./cli.py scene 0      # Off
$ ./cli.py monitor      # stream raw device output (Ctrl-C to stop)
```

## Development

Tooling is managed with [mise](https://mise.jdx.dev/) and [prek](https://github.com/j178/prek):

```console
$ mise install           # install python, uv, ruff, prek
$ mise x -- uv sync      # install dev + cli dependency groups
$ mise x -- prek install # install git hooks
$ mise run test          # pytest
$ mise run check         # ruff + ty + pyright + pytest (via prek)
$ mise run hassfest      # validate the manifest
```

## License

[MIT](./LICENSE)
