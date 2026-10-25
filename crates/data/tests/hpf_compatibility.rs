//! Independent binary fixtures: do not use the production exporter to build them.
use labdaq_data::Recording;
use std::fs;
use std::path::Path;

fn block(kind: i64, payload: &[u8]) -> Vec<u8> {
    let size = ((payload.len() + 16 + 65535) / 65536) * 65536;
    let mut b = kind.to_le_bytes().to_vec();
    b.extend((size as i64).to_le_bytes());
    b.extend(payload);
    b.resize(size, 0);
    b
}
fn xml_field(name: &str, value: impl ToString) -> String {
    format!("<{name}>{}</{name}>", value.to_string())
}
fn fixture(path: &Path, force: bool, aux: bool, unknown: bool) {
    let mut pieces = vec![vec![0u8; 65536]];
    let mut entries = vec![[-1i64, -1, 0x1000, 0, 0]];
    let mut append = |kind: i64, payload: Vec<u8>, start: i64, count: i64| {
        let offset = pieces.iter().map(Vec::len).sum::<usize>();
        entries.push([start, count, kind, 0, offset as i64]);
        pieces.push(block(kind, &payload));
    };
    let mut xml = String::from("<ChannelInformationData>");
    for column in 0..2 {
        xml.push_str("<ChannelInformation>");
        let physical = column * 2;
        let sensitivity = if column == 0 { 500.0 } else { 333.3330078125 };
        for (name, value) in [
            (
                "Name",
                if force {
                    format!("DT9837-C(00).Ain {physical}")
                } else {
                    format!("test {column}")
                },
            ),
            ("Unit", if force { "N" } else { "V" }.into()),
            ("ChannelType", "RandomDataChannel".into()),
            ("DataType", "Double".into()),
            ("AssignedTimeChannelIndex", "-1".into()),
            ("DataIndex", (1 - column).to_string()),
            ("StartTime", "0".into()),
            ("TimeIncrement", "0.0005".into()),
            ("PerChannelSampleRate", "2000".into()),
            ("DataScale", "1".into()),
            ("DataOffset", "0".into()),
            (
                "SensorScale",
                if force {
                    (1000.0 / sensitivity).to_string()
                } else {
                    "1".into()
                },
            ),
            ("SensorOffset", "0".into()),
            ("PhysicalChannelNumber", physical.to_string()),
        ] {
            xml.push_str(&xml_field(name, value));
        }
        xml.push_str("</ChannelInformation>");
    }
    xml.push_str("</ChannelInformationData>");
    let mut payload = 0i32.to_le_bytes().to_vec();
    payload.extend(2i32.to_le_bytes());
    payload.extend(xml.as_bytes());
    append(0x2000, payload, -1, -1);
    if force {
        let mut xml = String::from(
            "<PersitantObjects xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"><devConfig><Device><DeviceFamily>DT9837_C</DeviceFamily><DeviceId>DT9837-C(00)</DeviceId><InputSubsystem><Channels>",
        );
        for physical in 0..4 {
            xml.push_str("<ListItem><ChannelBase xsi:type=\"IEPEInputChannel\">");
            for (name, value) in [
                ("Name", format!("Ain {physical}")),
                ("ChannelNumber", physical.to_string()),
                ("ChannelType", "IEPE".into()),
                (
                    "Enabled",
                    if physical == 0 || physical == 2 {
                        "true"
                    } else {
                        "false"
                    }
                    .into(),
                ),
                ("EditUnitLabel", "N".into()),
                (
                    "MilliVoltsPerUnit",
                    if physical == 0 {
                        "500"
                    } else {
                        "333.3330078125"
                    }
                    .into(),
                ),
                ("Offset", "0".into()),
                ("RangeHigh", "10".into()),
                ("RangeLow", "-10".into()),
                ("Coupling", "DC".into()),
                ("CurrentSourceEnabled", "false".into()),
            ] {
                xml.push_str(&xml_field(name, value));
            }
            xml.push_str("<Filter><FilterType>None</FilterType></Filter></ChannelBase></ListItem>");
        }
        xml.push_str("</Channels></InputSubsystem></Device></devConfig><acqParams><FilterType>None</FilterType></acqParams></PersitantObjects>");
        let mut payload = 0i32.to_le_bytes().to_vec();
        payload.extend(xml.as_bytes());
        append(0x7000, payload, -1, -1);
    }
    let values = [
        [0.125, -1.0],
        [1.25, -2.0],
        [2.5, -3.0],
        [3.75, -4.0],
        [4.5, -5.0],
        [5.25, -6.0],
    ];
    for (start, stop) in [(0usize, 2usize), (2, 6)] {
        let n = stop - start;
        let mut payload = 0i32.to_le_bytes().to_vec();
        payload.extend((start as i64).to_le_bytes());
        payload.extend(2i32.to_le_bytes());
        for (offset, length) in [(48, n * 8), (48 + n * 8, n * 8)] {
            payload.extend((offset as i32).to_le_bytes());
            payload.extend((length as i32).to_le_bytes());
        }
        for col in [1, 0] {
            for row in &values[start..stop] {
                let value: f64 = row[col];
                payload.extend(value.to_le_bytes());
            }
        }
        append(0x3000, payload, start as i64, n as i64);
    }
    if aux {
        let mut payload = 0i32.to_le_bytes().to_vec();
        payload.extend(0i64.to_le_bytes());
        payload.extend(2i32.to_le_bytes());
        for value in [64i32, 8, 72, 8] {
            payload.extend(value.to_le_bytes());
        }
        payload.extend(8i64.to_le_bytes());
        payload.extend([0u8; 8]);
        payload.extend(99f64.to_le_bytes());
        payload.extend(101f64.to_le_bytes());
        append(0x8000, payload, 0, 1);
        let base = if force { 3 } else { 2 };
        let mut xml = String::from("<IndexDescriptorToZoomMap>");
        for (key, factor) in [(base, 1), (base + 1, 1), (base + 2, 8)] {
            xml.push_str(&format!(
                "<Item><Key><int>{key}</int></Key><Value><int>{factor}</int></Value></Item>"
            ));
        }
        xml.push_str("</IndexDescriptorToZoomMap>");
        let mut payload = 0i32.to_le_bytes().to_vec();
        payload.extend(xml.as_bytes());
        append(0xA000, payload, base, -1);
        let mut payload = 0i32.to_le_bytes().to_vec();
        payload.extend(0i64.to_le_bytes());
        payload.extend(2i32.to_le_bytes());
        for value in [48i32, 80, 128, 80] {
            payload.extend(value.to_le_bytes());
        }
        for value in 0..20 {
            payload.extend((value as f64).to_le_bytes());
        }
        append(0x9000, payload, 0, 10);
    }
    if unknown {
        append(0x4000, vec![], -1, -1);
    }
    let index_offset = pieces.iter().map(Vec::len).sum::<usize>();
    let mut header = b"datx".to_vec();
    header.extend(0x10001i64.to_le_bytes());
    header.extend((index_offset as i64).to_le_bytes());
    header.extend(b"<RecordingDate>2026/10/25</RecordingDate>");
    pieces[0] = block(0x1000, &header);
    let mut index = (entries.len() as i64).to_le_bytes().to_vec();
    for entry in entries {
        for value in entry {
            index.extend(value.to_le_bytes());
        }
    }
    pieces.push(block(0x6000, &index));
    fs::write(path, pieces.concat()).unwrap();
}

