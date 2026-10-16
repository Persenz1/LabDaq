//! Independent simulation acquisition; no device or vendor runtime access.
mod signal;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::mpsc::{self, SyncSender};
use std::thread;
use std::time::{Duration, Instant};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

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

#[derive(Clone, Deserialize, Serialize)]
struct Profile {
    config: Config,
    actual_rate: f64,
    channels: [Channel; 4],
    signal_settings: signal::Signals,
    experiment: String,
    recording_root: PathBuf,
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
        self.signal_settings.validate()
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
    fn new(path: PathBuf, p: &Profile, start: u64) -> Result<Self, String> {
        let parent = path.parent().ok_or("记录路径无父目录")?;
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        fs::create_dir(&path).map_err(|e| e.to_string())?; // Never overwrite a group.
        let mut config = serde_json::to_value(&p.config).unwrap();
        config["sample_rate"] = json!(p.actual_rate);
        config["record_start_scan"] = json!(start);
        config["input"] = json!({"backend": "rust-core-v1", "scenario": "custom",
            "noise_algorithm": "splitmix64-box-muller-v1", "signal_settings": p.signal_settings});
        let channels: Vec<_> = p.config.channels.iter().map(|i| &p.channels[*i]).collect();
        let metadata = json!({"format": "labdaq-raw-voltage", "format_version": 1,
            "dtype": "<f8", "layout": "sample-major", "voltage_unit": "V",
            "sample_rate": p.actual_rate, "channels": channels, "source": "simulation",
            "acquisition_config": config, "experiment": p.experiment,
            "started_at_utc": utc_now(), "sample_count": 0, "finalized": false});
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

    fn write(&mut self, values: &[f64]) -> Result<(), String> {
        let bytes: Vec<u8> = values.iter().flat_map(|v| v.to_le_bytes()).collect();
        if let Err(e) = self.file.write_all(&bytes) {
            // A disk error may leave part of the last frame. Keep complete frames only.
            let size = self.file.metadata().map_err(|e| e.to_string())?.len();
            let frame_bytes = self.width as u64 * 8;
            self.frames = size / frame_bytes;
            self.file
                .set_len(self.frames * frame_bytes)
                .map_err(|e| e.to_string())?;
            return Err(e.to_string());
        }
        self.frames += (values.len() / self.width) as u64;
        Ok(())
    }

    fn close(mut self, reason: &str, error: &str) -> Result<(), String> {
        self.file.flush().map_err(|e| e.to_string())?;
        self.metadata["sample_count"] = json!(self.frames);
        self.metadata["finalized"] = json!(true);
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
    },
    Start {
        profile: Box<Profile>,
        recording_path: Option<PathBuf>,
    },
    StartRecording {
        path: PathBuf,
        channels: [Channel; 4],
        experiment: String,
    },
    SetChannels {
        channels: [Channel; 4],
    },
    StopRecording,
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
    profile: Option<Profile>,
    running: bool,
    recorder: Option<Recorder>,
    last_recording: Option<PathBuf>,
    recorded_scans: u64,
    total_scans: u64,
    ring: VecDeque<f64>,
    capacity: usize,
    previous_x: [f64; 4],
    previous_y: [f64; 4],
    error: String,
    termination_reason: String,
}

impl State {
    fn new() -> Self {
        Self {
            profile: None,
            running: false,
            recorder: None,
            last_recording: None,
            recorded_scans: 0,
            total_scans: 0,
            ring: VecDeque::new(),
            capacity: 0,
            previous_x: [0.0; 4],
            previous_y: [0.0; 4],
            error: String::new(),
            termination_reason: "user_stop".into(),
        }
    }

    fn poll(&self, seconds: f64) -> Value {
        let width = self.profile.as_ref().map_or(1, |p| p.config.channels.len());
        let rate = self.profile.as_ref().map_or(1.0, |p| p.actual_rate);
        let n = ((seconds.clamp(0.0, 12.0) * rate).round() as usize).min(self.ring.len() / width);
        let raw: Vec<f64> = self
            .ring
            .iter()
            .skip(self.ring.len() - n * width)
            .copied()
            .collect();
        json!({"pid": std::process::id(), "running": self.running, "recording": self.recorder.is_some(),
            "profile": self.profile, "total_scans": self.total_scans, "recorded_scans": self.recorded_scans,
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
        let recorder = Recorder::new(path.clone(), p, self.total_scans)?;
        p.recording_root = path.parent().unwrap().to_path_buf();
        self.recorder = Some(recorder);
        self.last_recording = Some(path);
        self.recorded_scans = 0;
        Ok(())
    }

    fn finish_recording(&mut self) -> Result<(), String> {
        if let Some(r) = self.recorder.take() {
            self.recorded_scans = r.frames;
            r.close(&self.termination_reason, &self.error)?;
        }
        Ok(())
    }

    fn command(&mut self, command: Command) -> Result<Value, String> {
        match command {
            Command::Poll { seconds } => return Ok(self.poll(seconds)),
            Command::Start {
                profile,
                recording_path,
            } => {
                if self.running {
                    return Err("核心正在采集，请接回当前会话".into());
                }
                profile.validate()?;
                *self = Self::new();
                self.capacity = ((profile.actual_rate * 12.0).round() as usize).clamp(1, 200000);
                self.ring = VecDeque::with_capacity(self.capacity * profile.config.channels.len());
                self.profile = Some(*profile);
                self.running = true;
                if let Some(path) = recording_path {
                    if let Err(e) = self.start_recording(path) {
                        self.running = false;
                        return Err(e);
                    }
                }
            }
            Command::StartRecording {
                path,
                channels,
                experiment,
            } => {
                self.set_channels(channels)?;
                self.profile.as_mut().unwrap().experiment = experiment;
                self.start_recording(path)?;
            }
            Command::SetChannels { channels } => self.set_channels(channels)?,
            Command::StopRecording => self.finish_recording()?,
            Command::Stop => {
                self.running = false;
                self.finish_recording()?;
            }
            Command::Shutdown => {
                if self.recorder.is_some() {
                    return Err("请先结束记录再退出核心".into());
                }
                self.running = false;
            }
        }
        Ok(self.poll(0.0))
    }

    fn sample(&mut self) -> Result<(), String> {
        let p = self.profile.as_ref().unwrap();
        let width = p.config.channels.len();
        let count = (p.actual_rate * 0.05).round().max(1.0) as usize;
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
        if let Some(r) = &mut self.recorder {
            r.write(&values)?;
            self.recorded_scans = r.frames;
        }
        let excess = (self.ring.len() + values.len()).saturating_sub(self.capacity * width);
        self.ring.drain(..excess);
        self.ring.extend(values);
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
    let _ = serde_json::to_writer(&mut stream, &body)
        .and_then(|_| stream.write_all(b"\n").map_err(serde_json::Error::io));
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
                state.error = error;
                state.termination_reason = "error".into();
                state.running = false;
                if let Err(e) = state.finish_recording() {
                    state.error.push_str(&format!("; 保存未完成: {e}"));
                }
            } else {
                let rate = state.profile.as_ref().unwrap().actual_rate;
                deadline += Duration::from_secs_f64((rate * 0.05).round() / rate);
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
