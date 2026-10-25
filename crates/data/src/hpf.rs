//! HPF 0x10001 synchronous Double subset, matching the existing reader.
use crate::exchange::{atomic_output, protect_source};
use crate::session::{HpfChunk, selection};
use crate::{Channel, OperationControl, Recording, Result, Segment, now, required_path};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

const ALIGN: u64 = 65536;
fn read<const N: usize>(f: &mut File) -> Result<[u8; N]> {
    let mut b = [0; N];
    f.read_exact(&mut b).map_err(|e| e.to_string())?;
    Ok(b)
}
fn i32le(f: &mut File) -> Result<i32> {
    Ok(i32::from_le_bytes(read(f)?))
}
fn i64le(f: &mut File) -> Result<i64> {
    Ok(i64::from_le_bytes(read(f)?))
}
fn xml(f: &mut File, size: u64, root: &str) -> Result<String> {
    if size > 16 * 1024 * 1024 {
        return Err("HPF XML 超过 16 MiB".into());
    }
    let mut b = vec![0; size as usize];
    f.read_exact(&mut b).map_err(|e| e.to_string())?;
    while b.last().is_some_and(|v| *v == 0 || v.is_ascii_whitespace()) {
        b.pop();
    }
    let s = String::from_utf8(b).map_err(|e| e.to_string())?;
    let s = s.trim_start_matches('\u{feff}').to_string();
    if s.contains("<!DOCTYPE") || s.contains("<!ENTITY") {
        return Err("HPF XML 不支持 DTD/实体定义".into());
    }
    let doc = roxmltree::Document::parse(&s).map_err(|e| e.to_string())?;
    if doc.root_element().tag_name().name() != root {
        return Err(format!("HPF XML 根节点不是 {root}"));
    }
    Ok(s)
}
fn field<'a, 'input>(node: roxmltree::Node<'a, 'input>, name: &str) -> Option<&'a str> {
    node.children()
        .find(|n| n.has_tag_name(name))
        .and_then(|n| n.text())
}
fn map(node: roxmltree::Node<'_, '_>) -> BTreeMap<String, String> {
    node.children()
        .filter(|n| n.is_element())
        .map(|n| {
            (
                n.tag_name().name().to_string(),
                n.text().unwrap_or("").to_string(),
            )
        })
        .collect()
}
fn number(info: &BTreeMap<String, String>, key: &str) -> Result<f64> {
    info.get(key)
        .and_then(|v| v.parse::<f64>().ok())
        .filter(|v| v.is_finite())
        .ok_or_else(|| format!("HPF 缺少有效 {key}"))
}
fn near(a: f64, b: f64, tolerance: f64) -> bool {
    (a - b).abs() <= tolerance * a.abs().max(b.abs()).max(1e-15)
}

#[derive(Clone)]
struct Directory {
    offset: u64,
    id: i64,
    group: i64,
    start: i64,
    count: i64,
    extra: Value,
}
impl Directory {
    fn index(&self) -> [i64; 5] {
        [
            self.start,
            self.count,
            self.id,
            self.group,
            self.offset as i64,
        ]
    }
    fn value(&self) -> Value {
        json!({"offset":self.offset,"chunk_id":self.id,"group_id":self.group,"start":self.start,"count":self.count,"details":self.extra})
    }
}

