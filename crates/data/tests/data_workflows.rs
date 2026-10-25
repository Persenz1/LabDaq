use labdaq_data::{OperationControl, Recording, execute_json, execute_json_with_control};
use serde_json::{Value, json};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

fn channels() -> Value {
    json!([
        {"index":0,"name":"力,一","unit":"N","sensitivity_mv_per_unit":500.0,"zero_volts":0.5,"inverted":false,"engineering_offset":3.0,"point_number":7,"direction":"+X"},
        {"index":2,"name":"位移二","unit":"mm","sensitivity_mv_per_unit":2000.0,"zero_volts":-1.0,"inverted":true,"engineering_offset":-2.0,"point_number":8,"direction":"-Y"}
    ])
}
fn native(root: &Path, name: &str, rate: f64, paused: bool, finalized: bool) -> PathBuf {
    let path = root.join(name);
    fs::create_dir_all(&path).unwrap();
    let segments = if paused {
        json!([{"file_start_scan":0,"source_start_scan":10,"sample_count":3},{"file_start_scan":3,"source_start_scan":20,"sample_count":3}])
    } else {
        json!([{"file_start_scan":0,"source_start_scan":10,"sample_count":6}])
    };
    let metadata = json!({"format":"labdaq-raw-voltage","format_version":1,"dtype":"<f8","layout":"sample-major","sample_rate":rate,"sample_count":6,
        "channels":channels(),"recording_segments":segments,"acquisition_config":{"record_start_scan":10},"finalized":finalized,
        "active_segment":if finalized{Value::Null}else{json!({"file_start_scan":0,"source_start_scan":10})},
        "recording_options":{"group_number":1,"notes":"notes","condition":"step","specimen":"steel"},"experiment":"experiment",
        "started_at_utc":"2026-01-01T00:00:00Z","acquisition_started_at_utc":"2026-01-01T00:00:00Z","termination_reason":"user_stop","error":null});
    fs::write(
        path.join("metadata.json"),
        serde_json::to_vec(&metadata).unwrap(),
    )
    .unwrap();
    let mut raw = File::create(path.join("raw.f64")).unwrap();
    for i in 0..6 {
        for value in [i as f64, (-2 * i) as f64] {
            raw.write_all(&value.to_le_bytes()).unwrap();
        }
    }
    path
}
fn metadata(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path.join("metadata.json")).unwrap()).unwrap()
}
fn set_metadata(path: &Path, value: &Value) {
    fs::write(
        path.join("metadata.json"),
        serde_json::to_vec(value).unwrap(),
    )
    .unwrap();
}
fn approx(actual: f64, expected: f64) {
    assert!((actual - expected).abs() < 1e-9, "{actual} != {expected}");
}

#[test]
fn window_keeps_nonconsecutive_channels_conversion_and_pause_gap() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, true);
    let result =
        execute_json(&json!({"op":"read","path":path,"start":1,"count":5,"channels":[2,0]}))
            .unwrap();
    assert_eq!(result["sample_indices"], json!([1, 2, 10, 11, 12]));
    assert_eq!(result["gaps"], json!([2]));
    assert_eq!(result["raw"][0], json!([-2.0, -4.0, -6.0, -8.0, -10.0]));
    approx(result["samples"][0][0].as_f64().unwrap(), -1.5);
    approx(result["samples"][1][0].as_f64().unwrap(), 4.0);
    assert_eq!(result["channels"][0]["index"], 2);
    assert_eq!(result["count"], 5);
    let empty = execute_json(&json!({"op":"read","path":path,"start":100,"count":5})).unwrap();
    assert_eq!(empty["count"], 0);
}

#[test]
fn read_selection_errors_and_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    for changes in [
        json!({"channels":[]}),
        json!({"channels":[1]}),
        json!({"channels":[0,0]}),
        json!({"start":-1}),
        json!({"count":1.5}),
        json!({"values":"missing"}),
    ] {
        let mut request = json!({"op":"read","path":path});
        request
            .as_object_mut()
            .unwrap()
            .extend(changes.as_object().unwrap().clone());
        assert!(execute_json(&request).is_err(), "{request}");
    }
    let r = Recording::open(path).unwrap();
    assert!(r.read(0, 200001, &[0]).is_err());
    assert!(r.read(0, 1, &[99]).is_err());
}

