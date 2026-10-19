"""Prepare local DT9837 RAM images from the archived libuldaq C arrays.

This only converts local files. It does not access USB or install a driver.
The output stays under ignored resources/, with the original loader notice.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labdaq.firmware import read_firmware_records


def main():
    source = ROOT / "resources/opensource/libuldaq-1.2.2/src/usb/fw"
    loader_path, runtime_path = source / "DTFX2LDR_FW.c", source / "DT9837A_FW.c"
    result = {"format": "dt9837-ram-v1",
              "source": "libuldaq 1.2.2: DTFX2LDR_FW.c, DT9837A_FW.c",
              "loader_notice": loader_path.read_text(encoding="utf-8").split('#include', 1)[0]}
    for name, path in (("loader", loader_path), ("runtime", runtime_path)):
        result[name] = [{"address": r.address, "data": list(r.data)}
                        for r in read_firmware_records(path) if r.record_type == 0]
    output = ROOT / "resources/firmware/dt9837-ram.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
