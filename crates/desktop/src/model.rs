use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub source: String,
    pub sample_rate: f64,
    pub active_channels: Vec<usize>,
    pub channels: Vec<Value>,
    pub ranges: [f64; 4],
    pub coupling: [String; 4],
    pub iepe: [bool; 4],
    pub signal_settings: Value,
    pub experiment: String,
    pub condition: String,
    pub notes: String,
    pub specimen: String,
    pub group_number: u64,
    pub duration: f64,
    pub recording_root: PathBuf,
    pub trigger: Value,
    pub output: Value,
    pub analysis: Value,
    pub filter: Value,
    pub display_seconds: f64,
    pub refresh_hz: f32,
    pub decimals: usize,
    pub engineering: bool,
    pub channel_visible: [bool; 4],
    pub axis: String,
    pub auto_connect: bool,
    pub devices: Vec<Value>,
    pub synchronized: bool,
    pub linked_output: bool,
    pub readout_modes: [String; 4],
    pub readout_decimals: [usize; 4],
}

impl Default for Settings {
    fn default() -> Self {
        let signal = |i| json!({"waveform":"sine","amplitude_v":1.0/(i as f64+1.0),"offset_v":0.0,"frequency_hz":10.0,"phase_deg":i as f64*30.0,"noise_rms_v":0.0,"drift_v_per_s":0.0,"duty_cycle":0.5,"start_s":0.0,"duration_s":8.0,"end_frequency_hz":100.0});
        Self {
            source:"vendor".into(), sample_rate:2000.0, active_channels:vec![0,1,2,3],
            channels:(0..4).map(|i|json!({"index":i,"name":format!("AI{i}"),"unit":"V","sensitivity_mv_per_unit":1000.0,"zero_volts":0.0,"inverted":false,"point_number":null,"direction":"","engineering_offset":0.0})).collect(),
            ranges:[10.0;4], coupling:std::array::from_fn(|_|"DC".into()), iepe:[false;4],
            signal_settings:json!({"channels":(0..4).map(signal).collect::<Vec<_>>(),"seed":2026}),
            experiment:"实验".into(),condition:"工况".into(),notes:String::new(),specimen:String::new(),group_number:1,duration:0.0,
            recording_root: std::env::var_os("USERPROFILE").map(PathBuf::from).unwrap_or_else(crate::client::user_directory).join("Documents/LabDAQ/recordings"),
            trigger:json!({"mode":"immediate","channel":0,"level_v":0.0,"pretrigger_percent":0.0,"retrigger":false,"delay_seconds":0.0,"record_count":1,"accept_reject":false}),
            output:json!({"waveform":"sine","amplitude_v":0.1,"offset_v":0.0,"frequency_hz":10.0,"sample_rate":48000.0,"end_frequency_hz":1000.0,"sweep_seconds":10.0,"burst_on_seconds":1.0,"burst_off_seconds":0.0,"burst_count":0,"fade_in_seconds":0.0,"fade_out_seconds":0.0,"seed":2026,"bandwidth_hz":1000.0}),
            analysis:json!({"nfft":1024,"mode":"amplitude","window":"hann","reference":0,"average":"rms","weighting":"linear","overlap":0.5,"integration":0,"scale":"peak","display":"magnitude","db_reference":1.0}),
            filter:json!({"family":"butterworth","kind":"lowpass","order":4,"cutoff_hz":100.0,"cutoff_high_hz":300.0,"ripple_db":1.0,"zero_phase":true}),
            display_seconds:8.0,refresh_hz:20.0,decimals:4,engineering:true,channel_visible:[true;4],axis:"relative".into(),auto_connect:true,
            devices:vec![],synchronized:false,linked_output:false,readout_modes:std::array::from_fn(|_|"latest".into()),readout_decimals:[4;4],
        }
    }
}

