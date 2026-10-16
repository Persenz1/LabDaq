"""DT9837C wire format and clock planning, without a vendor DLL.

Adapted from libuldaq 1.2.2 Usb9837xDefs.h, Usb9837x::Cmd_* / programClock,
DaqIUsb9837x::configureCGL / configureClock / processScanData32_dbl.
Copyright (c) 2018 Measurement Computing. MIT License; full notice is in
THIRD_PARTY_NOTICES.md.

Commands use LE uint32 opcodes, but local-bus words are BE uint16. ADC
samples are *unsigned offset binary*, 24 effective bits in LE uint32.
No sequence number, checksum, frame header or write acknowledgement exists
in the public command definition. Do not invent one for the hardware path.
"""

from dataclasses import dataclass
from enum import IntEnum
import math
import struct

import numpy as np

COMMAND_OUT = 0x01
COMMAND_IN = 0x81
STREAM_IN = 0x82
MESSAGE_IN = 0x88
STREAM_OUT = 0x06
COMMAND_SIZE = 64
ADC_BITS = 24
ADC_COUNTS = 1 << ADC_BITS
GROUP_DELAY_SCANS = 39
DT_VENDOR_ID = 0x0867
BOOT_PRODUCT_ID = 0x9839
RUNTIME_PRODUCT_ID = 0x3998
GENERAL_CONTROL_0 = 0x00
GENERAL_CONTROL_1 = 0x01
GENERAL_CONTROL_2 = 0x02
CHANNEL_SELECT = 0x10
GENERAL_CONTROL_6 = 0x20
FIFO_FLAG_MASK = 0x82


class Command(IntEnum):
    READ_I2C = 12
    WRITE_I2C = 13
    READ_WORD = 20
    WRITE_WORD = 21
    MODIFY_WORD = 24
    START = 30
    STOP = 31
    READ_SINGLE = 32
    WRITE_PLL = 40
    POWER = 52
    WRITE_SINGLE = 53


class Message(IntEnum):
    COUNTER_OVERFLOW = 1
    DIGITAL_CHANGED = 2
    DAC_THRESHOLD = 3
    ADC_OVERSAMPLE = 4
    ADC_OVERRUN = 5
    OUTPUT_UNDERFLOW = 6
    DAC_OVERSAMPLE = 7
    OUTPUT_DONE = 8


class ProtocolError(ValueError):
    """Malformed wire data or unsupported simulated command."""


@dataclass(frozen=True)
class AcquisitionConfig:
    """Per-channel tuples always use physical indices 0..3, even for 3 inputs."""

    sample_rate: float = 2000.0
    channels: tuple[int, ...] = (0, 1, 2, 3)
    ranges: tuple[float, ...] = (10.0, 10.0, 10.0, 10.0)
    coupling: tuple[str, ...] = ("DC", "DC", "DC", "DC")
    iepe: tuple[bool, ...] = (False, False, False, False)

    def __post_init__(self):
        if not math.isfinite(self.sample_rate) or not 195.313 <= self.sample_rate <= 105469.0:
            raise ValueError("DT9837C 采样率需在 195.313–105469 Hz 之间")
        if not self.channels or tuple(sorted(set(self.channels))) != tuple(self.channels):
            raise ValueError("通道必须不重复并按物理编号升序排列")
        if any(ch not in range(4) for ch in self.channels):
            raise ValueError("DT9837C 模拟输入通道为 0–3")
        if any(len(values) != 4 for values in (self.ranges, self.coupling, self.iepe)):
            raise ValueError("量程、耦合、IEPE 配置各需包含 4 个物理通道")
        if any(value not in (1.0, 10.0) for value in self.ranges):
            raise ValueError("DT9837C 仅支持 ±1 V 和 ±10 V 量程")
        if any(value not in ("AC", "DC") for value in self.coupling):
            raise ValueError("耦合需为 AC 或 DC")


def packet(command: int, payload: bytes = b"") -> bytes:
    if len(payload) > 60:
        raise ProtocolError("命令 payload 超过 60 字节")
    return (struct.pack("<I", command) + payload).ljust(COMMAND_SIZE, b"\0")


def decode_packet(data: bytes) -> tuple[Command, bytes]:
    if len(data) != COMMAND_SIZE:
        raise ProtocolError("DT9837C 命令必须为 64 字节")
    try:
        command = Command(struct.unpack_from("<I", data)[0])
    except ValueError as error:
        raise ProtocolError("此原型未实现该命令") from error
    return command, data[4:]


def read_word(address: int) -> bytes:
    return packet(Command.READ_WORD, struct.pack(">H", address))


def write_word(address: int, value: int) -> bytes:
    return packet(Command.WRITE_WORD, struct.pack(">HH", address, value))


def modify_word(address: int, changed_bits: int, value: int) -> bytes:
    """The source's AndMask means bits to REPLACE, not bits to preserve."""
    return packet(Command.MODIFY_WORD, struct.pack(">HHH", address, changed_bits, value))


def subsystem_command(start: bool, subsystem: int = 0, trigger_flags: int = 0) -> bytes:
    return packet(Command.START if start else Command.STOP, struct.pack("<IH", subsystem, trigger_flags))


def pll_write(register_values: tuple[tuple[int, int], ...], subsystem: int = 0) -> bytes:
    if not 1 <= len(register_values) <= 27:
        raise ProtocolError("每条 PLL 写入命令允许 1–27 个寄存器")
    return packet(Command.WRITE_PLL, struct.pack("<IBB", subsystem, 0x69, len(register_values))
                  + bytes(value for pair in register_values for value in pair))


