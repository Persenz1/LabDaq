//! Independent acquisition and recording. USB input uses no vendor runtime.
mod dt9837;
mod signal;
mod usb_boot;
mod usb_input;
mod usb_stream;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::mpsc::{self, SyncSender};
use std::thread;
use std::time::{Duration, Instant};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

const SIMULATION_BLOCK_SECONDS: f64 = 1.0 / 60.0;

#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Source {
    #[default]
    Simulation,
    Usb,
}

fn utc_now() -> String {
    OffsetDateTime::now_utc().format(&Rfc3339).unwrap()
}

#[derive(Clone, Deserialize, Serialize)]
struct Config {
    sample_rate: f64,
    channels: Vec<usize>,
    ranges: [f64; 4],
    coupling: [String; 4],
    iepe: [bool; 4],
}

#[derive(Clone, Deserialize, Serialize)]
struct Channel {
    index: usize,
    name: String,
    unit: String,
    sensitivity_mv_per_unit: f64,
    zero_volts: f64,
    inverted: bool,
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default)]
struct RecordingOptions {
    duration_seconds: Option<f64>,
    notes: String,
    group_number: u64,
}

impl RecordingOptions {
    fn target_scans(&self, sample_rate: f64) -> Result<Option<u64>, String> {
        let Some(seconds) = self.duration_seconds else {
            return Ok(None);
        };
        let scans = (sample_rate * seconds).round().max(1.0);
        if !seconds.is_finite() || seconds <= 0.0 || !scans.is_finite() || scans >= u64::MAX as f64
        {
            return Err("记录时长必须为正有限数，且对应采样帧数在 1 到 u64 范围内".into());
        }
        Ok(Some(scans as u64))
    }
}

#[derive(Clone, Deserialize, Serialize)]
struct Profile {
    #[serde(default)]
    source: Source,
    #[serde(default)]
    device: Value,
    config: Config,
    actual_rate: f64,
    channels: [Channel; 4],
    signal_settings: signal::Signals,
    experiment: String,
    recording_root: PathBuf,
    #[serde(default)]
    recording_options: RecordingOptions,
}

impl Profile {
    fn validate(&self) -> Result<(), String> {
        let c = &self.config;
        if !(195.313..=105469.0).contains(&c.sample_rate)
            || !(190.0..=106000.0).contains(&self.actual_rate)
            || c.channels.is_empty()
            || c.channels.iter().any(|i| *i > 3)
            || c.channels.windows(2).any(|w| w[0] >= w[1])
            || c.ranges.iter().any(|r| *r != 1.0 && *r != 10.0)
            || c.coupling.iter().any(|s| s != "AC" && s != "DC")
            || self
                .channels
                .iter()
                .enumerate()
                .any(|(i, ch)| ch.index != i || ch.sensitivity_mv_per_unit <= 0.0)
        {
            return Err("采样率、通道或换算配置无效".into());
        }
        self.recording_options.target_scans(self.actual_rate)?;
        if self.source == Source::Simulation {
            self.signal_settings.validate()
        } else {
            Ok(())
        }
    }
}

struct Recorder {
    file: File,
    path: PathBuf,
    metadata: Value,
    frames: u64,
    width: usize,
}

impl Recorder {
    fn new(
        path: PathBuf,
        p: &Profile,
        start: u64,
        target_sample_count: Option<u64>,
        acquisition_started_at_utc: Option<&str>,
    ) -> Result<Self, String> {
        let parent = path.parent().ok_or("记录路径无父目录")?;
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        fs::create_dir(&path).map_err(|e| e.to_string())?; // Never overwrite a group.
        let mut config = serde_json::to_value(&p.config).unwrap();
        config["sample_rate"] = json!(p.actual_rate);
        config["record_start_scan"] = json!(start);
        config["input"] = match p.source {
            Source::Simulation => json!({"backend": "rust-core-v1", "scenario": "custom",
                "noise_algorithm": "splitmix64-box-muller-v1", "signal_settings": p.signal_settings}),
            Source::Usb => json!({"backend": "rust-usb-v1", "device": p.device,
                "startup_discard_scans": dt9837::GROUP_DELAY, "adc_encoding": "offset-binary-24-in-u32-le",
                "timebase": "hardware-sample-index", "continuity": "no-device-sequence-counter"}),
        };
        let channels: Vec<_> = p.config.channels.iter().map(|i| &p.channels[*i]).collect();
        let metadata = json!({"format": "labdaq-raw-voltage", "format_version": 1,
            "dtype": "<f8", "layout": "sample-major", "voltage_unit": "V",
            "sample_rate": p.actual_rate, "channels": channels, "source": p.source,
            "acquisition_config": config, "experiment": p.experiment,
            "recording_options": p.recording_options, "target_sample_count": target_sample_count,
            "acquisition_started_at_utc": acquisition_started_at_utc,
            "started_at_utc": utc_now(), "sample_count": 0, "duration_seconds": 0.0,
            "recording_segments": [], "recording_paused": false,
            "active_segment": {"file_start_scan": 0, "source_start_scan": start},
            "finalized": false});
        let file = File::create(path.join("raw.f64")).map_err(|e| e.to_string())?;
        let result = Self {
            file,
            path,
            metadata,
            frames: 0,
            width: channels.len(),
        };
        result.save_metadata()?;
        Ok(result)
    }

