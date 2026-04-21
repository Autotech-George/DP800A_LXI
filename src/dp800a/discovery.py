"""LXI / VXI-11 device discovery via mDNS (zeroconf)."""
from __future__ import annotations

import socket
from typing import List

from .models import DiscoveredDevice

_SERVICE_TYPES = ["_lxi._tcp.local.", "_scpi-raw._tcp.local.", "_vxi-11._tcp.local."]


def discover(timeout: float = 2.0) -> List[DiscoveredDevice]:
    """Browse the local network for LXI instruments. Returns a list of devices."""
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
            if not info or not info.addresses:
                return
            host = socket.inet_ntoa(info.addresses[0])
            port = info.port or 0
            label = name.split(".")[0]
            resource = f"TCPIP0::{host}::INSTR"
            found[label] = DiscoveredDevice(
                name=label, host=host, port=port, resource=resource
            )

    zc = Zeroconf()
    listener = _Listener()
    browsers = [ServiceBrowser(zc, st, listener) for st in _SERVICE_TYPES]
    try:
        import time

        time.sleep(timeout)
    finally:
        for b in browsers:
            try:
                b.cancel()
            except Exception:
                pass
        zc.close()
    return list(found.values())
