//! DT9837 RAM boot sequence, adapted from libuldaq 1.2.2 DtFx2FwLoader.
//! Copyright (c) 2018 Measurement Computing. MIT: THIRD_PARTY_NOTICES.md.
//! Firmware is a separate local resource, not taken from an installed SDK.
use crate::{dt9837 as dt, usb_input::Fault};
use rusb::{Context, Device, DeviceHandle, UsbContext};
use serde::Deserialize;
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

#[derive(Deserialize)]
struct Record {
    address: u16,
    data: Vec<u8>,
}

#[derive(Deserialize)]
struct Firmware {
    format: String,
    loader: Vec<Record>,
    runtime: Vec<Record>,
}

fn control(
    h: &DeviceHandle<Context>,
    request: u8,
    address: u16,
    bytes: &[u8],
) -> Result<(), Fault> {
    let n = h
        .write_control(0x40, request, address, 0, bytes, Duration::from_secs(2))
        .map_err(|e| Fault::usb("加载 RAM 固件", e))?;
    if n != bytes.len() {
        return Err(Fault::new(
            "firmware_error",
            "RAM 固件传输不完整，已停止加载",
        ));
    }
    Ok(())
}

fn reset(h: &DeviceHandle<Context>, value: u8) -> Result<(), Fault> {
    control(h, 0xa0, 0x7f92, &[value])?;
    control(h, 0xa0, 0xe600, &[value])
}

fn image(h: &DeviceHandle<Context>, records: &[Record]) -> Result<(), Fault> {
    for r in records.iter().filter(|r| r.address > 0x1b3f) {
        control(h, 0xa3, r.address, &r.data)?;
    }
    reset(h, 1)?;
    for r in records.iter().filter(|r| r.address <= 0x1b3f) {
        control(h, 0xa0, r.address, &r.data)?;
    }
    Ok(())
}

fn read_firmware(path: &Path) -> Result<Firmware, Fault> {
    let bytes = std::fs::read(path).map_err(|e| {
        Fault::new(
            "firmware_required",
            format!(
                "无法读取 {}：{e}；请先运行 tools/prepare_usb_firmware.py",
                path.display()
            ),
        )
    })?;
    let f: Firmware =
        serde_json::from_slice(&bytes).map_err(|e| Fault::new("firmware_error", e.to_string()))?;
    if f.format != "dt9837-ram-v1"
        || f.loader.is_empty()
        || f.runtime.is_empty()
        || f.loader.iter().any(|r| r.address > 0x1b3f)
        || f.loader.iter().chain(&f.runtime).any(|r| {
            r.data.is_empty()
                || r.data.len() > 16
                || r.address as usize + r.data.len() > 65536
                || (r.address..=r.address.saturating_add(r.data.len() as u16 - 1))
                    .any(|a| [0x7f92, 0xe600].contains(&a))
        })
    {
        return Err(Fault::new("firmware_error", "RAM 固件记录格式或地址无效"));
    }
    Ok(f)
}

pub fn load(ctx: &Context, device: Device<Context>) -> Result<Device<Context>, Fault> {
    let firmware = read_firmware(Path::new("resources/firmware/dt9837-ram.json"))?;
    let bus = device.bus_number();
    let ports = device
        .port_numbers()
        .map_err(|e| Fault::usb("读取冷启动端口", e))?;
    let h = device
        .open()
        .map_err(|e| Fault::usb("打开冷启动设备 PID 9839", e))?;
    h.claim_interface(0)
        .map_err(|e| Fault::usb("占用冷启动接口", e))?;
    reset(&h, 1)?;
    image(&h, &firmware.loader)?;
    reset(&h, 0)?;
    thread::sleep(Duration::from_millis(1));
    image(&h, &firmware.runtime)?;
    reset(&h, 1)?;
    reset(&h, 0)?;
    drop(h);
    drop(device);
    // The upstream device loader requires this FPGA/reenumeration settling time.
    thread::sleep(Duration::from_secs(5));
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        for d in ctx
            .devices()
            .map_err(|e| Fault::usb("固件加载后重新枚举", e))?
            .iter()
        {
            let desc = d
                .device_descriptor()
                .map_err(|e| Fault::usb("读取重新枚举描述符", e))?;
            if desc.vendor_id() == dt::VENDOR
                && desc.product_id() == dt::RUNTIME
                && d.bus_number() == bus
                && d.port_numbers()
                    .map_err(|e| Fault::usb("核实重新枚举端口", e))?
                    == ports
            {
                return Ok(d);
            }
        }
        if Instant::now() >= deadline {
            return Err(Fault::new(
                "firmware_error",
                "RAM 固件已发送，但原 USB 端口未出现运行态 PID 3998",
            ));
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn local_firmware_has_expected_two_stages() {
        let path = Path::new("../resources/firmware/dt9837-ram.json");
        if !path.exists() {
            return;
        } // Local-only firmware is not distributed with sources.
        let f = read_firmware(path).unwrap();
        assert_eq!(f.loader.len(), 59);
        assert_eq!(f.runtime.len(), 1034);
        assert!(f.runtime.iter().any(|r| r.address > 0x1b3f));
        assert!(f.runtime.iter().any(|r| r.address <= 0x1b3f));
    }
}