    fn save_metadata(&self) -> Result<(), String> {
        let temp = self.path.join("metadata.json.tmp");
        fs::write(&temp, serde_json::to_vec_pretty(&self.metadata).unwrap())
            .map_err(|e| e.to_string())?;
        fs::rename(temp, self.path.join("metadata.json")).map_err(|e| e.to_string())
    }

    fn write(&mut self, values: &[f64], source_start_scan: u64) -> Result<(), String> {
        if values.is_empty() {
            return Ok(());
        }
        let bytes: Vec<u8> = values.iter().flat_map(|v| v.to_le_bytes()).collect();
        let write_error = self.file.write_all(&bytes).err();
        let written_frames = if write_error.is_some() {
            // A disk error may leave part of the last frame. Keep complete frames only.
            let size = self.file.metadata().map_err(|e| e.to_string())?.len();
            let frame_bytes = self.width as u64 * 8;
            let frames = size / frame_bytes;
            self.file
                .set_len(frames * frame_bytes)
                .map_err(|e| e.to_string())?;
            frames - self.frames
        } else {
            (values.len() / self.width) as u64
        };
        if written_frames > 0 {
            let new_segment =
                self.metadata["active_segment"]["file_start_scan"].as_u64() == Some(self.frames);
            let segments = self.metadata["recording_segments"].as_array_mut().unwrap();
            if let Some(last) = segments.last_mut().filter(|last| {
                !new_segment
                    && last["source_start_scan"].as_u64().unwrap()
                        + last["sample_count"].as_u64().unwrap()
                        == source_start_scan
            }) {
                last["sample_count"] =
                    json!(last["sample_count"].as_u64().unwrap() + written_frames);
            } else {
                segments.push(json!({"file_start_scan": self.frames,
                    "source_start_scan": source_start_scan, "sample_count": written_frames}));
            }
            self.frames += written_frames;
            self.metadata["sample_count"] = json!(self.frames);
        }
        if let Some(error) = write_error {
            return Err(error.to_string());
        }
        Ok(())
    }

    fn set_paused(&mut self, paused: bool, source_start_scan: u64) -> Result<(), String> {
        self.file.flush().map_err(|e| e.to_string())?;
        self.metadata["recording_paused"] = json!(paused);
        self.metadata["active_segment"] = if paused {
            Value::Null
        } else {
            json!({"file_start_scan": self.frames, "source_start_scan": source_start_scan})
        };
        self.save_metadata()
    }

    fn close(mut self, reason: &str, error: &str) -> Result<(), String> {
        self.file.flush().map_err(|e| e.to_string())?;
        self.metadata["sample_count"] = json!(self.frames);
        self.metadata["duration_seconds"] =
            json!(self.frames as f64 / self.metadata["sample_rate"].as_f64().unwrap());
        self.metadata["finalized"] = json!(true);
        self.metadata["recording_paused"] = json!(false);
        self.metadata["active_segment"] = Value::Null;
        self.metadata["stopped_at_utc"] = json!(utc_now());
        self.metadata["termination_reason"] = json!(reason);
        self.metadata["error"] = if error.is_empty() {
            Value::Null
        } else {
            json!(error)
        };
        self.save_metadata()
    }
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Command {
    Poll {
        #[serde(default)]
        seconds: f64,
        #[serde(default)]
        after_scan: Option<u64>,
    },
    Connect {
        profile: Box<Profile>,
    },
    Disconnect,
    Start {
        profile: Box<Profile>,
        recording_path: Option<PathBuf>,
    },
    StartRecording {
        path: PathBuf,
        channels: [Channel; 4],
        experiment: String,
        #[serde(default)]
        recording_options: RecordingOptions,
    },
    SetChannels {
        channels: [Channel; 4],
    },
    StopRecording,
    PauseRecording,
    ResumeRecording,
    Stop,
    Shutdown,
}

#[derive(Deserialize)]
struct Request {
    token: String,
    #[serde(flatten)]
    command: Command,
}

struct State {
    usb: Option<usb_input::UsbInput>,
    profile: Option<Profile>,
    running: bool,
    acquisition_started_at_utc: Option<String>,
    recorder: Option<Recorder>,
    recording_paused: bool,
    last_recording: Option<PathBuf>,
    recorded_scans: u64,
    recording_target_scans: Option<u64>,
    recording_remaining_scans: Option<u64>,
    total_scans: u64,
    ring: VecDeque<f64>,
    capacity: usize,
    previous_x: [f64; 4],
    previous_y: [f64; 4],
    error: String,
    termination_reason: String,
    last_device_check: Instant,
}

impl State {
    fn new() -> Self {
        Self {
            usb: None,
            profile: None,
            running: false,
            acquisition_started_at_utc: None,
            recorder: None,
            recording_paused: false,
            last_recording: None,
            recorded_scans: 0,
            recording_target_scans: None,
            recording_remaining_scans: None,
            total_scans: 0,
            ring: VecDeque::new(),
            capacity: 0,
            previous_x: [0.0; 4],
            previous_y: [0.0; 4],
            error: String::new(),
            termination_reason: "user_stop".into(),
            last_device_check: Instant::now(),
        }
    }

