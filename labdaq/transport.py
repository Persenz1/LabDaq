"""Protocol session and read-only USB discovery.

The session emits the public DT9837C packet format. It deliberately accepts
only the simulated transport in this prototype: power-up, firmware loading,
asynchronous USB transfers and real timebase settling have not been tested.
No original-driver DLL is loaded. USB discovery does not claim interfaces,
change drivers, issue vendor commands or read/write EEPROM.
"""

import struct

import numpy as np

from .protocol import (
    AcquisitionConfig, Command, Message, ProtocolError, COMMAND_OUT, COMMAND_IN,
    STREAM_IN, DT_VENDOR_ID, BOOT_PRODUCT_ID, RUNTIME_PRODUCT_ID,
    GENERAL_CONTROL_0, GENERAL_CONTROL_1, GENERAL_CONTROL_2,
    CHANNEL_SELECT, FIFO_FLAG_MASK, GROUP_DELAY_SCANS, packet, modify_word,
    read_word, pll_write, plan_clock, subsystem_command, decode_adc, decode_message,
)


class TransportError(RuntimeError):
    pass


class TransportTimeout(TransportError):
    pass


class DeviceDisconnected(TransportError):
    pass


class DataOverrun(TransportError):
    pass


def enumerate_usb_devices() -> list[dict]:
    """List DT9837-family descriptor identities using bundled libusb/PyUSB.

    A/B/C share the runtime PID. Identifying C requires EEPROM bytes 05..06;
    descriptor-only discovery therefore never labels an unknown unit as C.
    Missing backend or access failure raises TransportError, not an empty list.
    """
    try:
        import usb.core
        import usb.backend.libusb1
        import libusb_package
    except ImportError as error:
        raise TransportError("USB 枚举需要 pyusb 和 libusb-package") from error
    backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    if backend is None:
        raise TransportError("未找到可用的 libusb 后端")
    try:
        devices = usb.core.find(find_all=True, idVendor=DT_VENDOR_ID, backend=backend)
        return [dict(vendor_id=dev.idVendor, product_id=dev.idProduct,
                     bus=dev.bus, address=dev.address,
                     state="bootloader" if dev.idProduct == BOOT_PRODUCT_ID else "runtime",
                     model="DT9837A/B/C（型号未核实）")
                for dev in devices if dev.idProduct in (BOOT_PRODUCT_ID, RUNTIME_PRODUCT_ID)]
    except usb.core.USBError as error:
        raise TransportError(f"USB 设备枚举失败：{error}") from error


