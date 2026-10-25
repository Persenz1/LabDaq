//! Real process IPC and full data/analysis workflow using synthetic input only.
use labdaq_desktop::{client::CoreClient, model::Settings};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
struct Session {
    child: Child,
    client: CoreClient,
    dir: PathBuf,
}
impl Session {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let dir = root
            .join(".task-tmp")
            .join(format!("rust-workflow-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let exe = root.join("target/debug/labdaq-core.exe");
        assert!(
            exe.is_file(),
            "Build core first with cargo build -p labdaq-core"
        );
        let endpoint = dir.join("core.json");
        let mut command = Command::new(exe);
        command
            .args([endpoint.to_str().unwrap(), "0", "integration-test-token"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let child = command.spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let client = loop {
            if let Ok(c) = CoreClient::from_endpoint(&endpoint) {
                if c.request(json!({"op":"poll"})).is_ok() {
                    break c;
                }
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(20));
        };
        Self { child, client, dir }
    }
    fn call(&self, v: Value) -> Value {
        self.client.request(v).unwrap()
    }
    fn until(&self, predicate: impl Fn(&Value) -> bool) -> Value {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let s = self.call(json!({"op":"poll","seconds":1.0}));
            if predicate(&s) {
                return s;
            }
            assert!(Instant::now() < deadline, "Timed out: {s}");
            std::thread::sleep(Duration::from_millis(15));
        }
    }
    fn settings(&self) -> Settings {
        let mut s = Settings::default();
        s.source = "simulation".into();
        s.sample_rate = 2000.;
        s.recording_root = self.dir.clone();
        s
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.client.request(json!({"op":"shutdown"}));
        let _ = self.child.wait();
    }
}
#[test]
fn recording_pause_reconnect_analysis_exports_and_legacy_config() {
    let s = Session::new();
    let cfg = s.settings();
    s.call(json!({"op":"start","profile":cfg.profile(),"recording_path":null}));
    s.until(|v| v["total_scans"].as_u64().unwrap() > 200);
    let path = s.dir.join("recording");
    s.call(json!({"op":"start_recording","path":path,"channels":cfg.channels,"experiment":"中文实验","recording_options":{"condition":"暂停","group_number":1}}));
    s.until(|v| v["recorded_scans"].as_u64().unwrap() > 1200);
    let paused = s.call(json!({"op":"pause_recording"}));
    let before = paused["recorded_scans"].as_u64().unwrap();
    let scans = paused["total_scans"].as_u64().unwrap();
    s.until(|v| v["total_scans"].as_u64().unwrap() > scans + 300);
    assert_eq!(s.call(json!({"op":"poll"}))["recorded_scans"], before);
    let reconnected = CoreClient::from_endpoint(&s.client.endpoint).unwrap();
    assert_eq!(
        reconnected.request(json!({"op":"poll"})).unwrap()["recording_paused"],
        true
    );
    s.call(json!({"op":"resume_recording"}));
    s.until(|v| v["recorded_scans"].as_u64().unwrap() > before + 1000);
    s.call(json!({"op":"stop_recording"}));
    let read = labdaq_data::execute_json(&json!({"op":"read","path":path,"count":200000})).unwrap();
    assert!(!read["gaps"].as_array().unwrap().is_empty());
    let raw =
        labdaq_data::execute_json(&json!({"op":"read","path":path,"count":1024,"values":"raw"}))
            .unwrap();
    let result=labdaq_analysis::analyze_json(&json!({"samples":raw["samples"],"sample_rate":raw["sample_rate"],"nfft":1024,"window":"hann","mode":"psd"})).unwrap();
    assert_eq!(result["frequencies"].as_array().unwrap().len(), 513);
    let stats = labdaq_data::execute_json(&json!({"op":"stats","path":path})).unwrap();
    assert!(stats["statistics"].is_array());
    let csv = s.dir.join("export.csv");
    labdaq_data::execute_json(&json!({"op":"export","path":path,"output":csv,"values":"both"}))
        .unwrap();
    assert!(std::fs::metadata(csv).unwrap().len() > 1000);
    let file = s.dir.join("config.json");
    cfg.save(&file).unwrap();
    let loaded = Settings::load(&file).unwrap();
    assert_eq!(loaded.profile(), cfg.profile());
}
#[test]
fn timed_recording_finishes_with_no_client_and_core_keeps_preview() {
    let s = Session::new();
    let mut cfg = s.settings();
    cfg.duration = 0.12;
    let path = s.dir.join("timed");
    s.call(json!({"op":"start","profile":cfg.profile(),"recording_path":path}));
    std::thread::sleep(Duration::from_millis(350));
    let state = s.call(json!({"op":"poll"}));
    assert_eq!(state["recording"], false);
    assert_eq!(state["running"], true);
    let info = labdaq_data::execute_json(&json!({"op":"open","path":path})).unwrap();
    assert_eq!(info["sample_count"], 240);
}
#[test]
fn software_pretrigger_review_and_simulated_ao_are_independent() {
    let s = Session::new();
    let mut cfg = s.settings();
    cfg.duration = 0.1;
    cfg.trigger = json!({"mode":"software","channel":0,"level_v":0.0,"pretrigger_percent":25.0,"retrigger":false,"delay_seconds":0.0,"record_count":1,"accept_reject":true});
    let path = s.dir.join("trigger");
    s.call(json!({"op":"start","profile":cfg.profile(),"recording_path":path}));
    s.until(|v| v["total_scans"].as_u64().unwrap() > 100);
    s.call(json!({"op":"fire_trigger"}));
    s.until(|v| v["trigger"]["pending_review"] == true);
    s.call(json!({"op":"accept_record"}));
    s.call(json!({"op":"start_output","settings":cfg.output}));
    assert_eq!(s.call(json!({"op":"poll"}))["output"]["running"], true);
    s.call(json!({"op":"stop"}));
    assert_eq!(s.call(json!({"op":"poll"}))["output"]["running"], true);
    s.call(json!({"op":"stop_output"}));
    assert_eq!(s.call(json!({"op":"poll"}))["output"]["running"], false);
}
