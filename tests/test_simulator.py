import numpy as np
import pytest

from labdaq.protocol import (
    AcquisitionConfig, Command, ProtocolError, COMMAND_OUT, COMMAND_IN,
    STREAM_IN, GENERAL_CONTROL_2, CHANNEL_SELECT, GROUP_DELAY_SCANS,
    modify_word, read_word, read_eeprom, subsystem_command,
)
from labdaq.simulator import SimulatedTransport
from labdaq.transport import (
    DaqSession, TransportError, TransportTimeout, DeviceDisconnected, DataOverrun,
)


def session(scenario="sine", channels=(0, 1, 2, 3), fragment_size=None, **config_kwargs):
    device = SimulatedTransport(seed=17, scenario=scenario, fragment_size=fragment_size)
    client = DaqSession(device)
    client.configure(AcquisitionConfig(channels=channels, **config_kwargs))
    client.start()
    return client, device


def test_register_wire_commands_are_stateful_and_have_no_write_ack():
    device = SimulatedTransport()
    device.registers[GENERAL_CONTROL_2] = 0xABCD
    device.bulk_write(COMMAND_OUT, modify_word(GENERAL_CONTROL_2, 0x00F0, 0x0020))
    assert device.registers[GENERAL_CONTROL_2] == 0xAB2D
    with pytest.raises(TransportTimeout):
        device.bulk_read(COMMAND_IN, 64)
    device.bulk_write(COMMAND_OUT, read_word(GENERAL_CONTROL_2))
    assert device.bulk_read(COMMAND_IN, 2) == bytes.fromhex("ab2d")
    device.bulk_write(COMMAND_OUT, read_eeprom((5, 6)))
    assert device.bulk_read(COMMAND_IN, 2) == b"\x04\x00"
    assert device.stats["command_bytes"] == 3 * 64


def test_invalid_state_and_no_implicit_hardware_transport():
    device = SimulatedTransport()
    with pytest.raises(TransportError):
        device.bulk_write(COMMAND_OUT, subsystem_command(True))
    with pytest.raises(TransportTimeout):
        device.bulk_read(STREAM_IN, 16)
    with pytest.raises(NotImplementedError):
        DaqSession(object())
    client = DaqSession(device)
    with pytest.raises(TransportError):
        client.start()


def test_nominal_rate_waveform_channels_and_39_scan_discard():
    client, device = session(channels=(0, 2, 3), sample_rate=2000)
    actual = client.actual_rate
    assert actual == pytest.approx(device.sample_rate, rel=1e-15)
    assert device.registers[CHANNEL_SELECT] == 0b1101
    result = client.read(1000)
    assert result.shape == (1000, 3)
    t = np.arange(1000) / actual
    for index, channel in enumerate((0, 2, 3)):
        truth = 0.02 * channel + (0.20 + 0.05 * channel) * np.sin(2 * np.pi * 10 * (channel + 1) * t)
        np.testing.assert_allclose(result[:, index], truth, atol=10 / 2**24, rtol=0)
    assert device.stats["stream_scans"] == 1000 + GROUP_DELAY_SCANS
    assert client.samples_read == 1000
    assert any(cmd[:4] == bytes.fromhex("28000000") for cmd in device.command_log)
    client.close()


def test_stream_fragmentation_and_three_four_channel_identity():
    reference, _ = session(scenario="drag")
    fragment, _ = session(scenario="drag", channels=(0, 2, 3), fragment_size=7)
    expected = reference.read(1500)
    received = np.vstack([fragment.read(101), fragment.read(599), fragment.read(800)])
    np.testing.assert_array_equal(received, expected[:, [0, 2, 3]])
    reference.close()
    fragment.close()


