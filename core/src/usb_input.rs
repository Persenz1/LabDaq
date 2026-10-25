//! Single-owner DT9837C USB input. ADC and status reads run independently of IPC.
use crate::{Config, dt9837 as dt};
use rusb::{Context, DeviceHandle, UsbContext};
use serde::{Deserialize, Serialize};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc::{self, Receiver},
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const COMMAND_TIMEOUT: Duration = Duration::from_secs(1);
const READ_TIMEOUT: Duration = Duration::from_millis(100);
const QUEUE_BLOCKS: usize = 64;

#[derive(Clone, Debug)]
pub struct Fault {
    pub reason: &'static str,
    pub message: String,
}

impl Fault {
    pub(crate) fn new(reason: &'static str, message: impl Into<String>) -> Self {
        Self {
            reason,
            message: message.into(),
        }
    }
    pub(crate) fn usb(action: &str, error: rusb::Error) -> Self {
        let hint = match error {
            rusb::Error::NotSupported => {
                "；Windows 上需要为此设备绑定 WinUSB，原厂驱动不能用于独立 USB 访问"
            }
            rusb::Error::Access => "；设备访问被拒绝，请检查驱动与权限",
            rusb::Error::Busy => "；设备被占用，请结束其他采集程序",
            rusb::Error::NoDevice => "；数采已断开，请重新连接后手动开始",
            _ => "",
        };
        Self::new(
            if error == rusb::Error::NoDevice {
                "device_disconnected"
            } else {
                "usb_error"
            },
            format!("{action}：{error}{hint}"),
        )
    }
}

impl std::fmt::Display for Fault {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.message.fmt(f)
    }
}

