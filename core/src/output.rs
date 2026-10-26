//! DT9837C continuous AO: ±3 V, offset-binary 24-bit words in framed USB blocks.
//! Protocol adapted from libuldaq 1.2.2 (MIT); see THIRD_PARTY_NOTICES.md.
use crate::{
    dt9837 as dt,
    usb_input::{Fault, modify, set_fault, write},
};
use rusb::{Context, DeviceHandle, UsbContext, ffi};
use serde::{Deserialize, Serialize};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct Settings {
    pub waveform: String,
    pub amplitude_v: f64,
    pub offset_v: f64,
    pub frequency_hz: f64,
    pub sample_rate: f64,
    pub end_frequency_hz: f64,
    pub sweep_seconds: f64,
    pub sweep_repeat: bool,
    pub duration_seconds: Option<f64>,
    pub burst_on_seconds: f64,
    pub burst_off_seconds: f64,
    pub burst_count: u64,
    pub fade_in_seconds: f64,
    pub fade_out_seconds: f64,
    pub seed: u64,
    pub bandwidth_hz: f64,
    pub repeat_noise: bool,
    pub muted: bool,
    pub trigger: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            waveform: "sine".into(),
            amplitude_v: 0.1,
            offset_v: 0.0,
            frequency_hz: 10.,
            sample_rate: 48000.,
            end_frequency_hz: 1000.,
            sweep_seconds: 1.,
            sweep_repeat: true,
            duration_seconds: None,
            burst_on_seconds: 0.,
            burst_off_seconds: 0.,
            burst_count: 0,
            fade_in_seconds: 0.,
            fade_out_seconds: 0.,
            seed: 1,
            bandwidth_hz: 0.,
            repeat_noise: false,
            muted: false,
            trigger: "immediate".into(),
        }
    }
}

