use super::*;
use std::collections::BTreeMap;

#[derive(Clone)]
struct CompareSource {
    path: String,
    channels: String,
    zero_seconds: f64,
    sample_origin: u64,
    shift_seconds: f64,
}
impl CompareSource {
    fn from_value(value: &Value) -> Self {
        Self {
            path: value["path"].as_str().unwrap_or("").into(),
            channels: value["channel_indices"]
                .as_array()
                .map(|v| {
                    v.iter()
                        .filter_map(Value::as_u64)
                        .map(|n| n.to_string())
                        .collect::<Vec<_>>()
                        .join(",")
                })
                .unwrap_or_default(),
            zero_seconds: value["zero_seconds"].as_f64().unwrap_or(0.0),
            sample_origin: value["sample_origin"].as_u64().unwrap_or(0),
            shift_seconds: value["shift_seconds"].as_f64().unwrap_or(0.0),
        }
    }
    fn value(&self) -> Result<Value, String> {
        let channels = if self.channels.trim().is_empty() {
            Value::Null
        } else {
            let mut indices = Vec::new();
            for part in self
                .channels
                .split([',', '，', ';', ' '])
                .filter(|v| !v.is_empty())
            {
                let index = part
                    .parse::<u64>()
                    .map_err(|_| "通道列表请输入物理 AI 编号，例如 0,2".to_string())?;
                if indices.contains(&index) {
                    return Err("比较通道不能重复".into());
                }
                indices.push(index);
            }
            json!(indices)
        };
        Ok(
            json!({"path":self.path,"channel_indices":channels,"zero_seconds":self.zero_seconds,
            "sample_origin":self.sample_origin,"shift_seconds":self.shift_seconds}),
        )
    }
}

pub(super) struct DataPageState {
    pub(super) opened_path: String,
    pub(super) channel_indices: Vec<usize>,
    channel_overrides: Value,
    selection_enabled: bool,
    selection_start: f64,
    selection_stop: f64,
    export_values: String,
    time_format: String,
    include_statistics: bool,
    replay_axis: String,
    edit_experiment: String,
    edit_condition: String,
    group_units: bool,
    comparison_sources: Vec<CompareSource>,
    comparison_start: f64,
    comparison_span: f64,
    comparison_values: String,
    appearance: Value,
}
impl Default for DataPageState {
    fn default() -> Self {
        Self {
            opened_path: String::new(),
            channel_indices: vec![],
            channel_overrides: json!([]),
            selection_enabled: false,
            selection_start: 0.0,
            selection_stop: 1.0,
            export_values: "both".into(),
            time_format: "seconds".into(),
            include_statistics: false,
            replay_axis: "relative".into(),
            edit_experiment: String::new(),
            edit_condition: String::new(),
            group_units: true,
            comparison_sources: vec![],
            comparison_start: 0.0,
            comparison_span: 10.0,
            comparison_values: "engineering".into(),
            appearance: json!({}),
        }
    }
}

fn record_label(entry: &Value) -> String {
    let path = entry["relative_path"]
        .as_str()
        .or(entry["path"].as_str())
        .unwrap_or("记录");
    let duration = entry["duration_seconds"].as_f64().unwrap_or(0.0);
    let channels = entry["channels"].as_array().map_or(0, Vec::len);
    format!(
        "{path}   ·   {duration:.3} s   ·   {channels} 通道   ·   {}",
        entry["status"].as_str().unwrap_or("")
    )
}
fn comparison_label(trace: &Value) -> String {
    format!(
        "{} · {}",
        trace["source_index"].as_u64().unwrap_or(0) + 1,
        trace["label"].as_str().unwrap_or("曲线")
    )
}
fn list_combo(ui: &mut egui::Ui, id: &str, value: &mut String, options: &[(&str, &str)]) {
    let selected = options
        .iter()
        .find(|v| v.0 == value.as_str())
        .map(|v| v.1)
        .unwrap_or(value.as_str());
    egui::ComboBox::from_id_salt(id)
        .selected_text(selected)
        .show_ui(ui, |ui| {
            for (code, label) in options {
                ui.selectable_value(value, (*code).into(), *label);
            }
        });
}
fn csv_text(stats: &Value) -> String {
    let mut lines = vec!["AI\t名称\t单位\t样本数\t最小\t最大\t峰峰\t平均\t标准差\tRMS".to_owned()];
    for stat in stats["statistics"].as_array().into_iter().flatten() {
        lines.push(
            [
                "index", "name", "unit", "count", "min", "max", "delta", "mean", "std", "rms",
            ]
            .iter()
            .map(|k| {
                stat[*k]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| stat[*k].to_string())
            })
            .collect::<Vec<_>>()
            .join("\t"),
        );
    }
    lines.join("\n")
}
fn reliable_epoch(metadata: &Value) -> Option<f64> {
    let parse = |v: &Value| {
        time::OffsetDateTime::parse(v.as_str()?, &time::format_description::well_known::Rfc3339)
            .ok()
            .map(|v| v.unix_timestamp_nanos() as f64 / 1e9)
    };
    parse(&metadata["acquisition_started_at_utc"])
        .map(|v| {
            v + metadata["acquisition_config"]["record_start_scan"]
                .as_u64()
                .unwrap_or(0) as f64
                / metadata["sample_rate"].as_f64().unwrap_or(1.0)
        })
        .or_else(|| parse(&metadata["started_at_utc"]))
}
fn color_text(color: Color32) -> String {
    format!("#{:02x}{:02x}{:02x}", color.r(), color.g(), color.b())
}
fn parse_color(value: &Value, fallback: Color32) -> Color32 {
    let Some(s) = value
        .as_str()
        .filter(|s| s.starts_with('#') && s.len() == 7)
    else {
        return fallback;
    };
    match u32::from_str_radix(&s[1..], 16) {
        Ok(v) => Color32::from_rgb((v >> 16) as u8, (v >> 8) as u8, v as u8),
        Err(_) => fallback,
    }
}

