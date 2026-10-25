use super::*;

const MODES: &[(&str, &str)] = &[
    ("amplitude", "幅度谱"),
    ("autospectrum", "自功率谱"),
    ("psd", "功率谱密度 PSD"),
    ("h1", "频响 H1"),
    ("h2", "频响 H2"),
    ("h3", "频响 H3"),
    ("cross_spectrum", "互功率谱"),
    ("cross_psd", "互功率谱密度"),
    ("coherence", "相干函数"),
    ("cop", "相干输出功率密度"),
];
const WINDOWS: &[(&str, &str)] = &[
    ("rectangular", "矩形"),
    ("hann", "Hann"),
    ("hamming", "Hamming"),
    ("bartlett", "Bartlett"),
    ("blackman", "Blackman"),
    ("blackman_harris", "Blackman–Harris"),
    ("flattop", "FlatTop"),
    ("force", "Force 力窗"),
    ("exponential", "指数响应窗"),
    ("cosine_taper", "Cosine Taper"),
];

fn defaults(value: &mut Value, template: Value) {
    if !value.is_object() {
        *value = json!({});
    }
    for (k, v) in template.as_object().unwrap() {
        if value.get(k).is_none() {
            value[k] = v.clone();
        }
    }
}
fn optional_number(
    ui: &mut egui::Ui,
    v: &mut Value,
    key: &str,
    label: &str,
    default: f64,
    range: std::ops::RangeInclusive<f64>,
) {
    let mut enabled = v[key].as_f64().is_some();
    if ui.checkbox(&mut enabled, label).changed() {
        v[key] = if enabled { json!(default) } else { Value::Null };
    }
    if enabled {
        number(ui, v, key, "", range, 0.1);
    }
}
fn window_control(ui: &mut egui::Ui, v: &mut Value, key: &str, label: &str) {
    let current = v[key].clone();
    let mut item = if current.is_object() {
        current
    } else {
        json!({"name":current.as_str().unwrap_or("hann"),"fraction":0.1,"end_amplitude":0.01})
    };
    defaults(
        &mut item,
        json!({"name":"hann","fraction":0.1,"end_amplitude":0.01}),
    );
    ui.push_id(key, |ui| {
        choice(ui, &mut item, "name", label, WINDOWS);
        match item["name"].as_str().unwrap_or("") {
            "force" => number(
                ui,
                &mut item,
                "fraction",
                "保留记录比例",
                0.00001..=1.0,
                0.01,
            ),
            "cosine_taper" => number(
                ui,
                &mut item,
                "fraction",
                "两端渐变比例",
                0.00001..=1.0,
                0.01,
            ),
            "exponential" => number(
                ui,
                &mut item,
                "end_amplitude",
                "末端权重",
                0.000001..=1.0,
                0.001,
            ),
            _ => {}
        }
    });
    v[key] = item;
}
fn continuous_bounds(w: &WaveView) -> Vec<(usize, usize)> {
    let n = w.samples.first().map_or(0, Vec::len).min(w.times.len());
    let mut cuts: Vec<_> = w.gaps.iter().copied().filter(|&g| g > 0 && g < n).collect();
    cuts.sort_unstable();
    cuts.dedup();
    cuts.insert(0, 0);
    cuts.push(n);
    cuts.windows(2)
        .filter(|x| x[1] > x[0])
        .map(|x| (x[0], x[1]))
        .collect()
}
fn select_wave(
    w: &WaveView,
    segment: usize,
    cursors: Option<(f64, f64)>,
) -> Result<WaveView, String> {
    let bounds = continuous_bounds(w);
    let &(mut a, mut b) = bounds.get(segment).ok_or("没有可分析的连续数据段")?;
    if let Some((left, right)) = cursors {
        let lo = left.min(right);
        let hi = left.max(right);
        a = a.max(w.times.partition_point(|t| *t < lo));
        b = b.min(w.times.partition_point(|t| *t < hi));
    }
    if b <= a + 1 {
        return Err("所选区间至少需要两个实际采样点".into());
    }
    if w.samples.iter().any(|c| c.len() < b) {
        return Err("数据通道长度不一致".into());
    }
    Ok(WaveView {
        times: w.times[a..b].to_vec(),
        samples: w.samples.iter().map(|c| c[a..b].to_vec()).collect(),
        channels: w.channels.clone(),
        sample_rate: w.sample_rate,
        gaps: vec![],
    })
}
fn wave_value(w: &WaveView) -> Value {
    json!({"times":w.times,"samples":w.samples,"channels":w.channels,"sample_rate":w.sample_rate,"gaps":w.gaps})
}
fn filter_channel_input(w: &WaveView, channel: i64) -> Result<WaveView, String> {
    if channel < 0 {
        return Ok(w.clone());
    }
    let index = channel as usize;
    let samples = w
        .samples
        .get(index)
        .ok_or("所选滤波通道已不在当前输入中")?
        .clone();
    let meta = w.channels.get(index).ok_or("滤波通道缺少元数据")?.clone();
    Ok(WaveView {
        times: w.times.clone(),
        samples: vec![samples],
        channels: vec![meta],
        sample_rate: w.sample_rate,
        gaps: w.gaps.clone(),
    })
}
fn filter_parameters(v: &Value) -> Value {
    let mut p = json!({});
    for key in [
        "family",
        "kind",
        "order",
        "cutoff_hz",
        "cutoff_high_hz",
        "ripple_db",
        "zero_phase",
    ] {
        p[key] = v[key].clone();
    }
    p
}
fn filter_profile_key(input: &WaveView, channel: i64) -> String {
    if channel < 0 {
        "all".into()
    } else {
        format!(
            "ai{}",
            input
                .channels
                .get(channel as usize)
                .and_then(|v| v["index"].as_u64())
                .unwrap_or(channel as u64)
        )
    }
}
fn record_selection(w: &WaveView, config: &Value) -> Result<WaveView, String> {
    if config["_ui_blocks"] != true {
        return Ok(w.clone());
    }
    let count = w.times.len();
    let requested = config["nfft"].as_u64().unwrap_or(0) as usize;
    let n = if requested == 0 && count > 0 {
        1usize << (usize::BITS - 1 - count.min(262144).leading_zeros())
    } else {
        requested
    };
    if n < 2 || n > count {
        return Err("当前连续段不足一个 FFT 记录块".into());
    }
    let step =
        ((n as f64 * (1. - config["overlap"].as_f64().unwrap_or(0.5))).round() as usize).max(1);
    let first = config["_ui_first_record"].as_u64().unwrap_or(0) as usize;
    let last = config["_ui_last_record"].as_u64().unwrap_or(first as u64) as usize;
    let a = first.checked_mul(step).ok_or("记录块编号过大")?;
    let b = last
        .checked_mul(step)
        .and_then(|x| x.checked_add(n))
        .ok_or("记录块编号过大")?;
    if first > last || b > count {
        return Err("记录块范围超出当前连续段".into());
    }
    Ok(WaveView {
        times: w.times[a..b].to_vec(),
        samples: w.samples.iter().map(|c| c[a..b].to_vec()).collect(),
        channels: w.channels.clone(),
        sample_rate: w.sample_rate,
        gaps: vec![],
    })
}
fn value_text(value: &Value, precision: usize) -> String {
    value
        .as_f64()
        .filter(|v| v.is_finite())
        .map_or_else(|| "—".into(), |v| format!("{v:.precision$}"))
}
fn finite_segments(x: &[Value], y: &[Value]) -> Vec<Vec<[f64; 2]>> {
    let mut parts = vec![];
    let mut current = vec![];
    for (a, b) in x.iter().zip(y) {
        match (a.as_f64(), b.as_f64()) {
            (Some(a), Some(b)) if a.is_finite() && b.is_finite() => current.push([a, b]),
            _ => {
                if !current.is_empty() {
                    parts.push(std::mem::take(&mut current));
                }
            }
        }
    }
    if !current.is_empty() {
        parts.push(current);
    }
    parts
}