    fn poll(&self, seconds: f64, after_scan: Option<u64>) -> Value {
        let width = self.profile.as_ref().map_or(1, |p| p.config.channels.len());
        let rate = self.profile.as_ref().map_or(1.0, |p| p.actual_rate);
        let n = ((seconds.clamp(0.0, 60.0) * rate).round() as usize).min(self.ring.len() / width);
        let start = self.total_scans - n as u64;
        let start = after_scan
            .filter(|&scan| scan <= self.total_scans)
            .map_or(start, |scan| scan.max(start));
        let n = (self.total_scans - start) as usize;
        let raw: Vec<f64> = self
            .ring
            .iter()
            .skip(self.ring.len() - n * width)
            .copied()
            .collect();
        json!({"pid": std::process::id(), "running": self.running, "recording": self.recorder.is_some(),
            "recording_paused": self.recording_paused,
            "acquisition_started_at_utc": self.acquisition_started_at_utc,
            "profile": self.profile, "device_connected": self.usb.is_some(),
            "total_scans": self.total_scans, "recorded_scans": self.recorded_scans,
            "recording_target_scans": self.recording_target_scans,
            "recording_remaining_scans": self.recording_remaining_scans,
            "last_recording": self.last_recording, "error": self.error, "termination_reason": self.termination_reason,
            "capacity": self.capacity, "buffered_scans": self.ring.len() / width,
            "snapshot_start": self.total_scans - n as u64, "raw": raw})
    }

    fn set_channels(&mut self, channels: [Channel; 4]) -> Result<(), String> {
        if self.recorder.is_some() {
            return Err("记录中不能修改换算配置".into());
        }
        let mut p = self.profile.clone().ok_or("请先开始预览")?;
        p.channels = channels;
        p.validate()?;
        self.profile = Some(p);
        Ok(())
    }

    fn start_recording(&mut self, path: PathBuf) -> Result<(), String> {
        if !self.running || self.recorder.is_some() {
            return Err("未预览或已在记录".into());
        }
        let p = self.profile.as_mut().unwrap();
        let target = p.recording_options.target_scans(p.actual_rate)?;
        let recorder = Recorder::new(
            path.clone(),
            p,
            self.total_scans,
            target,
            self.acquisition_started_at_utc.as_deref(),
        )?;
        p.recording_root = path.parent().unwrap().to_path_buf();
        self.recorder = Some(recorder);
        self.last_recording = Some(path);
        self.recorded_scans = 0;
        self.recording_paused = false;
        self.recording_target_scans = target;
        self.recording_remaining_scans = target;
        self.termination_reason = "user_stop".into();
        Ok(())
    }

    fn finish_recording(&mut self) -> Result<(), String> {
        self.recording_paused = false;
        if let Some(r) = self.recorder.take() {
            self.recorded_scans = r.frames;
            self.recording_remaining_scans = self
                .recording_target_scans
                .map(|target| target.saturating_sub(r.frames));
            r.close(&self.termination_reason, &self.error)?;
        }
        Ok(())
    }