impl LabDaqApp {
    fn scan_library(&mut self) {
        self.start_job(
            "扫描记录库",
            json!({"op":"list","root":self.settings.recording_root,"query":self.query}),
        );
    }
    fn replay_request(&self, op: &str) -> Value {
        let mut request = json!({"op":op,"path":self.open_path,"channels":self.data_ui.channel_indices,
            "channel_overrides":self.data_ui.channel_overrides,"values":if self.raw_view{"raw"}else{"engineering"}});
        if self.data_ui.selection_enabled {
            request["start_seconds"] = json!(self.data_ui.selection_start);
            request["stop_seconds"] = json!(self.data_ui.selection_stop);
        }
        request
    }
    fn load_selected_window(&mut self) {
        if self.data_ui.channel_indices.is_empty() {
            self.error = "请选择至少一个回看通道。".into();
            return;
        }
        self.start_job("读取窗口",json!({"op":"read","path":self.open_path,"start_seconds":self.replay_start,
            "count":self.replay_count,"channels":self.data_ui.channel_indices,"channel_overrides":self.data_ui.channel_overrides,"values":if self.raw_view{"raw"}else{"engineering"}}));
    }
    fn add_comparison_path(&mut self, path: String) {
        if !self
            .data_ui
            .comparison_sources
            .iter()
            .any(|s| s.path == path)
        {
            self.data_ui.comparison_sources.push(CompareSource {
                path: path.clone(),
                channels: String::new(),
                zero_seconds: 0.0,
                sample_origin: 0,
                shift_seconds: 0.0,
            });
        }
        if !self.comparison_paths.contains(&path) {
            self.comparison_paths.push(path);
        }
    }
    pub(super) fn apply_comparison_layout(&mut self, layout: Value) {
        self.data_ui.comparison_sources = layout["sources"]
            .as_array()
            .map(|v| v.iter().map(CompareSource::from_value).collect())
            .unwrap_or_default();
        self.comparison_paths = self
            .data_ui
            .comparison_sources
            .iter()
            .map(|v| v.path.clone())
            .collect();
        self.comparison_axis = layout["axis"].as_str().unwrap_or("relative").into();
        self.data_ui.comparison_start = layout["window"]["start"].as_f64().unwrap_or(0.0);
        self.data_ui.comparison_span = layout["window"]["span"].as_f64().unwrap_or(10.0);
        self.data_ui.comparison_values = layout["values"].as_str().unwrap_or("engineering").into();
        self.data_ui.appearance = layout
            .get("appearance")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or(json!({}));
        self.comparison_result = Value::Null;
        self.page = Page::Compare;
        match self.comparison_request("compare") {
            Ok(request) => self.start_job("比较记录", request),
            Err(e) => self.error = e,
        }
    }
    fn comparison_request(&self, op: &str) -> Result<Value, String> {
        let sources = self
            .data_ui
            .comparison_sources
            .iter()
            .map(CompareSource::value)
            .collect::<Result<Vec<_>, _>>()?;
        if sources.is_empty() {
            return Err("请先添加至少一组记录。".into());
        }
        Ok(
            json!({"op":op,"schema":"labdaq-comparison-v1","sources":sources,"axis":self.comparison_axis,
            "values":self.data_ui.comparison_values,"window":{"start":self.data_ui.comparison_start,"span":self.data_ui.comparison_span},
            "appearance":self.data_ui.appearance,"view":{}}),
        )
    }
    pub(super) fn library_page(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.label("记录目录");
            ui.monospace(self.settings.recording_root.display().to_string());
            if ui.button("更换目录").clicked() {
                if let Some(path) = rfd::FileDialog::new().pick_folder() {
                    self.settings.recording_root = path;
                    self.scan_library();
                }
            }
            if ui.button("刷新").clicked() {
                self.scan_library();
            }
            if ui.button("打开原生记录…").clicked() {
                if let Some(path) = rfd::FileDialog::new().pick_folder() {
                    self.open_recording(path.display().to_string());
                }
            }
            if ui.button("打开 HPF…").clicked() {
                if let Some(path) = rfd::FileDialog::new()
                    .add_filter("HPF", &["hpf"])
                    .pick_file()
                {
                    self.open_recording(path.display().to_string());
                }
            }
        });
        ui.horizontal_wrapped(|ui| {
            ui.label("搜索实验、工况或备注");
            let response = ui.add(egui::TextEdit::singleline(&mut self.query).desired_width(260.0));
            if ui.button("搜索").clicked()
                || (response.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)))
            {
                self.scan_library();
            }
            ui.label(format!(
                "{} 组 · 已选 {} 组",
                self.library.len(),
                self.selected.len()
            ));
            if ui.button("清空选择").clicked() {
                self.selected.clear();
            }
        });
        let mut open = None;
        egui::ScrollArea::vertical()
            .id_salt("recording_list")
            .max_height(220.0)
            .show(ui, |ui| {
                for entry in &self.library {
                    let path = entry["path"].as_str().unwrap_or("").to_owned();
                    ui.horizontal(|ui| {
                        let mut selected = self.selected.contains(&path);
                        if ui.checkbox(&mut selected, "").changed() {
                            if selected {
                                self.selected.push(path.clone());
                            } else {
                                self.selected.retain(|v| v != &path);
                            }
                        }
                        let label = record_label(entry);
                        let response = ui.selectable_label(self.open_path == path, label);
                        if response.double_clicked() {
                            open = Some(path.clone());
                        }
                        if ui.small_button("打开").clicked() {
                            open = Some(path.clone());
                        }
                        if let Some(error) = entry["error"].as_str() {
                            ui.colored_label(Color32::RED, error);
                        }
                    });
                }
                if self.library.is_empty() {
                    ui.label("当前目录没有记录。可打开其他目录或导入 HPF。");
                }
            });
        if let Some(path) = open {
            self.open_recording(path);
        }
        ui.horizontal_wrapped(|ui|{
            if ui.add_enabled(!self.selected.is_empty(),egui::Button::new("比较所选记录")).clicked(){for path in self.selected.clone(){self.add_comparison_path(path);}self.page=Page::Compare;}
            if ui.add_enabled(!self.selected.is_empty(),egui::Button::new("批量导出 CSV…")).clicked(){if let Some(output)=rfd::FileDialog::new().pick_folder(){self.start_job("批量导出",json!({"op":"batch_export","paths":self.selected,"output_dir":output,"values":self.data_ui.export_values,"time_format":self.data_ui.time_format,"include_statistics":self.data_ui.include_statistics}));}}
            if ui.add_enabled(!self.selected.is_empty(),egui::Button::new("批量导出 TXT…")).clicked(){if let Some(output)=rfd::FileDialog::new().pick_folder(){self.start_job("批量导出",json!({"op":"batch_export","paths":self.selected,"output_dir":output,"delimiter":"\t","values":self.data_ui.export_values,"time_format":self.data_ui.time_format,"include_statistics":self.data_ui.include_statistics}));}}
        });
        ui.separator();
        if self.replay_info["path"].as_str() != Some(self.open_path.as_str())
            || self.open_path.is_empty()
        {
            ui.label("打开一组记录，查看波形、统计与导出。");
            return;
        }
        if self.data_ui.opened_path != self.open_path {
            self.data_ui.opened_path = self.open_path.clone();
            self.data_ui.channel_indices = self.replay_info["channels"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|c| c["index"].as_u64().map(|v| v as usize))
                .collect();
            self.data_ui.channel_overrides = self.replay_info["channels"].clone();
            self.data_ui.selection_enabled = false;
            self.data_ui.selection_start = 0.0;
            self.data_ui.selection_stop =
                self.replay_info["elapsed_seconds"].as_f64().unwrap_or(1.0);
            self.data_ui.edit_experiment = self.replay_info["metadata"]["experiment"]
                .as_str()
                .unwrap_or("")
                .into();
            self.data_ui.edit_condition =
                self.replay_info["metadata"]["recording_options"]["condition"]
                    .as_str()
                    .unwrap_or("")
                    .into();
            self.data_ui.replay_axis = "relative".into();
            self.stats = Value::Null;
        }
        ui.heading(
            PathBuf::from(&self.open_path)
                .file_name()
                .unwrap_or_default()
                .to_string_lossy(),
        );
        ui.label(format!(
            "{} 帧 · {:.6} S/s · 记录 {:.3} s · 时间跨度 {:.3} s",
            self.replay_info["sample_count"],
            self.replay_info["sample_rate"].as_f64().unwrap_or(0.0),
            self.replay_info["duration_seconds"].as_f64().unwrap_or(0.0),
            self.replay_info["elapsed_seconds"].as_f64().unwrap_or(0.0)
        ));
        if self.replay_info["abnormal"] == true || self.replay_info["finalized"] != true {
            ui.colored_label(
                Color32::from_rgb(180, 90, 20),
                "此记录异常结束或尚未收尾。可查看已有完整帧；请查看结束原因。",
            );
            ui.label(self.replay_info["metadata"]["error"].as_str().unwrap_or(""));
        }
        let mut channels_changed = false;
        ui.horizontal_wrapped(|ui| {
            ui.label("回看 / 统计 / 导出通道");
            for ch in self.replay_info["channels"]
                .as_array()
                .into_iter()
                .flatten()
            {
                let Some(index) = ch["index"].as_u64().map(|v| v as usize) else {
                    continue;
                };
                let mut checked = self.data_ui.channel_indices.contains(&index);
                if ui
                    .checkbox(
                        &mut checked,
                        format!("AI{index} {}", ch["name"].as_str().unwrap_or("")),
                    )
                    .changed()
                {
                    channels_changed = true;
                    if checked {
                        self.data_ui.channel_indices.push(index);
                    } else {
                        self.data_ui.channel_indices.retain(|v| *v != index);
                    }
                }
            }
        });
        if channels_changed && !self.data_ui.channel_indices.is_empty() {
            self.load_selected_window();
        }
        let fs = self.replay_info["sample_rate"].as_f64().unwrap_or(1.0);
        let elapsed = self.replay_info["elapsed_seconds"].as_f64().unwrap_or(0.0);
        ui.horizontal_wrapped(|ui| {
            ui.label("定位 / s");
            ui.add(
                egui::DragValue::new(&mut self.replay_start)
                    .range(0.0..=elapsed.max(0.0))
                    .speed(0.01),
            );
            ui.label("窗口帧数");
            ui.add(egui::DragValue::new(&mut self.replay_count).range(1..=200000));
            if ui.button("前一窗口").clicked() {
                self.replay_start = (self.replay_start - self.replay_count as f64 / fs).max(0.0);
                self.load_selected_window();
            }
            if ui.button("定位 / 刷新").clicked() {
                self.load_selected_window();
            }
            if ui.button("后一窗口").clicked() {
                self.replay_start =
                    (self.replay_start + self.replay_count as f64 / fs).min(elapsed);
                self.load_selected_window();
            }
        });
        if elapsed > 0.0
            && ui
                .add(egui::Slider::new(&mut self.replay_start, 0.0..=elapsed).text("时间位置 / s"))
                .drag_stopped()
        {
            self.load_selected_window();
        }
        ui.horizontal_wrapped(|ui| {
            if ui.checkbox(&mut self.raw_view, "原始电压").changed() {
                self.load_selected_window();
            }
            ui.label("横轴");
            list_combo(
                ui,
                "replay_axis",
                &mut self.data_ui.replay_axis,
                &[
                    ("relative", "相对时间 / s"),
                    ("absolute", "绝对 UTC 时间 / s"),
                    ("samples", "样本号"),
                ],
            );
            ui.checkbox(&mut self.cursors, "双游标");
            ui.label("A");
            ui.add(egui::DragValue::new(&mut self.cursor_a).speed(0.001));
            ui.label("B");
            ui.add(egui::DragValue::new(&mut self.cursor_b).speed(0.001));
            if ui.button("游标区间 → 统计选区").clicked() {
                let convert = |x: f64| match self.data_ui.replay_axis.as_str() {
                    "samples" => x / fs,
                    "absolute" => x - reliable_epoch(&self.replay_info["metadata"]).unwrap_or(0.0),
                    _ => x,
                };
                self.data_ui.selection_start = convert(self.cursor_a.min(self.cursor_b));
                self.data_ui.selection_stop = convert(self.cursor_a.max(self.cursor_b));
                self.data_ui.selection_enabled = true;
            }
        });
        self.replay_actions(ui);
        self.show_statistics(ui);
        self.replay_plots(ui);
        egui::CollapsingHeader::new("回看换算设置").show(ui, |ui| {
            ui.label("调整本次回看和导出的工程量换算，原始电压与采集配置保持原样。");
            for channel in self
                .data_ui
                .channel_overrides
                .as_array_mut()
                .into_iter()
                .flatten()
            {
                let index = channel["index"].as_u64().unwrap_or(0);
                ui.push_id(("replay_conversion", index), |ui| {
                    egui::CollapsingHeader::new(format!("AI{index}")).show(ui, |ui| {
                        text(ui, channel, "name", "名称");
                        text(ui, channel, "unit", "单位");
                        number(
                            ui,
                            channel,
                            "sensitivity_mv_per_unit",
                            "灵敏度 mV/EU",
                            0.0000001..=1e12,
                            0.1,
                        );
                        number(ui, channel, "zero_volts", "电压零点 / V", -1e6..=1e6, 0.001);
                        number(
                            ui,
                            channel,
                            "engineering_offset",
                            "工程偏移",
                            -1e12..=1e12,
                            0.01,
                        );
                        flag(ui, channel, "inverted", "反向");
                    });
                });
            }
            ui.horizontal(|ui| {
                if ui.button("应用到当前窗口").clicked() {
                    self.load_selected_window();
                }
                if ui.button("恢复采集时换算").clicked() {
                    self.data_ui.channel_overrides = self.replay_info["channels"].clone();
                    self.load_selected_window();
                }
            });
        });
        egui::CollapsingHeader::new("记录描述").show(ui,|ui|{
            ui.add_enabled_ui(self.replay_info["metadata"]["format"]=="labdaq-raw-voltage"&&self.replay_info["finalized"]==true,|ui|{
                ui.horizontal(|ui|{ui.label("实验");ui.text_edit_singleline(&mut self.data_ui.edit_experiment);ui.label("工况");ui.text_edit_singleline(&mut self.data_ui.edit_condition);});
                ui.horizontal(|ui|{ui.label("试样");ui.text_edit_singleline(&mut self.edit_specimen);});ui.label("备注");ui.text_edit_multiline(&mut self.edit_notes);
                if ui.button("保存描述").clicked(){self.start_job("保存描述",json!({"op":"update_metadata","path":self.open_path,"experiment":self.data_ui.edit_experiment,"condition":self.data_ui.edit_condition,"specimen":self.edit_specimen,"notes":self.edit_notes}));}
            });
            if self.replay_info["finalized"]!=true&&ui.button("将完整帧恢复到新目录…").clicked(){if let Some(parent)=rfd::FileDialog::new().pick_folder(){let name=PathBuf::from(&self.open_path).file_name().unwrap_or_default().to_string_lossy().into_owned();let output=parent.join(format!("{name}-恢复-{}",time::OffsetDateTime::now_utc().unix_timestamp()));self.start_job("恢复记录",json!({"op":"recover","path":self.open_path,"output":output}));}}
            egui::CollapsingHeader::new("采集信息与结束原因").show(ui,|ui|{ui.monospace(serde_json::to_string_pretty(&self.replay_info["metadata"]).unwrap_or_default());});
        });
    }
    fn replay_actions(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.checkbox(&mut self.data_ui.selection_enabled, "仅选区");
            ui.label("起点 / s");
            ui.add_enabled(
                self.data_ui.selection_enabled,
                egui::DragValue::new(&mut self.data_ui.selection_start).speed(0.001),
            );
            ui.label("终点 / s（不含）");
            ui.add_enabled(
                self.data_ui.selection_enabled,
                egui::DragValue::new(&mut self.data_ui.selection_stop).speed(0.001),
            );
            if ui.button("计算统计").clicked() {
                self.start_job("区间统计", self.replay_request("stats"));
            }
            if ui.button("送到信号分析").clicked() {
                self.page = Page::Analysis;
            }
            if ui.button("加入多组比较").clicked() {
                self.add_comparison_path(self.open_path.clone());
                self.page = Page::Compare;
            }
            if ui.button("复制当前窗口数据").clicked() {
                let mut rows = vec![
                    std::iter::once("time_s".to_owned())
                        .chain(self.replay.channels.iter().map(|c| {
                            c["name"]
                                .as_str()
                                .unwrap_or("signal")
                                .replace(['\t', '\n'], " ")
                        }))
                        .collect::<Vec<_>>()
                        .join("\t"),
                ];
                for i in 0..self.replay.times.len() {
                    let mut row = vec![self.replay.times[i].to_string()];
                    row.extend(
                        self.replay
                            .samples
                            .iter()
                            .filter_map(|v| v.get(i))
                            .map(|v| v.to_string()),
                    );
                    rows.push(row.join("\t"));
                }
                ui.ctx().copy_text(rows.join("\n"));
                self.notice = "当前窗口数据已复制".into();
            }
        });
        ui.horizontal_wrapped(|ui| {
            ui.label("导出内容");
            list_combo(
                ui,
                "data_export_values",
                &mut self.data_ui.export_values,
                &[
                    ("both", "原始 V + 工程量"),
                    ("raw", "原始电压"),
                    ("engineering", "工程量"),
                ],
            );
            list_combo(
                ui,
                "data_export_time",
                &mut self.data_ui.time_format,
                &[
                    ("seconds", "秒"),
                    ("milliseconds", "毫秒"),
                    ("microseconds", "微秒"),
                    ("samples", "样本号"),
                    ("datetime", "UTC 时间字符串"),
                    ("excel", "Excel UTC 日期"),
                ],
            );
            ui.checkbox(&mut self.data_ui.include_statistics, "附统计表");
            for (label, extension, op, delimiter) in [
                ("CSV…", "csv", "export", ","),
                ("TXT…", "txt", "export", "\t"),
                ("HPF…", "hpf", "export_hpf", ","),
            ] {
                if ui.button(label).clicked() {
                    if let Some(output) = rfd::FileDialog::new()
                        .set_file_name(format!("LabDAQ.{extension}"))
                        .save_file()
                    {
                        let mut request = self.replay_request(op);
                        request["output"] = json!(output);
                        request["values"] = json!(self.data_ui.export_values);
                        request["time_format"] = json!(self.data_ui.time_format);
                        request["include_statistics"] = json!(self.data_ui.include_statistics);
                        request["delimiter"] = json!(delimiter);
                        request["overwrite"] = json!(true);
                        self.start_job("导出记录", request);
                    }
                }
            }
        });
    }
    fn show_statistics(&mut self, ui: &mut egui::Ui) {
        let Some(stats) = self.stats["statistics"].as_array() else {
            return;
        };
        egui::CollapsingHeader::new("区间统计结果")
            .default_open(true)
            .show(ui, |ui| {
                if ui.button("复制统计表").clicked() {
                    ui.ctx().copy_text(csv_text(&self.stats));
                }
                egui::ScrollArea::horizontal().show(ui, |ui| {
                    egui::Grid::new("statistics_grid")
                        .striped(true)
                        .show(ui, |ui| {
                            for title in [
                                "通道",
                                "单位",
                                "样本数",
                                "最小",
                                "最大",
                                "峰峰",
                                "平均",
                                "标准差",
                                "RMS",
                            ] {
                                ui.strong(title);
                            }
                            ui.end_row();
                            for row in stats {
                                ui.label(row["name"].as_str().unwrap_or(""));
                                ui.label(row["unit"].as_str().unwrap_or(""));
                                ui.label(row["count"].to_string());
                                for key in ["min", "max", "delta", "mean", "std", "rms"] {
                                    ui.monospace(format!(
                                        "{:.6}",
                                        row[key].as_f64().unwrap_or(f64::NAN)
                                    ));
                                }
                                ui.end_row();
                            }
                        });
                });
            });
    }
    fn replay_plots(&mut self, ui: &mut egui::Ui) {
        let wave = self.replay.clone();
        if wave.samples.is_empty() {
            return;
        }
        let axis = self.data_ui.replay_axis.as_str();
        let fs = wave.sample_rate;
        let epoch = reliable_epoch(&self.replay_info["metadata"]);
        if axis == "absolute" && epoch.is_none() {
            ui.colored_label(
                Color32::RED,
                "此记录没有可靠绝对时间，请使用相对时间或样本号。",
            );
            return;
        }
        let times: Vec<_> = wave
            .times
            .iter()
            .map(|t| match axis {
                "absolute" => epoch.unwrap_or(0.0) + t,
                "samples" => t * fs,
                _ => *t,
            })
            .collect();
        let x_label = match axis {
            "absolute" => "UTC Unix 秒",
            "samples" => "源样本号",
            _ => "相对时间 / s",
        };
        for (i, samples) in wave.samples.iter().enumerate() {
            let channel = wave.channels.get(i).cloned().unwrap_or(Value::Null);
            let name = channel["name"].as_str().unwrap_or("通道");
            let unit = if self.raw_view {
                "V"
            } else {
                channel["unit"].as_str().unwrap_or("")
            };
            ui.horizontal_wrapped(|ui| {
                ui.strong(name);
                ui.label(unit);
                if self.cursors {
                    let a = nearest(&times, self.cursor_a);
                    let b = nearest(&times, self.cursor_b);
                    for (label, index) in [("A", a), ("B", b)] {
                        if let Some(index) = index.filter(|v| *v < samples.len()) {
                            ui.label(format!(
                                "{label}: {:.6} / {:.6}",
                                times[index], samples[index]
                            ));
                        }
                    }
                    if let (Some(a), Some(b)) = (a, b) {
                        if a < samples.len() && b < samples.len() {
                            let seconds = (wave.times[b] - wave.times[a]).abs();
                            ui.label(format!(
                                "Δy {:.6} · Δt {:.6} s",
                                samples[b] - samples[a],
                                seconds
                            ));
                            if seconds > 0.0 {
                                ui.label(format!("1/Δt {:.4} Hz", 1.0 / seconds));
                            }
                        }
                    }
                }
            });
            let points: Vec<_> = times
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
            let mut boundaries = vec![0];
            boundaries.extend(
                wave.gaps
                    .iter()
                    .copied()
                    .filter(|v| *v > 0 && *v < points.len()),
            );
            boundaries.push(points.len());
            let plot = Plot::new(("replay", i))
                .height(self.plot_height)
                .link_axis("replay_axes", [true, false])
                .x_axis_label(x_label)
                .y_axis_label(if self.log_y {
                    format!("log₁₀({unit})")
                } else {
                    unit.to_string()
                })
                .show(ui, |plot| {
                    for range in boundaries.windows(2) {
                        plot.line(
                            Line::new(
                                name,
                                PlotPoints::from(downsample(&points[range[0]..range[1]], 4000)),
                            )
                            .color(COLORS[i % 4])
                            .width(self.line_width),
                        );
                    }
                    if self.cursors {
                        plot.vline(VLine::new("A", self.cursor_a));
                        plot.vline(VLine::new("B", self.cursor_b));
                    }
                    plot.pointer_coordinate()
                });
            if self.cursors && plot.response.clicked() {
                if let Some(point) = plot.inner {
                    if ui.input(|i| i.modifiers.shift) {
                        self.cursor_b = point.x
                    } else {
                        self.cursor_a = point.x
                    }
                }
            }
        }
    }
    pub(super) fn comparison_page(&mut self, ui: &mut egui::Ui) {
        ui.label("各组按真实采样时间叠加；暂停缺口保持断线。");
        ui.horizontal_wrapped(|ui|{
            if ui.button("添加原生记录…").clicked(){if let Some(path)=rfd::FileDialog::new().pick_folder(){self.add_comparison_path(path.display().to_string());}}
            if ui.button("添加 HPF…").clicked(){if let Some(path)=rfd::FileDialog::new().add_filter("HPF",&["hpf"]).pick_file(){self.add_comparison_path(path.display().to_string());}}
            if ui.button("加入记录库所选").clicked(){for path in self.selected.clone(){self.add_comparison_path(path);}}
            if ui.button("载入布局…").clicked(){if let Some(path)=rfd::FileDialog::new().add_filter("JSON",&["json"]).pick_file(){self.start_job("加载比较布局",json!({"op":"load_layout","path":path}));}}
            if ui.button("保存布局…").clicked(){match self.comparison_request("compare"){Ok(mut layout)=>{layout.as_object_mut().unwrap().remove("op");if let Some(path)=rfd::FileDialog::new().set_file_name("comparison.json").save_file(){self.start_job("保存比较布局",json!({"op":"save_layout","layout":layout,"output":path,"overwrite":true}));}},Err(e)=>self.error=e}}
        });
        let mut remove = None;
        for (index, source) in self.data_ui.comparison_sources.iter_mut().enumerate() {
            ui.push_id(("source", index), |ui| {
                egui::Frame::group(ui.style()).show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.strong(format!("记录 {}", index + 1));
                        ui.label(&source.path);
                        if ui.small_button("移除").clicked() {
                            remove = Some(index);
                        }
                    });
                    ui.horizontal_wrapped(|ui| {
                        ui.label("物理 AI（空白为全部）");
                        ui.add(
                            egui::TextEdit::singleline(&mut source.channels).desired_width(100.0),
                        );
                        ui.label("相对时间零点 / s");
                        ui.add(
                            egui::DragValue::new(&mut source.zero_seconds)
                                .range(0.0..=1e12)
                                .speed(0.001),
                        );
                        ui.label("样本零点");
                        ui.add(egui::DragValue::new(&mut source.sample_origin));
                        ui.label("绝对时间偏移 / s");
                        ui.add(egui::DragValue::new(&mut source.shift_seconds).speed(0.001));
                        if ui.small_button("重置对齐").clicked() {
                            source.zero_seconds = 0.0;
                            source.sample_origin = 0;
                            source.shift_seconds = 0.0;
                        }
                    });
                });
            });
        }
        if let Some(index) = remove {
            self.data_ui.comparison_sources.remove(index);
            self.data_ui.appearance = json!({});
            self.comparison_result = Value::Null;
        }
        ui.horizontal_wrapped(|ui| {
            list_combo(
                ui,
                "compare_axis",
                &mut self.comparison_axis,
                &[
                    ("relative", "相对时间"),
                    ("absolute", "绝对 UTC 时间"),
                    ("samples", "样本号"),
                ],
            );
            list_combo(
                ui,
                "compare_values",
                &mut self.data_ui.comparison_values,
                &[("engineering", "工程量"), ("raw", "原始 V")],
            );
            ui.label("窗口起点");
            ui.add(egui::DragValue::new(&mut self.data_ui.comparison_start).speed(0.01));
            ui.label("宽度");
            ui.add(
                egui::DragValue::new(&mut self.data_ui.comparison_span)
                    .range(0.000001..=1e12)
                    .speed(0.1),
            );
            if ui.button("更新比较").clicked() {
                match self.comparison_request("compare") {
                    Ok(request) => self.start_job("比较记录", request),
                    Err(e) => self.error = e,
                }
            }
            if ui.button("从头查看").clicked() {
                self.data_ui.comparison_start = if self.comparison_axis == "absolute" {
                    self.comparison_result["bounds"][0].as_f64().unwrap_or(0.0)
                } else {
                    0.0
                };
                match self.comparison_request("compare") {
                    Ok(request) => self.start_job("比较记录", request),
                    Err(e) => self.error = e,
                }
            }
            ui.checkbox(&mut self.data_ui.group_units, "按单位分图");
        });
        ui.horizontal_wrapped(|ui|{
            ui.checkbox(&mut self.cursors,"联动游标");ui.label("A");ui.add(egui::DragValue::new(&mut self.cursor_a).speed(0.001));ui.label("B");ui.add(egui::DragValue::new(&mut self.cursor_b).speed(0.001));
            for(label,extension,delimiter)in[("导出当前窗口 CSV…","csv",","),("导出 TXT…","txt","\t")]{if ui.add_enabled(self.comparison_result["traces"].as_array().is_some_and(|v|!v.is_empty()),egui::Button::new(label)).clicked(){if let Some(output)=rfd::FileDialog::new().set_file_name(format!("comparison.{extension}")).save_file(){let request=json!({"op":"export_comparison","output":output,"delimiter":delimiter,"overwrite":true,"sources":self.comparison_result["sources"],"axis":self.comparison_result["axis"],"window":self.comparison_result["window"],"values":self.comparison_result["values"]});self.start_job("导出比较",request);}}}
        });
        if self.comparison_result["truncated"] == true {
            ui.colored_label(
                Color32::from_rgb(180, 90, 20),
                "当前窗口达到读取上限，显示和导出仅包含已加载部分。缩小窗口以查看完整细节。",
            );
        }
        let traces = self.comparison_result["traces"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if traces.is_empty() {
            ui.label("添加记录并点击“更新比较”。");
            return;
        }
        let mut groups: BTreeMap<String, Vec<(usize, Value)>> = BTreeMap::new();
        for (index, trace) in traces.into_iter().enumerate() {
            let unit = trace["unit"].as_str().unwrap_or("").to_owned();
            let key = if self.data_ui.group_units {
                unit
            } else {
                format!("{index:04}|{unit}")
            };
            groups.entry(key).or_default().push((index, trace));
        }
        // Different physical units must never share a numeric ordinate.
        for (group_key, traces) in groups {
            let unit = traces[0].1["unit"].as_str().unwrap_or("").to_owned();
            ui.strong(format!("单位：{unit}"));
            for (index, trace) in &traces {
                let key = format!("{}:{}", trace["source_index"], trace["channel"]["index"]);
                let style = &self.data_ui.appearance[&key];
                let mut visible = style["visible"].as_bool().unwrap_or(true);
                let mut color = parse_color(&style["color"], COLORS[index % 4]);
                ui.horizontal_wrapped(|ui| {
                    ui.checkbox(&mut visible, comparison_label(trace));
                    ui.color_edit_button_srgba(&mut color);
                    if self.cursors {
                        let times: Vec<_> = trace["x"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .filter_map(Value::as_f64)
                            .collect();
                        for (label, x) in [("A", self.cursor_a), ("B", self.cursor_b)] {
                            if let Some(i) = nearest(&times, x) {
                                ui.label(format!(
                                    "{label}: {:.6} / {:.6}",
                                    times[i],
                                    trace["values"][i].as_f64().unwrap_or(f64::NAN)
                                ));
                            }
                        }
                    }
                });
                self.data_ui.appearance[&key] =
                    json!({"visible":visible,"color":color_text(color)});
            }
            let x_label = match self.comparison_result["axis"].as_str() {
                Some("samples") => "源样本号",
                Some("absolute") => "UTC Unix 秒",
                _ => "相对时间 / s",
            };
            let response = Plot::new(("comparison", &group_key))
                .height(self.plot_height)
                .legend(Legend::default())
                .link_axis("comparison_axes", [true, false])
                .x_axis_label(x_label)
                .y_axis_label(&unit)
                .show(ui, |plot| {
                    for (index, trace) in &traces {
                        let key =
                            format!("{}:{}", trace["source_index"], trace["channel"]["index"]);
                        let style = &self.data_ui.appearance[&key];
                        if style["visible"] == false {
                            continue;
                        }
                        let x = trace["x"].as_array().unwrap();
                        let y = trace["values"].as_array().unwrap();
                        let points: Vec<_> = x
                            .iter()
                            .zip(y)
                            .map(|(x, y)| {
                                [
                                    x.as_f64().unwrap_or(f64::NAN),
                                    y.as_f64().unwrap_or(f64::NAN),
                                ]
                            })
                            .collect();
                        let mut bounds = vec![0];
                        bounds.extend(
                            trace["gaps"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter_map(|v| v.as_u64().map(|n| n as usize))
                                .filter(|n| *n > 0 && *n < points.len()),
                        );
                        bounds.push(points.len());
                        for range in bounds.windows(2) {
                            plot.line(
                                Line::new(
                                    comparison_label(trace),
                                    PlotPoints::from(downsample(&points[range[0]..range[1]], 4000)),
                                )
                                .color(parse_color(&style["color"], COLORS[index % 4]))
                                .width(self.line_width),
                            );
                        }
                    }
                    if self.cursors {
                        plot.vline(VLine::new("A", self.cursor_a));
                        plot.vline(VLine::new("B", self.cursor_b));
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
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn comparison_legends_distinguish_equal_group_names() {
        let a = json!({"source_index":0,"label":"recording / AI0"});
        let b = json!({"source_index":1,"label":"recording / AI0"});
        assert_eq!(comparison_label(&a), "1 · recording / AI0");
        assert_ne!(comparison_label(&a), comparison_label(&b));
    }
    #[test]
    fn comparison_channels_preserve_physical_indices() {
        let source = CompareSource {
            path: "record".into(),
            channels: "0, 2".into(),
            zero_seconds: 0.0,
            sample_origin: 0,
            shift_seconds: 0.0,
        };
        assert_eq!(source.value().unwrap()["channel_indices"], json!([0, 2]));
        let mut duplicate = source;
        duplicate.channels = "0,0".into();
        assert!(duplicate.value().is_err());
    }
    #[test]
    fn colors_roundtrip_and_invalid_falls_back() {
        let c = Color32::from_rgb(123, 45, 67);
        assert_eq!(parse_color(&json!(color_text(c)), Color32::WHITE), c);
        assert_eq!(parse_color(&json!("invalid"), c), c);
    }
    #[test]
    fn absolute_replay_uses_record_offset() {
        let meta = json!({"acquisition_started_at_utc":"1970-01-01T00:00:00Z","sample_rate":10.0,"acquisition_config":{"record_start_scan":15}});
        assert_eq!(reliable_epoch(&meta), Some(1.5));
        assert_eq!(reliable_epoch(&json!({})), None);
    }
}