#[derive(Clone, Serialize)]
pub struct DeviceInfo {
    pub model: &'static str,
    pub serial: u32,
    pub bus: u8,
    pub ports: Vec<u8>,
    pub power_always_on: u8,
    pub control0: u16,
    pub control1: u16,
    pub control2: u16,
    pub channel_select: u16,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DeviceSelector {
    #[serde(default)]
    pub bus: u8,
    #[serde(default)]
    pub ports: Vec<u8>,
    #[serde(default)]
    pub vendor_name: Option<String>,
}

/// Enumeration does not claim interfaces, initialize a device, or start output.
pub fn enumerate() -> Result<Vec<serde_json::Value>, Fault> {
    let context = Context::new().map_err(|e| Fault::usb("初始化 USB 枚举", e))?;
    let devices = context.devices().map_err(|e| Fault::usb("枚举 USB", e))?;
    let mut found = Vec::new();
    for device in devices.iter() {
        let d = device
            .device_descriptor()
            .map_err(|e| Fault::usb("读取 USB 描述符", e))?;
        if d.vendor_id() == dt::VENDOR && [dt::BOOT, dt::RUNTIME].contains(&d.product_id()) {
            found.push(serde_json::json!({"bus":device.bus_number(),
                "ports":device.port_numbers().map_err(|e|Fault::usb("读取 USB 端口",e))?,
                "vendor_id":d.vendor_id(),"product_id":d.product_id(),
                "state":if d.product_id()==dt::BOOT {"cold"} else {"runtime"},
                "model":"DT9837 family; C subtype verified on connection"}));
        }
    }
    Ok(found)
}

pub(crate) fn write(handle: &DeviceHandle<Context>, data: &[u8; 64]) -> Result<(), Fault> {
    let n = handle
        .write_bulk(0x01, data, COMMAND_TIMEOUT)
        .map_err(|e| Fault::usb("发送设备命令", e))?;
    if n != data.len() {
        return Err(Fault::new("usb_error", "USB 命令写入不完整，未重发"));
    }
    Ok(())
}

fn read_reply(handle: &DeviceHandle<Context>, length: usize) -> Result<Vec<u8>, Fault> {
    // Full endpoint packet avoids a libusb overflow if a device replies with padding.
    let mut bytes = [0; 64];
    let n = handle
        .read_bulk(0x81, &mut bytes, COMMAND_TIMEOUT)
        .map_err(|e| Fault::usb("读取命令回复", e))?;
    if n != length {
        return Err(Fault::new(
            "protocol_error",
            format!("命令回复长度 {n}，预期 {length}"),
        ));
    }
    Ok(bytes[..n].to_vec())
}

fn read_word(handle: &DeviceHandle<Context>, address: u16) -> Result<u16, Fault> {
    write(handle, &dt::packet(20, &address.to_be_bytes()))?;
    Ok(u16::from_be_bytes(
        read_reply(handle, 2)?.try_into().unwrap(),
    ))
}

pub(crate) fn modify(
    handle: &DeviceHandle<Context>,
    address: u16,
    mask: u16,
    value: u16,
) -> Result<(), Fault> {
    write(handle, &dt::modify(address, mask, value))
}

pub(crate) fn set_fault(slot: &Mutex<Option<Fault>>, fault: Fault) {
    let mut slot = slot.lock().unwrap();
    if slot.is_none() {
        *slot = Some(fault);
    }
}

pub struct UsbInput {
    handle: Option<Arc<DeviceHandle<Context>>>,
    packet_size: usize,
    pub info: DeviceInfo,
    pub actual_rate: f64,
    pub blocks: Receiver<Vec<f64>>,
    fault: Arc<Mutex<Option<Fault>>>,
    stopping: Arc<AtomicBool>,
    workers: Vec<JoinHandle<()>>,
    powered: bool,
    started: bool,
    pub output: crate::output::Output,
}

impl UsbInput {
    pub fn connect_selected(selector: Option<&DeviceSelector>) -> Result<Self, Fault> {
        let ctx = Context::new().map_err(|e| Fault::usb("初始化 libusb", e))?;
        let devices = ctx.devices().map_err(|e| Fault::usb("枚举 USB", e))?;
        let mut matches = Vec::new();
        for device in devices.iter() {
            let d = device
                .device_descriptor()
                .map_err(|e| Fault::usb("读取 USB 描述符", e))?;
            if d.vendor_id() == dt::VENDOR && [dt::BOOT, dt::RUNTIME].contains(&d.product_id()) {
                if let Some(s) = selector {
                    if device.bus_number() != s.bus
                        || device
                            .port_numbers()
                            .map_err(|e| Fault::usb("读取 USB 端口", e))?
                            != s.ports
                    {
                        continue;
                    }
                }
                matches.push((device, d.product_id()));
            }
        }
        if matches.len() != 1 {
            return Err(Fault::new(
                "device_selection",
                format!(
                    "检测到 {} 台匹配的 DT9837 系列设备；请选择明确的 USB 总线和端口路径",
                    matches.len()
                ),
            ));
        }
        let (device, pid) = matches.remove(0);
        let device = if pid == dt::BOOT {
            crate::usb_boot::load(&ctx, device)?
        } else {
            device
        };
        let desc = device
            .active_config_descriptor()
            .map_err(|e| Fault::usb("读取端点配置", e))?;
        let interface = desc
            .interfaces()
            .flat_map(|i| i.descriptors())
            .find(|i| i.interface_number() == 0 && i.setting_number() == 0)
            .ok_or_else(|| Fault::new("protocol_error", "未找到 USB 接口 0"))?;
        let endpoints: Vec<_> = interface.endpoint_descriptors().collect();
        for address in [0x01, 0x06, 0x81, 0x82, 0x88] {
            if !endpoints
                .iter()
                .any(|e| e.address() == address && e.transfer_type() == rusb::TransferType::Bulk)
            {
                return Err(Fault::new(
                    "protocol_error",
                    format!("缺少 bulk 端点 {address:02x}"),
                ));
            }
        }
        let packet_size = endpoints
            .iter()
            .find(|e| e.address() == 0x82)
            .unwrap()
            .max_packet_size() as usize;
        let handle = device.open().map_err(|e| Fault::usb("打开 DT9837", e))?;
        handle
            .claim_interface(0)
            .map_err(|e| Fault::usb("占用 DT9837 接口", e))?;
        write(
            &handle,
            &dt::eeprom(&[0x05, 0x06, 0x08, 0x09, 0x0a, 0x0b, 0x40]),
        )?;
        let identity = read_reply(&handle, 7)?;
        if u16::from_le_bytes([identity[0], identity[1]]) != 4 {
            return Err(Fault::new(
                "wrong_model",
                "EEPROM 型号不是 DT9837C；未启动采集",
            ));
        }
        let (_, blocks) = mpsc::sync_channel(QUEUE_BLOCKS);
        let mut input = Self {
            info: DeviceInfo {
                model: "DT9837C",
                serial: u32::from_le_bytes(identity[2..6].try_into().unwrap()),
                bus: device.bus_number(),
                ports: device
                    .port_numbers()
                    .map_err(|e| Fault::usb("读取 USB 端口", e))?,
                power_always_on: identity[6],
                control0: 0,
                control1: 0,
                control2: 0,
                channel_select: 0,
            },
            actual_rate: 0.0,
            handle: Some(Arc::new(handle)),
            packet_size,
            blocks,
            fault: Arc::new(Mutex::new(None)),
            stopping: Arc::new(AtomicBool::new(false)),
            workers: Vec::new(),
            powered: false,
            started: false,
            output: crate::output::Output::new(),
        };
        let h = input.handle.as_ref().unwrap();
        input.powered = true; // Also attempt power-off if a power command completes only partially.
        write(h, &dt::packet(52, &[1]))?;
        thread::sleep(Duration::from_millis(100));
        write(h, &dt::subsystem(false))?;
        input.output.stop(h)?;
        // Connecting powers the module without starting ADC transfers. Use
        // unexcited DC inputs while idle; apply the requested settings at start.
        modify(h, 2, 0x0f00, 0)?;
        modify(h, 2, 0x00ff, 0)?;
        input.info.control0 = read_word(h, 0)?;
        input.info.control1 = read_word(h, 1)?;
        input.info.control2 = read_word(h, 2)?;
        input.info.channel_select = read_word(h, 0x10)?;
        Ok(input)
    }