    fn command(&mut self, command: Command) -> Result<Value, String> {
        match command {
            Command::Poll { seconds, after_scan } => {
                if !self.running && self.usb.is_some()
                    && self.last_device_check.elapsed() >= Duration::from_millis(500)
                {
                    self.last_device_check = Instant::now();
                    if let Some(error) = self.usb.as_ref().and_then(|usb| usb.check_connection().err()) {
                        self.fail(error);
                        self.stop_input(false);
                    }
                }
                return Ok(self.poll(seconds, after_scan));
            }
            Command::Connect { mut profile } => {
                if self.running || self.usb.is_some() {
                    return Err("核心已在采集或设备已经连接，请接回当前会话".into());
                }
                profile.validate()?;
                if profile.source != Source::Usb {
                    return Err("连接设备只适用于真实 USB 输入".into());
                }
                let usb = usb_input::UsbInput::connect().map_err(|e| e.to_string())?;
                profile.actual_rate = dt9837::plan_clock(profile.config.sample_rate)?.actual_rate;
                profile.device = serde_json::to_value(&usb.info).unwrap();
                *self = Self::new();
                self.usb = Some(usb);
                self.profile = Some(*profile);
            }
            Command::Disconnect => {
                if self.recorder.is_some() {
                    self.termination_reason = "user_stop".into();
                }
                self.stop_input(false);
                self.finish_recording()?;
            }
            Command::Start {
                mut profile,
                recording_path,
            } => {
                if self.running {
                    return Err("核心正在采集，请接回当前会话".into());
                }
                profile.validate()?;
                profile.actual_rate = dt9837::plan_clock(profile.config.sample_rate)?.actual_rate;
                // Connect/configure before replacing the previous session or creating a file.
                let usb = if profile.source == Source::Usb {
                    let mut usb = match self.usb.take() {
                        Some(usb) => usb,
                        None => usb_input::UsbInput::connect().map_err(|e| e.to_string())?,
                    };
                    if let Err(error) = usb.start(&profile.config) {
                        self.fail(error.clone());
                        return Err(error.to_string());
                    }
                    profile.actual_rate = usb.actual_rate;
                    profile.device = serde_json::to_value(&usb.info).unwrap();
                    Some(usb)
                } else {
                    profile.device = Value::Null;
                    None
                };
                *self = Self::new();
                self.usb = usb;
                self.capacity = ((profile.actual_rate * 60.0).round() as usize).clamp(1, 200000);
                self.ring = VecDeque::with_capacity(self.capacity * profile.config.channels.len());
                self.profile = Some(*profile);
                self.running = true;
                self.acquisition_started_at_utc = Some(utc_now());
                if let Some(path) = recording_path {
                    if let Err(e) = self.start_recording(path) {
                        self.stop_input(true);
                        return Err(e);
                    }
                }
            }
            Command::StartRecording {
                path,
                channels,
                experiment,
                recording_options,
            } => {
                if self.recorder.is_some() {
                    return Err("记录中不能修改换算配置".into());
                }
                let mut p = self.profile.clone().ok_or("请先开始预览")?;
                p.channels = channels;
                p.experiment = experiment;
                p.recording_options = recording_options;
                p.validate()?;
                self.profile = Some(p);
                self.start_recording(path)?;
            }
            Command::SetChannels { channels } => self.set_channels(channels)?,
            Command::PauseRecording => {
                let recorder = self.recorder.as_mut().ok_or("当前没有记录")?;
                if !self.recording_paused {
                    recorder.set_paused(true, self.total_scans)?;
                    self.recording_paused = true;
                }
            }
            Command::ResumeRecording => {
                let recorder = self.recorder.as_mut().ok_or("当前没有记录")?;
                if self.recording_paused {
                    recorder.set_paused(false, self.total_scans)?;
                    self.recording_paused = false;
                }
            }
            Command::StopRecording => {
                if self.recorder.is_some() {
                    self.termination_reason = "user_stop".into();
                }
                self.finish_recording()?;
            }
            Command::Stop => {
                if self.recorder.is_some() {
                    self.termination_reason = "user_stop".into();
                }
                self.stop_input(true);
                self.finish_recording()?;
            }
            Command::Shutdown => {
                if self.recorder.is_some() {
                    return Err("请先结束记录再退出核心".into());
                }
                self.stop_input(false);
            }
        }
        Ok(self.poll(0.0, None))
    }

    fn fail(&mut self, fault: usb_input::Fault) {
        if self.error.is_empty() {
            self.error = fault.message;
            self.termination_reason = fault.reason.into();
        }
    }