def read_eeprom(offsets: tuple[int, ...]) -> bytes:
    if not 1 <= len(offsets) <= 29:
        raise ProtocolError("单次 EEPROM 读取允许 1–29 字节")
    return packet(Command.READ_I2C, bytes([len(offsets)])
                  + bytes(value for offset in offsets for value in (0x50, offset)))


def decode_message(data: bytes) -> tuple[Message, bytes]:
    if len(data) < 4:
        raise ProtocolError("异步消息头不足 4 字节")
    try:
        return Message(struct.unpack_from("<I", data)[0]), data[4:]
    except ValueError as error:
        raise ProtocolError("未知异步消息代码") from error


def decode_adc(data: bytes, ranges: tuple[float, ...]) -> np.ndarray:
    """Decode complete interleaved scans. Stream assembly belongs to session."""
    width = len(ranges)
    if not width or len(data) % (4 * width):
        raise ProtocolError("ADC 数据没有组成完整扫描")
    codes = np.frombuffer(data, dtype="<u4").reshape(-1, width)
    if np.any(codes >= ADC_COUNTS):
        raise ProtocolError("24 位 ADC 数据高 8 位非零；不能静默截断")
    return codes.astype(np.float64) * (2.0 * np.asarray(ranges) / ADC_COUNTS) - ranges


def encode_adc(volts: np.ndarray, ranges: tuple[float, ...]) -> bytes:
    """Simulator's quantizer; maximum code is one LSB below positive range."""
    limits = np.asarray(ranges)
    values = np.asarray(volts, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != len(limits) or not np.isfinite(values).all():
        raise ProtocolError("ADC 编码要求有限的二维电压数组")
    codes = np.rint((values + limits) * (ADC_COUNTS / (2.0 * limits)))
    return np.clip(codes, 0, ADC_COUNTS - 1).astype("<u4").tobytes()


@dataclass(frozen=True)
class ClockPlan:
    requested_rate: float
    actual_rate: float
    divider: int
    divider_register: int
    high_speed: bool
    p_total: int
    q_total: int
    registers: tuple[tuple[int, int], ...]


def plan_clock(sample_rate: float) -> ClockPlan:
    """Port the public 48 MHz CY22150 clock search, including first-tie rule.

    This is a nominal digital frequency. Crystal tolerance and actual hardware
    timebase accuracy require comparison against a reference on the real unit.
    """
    if not math.isfinite(sample_rate) or not 195.313 <= sample_rate <= 105469.0:
        raise ValueError("DT9837C 采样率超出支持范围")
    high_speed = sample_rate > 52734.0
    ticks = 256 if high_speed else 512
    reference = 48.0
    q_max = min(129, max(2, int((reference + 0.095) / 0.190)))
    for divider in (2, 4, 8, 16):
        target = sample_rate * ticks * divider / 1e6
        div_n = min(128, max(4, int((100.0 + target / 2) / target)))
        initial_vco = target * div_n
        if initial_vco < 100:
            div_n += 1
        if initial_vco > 400:
            div_n -= 1
        vco = target * div_n
        candidates = []
        for q_total in range(3, q_max + 1):
            p_total = int(vco / reference * q_total + 0.5)
            if p_total > 2055:
                break
            frequency = reference * p_total / q_total
            # Match C++ truncation and its sub-unit negative error handling.
            error = 10000.0 * (frequency - vco)
            truncated = int(error)
            if truncated < 0:
                error = -error
                truncated = int(error)
            if error - truncated >= 0.5:
                truncated += 1
            candidates.append((truncated, q_total, p_total, frequency))
        _, q_total, p_total, frequency = min(candidates, key=lambda item: item[0])
        actual = frequency * 1e6 / div_n / (ticks * divider)
        if div_n < 128:
            break
    p_value = (p_total - (p_total % 2)) // 2 - 4
    q_value = (p_total % 2) * 128 + q_total - 2
    pump = 0
    for low, high, setting in ((16, 45, 0), (45, 480, 1), (480, 640, 2), (640, 800, 3), (800, 1024, 4)):
        if low <= p_total < high:
            pump = setting
    registers = ((0x0C, div_n), (0x47, (div_n << 1) & 0xFF),
                 (0x40, (p_value >> 8) | 0xC0 | (pump << 2)),
                 (0x41, p_value & 0xFF), (0x42, q_value & 0xFF),
                 (0x44, 0x3F), (0x45, 0xFF), (0x46, 0xFF), (0x09, 0x01))
    return ClockPlan(sample_rate, actual, divider, (2, 4, 8, 16).index(divider) << 8,
                     high_speed, p_total, q_total, registers)


def clock_rate_from_registers(registers: dict[int, int], control0: int, control1: int) -> float:
    """Independent decoding used by the virtual device, not its host clock plan."""
    p_value = ((registers[0x40] & 0x03) << 8) | registers[0x41]
    q_value = registers[0x42]
    p_total = 2 * (p_value + 4) + (q_value >> 7)
    q_total = (q_value & 0x7F) + 2
    divider = (2, 4, 8, 16)[(control0 >> 8) & 3]
    ticks = 256 if control1 & 1 else 512
    return 48e6 * p_total / q_total / (registers[0x0C] & 0x7F) / ticks / divider
