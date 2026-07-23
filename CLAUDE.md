# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A control stack for a Rigol DP800A-series bench power supply over LXI/VXI-11 (PyVISA).
It ships two entry points that share one instrument connection:

- **`dp800a-web`** — FastAPI backend + vanilla-JS GUI (set V/I, toggle outputs, OVP/OCP,
  tracking, memory slots, live V/I charts over WebSocket).
- **`dp800a-mcp`** — MCP stdio server exposing the same operations to Claude Code / any MCP agent.

## Commands

```powershell
pip install -e ".[dev]"        # install with pytest (Python 3.11+)
pytest -q                       # run all tests
pytest tests/test_driver.py -q  # run one test file
pytest tests/test_driver.py::test_apply_and_measure_roundtrip   # run one test
dp800a-web                      # serve GUI+API at http://127.0.0.1:8765
dp800a-web --host 127.0.0.1 --port 9000   # non-default port
dp800a-mcp                      # MCP stdio server (usually launched by an MCP client, not directly)
dp800a-mcp --http --host 0.0.0.0 --port 8000   # serve MCP over Streamable HTTP (network service; endpoint /mcp/)
```

There is no configured linter or formatter, and no CI. Tests are the only gate.

## Architecture — the one thing to understand first

**A single VISA session lives in the FastAPI backend. The MCP server does NOT touch the
instrument directly — it is a thin `httpx` client to the REST API.** This is deliberate: the
browser GUI and Claude can both be active without fighting over the one serial/network session.

```
browser GUI ─┐
             ├─► FastAPI (server.py) ─► DP800ADriver (driver.py) ─► PyVISA ─► instrument
dp800a-mcp ──┘        REST + WS              (owns the only session, RLock-serialized)
  (httpx → REST)
```

Consequence: `dp800a-web` must be running before `dp800a-mcp` is useful. `mcp_server.py`
imports no driver code; it maps each MCP tool to an HTTP call against `DP800A_API_BASE`
(default `http://127.0.0.1:8765`).

## Layers

- **`driver.py`** — the *only* module that speaks SCPI/PyVISA. All public methods serialize on a
  single `threading.RLock`. Input validation and safety caps live here. PyVISA is imported
  **lazily** (inside `_open_resource`) so tests and VISA-less environments import the module fine.
  Uses the pure-Python backend: `pyvisa.ResourceManager("@py")`.
- **`server.py`** — FastAPI app built by `create_app(driver=None)`. The driver is **synchronous
  and blocking**, so every driver call is dispatched through `run_blocking` (`loop.run_in_executor`)
  to keep the event loop responsive. One driver instance is injected via a `Depends` factory.
  `DriverError` is mapped to HTTP 400 by an exception handler. `app = create_app()` at module
  scope is what uvicorn serves.
- **`mcp_server.py`** — MCP server; `TOOLS` list + a single `call_tool` dispatcher that forwards
  to REST via `_call`. Runs over **stdio by default**; `main()` also accepts `--http` to serve the
  same server over Streamable HTTP (Starlette + uvicorn) at `/mcp/` — used when running it as a
  network service on the VM. Either transport is a thin client to the REST backend; it imports no
  driver code.
- **`models.py`** — Pydantic v2 schemas shared across layers (`AppConfig`, `ChannelLimits`,
  `StatusSnapshot`, request/response bodies).
- **`config.py`** — JSON persistence at `~/.dp800a/config.json`, overridable with the
  `DP800A_CONFIG_DIR` env var. Load failures fall back to defaults silently.
- **`discovery.py`** — device discovery: mDNS (`zeroconf`) first, then a fallback that probes
  ARP-table hosts and adjacent /24–/23 subnets by opening ports 111/5555/5025 and issuing `*IDN?`.
- **`web/`** — `index.html` / `app.js` / `style.css`, plain JS (no build step). `app.js` opens a
  WebSocket to `/api/ws/telemetry`, keeps a sample ring buffer, and draws V/I charts. Packaged as
  `package-data` in `pyproject.toml`; served under `/static` with `/` returning `index.html`.

## Safety model (spread across layers — respect all three points)

1. **Per-channel V/I caps** enforced in `driver._check_limits` *before* any SCPI is written.
   Also OVP/OCP min/max bounds in `set_ovp`/`set_ocp`. Caps come from `AppConfig.limits`.
2. **Enabling an output requires `confirm=true`** — enforced in the `server.py` `/output` route
   (not in the driver). Note the MCP tool `dp800a_set_output` defaults `confirm=true`, whereas the
   REST/Pydantic default is `false`.
3. **Raw SCPI is gated** by `AppConfig.raw_scpi_enabled` (default off), enforced in
   `driver.raw_write`/`raw_query`. The GUI hides the raw console unless enabled in Settings.

## Testing conventions

- Hardware is faked by `tests/fake_visa.py::FakeVisaResource`, an in-memory SCPI interpreter.
  Inject it via the driver's `opener` parameter: `DP800ADriver(cfg, opener=fake_opener())`.
  **Never** patch PyVISA globally — the `opener` seam is the intended injection point.
- `tests/conftest.py` prepends `src/` and the tests dir to `sys.path`, so tests import both
  `from dp800a.driver import ...` and `from fake_visa import fake_opener` without installing.
- API tests use FastAPI's `TestClient` against an app built with `create_app(driver=...)` and
  isolate config by setting `DP800A_CONFIG_DIR` to a temp dir via `monkeypatch`.
- `asyncio_mode = "auto"` (pytest-asyncio) — async tests need no explicit marker.

## Behavioral notes / gotchas

- `driver.snapshot()` iterates CH1–3 and **swallows per-channel `DriverError`**, so 2-channel
  models silently skip CH3. Don't "fix" this into a hard error.
- `measure()` falls back to computing power as `V*I` if `MEAS:POWE?` isn't supported.
- The WS telemetry loop pushes a full snapshot every 0.5 s (~2 Hz); `SAMPLE_HZ` in `app.js`
  must stay in sync with that rate.
- Loopback only, no auth — the backend is meant to bind `127.0.0.1`.
- Tested against a DP832A; the driver targets any 3-channel DP800-series unit.
