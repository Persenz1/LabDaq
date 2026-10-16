import numpy as np
import pytest

from labdaq.protocol import (
    AcquisitionConfig, Command, Message, ProtocolError, decode_adc, decode_message,
    decode_packet, encode_adc, modify_word, packet, plan_clock, read_eeprom,
    read_word, subsystem_command, pll_write, clock_rate_from_registers,
)


def test_command_golden_vectors_from_packed_cpp_definitions():
    # uint32 opcode LE followed by BE address/mask/value, packed, zero padded.
    assert modify_word(0x0002, 0x0F00, 0x0500) == bytes.fromhex("1800000000020f000500") + bytes(54)
    assert read_word(0x0082) == bytes.fromhex("140000000082") + bytes(58)
    assert subsystem_command(True, 0, 4) == bytes.fromhex("1e000000000000000400") + bytes(54)
    assert subsystem_command(False) == bytes.fromhex("1f000000") + bytes(60)
    assert pll_write(((0x0C, 0x31),)) == bytes.fromhex("280000000000000069010c31") + bytes(52)
    assert read_eeprom((5, 6)) == bytes.fromhex("0c0000000250055006") + bytes(55)


def test_adc_offset_binary_golden_boundary_codes():
    # Device stream: zero code is -FS; 0x800000 is 0 V; 0xffffff is +FS-1 LSB.
    wire = bytes.fromhex("00000000 00008000 ffffff00")
    volts = decode_adc(wire, (10.0,)).ravel()
    np.testing.assert_allclose(volts, [-10, 0, 10 - 20 / 2**24], atol=0, rtol=0)
    np.testing.assert_array_equal(decode_adc(wire, (1.0,)).ravel(), [-1, 0, 1 - 2 / 2**24])
    with pytest.raises(ProtocolError, match="高 8 位"):
        decode_adc(bytes.fromhex("00000001"), (10.0,))
    with pytest.raises(ProtocolError, match="完整扫描"):
        decode_adc(wire[:7], (10.0,))


def test_adc_saturation_and_one_lsb_error():
    values = np.array([[-12.0, -2.0], [0.123456, 0.123456], [12.0, 2.0]])
    result = decode_adc(encode_adc(values, (10.0, 1.0)), (10.0, 1.0))
    np.testing.assert_array_equal(result[0], [-10.0, -1.0])
    np.testing.assert_array_equal(result[-1], [10 - 20 / 2**24, 1 - 2 / 2**24])
    assert np.all(np.abs(result[1] - values[1]) <= np.array([20, 2]) / 2**25)


def test_clock_48khz_exact_rational_and_register_bytes():
    # 48 MHz * (512 / 125) / DIV1=4 / ticks=512 / divider=2 = 48 kHz.
    clock = plan_clock(48000)
    assert clock.actual_rate == 48000
    assert (clock.p_total, clock.q_total, clock.divider) == (512, 125, 2)
    regs = dict(clock.registers)
    assert regs[0x0C] == 4
    assert regs[0x40] == 0xC8
    assert regs[0x41] == 0xFC
    assert regs[0x42] == 0x7B
    assert clock_rate_from_registers(regs, 0, 0) == 48000
    fast = plan_clock(96000)
    assert fast.actual_rate == 96000
    assert fast.high_speed
    assert fast.registers == clock.registers
    assert clock_rate_from_registers(dict(fast.registers), 0, 1) == 96000


@pytest.mark.parametrize("rate", [195.313, 2000, 10000, 52734, 52735, 105469])
def test_clock_boundaries_and_reconstruction(rate):
    clock = plan_clock(rate)
    assert abs(clock.actual_rate / rate - 1) < 0.001
    decoded = clock_rate_from_registers(dict(clock.registers), clock.divider_register, int(clock.high_speed))
    assert decoded == pytest.approx(clock.actual_rate, rel=1e-15)


def test_wire_messages_have_no_invented_sequence_or_ack():
    assert decode_message(bytes.fromhex("0500000000")) == (Message.ADC_OVERRUN, b"\0")
    command, body = decode_packet(subsystem_command(True))
    assert command == Command.START and body == bytes(60)
    with pytest.raises(ProtocolError):
        decode_packet(bytes(63))
    with pytest.raises(ProtocolError):
        packet(30, bytes(61))


@pytest.mark.parametrize("kwargs", [dict(sample_rate=100), dict(sample_rate=float("nan")),
                                   dict(channels=(2, 0)), dict(channels=(0, 0)),
                                   dict(channels=()), dict(channels=(4,)),
                                   dict(ranges=(5, 10, 10, 10)), dict(coupling=("DC",))])
def test_configuration_hardware_constraints(kwargs):
    with pytest.raises(ValueError):
        AcquisitionConfig(**kwargs)
