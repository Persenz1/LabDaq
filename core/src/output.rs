//! DT9837C continuous AO: ±3 V, offset-binary 24-bit words in framed USB blocks.
//! Protocol adapted from libuldaq 1.2.2 (MIT); see THIRD_PARTY_NOTICES.md.
use crate::{
    dt9837 as dt,
    usb_input::{Fault, modify, set_fault, write},
};
use rusb::{Context, DeviceHandle};
use serde::{Deserialize, Serialize};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Settings {
    pub waveform: String,
    pub amplitude_v: f64,
    pub offset_v: f64,
    pub frequency_hz: f64,
    pub sample_rate: f64,
}

impl Settings {
    pub fn validate(&self) -> Result<(), String> {
        if !["dc", "sine", "square", "triangle"].contains(&self.waveform.as_str()) {
            return Err("AO 支持直流、正弦、方波和三角波".into());
        }
        if ![
            self.amplitude_v,
            self.offset_v,
            self.frequency_hz,
            self.sample_rate,
        ]
        .iter()
        .all(|x| x.is_finite())
            || self.amplitude_v < 0.0
        {
            return Err("AO 参数必须为有限数，峰值幅度不能为负".into());
        }
        let peak = if self.waveform == "dc" {
            0.0
        } else {
            self.amplitude_v
        };
        if self.offset_v.abs() + peak > 3.0 {
            return Err("AO 偏置绝对值 + 峰值幅度不能超过 3 V；直流电平由偏置设置".into());
        }
        let rate = dt::plan_output_clock(self.sample_rate)?.actual_rate;
        if self.waveform != "dc" && (self.frequency_hz <= 0.0 || self.frequency_hz >= rate / 2.0) {
            return Err("AO 频率必须大于 0 且小于实际更新率的一半".into());
        }
        Ok(())
    }

    fn volts(&self, index: u64, rate: f64) -> f64 {
        let phase = (index as f64 * self.frequency_hz / rate).fract();
        let wave = match self.waveform.as_str() {
            "sine" => (std::f64::consts::TAU * phase).sin(),
            "square" => {
                if phase < 0.5 {
                    1.0
                } else {
                    -1.0
                }
            }
            "triangle" => 1.0 - 4.0 * (phase - 0.5).abs(),
            _ => 0.0,
        };
        self.offset_v + self.amplitude_v * wave
    }

    fn block(&self, first: u64, count: usize, rate: f64) -> Vec<u8> {
        let mut bytes = vec![0; 512];
        bytes[..4].copy_from_slice(&(count as u32 * 4).to_le_bytes());
        for i in 0..count {
            bytes.extend(code(self.volts(first + i as u64, rate)).to_le_bytes());
        }
        bytes
    }
}

fn code(volts: f64) -> u32 {
    ((volts + 3.0) * (1u32 << 24) as f64 / 6.0)
        .round()
        .clamp(0.0, ((1u32 << 24) - 1) as f64) as u32
}

fn subsystem(start: bool) -> [u8; 64] {
    dt::packet(if start { 30 } else { 31 }, &[1, 0, 0, 0, 0, 0])
}

fn zero(handle: &DeviceHandle<Context>) -> Result<(), Fault> {
    let mut payload = vec![1, 0, 0, 0, 0]; // SS_DA, channel 0 (packed structure).
    payload.extend(code(0.0).to_le_bytes());
    write(handle, &dt::packet(53, &payload))
}

#[derive(Clone, Default, Serialize)]
pub struct Status {
    pub running: bool,
    pub settings: Option<Settings>,
    pub actual_rate: f64,
    pub zeroed: bool,
}

