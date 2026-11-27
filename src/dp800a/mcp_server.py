"""MCP server exposing DP800A control (stdio or Streamable HTTP).

Tools are served through the same REST routes the web GUI uses, reached by one
of two backends:

* ``rest``  - forward over HTTP to a running ``dp800a-web`` (``DP800A_API_BASE``).
* ``local`` - run the same FastAPI app *in-process* (via ``httpx.ASGITransport``)
  so this process owns the VISA session itself and no web GUI is needed.
* ``auto``  (default) - per call: use ``dp800a-web`` if it answers, otherwise
  the in-process app. Before handing over to a running web backend, the local
  VISA session is closed so the two never hold the instrument at once.

Either way the validation, safety caps and ``confirm`` rule are identical,
because both backends run the very same routes from ``server.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_BASE = os.environ.get("DP800A_API_BASE", "http://127.0.0.1:8765")
BACKEND_MODES = ("auto", "rest", "local")
BACKEND_MODE = os.environ.get("DP800A_MCP_BACKEND", "auto").lower()

# Tools that must not trigger an automatic reconnect to the last instrument.
_NO_AUTOCONNECT = {"dp800a_discover", "dp800a_connect", "dp800a_disconnect"}

server = Server("dp800a")


def _ok(data: Any) -> list[TextContent]:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
    return [TextContent(type="text", text=text)]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"ERROR: {msg}")]


class _LocalBackend:
    """The FastAPI app from ``server.py`` hosted in this process.

    Imported lazily so ``rest`` mode never constructs a driver. The driver
    config is re-read from disk whenever this backend is (re)selected so caps
    or raw-SCPI changes made through the GUI are honoured.
    """

    def __init__(self, driver=None) -> None:
        from .config import load_config
        from .driver import DP800ADriver
        from .server import create_app

        self.driver = driver or DP800ADriver(load_config())
        self.app = create_app(driver=self.driver)

    def refresh_config(self) -> None:
        from .config import load_config

        self.driver.update_config(load_config())

    def release(self) -> None:
        if self.driver.is_connected():
            self.driver.disconnect()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://dp800a-local",
            timeout=30.0,
        )


_local: _LocalBackend | None = None


def _get_local() -> _LocalBackend:
    global _local
    if _local is None:
        _local = _LocalBackend()
    return _local


_PROBE_TTL = 5.0  # seconds to trust a web-backend health probe
_probe_cache: tuple[float, bool] | None = None


async def _rest_alive() -> bool:
    """Is dp800a-web answering? Cached briefly so 'down' doesn't cost a probe per call."""
    global _probe_cache
    loop = asyncio.get_running_loop()
    if _probe_cache is not None and loop.time() - _probe_cache[0] < _PROBE_TTL:
        return _probe_cache[1]
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            r = await client.get(f"{API_BASE}/api/health")
        alive = r.status_code == 200 and r.json().get("ok") is True
    except Exception:
        alive = False
    _probe_cache = (loop.time(), alive)
    return alive


async def _select_backend() -> str:
    """Return "rest" or "local" for the next call, per BACKEND_MODE."""
    if BACKEND_MODE == "rest":
        return "rest"
    if BACKEND_MODE == "local":
        return "local"
    if await _rest_alive():
        # Hand the instrument over to the web backend if we were holding it.
        if _local is not None:
            await asyncio.get_running_loop().run_in_executor(None, _local.release)
        return "rest"
    return "local"


def _client_for(backend: str) -> httpx.AsyncClient:
    if backend == "local":
        local = _get_local()
        local.refresh_config()
        return local.client()
    return httpx.AsyncClient(base_url=API_BASE, timeout=10.0)


async def _request(client: httpx.AsyncClient, backend: str, method: str, path: str, **kwargs) -> Any:
    try:
        r = await client.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        if backend == "rest":
            raise RuntimeError(
                f"Cannot reach DP800A web backend at {API_BASE} ({e}). "
                "Start `dp800a-web`, or run this MCP server with --backend local/auto."
            ) from e
        raise RuntimeError(str(e)) from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"{r.status_code}: {detail}")
    if not r.content:
        return {"ok": True}
    try:
        return r.json()
    except Exception:
        return r.text