impl Settings {
    pub fn profile(&self) -> Value {
        json!({"source":self.source,"config":{"sample_rate":self.sample_rate,"channels":self.active_channels,"ranges":self.ranges,"coupling":self.coupling,"iepe":self.iepe},
            "actual_rate":self.sample_rate,"channels":self.channels,"signal_settings":self.signal_settings,"experiment":self.experiment,"recording_root":self.recording_root,"recording_options":self.recording_options(),"trigger":self.trigger,
            "device_selector":self.devices.first(),"linked_output":if self.linked_output{Some(&self.output)}else{None}})
    }
    pub fn recording_options(&self) -> Value {
        json!({"duration_seconds":if self.duration>0.0 {Some(self.duration)} else {None},"condition":self.condition,"notes":self.notes,"specimen":self.specimen,"group_number":self.group_number})
    }
    pub fn next_recording(&self) -> PathBuf {
        self.recording_root
            .join(path_component(&self.experiment, "实验"))
            .join(format!(
                "{}-{}",
                path_component(&self.condition, "未命名"),
                self.group_number
            ))
    }
    pub fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut value = serde_json::to_value(self).map_err(|e| e.to_string())?;
        value["schema"] = json!("labdaq-profile-v1");
        let temp = path.with_extension("json.tmp");
        std::fs::write(
            &temp,
            serde_json::to_vec_pretty(&value).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        std::fs::rename(temp, path).map_err(|e| e.to_string())
    }
    pub fn load(path: &Path) -> Result<Self, String> {
        let mut value: Value =
            serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
        if value.get("source").is_none() {
            value["source"] = json!(match value["scenario"].as_str() {
                Some("usb") => "vendor",
                Some("rust" | "custom" | "dc" | "sine" | "step" | "noise") => "simulation",
                Some("hpf") => return Err("旧 HPF 输入配置请通过记录与回看页面打开原文件。".into()),
                _ => "vendor",
            });
        }
        if let Some(output) = value.get("output_settings").cloned() {
            value["output"] = output;
        }
        if let Some(display) = value.get("display").cloned() {
            if let Some(axis) = display.get("live_time_axis") {
                value["axis"] = axis.clone();
            }
            if let Some(seconds) = display.get("live_seconds") {
                value["display_seconds"] = seconds.clone();
            }
            if let Some(modes) = display["value_modes"].as_array() {
                value["readout_modes"] = json!(
                    modes
                        .iter()
                        .map(|v| match v.as_str().unwrap_or("last") {
                            "last" => "latest",
                            "std_dev" => "std",
                            "peak_to_peak" => "pkpk",
                            mode => mode,
                        })
                        .collect::<Vec<_>>()
                );
            } else if let Some(mode) = display["value_mode"].as_str() {
                let mode = match mode {
                    "last" => "latest",
                    "std_dev" => "std",
                    "peak_to_peak" => "pkpk",
                    x => x,
                };
                value["readout_modes"] = json!(vec![mode; 4]);
            }
            if let Some(decimals) = display["value_decimals"].as_u64() {
                value["decimals"] = json!(decimals);
                value["readout_decimals"] = json!(vec![decimals; 4]);
            }
        }
        if let Some(options) = value.get("recording_options").cloned() {
            for key in ["condition", "notes", "specimen", "group_number"] {
                if let Some(v) = options.get(key) {
                    value[key] = v.clone();
                }
            }
            if let Some(v) = options.get("duration_seconds").filter(|v| !v.is_null()) {
                value["duration"] = v.clone();
            }
        }
        let result: Self = serde_json::from_value(value).map_err(|e| e.to_string())?;
        if result.channels.len() != 4
            || result.active_channels.is_empty()
            || result.active_channels.iter().any(|&i| i > 3)
            || result.active_channels.windows(2).any(|w| w[0] >= w[1])
        {
            return Err("配置中的通道集合无效".into());
        }
        Ok(result)
    }
    pub fn restore_profile(&mut self, profile: &Value) {
        if profile.is_null() {
            return;
        }
        let mut value = serde_json::to_value(&*self).unwrap();
        for key in [
            "source",
            "channels",
            "signal_settings",
            "experiment",
            "recording_root",
            "trigger",
        ] {
            if let Some(v) = profile.get(key) {
                value[key] = v.clone();
            }
        }
        for key in ["sample_rate", "ranges", "coupling", "iepe"] {
            if let Some(v) = profile["config"].get(key) {
                value[key] = v.clone();
            }
        }
        value["active_channels"] = profile["config"]["channels"].clone();
        for key in ["condition", "notes", "specimen", "group_number"] {
            if let Some(v) = profile["recording_options"].get(key) {
                value[key] = v.clone();
            }
        }
        value["duration"] = profile["recording_options"]["duration_seconds"]
            .as_f64()
            .map_or(json!(0), |x| json!(x));
        if let Ok(settings) = serde_json::from_value(value) {
            *self = settings;
        }
    }
}
pub fn path_component(value: &str, fallback: &str) -> String {
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
        fallback.into()
    } else {
        result.into()
    }
}
pub fn engineering(volts: f64, ch: &Value) -> f64 {
    (volts - ch["zero_volts"].as_f64().unwrap_or(0.0))
        * (if ch["unit"] == "V" {
            1.0
        } else {
            1000.0 / ch["sensitivity_mv_per_unit"].as_f64().unwrap_or(1000.0)
        })
        * if ch["inverted"] == true { -1.0 } else { 1.0 }
        + ch["engineering_offset"].as_f64().unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_example_and_defaults_are_compatible() {
        let s = Settings::load(Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../configs/电信号模拟.json"
        )))
        .unwrap();
        assert_eq!(s.active_channels, vec![0, 1, 2, 3]);
        assert_eq!(s.channels[0]["name"], "10 Hz 正弦");
        assert_eq!(s.profile()["config"]["sample_rate"], 2000.0);
    }
    #[test]
    fn conversion_order_and_paths() {
        let ch = json!({"zero_volts":0.1,"sensitivity_mv_per_unit":100.0,"inverted":true,"engineering_offset":5.0});
        assert!((engineering(0.3, &ch) - 3.0).abs() < 1e-12);
        assert_eq!(path_component("a/b:c", "x"), "a_b_c");
        assert_eq!(path_component(" .. ", "x"), "x");
    }
}
