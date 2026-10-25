mod analysis_pages;
mod data_pages;
mod pages;
#[cfg(test)]
mod qa;
mod qa_runtime;
use crate::{
    client::{CoreClient, user_directory},
    model::{Settings, engineering},
};
use eframe::egui::{self, Color32, RichText};
use egui_plot::{Legend, Line, Plot, PlotPoints, VLine};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
    time::Duration,
};
const COLORS: [Color32; 4] = [
    Color32::from_rgb(39, 112, 218),
    Color32::from_rgb(12, 149, 131),
    Color32::from_rgb(220, 137, 40),
    Color32::from_rgb(155, 92, 195),
];
#[derive(Clone, Copy, PartialEq, Debug)]
enum Page {
    Live,
    Library,
    Compare,
    Analysis,
    Output,
    Settings,
}
impl Page {
    fn title(self) -> &'static str {
        match self {
            Self::Live => "实时采集",
            Self::Library => "记录与回看",
            Self::Compare => "多组比较",
            Self::Analysis => "信号分析",
            Self::Output => "信号输出",
            Self::Settings => "设置",
        }
    }
}
struct CoreWorker {
    tx: SyncSender<Value>,
    rx: Receiver<Result<Value, String>>,
    stop: Arc<AtomicBool>,
    display_seconds: Arc<AtomicU64>,
}
impl CoreWorker {
    fn new() -> Self {
        let (tx, commands) = mpsc::sync_channel::<Value>(16);
        let (events, rx) = mpsc::sync_channel(4);
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let display_seconds = Arc::new(AtomicU64::new(8.0f64.to_bits()));
        let window = display_seconds.clone();
        std::thread::spawn(move || {
            let endpoint = user_directory().join("runtime/core.json");
            let mut client = None;
            let mut previous_end = None;
            let mut raw: Vec<Value> = Vec::new();
            let mut shutdown = false;
            let mut last_seconds = 8.0;
            while !stopping.load(Ordering::Acquire) {
                if client.is_none() {
                    match CoreClient::connect_or_start(&endpoint) {
                        Ok((c, s)) => {
                            client = Some(c);
                            let _ = events.try_send(Ok(s));
                        }
                        Err(e) => {
                            let _ = events.try_send(Err(e));
                            std::thread::sleep(Duration::from_millis(500));
                            continue;
                        }
                    }
                }
                let seconds = f64::from_bits(window.load(Ordering::Relaxed)).clamp(0.001, 60.0);
                if seconds != last_seconds {
                    previous_end = None;
                    last_seconds = seconds;
                }
                let command = match commands.recv_timeout(Duration::from_millis(80)) {
                    Ok(v) => v,
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        if shutdown {
                            continue;
                        }
                        json!({"op":"poll","seconds":seconds,"after_scan":previous_end})
                    }
                    Err(_) => break,
                };
                if shutdown && command["op"] != "poll" {
                    match CoreClient::connect_or_start(&endpoint) {
                        Ok((c, _)) => client = Some(c),
                        Err(e) => {
                            let _ = events.try_send(Err(e));
                            continue;
                        }
                    }
                }
                let polling = command["op"] == "poll";
                shutdown = command["op"] == "shutdown";
                match client.as_ref().unwrap().request(command) {
                    Ok(mut s) => {
                        if polling && s["profile"].is_object() {
                            let width = s["profile"]["config"]["channels"]
                                .as_array()
                                .map_or(1, Vec::len)
                                .max(1);
                            if s["snapshot_start"].as_u64() != previous_end {
                                raw.clear();
                            }
                            if let Some(v) = s["raw"].as_array() {
                                raw.extend(v.iter().cloned());
                            }
                            let limit = s["capacity"].as_u64().unwrap_or(200000).min(
                                (s["profile"]["actual_rate"].as_f64().unwrap_or(2000.0) * seconds)
                                    as u64,
                            ) as usize
                                * width;
                            if raw.len() > limit {
                                raw.drain(..raw.len() - limit);
                            }
                            previous_end = s["total_scans"].as_u64();
                            s["snapshot_start"] = json!(
                                previous_end
                                    .unwrap_or(0)
                                    .saturating_sub((raw.len() / width) as u64)
                            );
                            s["raw"] = json!(raw);
                        } else if s["total_scans"]
                            .as_u64()
                            .is_some_and(|n| n < previous_end.unwrap_or(0))
                        {
                            previous_end = None;
                            raw.clear();
                        }
                        if polling {
                            let _ = events.try_send(Ok(s));
                        } else if events.send(Ok(s)).is_err() {
                            break;
                        }
                    }
                    Err(e) => {
                        if polling
                            && (e.contains("无法连接采集核心") || e.contains("核心响应不完整"))
                        {
                            client = None;
                            previous_end = None;
                            raw.clear();
                        }
                        let _ = events.try_send(Err(e));
                    }
                }
            }
        });
        Self {
            tx,
            rx,
            stop,
            display_seconds,
        }
    }
    fn send(&self, v: Value) -> Result<(), String> {
        self.tx.try_send(v).map_err(|e| format!("操作队列忙：{e}"))
    }
}
impl Drop for CoreWorker {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
    }
}
struct Job {
    name: String,
    rx: Receiver<Result<Value, String>>,
    cancel: Arc<AtomicBool>,
    progress: Arc<std::sync::Mutex<(u64, u64)>>,
}
#[derive(Clone, Default)]
struct WaveView {
    times: Vec<f64>,
    samples: Vec<Vec<f64>>,
    channels: Vec<Value>,
    sample_rate: f64,
    gaps: Vec<usize>,
}
pub struct LabDaqApp {
    qa_run: Option<qa_runtime::Run>,
    data_ui: data_pages::DataPageState,
    devices_found: Vec<Value>,
    settings: Settings,
    page: Page,
    worker: CoreWorker,
    state: Value,
    first_state: bool,
    demo: bool,
    error: String,
    notice: String,
    live: WaveView,
    replay: WaveView,
    analysis_result: Value,
    filter_result: Value,
    filtered: WaveView,
    output_preview: WaveView,
    job: Option<Job>,
    library: Vec<Value>,
    query: String,
    selected: Vec<String>,
    open_path: String,
    replay_info: Value,
    replay_start: f64,
    replay_count: usize,
    comparison_result: Value,
    comparison_paths: Vec<String>,
    comparison_axis: String,
    cursor_a: f64,
    cursor_b: f64,
    cursors: bool,
    freeze: bool,
    raw_view: bool,
    stats: Value,
    live_snapshot: Value,
    last_zero: Option<Vec<Value>>,
    plot_height: f32,
    line_width: f32,
    log_y: bool,
    last_status_recording: bool,
    config_file: PathBuf,
    edit_notes: String,
    edit_specimen: String,
    job_request: Value,
    last_capture: Option<PathBuf>,
}
fn number(
    ui: &mut egui::Ui,
    v: &mut Value,
    key: &str,
    label: &str,
    range: std::ops::RangeInclusive<f64>,
    speed: f64,
) {
    let mut n = v[key].as_f64().unwrap_or(0.0);
    ui.horizontal(|ui| {
        ui.label(label);
        if ui
            .add(egui::DragValue::new(&mut n).range(range).speed(speed))
            .changed()
        {
            v[key] = json!(n);
        }
    });
}
fn integer(
    ui: &mut egui::Ui,
    v: &mut Value,
    key: &str,
    label: &str,
    range: std::ops::RangeInclusive<u64>,
) {
    let mut n = v[key].as_u64().unwrap_or(0);
    ui.horizontal(|ui| {
        ui.label(label);
        if ui.add(egui::DragValue::new(&mut n).range(range)).changed() {
            v[key] = json!(n);
        }
    });
}
fn flag(ui: &mut egui::Ui, v: &mut Value, key: &str, label: &str) {
    let mut b = v[key].as_bool().unwrap_or(false);
    if ui.checkbox(&mut b, label).changed() {
        v[key] = json!(b);
    }
}
fn text(ui: &mut egui::Ui, v: &mut Value, key: &str, label: &str) {
    let mut s = v[key].as_str().unwrap_or("").to_owned();
    ui.horizontal(|ui| {
        ui.label(label);
        if ui.text_edit_singleline(&mut s).changed() {
            v[key] = json!(s);
        }
    });
}
fn choice(ui: &mut egui::Ui, v: &mut Value, key: &str, label: &str, items: &[(&str, &str)]) {
    let mut s = v[key].as_str().unwrap_or(items[0].0).to_owned();
    egui::ComboBox::from_id_salt((label, key))
        .selected_text(format!(
            "{label}：{}",
            items
                .iter()
                .find(|(id, _)| *id == s)
                .map_or(s.as_str(), |(_, n)| *n)
        ))
        .show_ui(ui, |ui| {
            for (id, n) in items {
                ui.selectable_value(&mut s, (*id).into(), *n);
            }
        });
    v[key] = json!(s);
}
pub fn install_fonts(ctx: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();
    for path in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ] {
        if let Ok(bytes) = std::fs::read(path) {
            fonts
                .font_data
                .insert("chinese".into(), egui::FontData::from_owned(bytes).into());
            fonts
                .families
                .get_mut(&egui::FontFamily::Proportional)
                .unwrap()
                .insert(0, "chinese".into());
            fonts
                .families
                .get_mut(&egui::FontFamily::Monospace)
                .unwrap()
                .push("chinese".into());
            break;
        }
    }
    ctx.set_fonts(fonts);
}
impl LabDaqApp {
    pub fn new(cc: &eframe::CreationContext<'_>, demo: bool) -> Self {
        install_fonts(&cc.egui_ctx);
        let mut style = (*cc.egui_ctx.style()).clone();
        style.visuals = egui::Visuals::light();
        style.spacing.item_spacing = egui::vec2(10.0, 9.0);
        style.visuals.selection.bg_fill = Color32::from_rgb(40, 105, 210);
        style
            .text_styles
            .insert(egui::TextStyle::Body, egui::FontId::proportional(15.0));
        cc.egui_ctx.set_style(style);
        let config_file = user_directory().join("settings.json");
        let mut settings = Settings::load(&config_file).unwrap_or_default();
        if demo {
            settings.source = "simulation".into();
            settings.auto_connect = false;
        }
        Self {
            qa_run: qa_runtime::Run::from_args(),
            data_ui: Default::default(),
            devices_found: vec![],
            settings,
            page: Page::Live,
            worker: CoreWorker::new(),
            state: Value::Null,
            first_state: true,
            demo,
            error: String::new(),
            notice: String::new(),
            live: WaveView::default(),
            replay: WaveView::default(),
            analysis_result: Value::Null,
            filter_result: Value::Null,
            filtered: WaveView::default(),
            output_preview: WaveView::default(),
            job: None,
            library: vec![],
            query: String::new(),
            selected: vec![],
            open_path: String::new(),
            replay_info: Value::Null,
            replay_start: 0.0,
            replay_count: 16000,
            comparison_result: Value::Null,
            comparison_paths: vec![],
            comparison_axis: "relative".into(),
            cursor_a: 0.0,
            cursor_b: 1.0,
            cursors: false,
            freeze: false,
            raw_view: false,
            stats: Value::Null,
            live_snapshot: Value::Null,
            last_zero: None,
            plot_height: 220.0,
            line_width: 1.5,
            log_y: false,
            last_status_recording: false,
            config_file,
            edit_notes: String::new(),
            edit_specimen: String::new(),
            job_request: Value::Null,
            last_capture: None,
        }
    }
    fn command(&mut self, v: Value) {
        if let Err(e) = self.worker.send(v) {
            self.error = e;
        }
    }
    fn start_job(&mut self, name: &str, request: Value) {
        if self.job.is_some() {
            self.error = "请等待当前任务完成或取消。".into();
            return;
        }
        self.error.clear();
        self.job_request = request.clone();
        let (tx, rx) = mpsc::channel();
        let cancel = Arc::new(AtomicBool::new(false));
        let flag = cancel.clone();
        let progress = Arc::new(std::sync::Mutex::new((0, 0)));
        let pg = progress.clone();
        std::thread::spawn(move || {
            let result = match request["task"].as_str() {
                Some("analysis") => labdaq_analysis::analyze_json(&request),
                Some("filter") => labdaq_analysis::filter_json(&request),
                _ => {
                    let notify = |done, total| {
                        *pg.lock().unwrap() = (done, total);
                    };
                    let control = labdaq_data::OperationControl {
                        cancelled: Some(&flag),
                        progress: Some(&notify),
                    };
                    labdaq_data::execute_json_with_control(&request, &control)
                }
            };
            let result = if flag.load(Ordering::Acquire) {
                Err("任务已取消，结果未应用".into())
            } else {
                result
            };
            let _ = tx.send(result);
        });
        self.job = Some(Job {
            name: name.into(),
            rx,
            cancel,
            progress,
        });
    }
    fn receive(&mut self) {
        while let Ok(event) = self.worker.rx.try_recv() {
            match event {
                Err(e) => self.error = e,
                Ok(s) => {
                    if s["samples"].is_array() && s["sample_rate"].is_number() {
                        let values: Vec<f64> = s["samples"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .filter_map(Value::as_f64)
                            .collect();
                        let rate = s["sample_rate"].as_f64().unwrap();
                        self.output_preview = WaveView {
                            times: (0..values.len()).map(|i| i as f64 / rate).collect(),
                            samples: vec![values],
                            channels: vec![json!({"name":"AO 预览","unit":"V"})],
                            sample_rate: rate,
                            gaps: vec![],
                        };
                        continue;
                    }
                    if let Some(devices) = s["devices"].as_array() {
                        self.devices_found = devices.clone();
                        continue;
                    }
                    if s.get("running").is_none() {
                        self.notice = serde_json::to_string_pretty(&s).unwrap_or_default();
                        continue;
                    }
                    if self.first_state {
                        self.first_state = false;
                        if let Some(output) = s["output"].get("settings").filter(|v| v.is_object())
                        {
                            self.settings.output = output.clone();
                        }
                        if s["profile"].is_object() {
                            self.settings.restore_profile(&s["profile"]);
                        }
                        if self.demo {
                            self.settings.source = "simulation".into();
                            if s["running"] != true {
                                self.command(json!({"op":"start","profile":self.settings.profile(),"recording_path":null}));
                            }
                        } else if self.settings.auto_connect
                            && s["running"] != true
                            && s["device_connected"] != true
                        {
                            self.command(json!({"op":"connect","profile":self.settings.profile()}));
                        }
                    }
                    if self.last_status_recording
                        || s["recording"] == true
                        || self.state["trigger"] != s["trigger"]
                    {
                        if let Some(group) =
                            s["profile"]["recording_options"]["group_number"].as_u64()
                        {
                            self.settings.group_number = group.max(1);
                        }
                    }
                    self.last_status_recording = s["recording"] == true;
                    if !self.freeze && s["raw"].as_array().is_some_and(|x| !x.is_empty()) {
                        self.live_snapshot = s.clone();
                        self.refresh_live();
                    }
                    if let Some(e) = s["error"].as_str().filter(|x| !x.is_empty()) {
                        self.error = e.into();
                    }
                    self.state = s;
                }
            }
        }
        if let Some(result) = self.job.as_ref().and_then(|j| j.rx.try_recv().ok()) {
            let job = self.job.take().unwrap();
            let result = if job.cancel.load(Ordering::Acquire) {
                Err("任务已取消，结果未应用".into())
            } else {
                result
            };
            let name = job.name;
            match result {
                Err(e) => self.error = e,
                Ok(v) => {
                    self.notice = format!("{name}完成");
                    match name.as_str() {
                        "扫描记录库" => {
                            self.library = v["recordings"].as_array().cloned().unwrap_or_default()
                        }
                        "打开记录" => {
                            self.edit_notes = v["metadata"]["recording_options"]["notes"]
                                .as_str()
                                .unwrap_or("")
                                .into();
                            self.edit_specimen = v["metadata"]["recording_options"]["specimen"]
                                .as_str()
                                .unwrap_or("")
                                .into();
                            self.replay_info = v;
                            self.read_window();
                        }
                        "读取窗口" => self.replay = wave_from_data(&v),
                        "区间统计" => self.stats = v,
                        "频域分析" => self.analysis_result = v,
                        "数字滤波" => {
                            self.filter_result = v.clone();
                            self.filtered = wave_from_data(&self.job_request["input"]);
                            self.filtered.samples = matrix(&v["samples"]);
                        }
                        "加载比较布局" => self.apply_comparison_layout(v),
                        "保存描述" => {
                            self.replay_info = v;
                            self.data_ui.opened_path.clear();
                        }
                        "比较记录" => self.comparison_result = v,
                        _ => {
                            if let Some(errors) = v["errors"].as_array() {
                                let count = v["completed"].as_array().map_or(0, Vec::len);
                                self.notice = format!(
                                    "{name}：成功 {count} 组，失败 {} 组{}",
                                    errors.len(),
                                    if v["cancelled"] == true {
                                        "，已取消"
                                    } else {
                                        ""
                                    }
                                );
                                if !errors.is_empty() {
                                    self.error = errors
                                        .iter()
                                        .map(|e| {
                                            format!(
                                                "{}：{}",
                                                e["path"].as_str().unwrap_or(""),
                                                e["error"].as_str().unwrap_or("失败")
                                            )
                                        })
                                        .collect::<Vec<_>>()
                                        .join("\n");
                                }
                            } else if let Some(p) = v["path"].as_str() {
                                self.notice = format!("{name}完成：{p}");
                            }
                        }
                    }
                }
            }
        }
    }
    fn refresh_live(&mut self) {
        let s = &self.live_snapshot;
        let p = &s["profile"];
        let active = p["config"]["channels"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let rate = p["actual_rate"].as_f64().unwrap_or(2000.0);
        let width = active.len();
        if width == 0 {
            return;
        }
        let raw = s["raw"].as_array().cloned().unwrap_or_default();
        let count = raw.len() / width;
        let keep = count.min((self.settings.display_seconds * rate) as usize);
        let begin = count - keep;
        let start = s["snapshot_start"].as_u64().unwrap_or(0) as f64;
        let channels: Vec<Value> = active
            .iter()
            .map(|i| p["channels"][i.as_u64().unwrap_or(0) as usize].clone())
            .collect();
        let samples = (0..width)
            .map(|col| {
                (begin..count)
                    .map(|row| {
                        let v = raw[row * width + col].as_f64().unwrap_or(f64::NAN);
                        if self.raw_view {
                            v
                        } else {
                            engineering(v, &channels[col])
                        }
                    })
                    .collect()
            })
            .collect();
        self.live = WaveView {
            times: (begin..count).map(|i| (start + i as f64) / rate).collect(),
            samples,
            channels,
            sample_rate: rate,
            gaps: vec![],
        };
    }
    fn read_window(&mut self) {
        self.start_job("读取窗口",json!({"op":"read","path":self.open_path,"start_seconds":self.replay_start,"count":self.replay_count,"values":if self.raw_view{"raw"}else{"engineering"}}));
    }
    fn open_recording(&mut self, path: String) {
        self.open_path = path;
        self.replay_start = 0.0;
        self.data_ui.channel_indices.clear();
        self.replay_info = Value::Null;
        self.replay = WaveView::default();
        self.start_job("打开记录", json!({"op":"open","path":self.open_path}));
    }
    fn plot_toolbar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.checkbox(&mut self.cursors, "双游标");
            let changed = ui.checkbox(&mut self.raw_view, "原始电压").changed();
            if changed {
                self.refresh_live();
                if !self.open_path.is_empty() {
                    self.read_window();
                }
            }
            ui.label("A");
            ui.add(egui::DragValue::new(&mut self.cursor_a).speed(0.001));
            ui.label("B");
            ui.add(egui::DragValue::new(&mut self.cursor_b).speed(0.001));
            let dt = (self.cursor_b - self.cursor_a).abs();
            ui.label(format!("Δt {dt:.6} s"));
            if dt > 0.0 {
                ui.label(format!("1/Δt {:.4} Hz", 1.0 / dt));
            }
        });
    }
    fn plot_wave(&mut self, ui: &mut egui::Ui, id: &str, w: &WaveView) {
        if w.samples.is_empty() {
            ui.add_space(40.0);
            ui.label(RichText::new("开始预览或打开一组记录以查看波形").color(Color32::GRAY));
            return;
        }
        for (i, samples) in w.samples.iter().enumerate() {
            let ch = w
                .channels
                .get(i)
                .cloned()
                .unwrap_or(json!({"name":format!("通道 {}",i+1),"unit":"V"}));
            let physical = ch["index"].as_u64().unwrap_or(i as u64) as usize;
            if physical < 4 && !self.settings.channel_visible[physical] {
                continue;
            }
            let unit = if self.raw_view {
                "V"
            } else {
                ch["unit"].as_str().unwrap_or("V")
            };
            ui.horizontal(|ui| {
                ui.label(
                    RichText::new(ch["name"].as_str().unwrap_or("信号"))
                        .strong()
                        .color(COLORS[i % 4]),
                );
                ui.label(unit);
                if let Some(value) = readout(
                    samples,
                    w.sample_rate,
                    &self.settings.readout_modes[physical.min(3)],
                ) {
                    ui.label(
                        RichText::new(format!(
                            "{value:.prec$}",
                            prec = self.settings.readout_decimals[physical.min(3)]
                        ))
                        .size(20.0),
                    );
                }
                if self.cursors {
                    for (label, x) in [("A", self.cursor_a), ("B", self.cursor_b)] {
                        if let Some(k) = nearest(&w.times, x) {
                            if k < samples.len() {
                                ui.label(format!(
                                    "{label}: {:.6}s / {:.6}",
                                    w.times[k], samples[k]
                                ));
                            }
                        }
                    }
                }
            });
            let points: Vec<[f64; 2]> = w
                .times
                .iter()
                .copied()
                .zip(samples.iter().copied())
                .map(|(x, y)| {
                    [
                        x,
                        if self.log_y {
                            if y > 0.0 { y.log10() } else { f64::NAN }
                        } else {
                            y
                        },
                    ]
                })
                .collect();
            let mut bounds = w.gaps.clone();
            bounds.retain(|&n| n > 0 && n < points.len());
            bounds.insert(0, 0);
            bounds.push(points.len());
            let response = Plot::new((id, i))
                .height(self.plot_height)
                .legend(Legend::default())
                .link_axis(egui::Id::new(id), [true, false])
                .x_axis_label("时间 / s")
                .y_axis_label(if self.log_y {
                    format!("log₁₀({unit})")
                } else {
                    unit.into()
                })
                .show(ui, |plot| {
                    for (seg, r) in bounds.windows(2).enumerate() {
                        plot.line(
                            Line::new(
                                if seg == 0 {
                                    ch["name"].as_str().unwrap_or("信号").to_owned()
                                } else {
                                    format!("连续段 {}", seg + 1)
                                },
                                PlotPoints::from(downsample(&points[r[0]..r[1]], 4000)),
                            )
                            .color(COLORS[i % 4])
                            .width(self.line_width),
                        );
                    }
                    if self.cursors {
                        plot.vline(VLine::new("A", self.cursor_a).color(Color32::DARK_GRAY));
                        plot.vline(VLine::new("B", self.cursor_b).color(Color32::GRAY));
                    }
                    plot.pointer_coordinate()
                });
            if self.cursors && response.response.clicked() {
                if let Some(p) = response.inner {
                    if ui.input(|i| i.modifiers.shift) {
                        self.cursor_b = p.x
                    } else {
                        self.cursor_a = p.x
                    }
                }
            }
        }
    }
    fn capture(&mut self, ctx: &egui::Context) {
        if let Some(p) = rfd::FileDialog::new()
            .set_file_name("LabDAQ.png")
            .save_file()
        {
            self.last_capture = Some(p);
            ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
        }
    }
}
impl eframe::App for LabDaqApp {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        self.worker
            .display_seconds
            .store(self.settings.display_seconds.to_bits(), Ordering::Relaxed);
        self.receive();
        if let Some(mut qa) = self.qa_run.take() {
            qa.tick(self, ctx);
            self.qa_run = Some(qa);
        }
        for event in ctx.input(|i| i.events.clone()) {
            if let egui::Event::Screenshot { image, .. } = event {
                if let Some(path) = self.last_capture.take() {
                    let bytes: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
                    match image::save_buffer(
                        &path,
                        &bytes,
                        image.size[0] as u32,
                        image.size[1] as u32,
                        image::ColorType::Rgba8,
                    ) {
                        Ok(_) => self.notice = format!("已保存 {}", path.display()),
                        Err(e) => self.error = e.to_string(),
                    }
                }
            }
        }
        egui::TopBottomPanel::top("header")
            .exact_height(66.0)
            .show(ctx, |ui| {
                ui.horizontal_centered(|ui| {
                    ui.add_space(14.0);
                    ui.label(
                        RichText::new("LabDAQ")
                            .size(27.0)
                            .strong()
                            .color(Color32::from_rgb(30, 69, 135)),
                    );
                    ui.separator();
                    ui.label(RichText::new(self.page.title()).size(20.0));
                    if self.demo {
                        ui.label(RichText::new("模拟输入").color(Color32::from_rgb(180, 110, 0)));
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("保存窗口 PNG").clicked() {
                            self.capture(ctx);
                        }
                        ui.label(if self.state["recording"] == true {
                            "● 正在记录"
                        } else if self.state["running"] == true {
                            "● 预览中"
                        } else {
                            "○ 空闲"
                        });
                    });
                });
            });
        egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(format!(
                    "{:.3} S/s",
                    self.state["profile"]["actual_rate"]
                        .as_f64()
                        .unwrap_or(self.settings.sample_rate)
                ));
                ui.separator();
                ui.label(format!(
                    "接收 {} 帧 · 记录 {} 帧",
                    self.state["total_scans"].as_u64().unwrap_or(0),
                    self.state["recorded_scans"].as_u64().unwrap_or(0)
                ));
                if let Some(n) = self.state["recording_remaining_scans"].as_u64() {
                    ui.label(format!(
                        "剩余 {:.2} s",
                        n as f64 / self.state["profile"]["actual_rate"].as_f64().unwrap_or(1.0)
                    ));
                }
                if let Some(job) = &self.job {
                    let (done, total) = *job.progress.lock().unwrap();
                    ui.spinner();
                    ui.label(&job.name);
                    if total > 0 {
                        ui.add(
                            egui::ProgressBar::new(done as f32 / total as f32).desired_width(120.0),
                        );
                    }
                    if ui.button("取消").clicked() {
                        job.cancel.store(true, Ordering::Release);
                    }
                } else {
                    ui.label(&self.notice);
                }
            });
        });
        egui::SidePanel::left("navigation").exact_width(150.0).resizable(false).show(ctx,|ui|{ui.add_space(16.0);for page in[Page::Live,Page::Library,Page::Compare,Page::Analysis,Page::Output,Page::Settings]{if ui.add_sized([128.0,42.0],egui::Button::new(page.title()).selected(self.page==page)).clicked(){self.page=page;if page==Page::Library{self.start_job("扫描记录库",json!({"op":"list","root":self.settings.recording_root,"query":self.query}));}}}ui.add_space(20.0);ui.small("原始记录保持不变");ui.small("显示与分析独立处理");});
        if self.page == Page::Live {
            egui::SidePanel::right("parameters")
                .default_width(300.0)
                .width_range(240.0..=440.0)
                .show(ctx, |ui| {
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        ui.heading("输入参数");
                        self.channel_settings(ui);
                    });
                });
        }
        egui::CentralPanel::default().show(ctx, |ui| {
            if !self.error.is_empty() {
                egui::Frame::new()
                    .fill(Color32::from_rgb(255, 239, 234))
                    .inner_margin(10.0)
                    .show(ui, |ui| {
                        ui.horizontal_wrapped(|ui| {
                            ui.colored_label(Color32::from_rgb(170, 51, 35), &self.error);
                            if ui.small_button("关闭").clicked() {
                                self.error.clear();
                            }
                        });
                    });
            }
            egui::ScrollArea::vertical().show(ui, |ui| match self.page {
                Page::Live => self.live_page(ui),
                Page::Library => self.library_page(ui),
                Page::Compare => self.comparison_page(ui),
                Page::Analysis => self.analysis_page(ui),
                Page::Output => self.output_page(ui),
                Page::Settings => self.settings_page(ui),
            });
        });
        ctx.request_repaint_after(Duration::from_secs_f32(
            1.0 / self.settings.refresh_hz.max(1.0),
        ));
    }
    fn on_exit(&mut self, _: Option<&eframe::glow::Context>) {
        let _ = self.settings.save(&self.config_file);
        if let Ok(c) = CoreClient::from_endpoint(&user_directory().join("runtime/core.json")) {
            let _ = c.request(json!({"op":"shutdown_if_idle"}));
        }
    }
}
fn matrix(v: &Value) -> Vec<Vec<f64>> {
    v.as_array().map_or(vec![], |a| {
        a.iter()
            .map(|s| {
                s.as_array().map_or(vec![], |b| {
                    b.iter().map(|x| x.as_f64().unwrap_or(f64::NAN)).collect()
                })
            })
            .collect()
    })
}
fn readout(samples: &[f64], rate: f64, mode: &str) -> Option<f64> {
    if samples.is_empty() {
        return None;
    }
    let count = (rate.round() as usize).max(1).min(samples.len());
    let s = &samples[samples.len() - count..];
    let mean = s.iter().sum::<f64>() / count as f64;
    let min = s.iter().copied().fold(f64::INFINITY, f64::min);
    let max = s.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    Some(match mode {
        "first" => s[0],
        "mean" => mean,
        "min" => min,
        "max" => max,
        "rms" => (s.iter().map(|v| v * v).sum::<f64>() / count as f64).sqrt(),
        "std" => (s.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / count as f64).sqrt(),
        "pkpk" => max - min,
        _ => *s.last().unwrap(),
    })
}
fn wave_from_data(v: &Value) -> WaveView {
    WaveView {
        times: v["times"].as_array().map_or(vec![], |a| {
            a.iter().map(|x| x.as_f64().unwrap_or(f64::NAN)).collect()
        }),
        samples: matrix(&v["samples"]),
        channels: v["channels"].as_array().cloned().unwrap_or_default(),
        sample_rate: v["sample_rate"].as_f64().unwrap_or(1.0),
        gaps: v["gaps"].as_array().map_or(vec![], |a| {
            a.iter()
                .filter_map(|x| x.as_u64().map(|n| n as usize))
                .collect()
        }),
    }
}
fn nearest(times: &[f64], x: f64) -> Option<usize> {
    if times.is_empty() {
        return None;
    }
    let n = times.partition_point(|&t| t < x);
    Some(if n == 0 {
        0
    } else if n == times.len() || (times[n - 1] - x).abs() <= (times[n] - x).abs() {
        n - 1
    } else {
        n
    })
}
fn downsample(points: &[[f64; 2]], limit: usize) -> Vec<[f64; 2]> {
    if points.len() <= limit {
        return points.to_vec();
    }
    let chunk = (points.len() * 2).div_ceil(limit);
    let mut out = Vec::new();
    for part in points.chunks(chunk) {
        let min = part
            .iter()
            .enumerate()
            .filter(|(_, p)| p[1].is_finite())
            .min_by(|(_, a), (_, b)| a[1].total_cmp(&b[1]));
        let max = part
            .iter()
            .enumerate()
            .filter(|(_, p)| p[1].is_finite())
            .max_by(|(_, a), (_, b)| a[1].total_cmp(&b[1]));
        if let (Some((mi, mn)), Some((ma, mx))) = (min, max) {
            if mi <= ma {
                out.push(*mn);
                out.push(*mx);
            } else {
                out.push(*mx);
                out.push(*mn);
            }
        } else {
            out.push(part[0]);
        }
    }
    out
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn peak_preserving_display() {
        let p: Vec<_> = (0..20000)
            .map(|i| [i as f64, if i == 12345 { 100.0 } else { 0.0 }])
            .collect();
        let shown = downsample(&p, 1000);
        assert!(shown.contains(&[12345.0, 100.0]));
        assert!(shown.len() <= 1000);
        assert!(shown.windows(2).all(|w| w[0][0] <= w[1][0]));
    }
    #[test]
    fn nearest_real_samples() {
        assert_eq!(nearest(&[], 0.0), None);
        assert_eq!(nearest(&[1., 2., 10.], 6.0), Some(1));
        assert_eq!(nearest(&[1., 2., 10.], -1.0), Some(0));
        assert_eq!(nearest(&[1., 2., 10.], 99.0), Some(2));
    }
}