#[test]
fn independent_double_fixture_obeys_dataindex_and_cross_chunk_seek() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("raw.hpf");
    fixture(&path, false, false, false);
    let r = Recording::open(path).unwrap();
    assert_eq!(r.sample_count, 6);
    let samples = r.read(1, 4, &[0, 1]).unwrap();
    assert_eq!(samples[0], vec![1.25, 2.5, 3.75, 4.5]);
    assert_eq!(samples[1], vec![-2.0, -3.0, -4.0, -5.0]);
}

#[test]
fn observed_force_profile_uses_full_precision_configuration_and_ignores_auxiliary_values() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("force.hpf");
    fixture(&path, true, true, false);
    let r = Recording::open(path).unwrap();
    assert_eq!(r.channels[1].index, 2);
    assert_eq!(r.channels[1].sensitivity_mv_per_unit, 333.3330078125);
    assert_eq!(r.metadata["auxiliary_chunks"].as_array().unwrap().len(), 3);
    assert_eq!(
        r.read(0, 6, &[0]).unwrap()[0],
        vec![0.125, 1.25, 2.5, 3.75, 4.5, 5.25]
    );
}

#[test]
fn events_and_filtering_are_rejected_instead_of_silently_changed() {
    let temp = tempfile::tempdir().unwrap();
    let event = temp.path().join("event.hpf");
    fixture(&event, false, false, true);
    assert!(Recording::open(&event).unwrap_err().contains("0x4000"));
    let force = temp.path().join("filtered.hpf");
    fixture(&force, true, false, false);
    let mut bytes = fs::read(&force).unwrap();
    let pattern = b"<FilterType>None</FilterType>";
    let at = bytes
        .windows(pattern.len())
        .position(|w| w == pattern)
        .unwrap();
    bytes[at + 12..at + 16].copy_from_slice(b"LowP");
    fs::write(&force, bytes).unwrap();
    assert!(Recording::open(force).is_err());
}

#[test]
fn invalid_zoom_mapping_and_data_overlap_are_rejected() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("force.hpf");
    fixture(&path, true, true, false);
    let mut bytes = fs::read(&path).unwrap();
    let pattern = b"<Value><int>8</int></Value>";
    let at = bytes
        .windows(pattern.len())
        .position(|w| w == pattern)
        .unwrap();
    bytes[at + 12] = b'1';
    fs::write(&path, bytes).unwrap();
    assert!(Recording::open(&path).is_err());
    let raw = temp.path().join("overlap.hpf");
    fixture(&raw, false, false, false);
    let mut bytes = fs::read(&raw).unwrap();
    let data = 2 * 65536;
    bytes[data + 40..data + 44].copy_from_slice(&48i32.to_le_bytes());
    fs::write(&raw, bytes).unwrap();
    assert!(Recording::open(raw).is_err());
}