impl Settings {
    pub fn validate(&self) -> Result<(), String> {
        if ![
            "dc",
            "sine",
            "square",
            "triangle",
            "sawtooth",
            "sweep_linear",
            "sweep_log",
            "uniform_noise",
            "gaussian_noise",
            "pink_noise",
        ]
        .contains(&self.waveform.as_str())
        {
            return Err("未知 AO 波形".into());
        }
        if ![
            self.amplitude_v,
            self.offset_v,
            self.frequency_hz,
            self.sample_rate,
            self.end_frequency_hz,
            self.sweep_seconds,
            self.burst_on_seconds,
            self.burst_off_seconds,
            self.fade_in_seconds,
            self.fade_out_seconds,
            self.bandwidth_hz,
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
        if self.amplitude_v > 3.0 || self.offset_v.abs() + peak > 3.0 {
            return Err("AO 偏置绝对值 + 峰值幅度不能超过 3 V；直流电平由偏置设置".into());
        }
        let rate = dt::plan_output_clock(self.sample_rate)?.actual_rate;
        self.validate_frequency(rate)?;
        if self.burst_on_seconds < 0.
            || self.burst_off_seconds < 0.
            || self.fade_in_seconds < 0.
            || self.fade_out_seconds < 0.
            || self
                .duration_seconds
                .is_some_and(|x| !x.is_finite() || x <= 0.)
            || (self.burst_on_seconds == 0.
                && (self.burst_off_seconds > 0. || self.burst_count > 0))
        {
            return Err("AO 时长、Burst、渐变或噪声带宽无效".into());
        }
        if !["immediate", "external_rising"].contains(&self.trigger.as_str()) {
            return Err("AO 启动方式为 immediate 或 external_rising".into());
        }
        if self.trigger == "external_rising" && self.end_seconds().is_some() {
            return Err("TTL 输出暂不支持自动停止计时；可使用连续波形并手动停止".into());
        }
        if [
            self.burst_on_seconds,
            self.burst_off_seconds,
            self.sweep_seconds,
        ]
        .into_iter()
        .chain(self.duration_seconds)
        .any(|t| t * rate >= u64::MAX as f64 / 4.)
            || self
                .end_seconds()
                .is_some_and(|t| !t.is_finite() || t * rate >= u64::MAX as f64 / 2.)
        {
            return Err("AO 序列时长超出可表示的采样帧数".into());
        }
        Ok(())
    }

    pub fn validate_frequency(&self, rate: f64) -> Result<(), String> {
        if !rate.is_finite() || rate <= 0. {
            return Err("AO 实际更新率无效".into());
        }
        if self.waveform != "dc"
            && !self.waveform.ends_with("noise")
            && (self.frequency_hz <= 0. || self.frequency_hz >= rate / 2.)
        {
            return Err(format!("AO 频率必须大于 0 且小于 {} Hz（实际更新率的一半）", rate / 2.));
        }
        if self.waveform.starts_with("sweep_")
            && (self.end_frequency_hz <= 0.
                || self.end_frequency_hz >= rate / 2.
                || self.sweep_seconds <= 0.)
        {
            return Err("扫频终频必须位于有效频带，扫频时间必须为正".into());
        }
        if self.bandwidth_hz < 0. || self.bandwidth_hz >= rate / 2. {
            return Err("AO 噪声带宽必须非负且小于实际更新率的一半".into());
        }
        Ok(())
    }

    pub fn end_seconds(&self) -> Option<f64> {
        let burst = (self.burst_on_seconds > 0. && self.burst_count > 0).then(|| {
            self.burst_count as f64 * (self.burst_on_seconds + self.burst_off_seconds)
                - self.burst_off_seconds
        });
        let sweep = (self.waveform.starts_with("sweep_") && !self.sweep_repeat)
            .then_some(self.sweep_seconds);
        [self.duration_seconds, burst, sweep]
            .into_iter()
            .flatten()
            .reduce(f64::min)
    }

    fn burst_frames(&self, rate: f64) -> (u64, u64) {
        let on = (self.burst_on_seconds * rate).round().max(1.) as u64;
        (
            on,
            on.saturating_add((self.burst_off_seconds * rate).round() as u64),
        )
    }

    pub(crate) fn end_frame(&self, rate: f64) -> Option<u64> {
        let duration = self
            .duration_seconds
            .map(|t| (t * rate).round().max(1.) as u64);
        let burst = (self.burst_on_seconds > 0. && self.burst_count > 0).then(|| {
            let (on, cycle) = self.burst_frames(rate);
            (self.burst_count - 1)
                .saturating_mul(cycle)
                .saturating_add(on)
        });
        let sweep = (self.waveform.starts_with("sweep_") && !self.sweep_repeat)
            .then(|| (self.sweep_seconds * rate).round().max(1.) as u64);
        [duration, burst, sweep].into_iter().flatten().min()
    }

    fn silent(&self, index: u64, rate: f64) -> bool {
        self.muted
            || self.end_frame(rate).is_some_and(|end| index >= end)
            || (self.burst_on_seconds > 0. && {
                let (on, cycle) = self.burst_frames(rate);
                index % cycle >= on
            })
    }

    pub fn volts(&self, index: u64, rate: f64) -> f64 {
        let t = index as f64 / rate;
        if self.silent(index, rate) {
            return 0.;
        }
        let (_, cycle) = self.burst_frames(rate);
        let local = if self.burst_on_seconds > 0. {
            (index % cycle) as f64 / rate
        } else {
            t
        };
        let mut envelope = if self.fade_in_seconds > 0. {
            (local / self.fade_in_seconds).min(1.)
        } else {
            1.
        };
        let end = if self.burst_on_seconds > 0. {
            Some(self.burst_on_seconds)
        } else {
            self.end_seconds()
        };
        if let Some(end) = end {
            if self.fade_out_seconds > 0. {
                envelope = envelope.min(((end - local) / self.fade_out_seconds).clamp(0., 1.));
            }
        }
        let phase = (index as f64 * self.frequency_hz / rate).fract();
        let noise_index = if self.repeat_noise && self.burst_on_seconds > 0. {
            index % cycle
        } else {
            index
        };
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
            "sawtooth" => 2. * phase - 1.,
            "sweep_linear" | "sweep_log" => {
                let elapsed =
                    (index % ((self.sweep_seconds * rate).round().max(1.) as u64)) as f64 / rate;
                let cycles = if self.waveform == "sweep_linear" {
                    self.frequency_hz * elapsed
                        + 0.5 * (self.end_frequency_hz - self.frequency_hz) / self.sweep_seconds
                            * elapsed
                            * elapsed
                } else {
                    let k = (self.end_frequency_hz / self.frequency_hz).ln() / self.sweep_seconds;
                    if k.abs() < 1e-12 {
                        self.frequency_hz * elapsed
                    } else {
                        self.frequency_hz * (k * elapsed).exp_m1() / k
                    }
                };
                (std::f64::consts::TAU * cycles).sin()
            }
            "uniform_noise" => 2. * uniform(self.seed, noise_index) - 1.,
            // amplitude_v is the bounded peak (four standard deviations before clipping).
            "gaussian_noise" => {
                ((-2. * (1. - uniform(self.seed, noise_index.wrapping_mul(2))).ln()).sqrt()
                    * (std::f64::consts::TAU
                        * uniform(self.seed, noise_index.wrapping_mul(2).wrapping_add(1)))
                    .cos()
                    / 4.)
                    .clamp(-1., 1.)
            }
            // Deterministic octave-row Voss noise plus a white row. Peak is bounded.
            "pink_noise" => {
                (0..16)
                    .map(|oct| 2. * uniform(self.seed.wrapping_add(oct), noise_index >> oct) - 1.)
                    .sum::<f64>()
                    / 16.
            }
            _ => 0.0,
        };
        envelope * (self.offset_v + self.amplitude_v * wave)
    }
}

