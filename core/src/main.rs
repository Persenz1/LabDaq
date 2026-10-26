//! Independent acquisition and recording with simulation and physical input backends.
mod dt9837;
mod input;
mod output;
mod signal;
mod trigger;
mod usb_boot;
mod usb_input;
mod usb_stream;
mod vendor_input;

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
    Vendor,
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
    #[serde(default)]
    point_number: Option<u32>,
    #[serde(default)]
    direction: String,
    #[serde(default)]
    engineering_offset: f64,
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default)]
struct RecordingOptions {
    duration_seconds: Option<f64>,
    notes: String,
    specimen: String,
    condition: String,
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
    #[serde(default)]
    device_selector: Option<usb_input::DeviceSelector>,
    #[serde(default)]
    trigger: trigger::Settings,
    #[serde(default = "default_sync_mode")]
    sync_mode: String,
    #[serde(skip)]
    defer_hardware_start: bool,
    #[serde(default)]
    linked_output: Option<output::Settings>,
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
        if let Some(output) = &self.linked_output {
            output.validate()?;
            if output.trigger != "immediate" {
                return Err("随采集输出使用软件联动启动；外部 TTL 输出请独立控制".into());
            }
        }
        if !["independent", "master", "slave"].contains(&self.sync_mode.as_str()) {
            return Err("未知设备同步模式".into());
        }
        if self.sync_mode == "slave" && self.trigger.mode == "external_rising" {
            return Err("从机启动由主机控制，不能独立使用 TTL 触发".into());
        }
        if !(195.313..=105469.0).contains(&c.sample_rate)
            || !(190.0..=106000.0).contains(&self.actual_rate)
            || c.channels.is_empty()
            || c.channels.iter().any(|i| *i > 3)
            || c.channels.windows(2).any(|w| w[0] >= w[1])
            || c.ranges.iter().any(|r| *r != 1.0 && *r != 10.0)
            || c.coupling.iter().any(|s| s != "AC" && s != "DC")
            || self.channels.iter().enumerate().any(|(i, ch)| {
                ch.index != i
                    || ch.sensitivity_mv_per_unit < 0.0
                    || (ch.unit != "V" && ch.sensitivity_mv_per_unit == 0.0)
                    || !ch.sensitivity_mv_per_unit.is_finite()
                    || !ch.zero_volts.is_finite()
                    || !ch.engineering_offset.is_finite()
            })
        {
            return Err("采样率、通道或换算配置无效".into());
        }
        let target = self.recording_options.target_scans(self.actual_rate)?;
        self.trigger.validate(
            &c.channels,
            &c.ranges,
            self.actual_rate,
            target,
            self.source != Source::Simulation,
        )?;
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
        config["sync_mode"] = json!(p.sync_mode);
        config["input"] = match p.source {
            Source::Simulation => json!({"backend": "rust-core-v1", "scenario": "custom",
                "noise_algorithm": "splitmix64-box-muller-v1", "signal_settings": p.signal_settings}),
            Source::Usb => json!({"backend": "rust-usb-v1", "device": p.device,
                "startup_discard_scans": dt9837::GROUP_DELAY, "adc_encoding": "offset-binary-24-in-u32-le",
                "timebase": "hardware-sample-index", "continuity": "no-device-sequence-counter"}),
            Source::Vendor => json!({"backend": "dt-open-layers-c-abi", "device": p.device,
                "adc_encoding": "open-layers-24-bit-to-f64-volts",
                "timebase": "hardware-sample-index", "continuity": "open-layers-buffer-stream"}),
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

#[derive(Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Command {
    ListDevices {
        #[serde(default)]
        source: Option<Source>,
    },
    StartCollection {
        profiles: Vec<Profile>,
        #[serde(default)]
        recording_path: Option<PathBuf>,
    },
    DeviceCommand {
        index: usize,
        command: Box<Command>,
    },
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
    StartOutput {
        settings: output::Settings,
    },
    UpdateOutput {
        settings: output::Settings,
    },
    MuteOutput {
        muted: bool,
    },
    PreviewOutput {
        settings: output::Settings,
        #[serde(default = "default_preview_count")]
        count: usize,
    },
    StopOutput,
    FireTrigger,
    AcceptRecord,
    RejectRecord,
    CancelTrigger,
    StopRecording,
    PauseRecording,
    ResumeRecording,
    Stop,
    Shutdown,
    ShutdownIfIdle,
}

#[derive(Deserialize)]
struct Request {
    token: String,
    #[serde(flatten)]
    command: Command,
}

fn default_preview_count() -> usize {
    2048
}
fn default_sync_mode() -> String {
    "independent".into()
}

struct SimulatedOutput {
    status: output::Status,
    started: Instant,
}

struct State {
    input: Option<input::Input>,
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
    trigger: Option<trigger::Engine>,
    trigger_base_path: Option<PathBuf>,
    simulated_output: Option<SimulatedOutput>,
    collection: Option<Vec<State>>,
    persistence_path: Option<PathBuf>,
}

impl State {
    fn new() -> Self {
        Self {
            input: None,
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
            trigger: None,
            trigger_base_path: None,
            simulated_output: None,
            collection: None,
            persistence_path: None,
        }
    }