    fn stop_input(&mut self, keep_connected: bool) {
        self.running = false;
        if let Some(mut usb) = self.usb.take() {
            let mut healthy = true;
            if let Err(e) = usb.stop() {
                healthy = false;
                self.fail(e);
            }
            if let Some(e) = usb.error() {
                healthy = false;
                self.fail(e);
            }
            // Include every complete block already delivered by the USB reader.
            for values in usb.blocks.try_iter() {
                // A partial write is the end of the valid prefix. Never append
                // later blocks after the unwritten portion of a failed block.
                if self.termination_reason == "write_error" {
                    break;
                }
                if let Err(e) = self.accept_values(values) {
                    self.fail(usb_input::Fault {
                        reason: "write_error",
                        message: e,
                    });
                    break;
                }
            }
            if keep_connected && healthy {
                self.usb = Some(usb);
            } else if let Err(error) = usb.disconnect() {
                self.fail(error);
            }
        }
    }

    fn sample(&mut self) -> Result<(), usb_input::Fault> {
        if let Some(usb) = &self.usb {
            let error = usb.error();
            // Bound work per iteration so that control commands still get serviced.
            let blocks: Vec<_> = usb.blocks.try_iter().take(64).collect();
            for values in blocks {
                self.accept_values(values)
                    .map_err(|message| usb_input::Fault {
                        reason: "write_error",
                        message,
                    })?;
            }
            return error.map_or(Ok(()), Err);
        }
        self.sample_simulation()
            .map_err(|message| usb_input::Fault {
                reason: "error",
                message,
            })
    }

    fn sample_simulation(&mut self) -> Result<(), String> {
        let p = self.profile.as_ref().unwrap();
        let width = p.config.channels.len();
        let count = (p.actual_rate * SIMULATION_BLOCK_SECONDS).round().max(1.0) as usize;
        let alpha = 1.0 / (1.0 + std::f64::consts::TAU / p.actual_rate);
        let mut values = Vec::with_capacity(count * width);
        for offset in 0..count {
            for &ch in &p.config.channels {
                let mut v =
                    p.signal_settings
                        .volts(self.total_scans + offset as u64, ch, p.actual_rate);
                if !v.is_finite() {
                    return Err("模拟参数导致非有限电压".into());
                }
                if p.config.coupling[ch] == "AC" {
                    let y = alpha * (self.previous_y[ch] + v - self.previous_x[ch]);
                    self.previous_x[ch] = v;
                    self.previous_y[ch] = y;
                    v = y;
                }
                values.push(signal::quantize(v, p.config.ranges[ch]));
            }
        }
        self.accept_values(values)
    }