async def _ensure_connected(client: httpx.AsyncClient, backend: str) -> None:
    """Reconnect to the last-used instrument if the backend has no session.

    Best effort: failures are swallowed so the real call reports the error.
    """
    try:
        health = await _request(client, backend, "GET", "/api/health")
        if health.get("connected"):
            return
        cfg = await _request(client, backend, "GET", "/api/config")
        resource = cfg.get("last_resource")
        if resource:
            await _request(client, backend, "POST", "/api/connect", json={"resource": resource})
    except Exception:
        pass


async def _call_backend(method: str, path: str, *, autoconnect: bool = True, **kwargs) -> Any:
    backend = await _select_backend()
    async with _client_for(backend) as client:
        if autoconnect:
            await _ensure_connected(client, backend)
        return await _request(client, backend, method, path, **kwargs)


# ---- tools ---------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="dp800a_discover",
        description="Scan the local network (mDNS) for LXI instruments. Returns a list of discovered devices with VISA resource strings.",
        inputSchema={"type": "object", "properties": {"timeout": {"type": "number", "default": 2.0}}},
    ),
    Tool(
        name="dp800a_status",
        description="Get full instrument status: connection state, IDN, tracking, and per-channel setpoints, measurements, OVP/OCP.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="dp800a_connect",
        description="Connect to a DP800A by VISA resource string (e.g. TCPIP0::192.168.1.50::INSTR). Returns the *IDN? response.",
        inputSchema={
            "type": "object",
            "properties": {"resource": {"type": "string"}},
            "required": ["resource"],
        },
    ),
    Tool(
        name="dp800a_disconnect",
        description="Disconnect from the currently connected instrument.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="dp800a_apply",
        description="Set voltage and current setpoints on a channel (1-3). Equivalent to SCPI APPL CHn,V,A. Subject to per-channel safety caps.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "minimum": 1, "maximum": 3},
                "voltage": {"type": "number", "minimum": 0},
                "current": {"type": "number", "minimum": 0},
            },
            "required": ["channel", "voltage", "current"],
        },
    ),
    Tool(
        name="dp800a_set_output",
        description="Enable/disable a channel output. Enabling requires confirm=true.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "minimum": 1, "maximum": 3},
                "on": {"type": "boolean"},
                "confirm": {"type": "boolean", "default": True},
            },
            "required": ["channel", "on"],
        },
    ),
    Tool(
        name="dp800a_measure",
        description="Measure voltage, current, and power on a channel.",
        inputSchema={
            "type": "object",
            "properties": {"channel": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["channel"],
        },
    ),
    Tool(
        name="dp800a_set_ovp",
        description="Set Over-Voltage Protection value (V) and enable state on a channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "minimum": 1, "maximum": 3},
                "value": {"type": "number", "minimum": 0},
                "enabled": {"type": "boolean", "default": True},
            },
            "required": ["channel", "value"],
        },
    ),
    Tool(
        name="dp800a_set_ocp",
        description="Set Over-Current Protection value (A) and enable state on a channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "minimum": 1, "maximum": 3},
                "value": {"type": "number", "minimum": 0},
                "enabled": {"type": "boolean", "default": True},
            },
            "required": ["channel", "value"],
        },
    ),
    Tool(
        name="dp800a_set_tracking",
        description="Enable or disable tracking mode (CH1↔CH2). SCPI: OUTP:TRACK 1|0",
        inputSchema={
            "type": "object",
            "properties": {"on": {"type": "boolean"}},
            "required": ["on"],
        },
    ),
    Tool(
        name="dp800a_save",
        description="Save current settings to memory slot (1-10). SCPI: *SAV n",
        inputSchema={
            "type": "object",
            "properties": {"slot": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["slot"],
        },
    ),
    Tool(
        name="dp800a_recall",
        description="Recall settings from memory slot (1-10). SCPI: *RCL n",
        inputSchema={
            "type": "object",
            "properties": {"slot": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["slot"],
        },
    ),
    Tool(
        name="dp800a_raw_scpi",
        description=(
            "Send a raw SCPI command. Only works if raw_scpi_enabled=true in app config. "
            "Use expect_response=true for queries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scpi": {"type": "string"},
                "expect_response": {"type": "boolean", "default": False},
            },
            "required": ["scpi"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}
    auto = name not in _NO_AUTOCONNECT

    async def _call(method: str, path: str, **kwargs) -> Any:  # noqa: F811 - binds autoconnect
        return await _call_backend(method, path, autoconnect=auto, **kwargs)

    try:
        if name == "dp800a_discover":
            data = await _call("GET", "/api/discover", params={"timeout": args.get("timeout", 2.0)})
            return _ok(data)
        if name == "dp800a_status":
            return _ok(await _call("GET", "/api/status"))
        if name == "dp800a_connect":
            return _ok(await _call("POST", "/api/connect", json={"resource": args["resource"]}))
        if name == "dp800a_disconnect":
            return _ok(await _call("POST", "/api/disconnect"))
        if name == "dp800a_apply":
            ch = int(args["channel"])
            return _ok(await _call(
                "POST", f"/api/channel/{ch}/apply",
                json={"voltage": float(args["voltage"]), "current": float(args["current"])},
            ))
        if name == "dp800a_set_output":
            ch = int(args["channel"])
            return _ok(await _call(
                "POST", f"/api/channel/{ch}/output",
                json={"on": bool(args["on"]), "confirm": bool(args.get("confirm", True))},
            ))
        if name == "dp800a_measure":
            ch = int(args["channel"])
            return _ok(await _call("GET", f"/api/channel/{ch}/measure"))
        if name == "dp800a_set_ovp":
            ch = int(args["channel"])
            return _ok(await _call(
                "POST", f"/api/channel/{ch}/ovp",
                json={"value": float(args["value"]), "enabled": bool(args.get("enabled", True))},
            ))
        if name == "dp800a_set_ocp":
            ch = int(args["channel"])
            return _ok(await _call(
                "POST", f"/api/channel/{ch}/ocp",
                json={"value": float(args["value"]), "enabled": bool(args.get("enabled", True))},
            ))
        if name == "dp800a_set_tracking":
            return _ok(await _call("POST", "/api/tracking", json={"on": bool(args["on"])}))
        if name == "dp800a_save":
            return _ok(await _call("POST", "/api/memory/save", json={"slot": int(args["slot"])}))
        if name == "dp800a_recall":
            return _ok(await _call("POST", "/api/memory/recall", json={"slot": int(args["slot"])}))
        if name == "dp800a_raw_scpi":
            return _ok(await _call(
                "POST", "/api/raw",
                json={"scpi": args["scpi"], "expect_response": bool(args.get("expect_response", False))},
            ))
        return _err(f"Unknown tool: {name}")
    except Exception as e:
        return _err(str(e))


async def _run_stdio() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _build_http_app():
    """Build a Starlette app serving the MCP server over Streamable HTTP at /mcp.

    Imports Starlette here (not at module scope) so the default stdio path stays
    dependency-light. `security_settings` is left at None, which the SDK maps to
    "DNS-rebinding protection off" — the intended posture behind Tailscale/LAN,
    where the network is the trust boundary rather than the Host/Origin header.
    """
    import contextlib
    from collections.abc import AsyncIterator

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.types import Receive, Scope, Send

    session_manager = StreamableHTTPSessionManager(app=server)

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)


def main() -> None:
    global BACKEND_MODE
    import argparse

    parser = argparse.ArgumentParser(description="Run the DP800A MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio, e.g. to run as a network service.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for --http (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for --http (default 8000).")
    parser.add_argument(
        "--backend",
        choices=BACKEND_MODES,
        default=BACKEND_MODE if BACKEND_MODE in BACKEND_MODES else "auto",
        help=(
            "How to reach the instrument: 'rest' = via a running dp800a-web, "
            "'local' = own the VISA session in this process (no web GUI needed), "
            "'auto' = use dp800a-web when it is up, else local (default; env DP800A_MCP_BACKEND)."
        ),
    )
    args = parser.parse_args()
    BACKEND_MODE = args.backend

    if args.http:
        import uvicorn

        uvicorn.run(_build_http_app(), host=args.host, port=args.port)
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
