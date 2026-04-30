"""Pydantic schemas for the DP800A REST API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, conint


Channel = conint(ge=1, le=3)
Slot = conint(ge=1, le=10)


class ConnectRequest(BaseModel):
    resource: str = Field(..., description="VISA resource string, e.g. TCPIP0::192.168.1.50::INSTR")


class ConnectResponse(BaseModel):
    idn: str
    resource: str


class ApplyRequest(BaseModel):
    voltage: float = Field(..., ge=0)
    current: float = Field(..., ge=0)


class OutputRequest(BaseModel):
    on: bool
    confirm: bool = False


class ProtectionRequest(BaseModel):
    value: float = Field(..., ge=0)
    enabled: bool = True


class TrackingRequest(BaseModel):
    on: bool


class MemoryRequest(BaseModel):
    slot: Slot  # type: ignore[valid-type]


class RawScpiRequest(BaseModel):
    scpi: str
    expect_response: bool = False


class RawScpiResponse(BaseModel):
    response: Optional[str] = None


class ChannelLimits(BaseModel):
    max_voltage: float = Field(30.0, ge=0)
    max_current: float = Field(3.0, ge=0)
    max_ovp: float = Field(33.0, ge=0)
    max_ocp: float = Field(3.30, ge=0)
    min_ovp: float = Field(0.001, ge=0)  # hardware minimum: 1 mV
    min_ocp: float = Field(0.001, ge=0)  # hardware minimum: 1 mA


class AppConfig(BaseModel):
    last_resource: Optional[str] = None
    limits: dict[str, ChannelLimits] = Field(
        default_factory=lambda: {
            "1": ChannelLimits(max_voltage=30.0, max_current=3.0, max_ovp=33.0, max_ocp=3.30),
            "2": ChannelLimits(max_voltage=30.0, max_current=3.0, max_ovp=33.0, max_ocp=3.30),
            "3": ChannelLimits(max_voltage=5.0,  max_current=3.0, max_ovp=5.5,  max_ocp=3.30),
        }
    )
    raw_scpi_enabled: bool = False


class Measurement(BaseModel):
    voltage: float
    current: float
    power: float


class ChannelSnapshot(BaseModel):
    channel: int
    output_on: bool
    set_voltage: float
    set_current: float
    measurement: Measurement
    ovp_value: float
    ovp_enabled: bool
    ocp_value: float
    ocp_enabled: bool


class StatusSnapshot(BaseModel):
    connected: bool
    resource: Optional[str] = None
    idn: Optional[str] = None
    tracking_on: bool = False
    channels: list[ChannelSnapshot] = Field(default_factory=list)


class DiscoveredDevice(BaseModel):
    name: str
    host: str
    port: int
    resource: str
