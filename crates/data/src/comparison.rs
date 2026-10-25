use crate::exchange::{atomic_output, csv_row, delimiter, protect_source};
use crate::{
    OperationControl, Recording, Result, finite, integer, required_path, text, write_json,
};
use serde_json::{Value, json};
use std::fs;

fn source(entry: &Value) -> Result<(Recording, Vec<usize>, f64, u64, f64)> {
    let recording = Recording::open(required_path(entry, "path")?)?;
    if !recording.is_finalized() {
        return Err("比较来源必须已收尾".into());
    }
    let columns = recording.columns(entry.get("channel_indices").or(entry.get("channels")))?;
    let first = recording.indices(0, 1).first().copied().unwrap_or(0);
    let zero = finite(entry, "zero_seconds", first as f64 / recording.sample_rate)?;
    let origin = integer(entry, "sample_origin", first)?;
    let shift = finite(entry, "shift_seconds", 0.0)?;
    if zero < 0.0 {
        return Err("比较时间零点不能为负".into());
    }
    let true_index = |index: u64| {
        recording.segments.iter().any(|s| {
            let start = s.source_start_scan - recording.origin();
            index >= start && index < start + s.sample_count
        })
    };
    if recording.sample_count == 0 {
        if zero != 0.0 || origin != 0 {
            return Err("空记录零点须为零".into());
        }
    } else {
        if !true_index(origin) {
            return Err("样本零点必须对应实际源样本，不能位于暂停缺口".into());
        }
        let nearest = (zero * recording.sample_rate).round();
        if nearest < 0.0
            || nearest > u64::MAX as f64
            || !true_index(nearest as u64)
            || ((nearest / recording.sample_rate) - zero).abs() > 1e-10 / recording.sample_rate
        {
            return Err("时间零点必须对应实际样本".into());
        }
    }
    Ok((recording, columns, zero, origin, shift))
}
fn options(request: &Value) -> Result<(&str, &str, f64, f64)> {
    let axis = text(request, "axis", "relative");
    let values = text(request, "values", "engineering");
    if !["relative", "absolute", "samples"].contains(&axis) {
        return Err("比较横轴无效".into());
    }
    if !["raw", "engineering"].contains(&values) {
        return Err("比较 values 必须为 raw 或 engineering".into());
    }
    let window = request.get("window").unwrap_or(request);
    let start = finite(window, "start", 0.0)?;
    let span = finite(window, "span", 10.0)?;
    if span <= 0.0 {
        return Err("比较窗口宽度必须大于零".into());
    }
    Ok((axis, values, start, span))
}
pub fn compare(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let (axis, values, start, span) = options(request)?;
    let entries = request["sources"]
        .as_array()
        .ok_or("比较需要 sources 数组")?;
    let mut traces = Vec::new();
    let mut source_frames = Vec::new();
    let mut sources = Vec::new();
    let mut remaining = 2_000_000u64;
    let mut truncated = false;
    let mut bounds: Option<(f64, f64)> = None;
    for (source_index, entry) in entries.iter().enumerate() {
        control.check(source_index as u64, entries.len() as u64)?;
        let (r, columns, zero, origin, shift) = source(entry)?;
        let epoch = if axis == "absolute" {
            r.epoch().ok_or("该记录缺少可靠起点，不能使用绝对时间")?
        } else {
            0.0
        };
        let transform = |index: u64| match axis {
            "samples" => index as f64 - origin as f64,
            "absolute" => epoch + index as f64 / r.sample_rate + shift,
            _ => index as f64 / r.sample_rate - zero,
        };
        if r.sample_count > 0 {
            let first = transform(r.indices(0, 1)[0]);
            let last = transform(r.indices(r.sample_count - 1, 1)[0]);
            bounds = Some(
                bounds
                    .map(|b| (b.0.min(first), b.1.max(last)))
                    .unwrap_or((first, last)),
            );
        }
        let (first_time, last_time) = match axis {
            "samples" => (
                (start + origin as f64) / r.sample_rate,
                (start + span + origin as f64) / r.sample_rate,
            ),
            "absolute" => {
                let tolerance = 2.0 * f64::EPSILON * start.abs().max(epoch.abs()).max(1.0);
                (
                    start - epoch - shift - tolerance,
                    start + span - epoch - shift - tolerance,
                )
            }
            _ => (start + zero, start + span + zero),
        };
        let first = r.frame_at_time(first_time);
        let stop = r.frame_at_time(last_time);
        let requested = stop.saturating_sub(first);
        let count = requested.min(200_000).min(remaining / columns.len() as u64);
        truncated |= count < requested;
        remaining -= count * columns.len() as u64;
        source_frames.push(count);
        let data = r.window_json(first, count, &columns, values)?;
        let indices = r.indices(first, count);
        let x: Vec<_> = indices.iter().map(|i| transform(*i)).collect();
        for (column_index, column) in columns.iter().enumerate() {
            if count == 0 {
                continue;
            }
            let channel = &r.channels[*column];
            let key = format!("{source_index}:{}", channel.index);
            let style = &request["appearance"][&key];
            traces.push(json!({"source_index":source_index,"path":r.path,"channel":channel,
                "label":format!("{}/AI{}/{}",r.path.file_name().unwrap_or_default().to_string_lossy(),channel.index,channel.name),
                "unit":if values=="raw"{"V"}else{&channel.unit},"x":x,"times":data["times"],"sample_indices":indices,
                "raw_values":data["raw"][column_index],"values":data["samples"][column_index],"gaps":data["gaps"],
                "visible":style["visible"].as_bool().unwrap_or(true),"color":style["color"]}));
        }
        sources.push(json!({"path":r.path,"channel_indices":columns.iter().map(|c|r.channels[*c].index).collect::<Vec<_>>(),
            "zero_seconds":zero,"sample_origin":origin,"shift_seconds":shift,"sample_rate":r.sample_rate,"metadata":r.metadata}));
    }
    control.check(entries.len() as u64, entries.len() as u64)?;
    Ok(
        json!({"traces":traces,"sources":sources,"axis":axis,"values":values,"window":{"start":start,"span":span},
        "bounds":bounds.unwrap_or((0.0,0.0)),"source_frames":source_frames,"truncated":truncated,"value_count":2_000_000-remaining}),
    )
}
fn validate_layout(layout: &Value) -> Result<()> {
    if layout["schema"] != "labdaq-comparison-v1" {
        return Err("比较布局格式版本不支持".into());
    }
    options(layout)?;
    let sources = layout["sources"].as_array().ok_or("布局缺少来源数组")?;
    let mut keys = std::collections::HashSet::new();
    for (index, entry) in sources.iter().enumerate() {
        let (r, columns, _, _, _) = source(entry)?;
        for column in columns {
            keys.insert(format!("{index}:{}", r.channels[column].index));
        }
    }
    if let Some(appearance) = layout.get("appearance") {
        for (key, style) in appearance.as_object().ok_or("appearance 必须为对象")? {
            if !keys.contains(key)
                || !style["visible"].is_boolean()
                || style["color"].as_str().is_none_or(str::is_empty)
            {
                return Err("布局曲线样式无效".into());
            }
        }
    }
    if let Some(sizes) = layout.get("view").and_then(|v| v.get("splitter_sizes")) {
        if sizes.as_array().is_none_or(|v| {
            v.len() != 2 || v.iter().any(|x| x.as_u64().is_none_or(|x| x > 100_000))
        }) {
            return Err("布局分隔尺寸无效".into());
        }
    }
    Ok(())
}
pub fn save_layout(request: &Value) -> Result<Value> {
    let path = required_path(request, "output")?;
    let mut layout = request.get("layout").unwrap_or(request).clone();
    validate_layout(&layout)?;
    for entry in layout["sources"].as_array_mut().unwrap() {
        let (recording, columns, zero, origin, shift) = source(entry)?;
        entry["channel_indices"] = json!(
            columns
                .iter()
                .map(|c| recording.channels[*c].index)
                .collect::<Vec<_>>()
        );
        entry["zero_seconds"] = json!(zero);
        entry["sample_origin"] = json!(origin);
        entry["shift_seconds"] = json!(shift);
    }
    write_json(&path, &layout, request["overwrite"] == true)?;
    Ok(json!({"output":path}))
}
pub fn load_layout(request: &Value) -> Result<Value> {
    let path = required_path(request, "path")?;
    let layout: Value = serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    validate_layout(&layout)?;
    Ok(layout)
}
pub fn export_comparison(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let result = compare(request, control)?;
    let output = required_path(request, "output")?;
    for entry in request["sources"].as_array().unwrap() {
        protect_source(&Recording::open(required_path(entry, "path")?)?, &output)?;
    }
    let delimiter = delimiter(request)?;
    let total = result["value_count"].as_u64().unwrap();
    atomic_output(&output, request["overwrite"] == true, |w| {
        writeln!(w,"\u{feff}# labdaq_metadata: {}",json!({"format":"labdaq-comparison-csv-v1","axis":result["axis"],"window":result["window"],"sources":result["sources"]})).map_err(|e|e.to_string())?;
        csv_row(
            w,
            &[
                "recording",
                "channel_index",
                "channel_name",
                "sample_index",
                "source_time_s",
                "comparison_x",
                "raw_V",
                "value",
                "unit",
            ]
            .map(str::to_string),
            delimiter,
        )?;
        let mut done = 0;
        for trace in result["traces"].as_array().unwrap() {
            for i in 0..trace["x"].as_array().unwrap().len() {
                if done % 4096 == 0 {
                    control.check(done, total)?;
                }
                csv_row(
                    w,
                    &vec![
                        text(trace, "path", "").into(),
                        trace["channel"]["index"].to_string(),
                        text(&trace["channel"], "name", "").into(),
                        trace["sample_indices"][i].to_string(),
                        trace["times"][i].to_string(),
                        trace["x"][i].to_string(),
                        trace["raw_values"][i].to_string(),
                        trace["values"][i].to_string(),
                        text(trace, "unit", "").into(),
                    ],
                    delimiter,
                )?;
                done += 1;
            }
        }
        control.check(done, total)
    })?;
    Ok(json!({"output":output,"value_count":total,"truncated":result["truncated"]}))
}
