"""DP800A driver wrapping PyVISA with safety + validation."""
from __future__ import annotations

import threading
from typing import Optional, Protocol

from .models import (
    AppConfig,
    ChannelSnapshot,
    Measurement,
    StatusSnapshot,
)


class DriverError(RuntimeError):
    """Domain error from the driver."""


class _VisaResource(Protocol):
    timeout: int
    read_termination: str
    write_termination: str

    def write(self, cmd: str) -> int: ...
    def query(self, cmd: str) -> str: ...
    def close(self) -> None: ...


def _open_resource(resource: str) -> _VisaResource:
    """Open a real PyVISA resource. Imported lazily so tests can monkeypatch."""
    import pyvisa  # local import keeps test environments without VISA happy

    rm = pyvisa.ResourceManager("@py")
    inst = rm.open_resource(resource)
    inst.timeout = 5000
    inst.read_termination = "\n"
    inst.write_termination = "\n"
    return inst  # type: ignore[return-value]


class DP800ADriver:
    """Thread-safe driver for the Rigol DP800A series.

    All public methods serialize on a single lock so concurrent REST/WS calls
    cannot interleave SCPI traffic.
    """

    def __init__(self, config: AppConfig, opener=_open_resource) -> None:
        self._config = config
        self._opener = opener
        self._inst: Optional[_VisaResource] = None
        self._resource: Optional[str] = None
        self._idn: Optional[str] = None
        self._lock = threading.RLock()

    # -- config -------------------------------------------------------------
    def update_config(self, config: AppConfig) -> None:
        with self._lock:
            self._config = config

    @property
    def config(self) -> AppConfig:
        return self._config

    # -- connection ---------------------------------------------------------
    def is_connected(self) -> bool:
        return self._inst is not None

    def connect(self, resource: str) -> str:
        with self._lock:
            if self._inst is not None:
                self.disconnect()
            try:
                inst = self._opener(resource)
            except Exception as exc:  # pragma: no cover - depends on backend
                raise DriverError(f"Failed to open VISA resource '{resource}': {exc}") from exc
            self._inst = inst
            self._resource = resource
            try:
                self._idn = self._query("*IDN?")
            except Exception as exc:
                self.disconnect()
                raise DriverError(f"Connected but *IDN? failed: {exc}") from exc
            return self._idn or ""

    def disconnect(self) -> None:
        with self._lock:
            if self._inst is not None:
                try:
                    self._inst.close()
                except Exception:
                    pass
            self._inst = None
            self._resource = None
            self._idn = None

    @property
    def resource(self) -> Optional[str]:
        return self._resource

    @property
    def idn(self) -> Optional[str]:
        return self._idn

    # -- low level ----------------------------------------------------------
    def _require(self) -> _VisaResource:
        if self._inst is None:
            raise DriverError("Not connected to any instrument")
        return self._inst

    def _write(self, cmd: str) -> None:
        with self._lock:
            inst = self._require()
            try:
                inst.write(cmd)
            except Exception as exc:
                raise DriverError(f"SCPI write failed ({cmd!r}): {exc}") from exc

    def _query(self, cmd: str) -> str:
        with self._lock:
            inst = self._require()
            try:
                return inst.query(cmd).strip()
            except Exception as exc:
                raise DriverError(f"SCPI query failed ({cmd!r}): {exc}") from exc

    @staticmethod
    def _check_channel(channel: int) -> None:
        if channel not in (1, 2, 3):
            raise DriverError(f"Invalid channel {channel}; expected 1..3")

    def _check_limits(self, channel: int, voltage: float, current: float) -> None:
        limits = self._config.limits.get(str(channel))
        if limits is None:
            return
        if voltage < 0 or voltage > limits.max_voltage:
            raise DriverError(
                f"Voltage {voltage} V exceeds configured cap {limits.max_voltage} V for CH{channel}"
            )
        if current < 0 or current > limits.max_current:
            raise DriverError(
                f"Current {current} A exceeds configured cap {limits.max_current} A for CH{channel}"
            )

    # -- high level commands -----------------------------------------------
    def identify(self) -> str:
        return self._query("*IDN?")

    def apply(self, channel: int, voltage: float, current: float) -> None:
        self._check_channel(channel)
        self._check_limits(channel, voltage, current)
        self._write(f"APPL CH{channel},{voltage:.4f},{current:.4f}")

    def set_output(self, channel: int, on: bool) -> None:
        self._check_channel(channel)
        self._write(f"OUTP CH{channel},{'ON' if on else 'OFF'}")

    def get_output(self, channel: int) -> bool:
        self._check_channel(channel)
        resp = self._query(f"OUTP? CH{channel}")
        return resp.strip().upper() in ("ON", "1")

    def get_setpoint(self, channel: int) -> tuple[float, float]:
        """Returns (set_voltage, set_current) using APPL? CH{n}."""
        self._check_channel(channel)
        resp = self._query(f"APPL? CH{channel}")
        # Possible formats:
        #   "CH1:30V/3A,5.000,1.000"
        #   "5.000,1.000"
        last = resp.split(",")
        try:
            v = float(last[-2])
            i = float(last[-1])
        except (ValueError, IndexError):
            return 0.0, 0.0
        return v, i

    def measure(self, channel: int) -> Measurement:
        self._check_channel(channel)
        v = float(self._query(f"MEAS:VOLT? CH{channel}"))
        i = float(self._query(f"MEAS:CURR? CH{channel}"))
        try:
            p = float(self._query(f"MEAS:POWE? CH{channel}"))
        except DriverError:
            p = v * i
        return Measurement(voltage=v, current=i, power=p)

    # OVP -------------------------------------------------------------------
    def set_ovp(self, channel: int, value: float) -> None:
        self._check_channel(channel)
        self._write(f"OUTP:OVP:VAL CH{channel},{value:.4f}")

    def set_ovp_enabled(self, channel: int, enabled: bool) -> None:
        self._check_channel(channel)
        self._write(f"OUTP:OVP CH{channel},{'ON' if enabled else 'OFF'}")

    def get_ovp(self, channel: int) -> tuple[float, bool]:
        self._check_channel(channel)
        try:
            value = float(self._query(f"OUTP:OVP:VAL? CH{channel}"))
        except DriverError:
            value = 0.0
        try:
            enabled = self._query(f"OUTP:OVP? CH{channel}").strip().upper() in ("ON", "1")
        except DriverError:
            enabled = False
        return value, enabled

    # OCP -------------------------------------------------------------------
    def set_ocp(self, channel: int, value: float) -> None:
        self._check_channel(channel)
        self._write(f"OUTP:OCP:VAL CH{channel},{value:.4f}")

    def set_ocp_enabled(self, channel: int, enabled: bool) -> None:
        self._check_channel(channel)
        self._write(f"OUTP:OCP CH{channel},{'ON' if enabled else 'OFF'}")

    def get_ocp(self, channel: int) -> tuple[float, bool]:
        self._check_channel(channel)
        try:
            value = float(self._query(f"OUTP:OCP:VAL? CH{channel}"))
        except DriverError:
            value = 0.0
        try:
            enabled = self._query(f"OUTP:OCP? CH{channel}").strip().upper() in ("ON", "1")
        except DriverError:
            enabled = False
        return value, enabled

    # tracking --------------------------------------------------------------
    def set_tracking(self, on: bool) -> None:
        self._write(f"OUTP:TRACK {1 if on else 0}")

    def get_tracking(self) -> bool:
        try:
            return self._query("OUTP:TRACK?").strip().upper() in ("ON", "1")
        except DriverError:
            return False

    # memory ----------------------------------------------------------------
    def save(self, slot: int) -> None:
        if not 1 <= slot <= 10:
            raise DriverError("Memory slot must be 1..10")
        self._write(f"*SAV {slot}")

    def recall(self, slot: int) -> None:
        if not 1 <= slot <= 10:
            raise DriverError("Memory slot must be 1..10")
        self._write(f"*RCL {slot}")

    # raw -------------------------------------------------------------------
    def raw_write(self, scpi: str) -> None:
        if not self._config.raw_scpi_enabled:
            raise DriverError("Raw SCPI is disabled in configuration")
        self._write(scpi)

    def raw_query(self, scpi: str) -> str:
        if not self._config.raw_scpi_enabled:
            raise DriverError("Raw SCPI is disabled in configuration")
        return self._query(scpi)

    # snapshot --------------------------------------------------------------
    def snapshot(self) -> StatusSnapshot:
        if not self.is_connected():
            return StatusSnapshot(connected=False)
        channels: list[ChannelSnapshot] = []
        for ch in (1, 2, 3):
            try:
                output_on = self.get_output(ch)
                sv, si = self.get_setpoint(ch)
                meas = self.measure(ch)
                ovp_val, ovp_en = self.get_ovp(ch)
                ocp_val, ocp_en = self.get_ocp(ch)
                channels.append(
                    ChannelSnapshot(
                        channel=ch,
                        output_on=output_on,
                        set_voltage=sv,
                        set_current=si,
                        measurement=meas,
                        ovp_value=ovp_val,
                        ovp_enabled=ovp_en,
                        ocp_value=ocp_val,
                        ocp_enabled=ocp_en,
                    )
                )
            except DriverError:
                # Channel may not exist on a 2-channel model — skip silently.
                continue
        return StatusSnapshot(
            connected=True,
            resource=self._resource,
            idn=self._idn,
            tracking_on=self.get_tracking(),
            channels=channels,
        )