fn uniform(seed: u64, index: u64) -> f64 {
    let mut x = seed.wrapping_add(index.wrapping_add(1).wrapping_mul(0x9e3779b97f4a7c15));
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
    ((x ^ (x >> 31)) >> 11) as f64 / 9007199254740992.
}

/// Four cascaded one-pole low-pass sections; bandwidth is the combined -3 dB
/// point (not an ideal brick-wall cutoff). State is retained across USB blocks.
pub struct Generator {
    state: [f64; 4],
}
impl Generator {
    pub fn new() -> Self {
        Self { state: [0.; 4] }
    }
    pub fn samples(&mut self, s: &Settings, first: u64, count: usize, rate: f64) -> Vec<f64> {
        let alpha = if s.bandwidth_hz > 0. {
            let omega = std::f64::consts::TAU * s.bandwidth_hz / rate;
            let target = 2f64.powf(-0.25);
            let k = target * 2. * (omega / 2.).sin().powi(2) / (1. - target);
            (k * k + 2. * k).sqrt() - k
        } else {
            1.
        };
        (0..count)
            .map(|i| {
                let index = first + i as u64;
                let mut v = s.volts(index, rate);
                if s.waveform.ends_with("noise") && s.bandwidth_hz > 0. {
                    for state in &mut self.state {
                        *state += alpha * (v - *state);
                        v = *state;
                    }
                    if s.silent(index, rate) {
                        self.state = [0.; 4];
                        v = 0.;
                    }
                }
                v
            })
            .collect()
    }
    fn block(&mut self, s: &Settings, first: u64, count: usize, rate: f64) -> Vec<u8> {
        let mut bytes = vec![0; 512];
        bytes[..4].copy_from_slice(&(count as u32 * 4).to_le_bytes());
        for v in self.samples(s, first, count, rate) {
            bytes.extend(code(v).to_le_bytes());
        }
        bytes
    }
}

