//! Bounded recording access, data exchange and comparison, without a Python runtime.
mod comparison;
mod exchange;
mod hpf;
mod session;

pub use comparison::{compare, load_layout, save_layout};
pub use exchange::{export_csv, export_uff, statistics};
pub use hpf::export_hpf;
pub use session::{Channel, Recording, Segment, list_recordings, update_metadata};

use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

pub type Result<T> = std::result::Result<T, String>;

#[derive(Default)]
pub struct OperationControl<'a> {
    pub cancelled: Option<&'a AtomicBool>,
    pub progress: Option<&'a dyn Fn(u64, u64)>,
}
impl OperationControl<'_> {
    pub fn check(&self, completed: u64, total: u64) -> Result<()> {
        if self.cancelled.is_some_and(|v| v.load(Ordering::Relaxed)) {
            return Err("操作已取消".into());
        }
        if let Some(progress) = self.progress {
            progress(completed, total);
        }
        Ok(())
    }
}

pub(crate) fn text<'a>(v: &'a Value, key: &str, default: &'a str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or(default)
}
pub(crate) fn required_path(v: &Value, key: &str) -> Result<PathBuf> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .ok_or_else(|| format!("缺少路径 {key}"))
}
pub(crate) fn integer(v: &Value, key: &str, default: u64) -> Result<u64> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(value) => value
            .as_u64()
            .ok_or_else(|| format!("{key} 必须为非负整数")),
    }
}
pub(crate) fn finite(v: &Value, key: &str, default: f64) -> Result<f64> {
    let value = match v.get(key) {
        None | Some(Value::Null) => default,
        Some(value) => value.as_f64().ok_or_else(|| format!("{key} 必须为数值"))?,
    };
    if value.is_finite() {
        Ok(value)
    } else {
        Err(format!("{key} 必须有限"))
    }
}
pub(crate) fn now() -> String {
    time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap()
}
pub(crate) fn epoch(v: &Value) -> Option<f64> {
    time::OffsetDateTime::parse(v.as_str()?, &time::format_description::well_known::Rfc3339)
        .ok()
        .map(|d| d.unix_timestamp_nanos() as f64 / 1e9)
}
pub(crate) fn write_json(path: &Path, value: &Value, overwrite: bool) -> Result<()> {
    exchange::atomic_output(path, overwrite, |w| {
        serde_json::to_writer_pretty(w, value).map_err(|e| e.to_string())
    })
}

/// All arrays named `samples` or `raw` are channel-major. `start`/`stop` refer
/// to stored frames; `sample_indices` retain the original source timeline.
pub fn execute_json(request: &Value) -> Result<Value> {
    execute_json_with_control(request, &OperationControl::default())
}

pub fn execute_json_with_control(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    control.check(0, 0)?;
    match text(request, "op", "open") {
        "list" => list_recordings(&required_path(request, "root")?, text(request, "query", "")),
        "open" => Ok(Recording::open(required_path(request, "path")?)?.info()),
        "read" => {
            let recording = Recording::from_request(request)?;
            let start = if request.get("start_seconds").is_some() {
                recording.frame_at_time(finite(request, "start_seconds", 0.0)?)
            } else {
                integer(request, "start", 0)?
            };
            let count = integer(request, "count", 200_000)?.min(200_000);
            let columns = recording.columns(request.get("channels"))?;
            recording.window_json(
                start,
                count,
                &columns,
                text(request, "values", "engineering"),
            )
        }
        "stats" => statistics(request, control),
        "export" => export_csv(request, control),
        "export_hpf" => export_hpf(request, control),
        "export_uff" => export_uff(request, control),
        "batch_export" => exchange::batch_export(request, control),
        "update_metadata" => update_metadata(request),
        "recover" => session::recover(request, control),
        "compare" => compare(request, control),
        "export_comparison" => comparison::export_comparison(request, control),
        "save_layout" => save_layout(request),
        "load_layout" => load_layout(request),
        "export_spectrum" => exchange::export_spectrum(request, control),
        "capabilities" => Ok(json!({"format":"labdaq-data-capabilities-v1",
            "operations":["list","open","read","stats","export","batch_export","update_metadata",
                "recover","compare","export_comparison","save_layout","load_layout","export_hpf",
                "export_spectrum","export_uff"],
            "native":"labdaq-raw-voltage v1", "hpf":"datx 0x10001 synchronous Double; raw V and verified DT9837C force profile",
            "hpf_write":"contiguous raw voltage Double, one synchronous group; paused gaps rejected",
            "uff":"ASCII dataset 58, real/complex double, even/uneven abscissa",
            "max_read_frames":200000,"comparison_max_values":2000000})),
        op => Err(format!("未知数据操作：{op}")),
    }
}
