use crate::{OperationControl, Result, epoch, integer, now, required_path, write_json};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

fn thousand() -> f64 {
    1000.0
}
fn volts() -> String {
    "V".into()
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Channel {
    pub index: usize,
    pub name: String,
    #[serde(default = "volts")]
    pub unit: String,
    #[serde(default = "thousand")]
    pub sensitivity_mv_per_unit: f64,
    #[serde(default)]
    pub zero_volts: f64,
    #[serde(default)]
    pub inverted: bool,
    #[serde(default)]
    pub point_number: Option<u32>,
    #[serde(default)]
    pub direction: String,
    #[serde(default)]
    pub engineering_offset: f64,
}
impl Channel {
    pub fn convert(&self, value: f64) -> f64 {
        (value - self.zero_volts)
            * if self.unit == "V" {
                1.0
            } else {
                1000.0 / self.sensitivity_mv_per_unit
            }
            * if self.inverted { -1.0 } else { 1.0 }
            + self.engineering_offset
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Segment {
    pub file_start_scan: u64,
    pub source_start_scan: u64,
    pub sample_count: u64,
}

#[derive(Clone, Debug)]
pub(crate) struct HpfChunk {
    pub start: u64,
    pub count: u64,
    pub offsets: Vec<u64>,
}

#[derive(Clone, Debug)]
pub struct Recording {
    pub path: PathBuf,
    pub metadata: Value,
    pub channels: Vec<Channel>,
    pub sample_rate: f64,
    pub sample_count: u64,
    pub segments: Vec<Segment>,
    pub(crate) hpf_chunks: Option<Vec<HpfChunk>>,
}
impl Recording {
    /// Apply optional conversion overrides to derived values without editing the
    /// acquisition snapshot or stored voltages. Affected outputs carry both.
    pub fn from_request(request: &Value) -> Result<Self> {
        let mut recording = Self::open(required_path(request, "path")?)?;
        if let Some(overrides) = request.get("channel_overrides").filter(|v| !v.is_null()) {
            let entries = overrides
                .as_array()
                .ok_or("channel_overrides 必须为通道设置数组")?;
            let mut seen = std::collections::HashSet::new();
            for entry in entries {
                let index = entry["index"].as_u64().ok_or("换算覆盖缺少物理通道编号")? as usize;
                if !seen.insert(index) {
                    return Err("换算覆盖通道重复".into());
                }
                let column = recording
                    .channels
                    .iter()
                    .position(|v| v.index == index)
                    .ok_or("换算覆盖通道不在原记录中")?;
                let mut merged =
                    serde_json::to_value(&recording.channels[column]).map_err(|e| e.to_string())?;
                for (key, value) in entry.as_object().ok_or("换算覆盖须为对象")? {
                    if ![
                        "index",
                        "name",
                        "unit",
                        "sensitivity_mv_per_unit",
                        "zero_volts",
                        "inverted",
                        "point_number",
                        "direction",
                        "engineering_offset",
                    ]
                    .contains(&key.as_str())
                    {
                        return Err(format!("不支持的换算设置：{key}"));
                    }
                    merged[key] = value.clone();
                }
                recording.channels[column] =
                    serde_json::from_value(merged).map_err(|e| e.to_string())?;
            }
            Self::validate_channels(&recording.channels)?;
            recording.metadata["processing_channel_overrides"] = json!(recording.channels);
        }
        Ok(recording)
    }
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        if path
            .extension()
            .is_some_and(|v| v.eq_ignore_ascii_case("hpf"))
        {
            return crate::hpf::open(path);
        }
        let mut metadata: Value = serde_json::from_slice(
            &fs::read(path.join("metadata.json")).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        if metadata["format"] != "labdaq-raw-voltage"
            || metadata["format_version"] != 1
            || metadata["dtype"] != "<f8"
            || metadata["layout"] != "sample-major"
        {
            return Err("不支持的 LabDAQ 原生记录格式".into());
        }
        let channels: Vec<Channel> =
            serde_json::from_value(metadata["channels"].clone()).map_err(|e| e.to_string())?;
        Self::validate_channels(&channels)?;
        let sample_rate = metadata["sample_rate"]
            .as_f64()
            .filter(|v| v.is_finite() && *v > 0.0)
            .ok_or("记录采样率无效")?;
        let bytes = fs::metadata(path.join("raw.f64"))
            .map_err(|e| e.to_string())?
            .len();
        let width_bytes = channels.len() as u64 * 8;
        let sample_count = bytes / width_bytes;
        let remainder = bytes % width_bytes;
        let finalized = metadata["finalized"]
            .as_bool()
            .ok_or("记录缺少 finalized 状态")?;
        if finalized && (remainder != 0 || metadata["sample_count"].as_u64() != Some(sample_count))
        {
            return Err("已收尾记录长度与元数据不一致".into());
        }
        let origin = metadata["acquisition_config"]["record_start_scan"]
            .as_u64()
            .unwrap_or(0);
        let mut segments: Vec<Segment> = if metadata.get("recording_segments").is_some() {
            serde_json::from_value(metadata["recording_segments"].clone())
                .map_err(|e| e.to_string())?
        } else if sample_count > 0 {
            vec![Segment {
                file_start_scan: 0,
                source_start_scan: origin,
                sample_count,
            }]
        } else {
            vec![]
        };
        // On an interrupted writer the published active boundary identifies all
        // additional complete frames; it never establishes continuity across a pause.
        if !finalized && metadata["active_segment"].is_object() {
            let active = &metadata["active_segment"];
            let first = active["file_start_scan"]
                .as_u64()
                .ok_or("活动分段起点无效")?;
            let source = active["source_start_scan"]
                .as_u64()
                .ok_or("活动源样本起点无效")?;
            if first > sample_count {
                return Err("活动分段超出原始文件".into());
            }
            segments.retain(|s| s.file_start_scan < first);
            if sample_count > first {
                segments.push(Segment {
                    file_start_scan: first,
                    source_start_scan: source,
                    sample_count: sample_count - first,
                });
            }
        }
        let mut mapped = 0u64;
        let mut previous_end = origin;
        for segment in &segments {
            if segment.file_start_scan != mapped
                || segment.sample_count == 0
                || segment.source_start_scan < previous_end
            {
                return Err("记录分段不连续或源样本位置倒退".into());
            }
            mapped = mapped
                .checked_add(segment.sample_count)
                .ok_or("记录长度溢出")?;
            previous_end = segment
                .source_start_scan
                .checked_add(segment.sample_count)
                .ok_or("源样本位置溢出")?;
        }
        if mapped != sample_count {
            return Err("原始记录与已保存分段不完整".into());
        }
        metadata["sample_count"] = json!(sample_count);
        metadata["recording_segments"] = json!(segments);
        if remainder != 0 {
            metadata["incomplete_tail_bytes"] = json!(remainder);
        }
        Ok(Self {
            path: path.into(),
            metadata,
            channels,
            sample_rate,
            sample_count,
            segments,
            hpf_chunks: None,
        })
    }

    pub(crate) fn validate_channels(channels: &[Channel]) -> Result<()> {
        if channels.is_empty() || channels.len() > 256 {
            return Err("记录通道数无效".into());
        }
        let mut indices = std::collections::HashSet::new();
        for channel in channels {
            if !indices.insert(channel.index)
                || (channel.unit != "V" && channel.sensitivity_mv_per_unit <= 0.0)
                || ![
                    channel.sensitivity_mv_per_unit,
                    channel.zero_volts,
                    channel.engineering_offset,
                ]
                .iter()
                .all(|v| v.is_finite())
            {
                return Err("通道编号重复或换算参数无效".into());
            }
        }
        Ok(())
    }

    pub fn info(&self) -> Value {
        json!({"path":self.path,"metadata":self.metadata,"channels":self.channels,
            "sample_rate":self.sample_rate,"sample_count":self.sample_count,
            "duration_seconds":self.sample_count as f64 / self.sample_rate,
            "elapsed_seconds":self.indices(self.sample_count.saturating_sub(1),1).first().map(|v|(*v+1) as f64/self.sample_rate).unwrap_or(0.0),
            "finalized":self.is_finalized(),"abnormal":self.is_abnormal()})
    }
    pub fn is_finalized(&self) -> bool {
        self.hpf_chunks.is_some() || self.metadata["finalized"] == true
    }
    pub fn is_abnormal(&self) -> bool {
        self.metadata["error"]
            .as_str()
            .is_some_and(|s| !s.is_empty())
            || matches!(
                self.metadata["termination_reason"].as_str(),
                Some("error" | "acquisition_error")
            )
    }
    pub fn epoch(&self) -> Option<f64> {
        epoch(&self.metadata["acquisition_started_at_utc"])
            .map(|t| t + self.origin() as f64 / self.sample_rate)
            .or_else(|| epoch(&self.metadata["started_at_utc"]))
    }
    pub fn utc_start(&self) -> Option<time::OffsetDateTime> {
        let parse = |value: &Value| {
            time::OffsetDateTime::parse(
                value.as_str()?,
                &time::format_description::well_known::Rfc3339,
            )
            .ok()
        };
        parse(&self.metadata["acquisition_started_at_utc"])
            .and_then(|t| {
                t.checked_add(time::Duration::seconds_f64(
                    self.origin() as f64 / self.sample_rate,
                ))
            })
            .or_else(|| parse(&self.metadata["started_at_utc"]))
    }
    pub fn origin(&self) -> u64 {
        self.metadata["acquisition_config"]["record_start_scan"]
            .as_u64()
            .unwrap_or(0)
    }
    pub fn columns(&self, selection: Option<&Value>) -> Result<Vec<usize>> {
        let Some(selection) = selection.filter(|v| !v.is_null()) else {
            return Ok((0..self.channels.len()).collect());
        };
        let indices = selection
            .as_array()
            .ok_or("channels 必须为物理通道编号数组")?;
        let mut result = Vec::new();
        for index in indices {
            let index = index.as_u64().ok_or("通道编号必须为非负整数")? as usize;
            let column = self
                .channels
                .iter()
                .position(|c| c.index == index)
                .ok_or("所选通道不在记录中")?;
            if result.contains(&column) {
                return Err("通道选择不能重复".into());
            }
            result.push(column);
        }
        if result.is_empty() {
            return Err("至少选择一个通道".into());
        }
        Ok(result)
    }
    pub fn indices(&self, start: u64, count: u64) -> Vec<u64> {
        let stop = start.saturating_add(count).min(self.sample_count);
        let mut result = Vec::with_capacity(stop.saturating_sub(start) as usize);
        for segment in &self.segments {
            let first = start.max(segment.file_start_scan);
            let last = stop.min(segment.file_start_scan + segment.sample_count);
            if last > first {
                let source =
                    segment.source_start_scan - self.origin() + first - segment.file_start_scan;
                result.extend(source..source + last - first);
            }
        }
        result
    }
    pub fn frame_at_time(&self, seconds: f64) -> u64 {
        for s in &self.segments {
            let source = s.source_start_scan - self.origin();
            if seconds <= source as f64 / self.sample_rate {
                return s.file_start_scan;
            }
            let end = source + s.sample_count;
            if seconds <= (end - 1) as f64 / self.sample_rate {
                let mut offset = (seconds * self.sample_rate - source as f64)
                    .ceil()
                    .clamp(0.0, s.sample_count as f64) as u64;
                if offset < s.sample_count
                    && ((source + offset) as f64 / self.sample_rate) < seconds
                {
                    offset += 1;
                }
                if offset > 0 && ((source + offset - 1) as f64 / self.sample_rate) >= seconds {
                    offset -= 1;
                }
                return s.file_start_scan + offset;
            }
        }
        self.sample_count
    }
    /// Channel-major raw voltages, bounded to at most 200,000 frames per call.
    pub fn read(&self, start: u64, count: u64, columns: &[usize]) -> Result<Vec<Vec<f64>>> {
        if columns.iter().any(|c| *c >= self.channels.len()) {
            return Err("无效通道列".into());
        }
        if count > 200_000 {
            return Err("单次读取上限为 200000 帧，请分块读取".into());
        }
        if let Some(error) = self.metadata["voltage_replay_error"]
            .as_str()
            .filter(|s| !s.is_empty())
        {
            return Err(error.into());
        }
        let count = count.min(self.sample_count.saturating_sub(start));
        let mut output = vec![vec![0.0; count as usize]; columns.len()];
        let mut file = File::open(if self.hpf_chunks.is_some() {
            self.path.clone()
        } else {
            self.path.join("raw.f64")
        })
        .map_err(|e| e.to_string())?;
        if let Some(chunks) = &self.hpf_chunks {
            for chunk in chunks {
                let first = start.max(chunk.start);
                let last = (start + count).min(chunk.start + chunk.count);
                if last <= first {
                    continue;
                }
                for (dst, col) in columns.iter().enumerate() {
                    file.seek(SeekFrom::Start(
                        chunk.offsets[*col] + (first - chunk.start) * 8,
                    ))
                    .map_err(|e| e.to_string())?;
                    let mut bytes = vec![0u8; ((last - first) * 8) as usize];
                    file.read_exact(&mut bytes).map_err(|e| e.to_string())?;
                    for (i, b) in bytes.chunks_exact(8).enumerate() {
                        output[dst][(first - start) as usize + i] =
                            f64::from_le_bytes(b.try_into().unwrap());
                    }
                }
            }
        } else {
            let width = self.channels.len();
            file.seek(SeekFrom::Start(start * width as u64 * 8))
                .map_err(|e| e.to_string())?;
            let mut bytes = vec![0u8; count as usize * width * 8];
            file.read_exact(&mut bytes).map_err(|e| e.to_string())?;
            for (i, row) in bytes.chunks_exact(width * 8).enumerate() {
                for (dst, col) in columns.iter().enumerate() {
                    output[dst][i] =
                        f64::from_le_bytes(row[col * 8..col * 8 + 8].try_into().unwrap());
                }
            }
        }
        if output.iter().flatten().any(|v| !v.is_finite()) {
            return Err("数据包含 NaN 或无穷值".into());
        }
        Ok(output)
    }
    pub fn window_json(
        &self,
        start: u64,
        count: u64,
        columns: &[usize],
        values: &str,
    ) -> Result<Value> {
        if !["raw", "engineering", "both"].contains(&values) {
            return Err("values 必须为 raw、engineering 或 both".into());
        }
        let raw = self.read(start, count, columns)?;
        let n = raw.first().map(Vec::len).unwrap_or(0);
        let indices = self.indices(start, n as u64);
        let times: Vec<_> = indices
            .iter()
            .map(|v| *v as f64 / self.sample_rate)
            .collect();
        let gaps: Vec<_> = indices
            .windows(2)
            .enumerate()
            .filter_map(|(i, w)| (w[1] != w[0] + 1).then_some(i + 1))
            .collect();
        let channels: Vec<_> = columns.iter().map(|c| self.channels[*c].clone()).collect();
        let samples: Vec<Vec<f64>> = raw
            .iter()
            .zip(&channels)
            .map(|(v, c)| {
                v.iter()
                    .map(|x| if values == "raw" { *x } else { c.convert(*x) })
                    .collect()
            })
            .collect();
        if samples.iter().flatten().any(|v| !v.is_finite()) {
            return Err("工程量换算溢出".into());
        }
        Ok(
            json!({"path":self.path,"metadata":self.metadata,"sample_rate":self.sample_rate,"start":start,"count":n,
            "sample_count":self.sample_count,"channels":channels,"raw":raw,"samples":samples,"sample_indices":indices,"times":times,"gaps":gaps,"values":values}),
        )
    }
}

pub fn update_metadata(request: &Value) -> Result<Value> {
    let recording = Recording::open(required_path(request, "path")?)?;
    if recording.hpf_chunks.is_some() || !recording.is_finalized() {
        return Err("仅可编辑已收尾原生记录的描述信息".into());
    }
    let mut metadata = recording.metadata.clone();
    if let Some(experiment) = request.get("experiment") {
        let name = experiment
            .as_str()
            .filter(|v| !v.trim().is_empty())
            .ok_or("实验名称不能为空")?;
        metadata["experiment"] = json!(name.trim());
    }
    if !metadata["recording_options"].is_object() {
        metadata["recording_options"] = json!({});
    }
    for key in ["specimen", "condition", "notes"] {
        if let Some(value) = request.get(key) {
            metadata["recording_options"][key] = json!(value.as_str().ok_or("描述字段必须为文本")?);
        }
    }
    metadata["description_updated_at_utc"] = json!(now());
    write_json(&recording.path.join("metadata.json"), &metadata, true)?;
    Ok(Recording::open(recording.path)?.info())
}

pub fn list_recordings(root: &Path, query: &str) -> Result<Value> {
    if !root.exists() {
        return Ok(json!({"recordings":[]}));
    }
    let mut paths = Vec::new();
    for entry in fs::read_dir(root).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.is_dir() {
            if path.join("metadata.json").is_file() {
                paths.push(path);
            } else {
                for child in fs::read_dir(&path).map_err(|e| e.to_string())? {
                    let child = child.map_err(|e| e.to_string())?.path();
                    if child.is_dir() && child.join("metadata.json").is_file() {
                        paths.push(child);
                    }
                }
            }
        } else if path
            .extension()
            .is_some_and(|v| v.eq_ignore_ascii_case("hpf"))
        {
            paths.push(path);
        }
    }
    let query = query.to_lowercase();
    let mut entries = Vec::new();
    for path in paths {
        let mut value = match Recording::open(&path) {
            Ok(r) => {
                let mut v = r.info();
                v["batch_exportable"] = json!(r.is_finalized() && !r.is_abnormal());
                v["status"] = json!(if !r.is_finalized() {
                    "未正常收尾"
                } else if r.is_abnormal() {
                    "异常结束"
                } else {
                    "已结束"
                });
                v
            }
            Err(e) => json!({"path":path,"error":e,"status":"不可打开","batch_exportable":false}),
        };
        value["relative_path"] = json!(path.strip_prefix(root).unwrap_or(&path));
        if value.to_string().to_lowercase().contains(&query) {
            entries.push(value);
        }
    }
    entries.sort_by(|a, b| {
        let a_time = epoch(&a["metadata"]["started_at_utc"]).unwrap_or(0.0);
        let b_time = epoch(&b["metadata"]["started_at_utc"]).unwrap_or(0.0);
        b_time.total_cmp(&a_time).then_with(|| {
            b["relative_path"]
                .as_str()
                .cmp(&a["relative_path"].as_str())
        })
    });
    Ok(json!({"recordings":entries}))
}

pub fn recover(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let r = Recording::open(required_path(request, "path")?)?;
    if r.hpf_chunks.is_some() || r.is_finalized() {
        return Err("恢复仅用于未收尾的原生记录".into());
    }
    let destination = required_path(request, "output")?;
    let parent = destination.parent().ok_or("恢复目录没有父目录")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    if destination.exists() {
        return Err("恢复目标已存在，不能覆盖".into());
    }
    let staging = tempfile::Builder::new()
        .prefix(".labdaq-recovery-")
        .tempdir_in(parent)
        .map_err(|e| e.to_string())?;
    let mut metadata = r.metadata.clone();
    metadata["recovered_from"] = json!(r.path);
    metadata["recovered_at_utc"] = json!(now());
    metadata["finalized"] = json!(false);
    write_json(&staging.path().join("metadata.json"), &metadata, false)?;
    let mut output = File::create(staging.path().join("raw.f64")).map_err(|e| e.to_string())?;
    let columns: Vec<_> = (0..r.channels.len()).collect();
    let mut first = 0;
    while first < r.sample_count {
        control.check(first, r.sample_count)?;
        let count = (r.sample_count - first).min(8192);
        let raw = r.read(first, count, &columns)?;
        for i in 0..count as usize {
            for channel in &raw {
                output
                    .write_all(&channel[i].to_le_bytes())
                    .map_err(|e| e.to_string())?;
            }
        }
        first += count;
    }
    output.flush().map_err(|e| e.to_string())?;
    drop(output);
    control.check(r.sample_count, r.sample_count)?;
    metadata["finalized"] = json!(true);
    metadata["active_segment"] = Value::Null;
    metadata["recording_paused"] = json!(false);
    metadata["termination_reason"] = json!("recovered_incomplete");
    metadata["error"] = json!("原始采集未正常收尾；已恢复完整帧，未补造缺失样本");
    metadata["stopped_at_utc"] = json!(now());
    if let Some(bytes) = metadata
        .as_object_mut()
        .unwrap()
        .remove("incomplete_tail_bytes")
    {
        metadata["recovered_discarded_tail_bytes"] = bytes;
    }
    write_json(&staging.path().join("metadata.json"), &metadata, true)?;
    fs::rename(staging.path(), &destination).map_err(|e| e.to_string())?;
    Ok(Recording::open(destination)?.info())
}

pub(crate) fn selection(r: &Recording, request: &Value) -> Result<(u64, u64, Vec<usize>)> {
    let start = if request.get("start_seconds").is_some() {
        r.frame_at_time(crate::finite(request, "start_seconds", 0.0)?)
    } else {
        integer(request, "start", 0)?
    };
    let stop = if request.get("stop_seconds").is_some() {
        r.frame_at_time(crate::finite(request, "stop_seconds", 0.0)?)
    } else {
        integer(request, "stop", r.sample_count)?
    };
    if start > stop || stop > r.sample_count {
        return Err("区间必须满足 0 ≤ start ≤ stop ≤ sample_count".into());
    }
    Ok((start, stop, r.columns(request.get("channels"))?))
}
