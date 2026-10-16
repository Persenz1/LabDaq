from pathlib import Path

import pytest

from labdaq.firmware import (
    BootOperation, BootloaderSimulator, IntelHexRecord, parse_c_records,
    plan_firmware_load, run_boot_simulation,
)


def test_c_initializer_length_padding_and_terminator():
    source = """
    // Record initializer from the IntelHexRec C representation.
    INTEL_HEX_RECORD fixture[] = {
        3, 0x0000, 0, {0x02, 0x01, 0x20},
        4, 0x0040, 0, {0x10}, /* C zero fills omitted bytes */
        0, 0x0000, 1, {0}
    };
    """
    records = parse_c_records(source)
    assert records == (IntelHexRecord(0, b"\x02\x01\x20"),
                       IntelHexRecord(0x40, b"\x10\0\0\0"), IntelHexRecord(0, b"", 1))
    with pytest.raises(ValueError, match="终止"):
        parse_c_records("INTEL_HEX_RECORD x[]={1,0,0,{0x12}};")


def test_boot_plan_matches_golden_order_values_lengths_and_boundary():
    # Deliberately interleave source addresses: downloadIntelHex must output
    # external records first, then reset, then internal records in source order.
    loader = [IntelHexRecord(0x0010, b"\xA1\xA2"), IntelHexRecord(0, b"", 1)]
    device = [IntelHexRecord(0x1B3F, b"\x11"), IntelHexRecord(0x2300, b"\x22\x23"),
              IntelHexRecord(0x1B40, b"\x44"), IntelHexRecord(0x0020, b"\x55\x66"),
              IntelHexRecord(0, b"", 1), IntelHexRecord(0x40, b"\xFF")]
    plan = plan_firmware_load(loader, device)
    controls = [(op.request, op.address, op.length, op.data) for op in plan if op.kind == "control"]
    expected = [
        (0xA0, 0x7F92, 1, b"\1"), (0xA0, 0xE600, 1, b"\1"),
        (0xA0, 0x7F92, 1, b"\1"), (0xA0, 0xE600, 1, b"\1"),
        (0xA0, 0x0010, 2, b"\xA1\xA2"),
        (0xA0, 0x7F92, 1, b"\0"), (0xA0, 0xE600, 1, b"\0"),
        (0xA3, 0x2300, 2, b"\x22\x23"), (0xA3, 0x1B40, 1, b"\x44"),
        (0xA0, 0x7F92, 1, b"\1"), (0xA0, 0xE600, 1, b"\1"),
        (0xA0, 0x1B3F, 1, b"\x11"), (0xA0, 0x0020, 2, b"\x55\x66"),
        (0xA0, 0x7F92, 1, b"\1"), (0xA0, 0xE600, 1, b"\1"),
        (0xA0, 0x7F92, 1, b"\0"), (0xA0, 0xE600, 1, b"\0"),
    ]
    assert controls == expected
    assert all(op.request_type == 0x40 and op.index == 0 for op in plan if op.kind == "control")
    assert plan[7] == BootOperation("delay", delay_ms=1)
    assert plan[-2] == BootOperation("delay", delay_ms=5000)
    assert plan[-1] == BootOperation("reenumerate", product_id=0x3998)
    simulator = BootloaderSimulator()
    assert simulator.product_id == 0x9839
    for operation in plan:
        simulator.consume(operation)
    assert simulator.stage == "runtime" and simulator.product_id == 0x3998
    assert simulator.internal_ram == {0x10: 0xA1, 0x11: 0xA2, 0x1B3F: 0x11, 0x20: 0x55, 0x21: 0x66}
    assert simulator.external_ram == {0x2300: 0x22, 0x2301: 0x23, 0x1B40: 0x44}
    assert simulator.reset_transfers == 12
    assert simulator.simulated_delay_ms == 5001


def test_boot_simulator_rejects_wrong_sequence_and_non_ram_requests():
    simulator = BootloaderSimulator()
    with pytest.raises(ValueError, match="loader"):
        simulator.consume(BootOperation("control", 0xA3, 0x2300, b"\x12"))
    with pytest.raises(ValueError, match="复位"):
        simulator.consume(BootOperation("control", 0xA0, 0x0010, b"\x12"))
    with pytest.raises(ValueError, match="EEPROM"):
        simulator.consume(BootOperation("control", 0xA2, 0x0010, b"\x12"))
    with pytest.raises(ValueError, match="运行固件"):
        simulator.consume(BootOperation("reenumerate", product_id=0x3998))


def test_archived_firmware_record_counts_and_complete_offline_boot():
    firmware = Path(__file__).resolve().parents[1] / "resources" / "opensource" / "libuldaq-1.2.2" / "src" / "usb" / "fw"
    summary = run_boot_simulation(firmware / "DTFX2LDR_FW.c", firmware / "DT9837A_FW.c")
    # Counts independently documented by the SYS/C-array binary comparison.
    assert summary["loader_records"] == 60
    assert summary["device_records"] == 1035
    assert summary["data_records"] == 1093
    assert summary["control_transfers"] == 1105
    assert summary["operations"] == 1108
    assert summary["reset_transfers"] == 12
    assert summary["simulated_delay_ms"] == 5001
    assert summary["from_pid"] == 0x9839 and summary["to_pid"] == 0x3998
    assert summary["stage"] == "runtime" and not summary["hardware_access"]