#[test]
fn frame_lookup_handles_gap_and_half_open_exact_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    let r = Recording::open(native(temp.path(), "record", 10.0, true, true)).unwrap();
    assert_eq!(r.frame_at_time(-1.0), 0);
    assert_eq!(r.frame_at_time(0.1), 1);
    assert_eq!(r.frame_at_time(0.1001), 2);
    assert_eq!(r.frame_at_time(0.5), 3);
    assert_eq!(r.frame_at_time(1.0), 3);
    assert_eq!(r.frame_at_time(1.2), 5);
    assert_eq!(r.frame_at_time(1.21), 6);
}

#[test]
fn old_metadata_without_segments_remains_readable() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    let mut m = metadata(&path);
    m.as_object_mut().unwrap().remove("recording_segments");
    set_metadata(&path, &m);
    assert_eq!(
        Recording::open(path).unwrap().indices(0, 6),
        vec![0, 1, 2, 3, 4, 5]
    );
}

#[test]
fn malformed_metadata_and_finalized_length_are_rejected() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    let original = metadata(&path);
    for key_value in [
        ("format", json!("other")),
        ("dtype", json!("f32")),
        ("sample_rate", json!(0)),
        ("sample_count", json!(99)),
        ("channels", json!([])),
    ] {
        let mut m = original.clone();
        m[key_value.0] = key_value.1;
        set_metadata(&path, &m);
        assert!(Recording::open(&path).is_err());
    }
    let mut m = original.clone();
    m["recording_segments"][0]["source_start_scan"] = json!(9);
    set_metadata(&path, &m);
    assert!(Recording::open(&path).is_err());
    set_metadata(&path, &original);
    File::options()
        .append(true)
        .open(path.join("raw.f64"))
        .unwrap()
        .write_all(&[1])
        .unwrap();
    assert!(Recording::open(path).is_err());
}

#[test]
fn interrupted_complete_frames_are_recovered_without_modifying_source() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "source", 10.0, false, false);
    let mut m = metadata(&path);
    m["sample_count"] = json!(0);
    m["recording_segments"] = json!([]);
    set_metadata(&path, &m);
    File::options()
        .append(true)
        .open(path.join("raw.f64"))
        .unwrap()
        .write_all(&[1, 2, 3])
        .unwrap();
    let original = fs::read(path.join("raw.f64")).unwrap();
    let r = Recording::open(&path).unwrap();
    assert_eq!(r.sample_count, 6);
    assert_eq!(r.metadata["incomplete_tail_bytes"], 3);
    let out = temp.path().join("recovered");
    let result = execute_json(&json!({"op":"recover","path":path,"output":out})).unwrap();
    assert_eq!(result["sample_count"], 6);
    assert_eq!(result["abnormal"], true);
    assert_eq!(fs::read(path.join("raw.f64")).unwrap(), original);
    assert_eq!(fs::metadata(out.join("raw.f64")).unwrap().len(), 96);
    assert!(execute_json(&json!({"op":"recover","path":path,"output":out})).is_err());
}

#[test]
fn resumed_interrupted_record_reconstructs_only_known_active_segment() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, false);
    let mut m = metadata(&path);
    m["recording_segments"] =
        json!([{"file_start_scan":0,"source_start_scan":10,"sample_count":3}]);
    m["active_segment"] = json!({"file_start_scan":3,"source_start_scan":20});
    m["sample_count"] = json!(3);
    set_metadata(&path, &m);
    assert_eq!(
        Recording::open(&path).unwrap().indices(0, 6),
        vec![0, 1, 2, 10, 11, 12]
    );
    m["active_segment"] = Value::Null;
    set_metadata(&path, &m);
    assert!(Recording::open(&path).is_err());
}

#[test]
fn statistics_are_population_statistics_of_stored_samples() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, true);
    let result =
        execute_json(&json!({"op":"stats","path":path,"channels":[0],"values":"raw"})).unwrap();
    let s = &result["statistics"][0];
    assert_eq!(s["count"], 6);
    approx(s["mean"].as_f64().unwrap(), 2.5);
    approx(s["std"].as_f64().unwrap(), (35.0f64 / 12.0).sqrt());
    approx(s["rms"].as_f64().unwrap(), (55.0f64 / 6.0).sqrt());
    let result =
        execute_json(&json!({"op":"stats","path":path,"start":1,"stop":3,"channels":[2]})).unwrap();
    approx(result["statistics"][0]["mean"].as_f64().unwrap(), -1.0);
    for (start, stop) in [(2, 2), (4, 3), (0, 7)] {
        assert!(
            execute_json(&json!({"op":"stats","path":path,"start":start,"stop":stop})).is_err()
        );
    }
}