def test_mixed_ranges_and_iepe_coupling_bits():
    client, device = session(channels=(1, 3), ranges=(10, 1, 10, 1),
                            coupling=("DC", "AC", "DC", "DC"), iepe=(False, True, False, False))
    assert device.ranges == (1.0, 1.0)
    assert device.registers[GENERAL_CONTROL_2] & 0x0FFF == 0x02A2
    values = client.read(100)
    assert values.shape == (100, 2) and np.isfinite(values).all()
    with pytest.raises(TransportError):
        client.configure(AcquisitionConfig())
    client.stop()
    client.configure(AcquisitionConfig())
    client.start()
    np.testing.assert_allclose(client.read(1)[0], [0, 0.02, 0.04, 0.06], atol=10 / 2**24)
    client.close()


def test_timeout_keeps_partial_scan_without_channel_shift():
    class InterruptingTransport(SimulatedTransport):
        calls = 0

        def bulk_read(self, endpoint, size, timeout_ms=1000):
            if endpoint == STREAM_IN:
                self.calls += 1
                if self.calls == 104:
                    # Past the 39*16 settling bytes, in the middle of a scan.
                    raise TransportTimeout("partial transfer test")
            return super().bulk_read(endpoint, size, timeout_ms)

    device = InterruptingTransport(scenario="sine", fragment_size=7)
    client = DaqSession(device)
    client.configure(AcquisitionConfig())
    client.start()
    with pytest.raises(TransportTimeout):
        client.read(100)
    restored = client.read(100)
    reference, _ = session()
    np.testing.assert_array_equal(restored, reference.read(100))
    client.close()
    reference.close()


@pytest.mark.parametrize("fault,error", [("timeout", TransportTimeout), ("disconnect", DeviceDisconnected), ("overrun", DataOverrun)])
def test_faults_propagate_instead_of_becoming_valid_measurements(fault, error):
    client, device = session()
    client.read(10)
    device.inject_fault(fault)
    with pytest.raises(error):
        client.read(100)
    assert client.samples_read == 10
    try:
        client.close()
    except DeviceDisconnected:
        pass
    assert not device.running


def test_step_crossing_uses_actual_sample_time():
    client, _ = session(scenario="step", channels=(0,), sample_rate=48000)
    values = client.read(24001).ravel()
    np.testing.assert_array_equal(values[:24000], 0)
    assert values[24000] == pytest.approx(0.3, abs=10 / 2**24)
    client.close()


def test_ac_one_hz_step_decay_dc_retention_and_fragment_state():
    options = dict(scenario="step", channels=(0,), ranges=(1.0,) * 4)
    dc, _ = session(**options)
    ac, _ = session(**options, coupling=("AC", "DC", "DC", "DC"))
    fragmented, _ = session(**options, coupling=("AC", "DC", "DC", "DC"), fragment_size=7)
    try:
        count = round(ac.actual_rate * 1.6)
        step_index = int(np.ceil(0.5 * ac.actual_rate))
        dc_values = dc.read(count).ravel()
        ac_values = ac.read(count).ravel()
        fragment_values = np.concatenate([fragmented.read(900).ravel(), fragmented.read(count - 900).ravel()])
        np.testing.assert_array_equal(fragment_values, ac_values)
        # For a 0.3 V step, the specified one-pole high-pass has response
        # 0.3 * alpha**(n+1); DC coupling instead preserves the plateau.
        alpha = 1.0 / (1.0 + 2 * np.pi * 1.0 / ac.actual_rate)
        expected = 0.3 * alpha ** np.arange(1, count - step_index + 1)
        np.testing.assert_array_equal(dc_values[:step_index], 0)
        np.testing.assert_array_equal(ac_values[:step_index], 0)
        np.testing.assert_allclose(dc_values[step_index:], 0.3, atol=1 / 2**24, rtol=0)
        np.testing.assert_allclose(ac_values[step_index:], expected, atol=1 / 2**24, rtol=0)
        assert ac_values[-1] < 0.001  # over one second after load application
    finally:
        for client in (dc, ac, fragmented):
            client.close()