    pub fn start(
        &mut self,
        config: &Config,
        external_rising: bool,
        sync_mode: &str,
        defer_start: bool,
    ) -> Result<(), Fault> {
        if self.started || !self.workers.is_empty() {
            return Err(Fault::new("configuration_error", "设备已经开始采集"));
        }
        let plan =
            dt::plan_clock(config.sample_rate).map_err(|e| Fault::new("configuration_error", e))?;
        let input = self;
        let h = input
            .handle
            .as_ref()
            .ok_or_else(|| Fault::new("device_disconnected", "设备尚未连接"))?;
        let packet_size = input.packet_size;
        let (sender, blocks) = mpsc::sync_channel(QUEUE_BLOCKS);
        input.blocks = blocks;
        input.stopping = Arc::new(AtomicBool::new(false));
        if let Some(error) = input.fault.lock().unwrap().clone() {
            return Err(error);
        }
        input.actual_rate = plan.actual_rate;
        // Independent internal timebase; allow PLL lock and 128 samples to settle.
        let sync = match sync_mode {
            "master" => 2,
            "slave" => 1,
            _ => 0,
        };
        modify(h, 0, 3, sync)?;
        thread::sleep(Duration::from_secs_f64(128.0 / plan.actual_rate + 0.003));
        modify(h, 1, 2, 2)?;
        let mut control2 = 0;
        for ch in 0..4 {
            let coupling = 1 << ch;
            let current = 1 << (ch + 8);
            let value = if config.iepe[ch] { current } else { 0 };
            // libuldaq ordering: disable excitation before switching to DC.
            if config.coupling[ch] == "AC" {
                modify(h, 2, coupling, coupling)?;
                modify(h, 2, current, value)?;
                control2 |= coupling;
            } else {
                modify(h, 2, current, value)?;
                modify(h, 2, coupling, 0)?;
            }
            control2 |= value;
            if config.ranges[ch] == 1.0 {
                control2 |= 1 << (ch + 4);
            }
        }
        modify(h, 1, 1, u16::from(plan.high_speed))?;
        let ticks = if plan.high_speed { 256.0 } else { 512.0 };
        thread::sleep(Duration::from_secs_f64(
            12288.0 / (plan.actual_rate * ticks),
        ));
        for (address, value) in plan.registers {
            write(h, &dt::packet(40, &[0, 0, 0, 0, 0x69, 1, address, value]))?;
        }
        modify(h, 0, 0x0300, plan.divider_register)?;
        modify(h, 2, 0x00f0, control2 & 0x00f0)?;
        let select = config.channels.iter().fold(0, |mask, ch| mask | (1 << ch));
        modify(h, 0x10, 0xff, select)?;
        if read_word(h, 0x82)? > 0x200 {
            let threshold = ((plan.actual_rate * 0.02).ceil() as u32)
                .next_power_of_two()
                .clamp(2, 1024);
            modify(h, 0x82, 0xffff, 2048 - threshold as u16)?;
        }
        input.info.control0 = read_word(h, 0)?;
        input.info.control1 = read_word(h, 1)?;
        input.info.control2 = read_word(h, 2)?;
        input.info.channel_select = read_word(h, 0x10)?;
        if input.info.control0 & 0x0303 != (plan.divider_register | sync)
            || input.info.control1 & 1 != u16::from(plan.high_speed)
            || input.info.control2 & 0x0fff != control2
            || input.info.channel_select & 0xff != select
        {
            return Err(Fault::new(
                "configuration_error",
                "设备寄存器回读与配置不符，未开始采样",
            ));
        }
        thread::sleep(Duration::from_secs_f64(256.0 / plan.actual_rate));
        h.clear_halt(0x82)
            .map_err(|e| Fault::usb("复位 ADC 端点数据切换", e))?;
        // Drain data left by an earlier stopped session, never concatenate it to this run.
        let mut drain = [0; 65536];
        let drain_start = Instant::now();
        loop {
            match h.read_bulk(0x82, &mut drain, Duration::from_millis(20)) {
                Err(rusb::Error::Timeout) | Ok(0) => break,
                Ok(_) if drain_start.elapsed() < Duration::from_secs(1) => {}
                Ok(_) => return Err(Fault::new("usb_error", "停止后 ADC 端点仍在持续输出")),
                Err(e) => return Err(Fault::usb("清空停止后的 ADC 数据", e)),
            }
        }
        let message_handle = h.clone();
        let stopping = input.stopping.clone();
        let fault = input.fault.clone();
        let output_active = input.output.active.clone();
        input.workers.push(thread::spawn(move || {
            let mut bytes = [0; 64];
            while !stopping.load(Ordering::Acquire) {
                match message_handle.read_bulk(0x88, &mut bytes, READ_TIMEOUT) {
                    Err(rusb::Error::Timeout) | Ok(0) => {} // Idle status endpoint is normal.
                    Ok(n) => {
                        if let Some(error) =
                            message_fault(&bytes[..n], output_active.load(Ordering::Acquire))
                        {
                            set_fault(&fault, error);
                            break;
                        }
                    }
                    Err(e) => {
                        set_fault(&fault, Fault::usb("读取设备状态", e));
                        break;
                    }
                }
            }
        }));
        let adc_handle = h.clone();
        let stopping = input.stopping.clone();
        let fault = input.fault.clone();
        let ranges = config
            .channels
            .iter()
            .map(|ch| config.ranges[*ch])
            .collect();
        let size = ((plan.actual_rate * config.channels.len() as f64 * 4.0 * 0.02).ceil() as usize)
            .div_ceil(packet_size)
            .max(1)
            * packet_size;
        let no_data_timeout = Duration::from_secs_f64(2.0 + 1024.0 / plan.actual_rate);
        input.workers.push(crate::usb_stream::spawn(
            adc_handle,
            size.min(65536),
            ranges,
            stopping,
            fault,
            sender,
            no_data_timeout,
            external_rising || sync_mode == "slave" || defer_start,
        )?);
        input.started = true;
        if !defer_start {
            write(
                h,
                &dt::subsystem_triggered(true, if external_rising { 1 } else { 0 }),
            )?;
        }
        Ok(())
    }

