import os
import tempfile
import pytest
from fastapi.testclient import TestClient

from dp800a.driver import DP800ADriver
from dp800a.models import AppConfig, ChannelLimits

from fake_visa import fake_opener


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DP800A_CONFIG_DIR", tmp)
    cfg = AppConfig(
        limits={
            "1": ChannelLimits(max_voltage=10.0, max_current=2.0),
            "2": ChannelLimits(max_voltage=30.0, max_current=2.0),
            "3": ChannelLimits(max_voltage=30.0, max_current=2.0),
        },
        raw_scpi_enabled=False,
    )
    driver = DP800ADriver(cfg, opener=fake_opener())

    # Importing after env var so config dir is honored on first load
    from dp800a.server import create_app
    app = create_app(driver=driver)
    return TestClient(app), driver


def test_health(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_connect_status_disconnect(client):
    c, _ = client
    r = c.post("/api/connect", json={"resource": "FAKE"})
    assert r.status_code == 200
    assert "RIGOL" in r.json()["idn"]
    s = c.get("/api/status").json()
    assert s["connected"] is True
    assert len(s["channels"]) == 3
    r = c.post("/api/disconnect")
    assert r.status_code == 200


def test_apply_and_measure(client):
    c, _ = client
    c.post("/api/connect", json={"resource": "FAKE"})
    r = c.post("/api/channel/1/apply", json={"voltage": 5.0, "current": 1.0})
    assert r.status_code == 200
    r = c.post("/api/channel/1/output", json={"on": True, "confirm": True})
    assert r.status_code == 200
    m = c.get("/api/channel/1/measure").json()
    assert m["voltage"] == pytest.approx(5.0)


def test_output_requires_confirm(client):
    c, _ = client
    c.post("/api/connect", json={"resource": "FAKE"})
    r = c.post("/api/channel/1/output", json={"on": True})
    assert r.status_code == 400
    assert "confirm" in r.json()["detail"].lower()


def test_safety_cap_returns_400(client):
    c, _ = client
    c.post("/api/connect", json={"resource": "FAKE"})
    r = c.post("/api/channel/1/apply", json={"voltage": 99.0, "current": 1.0})
    assert r.status_code == 400


def test_ovp_ocp(client):
    c, _ = client
    c.post("/api/connect", json={"resource": "FAKE"})
    assert c.post("/api/channel/2/ovp", json={"value": 15.0, "enabled": True}).status_code == 200
    assert c.post("/api/channel/2/ocp", json={"value": 1.0, "enabled": True}).status_code == 200
    s = c.get("/api/status").json()
    ch2 = next(x for x in s["channels"] if x["channel"] == 2)
    assert ch2["ovp_value"] == pytest.approx(15.0)
    assert ch2["ovp_enabled"] is True


def test_tracking_and_memory(client):
    c, _ = client
    c.post("/api/connect", json={"resource": "FAKE"})
    assert c.post("/api/tracking", json={"on": True}).status_code == 200
    assert c.post("/api/memory/save", json={"slot": 3}).status_code == 200
    assert c.post("/api/memory/recall", json={"slot": 3}).status_code == 200


def test_raw_disabled(client):
    c, _ = client
    c.post("/api/connect", json={"resource": "FAKE"})
    r = c.post("/api/raw", json={"scpi": "*IDN?", "expect_response": True})
    assert r.status_code == 400


def test_config_get_put(client):
    c, _ = client
    cfg = c.get("/api/config").json()
    cfg["raw_scpi_enabled"] = True
    r = c.put("/api/config", json=cfg)
    assert r.status_code == 200
    assert r.json()["raw_scpi_enabled"] is True
    c.post("/api/connect", json={"resource": "FAKE"})
    r = c.post("/api/raw", json={"scpi": "*IDN?", "expect_response": True})
    assert r.status_code == 200
    assert "RIGOL" in r.json()["response"]
