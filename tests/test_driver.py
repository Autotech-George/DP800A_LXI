import pytest

from dp800a.driver import DP800ADriver, DriverError
from dp800a.models import AppConfig, ChannelLimits

from fake_visa import fake_opener


def make_driver(raw_enabled: bool = False) -> DP800ADriver:
    cfg = AppConfig(
        limits={
            "1": ChannelLimits(max_voltage=10.0, max_current=2.0),
            "2": ChannelLimits(max_voltage=30.0, max_current=2.0),
            "3": ChannelLimits(max_voltage=30.0, max_current=2.0),
        },
        raw_scpi_enabled=raw_enabled,
    )
    return DP800ADriver(cfg, opener=fake_opener())


def test_connect_and_idn():
    d = make_driver()
    idn = d.connect("TCPIP0::FAKE::INSTR")
    assert "RIGOL" in idn
    assert d.is_connected()
    d.disconnect()
    assert not d.is_connected()


def test_apply_and_measure_roundtrip():
    d = make_driver()
    d.connect("FAKE")
    d.apply(1, 5.0, 1.0)
    d.set_output(1, True)
    m = d.measure(1)
    assert m.voltage == pytest.approx(5.0)
    assert m.current == pytest.approx(1.0)
    assert m.power == pytest.approx(5.0)
    sv, si = d.get_setpoint(1)
    assert sv == pytest.approx(5.0) and si == pytest.approx(1.0)
    assert d.get_output(1) is True


def test_safety_caps_enforced():
    d = make_driver()
    d.connect("FAKE")
    with pytest.raises(DriverError):
        d.apply(1, 11.0, 1.0)  # exceeds max_voltage=10.0
    with pytest.raises(DriverError):
        d.apply(1, 5.0, 2.5)  # exceeds max_current=2.0


def test_invalid_channel():
    d = make_driver()
    d.connect("FAKE")
    with pytest.raises(DriverError):
        d.apply(7, 1.0, 1.0)


def test_ovp_ocp_roundtrip():
    d = make_driver()
    d.connect("FAKE")
    d.set_ovp(2, 12.5)
    d.set_ovp_enabled(2, True)
    val, en = d.get_ovp(2)
    assert val == pytest.approx(12.5) and en is True
    d.set_ocp(2, 1.5)
    d.set_ocp_enabled(2, True)
    val, en = d.get_ocp(2)
    assert val == pytest.approx(1.5) and en is True


def test_tracking_toggle():
    d = make_driver()
    d.connect("FAKE")
    d.set_tracking(True)
    assert d.get_tracking() is True
    d.set_tracking(False)
    assert d.get_tracking() is False


def test_save_recall():
    d = make_driver()
    d.connect("FAKE")
    d.apply(1, 3.3, 0.5)
    d.save(1)
    d.apply(1, 1.0, 0.1)
    d.recall(1)
    sv, si = d.get_setpoint(1)
    assert sv == pytest.approx(3.3) and si == pytest.approx(0.5)


def test_raw_scpi_disabled_by_default():
    d = make_driver(raw_enabled=False)
    d.connect("FAKE")
    with pytest.raises(DriverError):
        d.raw_write("APPL CH1,1.0,0.1")


def test_raw_scpi_enabled():
    d = make_driver(raw_enabled=True)
    d.connect("FAKE")
    d.raw_write("APPL CH1,2.0,0.2")
    sv, si = d.get_setpoint(1)
    assert sv == pytest.approx(2.0) and si == pytest.approx(0.2)


def test_snapshot_when_disconnected():
    d = make_driver()
    snap = d.snapshot()
    assert snap.connected is False
    assert snap.channels == []


def test_snapshot_when_connected():
    d = make_driver()
    d.connect("FAKE")
    d.apply(1, 5.0, 1.0)
    d.set_output(1, True)
    snap = d.snapshot()
    assert snap.connected is True
    assert len(snap.channels) == 3
    ch1 = next(c for c in snap.channels if c.channel == 1)
    assert ch1.output_on is True
    assert ch1.measurement.voltage == pytest.approx(5.0)