#[test]
fn csv_exports_raw_engineering_metadata_quotes_and_original_time() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, true);
    let output = temp.path().join("export.csv");
    execute_json(&json!({"op":"export","path":path,"output":output,"start":2,"stop":5,"channels":[0],"include_statistics":true})).unwrap();
    let csv = fs::read_to_string(&output).unwrap();
    let lines: Vec<_> = csv.lines().collect();
    assert!(lines[0].contains("labdaq_metadata"));
    assert!(lines[1].contains("\"力,一[AI0]_raw_V\""));
    assert_eq!(lines[2], "0.2,2,6");
    assert_eq!(lines[3], "1,3,8");
    assert!(lines.last().unwrap().starts_with("# labdaq_statistics:"));
    assert!(execute_json(&json!({"op":"export","path":path,"output":output})).is_err());
    for output in [path.join("raw.f64"), path.join("metadata.json")] {
        assert!(
            execute_json(&json!({"op":"export","path":path,"output":output,"overwrite":true}))
                .is_err()
        );
    }
}

#[test]
fn export_time_formats_cover_units_sample_and_absolute_time() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    for (format, expected) in [
        ("samples", "1"),
        ("milliseconds", "100"),
        ("microseconds", "100000"),
        ("datetime", "2026-01-01T00:00:01.1Z"),
        ("excel", "46023.000012731485"),
    ] {
        let output = temp.path().join(format!("{format}.txt"));
        execute_json(&json!({"op":"export","path":path,"output":output,"time_format":format,"delimiter":"\t","start":1,"stop":2,"values":"raw"})).unwrap();
        let content = fs::read_to_string(output).unwrap();
        let actual = content.lines().nth(2).unwrap().split('\t').next().unwrap();
        if format == "excel" {
            approx(actual.parse().unwrap(), expected.parse().unwrap());
        } else {
            assert_eq!(actual, expected);
        }
    }
    let mut m = metadata(&path);
    m.as_object_mut().unwrap().remove("started_at_utc");
    m.as_object_mut()
        .unwrap()
        .remove("acquisition_started_at_utc");
    set_metadata(&path, &m);
    assert!(execute_json(&json!({"op":"export","path":path,"output":temp.path().join("bad.csv"),"time_format":"datetime"})).is_err());
}

#[test]
fn cancelled_operations_do_not_replace_existing_exports() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    let output = temp.path().join("data.csv");
    fs::write(&output, "original").unwrap();
    let flag = AtomicBool::new(false);
    let progress = |_: u64, total: u64| {
        if total > 0 {
            flag.store(true, Ordering::Relaxed);
        }
    };
    let control = OperationControl {
        cancelled: Some(&flag),
        progress: Some(&progress),
    };
    assert!(
        execute_json_with_control(
            &json!({"op":"export","path":path,"output":output,"overwrite":true}),
            &control
        )
        .is_err()
    );
    assert_eq!(fs::read_to_string(output).unwrap(), "original");
    assert!(execute_json_with_control(&json!({"op":"stats","path":path}), &control).is_err());
}

#[test]
fn metadata_edits_preserve_raw_and_acquisition_history() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, true);
    let before = metadata(&path);
    let raw = fs::read(path.join("raw.f64")).unwrap();
    execute_json(&json!({"op":"update_metadata","path":path,"experiment":"new","condition":"changed","notes":"record"})).unwrap();
    let after = metadata(&path);
    for key in [
        "sample_count",
        "recording_segments",
        "termination_reason",
        "channels",
    ] {
        assert_eq!(before[key], after[key]);
    }
    assert_eq!(after["experiment"], "new");
    assert_eq!(raw, fs::read(path.join("raw.f64")).unwrap());
    let active = native(temp.path(), "active", 10.0, false, false);
    assert!(
        execute_json(&json!({"op":"update_metadata","path":active,"experiment":"new"})).is_err()
    );
}

