//! Headless interaction tests. The worker is an in-memory command recorder:
//! no CoreClient is constructed, no executable is launched and no USB/SDK is
//! opened. Only analysis/filter jobs run on real background worker threads.
use super::*;
use egui::{Event, FullOutput, PointerButton, Pos2, RawInput, Rect, Vec2};
use std::time::Instant;

fn fixture() -> (
    LabDaqApp,
    Receiver<Value>,
    SyncSender<Result<Value, String>>,
) {
    let (tx, commands) = mpsc::sync_channel(128);
    let (events, rx) = mpsc::sync_channel(16);
    let settings = Settings {
        source: "simulation".into(),
        auto_connect: false,
        ..Default::default()
    };
    let state = json!({"running":false,"recording":false,"recording_paused":false,"connected":true,"profile":settings.profile(),"output":{"running":false},"total_scans":0,"recorded_scans":0});
    let app = LabDaqApp {
        qa_run: None,
        data_ui: Default::default(),
        devices_found: vec![],
        settings,
        page: Page::Live,
        worker: CoreWorker {
            tx,
            rx,
            stop: Arc::new(AtomicBool::new(false)),
            display_seconds: Arc::new(AtomicU64::new(8.0f64.to_bits())),
        },
        state,
        first_state: false,
        demo: true,
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
        replay_start: 0.,
        replay_count: 16000,
        comparison_result: Value::Null,
        comparison_paths: vec![],
        comparison_axis: "relative".into(),
        cursor_a: 0.,
        cursor_b: 1.,
        cursors: false,
        freeze: false,
        raw_view: false,
        stats: Value::Null,
        live_snapshot: Value::Null,
        last_zero: None,
        plot_height: 220.,
        line_width: 1.5,
        log_y: false,
        last_status_recording: false,
        config_file: PathBuf::new(),
        edit_notes: String::new(),
        edit_specimen: String::new(),
        job_request: Value::Null,
        last_capture: None,
    };
    (app, commands, events)
}

fn context() -> egui::Context {
    let ctx = egui::Context::default();
    install_fonts(&ctx);
    ctx.set_pixels_per_point(1.);
    ctx.style_mut(|style| style.animation_time = 0.0);
    ctx
}
fn frame(app: &mut LabDaqApp, ctx: &egui::Context, page: Page, events: Vec<Event>) -> FullOutput {
    app.page = page;
    ctx.run(
        RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(1800., 5000.))),
            events,
            ..Default::default()
        },
        |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| match page {
                Page::Live => app.live_page(ui),
                Page::Library => app.library_page(ui),
                Page::Compare => app.comparison_page(ui),
                Page::Analysis => app.analysis_page(ui),
                Page::Output => app.output_page(ui),
                Page::Settings => app.settings_page(ui),
            });
        },
    )
}
fn text_shapes(shape: &egui::epaint::Shape, out: &mut Vec<(String, Pos2)>) {
    match shape {
        egui::epaint::Shape::Text(t) => out.push((
            t.galley.job.text.clone(),
            t.pos + t.galley.rect.center().to_vec2(),
        )),
        egui::epaint::Shape::Vec(shapes) => {
            for s in shapes {
                text_shapes(s, out)
            }
        }
        _ => {}
    }
}
fn texts(output: &FullOutput) -> Vec<(String, Pos2)> {
    let mut result = vec![];
    for s in &output.shapes {
        text_shapes(&s.shape, &mut result);
    }
    result
}
fn click(app: &mut LabDaqApp, ctx: &egui::Context, page: Page, label: &str) {
    let output = frame(app, ctx, page, vec![]);
    let all = texts(&output);
    let pos = all
        .iter()
        .find(|(t, _)| t == label)
        .map(|(_, p)| *p)
        .unwrap_or_else(|| {
            panic!(
                "未呈现控件 {label:?}；可见文字：{:?}",
                all.iter().map(|(t, _)| t).collect::<Vec<_>>()
            )
        });
    frame(
        app,
        ctx,
        page,
        vec![
            Event::PointerMoved(pos),
            Event::PointerButton {
                pos,
                button: PointerButton::Primary,
                pressed: true,
                modifiers: egui::Modifiers::NONE,
            },
        ],
    );
    frame(
        app,
        ctx,
        page,
        vec![Event::PointerButton {
            pos,
            button: PointerButton::Primary,
            pressed: false,
            modifiers: egui::Modifiers::NONE,
        }],
    );
}
fn finish_job(app: &mut LabDaqApp) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while app.job.is_some() {
        assert!(Instant::now() < deadline, "后台作业超时");
        app.receive();
        std::thread::sleep(Duration::from_millis(2));
    }
}
fn tone(count: usize, amplitude: f64) -> WaveView {
    let sample_rate = 1024.;
    WaveView {
        times: (0..count).map(|i| i as f64 / sample_rate).collect(),
        samples: vec![
            (0..count)
                .map(|i| amplitude * (std::f64::consts::TAU * 64. * i as f64 / sample_rate).sin())
                .collect(),
        ],
        channels: vec![
            json!({"index":2,"name":"AI2 测试","unit":"V","sensitivity_mv_per_unit":1000.,"zero_volts":0.,"inverted":false,"engineering_offset":0.,"point_number":17,"direction":"X+"}),
        ],
        sample_rate,
        gaps: vec![],
    }
}
fn analysis_fixture() -> (
    LabDaqApp,
    Receiver<Value>,
    SyncSender<Result<Value, String>>,
) {
    let (mut app, c, e) = fixture();
    app.live = tone(4096, 2.);
    app.settings.analysis = json!({"nfft":256,"window":"rectangular","mode":"amplitude","overlap":0.5,"_ui_source":"live"});
    (app, c, e)
}

