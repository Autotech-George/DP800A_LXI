"""LXI / VXI-11 device discovery via mDNS with ARP/VISA fallback."""
from __future__ import annotations

import contextlib
import concurrent.futures
import ipaddress
import re
import socket
import subprocess
import time
from typing import List

from .models import DiscoveredDevice

_SERVICE_TYPES = ["_lxi._tcp.local.", "_scpi-raw._tcp.local.", "_vxi-11._tcp.local."]
_SCPI_RAW_PORTS = (5555, 5025)


def _extract_hosts(info) -> list[str]:
    """Return parsed host addresses from a zeroconf ServiceInfo object."""
    hosts: list[str] = []

    parsed_addresses = getattr(info, "parsed_addresses", None)
    if callable(parsed_addresses):
        try:
            hosts.extend([h for h in parsed_addresses() if h])
        except Exception:
            pass

    if hosts:
        return hosts

    for raw in getattr(info, "addresses", []) or []:
        try:
            if len(raw) == 4:
                hosts.append(socket.inet_ntop(socket.AF_INET, raw))
            elif len(raw) == 16:
                hosts.append(socket.inet_ntop(socket.AF_INET6, raw))
        except Exception:
            continue
    return hosts


def _pick_host(hosts: list[str]) -> str | None:
    """Prefer a routable IPv4 host when available; otherwise use first host."""
    if not hosts:
        return None

    for host in hosts:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv4Address) and not (addr.is_loopback or addr.is_link_local):
            return host

    for host in hosts:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            continue
        if not (addr.is_loopback or addr.is_link_local):
            return host

    return hosts[0]


def _resource_for(type_: str, host: str, port: int) -> str:
    """Build a VISA resource string from the discovered service type."""
    if type_ == "_scpi-raw._tcp.local.":
        scpi_port = port or 5025
        return f"TCPIP0::{host}::{scpi_port}::SOCKET"
    return f"TCPIP0::{host}::INSTR"


def _parse_arp_hosts(text: str) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        host = None

        # Windows: "10.0.1.35  00-11-22-33-44-55  dynamic"
        m = re.match(
            r"^(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F-]{17}|[0-9a-fA-F:]{17})\s+\S+",
            line,
        )
        if m:
            host = m.group(1)

        # macOS/BSD: "? (10.0.1.35) at 00:11:22:33:44:55 ..."
        if host is None:
            m = re.search(
                r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F-]{17}|[0-9a-fA-F:]{17})",
                line,
            )
            if m:
                host = m.group(1)

        if host is None:
            continue

        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            continue
        if not isinstance(addr, ipaddress.IPv4Address):
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            continue
        if host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts


def _arp_hosts() -> list[str]:
    try:
        proc = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception:
        return []
    return _parse_arp_hosts(proc.stdout)


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _name_from_idn(idn: str, host: str) -> str:
    parts = [p.strip() for p in idn.split(",")]
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    if parts and parts[0]:
        return parts[0]
    return host


def _probe_host(host: str, timeout: float, *, try_instr: bool, try_socket: bool) -> DiscoveredDevice | None:
    try:
        import pyvisa
    except Exception:
        return None

    resources: list[tuple[str, int, bool]] = []
    if try_instr:
        resources.append((f"TCPIP0::{host}::INSTR", 0, False))
    if try_socket:
        for port in _SCPI_RAW_PORTS:
            resources.append((f"TCPIP0::{host}::{port}::SOCKET", port, True))
    if not resources:
        return None

    rm = None
    try:
        rm = pyvisa.ResourceManager("@py")
        timeout_ms = max(200, int(timeout * 1000))
        for resource, port, is_socket in resources:
            inst = None
            try:
                inst = rm.open_resource(resource)
                inst.timeout = timeout_ms
                if is_socket:
                    inst.read_termination = "\n"
                    inst.write_termination = "\n"
                idn = inst.query("*IDN?").strip()
                if not idn:
                    continue
                return DiscoveredDevice(
                    name=_name_from_idn(idn, host),
                    host=host,
                    port=port,
                    resource=resource,
                )
            except Exception:
                continue
            finally:
                if inst is not None:
                    with contextlib.suppress(Exception):
                        inst.close()
    finally:
        if rm is not None:
            with contextlib.suppress(Exception):
                rm.close()

    return None