#[test]
fn library_lists_nested_legacy_invalid_and_searchable_records() {
    let temp = tempfile::tempdir().unwrap();
    let _a = native(temp.path(), "legacy", 10.0, false, true);
    let _b = native(temp.path(), "exp/nested", 10.0, false, true);
    fs::create_dir(temp.path().join("broken")).unwrap();
    fs::write(temp.path().join("broken/metadata.json"), "{}").unwrap();
    let all = execute_json(&json!({"op":"list","root":temp.path()})).unwrap();
    assert_eq!(all["recordings"].as_array().unwrap().len(), 3);
    let found = execute_json(&json!({"op":"list","root":temp.path(),"query":"nested"})).unwrap();
    assert_eq!(found["recordings"].as_array().unwrap().len(), 1);
    assert_eq!(
        execute_json(&json!({"op":"list","root":temp.path().join("absent")})).unwrap()["recordings"],
        json!([])
    );
}

#[test]
fn batch_export_retains_success_and_rejects_abnormal_and_collision() {
    let temp = tempfile::tempdir().unwrap();
    let good = native(temp.path(), "good", 10.0, false, true);
    let bad = native(temp.path(), "bad", 10.0, false, true);
    let mut m = metadata(&bad);
    m["error"] = json!("lost");
    set_metadata(&bad, &m);
    let output = temp.path().join("exports");
    let result =
        execute_json(&json!({"op":"batch_export","paths":[good,bad],"output_dir":output})).unwrap();
    assert_eq!(result["completed"].as_array().unwrap().len(), 1);
    assert_eq!(result["errors"].as_array().unwrap().len(), 1);
    let again =
        execute_json(&json!({"op":"batch_export","paths":[good],"output_dir":output})).unwrap();
    assert_eq!(again["errors"].as_array().unwrap().len(), 1);
}

fn layout(a: &Path, b: &Path) -> Value {
    json!({"schema":"labdaq-comparison-v1","sources":[
    {"path":a,"channel_indices":[0],"zero_seconds":0.0,"sample_origin":0,"shift_seconds":0.0},
    {"path":b,"channel_indices":[2],"zero_seconds":0.0,"sample_origin":0,"shift_seconds":0.0}],
    "axis":"relative","values":"engineering","window":{"start":0.0,"span":2.0},"appearance":{"0:0":{"visible":false,"color":"#ff0000"}},"view":{"splitter_sizes":[200,800]}})
}

#[test]
fn comparison_uses_independent_rates_gaps_and_physical_channels() {
    let temp = tempfile::tempdir().unwrap();
    let a = native(temp.path(), "a", 10.0, true, true);
    let b = native(temp.path(), "b", 20.0, false, true);
    let mut req = layout(&a, &b);
    req["op"] = json!("compare");
    let result = execute_json(&req).unwrap();
    assert_eq!(result["source_frames"], json!([6, 6]));
    assert_eq!(result["traces"][0]["gaps"], json!([3]));
    approx(result["traces"][1]["x"][1].as_f64().unwrap(), 0.05);
    assert_eq!(result["traces"][0]["visible"], false);
    assert_eq!(result["traces"][1]["channel"]["index"], 2);
    req["window"] = json!({"start":0.1,"span":0.1});
    let result = execute_json(&req).unwrap();
    assert_eq!(result["source_frames"], json!([1, 2]));
    req["sources"][0]["sample_origin"] = json!(5);
    assert!(execute_json(&req).is_err());
}

#[test]
fn comparison_layout_roundtrip_and_long_table_export() {
    let temp = tempfile::tempdir().unwrap();
    let a = native(temp.path(), "a", 10.0, true, true);
    let b = native(temp.path(), "b", 20.0, false, true);
    let l = layout(&a, &b);
    let path = temp.path().join("view.json");
    execute_json(&json!({"op":"save_layout","layout":l,"output":path})).unwrap();
    assert_eq!(
        execute_json(&json!({"op":"load_layout","path":path})).unwrap(),
        l
    );
    let mut req = l.clone();
    req["op"] = json!("export_comparison");
    req["output"] = json!(temp.path().join("comparison.csv"));
    let result = execute_json(&req).unwrap();
    assert_eq!(result["value_count"], 12);
    let csv = fs::read_to_string(temp.path().join("comparison.csv")).unwrap();
    assert_eq!(csv.lines().count(), 14);
    let mut invalid = l;
    invalid["appearance"]["99:0"] = json!({"visible":true,"color":"red"});
    assert!(
        execute_json(
            &json!({"op":"save_layout","layout":invalid,"output":temp.path().join("invalid.json")})
        )
        .is_err()
    );
}