    fn accept_values(&mut self, values: Vec<f64>) -> Result<(), String> {
        let width = self.profile.as_ref().unwrap().config.channels.len();
        let count = values.len() / width;
        if let Some(r) = &mut self.recorder
            && !self.recording_paused
        {
            let frames = self
                .recording_remaining_scans
                .map_or(count, |remaining| remaining.min(count as u64) as usize);
            r.write(&values[..frames * width], self.total_scans)?;
            self.recorded_scans = r.frames;
            self.recording_remaining_scans = self
                .recording_target_scans
                .map(|target| target.saturating_sub(r.frames));
            if self.recording_remaining_scans == Some(0) {
                self.termination_reason = "duration_complete".into();
                self.finish_recording()?;
            }
        }
        let capacity = self.capacity * width;
        if values.len() >= capacity {
            self.ring.clear();
            self.ring
                .extend(values[values.len() - capacity..].iter().copied());
        } else {
            let excess = (self.ring.len() + values.len()).saturating_sub(capacity);
            self.ring.drain(..excess);
            self.ring.extend(values);
        }
        self.total_scans += count as u64;
        Ok(())
    }
}

struct Envelope {
    command: Command,
    reply: SyncSender<Result<Value, String>>,
}

fn serve(
    mut stream: TcpStream,
    token: &str,
    sender: &SyncSender<Envelope>,
) -> Result<bool, Box<dyn std::error::Error>> {
    stream.set_nodelay(true)?;
    stream.set_read_timeout(Some(Duration::from_secs(1)))?;
    stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    let mut line = String::new();
    BufReader::new((&stream).take(1024 * 1024)).read_line(&mut line)?;
    let request: Result<Request, _> = serde_json::from_str(&line);
    let mut shutdown = false;
    let response = match request {
        Ok(r) if r.token == token => {
            shutdown = matches!(r.command, Command::Shutdown);
            let (tx, rx) = mpsc::sync_channel(1);
            if sender
                .try_send(Envelope {
                    command: r.command,
                    reply: tx,
                })
                .is_err()
            {
                Err("核心忙，命令未执行".to_string())
            } else {
                rx.recv().unwrap_or_else(|_| Err("核心已退出".into()))
            }
        }
        _ => Err("请求格式或连接凭据无效".into()),
    };
    shutdown &= response.is_ok();
    let body = match response {
        Ok(s) => json!({"ok": true, "state": s}),
        Err(e) => json!({"ok": false, "error": e}),
    };
    // Even if this client disappears, an accepted shutdown must release the listener.
    // JSON emits many small writes; buffer them before writing to the socket.
    let mut writer = BufWriter::new(&mut stream);
    let _ = serde_json::to_writer(&mut writer, &body)
        .and_then(|_| writer.write_all(b"\n").map_err(serde_json::Error::io))
        .and_then(|_| writer.flush().map_err(serde_json::Error::io));
    Ok(shutdown)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        return Err("usage: labdaq-core ENDPOINT_FILE PORT TOKEN".into());
    }
    let endpoint = PathBuf::from(&args[1]);
    let port: u16 = args[2].parse()?;
    let token = args[3].clone();
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let (sender, receiver) = mpsc::sync_channel::<Envelope>(8);
    fs::create_dir_all(endpoint.parent().ok_or("endpoint parent missing")?)?;
    let temp = endpoint.with_extension("tmp");
    fs::write(
        &temp,
        serde_json::to_vec(
            &json!({"port": listener.local_addr()?.port(), "token": token, "pid": std::process::id()}),
        )?,
    )?;
    fs::rename(temp, &endpoint)?;
    let server = thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            if matches!(serve(stream, &token, &sender), Ok(true)) {
                break;
            }
        }
    });
    let mut state = State::new();
    let mut deadline = Instant::now();
    loop {
        if state.running && Instant::now() >= deadline {
            if let Err(error) = state.sample() {
                state.fail(error);
                state.stop_input(false);
                if let Err(e) = state.finish_recording() {
                    state.error.push_str(&format!("; 保存未完成: {e}"));
                }
            } else {
                if state.usb.is_some() {
                    deadline = Instant::now() + Duration::from_millis(10);
                } else {
                    let rate = state.profile.as_ref().unwrap().actual_rate;
                    deadline += Duration::from_secs_f64(
                        (rate * SIMULATION_BLOCK_SECONDS).round().max(1.0) / rate,
                    );
                }
            }
        }
        let wait = if state.running {
            deadline.saturating_duration_since(Instant::now())
        } else {
            Duration::from_secs(1)
        };
        if let Ok(envelope) = receiver.recv_timeout(wait) {
            let starting = matches!(envelope.command, Command::Start { .. });
            let shutdown = matches!(envelope.command, Command::Shutdown);
            let result = state.command(envelope.command);
            let exit = shutdown && result.is_ok();
            if starting && result.is_ok() {
                deadline = Instant::now();
            }
            let _ = envelope.reply.send(result);
            if exit {
                break;
            }
        }
    }
    let _ = server.join();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> Profile {
        Profile {
            source: Source::Simulation,
            device: Value::Null,
            config: Config {
                sample_rate: 2000.0,
                channels: vec![0, 2],
                ranges: [10.0; 4],
                coupling: std::array::from_fn(|_| "DC".into()),
                iepe: [false; 4],
            },
            actual_rate: 2000.0,
            channels: std::array::from_fn(|index| Channel {
                index,
                name: format!("AI{index}"),
                unit: "V".into(),
                sensitivity_mv_per_unit: 1000.0,
                zero_volts: 0.0,
                inverted: false,
            }),
            signal_settings: signal::Signals {
                channels: std::array::from_fn(|_| signal::Signal {
                    waveform: "sine".into(),
                    amplitude_v: 1.0,
                    offset_v: 0.0,
                    frequency_hz: 10.0,
                    phase_deg: 0.0,
                    noise_rms_v: 0.0,
                    drift_v_per_s: 0.0,
                    duty_cycle: 0.5,
                    start_s: 0.0,
                    duration_s: 1.0,
                    end_frequency_hz: 20.0,
                }),
                seed: 1,
            },
            experiment: "模拟记录测试".into(),
            recording_root: PathBuf::new(),
            recording_options: RecordingOptions::default(),
        }
    }

    fn recording_path(name: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../.task-tmp/basic-pause-20261019/rust")
            .join(std::process::id().to_string())
            .join(name)
    }

    fn metadata(path: &PathBuf) -> Value {
        serde_json::from_slice(&fs::read(path.join("metadata.json")).unwrap()).unwrap()
    }

    #[test]
    fn recording_duration_validates_frame_limits_and_old_requests_default_to_manual() {
        let mut options = RecordingOptions::default();
        assert_eq!(options.target_scans(2000.0).unwrap(), None);
        for duration in [0.0, -1.0, f64::NAN, f64::INFINITY, 1e30] {
            options.duration_seconds = Some(duration);
            assert!(options.target_scans(2000.0).is_err());
        }
        options.duration_seconds = Some(0.00025);
        assert_eq!(options.target_scans(2000.0).unwrap(), Some(1));
        options.duration_seconds = Some(0.001);
        assert_eq!(options.target_scans(195.313).unwrap(), Some(1));
        options.duration_seconds = Some(u64::MAX as f64 / 2000.0);
        assert!(options.target_scans(2000.0).is_err());

        let p = profile();
        let mut old_profile = serde_json::to_value(&p).unwrap();
        old_profile
            .as_object_mut()
            .unwrap()
            .remove("recording_options");
        let decoded: Profile = serde_json::from_value(old_profile).unwrap();
        assert_eq!(decoded.recording_options.duration_seconds, None);
        assert!(decoded.recording_options.notes.is_empty());
        assert_eq!(decoded.recording_options.group_number, 0);
        let command: Command = serde_json::from_value(json!({
            "op": "start_recording", "path": "unused", "channels": p.channels,
            "experiment": "旧请求"
        }))
        .unwrap();
        let Command::StartRecording {
            recording_options, ..
        } = command
        else {
            panic!("start_recording expected");
        };
        assert_eq!(recording_options.duration_seconds, None);
        assert_eq!(recording_options.group_number, 0);
    }

    #[test]
    fn display_window_can_include_twenty_seconds_of_original_samples() {
        let mut state = State::new();
        state.command(Command::Start {
            profile: Box::new(profile()),
            recording_path: None,
        }).unwrap();
        let values: Vec<f64> = (0..80000).map(|i| i as f64 / 10000.0).collect();
        state.accept_values(values.clone()).unwrap();
        let window = state.poll(20.0, None);
        assert_eq!(window["snapshot_start"], 0);
        assert_eq!(window["total_scans"], 40000);
        assert_eq!(window["raw"], json!(values));
    }

    #[test]
    fn direct_duration_recording_crops_disk_and_keeps_preview_running() {
        let path = recording_path("direct");
        let mut p = profile();
        p.recording_options = RecordingOptions {
            duration_seconds: Some(0.003),
            notes: "定长备注".into(),
            group_number: 1,
        };
        let mut state = State::new();
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: Some(path.clone()),
            })
            .unwrap();
        assert_eq!(state.recording_target_scans, Some(6));
        assert_eq!(state.recording_remaining_scans, Some(6));
        let values: Vec<_> = (0..16).map(|i| i as f64).collect();
        state.accept_values(values.clone()).unwrap();
        assert!(state.running && state.recorder.is_none());
        assert_eq!(state.total_scans, 8);
        assert_eq!(state.recorded_scans, 6);
        assert_eq!(state.recording_remaining_scans, Some(0));
        assert_eq!(state.termination_reason, "duration_complete");
        assert_eq!(state.poll(1.0, None)["raw"], json!(values));
        let bytes = fs::read(path.join("raw.f64")).unwrap();
        let expected: Vec<_> = values[..12].iter().flat_map(|v| v.to_le_bytes()).collect();
        assert_eq!(bytes, expected);
        let info = metadata(&path);
        assert_eq!(info["sample_count"], 6);
        assert_eq!(info["target_sample_count"], 6);
        assert_eq!(info["recording_options"]["notes"], "定长备注");
        assert_eq!(info["recording_options"]["group_number"], 1);
        assert_eq!(info["recording_options"]["duration_seconds"], 0.003);
        assert_eq!(
            info["duration_seconds"].as_f64().unwrap(),
            6.0 / state.profile.as_ref().unwrap().actual_rate
        );
        assert_eq!(info["termination_reason"], "duration_complete");
        assert_eq!(info["finalized"], true);
        state.accept_values(vec![0.0; 8]).unwrap();
        assert_eq!(state.total_scans, 12);
        assert_eq!(state.recorded_scans, 6);
        state.command(Command::Stop).unwrap();
        assert_eq!(state.termination_reason, "duration_complete");
        state
            .command(Command::Start {
                profile: Box::new(profile()),
                recording_path: None,
            })
            .unwrap();
        assert_eq!(state.recording_target_scans, None);
        assert_eq!(state.recording_remaining_scans, None);
    }

    #[test]
    fn preview_groups_can_complete_then_record_manually_or_stop_early() {
        let mut state = State::new();
        state
            .command(Command::Start {
                profile: Box::new(profile()),
                recording_path: None,
            })
            .unwrap();
        state.accept_values(vec![0.0; 16]).unwrap();
        let timed = recording_path("preview-timed");
        state
            .command(Command::StartRecording {
                path: timed.clone(),
                channels: profile().channels,
                experiment: "预览中记录".into(),
                recording_options: RecordingOptions {
                    duration_seconds: Some(0.0025),
                    notes: "第二组".into(),
                    group_number: 2,
                },
            })
            .unwrap();
        state.accept_values(vec![1.0; 16]).unwrap();
        assert_eq!(state.recorded_scans, 5);
        assert_eq!(
            metadata(&timed)["acquisition_config"]["record_start_scan"],
            8
        );

        let manual = recording_path("preview-manual");
        state
            .command(Command::StartRecording {
                path: manual.clone(),
                channels: profile().channels,
                experiment: "手动组".into(),
                recording_options: RecordingOptions::default(),
            })
            .unwrap();
        assert_eq!(state.termination_reason, "user_stop");
        assert_eq!(state.recording_target_scans, None);
        assert_eq!(state.recording_remaining_scans, None);
        state.accept_values(vec![2.0; 6]).unwrap();
        state.command(Command::StopRecording).unwrap();
        assert!(state.running);
        assert_eq!(metadata(&manual)["termination_reason"], "user_stop");
        assert_eq!(metadata(&manual)["target_sample_count"], Value::Null);

        let early = recording_path("preview-early");
        state
            .command(Command::StartRecording {
                path: early.clone(),
                channels: profile().channels,
                experiment: "提前停止".into(),
                recording_options: RecordingOptions {
                    duration_seconds: Some(0.01),
                    ..RecordingOptions::default()
                },
            })
            .unwrap();
        state.accept_values(vec![3.0; 6]).unwrap();
        state.command(Command::Stop).unwrap();
        assert!(!state.running);
        assert_eq!(state.recorded_scans, 3);
        assert_eq!(state.recording_remaining_scans, Some(17));
        assert_eq!(metadata(&early)["termination_reason"], "user_stop");
    }

    #[test]
    fn paused_recording_keeps_preview_freezes_duration_and_maps_resumed_source() {
        let mut state = State::new();
        let mut p = profile();
        p.recording_options.duration_seconds = Some(0.005);
        let path = recording_path("paused-duration");
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: Some(path.clone()),
            })
            .unwrap();
        state.accept_values(vec![1.0; 6]).unwrap();
        let paused = state.command(Command::PauseRecording).unwrap();
        assert_eq!(paused["recording"], true);
        assert_eq!(paused["recording_paused"], true);
        assert!(paused["acquisition_started_at_utc"].is_string());
        state.accept_values(vec![9.0; 200]).unwrap();
        assert_eq!(state.total_scans, 103);
        assert_eq!(state.recorded_scans, 3);
        assert_eq!(state.recording_remaining_scans, Some(7));
        assert_eq!(fs::metadata(path.join("raw.f64")).unwrap().len(), 3 * 2 * 8);
        assert_eq!(metadata(&path)["recording_paused"], true);
        state.command(Command::ResumeRecording).unwrap();
        state.accept_values(vec![2.0; 16]).unwrap();
        assert_eq!(state.total_scans, 111);
        assert_eq!(state.recorded_scans, 10);
        assert_eq!(state.recording_remaining_scans, Some(0));
        assert!(state.running && state.recorder.is_none() && !state.recording_paused);
        let info = metadata(&path);
        assert_eq!(info["termination_reason"], "duration_complete");
        assert_eq!(
            info["recording_segments"],
            json!([
                {"file_start_scan": 0, "source_start_scan": 0, "sample_count": 3},
                {"file_start_scan": 3, "source_start_scan": 103, "sample_count": 7},
            ])
        );
        let second = recording_path("paused-stop");
        state
            .command(Command::StartRecording {
                path: second.clone(),
                channels: profile().channels,
                experiment: "第二组".into(),
                recording_options: RecordingOptions::default(),
            })
            .unwrap();
        assert!(!state.recording_paused);
        state.accept_values(vec![3.0; 8]).unwrap();
        state.command(Command::PauseRecording).unwrap();
        state.accept_values(vec![8.0; 40]).unwrap();
        state.command(Command::StopRecording).unwrap();
        assert!(state.running && !state.recording_paused);
        assert_eq!(metadata(&second)["sample_count"], 4);
        assert_eq!(metadata(&second)["termination_reason"], "user_stop");
        assert_eq!(metadata(&second)["recording_paused"], false);
    }
}