def _local_ipv4_hosts() -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()

    # Best-effort local outbound address for the active route.
    with contextlib.suppress(Exception):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
            if host not in seen:
                seen.add(host)
                hosts.append(host)

    with contextlib.suppress(Exception):
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            host = info[4][0]
            if host in seen:
                continue
            seen.add(host)
            hosts.append(host)

    filtered: list[str] = []
    for host in hosts:
        with contextlib.suppress(ValueError):
            addr = ipaddress.ip_address(host)
            if isinstance(addr, ipaddress.IPv4Address) and addr.is_private and not addr.is_loopback:
                filtered.append(host)
    return filtered


def _adjacent_subnet_candidates(limit: int = 1024) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    for local_host in _local_ipv4_hosts():
        a, b, c, _d = [int(x) for x in local_host.split(".")]
        nets = [
            ipaddress.ip_network(f"{a}.{b}.{c}.0/24", strict=False),
            ipaddress.ip_network(f"{a}.{b}.{(c & ~1)}.0/23", strict=False),
        ]
        for net in nets:
            for addr in net.hosts():
                host = str(addr)
                if host == local_host or host in seen:
                    continue
                seen.add(host)
                candidates.append(host)
                if len(candidates) >= limit:
                    return candidates
    return candidates


def _probe_candidates(candidates: list[str], timeout: float, seen_hosts: set[str]) -> list[DiscoveredDevice]:
    if not candidates:
        return []

    found: list[DiscoveredDevice] = []
    connect_timeout = min(0.12, max(0.03, timeout / 20.0))
    query_timeout = min(0.8, max(0.25, timeout / 2.0))

    def _worker(host: str) -> DiscoveredDevice | None:
        if host in seen_hosts:
            return None
        has_instr = _port_open(host, 111, timeout=connect_timeout)
        has_socket = any(_port_open(host, p, timeout=connect_timeout) for p in _SCPI_RAW_PORTS)
        if not (has_instr or has_socket):
            return None
        return _probe_host(host, timeout=query_timeout, try_instr=has_instr, try_socket=has_socket)

    max_workers = min(64, max(4, len(candidates)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_worker, host): host for host in candidates}
        try:
            for fut in concurrent.futures.as_completed(future_map, timeout=max(0.5, timeout)):
                dev = fut.result()
                if dev is None:
                    continue
                if dev.host in seen_hosts:
                    continue
                seen_hosts.add(dev.host)
                found.append(dev)
        except concurrent.futures.TimeoutError:
            pass
    return found


def _fallback_discover(timeout: float, seen_hosts: set[str] | None = None) -> list[DiscoveredDevice]:
    seen_hosts = seen_hosts or set()
    candidates = [h for h in _arp_hosts() if h not in seen_hosts]
    found = _probe_candidates(candidates, timeout=max(1.0, timeout), seen_hosts=seen_hosts)
    if found:
        return found

    # mDNS can miss routed VLANs. Probe the local /24 and adjacent /24 as a bounded fallback.
    subnet_candidates = [h for h in _adjacent_subnet_candidates() if h not in seen_hosts]
    return _probe_candidates(subnet_candidates, timeout=max(1.0, timeout), seen_hosts=seen_hosts)


def _discover_mdns(timeout: float) -> list[DiscoveredDevice]:
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:
        return []

    found: dict[str, DiscoveredDevice] = {}

    class _Listener:
        def remove_service(self, *_a, **_k) -> None:  # noqa: D401
            pass

        def update_service(self, *_a, **_k) -> None:
            pass

        def add_service(self, zc, type_, name) -> None:
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if not info:
                return
            host = _pick_host(_extract_hosts(info))
            if not host:
                return
            port = info.port or 0
            label = name.split(".")[0]
            resource = _resource_for(type_, host, port)
            key = f"{label}:{host}"
            found[key] = DiscoveredDevice(
                name=label, host=host, port=port, resource=resource
            )

    zc = Zeroconf()
    listener = _Listener()
    browsers = [ServiceBrowser(zc, st, listener) for st in _SERVICE_TYPES]
    try:
        time.sleep(timeout)
    finally:
        for b in browsers:
            with contextlib.suppress(Exception):
                b.cancel()
        zc.close()
    return list(found.values())


def discover(timeout: float = 2.0) -> List[DiscoveredDevice]:
    """Browse the local network for LXI instruments. Returns a list of devices."""
    mdns_devices = _discover_mdns(timeout)
    if mdns_devices:
        return mdns_devices
    return _fallback_discover(timeout)
