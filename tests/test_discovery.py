import socket
import types

from dp800a import discovery
from dp800a.models import DiscoveredDevice


class _FakeInfo:
    def __init__(self, addresses=None, parsed=None, port=111):
        self.addresses = addresses or []
        self._parsed = parsed
        self.port = port

    def parsed_addresses(self):
        if self._parsed is None:
            raise RuntimeError("no parsed addresses")
        return self._parsed


class _FakeZeroconf:
    def __init__(self, service_info):
        self._service_info = service_info

    def get_service_info(self, _type, _name, timeout=0):
        return self._service_info

    def close(self):
        return None


class _FakeServiceBrowser:
    def __init__(self, zc, service_type, listener):
        self._listener = listener
        self._type = service_type
        self._zc = zc
        listener.add_service(zc, service_type, f"DP800A.{service_type}")

    def cancel(self):
        return None


def test_discover_prefers_ipv4_from_parsed_addresses(monkeypatch):
    service_info = _FakeInfo(parsed=["fe80::abcd", "10.0.1.35"], port=5555)

    fake_module = types.SimpleNamespace(
        Zeroconf=lambda: _FakeZeroconf(service_info),
        ServiceBrowser=_FakeServiceBrowser,
    )
    monkeypatch.setattr(discovery, "_SERVICE_TYPES", ["_lxi._tcp.local."])
    monkeypatch.setitem(__import__("sys").modules, "zeroconf", fake_module)

    devices = discovery.discover(timeout=0)

    assert len(devices) == 1
    assert devices[0].host == "10.0.1.35"
    assert devices[0].resource == "TCPIP0::10.0.1.35::INSTR"


def test_discover_builds_socket_resource_for_scpi_raw(monkeypatch):
    ipv4_bytes = socket.inet_pton(socket.AF_INET, "10.0.1.35")
    service_info = _FakeInfo(addresses=[ipv4_bytes], parsed=None, port=5025)

    fake_module = types.SimpleNamespace(
        Zeroconf=lambda: _FakeZeroconf(service_info),
        ServiceBrowser=_FakeServiceBrowser,
    )
    monkeypatch.setattr(discovery, "_SERVICE_TYPES", ["_scpi-raw._tcp.local."])
    monkeypatch.setitem(__import__("sys").modules, "zeroconf", fake_module)

    devices = discovery.discover(timeout=0)

    assert len(devices) == 1
    assert devices[0].host == "10.0.1.35"
    assert devices[0].resource == "TCPIP0::10.0.1.35::5025::SOCKET"


def test_parse_arp_hosts_filters_noise_and_duplicates():
    arp_text = """
Interface: 10.0.1.12 --- 0x14
  Internet Address      Physical Address      Type
  10.0.1.35             00-11-22-33-44-55     dynamic
  10.0.1.35             00-11-22-33-44-55     dynamic
  127.0.0.1             ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""
    assert discovery._parse_arp_hosts(arp_text) == ["10.0.1.35"]


def test_fallback_discover_probes_reachable_arp_hosts(monkeypatch):
    monkeypatch.setattr(discovery, "_arp_hosts", lambda: ["10.0.1.35"])
    monkeypatch.setattr(
        discovery,
        "_port_open",
        lambda _host, port, timeout: port in (111, 5555),
    )

    def _fake_probe(host, timeout, *, try_instr, try_socket):
        assert host == "10.0.1.35"
        assert try_instr is True
        assert try_socket is True
        return DiscoveredDevice(
            name="DP832A",
            host=host,
            port=0,
            resource=f"TCPIP0::{host}::INSTR",
        )

    monkeypatch.setattr(discovery, "_probe_host", _fake_probe)

    devices = discovery._fallback_discover(timeout=1.0)
    assert len(devices) == 1
    assert devices[0].host == "10.0.1.35"
    assert devices[0].resource == "TCPIP0::10.0.1.35::INSTR"


def test_adjacent_subnet_candidates_include_neighboring_class_c(monkeypatch):
    monkeypatch.setattr(discovery, "_local_ipv4_hosts", lambda: ["10.0.0.66"])
    candidates = discovery._adjacent_subnet_candidates(limit=600)
    assert "10.0.1.35" in candidates
    assert "10.0.0.66" not in candidates