#[test]
fn all_pages_render_empty_and_populated_without_connecting() {
    let (mut app, commands, _events) = fixture();
    let ctx = context();
    for populated in [false, true] {
        if populated {
            app.live = tone(1024, 1.);
            app.replay = app.live.clone();
            app.output_preview = app.live.clone();
        }
        for page in [
            Page::Live,
            Page::Library,
            Page::Compare,
            Page::Analysis,
            Page::Output,
            Page::Settings,
        ] {
            let output = frame(&mut app, &ctx, page, vec![]);
            assert!(!output.shapes.is_empty(), "{page:?}");
            assert!(!texts(&output).is_empty(), "{page:?}");
            assert!(app.error.is_empty(), "{page:?}: {}", app.error);
        }
    }
    assert!(commands.try_recv().is_err());
    assert!(app.job.is_none());
}

#[test]
fn live_buttons_emit_correct_simulated_lifecycle_commands() {
    let (mut app, commands, _events) = fixture();
    let ctx = context();
    click(&mut app, &ctx, Page::Live, "开始预览");
    let request = commands.try_recv().unwrap();
    assert_eq!(request["op"], "start");
    assert_eq!(request["profile"]["source"], "simulation");
    assert!(request["recording_path"].is_null());
    app.state["running"] = json!(true);
    click(&mut app, &ctx, Page::Live, "● 开始记录");
    assert_eq!(commands.try_recv().unwrap()["op"], "start_recording");
    app.state["recording"] = json!(true);
    click(&mut app, &ctx, Page::Live, "暂停记录");
    assert_eq!(commands.try_recv().unwrap()["op"], "pause_recording");
    app.state["recording_paused"] = json!(true);
    click(&mut app, &ctx, Page::Live, "继续记录");
    assert_eq!(commands.try_recv().unwrap()["op"], "resume_recording");
    click(&mut app, &ctx, Page::Live, "结束本组");
    assert_eq!(commands.try_recv().unwrap()["op"], "stop_recording");
    click(&mut app, &ctx, Page::Live, "停止采集");
    assert_eq!(commands.try_recv().unwrap()["op"], "stop");
    assert!(commands.try_recv().is_err());
}

#[test]
fn trigger_accept_reject_and_output_controls_only_emit_commands() {
    let (mut app, commands, _events) = fixture();
    let ctx = context();
    app.state["trigger"] = json!({"waiting":true,"pending_review":true});
    for (label, op) in [
        ("软件触发", "fire_trigger"),
        ("取消等待", "cancel_trigger"),
        ("接受本次", "accept_record"),
        ("拒绝本次", "reject_record"),
    ] {
        click(&mut app, &ctx, Page::Live, label);
        assert_eq!(commands.try_recv().unwrap()["op"], op);
    }
    for (label, op) in [
        ("预览输出波形", "preview_output"),
        ("开始输出", "start_output"),
        ("更新参数", "update_output"),
        ("停止并归零", "stop_output"),
        ("静音", "mute_output"),
        ("取消静音", "mute_output"),
    ] {
        click(&mut app, &ctx, Page::Output, label);
        let request = commands.try_recv().unwrap();
        assert_eq!(request["op"], op);
        if label == "静音" {
            assert_eq!(request["muted"], true);
        }
        if label == "取消静音" {
            assert_eq!(request["muted"], false);
        }
    }
    assert!(commands.try_recv().is_err());
}

