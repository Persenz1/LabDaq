use crate::session::selection;
use crate::{OperationControl, Recording, Result, integer, now, required_path, text};
use serde_json::{Value, json};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

pub(crate) fn atomic_output<F>(path: &Path, overwrite: bool, write: F) -> Result<()>
where
    F: FnOnce(&mut dyn Write) -> Result<()>,
{
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    if !overwrite && path.exists() {
        return Err(format!("目标已存在：{}", path.display()));
    }
    let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
    {
        let mut writer = BufWriter::new(temporary.as_file_mut());
        write(&mut writer)?;
        writer.flush().map_err(|e| e.to_string())?;
    }
    temporary.as_file().sync_all().map_err(|e| e.to_string())?;
    if overwrite {
        temporary.persist(path).map_err(|e| e.to_string())?;
    } else {
        temporary
            .persist_noclobber(path)
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}
pub(crate) fn protect_source(recording: &Recording, output: &Path) -> Result<()> {
    let parent = output
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let absolute = if output.exists() {
        fs::canonicalize(output).map_err(|e| e.to_string())?
    } else {
        fs::canonicalize(parent)
            .map_err(|e| e.to_string())?
            .join(output.file_name().ok_or("输出文件名无效")?)
    };
    let source = fs::canonicalize(&recording.path).map_err(|e| e.to_string())?;
    if absolute == source
        || (source.is_dir()
            && (absolute == source.join("raw.f64") || absolute == source.join("metadata.json")))
    {
        return Err("导出输出不能覆盖原始数据或元数据".into());
    }
    Ok(())
}
pub(crate) fn csv_row(w: &mut dyn Write, cells: &[String], delimiter: char) -> Result<()> {
    for (index, cell) in cells.iter().enumerate() {
        if index > 0 {
            write!(w, "{delimiter}").map_err(|e| e.to_string())?;
        }
        if cell.contains([delimiter, '"', '\r', '\n']) {
            write!(w, "\"{}\"", cell.replace('"', "\"\"")).map_err(|e| e.to_string())?;
        } else {
            write!(w, "{cell}").map_err(|e| e.to_string())?;
        }
    }
    writeln!(w).map_err(|e| e.to_string())
}
pub(crate) fn delimiter(request: &Value) -> Result<char> {
    match text(request, "delimiter", ",") {
        "," => Ok(','),
        "\t" => Ok('\t'),
        _ => Err("分隔符须为逗号或制表符".into()),
    }
}

#[derive(Default)]
struct Moments {
    count: u64,
    mean: f64,
    m2: f64,
    min: f64,
    max: f64,
}
impl Moments {
    fn add(&mut self, x: f64) {
        self.count += 1;
        if self.count == 1 {
            self.mean = x;
            self.min = x;
            self.max = x;
            return;
        }
        let delta = x - self.mean;
        self.mean += delta / self.count as f64;
        self.m2 += delta * (x - self.mean);
        self.min = self.min.min(x);
        self.max = self.max.max(x);
    }
}
pub fn statistics(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let r = Recording::from_request(request)?;
    let (start, stop, columns) = selection(&r, request)?;
    if start == stop {
        return Err("统计区间至少需要一个样本".into());
    }
    let values = text(request, "values", "engineering");
    if !["raw", "engineering"].contains(&values) {
        return Err("统计 values 必须为 raw 或 engineering".into());
    }
    let mut accum: Vec<_> = columns.iter().map(|_| Moments::default()).collect();
    let mut first = start;
    while first < stop {
        control.check(first - start, stop - start)?;
        let count = (stop - first).min(8192);
        let raw = r.read(first, count, &columns)?;
        for ((samples, moments), column) in raw.iter().zip(&mut accum).zip(&columns) {
            for v in samples {
                let value = if values == "raw" {
                    *v
                } else {
                    r.channels[*column].convert(*v)
                };
                if !value.is_finite() {
                    return Err("工程量换算溢出".into());
                }
                moments.add(value);
            }
        }
        first += count;
    }
    control.check(stop - start, stop - start)?;
    let channels:Vec<_>=columns.iter().zip(accum).map(|(column,m)|{
        let c=&r.channels[*column];let std=(m.m2/m.count as f64).max(0.0).sqrt();
        json!({"index":c.index,"name":c.name,"unit":if values=="raw"{"V"}else{&c.unit},"count":m.count,
            "min":m.min,"max":m.max,"delta":m.max-m.min,"mean":m.mean,"std":std,"rms":m.mean.hypot(std)})
    }).collect();
    Ok(json!({"path":r.path,"start":start,"stop":stop,"values":values,"statistics":channels}))
}

fn time_cell(
    index: u64,
    rate: f64,
    format: &str,
    epoch: Option<f64>,
    utc: Option<time::OffsetDateTime>,
) -> Result<String> {
    let seconds = index as f64 / rate;
    match format {
        "seconds" => Ok(seconds.to_string()),
        "milliseconds" => Ok((seconds * 1e3).to_string()),
        "microseconds" => Ok((seconds * 1e6).to_string()),
        "samples" => Ok(index.to_string()),
        "excel" => Ok(
            ((epoch.ok_or("Excel 绝对时间需要可靠的起点时间")? + seconds) / 86400.0 + 25569.0)
                .to_string(),
        ),
        "datetime" => utc
            .ok_or("绝对时间需要可靠的起点时间")?
            .checked_add(time::Duration::nanoseconds((seconds * 1e9).round() as i64))
            .ok_or("绝对时间超出范围")?
            .format(&time::format_description::well_known::Rfc3339)
            .map_err(|e| e.to_string()),
        _ => Err("未知时间格式".into()),
    }
}
pub fn export_csv(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let r = Recording::from_request(request)?;
    let (start, stop, columns) = selection(&r, request)?;
    let output = required_path(request, "output")?;
    protect_source(&r, &output)?;
    let values = text(request, "values", "both");
    if !["raw", "engineering", "both"].contains(&values) {
        return Err("导出 values 无效".into());
    }
    let format = text(request, "time_format", "seconds");
    let heading = match format {
        "seconds" => "time_s",
        "milliseconds" => "time_ms",
        "microseconds" => "time_us",
        "samples" => "sample_index",
        "excel" => "excel_utc",
        "datetime" => "time_utc",
        _ => return Err("未知时间格式".into()),
    };
    if ["excel", "datetime"].contains(&format) && r.epoch().is_none() {
        return Err("记录没有可靠绝对时间".into());
    }
    let delimiter = delimiter(request)?;
    let mut metadata = r.metadata.clone();
    metadata["export"] = json!({"scan_start":start,"scan_stop":stop,"exported_sample_count":stop-start,
        "channel_indices":columns.iter().map(|c|r.channels[*c].index).collect::<Vec<_>>(),"values":values,"time_format":format,"delimiter":delimiter.to_string()});
    let stats = if request["include_statistics"] == true {
        Some(statistics(
            &json!({"path":r.path,"start":start,"stop":stop,"channels":request["channels"],"values":if values=="raw"{"raw"}else{"engineering"}}),
            control,
        )?)
    } else {
        None
    };
    atomic_output(&output, request["overwrite"] == true, |w| {
        write!(w, "\u{feff}# labdaq_metadata: {}\n", metadata).map_err(|e| e.to_string())?;
        let mut headings = vec![heading.to_string()];
        if values != "engineering" {
            for c in &columns {
                headings.push(format!(
                    "{}[AI{}]_raw_V",
                    r.channels[*c].name, r.channels[*c].index
                ));
            }
        }
        if values != "raw" {
            for c in &columns {
                headings.push(format!(
                    "{}[AI{}]_{}",
                    r.channels[*c].name, r.channels[*c].index, r.channels[*c].unit
                ));
            }
        }
        csv_row(w, &headings, delimiter)?;
        let mut first = start;
        while first < stop {
            control.check(first - start, stop - start)?;
            let count = (stop - first).min(8192);
            let raw = r.read(first, count, &columns)?;
            let indices = r.indices(first, count);
            for i in 0..count as usize {
                let mut row = vec![time_cell(
                    indices[i],
                    r.sample_rate,
                    format,
                    r.epoch(),
                    r.utc_start(),
                )?];
                if values != "engineering" {
                    for samples in &raw {
                        row.push(samples[i].to_string());
                    }
                }
                if values != "raw" {
                    for (samples, c) in raw.iter().zip(&columns) {
                        let value = r.channels[*c].convert(samples[i]);
                        if !value.is_finite() {
                            return Err("工程量换算溢出".into());
                        }
                        row.push(value.to_string());
                    }
                }
                csv_row(w, &row, delimiter)?;
            }
            first += count;
        }
        if let Some(stats) = &stats {
            writeln!(w, "# labdaq_statistics: {}", stats["statistics"])
                .map_err(|e| e.to_string())?;
        }
        control.check(stop - start, stop - start)
    })?;
    Ok(json!({"output":output,"sample_count":stop-start,"channel_count":columns.len()}))
}

fn safe_name(value: &str) -> String {
    let result: String = value
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect();
    let result = result.trim_matches([' ', '.']);
    if result.is_empty() {
        "recording".into()
    } else {
        result.into()
    }
}
pub fn batch_export(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let paths = request["paths"]
        .as_array()
        .ok_or("paths 必须是记录路径数组")?;
    let output = required_path(request, "output_dir")?;
    fs::create_dir_all(&output).map_err(|e| e.to_string())?;
    let mut completed = Vec::new();
    let mut errors = Vec::new();
    for (index, path) in paths.iter().enumerate() {
        if let Err(error) = control.check(index as u64, paths.len() as u64) {
            return Ok(
                json!({"completed":completed,"errors":errors,"cancelled":true,"error":error}),
            );
        }
        let result = (|| -> Result<Value> {
            let path = PathBuf::from(path.as_str().ok_or("记录路径必须为文本")?);
            let r = Recording::open(&path)?;
            if !r.is_finalized() || r.is_abnormal() {
                return Err("批量导出仅接受正常收尾的记录".into());
            }
            let stem = safe_name(
                path.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .as_ref(),
            );
            let extension = if text(request, "delimiter", ",") == "\t" {
                "txt"
            } else {
                "csv"
            };
            let destination = output.join(format!("{stem}.{extension}"));
            let mut single = request.clone();
            single["path"] = json!(path);
            single["output"] = json!(destination);
            single["overwrite"] = json!(false);
            export_csv(
                &single,
                &OperationControl {
                    cancelled: control.cancelled,
                    progress: None,
                },
            )
        })();
        match result {
            Ok(v) => completed.push(v),
            Err(error) => errors.push(json!({"path":path,"error":error})),
        }
        if control
            .cancelled
            .is_some_and(|v| v.load(std::sync::atomic::Ordering::Relaxed))
        {
            return Ok(json!({"completed":completed,"errors":errors,"cancelled":true}));
        }
    }
    control.check(paths.len() as u64, paths.len() as u64)?;
    Ok(json!({"completed":completed,"errors":errors,"cancelled":false}))
}

pub(crate) fn numbers(v: &Value, key: &str) -> Result<Vec<f64>> {
    v[key]
        .as_array()
        .ok_or_else(|| format!("{key} 必须为数组"))?
        .iter()
        .map(|x| {
            x.as_f64()
                .filter(|v| v.is_finite())
                .ok_or_else(|| format!("{key} 包含无效数值"))
        })
        .collect()
}
pub fn export_spectrum(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let frequencies = numbers(request, "frequencies")?;
    let traces = request["traces"].as_array().ok_or("缺少 traces")?;
    let mut columns = Vec::new();
    let mut headings = vec!["frequency_Hz".to_string()];
    for trace in traces {
        for field in ["values", "real", "imag", "phase", "coherence"] {
            if trace.get(field).is_some() {
                let data = trace[field]
                    .as_array()
                    .ok_or_else(|| format!("{field} 必须为数组"))?
                    .iter()
                    .map(|value| {
                        if value.is_null() {
                            Ok(None)
                        } else {
                            value
                                .as_f64()
                                .filter(|v| v.is_finite())
                                .map(Some)
                                .ok_or_else(|| format!("{field} 包含非数值内容"))
                        }
                    })
                    .collect::<Result<Vec<Option<f64>>>>()?;
                if data.len() != frequencies.len() {
                    return Err("频谱列长度不一致".into());
                }
                let unit = trace
                    .get("field_units")
                    .and_then(|v| v.get(field))
                    .and_then(Value::as_str)
                    .unwrap_or(text(trace, "unit", ""));
                headings.push(format!(
                    "{}_{field}[{}]",
                    text(trace, "name", "signal"),
                    unit
                ));
                columns.push(data);
            }
        }
    }
    if columns.is_empty() {
        return Err("没有频谱数据可导出".into());
    }
    let output = required_path(request, "output")?;
    let delimiter = delimiter(request)?;
    atomic_output(&output, request["overwrite"] == true, |w| {
        write!(w,"\u{feff}# labdaq_metadata: {}\n",json!({"format":"labdaq-spectrum-v1","settings":request["settings"],"source":request["source"],"created_at":now(),"undefined":"empty cell"})).map_err(|e|e.to_string())?;
        csv_row(w, &headings, delimiter)?;
        for i in 0..frequencies.len() {
            if i % 4096 == 0 {
                control.check(i as u64, frequencies.len() as u64)?;
            }
            let mut row = vec![frequencies[i].to_string()];
            row.extend(
                columns
                    .iter()
                    .map(|c| c[i].map(|v| v.to_string()).unwrap_or_default()),
            );
            csv_row(w, &row, delimiter)?;
        }
        control.check(frequencies.len() as u64, frequencies.len() as u64)
    })?;
    Ok(json!({"output":output,"frequency_count":frequencies.len()}))
}

fn ascii(value: &str, width: usize) -> String {
    value
        .chars()
        .map(|c| {
            if c.is_ascii() && !c.is_control() {
                c
            } else {
                '?'
            }
        })
        .take(width)
        .collect()
}
fn field(w: &mut dyn Write, specific: i32, label: &str, unit: &str) -> Result<()> {
    writeln!(
        w,
        "{:>10}{:>5}{:>5}{:>5} {:<20} {:<20}",
        specific,
        0,
        0,
        0,
        ascii(label, 20),
        ascii(unit, 20)
    )
    .map_err(|e| e.to_string())
}
fn eformat(value: f64, width: usize, precision: usize) -> String {
    let raw = format!("{:.*E}", precision, value);
    let (mantissa, exponent) = raw.split_once('E').unwrap();
    let exp: i32 = exponent.parse().unwrap();
    format!("{:>width$}", format!("{mantissa}E{exp:+03}"), width = width)
}
fn direction(value: &Value) -> Result<i64> {
    if value.is_null() {
        return Ok(0);
    }
    if let Some(number) = value.as_i64() {
        return if (-6..=6).contains(&number) {
            Ok(number)
        } else {
            Err("UFF 方向必须为 -6 到 6".into())
        };
    }
    match value
        .as_str()
        .map(str::trim)
        .map(str::to_ascii_uppercase)
        .as_deref()
    {
        Some("" | "NONE" | "SCALAR") => Ok(0),
        Some("X" | "+X" | "X+") => Ok(1),
        Some("-X" | "X-") => Ok(-1),
        Some("Y" | "+Y" | "Y+") => Ok(2),
        Some("-Y" | "Y-") => Ok(-2),
        Some("Z" | "+Z" | "Z+") => Ok(3),
        Some("-Z" | "Z-") => Ok(-3),
        Some("RX" | "+RX" | "RX+") => Ok(4),
        Some("-RX" | "RX-") => Ok(-4),
        Some("RY" | "+RY" | "RY+") => Ok(5),
        Some("-RY" | "RY-") => Ok(-5),
        Some("RZ" | "+RZ" | "RZ+") => Ok(6),
        Some("-RZ" | "RZ-") => Ok(-6),
        _ => Err("无法识别 UFF 测点方向".into()),
    }
}
/// ASCII dataset 58. Uneven spacing is used when x is not a regular grid.
/// Non-ASCII display names are transliterated to '?' because UFF is ASCII;
/// original names remain in the supplied source metadata / native recording.
pub fn export_uff(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let x = if request.get("frequencies").is_some() {
        numbers(request, "frequencies")?
    } else {
        numbers(request, "x")?
    };
    if x.is_empty() || x.windows(2).any(|w| w[1] <= w[0]) {
        return Err("UFF 横轴必须非空且严格递增".into());
    }
    let traces = request["traces"].as_array().ok_or("UFF 缺少 traces")?;
    if traces.is_empty() {
        return Err("UFF 至少需要一条曲线".into());
    }
    let dx = if x.len() > 1 { x[1] - x[0] } else { 0.0 };
    let even = x
        .iter()
        .enumerate()
        .all(|(i, v)| (*v - (x[0] + i as f64 * dx)).abs() <= 1e-9 * dx.abs().max(1e-12));
    let output = required_path(request, "output")?;
    atomic_output(&output, request["overwrite"] == true, |w| {
        for (index, t) in traces.iter().enumerate() {
            control.check(index as u64, traces.len() as u64)?;
            let re = if t.get("real").is_some() {
                numbers(t, "real")?
            } else {
                numbers(t, "values")?
            };
            let im = if t.get("imag").is_some() {
                Some(numbers(t, "imag")?)
            } else {
                None
            };
            if re.len() != x.len() || im.as_ref().is_some_and(|v| v.len() != x.len()) {
                return Err("UFF 曲线长度与横轴不一致".into());
            }
            writeln!(w, "    -1\n    58").map_err(|e| e.to_string())?;
            for s in [
                text(t, "name", "LabDAQ"),
                text(request, "description", "NONE"),
                &now(),
                "LabDAQ Rust",
                "NONE",
            ] {
                writeln!(w, "{}", ascii(s, 80)).map_err(|e| e.to_string())?;
            }
            let function = integer(t, "function_type", if im.is_some() { 4 } else { 12 })?;
            let response = integer(t, "point_number", integer(t, "channel_index", 0)? + 1)?;
            let reference = integer(t, "reference_point", 0)?;
            let response_direction = direction(&t["direction"])?;
            let refdir = direction(&t["reference_direction"])?;
            writeln!(
                w,
                "{:>5}{:>10}{:>5}{:>10} {:<10}{:>10}{:>4} {:<10}{:>10}{:>4}",
                function,
                index + 1,
                1,
                0,
                "LabDAQ",
                response,
                response_direction,
                "NONE",
                reference,
                refdir
            )
            .map_err(|e| e.to_string())?;
            writeln!(
                w,
                "{:>10}{:>10}{:>10}{}{}{}",
                if im.is_some() { 6 } else { 4 },
                x.len(),
                i32::from(even),
                eformat(if even { x[0] } else { 0.0 }, 13, 5),
                eformat(if even { dx } else { 0.0 }, 13, 5),
                eformat(0.0, 13, 5)
            )
            .map_err(|e| e.to_string())?;
            let frequency =
                request.get("frequencies").is_some() || text(request, "x_unit", "Hz") == "Hz";
            field(
                w,
                if frequency { 18 } else { 17 },
                if frequency { "Frequency" } else { "Time" },
                if frequency { "Hz" } else { "s" },
            )?;
            field(w, 1, text(t, "name", "Response"), text(t, "unit", "NONE"))?;
            field(
                w,
                if im.is_some() { 1 } else { 0 },
                "Reference",
                text(t, "reference_unit", "NONE"),
            )?;
            field(w, 0, "NONE", "NONE")?;
            if even {
                let mut line = 0;
                for i in 0..x.len() {
                    write!(w, "{}", eformat(re[i], 20, 12)).map_err(|e| e.to_string())?;
                    line += 1;
                    if let Some(im) = &im {
                        write!(w, "{}", eformat(im[i], 20, 12)).map_err(|e| e.to_string())?;
                        line += 1;
                    }
                    if line == 4 {
                        writeln!(w).map_err(|e| e.to_string())?;
                        line = 0;
                    }
                }
                if line > 0 {
                    writeln!(w).map_err(|e| e.to_string())?;
                }
            } else {
                for i in 0..x.len() {
                    write!(w, "{}{}", eformat(x[i], 13, 5), eformat(re[i], 20, 12))
                        .map_err(|e| e.to_string())?;
                    if let Some(im) = &im {
                        write!(w, "{}", eformat(im[i], 20, 12)).map_err(|e| e.to_string())?;
                    }
                    writeln!(w).map_err(|e| e.to_string())?;
                }
            }
            writeln!(w, "    -1").map_err(|e| e.to_string())?;
        }
        control.check(traces.len() as u64, traces.len() as u64)
    })?;
    Ok(json!({"output":output,"datasets":traces.len(),"format":"UFF ASCII 58","even_spacing":even}))
}
