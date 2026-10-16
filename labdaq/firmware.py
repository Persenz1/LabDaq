"""Offline DT9837-family RAM boot planning and control-transfer simulation.

Sequence adapted from libuldaq 1.2.2 DtFx2FwLoader::downloadFirmware,
downloadIntelHex, reset8051 and prepareHardware. Copyright (c) 2018
Measurement Computing; MIT notice in THIRD_PARTY_NOTICES.md.

There is no physical USB I/O in this module. It does not execute 8051 code,
model FPGA internals, write EEPROM, sleep, or bundle original firmware images.
Source paths are supplied explicitly for analysis of the archived C arrays.
"""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence

from .protocol import BOOT_PRODUCT_ID, RUNTIME_PRODUCT_ID

MAX_INTERNAL_RAM = 0x1B3F
CPUCS_EZUSB = 0x7F92
CPUCS_FX2 = 0xE600
LOAD_INTERNAL = 0xA0
LOAD_EXTERNAL = 0xA3
VENDOR_DEVICE_OUT = 0x40


@dataclass(frozen=True)
class IntelHexRecord:
    address: int
    data: bytes = b""
    record_type: int = 0

    def __post_init__(self):
        if not 0 <= self.address <= 0xFFFF or len(self.data) > 16:
            raise ValueError("IntelHexRec 地址需为 16 位，单条数据最多 16 字节")
        if self.record_type not in (0, 1) or (self.record_type == 1 and self.data):
            raise ValueError("只支持 DT C 数组的数据记录和空 type=1 终止记录")

    @property
    def length(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class BootOperation:
    kind: str
    request: int | None = None
    address: int = 0
    data: bytes = b""
    request_type: int = VENDOR_DEVICE_OUT
    index: int = 0
    delay_ms: int = 0
    product_id: int | None = None

    @property
    def length(self) -> int:
        return len(self.data)


def parse_c_records(source: str) -> tuple[IntelHexRecord, ...]:
    """Parse the upstream IntelHexRec C initializer, not an Intel HEX text file.

    Data[16] is an implicit padded C array. Only Length bytes are transferred;
    the terminator's `{0}` initializer is therefore returned as empty data.
    """
    source = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", source, flags=re.S)
    array = re.search(r"INTEL_HEX_RECORD\s+\w+\s*\[\s*\]\s*=\s*\{(.*?)\};", source, re.S)
    if array is None:
        raise ValueError("未找到 INTEL_HEX_RECORD C 数组")
    number = r"(?:0[xX][0-9a-fA-F]+|[0-9]+)"
    record_pattern = re.compile(rf"({number})\s*,\s*({number})\s*,\s*({number})\s*,\s*\{{([^{{}}]*)\}}")
    body = array.group(1)
    records = []
    end = 0
    for match in record_pattern.finditer(body):
        if body[end:match.start()].strip(" \t\r\n,{}"):
            raise ValueError("C 数组中存在不支持的记录表达式")
        length, address, kind = (int(value, 0) for value in match.group(1, 2, 3))
        values = [int(value.strip(), 0) for value in match.group(4).split(",") if value.strip()]
        if not 0 <= length <= 16 or len(values) > 16 or any(not 0 <= value <= 255 for value in values):
            raise ValueError("固件记录长度或字节值不合法")
        # C zero-initializes omitted trailing elements of Data[16].
        data = bytes((values + [0] * 16)[:length])
        records.append(IntelHexRecord(address, data, kind))
        end = match.end()
    if body[end:].strip(" \t\r\n,{}") or not records:
        raise ValueError("固件 C 数组包含无法解析的内容")
    _data_records(records)  # also require the source's terminating record
    return tuple(records)


def read_firmware_records(path: str | Path) -> tuple[IntelHexRecord, ...]:
    return parse_c_records(Path(path).read_text(encoding="utf-8-sig"))


def _data_records(records: Sequence[IntelHexRecord]) -> tuple[IntelHexRecord, ...]:
    for index, record in enumerate(records):
        if record.record_type == 1:
            return tuple(records[:index])
    raise ValueError("固件数组缺少 type=1 终止记录")


def plan_firmware_load(loader_records: Sequence[IntelHexRecord],
                       device_records: Sequence[IntelHexRecord]) -> list[BootOperation]:
    """Build the public loader's ordered RAM operations; no transfer is run."""
    loader = _data_records(loader_records)
    device = _data_records(device_records)
    operations: list[BootOperation] = []

    def reset(value: int):
        for address in (CPUCS_EZUSB, CPUCS_FX2):
            operations.append(BootOperation("control", LOAD_INTERNAL, address, bytes([value])))

    def image(records: Sequence[IntelHexRecord]):
        # Match the original two passes and starting-address boundary exactly.
        for record in records:
            if record.address > MAX_INTERNAL_RAM:
                operations.append(BootOperation("control", LOAD_EXTERNAL, record.address, record.data))
        reset(1)
        for record in records:
            if record.address <= MAX_INTERNAL_RAM:
                operations.append(BootOperation("control", LOAD_INTERNAL, record.address, record.data))

    reset(1)
    image(loader)
    reset(0)
    operations.append(BootOperation("delay", delay_ms=1))
    image(device)
    reset(1)
    reset(0)
    # prepareHardware() waits five seconds before using the newly enumerated unit.
    operations.append(BootOperation("delay", delay_ms=5000))
    operations.append(BootOperation("reenumerate", product_id=RUNTIME_PRODUCT_ID))
    return operations


class BootloaderSimulator:
    """Model the known two-stage boot contract in RAM, without emulating code.

    Releasing reset after the first loaded image represents the helper loader;
    releasing it after the second represents the runtime firmware. This state
    transition is a simulation assumption, not proof those bytes execute.
    """

    def __init__(self):
        self.product_id = BOOT_PRODUCT_ID
        self.stage = "bootloader"
        self.cpu_reset = False
        self.internal_ram: dict[int, int] = {}
        self.external_ram: dict[int, int] = {}
        self.cpucs = {CPUCS_EZUSB: 0, CPUCS_FX2: 0}
        self.control_transfers = 0
        self.reset_transfers = 0
        self.internal_bytes_written = 0
        self.external_bytes_written = 0
        self.simulated_delay_ms = 0
        self.release_count = 0
        self._writes_since_release = 0

    def consume(self, operation: BootOperation):
        if operation.kind == "delay":
            if operation.delay_ms < 0:
                raise ValueError("模拟等待不能为负")
            self.simulated_delay_ms += operation.delay_ms
            return
        if operation.kind == "reenumerate":
            if self.stage != "runtime_pending" or operation.product_id != RUNTIME_PRODUCT_ID:
                raise ValueError("运行固件尚未释放复位，不能切换到运行态 PID")
            self.product_id = RUNTIME_PRODUCT_ID
            self.stage = "runtime"
            return
        if operation.kind != "control" or operation.request_type != VENDOR_DEVICE_OUT or operation.index != 0:
            raise ValueError("仅支持设备接收者的 vendor OUT 控制传输")
        if operation.request not in (LOAD_INTERNAL, LOAD_EXTERNAL):
            raise ValueError("离线加载器仅支持 RAM 请求 A0 / A3，不支持 EEPROM 写入")
        if not 0 <= operation.address <= 0xFFFF or operation.address + operation.length > 0x10000:
            raise ValueError("RAM 地址超出 16 位范围")
        self.control_transfers += 1
        if operation.request == LOAD_INTERNAL and operation.address in self.cpucs:
            if operation.data not in (b"\0", b"\1"):
                raise ValueError("CPUCS 需要单字节 reset=0/1")
            self.reset_transfers += 1
            value = operation.data[0]
            self.cpucs[operation.address] = value
            # Both chip-family addresses are sent by the public loader. The
            # FX2 address completes one reset operation in this model.
            if operation.address == CPUCS_FX2:
                if self.cpucs[CPUCS_EZUSB] != value:
                    raise ValueError("CPUCS 两个地址的 reset 值不一致")
                if value:
                    self.cpu_reset = True
                else:
                    if not self.cpu_reset or not self._writes_since_release:
                        raise ValueError("需要先停止 CPU 并载入映像，再释放复位")
                    self.cpu_reset = False
                    self.release_count += 1
                    self._writes_since_release = 0
                    self.stage = "loader" if self.release_count == 1 else "runtime_pending"
            return
        if operation.request == LOAD_EXTERNAL:
            if self.stage != "loader" or self.cpu_reset:
                raise ValueError("A3 外部 RAM 写入要求辅助 loader 正在运行")
            if operation.address <= MAX_INTERNAL_RAM:
                raise ValueError("A3 数据地址应高于 0x1B3F")
            target = self.external_ram
            self.external_bytes_written += operation.length
        else:
            if not self.cpu_reset:
                raise ValueError("A0 内部 RAM 写入要求 CPU 保持复位")
            if operation.address > MAX_INTERNAL_RAM:
                raise ValueError("A0 数据起始地址应不高于 0x1B3F")
            target = self.internal_ram
            self.internal_bytes_written += operation.length
        target.update((operation.address + offset, value) for offset, value in enumerate(operation.data))
        self._writes_since_release += 1


def run_boot_simulation(loader_source_path: str | Path, device_source_path: str | Path) -> dict:
    """Parse actual archived firmware arrays and perform a RAM-only boot trial."""
    loader = read_firmware_records(loader_source_path)
    device = read_firmware_records(device_source_path)
    plan = plan_firmware_load(loader, device)
    simulator = BootloaderSimulator()
    for operation in plan:
        simulator.consume(operation)
    return {
        "source": "offline_firmware_simulation",
        "hardware_access": False,
        "loader_records": len(loader),
        "device_records": len(device),
        "data_records": len(_data_records(loader)) + len(_data_records(device)),
        "operations": len(plan),
        "control_transfers": simulator.control_transfers,
        "reset_transfers": simulator.reset_transfers,
        "internal_bytes_written": simulator.internal_bytes_written,
        "external_bytes_written": simulator.external_bytes_written,
        "internal_ram_bytes": len(simulator.internal_ram),
        "external_ram_bytes": len(simulator.external_ram),
        "simulated_delay_ms": simulator.simulated_delay_ms,
        "from_pid": BOOT_PRODUCT_ID,
        "to_pid": simulator.product_id,
        "stage": simulator.stage,
        "summary": "冷启动离线验证完成：C 数组 → RAM 加载顺序 → 模拟 PID 切换；未访问硬件或执行固件/FPGA。",
    }
