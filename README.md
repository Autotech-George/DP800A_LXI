# DP800A_LXI — Web GUI + MCP Server for Rigol DP800A

Control a Rigol DP800-series power supply over the network (LXI / VXI-11 via
PyVISA) from:

- **A web GUI** in your browser — set V/I, toggle outputs, configure OVP/OCP,
  enable tracking, save/recall memory slots, view live measurements.
- **An MCP server** — let Claude Code (or any MCP-aware agent) drive the
  instrument with the same SCPI vocabulary.

A single VISA session lives in the FastAPI backend. The MCP server is a thin
HTTP client to that backend, so the browser GUI and Claude can be active at the
same time without fighting over the instrument.

## SCPI commands wrapped

| Task                     | SCPI                       |
|--------------------------|----------------------------|
| Set voltage/current      | `APPL CH1,<V>,<A>`         |
| Enable/disable output    | `OUTP CH1,ON/OFF`          |
| Measure voltage          | `MEAS:VOLT? CH1`           |
| Measure current          | `MEAS:CURR? CH1`           |
| Measure power            | `MEAS:POWE? CH1`           |
| Set / enable OVP         | `OUTP:OVP:VAL CH1,<V>` + `OUTP:OVP CH1,ON` |
| Set / enable OCP         | `OUTP:OCP:VAL CH1,<A>` + `OUTP:OCP CH1,ON` |
| Tracking mode            | `OUTP:TRACK 1`             |
| Save / recall settings   | `*SAV n` / `*RCL n`        |

## Install

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

(or `pip install -e ".[dev]"` to also pull in `pytest`.)

## Run the web app

```powershell
dp800a-web
```

Then open <http://127.0.0.1:8765>. Click **Discover** to mDNS-scan for LXI
instruments on the LAN, pick one (or paste a VISA resource string like
`TCPIP0::192.168.1.50::INSTR`) and hit **Connect**. The IDN appears next to the
status LED, the three channel cards become live, and the bottom charts start
plotting V/I at ~2 Hz over WebSocket.

### Safety guardrails

- Each channel has a configurable **max V / max I cap** (defaults: CH1 8 V / 5 A,
  CH2/CH3 30 V / 2 A). Setpoints exceeding caps are rejected by the backend
  before any SCPI is sent.
- Turning an output **on** requires explicit confirmation (a dialog in the GUI,
  `confirm=true` in the API and MCP tool).
- The **Raw SCPI console** is hidden by default. Enable it from the ⚙ Settings
  modal if you need to send arbitrary commands.

Configuration is persisted to `~/.dp800a/config.json` (override with the
`DP800A_CONFIG_DIR` env var).

## Run the MCP server

The MCP server talks to the running web backend, so start `dp800a-web` first.

```powershell
dp800a-mcp
```

It speaks MCP over stdio, so you don't normally invoke it directly — register
it with your MCP client.

### Claude Code

```powershell
claude mcp add dp800a -- s:\George\Projects\Python\DP800A_LXI\.venv\Scripts\dp800a-mcp.exe
```

Or in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dp800a": {
      "command": "s:\\George\\Projects\\Python\\DP800A_LXI\\.venv\\Scripts\\dp800a-mcp.exe",
      "env": { "DP800A_API_BASE": "http://127.0.0.1:8765" }
    }
  }
}
```

Tools exposed:

- `dp800a_discover`, `dp800a_status`
- `dp800a_connect(resource)`, `dp800a_disconnect`
- `dp800a_apply(channel, voltage, current)`
- `dp800a_set_output(channel, on, confirm=true)`
- `dp800a_measure(channel)`
- `dp800a_set_ovp(channel, value, enabled)`
- `dp800a_set_ocp(channel, value, enabled)`
- `dp800a_set_tracking(on)`
- `dp800a_save(slot)`, `dp800a_recall(slot)`
- `dp800a_raw_scpi(scpi, expect_response)` — only works if you've enabled raw
  SCPI in Settings.

Try: *"Connect to the DP800A at 192.168.1.50, set CH1 to 3.3 V / 0.5 A, and
turn it on."*

## Project layout

```
src/dp800a/
  driver.py        # PyVISA wrapper, validation, locking
  models.py        # Pydantic schemas
  config.py        # JSON-backed persistent settings
  discovery.py     # zeroconf LXI / VXI-11 / SCPI-RAW browser
  server.py        # FastAPI app (REST + WebSocket telemetry)
  mcp_server.py    # MCP stdio server (httpx → REST)
  web/             # vanilla HTML/JS GUI
tests/             # pytest with an in-memory FakeVisaResource
```

## Tests

```powershell
pytest -q
```

20 tests cover the driver (SCPI command shape, channel validation, safety caps,
OVP/OCP, tracking, save/recall, raw-SCPI gating) and the FastAPI surface
(connect/disconnect, apply, output confirm-gate, OVP/OCP, tracking, memory,
config round-trip).

## Hardware verification checklist

1. `pytest -q` passes.
2. `dp800a-web` → open the GUI → **Discover** finds the supply, **Connect**
   shows the IDN string.
3. CH1 *Apply* 1.0 V / 0.1 A → toggle output ON → measurement reads ~1.0 V.
4. Set OVP below the applied voltage, enable OVP, turn output ON → device trips.
5. Tracking ON / OFF; save slot 1 → change settings → recall slot 1 restores.
6. Register the MCP server in Claude Code and ask it to set CH2 to 5 V / 0.2 A
   and enable the output.

## Notes

- Loopback only. The backend binds to `127.0.0.1`. There is no auth.
- Tested with a DP832A; the driver works for any DP800-series unit (3-channel
  models). 2-channel variants will silently skip CH3 in the snapshot.
- For a non-default port: `dp800a-web --host 127.0.0.1 --port 9000` — and set
  `DP800A_API_BASE=http://127.0.0.1:9000` for the MCP server.