impl LabDaqApp {
    pub(super) fn analysis_page(&mut self, ui: &mut egui::Ui) {
        defaults(
            &mut self.settings.analysis,
            serde_json::to_value(labdaq_analysis::AnalysisRequest::default()).unwrap(),
        );
        // Samples belong to the job snapshot, not persistent settings.
        self.settings
            .analysis
            .as_object_mut()
            .unwrap()
            .remove("samples");
        defaults(
            &mut self.settings.analysis,
            json!({"_ui_source":"current","_ui_segment":0,"_ui_cursors":false,"_ui_panel":"spectrum","_ui_separate_windows":false,"_ui_band":false}),
        );
        let mut config = self.settings.analysis.clone();
        let mut filter = self.settings.filter.clone();
        defaults(
            &mut filter,
            serde_json::to_value(labdaq_analysis::FilterSpec::default()).unwrap(),
        );
        defaults(&mut filter, json!({"zero_phase":true}));
        ui.heading("信号分析");
        ui.horizontal_wrapped(|ui| {
            choice(
                ui,
                &mut config,
                "_ui_source",
                "输入",
                &[
                    ("current", "当前回看 / 实时窗口"),
                    ("live", "实时窗口"),
                    ("replay", "回看窗口"),
                    ("filtered", "最近滤波结果"),
                ],
            );
            choice(
                ui,
                &mut config,
                "_ui_panel",
                "工作区",
                &[("spectrum", "频域分析"), ("filter", "数字滤波")],
            );
        });
        let source = config["_ui_source"].as_str().unwrap_or("current");
        let mut input = match source {
            "live" => self.live.clone(),
            "replay" => self.replay.clone(),
            "filtered" => self.filtered.clone(),
            _ => {
                if self.open_path.is_empty() {
                    self.live.clone()
                } else {
                    self.replay.clone()
                }
            }
        };
        if self.raw_view && source != "filtered" {
            for ch in &mut input.channels {
                ch["unit"] = json!("V");
            }
        }
        let bounds = continuous_bounds(&input);
        let mut segment = config["_ui_segment"].as_u64().unwrap_or(0) as usize;
        if segment >= bounds.len() {
            segment = 0;
        }
        ui.horizontal_wrapped(|ui| {
            egui::ComboBox::from_id_salt("analysis-segment")
                .selected_text(format!("连续段 {} / {}", segment + 1, bounds.len()))
                .show_ui(ui, |ui| {
                    for (i, &(a, b)) in bounds.iter().enumerate() {
                        ui.selectable_value(
                            &mut segment,
                            i,
                            format!(
                                "{}：{:.4}–{:.4} s · {} 点",
                                i + 1,
                                input.times[a],
                                input.times[b - 1],
                                b - a
                            ),
                        );
                    }
                });
            flag(ui, &mut config, "_ui_cursors", "仅分析双游标区间");
            if config["_ui_cursors"] == true {
                ui.label("A / s");
                ui.add(egui::DragValue::new(&mut self.cursor_a).speed(0.001));
                ui.label("B / s");
                ui.add(egui::DragValue::new(&mut self.cursor_b).speed(0.001));
            }
        });
        config["_ui_segment"] = json!(segment);
        let selected = select_wave(
            &input,
            segment,
            if config["_ui_cursors"] == true {
                Some((self.cursor_a, self.cursor_b))
            } else {
                None
            },
        );
        match &selected {
            Ok(w) => {
                ui.label(format!(
                    "{} 通道 · {} 点 / 通道 · {:.6} S/s · {:.4} 秒",
                    w.samples.len(),
                    w.times.len(),
                    w.sample_rate,
                    w.times.len() as f64 / w.sample_rate
                ));
            }
            Err(e) => {
                ui.colored_label(Color32::GRAY, e);
            }
        }
        ui.separator();
        if config["_ui_panel"] == "filter" {
            let previous_channel = filter["_ui_channel"].as_i64().unwrap_or(-1);
            let mut channel = previous_channel;
            egui::ComboBox::from_id_salt("filter-channel")
                .selected_text(if channel < 0 {
                    "通道：全部（当前设置）".into()
                } else {
                    format!(
                        "通道：{}",
                        input
                            .channels
                            .get(channel as usize)
                            .and_then(|c| c["name"].as_str())
                            .unwrap_or("未选择")
                    )
                })
                .show_ui(ui, |ui| {
                    ui.selectable_value(&mut channel, -1, "全部通道（当前设置）");
                    for (i, ch) in input.channels.iter().enumerate() {
                        ui.selectable_value(
                            &mut channel,
                            i as i64,
                            ch["name"].as_str().unwrap_or("通道"),
                        );
                    }
                });
            if filter["_ui_profiles"].is_null() {
                filter["_ui_profiles"] = json!({});
            }
            let profile_key = filter_profile_key(&input, channel);
            let previous_key = filter["_ui_profile_key"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| filter_profile_key(&input, previous_channel));
            if profile_key != previous_key {
                let previous = filter_parameters(&filter);
                filter["_ui_profiles"][previous_key] = previous;
                if let Some(saved) = filter["_ui_profiles"].get(&profile_key).cloned() {
                    for (key, value) in saved.as_object().unwrap() {
                        filter[key] = value.clone();
                    }
                }
            }
            filter["_ui_channel"] = json!(channel);
            filter["_ui_profile_key"] = json!(profile_key);
            ui.columns(3, |columns| {
                choice(
                    &mut columns[0],
                    &mut filter,
                    "family",
                    "滤波器",
                    &[
                        ("butterworth", "Butterworth"),
                        ("bessel", "Bessel"),
                        ("chebyshev", "Chebyshev I"),
                    ],
                );
                choice(
                    &mut columns[0],
                    &mut filter,
                    "kind",
                    "频带",
                    &[
                        ("lowpass", "低通"),
                        ("highpass", "高通"),
                        ("bandpass", "带通"),
                        ("bandstop", "带阻"),
                    ],
                );
                integer(&mut columns[1], &mut filter, "order", "原型阶数", 1..=10);
                number(
                    &mut columns[1],
                    &mut filter,
                    "cutoff_hz",
                    "截止 / 下截止 Hz",
                    0.000001..=1e9,
                    1.0,
                );
                if filter["kind"]
                    .as_str()
                    .is_some_and(|s| s.starts_with("band"))
                {
                    number(
                        &mut columns[1],
                        &mut filter,
                        "cutoff_high_hz",
                        "上截止 Hz",
                        0.000001..=1e9,
                        1.0,
                    );
                }
                if filter["family"] == "chebyshev" {
                    number(
                        &mut columns[2],
                        &mut filter,
                        "ripple_db",
                        "通带纹波 dB",
                        0.0001..=60.0,
                        0.1,
                    );
                }
                flag(&mut columns[2], &mut filter, "zero_phase", "离线双向零相位");
                columns[2].small(if filter["zero_phase"] == true {
                    "前后向滤波；总阶数加倍，幅频响应为单次响应的平方。"
                } else {
                    "单次因果滤波；当前连续段从零状态开始。"
                });
                columns[2].small("Bessel 截止按 −3 dB 定义；Chebyshev 截止为通带边缘。");
            });
            ui.horizontal_wrapped(|ui| {
                if ui
                    .add_enabled(
                        self.job.is_none() && selected.is_ok(),
                        egui::Button::new("应用滤波"),
                    )
                    .clicked()
                {
                    if let Ok(w) = &selected {
                        match filter_channel_input(w, channel) {
                            Err(e) => self.error = e,
                            Ok(w) => {
                                let mut request = filter.clone();
                                request["task"] = json!("filter");
                                request["samples"] = json!(w.samples);
                                request["sample_rate"] = json!(w.sample_rate);
                                request["input"] = wave_value(&w);
                                self.start_job("数字滤波", request);
                            }
                        }
                    }
                }
                if ui
                    .add_enabled(self.job.is_none(), egui::Button::new("仅查看滤波器响应"))
                    .clicked()
                {
                    let mut request = filter.clone();
                    request["task"] = json!("filter");
                    request["samples"] = json!([]);
                    request["sample_rate"] = json!(if input.sample_rate > 0.0 {
                        input.sample_rate
                    } else {
                        self.settings.sample_rate
                    });
                    request["input"] = wave_value(&WaveView::default());
                    self.start_job("数字滤波", request);
                }
            });
            let saved = filter_parameters(&filter);
            filter["_ui_profiles"][profile_key] = saved;
            self.filter_charts(ui);
        } else {
            ui.columns(3, |columns| {
                choice(&mut columns[0], &mut config, "mode", "分析函数", MODES);
                let mut n = config["nfft"].as_u64().unwrap_or(1024);
                egui::ComboBox::from_id_salt("fft-size")
                    .selected_text(if n == 0 {
                        "FFT：自动".into()
                    } else {
                        format!("FFT：{n} 点")
                    })
                    .show_ui(&mut columns[0], |ui| {
                        ui.selectable_value(&mut n, 0, "自动");
                        for size in [
                            128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072,
                            262144,
                        ] {
                            ui.selectable_value(&mut n, size, size.to_string());
                        }
                    });
                config["nfft"] = json!(n);
                if n > 0 {
                    columns[0].label(format!("频率分辨率 {:.6} Hz", input.sample_rate / n as f64));
                }
                window_control(&mut columns[0], &mut config, "window", "分析窗");
                flag(&mut columns[0], &mut config, "detrend", "每块减去均值");
                let dual = [
                    "h1",
                    "h2",
                    "h3",
                    "cross_spectrum",
                    "cross_psd",
                    "coherence",
                    "cop",
                ]
                .contains(&config["mode"].as_str().unwrap_or(""));
                if dual {
                    config["average"] = json!("rms");
                    columns[1].label("自谱 / 复互谱成对平均");
                    let mut reference = config["reference"].as_u64().unwrap_or(0) as usize;
                    egui::ComboBox::from_id_salt("reference-channel")
                        .selected_text(format!(
                            "参考：{}",
                            input
                                .channels
                                .get(reference)
                                .and_then(|c| c["name"].as_str())
                                .unwrap_or("未选择")
                        ))
                        .show_ui(&mut columns[1], |ui| {
                            for (i, ch) in input.channels.iter().enumerate() {
                                ui.selectable_value(
                                    &mut reference,
                                    i,
                                    ch["name"].as_str().unwrap_or("通道"),
                                );
                            }
                        });
                    config["reference"] = json!(reference);
                } else {
                    choice(
                        &mut columns[1],
                        &mut config,
                        "average",
                        "平均方式",
                        &[
                            ("rms", "RMS 功率平均"),
                            ("vector", "Vector 复数平均"),
                            ("peak_hold", "PeakHold 峰值保持"),
                        ],
                    );
                }
                if config["average"] == "peak_hold" {
                    config["weighting"] = json!("linear");
                } else {
                    choice(
                        &mut columns[1],
                        &mut config,
                        "weighting",
                        "记录权重",
                        &[("linear", "等权线性"), ("exponential", "指数")],
                    );
                }
                if config["weighting"] == "exponential" {
                    number(
                        &mut columns[1],
                        &mut config,
                        "alpha",
                        "新记录权重",
                        0.0001..=1.0,
                        0.01,
                    );
                }
                number(
                    &mut columns[1],
                    &mut config,
                    "overlap",
                    "记录重叠比例",
                    0.0..=0.99,
                    0.05,
                );
                if config["average"] == "vector" {
                    columns[1].small("Vector 平均要求记录间保持相位对齐。");
                }
                choice(
                    &mut columns[2],
                    &mut config,
                    "display",
                    "显示",
                    &[
                        ("magnitude", "幅值"),
                        ("phase", "相位"),
                        ("real", "实部"),
                        ("imaginary", "虚部"),
                        ("db", "dB"),
                        ("nyquist", "Nyquist"),
                    ],
                );
                if !dual
                    && ["amplitude", "spectrum"].contains(&config["mode"].as_str().unwrap_or(""))
                {
                    choice(
                        &mut columns[2],
                        &mut config,
                        "scale",
                        "幅值标度",
                        &[("peak", "Peak"), ("rms", "RMS"), ("pkpk", "Pk-Pk")],
                    );
                }
                if config["display"] == "db" {
                    number(
                        &mut columns[2],
                        &mut config,
                        "db_reference",
                        "dB 参考值",
                        1e-15..=1e15,
                        0.01,
                    );
                }
                if !dual
                    && config["average"] == "rms"
                    && ["phase", "real", "imaginary", "nyquist"]
                        .contains(&config["display"].as_str().unwrap_or(""))
                {
                    columns[2].small(
                        "RMS 功率平均不保留相位。需要复数相位时选择 Vector，并保证记录相位对齐。",
                    );
                }
                optional_number(
                    &mut columns[2],
                    &mut config,
                    "max_frequency",
                    "限制最大频率 / Hz",
                    (input.sample_rate / 2.0).max(1.0),
                    0.000001..=1e9,
                );
                integer(
                    &mut columns[2],
                    &mut config,
                    "integration",
                    if dual {
                        "响应积分次数"
                    } else {
                        "积分次数"
                    },
                    0..=2,
                );
                if dual {
                    integer(
                        &mut columns[2],
                        &mut config,
                        "reference_integration",
                        "参考积分次数",
                        0..=2,
                    );
                }
            });
            let dual = [
                "h1",
                "h2",
                "h3",
                "cross_spectrum",
                "cross_psd",
                "coherence",
                "cop",
            ]
            .contains(&config["mode"].as_str().unwrap_or(""));
            if dual {
                ui.collapsing("双通道与冲击记录设置", |ui| {
                    if ["h1", "h2", "h3"].contains(&config["mode"].as_str().unwrap_or("")) {
                        choice(
                            ui,
                            &mut config,
                            "frf_form",
                            "物理形式",
                            &[
                                ("custom", "自定义单位比"),
                                ("inertance", "惯性 / Inertance"),
                                ("mobility", "导纳 / Mobility"),
                                ("compliance", "柔度 / Compliance"),
                                ("apparent_mass", "表观质量"),
                                ("impedance", "阻抗"),
                                ("dynamic_stiffness", "动态刚度"),
                            ],
                        );
                        ui.small("物理形式需要 N 参考与已标定运动量响应；按单位自动确定积分次数。");
                    }
                    flag(ui, &mut config, "_ui_separate_windows", "独立参考 / 响应窗");
                    if config["_ui_separate_windows"] == true {
                        ui.columns(2, |c| {
                            window_control(&mut c[0], &mut config, "reference_window", "参考窗");
                            window_control(&mut c[1], &mut config, "response_window", "响应窗");
                        });
                    } else {
                        config["reference_window"] = Value::Null;
                        config["response_window"] = Value::Null;
                    }
                });
            } else {
                config["frf_form"] = json!("custom");
                config["reference_window"] = Value::Null;
                config["response_window"] = Value::Null;
            }
            ui.collapsing("记录块与重处理", |ui| {
                flag(ui, &mut config, "_ui_blocks", "指定记录块范围");
                if config["_ui_blocks"] == true {
                    integer(
                        ui,
                        &mut config,
                        "_ui_first_record",
                        "首块（从 0 开始）",
                        0..=1000000,
                    );
                    integer(
                        ui,
                        &mut config,
                        "_ui_last_record",
                        "末块（包含）",
                        0..=1000000,
                    );
                    ui.small("首末相同时查看单块；双通道相干和 FRF 至少需要两个记录块。");
                }
                flag(
                    ui,
                    &mut config,
                    "_ui_auto_reprocess",
                    "参数变化后自动重新处理",
                );
            });
            ui.collapsing("频带积分、谱峰与动态性能",|ui|{ui.columns(3,|c|{
                flag(&mut c[0],&mut config,"_ui_band","计算频带功率");if config["_ui_band"]==true{let mut band=json!({"low":config["band"][0].as_f64().unwrap_or(0.0),"high":config["band"][1].as_f64().unwrap_or((input.sample_rate/2.).max(1.))});number(&mut c[0],&mut band,"low","下限 Hz",0.0..=1e9,1.0);number(&mut c[0],&mut band,"high","上限 Hz",0.0..=1e9,1.0);config["band"]=json!([band["low"],band["high"]]);}else{config["band"]=Value::Null;}
                integer(&mut c[1],&mut config,"peak_count","谱峰数量",0..=10);choice(&mut c[1],&mut config,"peak_priority","排序",&[("amplitude","幅值优先"),("prominence","峰高优先")]);number(&mut c[1],&mut config,"peak_min_prominence","最小峰高",0.0..=1e15,0.01);
                optional_number(&mut c[2],&mut config,"fundamental_hz","指定基频 / Hz",10.0,0.000001..=1e9);optional_number(&mut c[2],&mut config,"full_scale","指定满量程正峰值",10.0,0.000001..=1e15);integer(&mut c[2],&mut config,"harmonic_count","最高谐波次数",2..=100);integer(&mut c[2],&mut config,"tone_half_width_bins","谱线积分半宽",0..=100);
            });
                if config.get("_ui_full_scales").is_none(){config["_ui_full_scales"]=json!(config["full_scales"].as_array().is_some_and(|v|!v.is_empty()));}
                flag(ui,&mut config,"_ui_full_scales","按通道指定满量程（覆盖统一值）");
                if config["_ui_full_scales"]==true{let mut ranges=config["full_scales"].as_array().cloned().unwrap_or_default();ranges.resize(input.channels.len(),json!(10.));for(i,ch)in input.channels.iter().enumerate(){let mut row=json!({"full_scale":ranges[i]});number(ui,&mut row,"full_scale",&format!("{} / {}",ch["name"].as_str().unwrap_or("通道"),ch["unit"].as_str().unwrap_or("")),0.000001..=1e15,0.1);ranges[i]=row["full_scale"].clone();}config["full_scales"]=json!(ranges);}else{config["full_scales"]=json!([]);}
                ui.small("动态性能使用 Blackman–Harris 窗。满量程采用输入当前单位；未指定时不计算 IBF、满量程 SFDR 和 ENOB。");
            });
            ui.horizontal_wrapped(|ui| {
                if ui
                    .add_enabled(
                        self.job.is_none() && selected.is_ok(),
                        egui::Button::new("计算 / 重新处理"),
                    )
                    .clicked()
                {
                    if let Ok(w) = &selected {
                        self.queue_analysis(w, &config);
                    }
                }
                let exists = self.analysis_result["traces"]
                    .as_array()
                    .is_some_and(|v| !v.is_empty());
                if ui
                    .add_enabled(
                        exists && self.job.is_none(),
                        egui::Button::new("导出频域 CSV"),
                    )
                    .clicked()
                {
                    self.export_analysis(false, false);
                }
                if ui
                    .add_enabled(
                        exists && self.job.is_none(),
                        egui::Button::new("导出频域 TXT"),
                    )
                    .clicked()
                {
                    self.export_analysis(false, true);
                }
                if config.get("_ui_uff_complex").is_none() {
                    config["_ui_uff_complex"] = json!(true);
                }
                flag(ui, &mut config, "_ui_uff_complex", "UFF 保存复数");
                if ui
                    .add_enabled(exists && self.job.is_none(), egui::Button::new("导出 UFF"))
                    .clicked()
                {
                    self.export_analysis(true, false);
                }
            });
            self.spectrum_charts(ui, config["display"].as_str().unwrap_or("magnitude"));
            if config["_ui_auto_reprocess"] == true
                && self.job.is_none()
                && config != self.settings.analysis
            {
                if let Ok(w) = &selected {
                    self.queue_analysis(w, &config);
                }
            }
        }
        self.settings.analysis = config;
        self.settings.filter = filter;
    }

