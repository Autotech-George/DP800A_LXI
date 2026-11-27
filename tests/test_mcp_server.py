"""MCP server tests: the in-process (local) backend and auto backend selection."""
import json

import pytest

from dp800a import mcp_server
from dp800a.config import save_config
from dp800a.driver import DP800ADriver
from dp800a.models import AppConfig, ChannelLimits

from fake_visa import fake_opener


def _cfg(**kw) -> AppConfig:
    return AppConfig(
        limits={
            "1": ChannelLimits(max_voltage=10.0, max_current=2.0),
            "2": ChannelLimits(max_voltage=30.0, max_current=2.0),
            "3": ChannelLimits(max_voltage=30.0, max_current=2.0),
        },
        **kw,
    )


@pytest.fixture
def local(monkeypatch, tmp_path):
    """Local backend over the fake instrument; REST backend unreachable."""
    monkeypatch.setenv("DP800A_CONFIG_DIR", str(tmp_path))
    save_config(_cfg())
    backend = mcp_server._LocalBackend(driver=DP800ADriver(_cfg(), opener=fake_opener()))
    monkeypatch.setattr(mcp_server, "_local", backend)
    monkeypatch.setattr(mcp_server, "BACKEND_MODE", "auto")
    monkeypatch.setattr(mcp_server, "API_BASE", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setattr(mcp_server, "_probe_cache", None)
    return backend


async def _tool(name, **args):
    out = await mcp_server.call_tool(name, args)
    text = out[0].text
    if text.startswith("ERROR:"):
        return text
    return json.loads(text)


async def test_works_without_web_backend(local):
    r = await _tool("dp800a_connect", resource="FAKE")
    assert "RIGOL" in r["idn"]
    await _tool("dp800a_apply", channel=1, voltage=5.0, current=1.0)
    await _tool("dp800a_set_output", channel=1, on=True)
    m = await _tool("dp800a_measure", channel=1)
    assert m["voltage"] == pytest.approx(5.0)
    s = await _tool("dp800a_status")
    assert s["connected"] is True


async def test_safety_rules_apply_locally(local):
    await _tool("dp800a_connect", resource="FAKE")
    assert (await _tool("dp800a_apply", channel=1, voltage=20.0, current=1.0)).startswith("ERROR:")
    r = await _tool("dp800a_set_output", channel=1, on=True, confirm=False)
    assert r.startswith("ERROR:") and "confirm" in r
    assert (await _tool("dp800a_raw_scpi", scpi="*RST")).startswith("ERROR:")


async def test_autoconnects_to_last_resource(local):
    await _tool("dp800a_connect", resource="FAKE")  # persists last_resource
    local.driver.disconnect()
    s = await _tool("dp800a_status")
    assert s["connected"] is True


async def test_no_autoconnect_after_explicit_disconnect_call(local):
    await _tool("dp800a_connect", resource="FAKE")
    await _tool("dp800a_disconnect")
    assert local.driver.is_connected() is False


async def test_auto_releases_local_session_when_web_is_up(local, monkeypatch):
    await _tool("dp800a_connect", resource="FAKE")
    assert local.driver.is_connected()

    async def alive():
        return True

    monkeypatch.setattr(mcp_server, "_rest_alive", alive)
    assert await mcp_server._select_backend() == "rest"
    assert local.driver.is_connected() is False


async def test_rest_mode_reports_missing_web_backend(local, monkeypatch):
    monkeypatch.setattr(mcp_server, "BACKEND_MODE", "rest")
    r = await _tool("dp800a_status")
    assert r.startswith("ERROR:") and "Cannot reach" in r