#[test]
fn spectrum_and_uff_exports_preserve_complex_values_and_dof() {
    let temp = tempfile::tempdir().unwrap();
    let req = json!({"frequencies":[0.0,1.0,2.0],"traces":[{"name":"FRF","real":[1.0,-2.0,3.0],"imag":[0.0,4.0,-5.0],"unit":"m/s2","reference_unit":"N","point_number":7,"direction":-2,"reference_point":8,"reference_direction":1,"function_type":4}]});
    let mut csv = req.clone();
    csv["op"] = json!("export_spectrum");
    csv["output"] = json!(temp.path().join("freq.csv"));
    execute_json(&csv).unwrap();
    assert!(
        fs::read_to_string(temp.path().join("freq.csv"))
            .unwrap()
            .contains("1,-2,4")
    );
    let mut uff = req;
    uff["op"] = json!("export_uff");
    uff["output"] = json!(temp.path().join("freq.uff"));
    execute_json(&uff).unwrap();
    let content = fs::read_to_string(temp.path().join("freq.uff")).unwrap();
    let lines: Vec<_> = content.lines().collect();
    assert_eq!(lines[7][41..51].trim(), "7");
    assert_eq!(lines[7][51..55].trim(), "-2");
    assert_eq!(lines[8][..10].trim(), "6");
    assert!(content.contains("4.000000000000E+00"));
    uff["frequencies"] = json!([0.0, 1.0, 2.2]);
    uff["output"] = json!(temp.path().join("uneven.uff"));
    let info = execute_json(&uff).unwrap();
    assert_eq!(info["even_spacing"], false);
}

#[test]
fn hpf_raw_and_engineering_roundtrip_and_partial_window() {
    let temp = tempfile::tempdir().unwrap();
    let original = native(temp.path(), "record", 2000.0, false, true);
    let hpf = temp.path().join("record.hpf");
    execute_json(&json!({"op":"export_hpf","path":original,"output":hpf})).unwrap();
    let r = Recording::open(&hpf).unwrap();
    assert_eq!(r.sample_count, 6);
    assert_eq!(r.channels[1].index, 2);
    assert_eq!(r.channels[0].unit, "N");
    let before = execute_json(&json!({"op":"read","path":original,"start":1,"count":4})).unwrap();
    let after = execute_json(&json!({"op":"read","path":hpf,"start":1,"count":4})).unwrap();
    assert_eq!(before["samples"], after["samples"]);
    assert_eq!(before["raw"], after["raw"]);
    assert_eq!(fs::metadata(hpf).unwrap().len() % 65536, 0);
}

#[test]
fn hpf_rejects_paused_export_and_malformed_headers_indices() {
    let temp = tempfile::tempdir().unwrap();
    let original = native(temp.path(), "record", 2000.0, true, true);
    let hpf = temp.path().join("record.hpf");
    assert!(execute_json(&json!({"op":"export_hpf","path":original,"output":hpf})).is_err());
    execute_json(&json!({"op":"export_hpf","path":original,"output":hpf,"start":3,"stop":6}))
        .unwrap();
    let bytes = fs::read(&hpf).unwrap();
    for (offset, byte) in [(0usize, 0xffu8), (16, 0xff), (24, 0xff), (8, 1)] {
        let mut changed = bytes.clone();
        changed[offset] = byte;
        let bad = temp.path().join(format!("bad{offset}.hpf"));
        fs::write(&bad, changed).unwrap();
        assert!(Recording::open(&bad).is_err());
    }
    let mut short = bytes;
    short.pop();
    let bad = temp.path().join("short.hpf");
    fs::write(&bad, short).unwrap();
    assert!(Recording::open(bad).is_err());
}

#[test]
fn nonfinite_samples_are_never_exported_or_analysed() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    let mut bytes = fs::read(path.join("raw.f64")).unwrap();
    bytes[..8].copy_from_slice(&f64::NAN.to_le_bytes());
    fs::write(path.join("raw.f64"), bytes).unwrap();
    for op in ["read", "stats", "export"] {
        assert!(
            execute_json(&json!({"op":op,"path":path,"output":temp.path().join("bad.csv")}))
                .is_err()
        );
    }
    assert!(!temp.path().join("bad.csv").exists());
}