    pub fn arm(&self, external_rising: bool) -> Result<(), Fault> {
        let h = self
            .handle
            .as_ref()
            .ok_or_else(|| Fault::new("device_disconnected", "设备尚未连接"))?;
        write(
            h,
            &dt::subsystem_triggered(true, if external_rising { 1 } else { 0 }),
        )
    }

    pub fn error(&self) -> Option<Fault> {
        self.fault.lock().unwrap().clone()
    }

    pub fn start_output(&mut self, settings: crate::output::Settings) -> Result<(), Fault> {
        self.output
            .start(self.handle.as_ref().unwrap(), settings, self.fault.clone())
    }

    pub fn stop_output(&mut self) -> Result<(), Fault> {
        self.output.stop(
            self.handle
                .as_ref()
                .ok_or_else(|| Fault::new("device_disconnected", "设备尚未连接"))?,
        )
    }

    pub fn check_connection(&self) -> Result<(), Fault> {
        let handle = self
            .handle
            .as_ref()
            .ok_or_else(|| Fault::new("device_disconnected", "设备尚未连接"))?;
        read_word(handle, 0).map(|_| ())
    }

    pub fn stop(&mut self) -> Result<(), Fault> {
        let mut error = None;
        if let Some(h) = &self.handle {
            if self.started {
                let stop_error = write(h, &dt::subsystem(false)).err();
                if error.is_none() {
                    error = stop_error;
                }
                self.started = false;
            }
            self.stopping.store(true, Ordering::Release);
            for worker in self.workers.drain(..) {
                if worker.join().is_err() && error.is_none() {
                    error = Some(Fault::new("core_error", "USB 读取线程异常退出"));
                }
            }
        }
        error.map_or(Ok(()), Err)
    }