pub(crate) fn open(path: &Path) -> Result<Recording> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let length = file.metadata().map_err(|e| e.to_string())?.len();
    let mut offset = 0;
    let mut index_offset = 0;
    let mut directory = Vec::new();
    let mut indices: Option<Vec<[i64; 5]>> = None;
    let mut group = None;
    let mut vendor: Option<Vec<BTreeMap<String, String>>> = None;
    let mut channels = Vec::new();
    let mut chunks = Vec::new();
    let mut configuration = None;
    let mut recording_date = None;
    let mut zoom_map: Option<HashMap<usize, i64>> = None;
    let mut count = 0u64;
    let mut rate = 0.0;
    let mut voltage_error = String::new();
    while offset < length {
        file.seek(SeekFrom::Start(offset))
            .map_err(|e| e.to_string())?;
        let id = i64le(&mut file)?;
        let signed_size = i64le(&mut file)?;
        if signed_size < ALIGN as i64 {
            return Err("HPF 块长度无效".into());
        }
        let size = signed_size as u64;
        if size % ALIGN != 0 || offset.checked_add(size).is_none_or(|v| v > length) {
            return Err("HPF 块必须完整且按 64 KiB 对齐".into());
        }
        if offset == 0 && id != 0x1000 {
            return Err("HPF 必须以头块开始".into());
        }
        let mut entry = Directory {
            offset,
            id,
            group: 0,
            start: -1,
            count: -1,
            extra: Value::Null,
        };
        match id {
            0x1000 => {
                if offset != 0 || read::<4>(&mut file)? != *b"datx" || i64le(&mut file)? != 0x10001
                {
                    return Err("仅支持 datx HPF 0x10001".into());
                }
                index_offset = i64le(&mut file)?;
                let s = xml(&mut file, size - 36, "RecordingDate")?;
                let doc = roxmltree::Document::parse(&s).map_err(|e| e.to_string())?;
                recording_date = Some(doc.root_element().text().unwrap_or("").to_string());
            }
            0x2000 => {
                if vendor.is_some() {
                    return Err("HPF 当前只支持一个同步通道组".into());
                }
                let gid = i32le(&mut file)?;
                let width = i32le(&mut file)?;
                if !(1..=256).contains(&width) {
                    return Err("HPF 通道数无效".into());
                }
                group = Some(gid);
                entry.group = gid as i64;
                let s = xml(&mut file, size - 24, "ChannelInformationData")?;
                let doc = roxmltree::Document::parse(&s).map_err(|e| e.to_string())?;
                let infos: Vec<_> = doc
                    .root_element()
                    .children()
                    .filter(|n| n.is_element())
                    .collect();
                if infos.len() != width as usize
                    || infos.iter().any(|n| !n.has_tag_name("ChannelInformation"))
                {
                    return Err("HPF 通道 XML 数量不匹配".into());
                }
                let infos: Vec<_> = infos.into_iter().map(map).collect();
                let mut data_indexes = HashSet::new();
                let mut start_time = None;
                for (position, info) in infos.iter().enumerate() {
                    if info
                        .get("ChannelType")
                        .is_none_or(|v| !v.eq_ignore_ascii_case("randomdatachannel"))
                        || info.get("DataType").map(String::as_str) != Some("Double")
                        || info.get("AssignedTimeChannelIndex").map(String::as_str) != Some("-1")
                    {
                        return Err("HPF 仅支持没有独立时间通道的 RandomDataChannel/Double".into());
                    }
                    let index = number(info, "DataIndex")?;
                    if index.fract() != 0.0
                        || index < 0.0
                        || index >= width as f64
                        || !data_indexes.insert(index as usize)
                    {
                        return Err("HPF DataIndex 无效或重复".into());
                    }
                    let fs = number(info, "PerChannelSampleRate")?;
                    let dt = number(info, "TimeIncrement")?;
                    if fs <= 0.0
                        || dt <= 0.0
                        || !near(fs * dt, 1.0, 1e-9)
                        || (rate != 0.0 && !near(rate, fs, 1e-12))
                    {
                        return Err("HPF 通道采样率不同或时间间隔不一致".into());
                    }
                    rate = fs;
                    let begin = info.get("StartTime").ok_or("HPF 缺少 StartTime")?;
                    if start_time.as_ref().is_some_and(|v| v != begin) {
                        return Err("HPF 通道起点不同".into());
                    }
                    start_time = Some(begin.clone());
                    let name = info
                        .get("Name")
                        .filter(|v| !v.is_empty())
                        .ok_or("HPF 通道名称为空")?;
                    let unit = info
                        .get("Unit")
                        .filter(|v| !v.is_empty())
                        .ok_or("HPF 通道单位为空")?;
                    let scales = [
                        number(info, "DataScale")?,
                        number(info, "DataOffset")?,
                        number(info, "SensorScale")?,
                        number(info, "SensorOffset")?,
                    ];
                    if unit != "V" {
                        voltage_error = "HPF 非电压通道尚未确认原始电压语义".into();
                    } else if scales != [1.0, 0.0, 1.0, 0.0] && voltage_error.is_empty() {
                        voltage_error = "HPF 含非恒等换算，不能视为原始电压".into();
                    }
                    channels.push(Channel {
                        index: position,
                        name: name.clone(),
                        unit: unit.clone(),
                        sensitivity_mv_per_unit: 1000.0,
                        zero_volts: 0.0,
                        inverted: false,
                        point_number: None,
                        direction: String::new(),
                        engineering_offset: 0.0,
                    });
                }
                vendor = Some(infos);
            }
            0x3000 | 0x8000 | 0x9000 => {
                let infos = vendor.as_ref().ok_or("HPF 数据块之前缺少通道描述")?;
                let gid = i32le(&mut file)?;
                let start = i64le(&mut file)?;
                let width = i32le(&mut file)?;
                if Some(gid) != group || width != infos.len() as i32 || start < 0 {
                    return Err("HPF 数据组或起点无效".into());
                }
                let minimum = 32 + width as u64 * 8;
                let mut descriptors = Vec::new();
                for _ in 0..width {
                    let rel = i32le(&mut file)?;
                    let len = i32le(&mut file)?;
                    if rel < 0
                        || len < 0
                        || (rel as u64) < minimum
                        || len % 8 != 0
                        || rel as u64 + len as u64 > size
                    {
                        return Err("HPF 通道数据偏移/长度无效".into());
                    }
                    descriptors.push((rel as u64, len as u64));
                }
                let mut sorted = descriptors.clone();
                sorted.sort_unstable();
                if sorted.windows(2).any(|w| w[0].0 + w[0].1 > w[1].0) {
                    return Err("HPF 通道数据范围重叠".into());
                }
                let frames = descriptors[0].1 / 8;
                if descriptors.iter().any(|v| v.1 / 8 != frames) {
                    return Err("HPF 通道长度不同".into());
                }
                entry.group = gid as i64;
                entry.start = start;
                entry.count = frames as i64;
                if id == 0x3000 {
                    if start as u64 != count {
                        return Err("HPF 数据块不连续，不补样本或压缩缺口".into());
                    }
                    let offsets = infos
                        .iter()
                        .map(|i| Ok(offset + descriptors[number(i, "DataIndex")? as usize].0))
                        .collect::<Result<Vec<_>>>()?;
                    chunks.push(HpfChunk {
                        start: count,
                        count: frames,
                        offsets,
                    });
                    count = count.checked_add(frames).ok_or("HPF 样本数溢出")?;
                } else if id == 0x8000 {
                    let factor = i64le(&mut file)?;
                    if factor != 8 {
                        return Err("只支持已观察的 HPF 8 倍显示缓存".into());
                    }
                    entry.extra = json!({"role":"quickdaq_zoom_cache","zoom_factor":factor,"descriptors":descriptors});
                } else {
                    if start != 0 || frames != 10 {
                        return Err("只支持已观察的 HPF 10 值汇总块".into());
                    }
                    entry.extra =
                        json!({"role":"quickdaq_summary_uninterpreted","descriptors":descriptors});
                }
            }
            0x6000 => {
                if indices.is_some() || index_offset != offset as i64 {
                    return Err("HPF 索引位置错误或重复".into());
                }
                let n = i64le(&mut file)?;
                if n < 0 || n as u64 > (size - 24) / 40 {
                    return Err("HPF 索引数量超限".into());
                }
                let mut rows = Vec::new();
                for _ in 0..n {
                    let mut row = [0; 5];
                    for v in &mut row {
                        *v = i64le(&mut file)?;
                    }
                    rows.push(row);
                }
                indices = Some(rows);
            }
            0x7000 => {
                if configuration.is_some() {
                    return Err("HPF 配置块重复".into());
                }
                entry.group = i32le(&mut file)? as i64;
                configuration = Some(xml(&mut file, size - 20, "PersitantObjects")?);
            }
            0xA000 => {
                if zoom_map.is_some() {
                    return Err("HPF 缩放映射重复".into());
                }
                entry.group = i32le(&mut file)? as i64;
                let s = xml(&mut file, size - 20, "IndexDescriptorToZoomMap")?;
                let doc = roxmltree::Document::parse(&s).map_err(|e| e.to_string())?;
                let mut result = HashMap::new();
                for item in doc
                    .root_element()
                    .children()
                    .filter(|n| n.has_tag_name("Item"))
                {
                    let nested = |name| {
                        item.children()
                            .find(|n| n.has_tag_name(name))
                            .and_then(|n| field(n, "int"))
                            .and_then(|v| v.parse::<i64>().ok())
                    };
                    let key = nested("Key").filter(|v| *v >= 0).ok_or("HPF 缩放键无效")? as usize;
                    if result
                        .insert(key, nested("Value").ok_or("HPF 缩放值无效")?)
                        .is_some()
                    {
                        return Err("HPF 缩放键重复".into());
                    }
                }
                entry.start = *result.keys().min().ok_or("HPF 缩放映射为空")? as i64;
                entry.extra = json!({"role":"quickdaq_zoom_map","xml":s});
                zoom_map = Some(result);
            }
            _ => {
                return Err(format!(
                    "HPF 不支持块 0x{id:X}，事件/触发/FFT 扩展不能静默丢弃"
                ));
            }
        }
        directory.push(entry);
        offset += size;
    }
    let vendor = vendor.ok_or("HPF 缺少通道描述")?;
    if chunks.is_empty() {
        return Err("HPF 缺少原始数据块".into());
    }
    if let Some(indices) = &indices {
        let expected: HashSet<_> = directory
            .iter()
            .filter(|e| e.id != 0x6000)
            .map(|e| e.index())
            .collect();
        let actual: HashSet<_> = indices.iter().copied().collect();
        if expected != actual || actual.len() != indices.len() {
            return Err("HPF 索引未完整匹配文件块".into());
        }
    } else if index_offset != 0 && index_offset != -1 {
        return Err("HPF 头部指定的索引块缺失".into());
    }
    if directory
        .iter()
        .any(|e| [0x8000, 0x9000, 0xA000].contains(&e.id))
    {
        let rows = indices.as_ref().ok_or("HPF 附加显示块缺少索引")?;
        let zoom = zoom_map.as_ref().ok_or("HPF 附加显示块缺少缩放映射")?;
        let mut mapped = HashSet::new();
        for (index, factor) in zoom {
            let row = rows.get(*index).ok_or("HPF 缩放映射超出索引")?;
            if !((row[2] == 0x3000 && *factor == 1) || (row[2] == 0x8000 && *factor == 8)) {
                return Err("HPF 缩放映射与数据类型不匹配".into());
            }
            mapped.insert(row[4]);
        }
        let expected: HashSet<_> = directory
            .iter()
            .filter(|e| [0x3000, 0x8000].contains(&e.id))
            .map(|e| e.offset as i64)
            .collect();
        if mapped != expected {
            return Err("HPF 缩放映射未覆盖原始/显示块".into());
        }
    }
    let mut config = Value::Null;
    let mut native_metadata = None;
    if let Some(configuration) = &configuration {
        let doc = roxmltree::Document::parse(configuration).map_err(|e| e.to_string())?;
        if let Some(node) = doc
            .root_element()
            .children()
            .find(|n| n.has_tag_name("LabDAQMetadata"))
        {
            let metadata: Value =
                serde_json::from_str(node.text().unwrap_or("")).map_err(|e| e.to_string())?;
            if metadata["format"] != "labdaq-raw-voltage" || metadata["format_version"] != 1 {
                return Err("HPF 内嵌 LabDAQ 元数据版本不支持".into());
            }
            let restored: Vec<Channel> =
                serde_json::from_value(metadata["channels"].clone()).map_err(|e| e.to_string())?;
            Recording::validate_channels(&restored)?;
            if restored.len() != channels.len()
                || metadata["sample_count"].as_u64() != Some(count)
                || !near(metadata["sample_rate"].as_f64().unwrap_or(0.0), rate, 1e-12)
            {
                return Err("HPF 内嵌 LabDAQ 元数据与波形不匹配".into());
            }
            channels = restored;
            config = metadata["acquisition_config"].clone();
            native_metadata = Some(metadata);
        } else if vendor
            .iter()
            .all(|v| v.get("Unit").map(String::as_str) == Some("N"))
        {
            if let Some((settings, acquisition)) = force_settings(&vendor, &doc, rate)? {
                channels = settings;
                config = acquisition;
                voltage_error.clear();
            }
        }
    }
    let origin = config["record_start_scan"].as_u64().unwrap_or(0);
    let segments = if count > 0 {
        vec![Segment {
            file_start_scan: 0,
            source_start_scan: origin,
            sample_count: count,
        }]
    } else {
        vec![]
    };
    let mut metadata = json!({"format":"vendor-hpf","format_version":0x10001,"source":"vendor_hpf","source_file":path.file_name(),
        "sample_rate":rate,"sample_count":count,"channels":channels,"vendor_channels":vendor,"recording_date":recording_date,
        "voltage_replay_error":voltage_error,"vendor_configuration_xml":configuration,"acquisition_config":config,
        "waveform_only":true,"chunks":directory.iter().map(Directory::value).collect::<Vec<_>>(),
        "auxiliary_chunks":directory.iter().filter(|e|[0x8000,0x9000,0xA000].contains(&e.id)).map(Directory::value).collect::<Vec<_>>(),
        "recording_segments":segments});
    if let Some(native) = native_metadata {
        for key in [
            "acquisition_started_at_utc",
            "started_at_utc",
            "experiment",
            "recording_options",
            "export",
        ] {
            metadata[key] = native[key].clone();
        }
    }
    Ok(Recording {
        path: path.into(),
        metadata,
        channels,
        sample_rate: rate,
        sample_count: count,
        segments,
        hpf_chunks: Some(chunks),
    })
}