#[test]
fn analysis_button_runs_real_rust_job_and_preserves_request_snapshot() {
    let (mut app, commands, _events) = analysis_fixture();
    let ctx = context();
    let before = app.live.samples.clone();
    click(&mut app, &ctx, Page::Analysis, "计算 / 重新处理");
    assert_eq!(app.job_request["task"], "analysis");
    assert_eq!(app.job_request["source"]["kind"], "live");
    app.settings.analysis["mode"] = json!("psd");
    app.settings.analysis["display"] = json!("db");
    app.live = tone(4096, 0.25);
    finish_job(&mut app);
    assert!(app.error.is_empty(), "{}", app.error);
    assert_eq!(app.analysis_result["mode"], "amplitude");
    assert_eq!(app.analysis_result["display"], "magnitude");
    assert_eq!(app.analysis_result["records"], 31);
    assert!(
        (app.analysis_result["traces"][0]["values"][16]
            .as_f64()
            .unwrap()
            - 2.)
            .abs()
            < 1e-10
    );
    assert_eq!(app.analysis_result["traces"][0]["point_number"], 17);
    assert_eq!(app.analysis_result["traces"][0]["direction"], "X+");
    assert_eq!(app.job_request["samples"], json!(before));
    assert!(commands.try_recv().is_err());
}

#[test]
fn replay_gap_and_cursor_selection_reach_job_as_only_real_samples() {
    let (mut app, _commands, _events) = analysis_fixture();
    let ctx = context();
    let mut w = tone(1024, 1.);
    w.samples[0][512..].copy_from_slice(&tone(512, 3.).samples[0]);
    for t in &mut w.times[512..] {
        *t += 20.;
    }
    w.gaps = vec![512];
    app.replay = w;
    app.open_path = "simulated-record".into();
    app.cursor_a = 20.5;
    app.cursor_b = 20.75;
    app.settings.analysis["_ui_source"] = json!("replay");
    app.settings.analysis["_ui_segment"] = json!(1);
    app.settings.analysis["_ui_cursors"] = json!(true);
    click(&mut app, &ctx, Page::Analysis, "计算 / 重新处理");
    finish_job(&mut app);
    assert!(app.error.is_empty(), "{}", app.error);
    assert_eq!(app.job_request["samples"][0].as_array().unwrap().len(), 256);
    assert_eq!(app.job_request["input"]["times"][0], 20.5);
    assert_eq!(app.job_request["input"]["times"][255], 20.5 + 255. / 1024.);
    assert_eq!(app.analysis_result["records"], 1);
    assert!(
        (app.analysis_result["traces"][0]["values"][16]
            .as_f64()
            .unwrap()
            - 3.)
            .abs()
            < 1e-10
    );
}

#[test]
fn filter_button_preserves_sample_times_units_and_input_immutability() {
    let (mut app, commands, _events) = analysis_fixture();
    let ctx = context();
    app.live.samples[0].fill(5.);
    app.live.channels[0]["unit"] = json!("N");
    for t in &mut app.live.times {
        *t += 30.;
    }
    let expected = app.live.clone();
    app.settings.analysis["_ui_panel"] = json!("filter");
    app.settings.filter = json!({"family":"butterworth","kind":"lowpass","order":4,"cutoff_hz":100.,"zero_phase":true});
    click(&mut app, &ctx, Page::Analysis, "应用滤波");
    app.live = tone(128, 0.1);
    finish_job(&mut app);
    assert!(app.error.is_empty(), "{}", app.error);
    assert_eq!(app.filtered.times, expected.times);
    assert_eq!(app.filtered.channels[0]["unit"], "N");
    assert_eq!(app.filtered.samples[0].len(), 4096);
    assert!(
        app.filtered.samples[0]
            .iter()
            .all(|v| (v - 5.).abs() < 1e-10)
    );
    assert_eq!(app.job_request["samples"], json!(expected.samples));
    assert!(commands.try_recv().is_err());
    app.raw_view = true;
    frame(&mut app, &ctx, Page::Analysis, vec![]);
    assert!(app.raw_view, "绘制滤后单位不得更改全局原始显示设置");
}