    fn poll(&self, seconds: f64, after_scan: Option<u64>) -> Value {
        if let Some(devices) = &self.collection {
            return json!({"pid":std::process::id(),"running":self.running,"recording":devices.iter().any(|d|d.recorder.is_some()),
                "recording_paused":devices.iter().all(|d|d.recording_paused),"profile":self.profile,
                "device_connected":devices.iter().all(|d|d.input.is_some()),"output":devices.first().map(|d|d.poll(0.,None)["output"].clone()),
                "total_scans":devices.iter().map(|d|d.total_scans).min().unwrap_or(0),
                "recorded_scans":devices.iter().map(|d|d.recorded_scans).min().unwrap_or(0),
                "error":self.error,"termination_reason":self.termination_reason,"raw":[],
                "collection":devices.iter().map(|d|d.poll(seconds,after_scan)).collect::<Vec<_>>()});
        }
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
            "profile": self.profile, "device_connected": self.input.is_some(),
            "output": self.input.as_ref().map(|u| u.output_status()).or_else(||self.simulated_output.as_ref().map(|s|&s.status)),
            "total_scans": self.total_scans, "recorded_scans": self.recorded_scans,
            "recording_target_scans": self.recording_target_scans,
            "recording_remaining_scans": self.recording_remaining_scans,
            "last_recording": self.last_recording, "error": self.error, "termination_reason": self.termination_reason,
            "capacity": self.capacity, "buffered_scans": self.ring.len() / width,
            "snapshot_start": self.total_scans - n as u64, "raw": raw,
            "trigger": self.trigger.as_ref().map(|t| json!({"settings":t.settings,
                "waiting":t.waiting,"pending_review":t.pending_review,"finished":t.finished,
                "accepted":t.accepted,"rejected":t.rejected,"attempts":t.attempts,
                "last_trigger_scan":t.last_trigger_scan}))})
    }

    fn set_channels(&mut self, channels: [Channel; 4]) -> Result<(), String> {
        if self.recorder.is_some() || self.trigger.as_ref().is_some_and(|t| !t.finished) {
            return Err("记录中不能修改换算配置".into());
        }
        let mut p = self.profile.clone().ok_or("请先开始预览")?;
        p.channels = channels;
        p.validate()?;
        self.profile = Some(p);
        Ok(())
    }

    fn start_recording(&mut self, path: PathBuf) -> Result<(), String> {
        if !self.running
            || self.recorder.is_some()
            || self.trigger.as_ref().is_some_and(|t| !t.finished)
        {
            return Err("未预览或已在记录".into());
        }
        if path.exists() {
            return Err("记录目录已存在，不能覆盖".into());
        }
        let p = self.profile.as_ref().unwrap();
        if p.trigger.segmented() {
            let target = p
                .recording_options
                .target_scans(p.actual_rate)?
                .ok_or("触发记录需要有限时长")?;
            if p.source != Source::Simulation
                && p.trigger.mode == "external_rising"
                && self.total_scans > 0
            {
                return Err("TTL 触发必须停止采集后重新启动并同时设置记录路径".into());
            }
            self.trigger = Some(trigger::Engine::new(
                p.trigger.clone(),
                &p.config.channels,
                target,
                p.actual_rate,
                self.total_scans,
                p.source != Source::Simulation && p.trigger.mode == "external_rising",
            ));
            self.trigger_base_path = Some(path);
            self.recorded_scans = 0;
            self.recording_target_scans = Some(target);
            self.recording_remaining_scans = Some(target);
            return Ok(());
        }
        self.trigger = None;
        self.create_recording(path, self.total_scans)
    }

    fn create_recording(&mut self, path: PathBuf, start_scan: u64) -> Result<(), String> {
        let p = self.profile.as_mut().unwrap();
        let target = p.recording_options.target_scans(p.actual_rate)?;
        let mut recorder = Recorder::new(
            path.clone(),
            p,
            start_scan,
            target,
            self.acquisition_started_at_utc.as_deref(),
        )?;
        recorder.metadata["analog_output_at_start"] = json!(
            self.input
                .as_ref()
                .map(|u| u.output_status())
                .or_else(|| self.simulated_output.as_ref().map(|s| &s.status))
        );
        recorder.metadata["analog_output_events"] = json!([]);
        recorder.save_metadata()?;
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
            let completed_number = r.metadata["recording_options"]["group_number"]
                .as_u64()
                .unwrap_or(0);
            let advance = self.error.is_empty()
                && ["user_stop", "duration_complete", "trigger_complete"]
                    .contains(&self.termination_reason.as_str())
                && r.metadata["acceptance"]
                    .as_str()
                    .is_none_or(|s| s == "accepted");
            self.recorded_scans = r.frames;
            self.recording_remaining_scans = self
                .recording_target_scans
                .map(|target| target.saturating_sub(r.frames));
            r.close(&self.termination_reason, &self.error)?;
            if advance {
                self.advance_group(completed_number)?;
            }
        }
        Ok(())
    }

    fn advance_group(&mut self, completed: u64) -> Result<(), String> {
        if let Some(profile) = &mut self.profile {
            profile.recording_options.group_number = completed.saturating_add(1);
        }
        self.save_profile()
    }

    fn save_profile(&self) -> Result<(), String> {
        if let (Some(path), Some(profile)) = (&self.persistence_path, &self.profile) {
            let temp = path.with_extension("json.tmp");
            fs::write(&temp, serde_json::to_vec_pretty(profile).unwrap())
                .map_err(|e| format!("记录状态配置保存失败：{e}"))?;
            fs::rename(temp, path).map_err(|e| format!("记录状态配置替换失败：{e}"))?;
        }
        Ok(())
    }

    fn idle(&self) -> bool {
        !self.running
            && self.recorder.is_none()
            && self.trigger.as_ref().is_none_or(|t| t.finished)
            && self
                .input
                .as_ref()
                .is_none_or(|u| !u.output_status().running && !u.output_active())
            && self
                .simulated_output
                .as_ref()
                .is_none_or(|s| !s.status.running)
            && self
                .collection
                .as_ref()
                .is_none_or(|c| c.iter().all(State::idle))
    }

    fn refresh_outputs(&mut self) {
        if let Some(usb) = &mut self.input {
            usb.refresh_output();
        }
        if let Some(s) = &mut self.simulated_output {
            if s.status.running
                && s.status
                    .settings
                    .as_ref()
                    .and_then(|v| v.end_seconds())
                    .is_some_and(|end| s.started.elapsed().as_secs_f64() >= end)
            {
                s.status.running = false;
                s.status.zeroed = true;
                s.status.completed = true;
            }
        }
        if let Some(devices) = &mut self.collection {
            for device in devices {
                device.refresh_outputs();
            }
        }
    }

    fn command(&mut self, command: Command) -> Result<Value, String> {
        if matches!(command, Command::ShutdownIfIdle) {
            self.refresh_outputs();
            let idle = self.idle();
            if idle {
                self.save_profile()?;
                self.stop_input(false);
            }
            let mut result = self.poll(0., None);
            result["shutdown"] = json!(idle);
            return Ok(result);
        }
        if self.collection.is_some() {
            match &command {
                Command::Poll {
                    seconds,
                    after_scan,
                } => {
                    for device in self.collection.as_mut().unwrap() {
                        device.command(Command::Poll {
                            seconds: 0.,
                            after_scan: None,
                        })?;
                    }
                    return Ok(self.poll(*seconds, *after_scan));
                }
                Command::PauseRecording
                | Command::ResumeRecording
                | Command::StopRecording
                | Command::Stop
                | Command::Disconnect
                | Command::Shutdown => {
                    let devices = self.collection.as_mut().unwrap();
                    if matches!(command, Command::Shutdown)
                        && devices.iter().any(|d| d.recorder.is_some())
                    {
                        return Err("请先结束全部设备的记录".into());
                    }
                    for device in devices {
                        device.command(command.clone())?;
                    }
                    self.profile = self
                        .collection
                        .as_ref()
                        .and_then(|d| d.first())
                        .and_then(|d| d.profile.clone());
                    self.save_profile()?;
                    if matches!(
                        command,
                        Command::Stop | Command::Disconnect | Command::Shutdown
                    ) {
                        self.running = false;
                    }
                    if matches!(command, Command::Disconnect | Command::Shutdown) {
                        self.collection = None;
                    }
                    return Ok(self.poll(0., None));
                }
                Command::StartOutput { .. }
                | Command::UpdateOutput { .. }
                | Command::StopOutput
                | Command::MuteOutput { .. } => {
                    self.collection.as_mut().unwrap()[0].command(command)?;
                    return Ok(self.poll(0., None));
                }
                Command::DeviceCommand { .. }
                | Command::ListDevices { .. }
                | Command::PreviewOutput { .. }
                | Command::StartCollection { .. } => {}
                _ => return Err("多设备会话请使用集体控制，或 device_command 指定设备".into()),
            }
        }
        match command {
            Command::ListDevices { source } => {
                let devices = input::enumerate(source.unwrap_or(Source::Vendor))
                    .map_err(|e| e.to_string())?;
                return Ok(json!({"devices":devices}));
            }
            Command::StartCollection {
                profiles,
                recording_path,
            } => self.start_collection(profiles, recording_path)?,
            Command::DeviceCommand { index, command } => {
                if !matches!(
                    *command,
                    Command::Poll { .. }
                        | Command::StartRecording { .. }
                        | Command::StopRecording
                        | Command::PauseRecording
                        | Command::ResumeRecording
                        | Command::SetChannels { .. }
                        | Command::PreviewOutput { .. }
                        | Command::StartOutput { .. }
                        | Command::UpdateOutput { .. }
                        | Command::StopOutput
                        | Command::MuteOutput { .. }
                ) {
                    return Err("device_command 仅用于单设备显示、记录或主机 AO；采集启停必须作用于整个同步集合".into());
                }
                if index != 0
                    && matches!(
                        *command,
                        Command::StartOutput { .. } | Command::UpdateOutput { .. }
                    )
                {
                    return Err("多设备同步时仅允许主设备输出 AO".into());
                }
                self.collection
                    .as_mut()
                    .and_then(|d| d.get_mut(index))
                    .ok_or("多设备索引不存在")?
                    .command(*command)?;
            }
            Command::Poll {
                seconds,
                after_scan,
            } => {
                if let Some(usb) = &mut self.input {
                    usb.refresh_output();
                }
                if let Some(s) = &mut self.simulated_output {
                    if s.status.running
                        && s.status
                            .settings
                            .as_ref()
                            .and_then(|v| v.end_seconds())
                            .is_some_and(|end| s.started.elapsed().as_secs_f64() >= end)
                    {
                        s.status.running = false;
                        s.status.zeroed = true;
                        s.status.completed = true;
                    }
                }
                if let Some(error) = self.input.as_ref().and_then(|u| u.error()) {
                    self.fail(error);
                    self.stop_input(false);
                    self.finish_recording()?;
                }
                if !self.running
                    && self.input.is_some()
                    && self.last_device_check.elapsed() >= Duration::from_millis(500)
                {
                    self.last_device_check = Instant::now();
                    if let Some(error) = self
                        .input
                        .as_ref()
                        .and_then(|usb| usb.check_connection().err())
                    {
                        self.fail(error);
                        self.stop_input(false);
                    }
                }
                return Ok(self.poll(seconds, after_scan));
            }
            Command::Connect { mut profile } => {
                if self.running || self.input.is_some() {
                    return Err("核心已在采集或设备已经连接，请接回当前会话".into());
                }
                profile.validate()?;
                if profile.source == Source::Simulation {
                    return Err("连接设备只适用于物理输入".into());
                }
                let usb = input::Input::connect(profile.source, profile.device_selector.as_ref())
                    .map_err(|e| e.to_string())?;
                profile.actual_rate = dt9837::plan_clock(profile.config.sample_rate)?.actual_rate;
                profile.device = usb.info();
                let persistence = self.persistence_path.take();
                *self = Self::new();
                self.persistence_path = persistence;
                self.input = Some(usb);
                self.profile = Some(*profile);
            }
            Command::Disconnect => {
                self.trigger = None;
                self.simulated_output = None;
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
                if profile.linked_output.is_some()
                    && (self
                        .input
                        .as_ref()
                        .is_some_and(|u| u.output_status().running)
                        || self
                            .simulated_output
                            .as_ref()
                            .is_some_and(|s| s.status.running))
                {
                    return Err("请先停止现有 AO，再启动随采集联动输出".into());
                }
                if let Some(usb) = &self.input {
                    if usb.source() != profile.source {
                        return Err("请先断开当前设备，再切换输入后端".into());
                    }
                    if profile
                        .device_selector
                        .as_ref()
                        .is_some_and(|s| !usb.matches_selector(s))
                    {
                        return Err("选择的设备与当前连接不同，请先断开设备".into());
                    }
                }
                profile.actual_rate = dt9837::plan_clock(profile.config.sample_rate)?.actual_rate;
                // Connect/configure before replacing the previous session or creating a file.
                let usb = if profile.source != Source::Simulation {
                    let mut usb = match self.input.take() {
                        Some(usb) => usb,
                        None => {
                            input::Input::connect(profile.source, profile.device_selector.as_ref())
                                .map_err(|e| e.to_string())?
                        }
                    };
                    if let Err(error) = usb.start(
                        &profile.config,
                        profile.trigger.mode == "external_rising",
                        &profile.sync_mode,
                        profile.defer_hardware_start,
                    ) {
                        self.fail(error.clone());
                        return Err(error.to_string());
                    }
                    profile.actual_rate = usb.actual_rate();
                    profile.device = usb.info();
                    Some(usb)
                } else {
                    profile.device = Value::Null;
                    None
                };
                let simulated_output = self.simulated_output.take();
                let persistence = self.persistence_path.take();
                *self = Self::new();
                self.persistence_path = persistence;
                self.simulated_output = simulated_output;
                self.input = usb;
                self.capacity = ((profile.actual_rate * 60.0).round() as usize).clamp(1, 200000);
                self.ring = VecDeque::with_capacity(self.capacity * profile.config.channels.len());
                self.profile = Some(*profile);
                self.running = true;
                // The hardware does not timestamp its external trigger. Never turn
                // the host arm time into a fabricated acquisition timestamp.
                self.acquisition_started_at_utc = if self.profile.as_ref().is_some_and(|p| {
                    p.source != Source::Simulation && p.trigger.mode == "external_rising"
                }) {
                    None
                } else {
                    Some(utc_now())
                };
                if let Some(settings) = self.profile.as_ref().and_then(|p| p.linked_output.clone())
                {
                    if let Err(error) = self.command(Command::StartOutput { settings }) {
                        self.stop_input(true);
                        return Err(error);
                    }
                }
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
                if self.recorder.is_some() || self.trigger.as_ref().is_some_and(|t| !t.finished) {
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
            Command::FireTrigger => self.trigger.as_mut().ok_or("尚未布置触发记录")?.fire()?,
            Command::AcceptRecord => self.review_record(true)?,
            Command::RejectRecord => self.review_record(false)?,
            Command::CancelTrigger => {
                self.trigger = None;
                self.termination_reason = "trigger_cancelled".into();
                self.finish_recording()?;
            }
            Command::StartOutput { settings } => {
                settings.validate()?;
                let profile = self.profile.as_ref().ok_or("请先连接设备或开始模拟预览")?;
                // AO → AI0 is the current loopback wiring; excitation must remain off.
                if profile.source != Source::Simulation && profile.config.iepe[0] {
                    return Err(
                        "AO 回接 AI0 时必须关闭 AI0 的 IEPE；请停止采集后修改通道配置".into(),
                    );
                }
                if profile.source == Source::Simulation {
                    if self
                        .simulated_output
                        .as_ref()
                        .is_some_and(|s| s.status.running)
                    {
                        return Err("AO 已在运行；请使用更新参数或先停止".into());
                    }
                    if settings.trigger != "immediate" {
                        return Err(
                            "模拟 AO 预览仅支持立即启动；硬件 TTL 编码由离线测试覆盖".into()
                        );
                    }
                    let actual_rate = dt9837::plan_output_clock(settings.sample_rate)?.actual_rate;
                    self.simulated_output = Some(SimulatedOutput {
                        status: output::Status {
                            running: true,
                            settings: Some(settings),
                            actual_rate,
                            zeroed: false,
                            completed: false,
                        },
                        started: Instant::now(),
                    });
                } else {
                    self.input
                        .as_mut()
                        .ok_or("设备尚未连接")?
                        .start_output(settings)
                        .map_err(|e| e.to_string())?;
                }
                self.record_output_event("start");
            }
            Command::UpdateOutput { settings } => self.update_output(settings)?,
            Command::MuteOutput { muted } => {
                let mut settings = self
                    .input
                    .as_ref()
                    .and_then(|u| u.output_status().settings.clone())
                    .or_else(|| {
                        self.simulated_output
                            .as_ref()
                            .and_then(|s| s.status.settings.clone())
                    })
                    .ok_or("AO 尚未启动")?;
                settings.muted = muted;
                self.update_output(settings)?;
            }
            Command::PreviewOutput { settings, count } => {
                return Ok(json!({"samples":output::preview(&settings,count)?,
                "sample_rate":dt9837::plan_output_clock(settings.sample_rate)?.actual_rate,
                "end_seconds":settings.end_seconds(),"settings":settings}));
            }
            Command::StopOutput => {
                if let Some(s) = &mut self.simulated_output {
                    s.status.running = false;
                    s.status.zeroed = true;
                } else {
                    self.input
                        .as_mut()
                        .ok_or("设备尚未连接")?
                        .stop_output()
                        .map_err(|e| e.to_string())?;
                }
                self.record_output_event("stop");
            }
            Command::PauseRecording => {
                if self.trigger.is_some() {
                    return Err("触发分段记录不能暂停；可取消本次触发".into());
                }
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
                self.trigger = None;
                if self.recorder.is_some() {
                    self.termination_reason = "user_stop".into();
                }
                self.finish_recording()?;
            }
            Command::Stop => {
                self.trigger = None;
                if self.recorder.is_some() {
                    self.termination_reason = "user_stop".into();
                }
                self.stop_input(true);
                self.finish_recording()?;
            }
            Command::Shutdown => {
                if self.recorder.is_some() || self.trigger.as_ref().is_some_and(|t| !t.finished) {
                    return Err("请先结束记录再退出核心".into());
                }
                self.stop_input(false);
                self.simulated_output = None;
            }
            Command::ShutdownIfIdle => unreachable!(),
        }
        Ok(self.poll(0.0, None))
    }

    fn fail(&mut self, fault: usb_input::Fault) {
        self.trigger = None;
        if self.error.is_empty() {
            self.error = fault.message;
            self.termination_reason = fault.reason.into();
        }
    }

    fn record_output_event(&mut self, action: &str) {
        if let Some(recorder) = &mut self.recorder {
            recorder.metadata["analog_output_events"].as_array_mut().unwrap().push(json!({
                "action": action, "requested_at_utc": utc_now(), "observed_input_scan": self.total_scans,
                "output": self.input.as_ref().map(|u| u.output_status()).or_else(||self.simulated_output.as_ref().map(|s|&s.status)),
            }));
        }
    }

    fn update_output(&mut self, settings: output::Settings) -> Result<(), String> {
        settings.validate()?;
        if let Some(s) = &mut self.simulated_output {
            if !s.status.running {
                return Err("AO 尚未运行".into());
            }
            if s.status.settings.as_ref().is_some_and(|old| {
                old.sample_rate != settings.sample_rate || old.trigger != settings.trigger
            }) {
                return Err("运行中不能更改 AO 更新率或启动触发".into());
            }
            s.status.settings = Some(settings);
        } else {
            self.input
                .as_mut()
                .ok_or("设备尚未连接")?
                .update_output(settings)
                .map_err(|e| e.to_string())?;
        }
        self.record_output_event("update");
        Ok(())
    }

    fn stop_input(&mut self, keep_connected: bool) {
        self.record_output_event("acquisition_stop");
        self.running = false;
        let linked = self
            .profile
            .as_ref()
            .is_some_and(|p| p.linked_output.is_some());
        if linked {
            if let Some(output) = &mut self.simulated_output {
                output.status.running = false;
                output.status.zeroed = true;
            }
        }
        if let Some(devices) = &mut self.collection {
            for device in devices {
                device.trigger = None;
                if !self.error.is_empty() {
                    device.error = self.error.clone();
                    device.termination_reason = "collection_error".into();
                }
                device.stop_input(keep_connected);
                if let Err(e) = device.finish_recording() {
                    self.error.push_str(&format!("; {e}"));
                }
            }
            return;
        }
        if let Some(mut usb) = self.input.take() {
            let mut healthy = true;
            if let Err(e) = usb.stop() {
                healthy = false;
                self.fail(e);
            }
            if let Some(e) = usb.error() {
                healthy = false;
                self.fail(e);
            }
            // Include every complete block already delivered by the input reader.
            for values in usb.take_blocks(usize::MAX) {
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
            if linked {
                if let Err(error) = usb.stop_output() {
                    healthy = false;
                    self.fail(error);
                }
            }
            if keep_connected && healthy {
                self.input = Some(usb);
            } else if let Err(error) = usb.disconnect() {
                self.fail(error);
            }
        }
    }

    fn sample(&mut self) -> Result<(), usb_input::Fault> {
        if let Some(devices) = &mut self.collection {
            let previous = self
                .profile
                .as_ref()
                .map(|p| p.recording_options.group_number);
            for device in devices {
                if device.running {
                    device.sample()?;
                }
            }
            self.profile = self
                .collection
                .as_ref()
                .and_then(|d| d.first())
                .and_then(|d| d.profile.clone());
            if self
                .profile
                .as_ref()
                .map(|p| p.recording_options.group_number)
                != previous
            {
                self.save_profile().map_err(|message| usb_input::Fault {
                    reason: "configuration_write_error",
                    message,
                })?;
            }
            return Ok(());
        }
        if let Some(usb) = &self.input {
            let error = usb.error();
            // Bound work per iteration so that control commands still get serviced.
            let blocks = usb.take_blocks(64);
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
        if let Some(mut trigger) = self.trigger.take() {
            let events = trigger.process(&values, self.total_scans);
            for event in events {
                match event {
                    trigger::Event::Begin {
                        source_start,
                        trigger_scan,
                        pretrigger,
                        attempt,
                    } => {
                        let base = self.trigger_base_path.as_ref().unwrap();
                        let path = if attempt == 1 {
                            base.clone()
                        } else {
                            let name = base.file_name().unwrap_or_default().to_string_lossy();
                            base.with_file_name(format!("{name}-trigger-{attempt:04}"))
                        };
                        self.create_recording(path, source_start)?;
                        let r = self.recorder.as_mut().unwrap();
                        r.metadata["trigger"] = json!({"settings":trigger.settings,
                            "source_scan":trigger_scan,"file_scan":trigger_scan-source_start,
                            "attempt":attempt,"marker_definition":if trigger.settings.mode=="external_rising" {
                                "first_retained_sample_after_hardware_start" } else {"sample_threshold_or_software_request"}});
                        r.metadata["acceptance"] = json!(if trigger.settings.accept_reject {
                            "pending"
                        } else {
                            "accepted"
                        });
                        r.write(&pretrigger, source_start)?;
                        r.save_metadata()?;
                    }
                    trigger::Event::Samples {
                        source_start,
                        values,
                    } => {
                        self.recorder
                            .as_mut()
                            .ok_or("触发记录状态不一致")?
                            .write(&values, source_start)?;
                    }
                    trigger::Event::Complete => {
                        self.termination_reason = "trigger_complete".into();
                        self.finish_recording()?;
                    }
                }
            }
            if let Some(r) = &self.recorder {
                self.recorded_scans = r.frames;
                self.recording_remaining_scans = self
                    .recording_target_scans
                    .map(|n| n.saturating_sub(r.frames));
            }
            self.trigger = Some(trigger);
        } else if let Some(r) = &mut self.recorder
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

    fn review_record(&mut self, accepted: bool) -> Result<(), String> {
        let t = self.trigger.as_mut().ok_or("尚未布置触发记录")?;
        if !t.pending_review {
            return Err("当前没有等待接受或拒绝的记录".into());
        }
        let path = self.last_recording.as_ref().ok_or("缺少待审核记录")?;
        let mut meta: Value = serde_json::from_slice(
            &fs::read(path.join("metadata.json")).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        meta["acceptance"] = json!(if accepted { "accepted" } else { "rejected" });
        let temp = path.join("metadata.json.tmp");
        fs::write(&temp, serde_json::to_vec_pretty(&meta).unwrap()).map_err(|e| e.to_string())?;
        fs::rename(temp, path.join("metadata.json")).map_err(|e| e.to_string())?;
        t.review(accepted, self.total_scans)?;
        if accepted {
            self.advance_group(
                meta["recording_options"]["group_number"]
                    .as_u64()
                    .unwrap_or(0),
            )?;
        }
        Ok(())
    }

    fn start_collection(
        &mut self,
        mut profiles: Vec<Profile>,
        path: Option<PathBuf>,
    ) -> Result<(), String> {
        if self.running || self.input.is_some() || self.collection.is_some() {
            return Err("请先断开当前会话，再建立多设备采集".into());
        }
        if !(2..=4).contains(&profiles.len()) {
            return Err("同步集合需要 2–4 台 DT9837C".into());
        }
        let source = profiles[0].source;
        let rate = profiles[0].config.sample_rate;
        let duration = profiles[0].recording_options.duration_seconds;
        let mut selectors = std::collections::BTreeSet::new();
        for (i, p) in profiles.iter_mut().enumerate() {
            p.sync_mode = if i == 0 { "master" } else { "slave" }.into();
            p.defer_hardware_start = true;
            if p.linked_output.is_some() {
                return Err("同步集合的 AO 请通过主设备独立启动".into());
            }
            p.validate()?;
            if p.source != source
                || p.config.sample_rate != rate
                || p.recording_options.duration_seconds != duration
            {
                return Err("同步集合必须使用同一输入类型、采样率和记录时长".into());
            }
            if p.trigger.segmented() {
                return Err("多设备集合当前支持同步连续/定长记录，触发分段请使用单设备会话".into());
            }
            if source != Source::Simulation {
                let selector = p
                    .device_selector
                    .as_ref()
                    .ok_or("多设备配置必须明确选择各台设备")?;
                if source == Source::Vendor && selector.vendor_name.is_none() {
                    return Err("原厂多设备配置必须明确选择各台设备名".into());
                }
                if !selectors.insert((
                    selector.bus,
                    selector.ports.clone(),
                    selector.vendor_name.clone(),
                )) {
                    return Err("同步集合不能重复选择同一台设备".into());
                }
            }
        }
        if let Some(path) = &path {
            fs::create_dir_all(path.parent().ok_or("集合目录无父路径")?)
                .map_err(|e| e.to_string())?;
            fs::create_dir(path).map_err(|e| e.to_string())?;
        }
        let mut devices: Vec<_> = (0..profiles.len()).map(|_| State::new()).collect();
        // Configure the master's clock first; all USB buffers are then prepared
        // before any start command. USB scheduling itself is never synchronization.
        for i in 0..profiles.len() {
            if let Err(error) = devices[i].command(Command::Start {
                profile: Box::new(profiles[i].clone()),
                recording_path: path.as_ref().map(|p| p.join(format!("device-{i:02}"))),
            }) {
                for device in &mut devices {
                    device.error = error.clone();
                    device.termination_reason = "collection_start_failed".into();
                    device.stop_input(false);
                    let _ = device.finish_recording();
                }
                return Err(error);
            }
        }
        for i in (1..profiles.len()).chain(std::iter::once(0)) {
            if let Some(usb) = &devices[i].input {
                if let Err(error) = usb.arm(false) {
                    for device in &mut devices {
                        device.error = error.to_string();
                        device.termination_reason = "collection_start_failed".into();
                        device.stop_input(false);
                        let _ = device.finish_recording();
                    }
                    return Err(error.to_string());
                }
            }
        }
        if let Some(path) = path {
            fs::write(path.join("collection.json"),serde_json::to_vec_pretty(&json!({"format":"labdaq-collection-v1",
                "source":source,"sample_rate":devices[0].profile.as_ref().unwrap().actual_rate,
                "synchronization":if source==Source::Simulation {"shared_simulated_sample_index"} else {"LVDS_master_slave_configured_not_hardware_verified"},
                "devices":profiles.iter().enumerate().map(|(i,p)|json!({"directory":format!("device-{i:02}"),"role":p.sync_mode,"selector":p.device_selector})).collect::<Vec<_>>() })).unwrap()).map_err(|e|e.to_string())?;
        }
        self.profile = devices[0].profile.clone();
        self.running = true;
        self.collection = Some(devices);
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
    let mut conditional_shutdown = false;
    let response = match request {
        Ok(r) if r.token == token => {
            shutdown = matches!(r.command, Command::Shutdown | Command::ShutdownIfIdle);
            conditional_shutdown = matches!(r.command, Command::ShutdownIfIdle);
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
    if conditional_shutdown {
        shutdown &= response
            .as_ref()
            .is_ok_and(|state| state["shutdown"] == true);
    }
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
    if args.len() != 4 && !(args.len() == 6 && args[4] == "--firmware") {
        return Err("usage: labdaq-core ENDPOINT_FILE PORT TOKEN [--firmware PATH]".into());
    }
    if args.len() == 6 {
        usb_boot::set_firmware_path(PathBuf::from(&args[5]));
    }
    let endpoint = PathBuf::from(&args[1]);
    let profile_path = endpoint.with_extension("profile.json");
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
    if let Ok(bytes) = fs::read(&profile_path) {
        if let Ok(profile) = serde_json::from_slice::<Profile>(&bytes) {
            if profile.validate().is_ok() {
                state.profile = Some(profile);
            }
        }
    }
    state.persistence_path = Some(profile_path);
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
                if state.input.is_some()
                    || state
                        .collection
                        .as_ref()
                        .is_some_and(|d| d.iter().any(|v| v.input.is_some()))
                {
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
            let starting = matches!(
                envelope.command,
                Command::Start { .. } | Command::StartCollection { .. }
            );
            let conditional_shutdown = matches!(envelope.command, Command::ShutdownIfIdle);
            let shutdown = matches!(
                envelope.command,
                Command::Shutdown | Command::ShutdownIfIdle
            );
            let result = state.command(envelope.command);
            let exit = shutdown
                && result
                    .as_ref()
                    .is_ok_and(|state| !conditional_shutdown || state["shutdown"] == true);
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
            device_selector: None,
            trigger: trigger::Settings::default(),
            sync_mode: default_sync_mode(),
            defer_hardware_start: false,
            linked_output: None,
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
                point_number: None,
                direction: String::new(),
                engineering_offset: 0.0,
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
    fn channel_sensitivity_accepts_zero_only_for_voltage_profiles() {
        let mut p = profile();
        for channel in &mut p.channels {
            channel.sensitivity_mv_per_unit = 0.0;
        }
        assert!(p.validate().is_ok());
        for invalid in [-1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            p.channels[0].sensitivity_mv_per_unit = invalid;
            assert!(p.validate().is_err());
        }
        p.channels[0].sensitivity_mv_per_unit = 0.0;
        p.channels[0].unit = "N".into();
        assert!(p.validate().is_err());
        p.channels[0].sensitivity_mv_per_unit = 10.0;
        assert!(p.validate().is_ok());
    }

    #[test]
    fn channel_sensitivity_validation_is_preserved_for_live_updates() {
        let mut state = State::new();
        let mut p = profile();
        for channel in &mut p.channels {
            channel.sensitivity_mv_per_unit = 0.0;
        }
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: None,
            })
            .unwrap();
        let mut channels = state.profile.as_ref().unwrap().channels.clone();
        channels[0].unit = "N".into();
        assert!(
            state
                .command(Command::SetChannels {
                    channels: channels.clone(),
                })
                .is_err()
        );
        assert_eq!(state.profile.as_ref().unwrap().channels[0].unit, "V");
        channels[0].sensitivity_mv_per_unit = 10.0;
        state.command(Command::SetChannels { channels }).unwrap();
        assert_eq!(state.profile.as_ref().unwrap().channels[0].unit, "N");
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
        state
            .command(Command::Start {
                profile: Box::new(profile()),
                recording_path: None,
            })
            .unwrap();
        let values: Vec<f64> = (0..80000).map(|i| i as f64 / 10000.0).collect();
        state.accept_values(values.clone()).unwrap();
        let window = state.poll(20.0, None);
        assert_eq!(window["snapshot_start"], 0);
        assert_eq!(window["total_scans"], 40000);
        assert_eq!(window["raw"], json!(values));
    }

    #[test]
    fn starting_native_recording_preserves_the_generic_recording_root() {
        let root = recording_path("recording-root");
        let recording_root = root.join("recordings");
        let path = recording_root.join("实验名").join("工况-001");
        let mut p = profile();
        p.recording_root = recording_root.clone();
        let mut state = State::new();
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: None,
            })
            .unwrap();
        state.start_recording(path).unwrap();
        assert_eq!(
            state.profile.as_ref().unwrap().recording_root,
            recording_root
        );
        state.finish_recording().unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn direct_duration_recording_crops_disk_and_keeps_preview_running() {
        let path = recording_path("direct");
        let mut p = profile();
        p.recording_options = RecordingOptions {
            duration_seconds: Some(0.003),
            notes: "定长备注".into(),
            specimen: "试样 A".into(),
            condition: "室温 · 10 Hz".into(),
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
        assert_eq!(info["recording_options"]["specimen"], "试样 A");
        assert_eq!(info["recording_options"]["condition"], "室温 · 10 Hz");
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
                    specimen: String::new(),
                    condition: String::new(),
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

    #[test]
    fn triggered_recording_writes_raw_pretrigger_and_retains_rejected_attempts() {
        let mut p = profile();
        p.recording_options.duration_seconds = Some(0.002);
        p.trigger = trigger::Settings {
            mode: "analog_rising".into(),
            pretrigger_percent: 50.,
            retrigger: true,
            accept_reject: true,
            record_count: 1,
            ..trigger::Settings::default()
        };
        let path = recording_path("trigger-review");
        let mut state = State::new();
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: Some(path.clone()),
            })
            .unwrap();
        assert!(!path.exists());
        assert!(state.command(Command::Shutdown).is_err());
        state
            .accept_values(vec![-2., 20., -1., 21., 0., 22., 1., 23.])
            .unwrap();
        assert_eq!(metadata(&path)["trigger"]["file_scan"], 2);
        assert_eq!(metadata(&path)["sample_count"], 4);
        assert_eq!(metadata(&path)["acceptance"], "pending");
        let raw = fs::read(path.join("raw.f64")).unwrap();
        let values: Vec<_> = raw
            .chunks_exact(8)
            .map(|x| f64::from_le_bytes(x.try_into().unwrap()))
            .collect();
        assert_eq!(values, [-2., 20., -1., 21., 0., 22., 1., 23.]);
        assert!(
            state
                .command(Command::SetChannels {
                    channels: profile().channels
                })
                .is_err()
        );
        state.command(Command::RejectRecord).unwrap();
        assert_eq!(metadata(&path)["acceptance"], "rejected");
        state
            .accept_values(vec![-2., 30., -1., 31., 0., 32., 1., 33.])
            .unwrap();
        state.command(Command::AcceptRecord).unwrap();
        let next = path.with_file_name("trigger-review-trigger-0002");
        assert_eq!(metadata(&next)["acceptance"], "accepted");
        assert_eq!(metadata(&next)["trigger"]["source_scan"], 6);
        assert!(state.trigger.as_ref().unwrap().finished);
        assert_eq!(state.trigger.as_ref().unwrap().accepted, 1);
        assert!(state.command(Command::AcceptRecord).is_err());
        state.command(Command::Stop).unwrap();
    }

    #[test]
    fn software_trigger_cancel_and_retrigger_finish_without_client_polling() {
        let mut p = profile();
        p.recording_options.duration_seconds = Some(0.001);
        p.trigger = trigger::Settings {
            mode: "software".into(),
            retrigger: true,
            record_count: 2,
            ..trigger::Settings::default()
        };
        let path = recording_path("trigger-software");
        let mut state = State::new();
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: Some(path.clone()),
            })
            .unwrap();
        state.accept_values(vec![0.; 40]).unwrap();
        assert!(!path.exists());
        state.command(Command::FireTrigger).unwrap();
        state.accept_values(vec![1.; 10]).unwrap();
        assert_eq!(metadata(&path)["sample_count"], 2);
        assert!(state.trigger.as_ref().unwrap().waiting);
        state.command(Command::FireTrigger).unwrap();
        state.accept_values(vec![2.; 10]).unwrap();
        assert!(state.trigger.as_ref().unwrap().finished);
        assert!(state.running);
        state.command(Command::CancelTrigger).unwrap();
        assert!(state.trigger.is_none());
        assert!(state.command(Command::FireTrigger).is_err());
        state.command(Command::Stop).unwrap();
    }

    #[test]
    fn simulated_output_updates_mutes_completes_and_survives_input_stop() {
        let mut state = State::new();
        state
            .command(Command::Start {
                profile: Box::new(profile()),
                recording_path: None,
            })
            .unwrap();
        let mut settings = output::Settings::default();
        state
            .command(Command::StartOutput {
                settings: settings.clone(),
            })
            .unwrap();
        assert!(
            state
                .command(Command::StartOutput {
                    settings: settings.clone()
                })
                .is_err()
        );
        state.command(Command::Stop).unwrap();
        assert_eq!(state.poll(0., None)["output"]["running"], true);
        state.command(Command::MuteOutput { muted: true }).unwrap();
        assert_eq!(state.poll(0., None)["output"]["settings"]["muted"], true);
        settings.sample_rate = 96000.;
        assert!(
            state
                .command(Command::UpdateOutput {
                    settings: settings.clone()
                })
                .is_err()
        );
        settings.sample_rate = 48000.;
        settings.duration_seconds = Some(0.1);
        state.command(Command::UpdateOutput { settings }).unwrap();
        state.simulated_output.as_mut().unwrap().started = Instant::now() - Duration::from_secs(1);
        let result = state
            .command(Command::Poll {
                seconds: 0.,
                after_scan: None,
            })
            .unwrap();
        assert_eq!(result["output"]["completed"], true);
        assert_eq!(result["output"]["zeroed"], true);
        state.command(Command::Shutdown).unwrap();
    }

    #[test]
    fn simulated_collection_aligns_sample_indices_and_broadcasts_pause() {
        let mut p = profile();
        p.recording_options.duration_seconds = Some(0.05);
        let path = recording_path("collection");
        let mut state = State::new();
        state
            .command(Command::StartCollection {
                profiles: vec![p.clone(), p],
                recording_path: Some(path.clone()),
            })
            .unwrap();
        state.sample().unwrap();
        let poll = state.poll(0.1, None);
        assert_eq!(
            poll["collection"][0]["total_scans"],
            poll["collection"][1]["total_scans"]
        );
        state.command(Command::PauseRecording).unwrap();
        let before = state.poll(0., None)["recorded_scans"].clone();
        state.sample().unwrap();
        assert_eq!(state.poll(0., None)["recorded_scans"], before);
        state.command(Command::ResumeRecording).unwrap();
        state.sample().unwrap();
        state.command(Command::Stop).unwrap();
        let master = metadata(&path.join("device-00"));
        let slave = metadata(&path.join("device-01"));
        assert_eq!(master["sample_count"], slave["sample_count"]);
        assert_eq!(master["recording_segments"], slave["recording_segments"]);
        assert_eq!(master["acquisition_config"]["sync_mode"], "master");
        assert_eq!(slave["acquisition_config"]["sync_mode"], "slave");
        state.command(Command::Disconnect).unwrap();
        assert!(state.collection.is_none());
        let mut bad = profile();
        bad.config.sample_rate = 1000.;
        assert!(
            state
                .command(Command::StartCollection {
                    profiles: vec![profile(), bad],
                    recording_path: None
                })
                .is_err()
        );
    }

    #[test]
    fn linked_output_starts_and_stops_with_simulated_acquisition() {
        let mut state = State::new();
        let mut p = profile();
        p.linked_output = Some(output::Settings::default());
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: None,
            })
            .unwrap();
        assert_eq!(state.poll(0., None)["output"]["running"], true);
        state.command(Command::Stop).unwrap();
        assert_eq!(state.poll(0., None)["output"]["running"], false);
        assert_eq!(state.poll(0., None)["output"]["zeroed"], true);
    }

    #[test]
    fn idle_shutdown_is_atomic_for_preview_recording_output_and_pending_trigger() {
        let mut state = State::new();
        assert_eq!(
            state.command(Command::ShutdownIfIdle).unwrap()["shutdown"],
            true
        );
        state
            .command(Command::Start {
                profile: Box::new(profile()),
                recording_path: None,
            })
            .unwrap();
        assert_eq!(
            state.command(Command::ShutdownIfIdle).unwrap()["shutdown"],
            false
        );
        assert!(state.running);
        state
            .command(Command::StartOutput {
                settings: output::Settings::default(),
            })
            .unwrap();
        state.command(Command::Stop).unwrap();
        assert_eq!(
            state.command(Command::ShutdownIfIdle).unwrap()["shutdown"],
            false
        );
        state.command(Command::StopOutput).unwrap();
        assert_eq!(
            state.command(Command::ShutdownIfIdle).unwrap()["shutdown"],
            true
        );
        let mut p = profile();
        p.trigger = trigger::Settings {
            mode: "software".into(),
            accept_reject: true,
            ..trigger::Settings::default()
        };
        p.recording_options.duration_seconds = Some(0.001);
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: Some(recording_path("idle-trigger")),
            })
            .unwrap();
        state.command(Command::FireTrigger).unwrap();
        state.accept_values(vec![1.; 8]).unwrap();
        assert!(state.trigger.as_ref().unwrap().pending_review);
        assert_eq!(
            state.command(Command::ShutdownIfIdle).unwrap()["shutdown"],
            false
        );
        state.command(Command::CancelTrigger).unwrap();
        state.command(Command::Stop).unwrap();
        assert_eq!(
            state.command(Command::ShutdownIfIdle).unwrap()["shutdown"],
            true
        );
    }

    #[test]
    fn completed_group_number_persists_once_and_metadata_keeps_original() {
        let path = recording_path("group-increment");
        let save = recording_path("group-profile.json");
        fs::create_dir_all(save.parent().unwrap()).unwrap();
        let mut state = State::new();
        state.persistence_path = Some(save.clone());
        let mut p = profile();
        p.recording_options.group_number = 7;
        p.recording_options.duration_seconds = Some(0.001);
        state
            .command(Command::Start {
                profile: Box::new(p),
                recording_path: Some(path.clone()),
            })
            .unwrap();
        state.accept_values(vec![1.; 8]).unwrap();
        assert_eq!(metadata(&path)["recording_options"]["group_number"], 7);
        assert_eq!(
            state
                .profile
                .as_ref()
                .unwrap()
                .recording_options
                .group_number,
            8
        );
        state.command(Command::StopRecording).unwrap();
        assert_eq!(
            state
                .profile
                .as_ref()
                .unwrap()
                .recording_options
                .group_number,
            8
        );
        let restored: Profile = serde_json::from_slice(&fs::read(save).unwrap()).unwrap();
        assert_eq!(restored.recording_options.group_number, 8);
        state
            .command(Command::StartRecording {
                path: recording_path("group-error"),
                channels: profile().channels,
                experiment: String::new(),
                recording_options: restored.recording_options,
            })
            .unwrap();
        state.error = "simulated disk failure".into();
        state.termination_reason = "write_error".into();
        state.finish_recording().unwrap();
        assert_eq!(
            state
                .profile
                .as_ref()
                .unwrap()
                .recording_options
                .group_number,
            8
        );
    }
}