fn force_settings(
    vendor: &[BTreeMap<String, String>],
    doc: &roxmltree::Document<'_>,
    rate: f64,
) -> Result<Option<(Vec<Channel>, Value)>> {
    let devices: Vec<_> = doc
        .descendants()
        .filter(|n| n.has_tag_name("Device"))
        .collect();
    if devices.len() != 1 || field(devices[0], "DeviceFamily") != Some("DT9837_C") {
        return Ok(None);
    }
    let device = devices[0];
    let id = field(device, "DeviceId").ok_or("HPF 缺少 DeviceId")?;
    let acq = doc
        .root_element()
        .children()
        .find(|n| n.has_tag_name("acqParams"));
    if acq.and_then(|n| field(n, "FilterType")) != Some("None") {
        return Err("DT9837C HPF 采集滤波须为 None".into());
    }
    let inputs: Vec<_> = device
        .descendants()
        .filter(|n| n.has_tag_name("ChannelBase"))
        .collect();
    let mut settings = Vec::new();
    for info in vendor {
        let channel = inputs
            .iter()
            .copied()
            .find(|c| format!("{id}.{}", field(*c, "Name").unwrap_or("")) == info["Name"])
            .ok_or("HPF 波形通道与配置不匹配")?;
        let values = map(channel);
        let filter = channel
            .children()
            .find(|n| n.has_tag_name("Filter"))
            .and_then(|n| field(n, "FilterType"));
        if channel.attribute(("http://www.w3.org/2001/XMLSchema-instance", "type"))
            != Some("IEPEInputChannel")
            || field(channel, "ChannelType") != Some("IEPE")
            || field(channel, "Enabled") != Some("true")
            || field(channel, "EditUnitLabel") != Some("N")
            || filter != Some("None")
        {
            return Err("HPF 不是已确认的未滤波 DT9837C IEPE/N 配置".into());
        }
        if number(info, "DataScale")? != 1.0 || number(info, "DataOffset")? != 0.0 {
            return Err("HPF 存储值缩放非恒等".into());
        }
        let physical = number(&values, "ChannelNumber")?;
        if physical.fract() != 0.0
            || !(0.0..4.0).contains(&physical)
            || info.get("PhysicalChannelNumber") != values.get("ChannelNumber")
        {
            return Err("HPF 物理通道不一致".into());
        }
        let sensitivity = number(&values, "MilliVoltsPerUnit")?;
        let offset = number(&values, "Offset")?;
        if sensitivity == 0.0
            || !near(number(info, "SensorScale")?, 1000.0 / sensitivity, 1e-6)
            || (number(info, "SensorOffset")? - offset).abs() > 1e-12
        {
            return Err("HPF 换算参数与完整配置不一致".into());
        }
        settings.push(Channel {
            index: physical as usize,
            name: info["Name"].clone(),
            unit: "N".into(),
            sensitivity_mv_per_unit: sensitivity.abs(),
            zero_volts: -offset * sensitivity / 1000.0,
            inverted: sensitivity < 0.0,
            point_number: None,
            direction: String::new(),
            engineering_offset: 0.0,
        });
    }
    if settings.windows(2).any(|w| w[0].index >= w[1].index) {
        return Err("HPF 物理通道须升序且不重复".into());
    }
    let mut ordered = Vec::new();
    for i in 0..4 {
        ordered.push(
            inputs
                .iter()
                .copied()
                .find(|n| field(*n, "ChannelNumber") == Some(i.to_string().as_str()))
                .ok_or("HPF 缺少四路物理配置")?,
        );
    }
    let config = json!({"sample_rate":rate,"channels":settings.iter().map(|c|c.index).collect::<Vec<_>>(),
        "ranges":ordered.iter().map(|n|field(*n,"RangeHigh").and_then(|v|v.parse::<f64>().ok())).collect::<Vec<_>>(),
        "coupling":ordered.iter().map(|n|field(*n,"Coupling")).collect::<Vec<_>>(),
        "iepe":ordered.iter().map(|n|field(*n,"CurrentSourceEnabled")==Some("true")).collect::<Vec<_>>()});
    Ok(Some((settings, config)))
}

fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}
fn aligned(payload: usize) -> u64 {
    ((payload as u64 + 16 + ALIGN - 1) / ALIGN) * ALIGN
}
fn write_chunk(w: &mut dyn Write, id: i64, payload: &[u8]) -> Result<u64> {
    let size = aligned(payload.len());
    w.write_all(&id.to_le_bytes()).map_err(|e| e.to_string())?;
    w.write_all(&(size as i64).to_le_bytes())
        .map_err(|e| e.to_string())?;
    w.write_all(payload).map_err(|e| e.to_string())?;
    let padding = vec![0u8; (size - 16 - payload.len() as u64) as usize];
    w.write_all(&padding).map_err(|e| e.to_string())?;
    Ok(size)
}
pub fn export_hpf(request: &Value, control: &OperationControl<'_>) -> Result<Value> {
    let r = Recording::from_request(request)?;
    let (start, stop, columns) = selection(&r, request)?;
    if start == stop {
        return Err("HPF 导出至少一个样本".into());
    }
    let first_index = r.indices(start, 1)[0];
    let last_index = r.indices(stop - 1, 1)[0];
    if last_index - first_index + 1 != stop - start {
        return Err("HPF 此写出格式不表示暂停缺口，请选择一个连续片段".into());
    }
    let output = required_path(request, "output")?;
    protect_source(&r, &output)?;
    let mut xml = String::from("<ChannelInformationData>");
    for (position, column) in columns.iter().enumerate() {
        let c = &r.channels[*column];
        xml.push_str("<ChannelInformation>");
        for (key, value) in [
            ("Name", c.name.clone()),
            ("Unit", "V".into()),
            ("ChannelType", "RandomDataChannel".into()),
            ("DataType", "Double".into()),
            ("AssignedTimeChannelIndex", "-1".into()),
            ("DataIndex", position.to_string()),
            ("StartTime", "0".into()),
            ("TimeIncrement", (1.0 / r.sample_rate).to_string()),
            ("PerChannelSampleRate", r.sample_rate.to_string()),
            ("DataScale", "1".into()),
            ("DataOffset", "0".into()),
            ("SensorScale", "1".into()),
            ("SensorOffset", "0".into()),
            ("PhysicalChannelNumber", c.index.to_string()),
        ] {
            xml.push_str(&format!("<{key}>{}</{key}>", escape(&value)));
        }
        xml.push_str("</ChannelInformation>");
    }
    xml.push_str("</ChannelInformationData>");
    let mut channel_payload = Vec::new();
    channel_payload.extend(0i32.to_le_bytes());
    channel_payload.extend((columns.len() as i32).to_le_bytes());
    channel_payload.extend(xml.as_bytes());
    let mut metadata = r.metadata.clone();
    metadata["format"] = json!("labdaq-raw-voltage");
    metadata["format_version"] = json!(1);
    metadata["sample_count"] = json!(stop - start);
    metadata["channels"] = json!(
        columns
            .iter()
            .map(|c| r.channels[*c].clone())
            .collect::<Vec<_>>()
    );
    if !metadata["acquisition_config"].is_object() {
        metadata["acquisition_config"] = json!({});
    }
    metadata["acquisition_config"]["record_start_scan"] = json!(r.origin() + first_index);
    metadata["export"] =
        json!({"source":r.path,"scan_start":start,"scan_stop":stop,"storage":"raw volts"});
    let config_xml = format!(
        "<PersitantObjects><LabDAQMetadata>{}</LabDAQMetadata></PersitantObjects>",
        escape(&metadata.to_string())
    );
    let mut config_payload = 0i32.to_le_bytes().to_vec();
    config_payload.extend(config_xml.as_bytes());
    let header_size = ALIGN;
    let channel_size = aligned(channel_payload.len());
    let config_size = aligned(config_payload.len());
    let mut rows = vec![
        [-1, -1, 0x1000, 0, 0],
        [-1, -1, 0x2000, 0, header_size as i64],
        [-1, -1, 0x7000, 0, (header_size + channel_size) as i64],
    ];
    let mut offset = header_size + channel_size + config_size;
    let mut first = start;
    while first < stop {
        let n = (stop - first).min(8192);
        rows.push([(first - start) as i64, n as i64, 0x3000, 0, offset as i64]);
        offset += aligned(16 + columns.len() * 8 + n as usize * columns.len() * 8);
        first += n;
    }
    let index_offset = offset;
    let mut header = b"datx".to_vec();
    header.extend(0x10001i64.to_le_bytes());
    header.extend((index_offset as i64).to_le_bytes());
    header.extend(format!("<RecordingDate>{}</RecordingDate>", now()).as_bytes());
    atomic_output(&output, request["overwrite"] == true, |w| {
        write_chunk(w, 0x1000, &header)?;
        write_chunk(w, 0x2000, &channel_payload)?;
        write_chunk(w, 0x7000, &config_payload)?;
        let mut first = start;
        while first < stop {
            control.check(first - start, stop - start)?;
            let n = (stop - first).min(8192);
            let raw = r.read(first, n, &columns)?;
            let mut payload = 0i32.to_le_bytes().to_vec();
            payload.extend(((first - start) as i64).to_le_bytes());
            payload.extend((columns.len() as i32).to_le_bytes());
            let begin = 32 + columns.len() * 8;
            for column in 0..columns.len() {
                payload.extend(((begin + column * n as usize * 8) as i32).to_le_bytes());
                payload.extend((n as i32 * 8).to_le_bytes());
            }
            for channel in raw {
                for value in channel {
                    payload.extend(value.to_le_bytes());
                }
            }
            write_chunk(w, 0x3000, &payload)?;
            first += n;
        }
        let mut index_payload = (rows.len() as i64).to_le_bytes().to_vec();
        for row in &rows {
            for number in row {
                index_payload.extend(number.to_le_bytes());
            }
        }
        write_chunk(w, 0x6000, &index_payload)?;
        control.check(stop - start, stop - start)
    })?;
    Ok(
        json!({"output":output,"sample_count":stop-start,"format":"HPF 0x10001 synchronous Double","storage_unit":"V","conversion_metadata":"LabDAQMetadata XML extension"}),
    )
}