#[test]
fn filter_response_can_be_designed_without_acquisition() {
    let (mut app, commands, _events) = fixture();
    let ctx = context();
    app.settings.sample_rate = 2000.;
    app.settings.analysis["_ui_panel"] = json!("filter");
    click(&mut app, &ctx, Page::Analysis, "仅查看滤波器响应");
    finish_job(&mut app);
    assert!(app.error.is_empty(), "{}", app.error);
    assert_eq!(app.filter_result["response_sample_rate"], 2000.);
    assert_eq!(app.filter_result["stable"], true);
    assert!(app.filtered.samples.is_empty());
    assert!(commands.try_recv().is_err());
}

#[test]
fn analysis_errors_do_not_replace_previous_valid_results() {
    let (mut app, _commands, _events) = analysis_fixture();
    let ctx = context();
    app.analysis_result = json!({"saved":"previous"});
    app.settings.analysis["nfft"] = json!(262144);
    click(&mut app, &ctx, Page::Analysis, "计算 / 重新处理");
    finish_job(&mut app);
    assert!(app.error.contains("FFT"), "{}", app.error);
    assert_eq!(app.analysis_result["saved"], "previous");
    app.error.clear();
    app.live = WaveView::default();
    let request = app.job_request.clone();
    click(&mut app, &ctx, Page::Analysis, "计算 / 重新处理");
    assert!(app.job.is_none());
    assert_eq!(app.job_request, request);
}

#[test]
fn parameter_auto_reprocess_changes_units_without_modifying_input() {
    let (mut app, _commands, _events) = analysis_fixture();
    let ctx = context();
    let expected = app.live.samples.clone();
    app.settings.analysis["_ui_auto_reprocess"] = json!(true);
    frame(&mut app, &ctx, Page::Analysis, vec![]);
    finish_job(&mut app);
    assert!(app.error.is_empty(), "{}", app.error);
    app.settings.analysis["integration"] = json!(1); // emulate a committed settings edit before the next GUI frame
    // The automatic trigger observes edits made by widgets in-frame; use a
    // real checkbox toggle so the headless path exercises the event handling.
    click(&mut app, &ctx, Page::Analysis, "每块减去均值");
    finish_job(&mut app);
    assert!(app.error.is_empty(), "{}", app.error);
    assert_eq!(app.analysis_result["traces"][0]["quantity_unit"], "V·s");
    let expected_amplitude = 2. / (std::f64::consts::TAU * 64.);
    assert!(
        (app.analysis_result["traces"][0]["values"][16]
            .as_f64()
            .unwrap()
            - expected_amplitude)
            .abs()
            < 1e-10
    );
    assert_eq!(app.live.samples, expected);
}

#[test]
fn every_analysis_mode_and_filter_family_exposes_its_controls() {
    let (mut app, _commands, _events) = analysis_fixture();
    app.live
        .samples
        .push(app.live.samples[0].iter().map(|v| -2.5 * v).collect());
    app.live
        .channels
        .push(json!({"index":3,"name":"AI3","unit":"V"}));
    let ctx = context();
    for mode in [
        "amplitude",
        "autospectrum",
        "psd",
        "h1",
        "h2",
        "h3",
        "cross_spectrum",
        "cross_psd",
        "coherence",
        "cop",
    ] {
        app.settings.analysis["mode"] = json!(mode);
        let out = frame(&mut app, &ctx, Page::Analysis, vec![]);
        let labels = texts(&out);
        assert!(labels.iter().any(|(t, _)| t == "计算 / 重新处理"));
        if [
            "h1",
            "h2",
            "h3",
            "cross_spectrum",
            "cross_psd",
            "coherence",
            "cop",
        ]
        .contains(&mode)
        {
            assert!(labels.iter().any(|(t, _)| t.starts_with("参考：")));
            assert_eq!(app.settings.analysis["average"], "rms");
        }
    }
    app.settings.analysis["_ui_panel"] = json!("filter");
    for family in ["butterworth", "bessel", "chebyshev"] {
        for kind in ["lowpass", "highpass", "bandpass", "bandstop"] {
            app.settings.filter["family"] = json!(family);
            app.settings.filter["kind"] = json!(kind);
            let out = frame(&mut app, &ctx, Page::Analysis, vec![]);
            let labels = texts(&out);
            assert!(labels.iter().any(|(t, _)| t == "应用滤波"));
            if kind.starts_with("band") {
                assert!(labels.iter().any(|(t, _)| t == "上截止 Hz"));
            }
            if family == "chebyshev" {
                assert!(labels.iter().any(|(t, _)| t == "通带纹波 dB"));
            }
        }
    }
}

