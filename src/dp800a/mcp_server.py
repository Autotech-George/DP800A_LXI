"""Stdio MCP server exposing DP800A control through the FastAPI backend."""
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

server = Server("dp800a")


def _ok(data: Any) -> list[TextContent]:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
    return [TextContent(type="text", text=text)]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"ERROR: {msg}")]


async def _call(method: str, path: str, **kwargs) -> Any:
    url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Cannot reach DP800A web backend at {API_BASE} ({e}). "
                "Start it with `dp800a-web` first."
            ) from e
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
    import argparse

    parser = argparse.ArgumentParser(description="Run the DP800A MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio, e.g. to run as a network service.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for --http (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for --http (default 8000).")
    args = parser.parse_args()

    if args.http:
        import uvicorn

        uvicorn.run(_build_http_app(), host=args.host, port=args.port)
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
