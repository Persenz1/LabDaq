//! End-to-end tests of the independent core. These only select software simulation.
use serde_json::{Value, json};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

struct Core {
    child: Child,
    root: PathBuf,
    port: u16,
}
impl Core {
    fn launch(name: &str) -> Self {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../.task-tmp/core-ipc")
            .join(format!(
                "{name}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
        fs::create_dir_all(&root).unwrap();
        let endpoint = root.join("endpoint.json");
        let mut command = Command::new(env!("CARGO_BIN_EXE_labdaq-core"));
        command
            .args([endpoint.to_str().unwrap(), "0", "simulation-test-token"])
            .current_dir(&root);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().unwrap();
        let start = Instant::now();
        while !endpoint.exists() {
            assert!(
                child.try_wait().unwrap().is_none(),
                "core exited before readiness"
            );
            assert!(start.elapsed() < Duration::from_secs(5));
            thread::sleep(Duration::from_millis(10));
        }
        let endpoint: Value = serde_json::from_slice(&fs::read(endpoint).unwrap()).unwrap();
        Self {
            child,
            root,
            port: endpoint["port"].as_u64().unwrap() as u16,
        }
    }
    fn raw(&self, request: Value, receive: bool) -> Option<Value> {
        let mut socket = TcpStream::connect(("127.0.0.1", self.port)).unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        serde_json::to_writer(&mut socket, &request).unwrap();
        socket.write_all(b"\n").unwrap();
        if !receive {
            return None;
        }
        let mut line = String::new();
        BufReader::new(socket).read_line(&mut line).unwrap();
        Some(serde_json::from_str(&line).unwrap())
    }
    fn call(&self, mut request: Value) -> Value {
        request["token"] = json!("simulation-test-token");
        let answer = self.raw(request, true).unwrap();
        assert_eq!(answer["ok"], true, "{answer}");
        answer["state"].clone()
    }
    fn until(&self, predicate: impl Fn(&Value) -> bool) -> Value {
        let start = Instant::now();
        loop {
            let state = self.call(json!({"op":"poll","seconds":0.1}));
            if predicate(&state) {
                return state;
            }
            assert!(
                start.elapsed() < Duration::from_secs(5),
                "state did not converge: {state}"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }
    fn shutdown(&mut self) {
        self.call(json!({"op":"stop"}));
        self.call(json!({"op":"shutdown"}));
        let start = Instant::now();
        while self.child.try_wait().unwrap().is_none() {
            assert!(start.elapsed() < Duration::from_secs(3));
            thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Core {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn profile(root: &Path) -> Value {
    json!({"source":"simulation","config":{"sample_rate":1000.,"channels":[0,2],"ranges":[10.,10.,10.,10.],"coupling":["DC","DC","DC","DC"],"iepe":[false,false,false,false]},"actual_rate":1000.,
        "channels":(0..4).map(|i|json!({"index":i,"name":format!("AI{i}"),"unit":"V","sensitivity_mv_per_unit":1000.,"zero_volts":0.,"inverted":false})).collect::<Vec<_>>(),
        "signal_settings":{"seed":3,"channels":(0..4).map(|_|json!({"waveform":"sine","amplitude_v":1.,"offset_v":0.,"frequency_hz":10.,"phase_deg":0.,"noise_rms_v":0.,"drift_v_per_s":0.,"duty_cycle":0.5,"start_s":0.,"duration_s":1.,"end_frequency_hz":20.})).collect::<Vec<_>>()},
        "experiment":"IPC simulation","recording_root":root,"recording_options":{"duration_seconds":0.08}})
}

#[test]
fn detached_client_recording_pause_trigger_output_and_collection() {
    let mut core = Core::launch("workflow");
    let bad = core
        .raw(json!({"op":"poll","token":"wrong"}), true)
        .unwrap();
    assert_eq!(bad["ok"], false);
    let waveform=core.call(json!({"op":"preview_output","settings":{"waveform":"sweep_log","amplitude_v":0.1},"count":2048}));
    assert_eq!(waveform["samples"].as_array().unwrap().len(), 2048);
    let p = profile(&core.root);
    let first = core.root.join("detached");
    core.raw(
        json!({"op":"start","token":"simulation-test-token","profile":p,"recording_path":first}),
        false,
    );
    // This deliberately does not poll while the finite recording completes.
    thread::sleep(Duration::from_millis(200));
    let state = core.call(json!({"op":"poll","seconds":0.2}));
    assert_eq!(state["recording"], false);
    assert_eq!(state["running"], true);
    assert_eq!(
        core.call(json!({"op":"shutdown_if_idle"}))["shutdown"],
        false
    );
    assert!(core.child.try_wait().unwrap().is_none());
    let meta: Value =
        serde_json::from_slice(&fs::read(first.join("metadata.json")).unwrap()).unwrap();
    assert_eq!(meta["finalized"], true);
    assert_eq!(meta["sample_count"], 80);
    assert_eq!(
        fs::metadata(first.join("raw.f64")).unwrap().len(),
        80 * 2 * 8
    );
    let second = core.root.join("paused");
    core.call(json!({"op":"start_recording","path":second,"channels":p["channels"],"experiment":"pause","recording_options":{}}));
    core.until(|s| s["recorded_scans"].as_u64().unwrap() > 0);
    let paused = core.call(json!({"op":"pause_recording"}));
    thread::sleep(Duration::from_millis(60));
    let later = core.call(json!({"op":"poll"}));
    assert_eq!(paused["recorded_scans"], later["recorded_scans"]);
    assert!(later["total_scans"].as_u64() > paused["total_scans"].as_u64());
    core.call(json!({"op":"resume_recording"}));
    core.until(|s| s["recorded_scans"].as_u64() > paused["recorded_scans"].as_u64());
    core.call(json!({"op":"stop_recording"}));
    let meta: Value =
        serde_json::from_slice(&fs::read(second.join("metadata.json")).unwrap()).unwrap();
    assert_eq!(meta["recording_segments"].as_array().unwrap().len(), 2);
    core.call(
        json!({"op":"start_output","settings":{"waveform":"pink_noise","duration_seconds":0.03}}),
    );
    core.until(|s| s["output"]["completed"] == true);
    core.call(json!({"op":"stop"}));
    let mut trigger_profile = p.clone();
    trigger_profile["trigger"] =
        json!({"mode":"software","pretrigger_percent":25.,"accept_reject":true});
    let third = core.root.join("trigger");
    core.call(json!({"op":"start","profile":trigger_profile,"recording_path":third}));
    core.until(|s| s["total_scans"].as_u64().unwrap() >= 30);
    core.call(json!({"op":"fire_trigger"}));
    core.until(|s| s["trigger"]["pending_review"] == true);
    core.call(json!({"op":"accept_record"}));
    let meta: Value =
        serde_json::from_slice(&fs::read(third.join("metadata.json")).unwrap()).unwrap();
    assert_eq!(meta["trigger"]["file_scan"], 20);
    assert_eq!(meta["acceptance"], "accepted");
    core.call(json!({"op":"stop"}));
    core.call(json!({"op":"disconnect"}));
    let collection = core.root.join("collection");
    core.call(
        json!({"op":"start_collection","profiles":[p.clone(),p],"recording_path":collection}),
    );
    let state = core.until(|s| s["recording"] == false);
    assert_eq!(state["collection"].as_array().unwrap().len(), 2);
    assert_eq!(
        state["collection"][0]["recorded_scans"],
        state["collection"][1]["recorded_scans"]
    );
    core.shutdown();
}

#[test]
fn unexpected_process_exit_preserves_complete_raw_frames() {
    let mut core = Core::launch("crash");
    let mut p = profile(&core.root);
    p["recording_options"] = json!({});
    let path = core.root.join("interrupted");
    core.call(json!({"op":"start","profile":p,"recording_path":path}));
    core.until(|s| s["recorded_scans"].as_u64().unwrap() > 30);
    core.child.kill().unwrap();
    core.child.wait().unwrap();
    let meta: Value =
        serde_json::from_slice(&fs::read(path.join("metadata.json")).unwrap()).unwrap();
    assert_eq!(meta["finalized"], false);
    let bytes = fs::read(path.join("raw.f64")).unwrap();
    assert!(!bytes.is_empty());
    assert_eq!(bytes.len() % (2 * 8), 0);
    assert!(
        bytes
            .chunks_exact(8)
            .map(|b| f64::from_le_bytes(b.try_into().unwrap()))
            .all(f64::is_finite)
    );
}