#[test]
fn received_preview_and_recording_status_update_without_worker_process() {
    let (mut app, commands, events) = fixture();
    events
        .send(Ok(json!({"samples":[0.,1.,-1.],"sample_rate":1000.})))
        .unwrap();
    app.receive();
    assert_eq!(app.output_preview.samples, vec![vec![0., 1., -1.]]);
    assert_eq!(app.output_preview.times, [0., 0.001, 0.002]);
    app.last_status_recording = true;
    let before = app.settings.group_number;
    events
        .send(Ok(
            json!({"running":true,"recording":false,"termination_reason":"completed","profile":{"recording_options":{"group_number":before+1}}}),
        ))
        .unwrap();
    app.receive();
    assert_eq!(app.settings.group_number, before + 1);
    app.settings.group_number = 1;
    app.last_status_recording = true;
    events.send(Ok(json!({"running":false,"recording":false,"termination_reason":"error","profile":{"recording_options":{"group_number":1}}}))).unwrap();
    app.receive();
    assert_eq!(app.settings.group_number, 1);
    assert!(commands.try_recv().is_err());
}

#[test]
fn each_readout_mode_uses_last_second_and_population_statistics() {
    let samples = [-1000., 1000., 1., 2., 3., 4.];
    for (mode, expected) in [
        ("latest", 4.),
        ("first", 1.),
        ("mean", 2.5),
        ("min", 1.),
        ("max", 4.),
        ("rms", 7.5f64.sqrt()),
        ("std", 1.25f64.sqrt()),
        ("pkpk", 3.),
    ] {
        assert!(
            (readout(&samples, 4., mode).unwrap() - expected).abs() < 1e-12,
            "{mode}"
        );
    }
    assert_eq!(readout(&[], 1000., "mean"), None);
    assert_eq!(readout(&[2., 4.], 100., "mean"), Some(3.));
    assert_eq!(readout(&samples, 1.6, "first"), Some(3.));
    let (mut app, _commands, _events) = fixture();
    let ctx = context();
    app.live = WaveView {
        times: (0..6).map(|i| i as f64 / 4.).collect(),
        samples: vec![samples.to_vec()],
        channels: vec![json!({"index":2,"name":"AI2","unit":"V"})],
        sample_rate: 4.,
        gaps: vec![],
    };
    app.settings.readout_modes[2] = "mean".into();
    app.settings.readout_decimals[2] = 3;
    let out = frame(&mut app, &ctx, Page::Live, vec![]);
    assert!(
        texts(&out).iter().any(|(text, _)| text == "2.500"),
        "物理AI2应使用独立读数模式和小数位"
    );
}

