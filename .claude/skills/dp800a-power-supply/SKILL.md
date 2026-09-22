---
name: dp800a-power-supply
description: Control the Rigol DP832A bench power supply over the network through the `dp800a` MCP server — discover/connect, set voltage & current, configure OVP/OCP, enable/disable outputs, read live measurements, save/recall memory slots. Use whenever the user wants to power something up or down, set or change a voltage/current, turn a channel output on or off, check what the supply is doing, or mentions the DP800A / DP832A / bench PSU / lab power supply / LXI supply.
---

# DP800A / DP832A power supply control

Drives a real Rigol DP832A on the bench. **These are physical actions on live
hardware** — read the Safety rules before enabling any output.

## Architecture (why calls sometimes "go nowhere")

```
your session ──MCP(HTTP)──► dp800a-mcp (VM:8000) ──REST──► dp800a-web (VM:8765) ──PyVISA──► DP832A
                                                              (owns the ONE VISA session)
```

The MCP server holds no instrument state. A single VISA session lives in the
`dp800a-web` backend on the VM, shared by every client (MCP sessions and the
browser GUI alike). Consequences:

- **The connection is global.** If any client connected earlier, you are already
  connected — don't reconnect blindly. Check `dp800a_status` first.
- Connection **persists** until someone disconnects or `dp800a-web` restarts.
  There is no auto-reconnect on service start or VM reboot.
- If `dp800a_status` shows `connected: false`, instrument calls fail with
  *"Not connected to any instrument"*. `dp800a_status` and `dp800a_discover`
  still work while disconnected.

## Setup (once per machine)

```bash
claude mcp add --transport http --scope user dp800a http://10.0.0.36:8000/mcp/
```

Trailing slash on `/mcp/` is required. Use the **tailnet IP or MagicDNS name**
instead of `10.0.0.36` to have one registration that works both on the office
LAN and remotely over Tailscale.

## Standard opening move

1. `dp800a_status` — see whether it's connected and what each channel is doing.
2. If `connected: false` → `dp800a_discover`, then `dp800a_connect(resource)`.

**Discovery is racy.** It tries mDNS first, then a time-bounded ARP/subnet probe.
At the default `timeout=2.0` it often returns `[]` even though the supply is
online. **Use `timeout=8`**, and retry once before concluding nothing is there.
Never pass an empty resource to `dp800a_connect` — it fails with a VISA parse
error.

Known bench unit (as of 2026-09): **RIGOL DP832A, serial DP8B233902071**, at
`10.0.1.49`. Discovery returns either form depending on which path answered —
both work:

- `TCPIP0::10.0.1.49::INSTR` (mDNS / VXI-11)
- `TCPIP0::10.0.1.49::5555::SOCKET` (SCPI-RAW fallback)

If discovery keeps failing but you know the address, connect to it directly.

## Tools

| Tool | Notes |
|---|---|
| `dp800a_status` | Connection, IDN, tracking, per-channel setpoints/measurements/OVP/OCP |
| `dp800a_discover(timeout)` | Pass `timeout=8`; returns **any** LXI/SCPI instrument, not only DP800A |
| `dp800a_connect(resource)` / `dp800a_disconnect` | Shared session — see above |
| `dp800a_apply(channel, voltage, current)` | Sets setpoints; does **not** enable the output |
| `dp800a_set_output(channel, on, confirm)` | Guarded — see Safety |
| `dp800a_measure(channel)` | Live V / I / P; reads ~0 while the output is off |
| `dp800a_set_ovp(channel, value, enabled)` / `dp800a_set_ocp(...)` | Sets value **and** enable state in one call |
| `dp800a_set_tracking(on)` | CH1↔CH2 tracking |
| `dp800a_save(slot)` / `dp800a_recall(slot)` | Slots 1–10 |
| `dp800a_raw_scpi(scpi, expect_response)` | Only works if raw SCPI is enabled in Settings; off by default |

## Safety rules

1. **Never enable an output the user didn't ask to enable.** `dp800a_set_output`
   with `on=true` requires `confirm=true`; treat an explicit "turn it on" as that
   confirmation, and nothing less. Setting V/I, OVP or OCP does *not* energize
   anything — outputs stay off until separately enabled.
2. **Always ask which channel when it's unspecified.** Don't guess. The values
   usually narrow it but rarely determine it.
3. **Channel ranges** (rejected by the backend before any SCPI is sent):
   - CH1, CH2 — 30 V / 3 A, OVP ≤ 33 V, OCP ≤ 3.3 A
   - CH3 — **5 V** / 3 A, OVP ≤ 5.5 V — anything above 5 V must go on CH1/CH2
4. **Keep OVP above the voltage setpoint and OCP above the current setpoint**,
   or the channel trips the moment it's enabled. Flag it if the user's numbers
   would nuisance-trip.
5. **Read back after writing.** Follow any change with `dp800a_status` (or
   `dp800a_measure`) and report the actual values, not the requested ones.
6. Leave the output **off** when a task finishes unless the user wants it live.

## Ordinary sequence

```
dp800a_status                                  → connected?
dp800a_set_ovp(2, 15, enabled=true)            → protection first
dp800a_set_ocp(2, 2, enabled=true)
dp800a_apply(2, 14.2, 1.0)                     → setpoints, still off
dp800a_set_output(2, true, confirm=true)       → only on explicit request
dp800a_measure(2)                              → confirm it's actually sourcing
```

A floating (unloaded) channel reads its set voltage at ~0 A — that's normal, not
a fault.

## Troubleshooting

`Cannot reach DP800A web backend` → `dp800a-web` is down on the VM.
MCP tools missing entirely → the `dp800a-mcp` service is down or the URL is wrong.

On the VM (`tech@lxiserver`, `10.0.0.36`):

```bash
systemctl status dp800a-mcp --no-pager
sudo systemctl restart dp800a-mcp
curl -s http://localhost:8765/api/health
```

**Fallback without MCP.** Every tool is a thin wrapper over the REST API, so you
can drive the supply with plain HTTP if the MCP server is unavailable:

```bash
curl -s http://10.0.0.36:8765/api/status
curl -s "http://10.0.0.36:8765/api/discover?timeout=8"
curl -s -X POST http://10.0.0.36:8765/api/connect -H "Content-Type: application/json" -d '{"resource":"TCPIP0::10.0.1.49::INSTR"}'
curl -s -X POST http://10.0.0.36:8765/api/channel/2/apply -H "Content-Type: application/json" -d '{"voltage":14.2,"current":1}'
curl -s -X POST http://10.0.0.36:8765/api/channel/2/output -H "Content-Type: application/json" -d '{"on":true,"confirm":true}'
```

The same safety rules apply to the REST path — note `confirm` defaults to
`false` there, unlike the MCP tool.

Browser GUI for a human: <http://10.0.0.36:8765>
