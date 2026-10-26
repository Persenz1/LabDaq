use super::*;
impl LabDaqApp {
    pub(super) fn live_page(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui|{
   if ui.add_enabled(self.state["running"]!=true,egui::Button::new("开始预览")).clicked(){self.command(json!({"op":"start","profile":self.settings.profile(),"recording_path":null}));}
   if ui.add_enabled(self.state["recording"]!=true&&self.state["collection"].is_null(),egui::Button::new("● 开始记录")).clicked(){if self.state["running"]==true{self.command(json!({"op":"start_recording","path":self.settings.next_recording(),"channels":self.settings.channels,"experiment":self.settings.experiment,"recording_options":self.settings.recording_options()}));}else{self.command(json!({"op":"start","profile":self.settings.profile(),"recording_path":self.settings.next_recording()}));}}
   let paused=self.state["recording_paused"]==true;if ui.add_enabled(self.state["recording"]==true,egui::Button::new(if paused{"继续记录"}else{"暂停记录"})).clicked(){self.command(json!({"op":if paused{"resume_recording"}else{"pause_recording"}}));}
   if ui.add_enabled(self.state["recording"]==true,egui::Button::new("结束本组")).clicked(){self.command(json!({"op":"stop_recording"}));}
   if ui.add_enabled(self.state["running"]==true,egui::Button::new("停止采集")).clicked(){self.command(json!({"op":"stop"}));}ui.checkbox(&mut self.freeze,"暂停滚动");
  });
        if self.state["trigger"]["waiting"] == true {
            ui.horizontal(|ui| {
                ui.colored_label(Color32::from_rgb(185, 110, 0), "等待触发");
                if ui.button("软件触发").clicked() {
                    self.command(json!({"op":"fire_trigger"}));
                }
                if ui.button("取消等待").clicked() {
                    self.command(json!({"op":"cancel_trigger"}));
                }
            });
        }
        if self.state["trigger"]["pending_review"] == true {
            ui.horizontal(|ui| {
                ui.label("冲击记录待确认");
                if ui.button("接受本次").clicked() {
                    self.command(json!({"op":"accept_record"}));
                }
                if ui.button("拒绝本次").clicked() {
                    self.command(json!({"op":"reject_record"}));
                }
            });
        }
        ui.horizontal_wrapped(|ui| {
            ui.label("实验");
            ui.add(egui::TextEdit::singleline(&mut self.settings.experiment).desired_width(140.0));
            ui.label("工况");
            ui.add(egui::TextEdit::singleline(&mut self.settings.condition).desired_width(140.0));
            ui.label("组号");
            ui.add(egui::DragValue::new(&mut self.settings.group_number).range(1..=999999));
            ui.label("时长 / s（0 手动）");
            ui.add(egui::DragValue::new(&mut self.settings.duration).range(0.0..=864000.0));
        });
        self.plot_toolbar(ui);
        if let Some(devices) = self.state["collection"].as_array().cloned() {
            for (i, device) in devices.into_iter().enumerate() {
                ui.heading(format!(
                    "设备 {} · {}",
                    i + 1,
                    if i == 0 { "主机" } else { "从机" }
                ));
                let saved = self.live_snapshot.clone();
                self.live_snapshot = device;
                self.refresh_live();
                let w = self.live.clone();
                self.plot_wave(ui, &format!("collection-{i}"), &w);
                self.live_snapshot = saved;
            }
        } else {
            let w = self.live.clone();
            self.plot_wave(ui, "live", &w);
        }
    }
    pub(super) fn channel_settings(&mut self, ui: &mut egui::Ui) {
        ui.add_enabled_ui(self.state["running"] != true, |ui| {
            ui.horizontal(|ui| {
                ui.label("采样率 / Hz");
                ui.add(
                    egui::DragValue::new(&mut self.settings.sample_rate)
                        .range(195.313..=105469.0)
                        .speed(10.0),
                );
            });
            for i in 0..4 {
                ui.push_id(i, |ui| {
                    egui::CollapsingHeader::new(format!(
                        "AI{i} · {}",
                        self.settings.channels[i]["name"].as_str().unwrap_or("")
                    ))
                    .default_open(i == 0)
                    .show(ui, |ui| {
                        let mut enabled = self.settings.active_channels.contains(&i);
                        if ui.checkbox(&mut enabled, "启用输入").changed() {
                            if enabled {
                                self.settings.active_channels.push(i);
                                self.settings.active_channels.sort();
                            } else {
                                self.settings.active_channels.retain(|&n| n != i);
                            }
                        }
                        let ch = &mut self.settings.channels[i];
                        text(ui, ch, "name", "名称");
                        text(ui, ch, "unit", "单位");
                        number(
                            ui,
                            ch,
                            "sensitivity_mv_per_unit",
                            "灵敏度 mV/EU",
                            0.0000001..=1e12,
                            0.1,
                        );
                        number(ui, ch, "engineering_offset", "工程偏移", -1e12..=1e12, 0.01);
                        flag(ui, ch, "inverted", "反向");
                        integer(ui, ch, "point_number", "测点号", 0..=999999);
                        text(ui, ch, "direction", "方向");
                        ui.horizontal(|ui| {
                            ui.selectable_value(&mut self.settings.ranges[i], 10.0, "±10 V");
                            ui.selectable_value(&mut self.settings.ranges[i], 1.0, "±1 V");
                            ui.selectable_value(&mut self.settings.coupling[i], "DC".into(), "DC");
                            ui.selectable_value(&mut self.settings.coupling[i], "AC".into(), "AC");
                        });
                        ui.checkbox(&mut self.settings.iepe[i], "IEPE 2 mA");
                        if self.demo {
                            egui::CollapsingHeader::new("模拟输入").show(ui, |ui| {
                                let s = &mut self.settings.signal_settings["channels"][i];
                                choice(
                                    ui,
                                    s,
                                    "waveform",
                                    "波形",
                                    &[
                                        ("sine", "正弦"),
                                        ("dc", "直流"),
                                        ("square", "方波"),
                                        ("triangle", "三角"),
                                        ("sawtooth", "锯齿"),
                                        ("step", "阶跃"),
                                        ("pulse", "脉冲"),
                                        ("chirp", "扫频"),
                                        ("noise", "噪声"),
                                    ],
                                );
                                for (key, label) in [
                                    ("amplitude_v", "幅值 / V"),
                                    ("offset_v", "偏置 / V"),
                                    ("frequency_hz", "频率 / Hz"),
                                    ("phase_deg", "相位 / °"),
                                    ("noise_rms_v", "噪声 RMS / V"),
                                    ("start_s", "开始 / s"),
                                    ("duration_s", "持续 / s"),
                                    ("end_frequency_hz", "结束频率 / Hz"),
                                ] {
                                    number(
                                        ui,
                                        s,
                                        key,
                                        label,
                                        (if key == "offset_v" { -10.0 } else { 0.0 })..=100000.0,
                                        0.1,
                                    );
                                }
                            });
                        }
                    });
                });
            }
            if ui.button("将 AI0 单位/量程/耦合应用到启用通道").clicked() {
                for i in self.settings.active_channels.clone() {
                    self.settings.channels[i]["unit"] = self.settings.channels[0]["unit"].clone();
                    self.settings.channels[i]["sensitivity_mv_per_unit"] =
                        self.settings.channels[0]["sensitivity_mv_per_unit"].clone();
                    self.settings.ranges[i] = self.settings.ranges[0];
                    self.settings.coupling[i] = self.settings.coupling[0].clone();
                    self.settings.iepe[i] = self.settings.iepe[0];
                }
            }
            egui::CollapsingHeader::new("触发与冲击记录").show(ui, |ui| {
                let t = &mut self.settings.trigger;
                choice(
                    ui,
                    t,
                    "mode",
                    "方式",
                    &[
                        ("immediate", "连续 / 立即"),
                        ("software", "软件触发"),
                        ("analog_rising", "阈值上升沿"),
                        ("analog_falling", "阈值下降沿"),
                        ("external_rising", "外部 TTL 上升沿"),
                    ],
                );
                integer(ui, t, "channel", "触发 AI", 0..=3);
                number(ui, t, "level_v", "阈值 / V", -10.0..=10.0, 0.01);
                number(ui, t, "pretrigger_percent", "预触发 / %", 0.0..=100.0, 1.0);
                flag(ui, t, "retrigger", "自动重触发");
                number(ui, t, "delay_seconds", "重触发延迟 / s", 0.0..=86400.0, 0.1);
                integer(ui, t, "record_count", "记录次数", 1..=100000);
                flag(ui, t, "accept_reject", "每次接受 / 拒绝");
                ui.small("触发记录需设置正的记录时长。");
            });
        });
        ui.add_enabled_ui(self.state["recording"]!=true,|ui|{ui.horizontal(|ui|{
   if ui.button("窗口调零").clicked(){let p=&self.live_snapshot["profile"];if let(Some(raw),Some(active))=(self.live_snapshot["raw"].as_array(),p["config"]["channels"].as_array()){if !raw.is_empty()&&!active.is_empty(){self.last_zero=Some(self.settings.channels.clone());for(col,ch)in active.iter().enumerate(){let v:Vec<f64>=raw.iter().skip(col).step_by(active.len()).filter_map(Value::as_f64).collect();self.settings.channels[ch.as_u64().unwrap()as usize]["zero_volts"]=json!(v.iter().sum::<f64>()/v.len()as f64);}self.command(json!({"op":"set_channels","channels":self.settings.channels}));}}}
   if ui.add_enabled(self.last_zero.is_some(),egui::Button::new("撤销调零")).clicked(){self.settings.channels=self.last_zero.take().unwrap();self.command(json!({"op":"set_channels","channels":self.settings.channels}));}
  });});
    }
    pub(super) fn output_page(&mut self, ui: &mut egui::Ui) {
        ui.label("DT9837C · 单路 AO · ±3 V · 独立更新率 10–96 kS/s");
        let o = &mut self.settings.output;
        choice(
            ui,
            o,
            "waveform",
            "波形",
            &[
                ("dc", "直流"),
                ("sine", "正弦"),
                ("square", "方波"),
                ("triangle", "三角"),
                ("sawtooth", "锯齿"),
                ("sweep_linear", "线性扫频"),
                ("sweep_log", "对数扫频"),
                ("uniform_noise", "均匀噪声"),
                ("gaussian_noise", "高斯噪声"),
                ("pink_noise", "粉红噪声"),
            ],
        );
        ui.columns(2, |cols| {
            for (i, (key, label, low, high, speed)) in [
                ("amplitude_v", "峰值幅度 / V", 0.0, 3.0, 0.01),
                ("offset_v", "偏置 / V", -3.0, 3.0, 0.01),
                ("frequency_hz", "频率 / Hz", 0.0, 48000.0, 1.0),
                ("sample_rate", "更新率 / S/s", 10000.0, 96000.0, 100.0),
                ("end_frequency_hz", "扫频终点 / Hz", 0.0, 48000.0, 1.0),
                ("sweep_seconds", "扫频时长 / s", 0.001, 86400.0, 0.1),
                ("burst_on_seconds", "Burst 输出 / s", 0.0, 86400.0, 0.01),
                ("burst_off_seconds", "Burst 间隔 / s", 0.0, 86400.0, 0.01),
                ("fade_in_seconds", "渐入 / s", 0.0, 86400.0, 0.01),
                ("fade_out_seconds", "渐出 / s", 0.0, 86400.0, 0.01),
                ("bandwidth_hz", "噪声带宽 / Hz", 0.0, 48000.0, 1.0),
            ]
            .into_iter()
            .enumerate()
            {
                number(&mut cols[i % 2], o, key, label, low..=high, speed);
            }
        });
        integer(ui, o, "burst_count", "Burst 次数（0 连续）", 0..=1000000);
        flag(ui, o, "sweep_repeat", "循环扫频");
        flag(ui, o, "repeat_noise", "重复噪声序列");
        ui.checkbox(
            &mut self.settings.linked_output,
            "随采集软件启停输出（重新开始采集生效）",
        );
        ui.horizontal_wrapped(|ui| {
            if ui.button("预览输出波形").clicked() {
                self.command(
                    json!({"op":"preview_output","settings":self.settings.output,"count":24000}),
                );
            }
            if ui.button("开始输出").clicked() {
                self.command(json!({"op":"start_output","settings":self.settings.output}));
            }
            if ui.button("更新参数").clicked() {
                self.command(json!({"op":"update_output","settings":self.settings.output}));
            }
            if ui.button("停止并归零").clicked() {
                self.command(json!({"op":"stop_output"}));
            }
            if ui.button("静音").clicked() {
                self.command(json!({"op":"mute_output","muted":true}));
            }
            if ui.button("取消静音").clicked() {
                self.command(json!({"op":"mute_output","muted":false}));
            }
        });
        ui.label(if self.state["output"]["running"] == true {
            "输出状态：运行中"
        } else {
            "输出状态：已停止"
        });
        let w = self.output_preview.clone();
        self.plot_wave(ui, "output", &w);
    }
    pub(super) fn settings_page(&mut self, ui: &mut egui::Ui) {
        ui.heading("数据与显示");
        ui.label(self.settings.recording_root.display().to_string());
        if ui.button("选择记录根目录").clicked() {
            if let Some(p) = rfd::FileDialog::new().pick_folder() {
                self.settings.recording_root = p;
            }
        }
        ui.horizontal_wrapped(|ui| {
            ui.label("显示窗口 / s");
            ui.add(
                egui::DragValue::new(&mut self.settings.display_seconds)
                    .range(0.001..=60.0)
                    .speed(0.1),
            );
            ui.label("刷新率 / Hz");
            ui.add(egui::DragValue::new(&mut self.settings.refresh_hz).range(1.0..=60.0));
            ui.label("小数位");
            ui.add(egui::DragValue::new(&mut self.settings.decimals).range(0..=9));
        });
        ui.horizontal(|ui| {
            ui.label("图高");
            ui.add(egui::DragValue::new(&mut self.plot_height).range(100.0..=800.0));
            ui.label("线宽");
            ui.add(egui::DragValue::new(&mut self.line_width).range(0.5..=5.0));
            ui.checkbox(&mut self.log_y, "纵轴 log₁₀（仅正值）");
        });
        ui.checkbox(&mut self.settings.auto_connect, "启动时自动连接数采");
        ui.horizontal(|ui| {
            for i in 0..4 {
                ui.checkbox(&mut self.settings.channel_visible[i], format!("显示 AI{i}"));
            }
        });
        ui.horizontal(|ui| {
            if ui.button("保存配置到文件").clicked() {
                if let Some(p) = rfd::FileDialog::new()
                    .set_file_name("labdaq.json")
                    .save_file()
                {
                    match self.settings.save(&p) {
                        Ok(_) => self.notice = "配置已保存".into(),
                        Err(e) => self.error = e,
                    }
                }
            }
            if ui.button("载入配置").clicked() {
                if let Some(p) = rfd::FileDialog::new()
                    .add_filter("JSON", &["json"])
                    .pick_file()
                {
                    match Settings::load(&p) {
                        Ok(s) => {
                            self.settings = s;
                            if self.demo {
                                self.settings.source = "simulation".into();
                            }
                            self.notice = "配置已载入，停止采集后重新开始生效。".into();
                        }
                        Err(e) => self.error = e,
                    }
                }
            }
        });
        ui.separator();
        ui.heading("连接与后台核心");
        ui.label("关闭界面后，记录和输出继续运行；重开后恢复连接。");
        ui.horizontal_wrapped(|ui| {
            if ui.button("连接设备").clicked() {
                self.command(json!({"op":"connect","profile":self.settings.profile()}));
            }
            if ui.button("断开设备").clicked() {
                self.command(json!({"op":"disconnect"}));
            }
            if ui.button("停止并关闭后台核心").clicked() {
                self.command(json!({"op":"shutdown"}));
            }
        });
        ui.separator();
        ui.heading("设备集合");
        if self.demo {
            if ui.button("准备两台模拟设备").clicked() {
                self.settings.devices =
                    vec![json!({"bus":0,"ports":[1]}), json!({"bus":0,"ports":[2]})];
            }
        } else if ui.button("发现原厂数采").clicked() {
            self.command(json!({"op":"list_devices"}));
        }
        for device in self.devices_found.clone() {
            let vendor = device["vendor_name"].as_str().unwrap_or("");
            let selector = if vendor.is_empty() {
                json!({"bus":device["bus"],"ports":device["ports"]})
            } else {
                json!({"vendor_name":vendor})
            };
            let mut selected = self.settings.devices.contains(&selector);
            let label = if vendor.is_empty() {
                format!(
                    "总线 {} · 端口 {} · {}",
                    device["bus"],
                    device["ports"],
                    if device["state"] == "cold" {
                        "冷启动"
                    } else {
                        "运行态"
                    }
                )
            } else {
                format!("{} · {} · {}", vendor, device["model"], device["driver"])
            };
            if ui.checkbox(&mut selected, label).changed() {
                if selected {
                    self.settings.devices.push(selector);
                } else {
                    self.settings.devices.retain(|d| d != &selector);
                }
            }
        }
        ui.label(format!(
            "已选择 {} 台，选择顺序中第一台为主机",
            self.settings.devices.len()
        ));
        if ui
            .add_enabled(
                self.state["running"] != true && (2..=4).contains(&self.settings.devices.len()),
                egui::Button::new("启动集合并记录"),
            )
            .clicked()
        {
            let profiles: Vec<Value> = self
                .settings
                .devices
                .iter()
                .map(|d| {
                    let mut p = self.settings.profile();
                    p["device_selector"] = d.clone();
                    p["linked_output"] = Value::Null;
                    p["trigger"]["mode"] = json!("immediate");
                    p
                })
                .collect();
            self.command(json!({"op":"start_collection","profiles":profiles,"recording_path":self.settings.next_recording()}));
            self.page = Page::Live;
        }
        ui.small("真实多设备必须连接主从同步线；本轮只验证模拟集合。");
        ui.separator();
        ui.heading("每通道近一秒读数");
        for i in 0..4 {
            ui.push_id(i, |ui| {
                ui.horizontal(|ui| {
                    ui.label(format!("AI{i}"));
                    egui::ComboBox::from_id_salt("readout")
                        .selected_text(&self.settings.readout_modes[i])
                        .show_ui(ui, |ui| {
                            for (id, name) in [
                                ("latest", "最新值"),
                                ("first", "首值"),
                                ("mean", "平均"),
                                ("min", "最小"),
                                ("max", "最大"),
                                ("rms", "RMS"),
                                ("std", "标准差"),
                                ("pkpk", "峰峰值"),
                            ] {
                                ui.selectable_value(
                                    &mut self.settings.readout_modes[i],
                                    id.into(),
                                    name,
                                );
                            }
                        });
                    ui.add(
                        egui::DragValue::new(&mut self.settings.readout_decimals[i]).range(0..=9),
                    );
                });
            });
        }
        ui.separator();
        ui.label(format!(
            "LabDAQ {} · Rust 桌面应用",
            env!("CARGO_PKG_VERSION")
        ));
        ui.label(format!("程序数据：{}", user_directory().display()));
    }
}
