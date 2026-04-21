"""FastAPI backend for the DP800A web GUI."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, save_config
from .driver import DP800ADriver, DriverError
from .discovery import discover
from .models import (
    AppConfig,
    ApplyRequest,
    ConnectRequest,
    ConnectResponse,
    DiscoveredDevice,
    MemoryRequest,
    OutputRequest,
    ProtectionRequest,
    RawScpiRequest,
    RawScpiResponse,
    StatusSnapshot,
    TrackingRequest,
)

log = logging.getLogger("dp800a.server")

WEB_DIR = Path(__file__).parent / "web"


def _driver_dep_factory(driver: DP800ADriver):
    def _dep() -> DP800ADriver:
        return driver

    return _dep


def create_app(driver: DP800ADriver | None = None) -> FastAPI:
    config = load_config()
    drv = driver or DP800ADriver(config)
    drv.update_config(config)

    app = FastAPI(title="DP800A Control", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://127.0.0.1", "*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    get_driver = _driver_dep_factory(drv)

    # ---- error handler ---------------------------------------------------
    @app.exception_handler(DriverError)
    async def _driver_error_handler(_request, exc: DriverError):  # type: ignore[override]
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---- async helpers ---------------------------------------------------
    async def run_blocking(fn, *args, **kwargs) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ---- meta ------------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "connected": drv.is_connected()}

    @app.get("/api/discover", response_model=list[DiscoveredDevice])
    async def api_discover(timeout: float = 2.0) -> list[DiscoveredDevice]:
        return await run_blocking(discover, timeout)

    # ---- connection ------------------------------------------------------
    @app.post("/api/connect", response_model=ConnectResponse)
    async def api_connect(req: ConnectRequest, d: DP800ADriver = Depends(get_driver)):
        idn = await run_blocking(d.connect, req.resource)
        cfg = d.config.model_copy(update={"last_resource": req.resource})
        d.update_config(cfg)
        await run_blocking(save_config, cfg)
        return ConnectResponse(idn=idn, resource=req.resource)

    @app.post("/api/disconnect")
    async def api_disconnect(d: DP800ADriver = Depends(get_driver)) -> dict:
        await run_blocking(d.disconnect)
        return {"ok": True}

    @app.get("/api/status", response_model=StatusSnapshot)
    async def api_status(d: DP800ADriver = Depends(get_driver)) -> StatusSnapshot:
        return await run_blocking(d.snapshot)

    # ---- config ----------------------------------------------------------
    @app.get("/api/config", response_model=AppConfig)
    async def api_get_config(d: DP800ADriver = Depends(get_driver)) -> AppConfig:
        return d.config

    @app.put("/api/config", response_model=AppConfig)
    async def api_put_config(cfg: AppConfig, d: DP800ADriver = Depends(get_driver)) -> AppConfig:
        d.update_config(cfg)
        await run_blocking(save_config, cfg)
        return cfg

    # ---- channel ops -----------------------------------------------------
    @app.post("/api/channel/{channel}/apply")
    async def api_apply(channel: int, req: ApplyRequest, d: DP800ADriver = Depends(get_driver)):
        await run_blocking(d.apply, channel, req.voltage, req.current)
        return {"ok": True}

    @app.post("/api/channel/{channel}/output")
    async def api_output(channel: int, req: OutputRequest, d: DP800ADriver = Depends(get_driver)):
        if req.on and not req.confirm:
            raise HTTPException(
                status_code=400,
                detail="Enabling output requires confirm=true",
            )
        await run_blocking(d.set_output, channel, req.on)
        return {"ok": True, "on": req.on}

    @app.get("/api/channel/{channel}/measure")
    async def api_measure(channel: int, d: DP800ADriver = Depends(get_driver)):
        m = await run_blocking(d.measure, channel)
        return m.model_dump()

    @app.post("/api/channel/{channel}/ovp")
    async def api_ovp(channel: int, req: ProtectionRequest, d: DP800ADriver = Depends(get_driver)):
        await run_blocking(d.set_ovp, channel, req.value)
        await run_blocking(d.set_ovp_enabled, channel, req.enabled)
        return {"ok": True}

    @app.post("/api/channel/{channel}/ocp")
    async def api_ocp(channel: int, req: ProtectionRequest, d: DP800ADriver = Depends(get_driver)):
        await run_blocking(d.set_ocp, channel, req.value)
        await run_blocking(d.set_ocp_enabled, channel, req.enabled)
        return {"ok": True}

    # ---- system ops ------------------------------------------------------
    @app.post("/api/tracking")
    async def api_tracking(req: TrackingRequest, d: DP800ADriver = Depends(get_driver)):
        await run_blocking(d.set_tracking, req.on)
        return {"ok": True, "on": req.on}

    @app.post("/api/memory/save")
    async def api_save(req: MemoryRequest, d: DP800ADriver = Depends(get_driver)):
        await run_blocking(d.save, req.slot)
        return {"ok": True, "slot": req.slot}

    @app.post("/api/memory/recall")
    async def api_recall(req: MemoryRequest, d: DP800ADriver = Depends(get_driver)):
        await run_blocking(d.recall, req.slot)
        return {"ok": True, "slot": req.slot}

    # ---- raw -------------------------------------------------------------
    @app.post("/api/raw", response_model=RawScpiResponse)
    async def api_raw(req: RawScpiRequest, d: DP800ADriver = Depends(get_driver)) -> RawScpiResponse:
        if req.expect_response:
            resp = await run_blocking(d.raw_query, req.scpi)
            return RawScpiResponse(response=resp)
        await run_blocking(d.raw_write, req.scpi)
        return RawScpiResponse(response=None)

    # ---- WebSocket telemetry --------------------------------------------
    @app.websocket("/api/ws/telemetry")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                snap = await run_blocking(drv.snapshot)
                await ws.send_json(snap.model_dump())
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("telemetry ws failed: %s", exc)
            with contextlib.suppress(Exception):
                await ws.close()

    # ---- static GUI ------------------------------------------------------
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the DP800A web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("dp800a.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