#[test]
fn legacy_display_json_restores_modes_precision_window_and_axis() {
    // The parent task collects .task-tmp after the complete simulation suite.
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../.task-tmp")
        .join(format!("gui-settings-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let mut old = serde_json::to_value(Settings::default()).unwrap();
    old.as_object_mut().unwrap().remove("source");
    old["scenario"] = json!("usb");
    old["display"] = json!({"live_time_axis":"absolute","live_seconds":60.0,"value_modes":["last","std_dev","peak_to_peak","rms"],"value_decimals":6});
    let path = root.join("legacy.json");
    std::fs::write(&path, serde_json::to_vec_pretty(&old).unwrap()).unwrap();
    let s = Settings::load(&path).unwrap();
    assert_eq!(s.source, "vendor");
    assert_eq!(s.axis, "absolute");
    assert_eq!(s.display_seconds, 60.);
    assert_eq!(s.readout_modes, ["latest", "std", "pkpk", "rms"]);
    assert_eq!(s.readout_decimals, [6; 4]);
    assert_eq!(s.decimals, 6);
    old["display"]
        .as_object_mut()
        .unwrap()
        .remove("value_modes");
    old["display"]["value_mode"] = json!("rms");
    old["display"]["live_seconds"] = json!(0.001);
    std::fs::write(&path, serde_json::to_vec_pretty(&old).unwrap()).unwrap();
    let s = Settings::load(&path).unwrap();
    assert_eq!(s.readout_modes, ["rms", "rms", "rms", "rms"]);
    assert_eq!(s.display_seconds, 0.001);
    let saved = root.join("roundtrip.json");
    s.save(&saved).unwrap();
    let restored = Settings::load(&saved).unwrap();
    assert_eq!(restored.readout_modes, s.readout_modes);
    assert_eq!(restored.readout_decimals, s.readout_decimals);
    assert_eq!(restored.axis, s.axis);
}

#[test]
fn cancelled_analysis_and_filter_results_never_replace_current_views() {
    for name in ["频域分析", "数字滤波"] {
        let (mut app, _commands, _events) = fixture();
        app.analysis_result = json!({"keep":"analysis"});
        app.filter_result = json!({"keep":"filter"});
        app.filtered = tone(16, 1.);
        let original = app.filtered.samples.clone();
        let (tx, rx) = mpsc::channel();
        tx.send(Ok(json!({"samples":[vec![0.;16]],"new":true})))
            .unwrap();
        app.job = Some(Job {
            name: name.into(),
            rx,
            cancel: Arc::new(AtomicBool::new(true)),
            progress: Arc::new(std::sync::Mutex::new((0, 0))),
        });
        app.receive();
        assert!(app.job.is_none());
        assert_eq!(app.analysis_result["keep"], "analysis");
        assert_eq!(app.filter_result["keep"], "filter");
        assert_eq!(app.filtered.samples, original);
        assert!(app.error.contains("取消"));
    }
}

#[test]
fn cancelling_a_real_analysis_job_discards_its_computed_result() {
    let (mut app, commands, _events) = fixture();
    app.analysis_result = json!({"keep":true});
    let w = tone(8192, 1.);
    app.start_job(
        "频域分析",
        json!({"task":"analysis","samples":w.samples,"sample_rate":1024.,"nfft":4096}),
    );
    app.job
        .as_ref()
        .unwrap()
        .cancel
        .store(true, Ordering::Release);
    finish_job(&mut app);
    assert_eq!(app.analysis_result["keep"], true);
    assert!(app.error.contains("取消"));
    assert!(commands.try_recv().is_err());
}

#[test]
fn analysis_menus_select_dual_mode_and_independent_impact_windows() {
    let (mut app, commands, _events) = analysis_fixture();
    app.live.samples.push(app.live.samples[0].clone());
    app.live
        .channels
        .push(json!({"index":3,"name":"响应","unit":"V"}));
    let ctx = context();
    click(&mut app, &ctx, Page::Analysis, "分析函数：幅度谱");
    let out = frame(&mut app, &ctx, Page::Analysis, vec![]);
    let labels = texts(&out);
    for name in [
        "幅度谱",
        "自功率谱",
        "功率谱密度 PSD",
        "频响 H1",
        "频响 H2",
        "频响 H3",
        "互功率谱",
        "互功率谱密度",
        "相干函数",
        "相干输出功率密度",
    ] {
        assert!(labels.iter().any(|(t, _)| t == name), "缺少菜单项 {name}");
    }
    click(&mut app, &ctx, Page::Analysis, "频响 H1");
    assert_eq!(app.settings.analysis["mode"], "h1");
    click(&mut app, &ctx, Page::Analysis, "双通道与冲击记录设置");
    click(&mut app, &ctx, Page::Analysis, "独立参考 / 响应窗");
    assert_eq!(app.settings.analysis["reference_window"]["name"], "hann");
    click(&mut app, &ctx, Page::Analysis, "参考窗：Hann");
    click(&mut app, &ctx, Page::Analysis, "Force 力窗");
    click(&mut app, &ctx, Page::Analysis, "响应窗：Hann");
    click(&mut app, &ctx, Page::Analysis, "指数响应窗");
    assert_eq!(app.settings.analysis["reference_window"]["name"], "force");
    assert_eq!(
        app.settings.analysis["response_window"]["name"],
        "exponential"
    );
    let out = frame(&mut app, &ctx, Page::Analysis, vec![]);
    let labels = texts(&out);
    assert!(labels.iter().any(|(t, _)| t == "保留记录比例"));
    assert!(labels.iter().any(|(t, _)| t == "末端权重"));
    assert!(commands.try_recv().is_err());
}
