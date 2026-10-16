"""Stateful virtual DT9837C that consumes the real command packet subset.

No acquisition config object is passed to this device. Channel masks, gain,
coupling, IEPE and sample rate are derived from received register/PLL commands.
All output crosses the ADC quantizer and LE uint32 byte stream. Analogue
waveforms, settling, buffer timing and injected errors are simulation models,
not measurements or a claim of physical device equivalence.
"""

from collections import deque
import math
import struct

import numpy as np

from .protocol import (
    Command, Message, ProtocolError, COMMAND_OUT, COMMAND_IN, STREAM_IN,
    MESSAGE_IN, GENERAL_CONTROL_0, GENERAL_CONTROL_1, GENERAL_CONTROL_2,
    CHANNEL_SELECT, FIFO_FLAG_MASK, GROUP_DELAY_SCANS, decode_packet,
    clock_rate_from_registers, encode_adc,
)
from .transport import DeviceDisconnected, TransportError, TransportTimeout
from .signals import SimulationSettings, generate_signals


class SimulatedTransport:
    is_simulated = True

    def __init__(self, seed: int = 2026, scenario: str = "drag", fragment_size: int | None = None,
                 signal_settings: SimulationSettings | None = None):
        if signal_settings is None and scenario not in ("drag", "sine", "step"):
            raise ValueError("模拟场景需为 drag、sine 或 step")
        if fragment_size is not None and fragment_size <= 0:
            raise ValueError("分段长度必须为正数")
        self.signal_settings = signal_settings
        self.seed = signal_settings.seed if signal_settings is not None else seed
        self.scenario = scenario
        self.fragment_size = fragment_size
        self.connected = True
        self.powered = False
        self.running = False
        self.registers = {GENERAL_CONTROL_0: 0, GENERAL_CONTROL_1: 0,
                          GENERAL_CONTROL_2: 0, CHANNEL_SELECT: 0, FIFO_FLAG_MASK: 0x600}
        self.pll_registers: dict[int, int] = {}
        self.command_log: list[bytes] = []
        self.stats = dict(commands=0, command_bytes=0, stream_bytes=0, stream_scans=0, message_count=0)
        self._replies: deque[bytes] = deque()
        self._messages: deque[bytes] = deque()
        self._faults: deque[str] = deque()
        self._stream = bytearray()
        self._scan_index = 0
        self._rng = np.random.default_rng(self.seed)
        self._previous_x = np.zeros(4)
        self._previous_y = np.zeros(4)
        self.eeprom = bytearray(256)
        self.eeprom[5:7] = struct.pack("<H", 4)  # public subtype code for C
        self.eeprom[8:12] = struct.pack("<I", 98370001)  # explicitly simulated serial

    @property
    def channels(self) -> tuple[int, ...]:
        return tuple(ch for ch in range(4) if self.registers[CHANNEL_SELECT] & (1 << ch))

    @property
    def ranges(self) -> tuple[float, ...]:
        control = self.registers[GENERAL_CONTROL_2]
        return tuple(1.0 if control & (1 << (ch + 4)) else 10.0 for ch in self.channels)

    @property
    def sample_rate(self) -> float:
        return clock_rate_from_registers(self.pll_registers,
                                         self.registers[GENERAL_CONTROL_0], self.registers[GENERAL_CONTROL_1])

    def inject_fault(self, kind: str):
        """Queue one stream timeout/disconnect, or real-format ADC overrun message."""
        if kind not in ("timeout", "disconnect", "overrun"):
            raise ValueError("故障类型需为 timeout、disconnect 或 overrun")
        if kind == "overrun":
            self._messages.append(struct.pack("<IB", Message.ADC_OVERRUN, 0))
        else:
            self._faults.append(kind)

    def _require_connected(self):
        if not self.connected:
            raise DeviceDisconnected("模拟 USB 设备已断开")

    def bulk_write(self, endpoint: int, data: bytes, timeout_ms: int = 1000) -> int:
        self._require_connected()
        if endpoint != COMMAND_OUT:
            raise ProtocolError("原型未实现模拟输出数据端点")
        command, payload = decode_packet(bytes(data))
        self.command_log.append(bytes(data))
        if len(self.command_log) > 2048:
            del self.command_log[:-2048]
        self.stats["commands"] += 1
        self.stats["command_bytes"] += len(data)
        if command == Command.POWER:
            if payload[0] not in (0, 1):
                raise ProtocolError("非法电源命令")
            self.powered = bool(payload[0])
            if not self.powered:
                self.running = False
        elif command in (Command.WRITE_WORD, Command.MODIFY_WORD):
            if self.running:
                raise TransportError("模拟设备正在运行，不能更改寄存器")
            address, value = struct.unpack_from(">HH", payload)
            if command == Command.MODIFY_WORD:
                changed_bits = value
                value = struct.unpack_from(">H", payload, 4)[0]
                value = (self.registers.get(address, 0) & ~changed_bits) | (value & changed_bits)
            self.registers[address] = value & 0xFFFF
        elif command == Command.READ_WORD:
            address = struct.unpack_from(">H", payload)[0]
            self._replies.append(struct.pack(">H", self.registers.get(address, 0)))
        elif command == Command.WRITE_PLL:
            subsystem, device_address, count = struct.unpack_from("<IBB", payload)
            if subsystem != 0 or device_address != 0x69 or not 1 <= count <= 27:
                raise ProtocolError("模拟器只支持 A/D 的 CY22150 PLL")
            if self.running:
                raise TransportError("采集中不能改变时钟")
            for offset in range(6, 6 + count * 2, 2):
                self.pll_registers[payload[offset]] = payload[offset + 1]
        elif command in (Command.START, Command.STOP):
            subsystem, flags = struct.unpack_from("<IH", payload)
            if subsystem != 0 or flags:
                raise ProtocolError("当前协议模拟仅实现内部时钟、软件启动 A/D")
            if command == Command.START:
                if self.running:
                    raise TransportError("模拟 A/D 已经运行")
                if not self.powered or not self.channels:
                    raise TransportError("A/D 尚未上电或未选择通道")
                try:
                    rate = self.sample_rate
                except (KeyError, ZeroDivisionError) as error:
                    raise ProtocolError("PLL 寄存器尚未完整配置") from error
                if not 190 < rate < 106000:
                    raise ProtocolError("PLL 产生了不受支持的采样率")
                self._scan_index = 0
                self._rng = np.random.default_rng(self.seed)
                self._stream.clear()
                self._messages.clear()
                self._previous_x.fill(0)
                self._previous_y.fill(0)
                self.running = True
            else:
                self.running = False
                self._stream.clear()
        elif command == Command.READ_I2C:
            count = payload[0]
            if not 1 <= count <= 29:
                raise ProtocolError("非法 I2C 读取长度")
            values = []
            for offset in range(1, 1 + count * 2, 2):
                if payload[offset] != 0x50:
                    raise ProtocolError("模拟器仅提供 EEPROM I2C 地址 0x50")
                values.append(self.eeprom[payload[offset + 1]])
            self._replies.append(bytes(values))
        else:
            raise ProtocolError(f"模拟器未实现命令 {command.name}")
        return len(data)

    def bulk_read(self, endpoint: int, size: int, timeout_ms: int = 1000) -> bytes:
        self._require_connected()
        if size <= 0:
            raise ValueError("USB 读取长度需为正数")
        if endpoint == COMMAND_IN:
            if not self._replies:
                raise TransportTimeout("命令回复端点为空；写入命令没有 ACK")
            reply = self._replies.popleft()
            if len(reply) > size:
                self._replies.appendleft(reply[size:])
            return reply[:size]
        if endpoint == MESSAGE_IN:
            message = self.poll_message()
            if not message:
                raise TransportTimeout("暂无异步消息")
            return message[:size]
        if endpoint != STREAM_IN:
            raise ProtocolError("未知输入端点")
        if not self.running:
            raise TransportTimeout("模拟 A/D 尚未启动")
        if self._faults:
            fault = self._faults.popleft()
            if fault == "disconnect":
                self.connected = False
                self.running = False
                raise DeviceDisconnected("故障注入：模拟 USB 拔出")
            raise TransportTimeout("故障注入：采样端点读取超时")
        length = min(size, self.fragment_size) if self.fragment_size else size
        frame_bytes = 4 * len(self.channels)
        if len(self._stream) < length:
            scans = math.ceil((length - len(self._stream)) / frame_bytes)
            samples = self._generate(scans)
            self._stream.extend(encode_adc(samples[:, self.channels], self.ranges))
            self.stats["stream_scans"] += scans
        result = bytes(self._stream[:length])
        del self._stream[:length]
        self.stats["stream_bytes"] += len(result)
        return result

    def _generate(self, count: int) -> np.ndarray:
        indices = np.arange(self._scan_index, self._scan_index + count)
        if self.signal_settings is not None:
            values = np.zeros((count, 4))
            first_valid = min(count, max(0, GROUP_DELAY_SCANS - self._scan_index))
            values[first_valid:] = generate_signals(
                self.signal_settings, max(0, self._scan_index - GROUP_DELAY_SCANS),
                count - first_valid, self.sample_rate)
        else:
            # Preserve the original scenarios, including their original noise.
            t = np.maximum(indices - GROUP_DELAY_SCANS, 0) / self.sample_rate
            values = np.empty((count, 4))
            noise = self._rng.normal(0, 0.002, size=(count, 4))
            for channel in range(4):
                if self.scenario == "sine":
                    values[:, channel] = 0.02 * channel + (0.20 + 0.05 * channel) * np.sin(2 * np.pi * 10 * (channel + 1) * t)
                elif self.scenario == "step":
                    values[:, channel] = 0.02 * channel + (0.3 + channel * 0.1) * ((t % 4) >= 0.5) * ((t % 4) < 2.5)
                else:
                    phase = (t + channel * 0.07) % 8.0
                    rise = np.clip((phase - 1.0) / 1.2, 0, 1)
                    decay = np.clip((6.0 - phase) / 0.6, 0, 1)
                    load = np.minimum(rise, decay)
                    slip = 0.035 * np.sin(2 * np.pi * (8 + channel) * t) * load
                    values[:, channel] = 0.025 + channel * 0.01 + (0.35 + channel * 0.08) * load + slip + noise[:, channel]
            values[indices < GROUP_DELAY_SCANS] = 0.0
        # Use the C manual's nominal 1 Hz corner in a first-order demonstration
        # model. This does NOT reproduce the measured analogue filter/response.
        alpha = 1.0 / (1.0 + 2 * np.pi * 1.0 / self.sample_rate)
        control = self.registers[GENERAL_CONTROL_2]
        for channel in range(4):
            if control & (1 << channel):
                prev_x, prev_y = self._previous_x[channel], self._previous_y[channel]
                for row in range(count):
                    current_x = values[row, channel]
                    prev_y = alpha * (prev_y + current_x - prev_x)
                    values[row, channel] = prev_y
                    prev_x = current_x
                self._previous_x[channel], self._previous_y[channel] = prev_x, prev_y
        self._scan_index += count
        return values

    def poll_message(self) -> bytes | None:
        self._require_connected()
        if self._messages:
            self.stats["message_count"] += 1
            return self._messages.popleft()
        return None

    def close(self):
        self.running = False
        self.connected = False
        self._stream.clear()