class DaqSession:
    """Single-owner synchronous session; callers can put it on a worker thread.

    read(n) returns exactly n interleaved scans, converted to volts. On timeout,
    any partial transfer is retained, so a caller can retry without introducing
    a channel shift. No automatic timeout retry hides communication failures.
    """

    def __init__(self, transport):
        if not getattr(transport, "is_simulated", False):
            raise NotImplementedError("实机采集尚未验证；本版本只允许协议模拟传输")
        self.transport = transport
        self.config: AcquisitionConfig | None = None
        self.actual_rate = 0.0
        self.running = False
        self.closed = False
        self.samples_read = 0
        self.commands_sent = 0
        self._pending = bytearray()
        self._discard_bytes = 0

    def _write(self, command: bytes):
        if self.closed:
            raise TransportError("会话已关闭")
        transferred = self.transport.bulk_write(COMMAND_OUT, command, timeout_ms=1000)
        if transferred != len(command):
            raise TransportError("命令写入不完整")
        self.commands_sent += 1

    def _modify(self, address: int, changed: int, value: int):
        self._write(modify_word(address, changed, value))

    def configure(self, config: AcquisitionConfig) -> float:
        if self.running:
            raise TransportError("采集时不能更改硬件参数")
        clock = plan_clock(config.sample_rate)
        self.config = None
        # The real driver powers the board, waits 100 ms and starts its async
        # message reader here. Simulator has no wall-clock settling delay.
        self._write(packet(Command.POWER, b"\x01"))
        self._modify(GENERAL_CONTROL_0, 0x0003, 0)  # independent internal clock
        self._modify(GENERAL_CONTROL_1, 0x0002, 0x0002)  # ADC sync pulse
        for channel in range(4):
            coupling_bit, current_bit = 1 << channel, 1 << (channel + 8)
            current = current_bit if config.iepe[channel] else 0
            if config.coupling[channel] == "AC":
                self._modify(GENERAL_CONTROL_2, coupling_bit, coupling_bit)
                self._modify(GENERAL_CONTROL_2, current_bit, current)
            else:
                self._modify(GENERAL_CONTROL_2, current_bit, current)
                self._modify(GENERAL_CONTROL_2, coupling_bit, 0)
        self._modify(GENERAL_CONTROL_1, 1, int(clock.high_speed))
        for register in clock.registers:
            self._write(pll_write((register,)))
        self._modify(GENERAL_CONTROL_0, 0x0300, clock.divider_register)
        gain_mask = sum(1 << (ch + 4) for ch in config.channels)
        gain_value = sum(1 << (ch + 4) for ch in config.channels if config.ranges[ch] == 1.0)
        self._modify(GENERAL_CONTROL_2, gain_mask, gain_value)
        self._modify(CHANNEL_SELECT, 0xFF, sum(1 << ch for ch in config.channels))
        self._write(read_word(FIFO_FLAG_MASK))
        response = self.transport.bulk_read(COMMAND_IN, 2, timeout_ms=1000)
        if len(response) != 2:
            raise ProtocolError("FIFO 寄存器回复长度不正确")
        if struct.unpack(">H", response)[0] > 0x200:
            target = max(2, min(1024, 1 << max(1, int(np.ceil(np.log2(clock.actual_rate * 0.02))))))
            self._modify(FIFO_FLAG_MASK, 0xFFFF, 2048 - target)
        self.config = config
        self.actual_rate = clock.actual_rate
        self._pending.clear()
        return self.actual_rate

    def start(self):
        if self.config is None:
            raise TransportError("请先配置采集参数")
        if self.running:
            raise TransportError("采集已经开始")
        self._pending.clear()
        self._discard_bytes = GROUP_DELAY_SCANS * len(self.config.channels) * 4
        self.samples_read = 0
        self._write(subsystem_command(True))
        self.running = True

    def _check_messages(self):
        # A true USB implementation must continuously service 0x88 via async
        # reads. The simulator supplies a nonblocking queue of the same bytes.
        while data := self.transport.poll_message():
            message, _ = decode_message(data)
            if message == Message.ADC_OVERRUN:
                self._pending.clear()
                raise DataOverrun("ADC 缓冲区溢出，数据连续性已中断")

    def read(self, scans: int, timeout_ms: int = 1000) -> np.ndarray:
        if not self.running or self.config is None:
            raise TransportError("采集尚未开始")
        if not isinstance(scans, int) or scans <= 0:
            raise ValueError("读取扫描数需为正整数")
        frame_bytes = 4 * len(self.config.channels)
        self._check_messages()
        while self._discard_bytes or len(self._pending) < scans * frame_bytes:
            needed = self._discard_bytes + scans * frame_bytes - len(self._pending)
            block = self.transport.bulk_read(STREAM_IN, min(65536, needed), timeout_ms=timeout_ms)
            if not block:
                raise TransportTimeout("采样端点未返回数据")
            self._pending.extend(block)
            if self._discard_bytes:
                remove = min(len(self._pending), self._discard_bytes)
                del self._pending[:remove]
                self._discard_bytes -= remove
            self._check_messages()
        consume = scans * frame_bytes
        data = bytes(self._pending[:consume])
        del self._pending[:consume]
        limits = tuple(self.config.ranges[ch] for ch in self.config.channels)
        result = decode_adc(data, limits)
        self.samples_read += scans
        return result

    def stop(self):
        if self.running:
            try:
                self._write(subsystem_command(False))
            finally:
                self.running = False
                self._pending.clear()

    def close(self):
        if not self.closed:
            try:
                self.stop()
            finally:
                self.transport.close()
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