pub fn preview(settings: &Settings, count: usize) -> Result<Vec<f64>, String> {
    settings.validate()?;
    if count > 262144 {
        return Err("AO 预览最多 262144 点".into());
    }
    Ok(Generator::new().samples(
        settings,
        0,
        count,
        dt::plan_output_clock(settings.sample_rate)?.actual_rate,
    ))
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
    pub completed: bool,
}

pub struct Output {
    pub status: Status,
    pub active: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    settings: Arc<Mutex<Settings>>,
    completed: Arc<AtomicBool>,
}

impl Output {
    pub fn new() -> Self {
        Self {
            status: Status::default(),
            active: Arc::new(AtomicBool::new(false)),
            worker: None,
            settings: Arc::new(Mutex::new(Settings::default())),
            completed: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn start(
        &mut self,
        handle: &Arc<DeviceHandle<Context>>,
        settings: Settings,
        fault: Arc<Mutex<Option<Fault>>>,
    ) -> Result<(), Fault> {
        self.refresh();
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
        self.status.completed = false;
        self.completed.store(false, Ordering::Release);
        *self.settings.lock().unwrap() = settings.clone();
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
        let mut generator = Generator::new();
        for _ in 0..4 {
            send_block(h, &generator.block(&settings, first, count, rate))?;
            first += count as u64;
        }
        self.active.store(true, Ordering::Release);
        let active = self.active.clone();
        let handle = h.clone();
        let live_settings = self.settings.clone();
        let completed = self.completed.clone();
        self.worker = Some(thread::spawn(move || {
            while active.load(Ordering::Acquire) {
                let settings = live_settings.lock().unwrap().clone();
                let block = generator.block(&settings, first, count, rate);
                if let Err(error) = send_block_cancellable(&handle, &block, &active) {
                    if active.load(Ordering::Acquire) {
                        set_fault(&fault, error);
                    }
                    break;
                }
                first += count as u64;
                // 32768-byte FIFO = 8192 samples. After that many trailing zeros
                // have entered the FIFO, all remaining output is zero.
                if settings
                    .end_frame(rate)
                    .is_some_and(|end| first >= end.saturating_add(8192 + count as u64))
                {
                    active.store(false, Ordering::Release);
                    let result = write(&handle, &subsystem(false)).and_then(|_| zero(&handle));
                    match result {
                        Ok(()) => completed.store(true, Ordering::Release),
                        Err(e) => set_fault(&fault, e),
                    }
                    break;
                }
            }
            active.store(false, Ordering::Release);
        }));
        thread::sleep(Duration::from_millis(1));
        let mut start = subsystem(true);
        if settings.trigger == "external_rising" {
            start[8] = 1;
        }
        write(h, &start)?;
        self.status.running = true;
        Ok(())
    }

    pub fn update(&mut self, settings: Settings) -> Result<(), Fault> {
        settings
            .validate()
            .map_err(|e| Fault::new("configuration_error", e))?;
        if !self.status.running {
            return Err(Fault::new("configuration_error", "AO 尚未运行"));
        }
        if self
            .status
            .settings
            .as_ref()
            .is_some_and(|s| s.sample_rate != settings.sample_rate || s.trigger != settings.trigger)
        {
            return Err(Fault::new(
                "configuration_error",
                "运行中不能更改 AO 更新率或启动触发",
            ));
        }
        *self.settings.lock().unwrap() = settings.clone();
        self.status.settings = Some(settings);
        Ok(())
    }

    pub fn refresh(&mut self) {
        if self.status.running && !self.active.load(Ordering::Acquire) {
            if let Some(worker) = self.worker.take() {
                let _ = worker.join();
            }
            self.status.running = false;
            self.status.completed = self.completed.load(Ordering::Acquire);
            self.status.zeroed = self.status.completed;
        }
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

struct OutCompletion {
    done: AtomicBool,
    status: AtomicI32,
    actual: AtomicUsize,
}

extern "system" fn output_completed(transfer: *mut ffi::libusb_transfer) {
    // SAFETY: send_block_cancellable keeps the box, buffer, and transfer alive
    // until this completion is observed. No transfer is resubmitted here.
    unsafe {
        let transfer = &*transfer;
        let state = &*(transfer.user_data as *const OutCompletion);
        state.status.store(transfer.status, Ordering::Relaxed);
        state
            .actual
            .store(transfer.actual_length.max(0) as usize, Ordering::Relaxed);
        state.done.store(true, Ordering::Release);
    }
}

fn send_block_cancellable(
    handle: &DeviceHandle<Context>,
    bytes: &[u8],
    active: &AtomicBool,
) -> Result<(), Fault> {
    let state = Box::new(OutCompletion {
        done: AtomicBool::new(false),
        status: AtomicI32::new(0),
        actual: AtomicUsize::new(0),
    });
    let mut buffer = bytes.to_vec();
    // SAFETY: transfer owns no Rust data. Cancellation is drained before either
    // its pointed-to completion box or its data buffer can be released.
    unsafe {
        let transfer = ffi::libusb_alloc_transfer(0);
        if transfer.is_null() {
            return Err(Fault::new("usb_error", "无法分配 AO USB 传输"));
        }
        ffi::libusb_fill_bulk_transfer(
            transfer,
            handle.as_raw(),
            0x06,
            buffer.as_mut_ptr(),
            buffer.len() as i32,
            output_completed,
            (&*state as *const OutCompletion).cast_mut().cast(),
            0,
        );
        let result = ffi::libusb_submit_transfer(transfer);
        if result != 0 {
            ffi::libusb_free_transfer(transfer);
            return Err(Fault::new(
                "usb_error",
                format!("提交 AO 传输失败（{result}）"),
            ));
        }
        let mut cancelled = false;
        let mut event_error = None;
        while !state.done.load(Ordering::Acquire) {
            if (!active.load(Ordering::Acquire) || event_error.is_some()) && !cancelled {
                ffi::libusb_cancel_transfer(transfer);
                cancelled = true;
            }
            if let Err(e) = handle
                .context()
                .handle_events(Some(Duration::from_millis(20)))
            {
                event_error.get_or_insert_with(|| Fault::usb("处理 AO USB 事件", e));
            }
        }
        ffi::libusb_free_transfer(transfer);
        if !active.load(Ordering::Acquire) {
            return Ok(());
        }
        if let Some(error) = event_error {
            return Err(error);
        }
        if state.status.load(Ordering::Relaxed) != ffi::constants::LIBUSB_TRANSFER_COMPLETED
            || state.actual.load(Ordering::Relaxed) != bytes.len()
        {
            return Err(Fault::new(
                "output_error",
                "AO USB 传输失败或不完整，输出已停止",
            ));
        }
    }
    Ok(())
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
            ..Settings::default()
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
        s = settings("dc");
        s.amplitude_v = 3.0001;
        assert!(s.validate().is_err());
        s.amplitude_v = 3.;
        s.offset_v = -3.;
        assert!(s.validate().is_ok());
        s.offset_v = -3.0001;
        assert!(s.validate().is_err());
    }
    #[test]
    fn output_presets_and_actual_clock_boundaries() {
        for rate in [10000., 20000., 48000., 96000.] {
            let mut s = settings("sine");
            s.sample_rate = rate;
            s.frequency_hz = rate / 2. - 0.001;
            assert!(s.validate().is_ok());
            assert!(s.validate_frequency(rate).is_ok());
            assert!(s.validate_frequency(rate - 1.).is_err());
            s.frequency_hz = rate / 2.;
            assert!(s.validate_frequency(rate).is_err());
        }
        let mut s = settings("sine");
        s.amplitude_v = 1.;
        for offset in [-2., 2.] {
            s.offset_v = offset;
            assert!(s.validate().is_ok());
            for i in 0..480 {
                assert!(s.volts(i, 48000.).abs() <= 3.);
            }
            s.offset_v = offset * 1.0001;
            assert!(s.validate().is_err());
        }
    }
    #[test]
    fn frames_preserve_phase_and_dac_scale() {
        assert_eq!(
            [code(-3.0), code(0.0), code(3.0)],
            [0, 1 << 23, (1 << 24) - 1]
        );
        let s = settings("sine");
        let all = Generator::new().block(&s, 0, 1000, 48000.0);
        assert_eq!(&all[..4], &4000u32.to_le_bytes());
        let mut generator = Generator::new();
        let mut parts = generator.block(&s, 0, 123, 48000.0)[512..].to_vec();
        parts.extend(&generator.block(&s, 123, 877, 48000.0)[512..]);
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

    #[test]
    fn all_waveforms_are_bounded_and_block_partition_invariant() {
        for waveform in [
            "dc",
            "sine",
            "square",
            "triangle",
            "sawtooth",
            "sweep_linear",
            "sweep_log",
            "uniform_noise",
            "gaussian_noise",
            "pink_noise",
        ] {
            let s = Settings {
                waveform: waveform.into(),
                amplitude_v: 2.5,
                offset_v: 0.5,
                bandwidth_hz: if waveform.ends_with("noise") {
                    2000.
                } else {
                    0.
                },
                ..Settings::default()
            };
            s.validate().unwrap();
            let rate = 48000.;
            let all = Generator::new().samples(&s, 0, 12000, rate);
            let mut g = Generator::new();
            let mut parts = Vec::new();
            let mut start = 0;
            for count in [1, 17, 896, 2048, 9038] {
                parts.extend(g.samples(&s, start, count, rate));
                start += count as u64;
            }
            assert_eq!(all, parts, "{waveform}");
            assert!(
                all.iter().all(|v| v.is_finite() && v.abs() <= 3.),
                "{waveform}"
            );
        }
    }

    #[test]
    fn burst_duration_fades_and_muting_have_sample_boundaries() {
        let mut s = Settings {
            waveform: "dc".into(),
            offset_v: 1.,
            burst_on_seconds: 0.01,
            burst_off_seconds: 0.005,
            burst_count: 2,
            fade_in_seconds: 0.002,
            fade_out_seconds: 0.002,
            ..Settings::default()
        };
        let rate = 48000.;
        assert_eq!(s.volts(0, rate), 0.);
        assert_eq!(s.volts(96, rate), 1.);
        assert!((s.volts(432, rate) - 0.5).abs() < 1e-12);
        assert_eq!(s.volts(480, rate), 0.);
        assert_eq!(s.volts(720, rate), 0.);
        assert_eq!(s.volts(1200, rate), 0.);
        assert!((s.end_seconds().unwrap() - 0.025).abs() < 1e-12);
        s.muted = true;
        assert!(preview(&s, 1500).unwrap().iter().all(|v| *v == 0.));
    }

    #[test]
    fn noise_statistics_seed_and_bandwidth_match_declared_semantics() {
        let mut s = Settings {
            waveform: "uniform_noise".into(),
            amplitude_v: 1.,
            offset_v: 0.,
            ..Settings::default()
        };
        let uniform = preview(&s, 100000).unwrap();
        let mean = uniform.iter().sum::<f64>() / uniform.len() as f64;
        let variance = uniform.iter().map(|v| v * v).sum::<f64>() / uniform.len() as f64;
        assert!(mean.abs() < 0.01);
        assert!((variance - 1. / 3.).abs() < 0.01);
        s.seed = 2;
        assert_ne!(&uniform[..20], &preview(&s, 20).unwrap());
        s.waveform = "gaussian_noise".into();
        let gaussian = preview(&s, 100000).unwrap();
        let rms = (gaussian.iter().map(|x| x * x).sum::<f64>() / gaussian.len() as f64).sqrt();
        assert!((rms - 0.25).abs() < 0.01);
        s.waveform = "uniform_noise".into();
        s.bandwidth_hz = 200.;
        let filtered = preview(&s, 100000).unwrap();
        assert!(filtered[2000..].iter().map(|x| x * x).sum::<f64>() / 98000. < variance / 10.);
    }

    #[test]
    fn sweep_phase_is_integral_of_instantaneous_frequency() {
        let mut s = Settings {
            waveform: "sweep_linear".into(),
            frequency_hz: 10.,
            end_frequency_hz: 100.,
            sweep_seconds: 2.,
            sweep_repeat: false,
            amplitude_v: 1.,
            ..Settings::default()
        };
        let t = 0.123;
        let index = (t * 48000.) as u64;
        let expected = (std::f64::consts::TAU * (10. * t + 22.5 * t * t)).sin();
        assert!((s.volts(index, 48000.) - expected).abs() < 1e-10);
        s.waveform = "sweep_log".into();
        let k = 10f64.ln() / 2.;
        let expected = (std::f64::consts::TAU * 10. * (k * t).exp_m1() / k).sin();
        assert!((s.volts(index, 48000.) - expected).abs() < 1e-10);
        assert_eq!(s.volts(96000, 48000.), 0.);
    }

    #[test]
    fn invalid_advanced_parameters_and_oversized_previews_are_rejected() {
        let mut s = Settings::default();
        s.burst_count = 3;
        assert!(s.validate().is_err());
        s.burst_count = 0;
        s.duration_seconds = Some(f64::INFINITY);
        assert!(s.validate().is_err());
        s.duration_seconds = None;
        s.bandwidth_hz = 24000.;
        assert!(s.validate().is_err());
        s.bandwidth_hz = 0.;
        s.waveform = "sweep_log".into();
        s.end_frequency_hz = 0.;
        assert!(s.validate().is_err());
        assert!(preview(&Settings::default(), 262145).is_err());
    }

    #[test]
    fn noise_burst_repeats_exact_samples_and_has_no_filter_tail_in_off_interval() {
        let s = Settings {
            waveform: "uniform_noise".into(),
            amplitude_v: 1.,
            repeat_noise: true,
            burst_on_seconds: 0.001,
            burst_off_seconds: 0.001,
            burst_count: 7,
            bandwidth_hz: 2000.,
            ..Settings::default()
        };
        let samples = Generator::new().samples(&s, 0, 7 * 96, 48000.);
        for cycle in 0..7 {
            assert_eq!(&samples[cycle * 96..cycle * 96 + 48], &samples[..48]);
            assert!(
                samples[cycle * 96 + 48..cycle * 96 + 96]
                    .iter()
                    .all(|v| *v == 0.)
            );
        }
        assert_eq!(s.end_frame(48000.), Some(6 * 96 + 48));
    }

    #[test]
    fn noise_filter_combined_response_has_declared_three_db_bandwidth() {
        let s = Settings {
            waveform: "uniform_noise".into(),
            amplitude_v: 0.,
            offset_v: 1.,
            bandwidth_hz: 500.,
            ..Settings::default()
        };
        let step = Generator::new().samples(&s, 0, 16384, 48000.);
        let mut previous = 0.;
        let (mut re, mut im) = (0., 0.);
        for (i, y) in step.iter().enumerate() {
            let impulse = y - previous;
            previous = *y;
            let angle = std::f64::consts::TAU * 500. * i as f64 / 48000.;
            re += impulse * angle.cos();
            im -= impulse * angle.sin();
        }
        assert!((re.hypot(im) - 2f64.sqrt().recip()).abs() < 1e-9);
        assert!((step.last().unwrap() - 1.).abs() < 1e-12);
    }
}