#[test]
fn unknown_operations_and_missing_paths_fail_explicitly() {
    assert!(execute_json(&json!({"op":"unimplemented"})).is_err());
    assert!(execute_json(&json!({"op":"read"})).is_err());
    assert!(
        execute_json(&json!({"op":"capabilities"})).unwrap()["operations"]
            .as_array()
            .unwrap()
            .len()
            > 10
    );
}

#[test]
fn spectrum_null_values_are_empty_cells_without_losing_valid_frf() {
    let temp = tempfile::tempdir().unwrap();
    let output = temp.path().join("partial.csv");
    let request = json!({"op":"export_spectrum","output":output,"frequencies":[1.0,2.0],"traces":[{"name":"FRF","unit":"N/V","real":[0.0,1.0],"imag":[0.0,2.0],"coherence":[null,0.5],"field_units":{"coherence":"1"}}]});
    execute_json(&request).unwrap();
    let contents = fs::read_to_string(&output).unwrap();
    let lines: Vec<_> = contents.lines().collect();
    assert!(lines[0].contains("empty cell"));
    assert!(lines[1].contains("coherence[1]"));
    assert_eq!(lines[2], "1,0,0,");
    assert_eq!(lines[3], "2,1,2,0.5");
    let mut invalid = request;
    invalid["overwrite"] = json!(true);
    invalid["traces"][0]["real"][0] = json!("invalid");
    assert!(execute_json(&invalid).is_err());
    assert_eq!(fs::read_to_string(output).unwrap(), contents);
}

#[test]
fn source_time_requests_seek_through_pauses_and_include_left_exclude_right() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, true);
    let read =
        execute_json(&json!({"op":"read","path":path,"start_seconds":0.6,"count":2})).unwrap();
    assert_eq!(read["start"], 3);
    assert_eq!(read["sample_indices"], json!([10, 11]));
    let stats=execute_json(&json!({"op":"stats","path":path,"start_seconds":0.2,"stop_seconds":1.1,"channels":[0],"values":"raw"})).unwrap();
    assert_eq!(stats["statistics"][0]["count"], 2);
    approx(stats["statistics"][0]["mean"].as_f64().unwrap(), 2.5);
}

#[test]
fn conversion_overrides_reprocess_values_but_never_edit_recording() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    let before = fs::read(path.join("metadata.json")).unwrap();
    let request = json!({"op":"read","path":path,"channel_overrides":[{"index":0,"sensitivity_mv_per_unit":1000.0,"zero_volts":0.0,"engineering_offset":0.0,"unit":"V"}]});
    let read = execute_json(&request).unwrap();
    assert_eq!(read["samples"][0], read["raw"][0]);
    assert_eq!(read["channels"][0]["unit"], "V");
    assert!(read["metadata"]["processing_channel_overrides"].is_array());
    assert_eq!(fs::read(path.join("metadata.json")).unwrap(), before);
    let mut invalid = request;
    invalid["channel_overrides"][0]["sensitivity_mv_per_unit"] = json!(0);
    invalid["channel_overrides"][0]["unit"] = json!("N");
    assert!(execute_json(&invalid).is_err());
}

#[test]
fn voltage_units_bypass_sensitivity_but_keep_zero_polarity_and_offset() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, false, true);
    let r=execute_json(&json!({"op":"read","path":path,"channel_overrides":[{"index":0,"unit":"V","sensitivity_mv_per_unit":10.0,"zero_volts":0.5,"inverted":true,"engineering_offset":3.0}]})).unwrap();
    approx(r["samples"][0][1].as_f64().unwrap(), 2.5);
}

fn long_record(root: &Path, count: u64) -> PathBuf {
    let path = native(root, "long", 2000.0, false, true);
    let mut m = metadata(&path);
    m["sample_count"] = json!(count);
    m["recording_segments"][0]["sample_count"] = json!(count);
    set_metadata(&path, &m);
    let mut output = File::create(path.join("raw.f64")).unwrap();
    for i in 0..count {
        for value in [if i % 2 == 0 { 1e9 + 1.0 } else { 1e9 - 1.0 }, i as f64] {
            output.write_all(&value.to_le_bytes()).unwrap();
        }
    }
    path
}