    fn queue_analysis(&mut self, w: &WaveView, config: &Value) {
        match record_selection(w, config) {
            Err(e) => self.error = e,
            Ok(w) => {
                let mut request = config.clone();
                request["task"] = json!("analysis");
                request["samples"] = json!(w.samples);
                request["sample_rate"] = json!(w.sample_rate);
                request["channels"] = json!(w.channels);
                request["raw_volts"] = json!(false);
                request["input"] = wave_value(&w);
                request["source"] = json!({"kind":config["_ui_source"],"recording":if config["_ui_source"]=="live"{""}else{self.open_path.as_str()},"first_time_s":w.times.first(),"last_time_s":w.times.last(),"frames":w.times.len(),"first_record":config["_ui_first_record"],"last_record":config["_ui_last_record"]});
                self.start_job("频域分析", request);
            }
        }
    }

    fn spectrum_charts(&mut self, ui: &mut egui::Ui, display: &str) {
        let result = &self.analysis_result;
        let display = result["display"].as_str().unwrap_or(display);
        let Some(traces) = result["traces"].as_array() else {
            return;
        };
        let frequencies = result["frequencies"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        ui.separator();
        ui.label(format!(
            "{} 个记录块 · FFT {} 点 · Δf {} Hz",
            result["records"],
            result["nfft"],
            value_text(&result["resolution_hz"], 6)
        ));
        for (i, trace) in traces.iter().enumerate() {
            let name = trace["name"].as_str().unwrap_or("频谱");
            let unit = trace["unit"].as_str().unwrap_or("");
            let nyquist = display == "nyquist";
            ui.label(RichText::new(format!("{name} / {unit}")).strong());
            let x = if nyquist {
                trace["real"].as_array().cloned().unwrap_or_default()
            } else {
                frequencies.clone()
            };
            let y = trace[if nyquist { "imaginary" } else { "values" }]
                .as_array()
                .cloned()
                .unwrap_or_default();
            let segments = finite_segments(&x, &y);
            Plot::new(("analysis-spectrum", i))
                .height(280.)
                .legend(Legend::default())
                .x_axis_label(if nyquist { "实部" } else { "频率 / Hz" })
                .y_axis_label(if nyquist { "虚部" } else { unit })
                .show(ui, |p| {
                    for (segment, points) in segments.into_iter().enumerate() {
                        p.line(
                            Line::new(
                                if segment == 0 { name } else { "" },
                                PlotPoints::from(if nyquist {
                                    points
                                } else {
                                    downsample(&points, 4000)
                                }),
                            )
                            .color(COLORS[i % 4]),
                        );
                    }
                });
            if let Some(power) = trace["band_power"].as_f64() {
                ui.label(format!(
                    "所选频带功率：{power:.8} {}",
                    trace["band_power_unit"].as_str().unwrap_or("")
                ));
            }
            if let Some(coherence) = trace["coherence"].as_array() {
                let segments = finite_segments(&frequencies, coherence);
                Plot::new(("analysis-coherence", i))
                    .height(150.)
                    .include_y(0.)
                    .include_y(1.)
                    .x_axis_label("频率 / Hz")
                    .y_axis_label("相干")
                    .show(ui, |p| {
                        for (segment, points) in segments.into_iter().enumerate() {
                            p.line(
                                Line::new(
                                    if segment == 0 { "相干" } else { "" },
                                    PlotPoints::from(downsample(&points, 2000)),
                                )
                                .color(COLORS[i % 4]),
                            );
                        }
                    });
            }
            ui.collapsing(format!("{name}：谱峰和动态性能"), |ui| {
                ui.label(format!(
                    "动态性能输入单位：{}",
                    trace["metrics"]["unit"].as_str().unwrap_or("")
                ));
                egui::Grid::new(("metrics", i))
                    .striped(true)
                    .show(ui, |ui| {
                        for (key, label) in [
                            ("fundamental_hz", "基频 Hz"),
                            ("fundamental_rms", "基频 RMS"),
                            ("ibf_db", "IBF dB"),
                            ("thd_percent", "THD %"),
                            ("thd_db", "THD dB"),
                            ("sfdr_db", "SFDR dBc"),
                            ("sfdr_dbfs", "SFDR dBFS"),
                            ("sinad_db", "SINAD dB"),
                            ("snr_db", "SNR dB"),
                            ("enob", "ENOB bits"),
                            ("noise_rms", "噪声 RMS"),
                        ] {
                            ui.label(label);
                            ui.label(value_text(&trace["metrics"][key], 6));
                            ui.end_row();
                        }
                    });
                if let Some(peaks) = trace["peaks"].as_array() {
                    egui::Grid::new(("peaks", i)).striped(true).show(ui, |ui| {
                        ui.strong("频率 / Hz");
                        ui.strong("幅值");
                        ui.strong("峰高");
                        ui.end_row();
                        for peak in peaks {
                            for field in ["frequency_hz", "amplitude", "prominence"] {
                                ui.label(value_text(&peak[field], 6));
                            }
                            ui.end_row();
                        }
                    });
                }
            });
        }
    }

    fn filter_charts(&mut self, ui: &mut egui::Ui) {
        let r = &self.filter_result;
        if !r.is_object() {
            return;
        }
        ui.separator();
        ui.label(format!(
            "有效阶数：{} · {}",
            r["effective_order"],
            r["cutoff_definition"].as_str().unwrap_or("")
        ));
        let draw =
            |ui: &mut egui::Ui, id: &str, x: &Value, y: &Value, xlabel: &str, ylabel: &str| {
                let pts: Vec<_> = x
                    .as_array()
                    .into_iter()
                    .flatten()
                    .zip(y.as_array().into_iter().flatten())
                    .filter_map(|(a, b)| Some([a.as_f64()?, b.as_f64()?]))
                    .collect();
                Plot::new(id)
                    .height(200.)
                    .x_axis_label(xlabel)
                    .y_axis_label(ylabel)
                    .show(ui, |p| {
                        p.line(Line::new(ylabel, PlotPoints::from(pts)).color(COLORS[0]))
                    });
            };
        ui.columns(2, |c| {
            draw(
                &mut c[0],
                "filter-magnitude",
                &r["frequencies"],
                &r["magnitude"],
                "频率 / Hz",
                "幅频响应",
            );
            draw(
                &mut c[1],
                "filter-phase",
                &r["frequencies"],
                &r["phase"],
                "频率 / Hz",
                "相位 / °",
            );
        });
        ui.collapsing("单次因果冲激 / 阶跃响应", |ui| {
            let fs = r["response_sample_rate"].as_f64().unwrap_or(1.);
            let n = r["impulse"].as_array().map_or(0, Vec::len);
            let t = json!((0..n).map(|i| i as f64 / fs).collect::<Vec<_>>());
            ui.columns(2, |c| {
                draw(
                    &mut c[0],
                    "filter-impulse",
                    &t,
                    &r["impulse"],
                    "时间 / s",
                    "冲激响应",
                );
                draw(
                    &mut c[1],
                    "filter-step",
                    &t,
                    &r["step"],
                    "时间 / s",
                    "阶跃响应",
                );
            });
        });
        if !self.filtered.samples.is_empty() {
            ui.label("滤波后的当前连续段");
            let raw_view = self.raw_view;
            self.raw_view = false;
            self.plot_wave(ui, "filtered-analysis", &self.filtered.clone());
            self.raw_view = raw_view;
        }
    }

    fn export_analysis(&mut self, uff: bool, txt: bool) {
        let extension = if uff {
            "uff"
        } else if txt {
            "txt"
        } else {
            "csv"
        };
        let Some(path) = rfd::FileDialog::new()
            .set_file_name(format!("LabDAQ-spectrum.{extension}"))
            .add_filter("分析结果", &[extension])
            .save_file()
        else {
            return;
        };
        match spectrum_export_request(
            &self.analysis_result,
            &self.settings.analysis,
            &self.open_path,
            &path,
            uff,
            txt,
        ) {
            Ok((request, removed)) => {
                if removed > 0 {
                    self.notice = format!("导出将跳过 {removed} 个未定义频点，保留其余真实频率。");
                }
                self.start_job(if uff { "导出 UFF" } else { "导出频谱" }, request);
            }
            Err(e) => self.error = e,
        }
    }
}

fn spectrum_export_request(
    result: &Value,
    settings: &Value,
    source: &str,
    path: &std::path::Path,
    uff: bool,
    txt: bool,
) -> Result<(Value, usize), String> {
    let complex_uff = settings["_ui_uff_complex"].as_bool().unwrap_or(true);
    let settings = if result["parameters"].is_object() {
        &result["parameters"]
    } else {
        settings
    };
    let frequencies = result["frequencies"].as_array().ok_or("没有频率轴")?;
    let traces = result["traces"].as_array().ok_or("没有频域曲线")?;
    let fields = if uff && complex_uff {
        vec!["real", "imaginary"]
    } else if uff {
        vec!["values"]
    } else {
        vec!["values", "real", "imaginary", "phase", "coherence"]
    };
    let keep: Vec<_> = (0..frequencies.len())
        .filter(|&i| {
            frequencies[i].as_f64().is_some_and(f64::is_finite)
                && (!uff
                    || traces.iter().all(|t| {
                        fields.iter().all(|f| {
                            t[*f].is_null() || t[*f][i].as_f64().is_some_and(f64::is_finite)
                        })
                    }))
        })
        .collect();
    if keep.is_empty() {
        return Err("结果没有可导出的有限频点".into());
    }
    let mut output = vec![];
    for trace in traces {
        let mut t = trace.clone();
        let display_unit = trace["unit"].as_str().unwrap_or("");
        let quantity = trace["quantity_unit"].as_str().unwrap_or(display_unit);
        for f in &fields {
            if let Some(values) = trace[*f].as_array() {
                t[if *f == "imaginary" { "imag" } else { f }] =
                    json!(keep.iter().map(|&i| values[i].clone()).collect::<Vec<_>>());
            }
        }
        t.as_object_mut().unwrap().remove("imaginary");
        for field in ["coherence", "point_number", "reference_point"] {
            if t[field].is_null() {
                t.as_object_mut().unwrap().remove(field);
            }
        }
        t["unit"] = json!(if uff && !complex_uff {
            display_unit
        } else {
            quantity
        });
        if uff && !complex_uff {
            for field in ["real", "imag"] {
                t.as_object_mut().unwrap().remove(field);
            }
        }
        t["field_units"] = json!({"values":display_unit,"real":quantity,"imag":quantity,"phase":"deg","coherence":"1"});
        t["function_type"] = json!(match settings["mode"].as_str().unwrap_or("") {
            "h1" | "h2" | "h3" => 4,
            "psd" | "cop" => 9,
            "autospectrum" => 2,
            "cross_spectrum" | "cross_psd" => 3,
            "coherence" => 6,
            _ => 12,
        });
        output.push(t);
    }
    let removed = frequencies.len() - keep.len();
    Ok((
        json!({"op":if uff{"export_uff"}else{"export_spectrum"},"output":path,"delimiter":if txt{"\t"}else{","},"overwrite":true,"frequencies":keep.iter().map(|&i|frequencies[i].clone()).collect::<Vec<_>>(),"traces":output,"settings":settings,"source":if result["source"].is_null(){json!(source)}else{result["source"].clone()},"description":format!("LabDAQ; omitted {removed} undefined frequency bins")}),
        removed,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn analysis_selection_never_bridges_pause() {
        let w = WaveView {
            times: vec![0., 1., 2., 10., 11., 12.],
            samples: vec![vec![0., 1., 2., 3., 4., 5.]],
            channels: vec![json!({"unit":"V"})],
            sample_rate: 1.,
            gaps: vec![3],
        };
        assert_eq!(continuous_bounds(&w), [(0, 3), (3, 6)]);
        assert_eq!(select_wave(&w, 1, None).unwrap().times, [10., 11., 12.]);
        assert_eq!(select_wave(&w, 0, Some((1., 11.))).unwrap().times, [1., 2.]);
        assert!(select_wave(&w, 0, Some((1., 2.))).is_err());
    }
    #[test]
    fn export_keeps_complex_units_and_filters_only_undefined_bins() {
        let r = json!({"frequencies":[0.,1.,2.],"traces":[{"name":"x","unit":"deg","quantity_unit":"m/N","values":[null,10.,20.],"real":[null,2.,3.],"imaginary":[null,4.,5.],"phase":[null,10.,20.],"coherence":null,"point_number":7,"direction":"+X"}]});
        let (v, n) = spectrum_export_request(
            &r,
            &json!({"mode":"h1"}),
            "recording",
            std::path::Path::new("sample.uff"),
            true,
            false,
        )
        .unwrap();
        assert_eq!(n, 1);
        assert_eq!(v["frequencies"], json!([1., 2.]));
        assert_eq!(v["traces"][0]["imag"], json!([4., 5.]));
        assert_eq!(v["traces"][0]["unit"], "m/N");
        assert_eq!(v["traces"][0]["point_number"], 7);
    }
    #[test]
    fn record_ranges_keep_sample_grid_and_exact_window() {
        let w = WaveView {
            times: (0..12).map(|i| i as f64).collect(),
            samples: vec![(0..12).map(|i| i as f64).collect()],
            sample_rate: 1.,
            channels: vec![],
            gaps: vec![],
        };
        let v=record_selection(&w,&json!({"_ui_blocks":true,"nfft":4,"overlap":0.5,"_ui_first_record":1,"_ui_last_record":2})).unwrap();
        assert_eq!(v.times, [2., 3., 4., 5., 6., 7.]);
        assert_eq!(v.samples[0], v.times);
        let v=record_selection(&w,&json!({"_ui_blocks":true,"nfft":0,"overlap":0.5,"_ui_first_record":0,"_ui_last_record":1})).unwrap();
        assert_eq!(v.times, w.times);
        for config in [
            json!({"_ui_blocks":true,"nfft":32}),
            json!({"_ui_blocks":true,"nfft":4,"_ui_first_record":2,"_ui_last_record":1}),
            json!({"_ui_blocks":true,"nfft":4,"_ui_last_record":100}),
        ] {
            assert!(record_selection(&w, &config).is_err());
        }
    }
    #[test]
    fn plot_lines_break_at_undefined_frequency_bins() {
        let x = vec![json!(0), json!(1), json!(2), json!(3), json!(4)];
        let y = vec![json!(2), json!(3), Value::Null, json!(5), json!(6)];
        assert_eq!(
            finite_segments(&x, &y),
            vec![vec![[0., 2.], [1., 3.]], vec![[3., 5.], [4., 6.]]]
        );
    }
    #[test]
    fn export_uses_result_snapshot_and_real_uff_display_units() {
        let result = json!({"parameters":{"mode":"psd","window":"hann"},"source":{"recording":"original"},"frequencies":[1.,2.],"traces":[{"name":"PSD","unit":"dB re 1 V²/Hz","quantity_unit":"V²/Hz","values":[10.,20.],"real":[10.,100.],"imaginary":[0.,0.],"phase":[0.,0.],"coherence":null}]});
        let (v, n) = spectrum_export_request(
            &result,
            &json!({"mode":"h1","_ui_uff_complex":false}),
            "wrong",
            std::path::Path::new("x.uff"),
            true,
            false,
        )
        .unwrap();
        assert_eq!(n, 0);
        assert_eq!(v["settings"]["mode"], "psd");
        assert_eq!(v["source"]["recording"], "original");
        assert_eq!(v["traces"][0]["function_type"], 9);
        assert!(v["traces"][0].get("real").is_none());
        assert_eq!(v["traces"][0]["unit"], "dB re 1 V²/Hz");
        assert_eq!(v["traces"][0]["values"], json!([10., 20.]));
        let (v, _) = spectrum_export_request(
            &result,
            &json!({}),
            "",
            std::path::Path::new("x.csv"),
            false,
            false,
        )
        .unwrap();
        assert_eq!(v["traces"][0]["field_units"]["phase"], "deg");
        assert_eq!(v["traces"][0]["field_units"]["real"], "V²/Hz");
    }
    #[test]
    fn undefined_export_and_empty_selection_fail_without_fabricated_values() {
        let result = json!({"frequencies":[0.,1.],"traces":[{"values":[null,null],"real":[null,null],"imaginary":[null,null]}]});
        assert!(
            spectrum_export_request(
                &result,
                &json!({}),
                "",
                std::path::Path::new("x.uff"),
                true,
                false
            )
            .is_err()
        );
        let (csv, omitted) = spectrum_export_request(
            &result,
            &json!({}),
            "",
            std::path::Path::new("x.csv"),
            false,
            false,
        )
        .unwrap();
        assert_eq!(omitted, 0);
        assert_eq!(csv["frequencies"], json!([0., 1.]));
        assert!(csv["traces"][0]["values"][0].is_null());
        assert!(select_wave(&WaveView::default(), 0, None).is_err());
        let w = WaveView {
            times: vec![0., 1., 2.],
            samples: vec![vec![0., 1., 2.], vec![0.]],
            sample_rate: 1.,
            ..Default::default()
        };
        assert!(select_wave(&w, 0, None).is_err());
    }
    #[test]
    fn per_channel_filter_selection_keeps_physical_metadata() {
        let w = WaveView {
            times: vec![0., 1., 2.],
            samples: vec![vec![1., 2., 3.], vec![4., 5., 6.]],
            channels: vec![json!({"index":0,"unit":"N"}), json!({"index":3,"unit":"V"})],
            sample_rate: 1.,
            gaps: vec![],
        };
        let selected = filter_channel_input(&w, 1).unwrap();
        assert_eq!(selected.samples, vec![vec![4., 5., 6.]]);
        assert_eq!(selected.channels[0]["index"], 3);
        assert_eq!(selected.times, w.times);
        assert_eq!(filter_channel_input(&w, -1).unwrap().samples, w.samples);
        assert!(filter_channel_input(&w, 2).is_err());
    }
}