pub struct Output {
    pub status: Status,
    pub active: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl Output {
    pub fn new() -> Self {
        Self {
            status: Status::default(),
            active: Arc::new(AtomicBool::new(false)),
            worker: None,
        }
    }

    pub fn start(
        &mut self,
        handle: &Arc<DeviceHandle<Context>>,
        settings: Settings,
        fault: Arc<Mutex<Option<Fault>>>,
    ) -> Result<(), Fault> {
        settings
            .validate()
            .map_err(|e| Fault::new("configuration_error", e))?;
        if self.status.running {
            return Err(Fault::new("configuration_error", "请先停止 AO，再修改输出"));
        }
        let plan = dt::plan_output_clock(settings.sample_rate).unwrap();
        self.status.settings = Some(settings.clone());
        self.status.actual_rate = plan.actual_rate;
        self.status.zeroed = false;
        let result = self.start_stream(handle, settings, plan, fault);
        if result.is_err() {
            // Even a partly completed start must stop the DAC and attempt zero.
            let cleanup = self.stop(handle);
            if let Err(cleanup) = cleanup {
                return Err(Fault::new(
                    "output_error",
                    format!("{}；停止归零也失败：{cleanup}", result.unwrap_err()),
                ));
            }
        }
        result
    }

    fn start_stream(
        &mut self,
        h: &Arc<DeviceHandle<Context>>,
        settings: Settings,
        plan: dt::ClockPlan,
        fault: Arc<Mutex<Option<Fault>>>,
    ) -> Result<(), Fault> {
        h.clear_halt(0x06)
            .map_err(|e| Fault::usb("复位 AO 端点", e))?;
        for (address, value) in plan.registers {
            write(h, &dt::packet(40, &[1, 0, 0, 0, 0x69, 1, address, value]))?;
        }
        modify(h, 0, 0x0c00, plan.divider_register)?;
        write(h, &subsystem(false))?;
        modify(h, 6, 0x000a, 0x0008)?; // Disable/clear underrun latch during setup.
        modify(h, 0, 0x0060, 0)?;
        for (mask, value) in [(0, 4), (4, 0), (256, 256), (256, 0), (8, 8)] {
            modify(h, 3, mask, value)?;
        }
        write(h, &dt::packet(34, &32768u32.to_le_bytes()))?;
        let rate = plan.actual_rate;
        let count = 896; // libuldaq's maximum payload is 0xE00 bytes.
        // Pre-fill a portion of the FIFO before arming; the worker supplies the rest.
        let mut first = 0;
        for _ in 0..4 {
            send_block(h, &settings.block(first, count, rate))?;
            first += count as u64;
        }
        self.active.store(true, Ordering::Release);
        let active = self.active.clone();
        let handle = h.clone();
        self.worker = Some(thread::spawn(move || {
            while active.load(Ordering::Acquire) {
                if let Err(error) = send_block(&handle, &settings.block(first, count, rate)) {
                    if active.load(Ordering::Acquire) {
                        set_fault(&fault, error);
                    }
                    break;
                }
                first += count as u64;
            }
        }));
        thread::sleep(Duration::from_millis(1));
        write(h, &subsystem(true))?;
        self.status.running = true;
        Ok(())
    }

    pub fn stop(&mut self, handle: &DeviceHandle<Context>) -> Result<(), Fault> {
        self.active.store(false, Ordering::Release);
        // Wait for the current bounded write before resetting the device FIFO.
        let mut error = self
            .worker
            .take()
            .and_then(|w| w.join().err())
            .map(|_| Fault::new("output_error", "AO 输出线程异常退出"));
        for result in [
            write(handle, &subsystem(false)),
            modify(handle, 6, 0x000a, 0x0008),
            zero(handle),
        ] {
            if error.is_none() {
                error = result.err();
            }
        }
        self.status.running = false;
        self.status.zeroed = error.is_none();
        error.map_or(Ok(()), Err)
    }
}

fn send_block(handle: &DeviceHandle<Context>, bytes: &[u8]) -> Result<(), Fault> {
    let n = handle
        .write_bulk(0x06, bytes, Duration::from_millis(300))
        .map_err(|e| Fault::usb("写入 AO 波形", e))?;
    if n != bytes.len() {
        return Err(Fault::new("output_error", "AO 波形写入不完整，输出已中止"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn settings(waveform: &str) -> Settings {
        Settings {
            waveform: waveform.into(),
            amplitude_v: 1.0,
            offset_v: 0.25,
            frequency_hz: 1000.0,
            sample_rate: 48000.0,
        }
    }
    #[test]
    fn voltage_limits_and_nyquist_are_checked() {
        let mut s = settings("sine");
        assert!(s.validate().is_ok());
        s.offset_v = 2.01;
        assert!(s.validate().is_err());
        s.waveform = "dc".into();
        assert!(s.validate().is_ok());
        s.waveform = "sine".into();
        s.offset_v = 0.0;
        s.frequency_hz = 24000.0;
        assert!(s.validate().is_err());
        s.frequency_hz = f64::NAN;
        assert!(s.validate().is_err());
    }
    #[test]
    fn frames_preserve_phase_and_dac_scale() {
        assert_eq!(
            [code(-3.0), code(0.0), code(3.0)],
            [0, 1 << 23, (1 << 24) - 1]
        );
        let s = settings("sine");
        let all = s.block(0, 1000, 48000.0);
        assert_eq!(&all[..4], &4000u32.to_le_bytes());
        let mut parts = s.block(0, 123, 48000.0)[512..].to_vec();
        parts.extend(&s.block(123, 877, 48000.0)[512..]);
        assert_eq!(&all[512..], parts);
        assert_eq!(s.volts(12, 48000.0), 1.25);
        assert_eq!(settings("triangle").volts(24, 48000.0), 1.25);
        assert_eq!(settings("dc").volts(12, 48000.0), 0.25);
    }
    #[test]
    fn dac_clock_uses_512_ticks_even_at_96khz() {
        for rate in [10000.0, 48000.0, 96000.0] {
            let p = dt::plan_output_clock(rate).unwrap();
            let pv = ((p.registers[2].1 as u32 & 3) << 8) | p.registers[3].1 as u32;
            let qv = p.registers[4].1 as u32;
            let decoded = 48e6 * (2 * (pv + 4) + (qv >> 7)) as f64
                / ((qv & 127) + 2) as f64
                / p.registers[0].1 as f64
                / 512.0
                / (2 << (p.divider_register >> 10)) as f64;
            assert_eq!(decoded, p.actual_rate);
            assert!((decoded / rate - 1.0).abs() < 0.0005);
        }
    }
}