#[test]
fn streaming_statistics_and_hpf_span_multiple_blocks() {
    let temp = tempfile::tempdir().unwrap();
    let path = long_record(temp.path(), 20000);
    let s = execute_json(&json!({"op":"stats","path":path,"channels":[0],"values":"raw"})).unwrap();
    approx(s["statistics"][0]["mean"].as_f64().unwrap(), 1e9);
    approx(s["statistics"][0]["std"].as_f64().unwrap(), 1.0);
    let hpf = temp.path().join("long.hpf");
    execute_json(&json!({"op":"export_hpf","path":path,"output":hpf})).unwrap();
    let r = Recording::open(hpf).unwrap();
    assert_eq!(
        r.read(8190, 5, &[1]).unwrap()[0],
        vec![8190.0, 8191.0, 8192.0, 8193.0, 8194.0]
    );
}

#[test]
fn cancelling_after_first_chunk_publishes_no_partial_file() {
    let temp = tempfile::tempdir().unwrap();
    let path = long_record(temp.path(), 20000);
    let flag = AtomicBool::new(false);
    let progress = |complete: u64, _: u64| {
        if complete >= 8192 {
            flag.store(true, Ordering::Relaxed);
        }
    };
    for op in ["export", "export_hpf"] {
        flag.store(false, Ordering::Relaxed);
        let output = temp.path().join(format!("cancelled-{op}.dat"));
        assert!(
            execute_json_with_control(
                &json!({"op":op,"path":path,"output":output}),
                &OperationControl {
                    cancelled: Some(&flag),
                    progress: Some(&progress)
                }
            )
            .is_err()
        );
        assert!(!output.exists());
    }
}

#[test]
fn comparison_reports_frame_cap_and_keeps_first_real_points() {
    let temp = tempfile::tempdir().unwrap();
    let path = long_record(temp.path(), 200005);
    let result=execute_json(&json!({"op":"compare","sources":[{"path":path,"channel_indices":[2]}],"span":200.0,"values":"raw"})).unwrap();
    assert_eq!(result["truncated"], true);
    assert_eq!(result["source_frames"], json!([200000]));
    assert_eq!(result["traces"][0]["values"][199999], 199999.0);
}

#[test]
fn comparison_absolute_axis_and_sample_axis_reject_fabricated_time() {
    let temp = tempfile::tempdir().unwrap();
    let path = native(temp.path(), "record", 10.0, true, true);
    let epoch = Recording::open(&path).unwrap().epoch().unwrap();
    let absolute=execute_json(&json!({"op":"compare","sources":[{"path":path}],"axis":"absolute","start":epoch+0.1,"span":0.1})).unwrap();
    assert_eq!(absolute["source_frames"], json!([1]));
    let samples=execute_json(&json!({"op":"compare","sources":[{"path":path,"sample_origin":10}],"axis":"samples","start":0,"span":3})).unwrap();
    assert_eq!(samples["source_frames"], json!([3]));
    assert_eq!(samples["traces"][0]["x"], json!([0.0, 1.0, 2.0]));
    let mut m = metadata(&path);
    m.as_object_mut()
        .unwrap()
        .remove("acquisition_started_at_utc");
    m.as_object_mut().unwrap().remove("started_at_utc");
    set_metadata(&path, &m);
    assert!(
        execute_json(&json!({"op":"compare","sources":[{"path":path}],"axis":"absolute"})).is_err()
    );
}

#[test]
fn uff_handles_string_direction_and_refuses_invalid_complex_data() {
    let temp = tempfile::tempdir().unwrap();
    let output = temp.path().join("direction.uff");
    let mut request = json!({"op":"export_uff","output":output,"frequencies":[1.0,2.0],"traces":[{"values":[1.0,2.0],"direction":"-Y"}]});
    execute_json(&request).unwrap();
    let content = fs::read_to_string(&output).unwrap();
    assert_eq!(content.lines().nth(7).unwrap()[51..55].trim(), "-2");
    request["overwrite"] = json!(true);
    request["traces"][0]["imag"] = json!([0.0]);
    assert!(execute_json(&request).is_err());
    assert_eq!(fs::read_to_string(output).unwrap(), content);
}