    pub fn disconnect(&mut self) -> Result<(), Fault> {
        let mut error = self.stop().err();
        if let Some(h) = &self.handle {
            if self.output.status.running || !self.output.status.zeroed {
                let output_error = self.output.stop(h).err();
                if error.is_none() {
                    error = output_error;
                }
            }
            if self.powered {
                if let Err(e) = write(h, &dt::packet(52, &[0])) {
                    if error.is_none() {
                        error = Some(e);
                    }
                }
                self.powered = false;
            }
        }
        self.handle.take(); // All worker handles have gone; releases interface and closes USB.
        error.map_or(Ok(()), Err)
    }
}

impl Drop for UsbInput {
    fn drop(&mut self) {
        let _ = self.disconnect();
    }
}

fn message_fault(bytes: &[u8], output_active: bool) -> Option<Fault> {
    if bytes.len() < 4 {
        return Some(Fault::new("protocol_error", "设备状态消息不足 4 字节"));
    }
    match u32::from_le_bytes(bytes[..4].try_into().unwrap()) {
        4 => Some(Fault::new(
            "adc_oversample",
            "设备报告 ADC oversample，采集已停止",
        )),
        5 => Some(Fault::new(
            "device_overrun",
            "设备 ADC FIFO 溢出，数据连续性中断",
        )),
        6..=8 if output_active => Some(Fault::new(
            "output_error",
            "设备报告 AO 欠载、过采样或意外结束，输入与输出已停止",
        )),
        1..=8 => None,
        code => Some(Fault::new(
            "protocol_error",
            format!("未知设备状态消息 {code}"),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn adc_errors_are_fatal_and_normal_notifications_are_not() {
        assert_eq!(
            message_fault(&5u32.to_le_bytes(), false).unwrap().reason,
            "device_overrun"
        );
        assert_eq!(
            message_fault(&4u32.to_le_bytes(), false).unwrap().reason,
            "adc_oversample"
        );
        assert!(message_fault(&2u32.to_le_bytes(), false).is_none());
        assert!(message_fault(&[5], false).is_some());
        assert!(message_fault(&99u32.to_le_bytes(), false).is_some());
        assert!(message_fault(&6u32.to_le_bytes(), false).is_none());
        assert_eq!(
            message_fault(&6u32.to_le_bytes(), true).unwrap().reason,
            "output_error"
        );
    }
}
