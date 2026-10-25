use crate::{
    ChannelSettings, convert_volts,
    metrics::{DynamicMetrics, Peak, dynamic_metrics, spectral_peaks},
};
use num_complex::Complex64 as C;
use rustfft::FftPlanner;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::f64::consts::{PI, SQRT_2, TAU};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Window {
    Named(String),
    Parameterized {
        name: String,
        #[serde(default = "window_fraction")]
        fraction: f64,
        #[serde(default = "window_end")]
        end_amplitude: f64,
    },
}
fn window_fraction() -> f64 {
    0.1
}
fn window_end() -> f64 {
    0.01
}
impl Default for Window {
    fn default() -> Self {
        Self::Named("hann".into())
    }
}
impl Window {
    pub fn name(&self) -> &str {
        match self {
            Self::Named(s) => s,
            Self::Parameterized { name, .. } => name,
        }
    }
    pub fn weights(&self, n: usize) -> Result<Vec<f64>, String> {
        if n < 2 {
            return Err("窗函数至少需要两个采样点".into());
        }
        let (fraction, end) = match self {
            Self::Named(_) => (0.1, 0.01),
            Self::Parameterized {
                fraction,
                end_amplitude,
                ..
            } => (*fraction, *end_amplitude),
        };
        if !fraction.is_finite()
            || fraction <= 0.0
            || fraction > 1.0
            || !end.is_finite()
            || end <= 0.0
            || end > 1.0
        {
            return Err("窗比例需在 (0,1]，指数窗终值需在 (0,1]".into());
        }
        let mut out = Vec::with_capacity(n);
        for i in 0..n {
            let t = i as f64 / n as f64;
            let x = TAU * t;
            let w = match self
                .name()
                .to_lowercase()
                .replace(['-', '_', ' '], "")
                .as_str()
            {
                "rectangular" | "boxcar" | "rect" => 1.0,
                "hann" | "hanning" => 0.5 - 0.5 * x.cos(),
                "hamming" => 0.54 - 0.46 * x.cos(),
                "bartlett" => 1.0 - (2.0 * t - 1.0).abs(),
                "blackman" => 0.42 - 0.5 * x.cos() + 0.08 * (2.0 * x).cos(),
                "blackmanharris" => {
                    0.35875 - 0.48829 * x.cos() + 0.14128 * (2.0 * x).cos()
                        - 0.01168 * (3.0 * x).cos()
                }
                "flattop" => {
                    0.21557895 - 0.41663158 * x.cos() + 0.277263158 * (2.0 * x).cos()
                        - 0.083578947 * (3.0 * x).cos()
                        + 0.006947368 * (4.0 * x).cos()
                }
                "force" => {
                    if t < fraction {
                        1.0
                    } else {
                        0.0
                    }
                }
                "exponential" => end.powf(i as f64 / (n - 1) as f64),
                "cosinetaper" | "tukey" => {
                    if t < fraction / 2.0 {
                        0.5 * (1.0 + (PI * (2.0 * t / fraction - 1.0)).cos())
                    } else if t > 1.0 - fraction / 2.0 {
                        0.5 * (1.0 + (PI * (2.0 * t / fraction - 2.0 / fraction + 1.0)).cos())
                    } else {
                        1.0
                    }
                }
                _ => return Err(format!("未知窗函数：{}", self.name())),
            };
            out.push(w);
        }
        Ok(out)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct AnalysisRequest {
    pub samples: Vec<Vec<f64>>,
    pub source: Value,
    pub sample_rate: f64,
    /// Zero selects the largest power of two fitting the input (up to 262144).
    pub nfft: usize,
    pub mode: String,
    pub window: Window,
    pub reference_window: Option<Window>,
    pub response_window: Option<Window>,
    pub reference: usize,
    pub average: String,
    pub weighting: String,
    /// Exponential new-record weight, 0 < alpha <= 1.
    pub alpha: f64,
    pub overlap: f64,
    pub detrend: bool,
    pub integration: u8,
    pub reference_integration: u8,
    pub scale: String,
    pub display: String,
    pub db_reference: f64,
    pub max_frequency: Option<f64>,
    pub band: Option<[f64; 2]>,
    pub channels: Vec<ChannelSettings>,
    pub raw_volts: bool,
    /// custom, inertance, mobility, compliance, apparent_mass, impedance,
    /// dynamic_stiffness. Named forms require a force reference and a known
    /// response motion unit; conversions are applied before spectral ratios.
    pub frf_form: String,
    pub peak_count: usize,
    pub peak_priority: String,
    pub peak_min_prominence: f64,
    pub fundamental_hz: Option<f64>,
    /// Full-scale positive peak in the same engineering unit as each input.
    pub full_scale: Option<f64>,
    pub full_scales: Vec<f64>,
    pub harmonic_count: usize,
    pub tone_half_width_bins: usize,
}
impl Default for AnalysisRequest {
    fn default() -> Self {
        Self {
            samples: vec![],
            source: Value::Null,
            sample_rate: 1000.0,
            nfft: 0,
            mode: "amplitude".into(),
            window: Window::default(),
            reference_window: None,
            response_window: None,
            reference: 0,
            average: "rms".into(),
            weighting: "linear".into(),
            alpha: 0.2,
            overlap: 0.5,
            detrend: false,
            integration: 0,
            reference_integration: 0,
            scale: "peak".into(),
            display: "magnitude".into(),
            db_reference: 1.0,
            max_frequency: None,
            band: None,
            channels: vec![],
            raw_volts: false,
            frf_form: "custom".into(),
            peak_count: 10,
            peak_priority: "amplitude".into(),
            peak_min_prominence: 0.0,
            fundamental_hz: None,
            full_scale: None,
            full_scales: vec![],
            harmonic_count: 6,
            tone_half_width_bins: 4,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Trace {
    pub channel: usize,
    pub channel_index: usize,
    pub point_number: Option<u32>,
    pub direction: String,
    pub reference_point: Option<u32>,
    pub reference_direction: String,
    pub reference_unit: String,
    pub name: String,
    pub unit: String,
    /// Unit of the linear complex value, independent of display scaling.
    pub quantity_unit: String,
    pub values: Vec<f64>,
    pub real: Vec<f64>,
    pub imaginary: Vec<f64>,
    pub phase: Vec<f64>,
    pub coherence: Option<Vec<f64>>,
    pub band_power: Option<f64>,
    pub band_power_unit: String,
    pub peaks: Vec<Peak>,
    pub metrics: DynamicMetrics,
}
#[derive(Clone, Debug, Serialize)]
pub struct AnalysisResult {
    pub source: Value,
    pub mode: String,
    pub display: String,
    pub parameters: Value,
    pub frequencies: Vec<f64>,
    pub traces: Vec<Trace>,
    pub records: usize,
    pub nfft: usize,
    pub resolution_hz: f64,
    pub record_starts: Vec<usize>,
    pub averaging: String,
    pub weighting: String,
    pub phase_unit: &'static str,
    pub semantics: &'static str,
}

fn unit_integral(unit: &str, n: u8) -> String {
    match (unit, n) {
        (_, 0) => unit.into(),
        ("m/s²" | "m/s^2" | "m/s2", 1) => "m/s".into(),
        ("m/s²" | "m/s^2" | "m/s2", 2) => "m".into(),
        ("m/s", 1) => "m".into(),
        (_, 1) => format!("{unit}·s"),
        (_, 2) => format!("{unit}·s²"),
        _ => unreachable!(),
    }
}
fn integrated(z: C, f: f64, n: u8) -> C {
    if n == 0 {
        z
    } else if f == 0.0 {
        C::new(f64::NAN, f64::NAN)
    } else {
        z / C::new(0.0, TAU * f).powu(n as u32)
    }
}
fn factor(bin: usize, n: usize) -> f64 {
    if bin == 0 || (n % 2 == 0 && bin == n / 2) {
        1.0
    } else {
        2.0
    }
}
fn select_fft_count(len: usize) -> usize {
    let capped = len.min(262144);
    1usize << (usize::BITS - 1 - capped.leading_zeros())
}
fn frac_weight(count: usize, r: &AnalysisRequest) -> f64 {
    if count == 0 {
        1.0
    } else if r.weighting == "exponential" {
        r.alpha
    } else {
        1.0 / (count + 1) as f64
    }
}

pub fn analyze_json(request: &Value) -> Result<Value, String> {
    let req: AnalysisRequest =
        serde_json::from_value(request.clone()).map_err(|e| format!("分析参数：{e}"))?;
    serde_json::to_value(analyze(&req)?).map_err(|e| e.to_string())
}

pub fn analyze(r: &AnalysisRequest) -> Result<AnalysisResult, String> {
    let len = r.samples.first().map_or(0, Vec::len);
    if len < 2
        || r.samples
            .iter()
            .any(|x| x.len() != len || x.iter().any(|v| !v.is_finite()))
    {
        return Err("分析数据需为等长、至少两个样本的连续通道，不允许 NaN 或无穷值".into());
    }
    if !r.sample_rate.is_finite() || r.sample_rate <= 0.0 {
        return Err("采样率必须为正有限数".into());
    }
    let n = if r.nfft == 0 {
        select_fft_count(len)
    } else {
        r.nfft
    };
    if n < 2 || n > 262144 || n > len {
        return Err("FFT 点数需在 2–262144 之间且不超过连续样本数".into());
    }
    if !r.overlap.is_finite() || !(0.0..1.0).contains(&r.overlap) {
        return Err("重叠比例需在 [0,1)".into());
    }
    if !["rms", "vector", "peak_hold"].contains(&r.average.as_str())
        || !["linear", "exponential"].contains(&r.weighting.as_str())
    {
        return Err("平均模式需为 rms/vector/peak_hold，权重为 linear/exponential".into());
    }
    if !r.alpha.is_finite() || r.alpha <= 0.0 || r.alpha > 1.0 {
        return Err("指数平均权重需在 (0,1]".into());
    }
    if r.integration > 2 || r.reference_integration > 2 {
        return Err("支持零次、一次或二次积分".into());
    }
    if !["peak", "rms", "pkpk"].contains(&r.scale.as_str())
        || !["magnitude", "phase", "real", "imaginary", "db", "nyquist"]
            .contains(&r.display.as_str())
    {
        return Err("未知谱标度或显示方式".into());
    }
    if !r.db_reference.is_finite() || r.db_reference <= 0.0 {
        return Err("dB 参考值必须为正有限数".into());
    }
    if r.max_frequency
        .is_some_and(|v| !v.is_finite() || v <= 0.0 || v > r.sample_rate / 2.0)
    {
        return Err("最大分析频率需在 (0,fs/2]".into());
    }
    if r.band.is_some_and(|b| {
        !b[0].is_finite()
            || !b[1].is_finite()
            || b[0] < 0.0
            || b[1] <= b[0]
            || b[1] > r.sample_rate / 2.0
    }) {
        return Err("频带需满足 0 <= 下限 < 上限 <= fs/2".into());
    }
    let dual = [
        "h1",
        "h2",
        "h3",
        "cross_spectrum",
        "cross_psd",
        "coherence",
        "cop",
    ]
    .contains(&r.mode.as_str());
    if !dual && !["amplitude", "spectrum", "autospectrum", "psd"].contains(&r.mode.as_str()) {
        return Err(format!("未知分析模式：{}", r.mode));
    }
    if dual && (r.samples.len() < 2 || r.reference >= r.samples.len()) {
        return Err("双通道分析需要有效的参考通道和响应通道".into());
    }
    if dual && r.average != "rms" {
        return Err(
            "FRF/互谱/相干使用成对自谱和复互谱平均；请选择 RMS 模式，保留线性或指数权重".into(),
        );
    }
    if r.average == "peak_hold" && r.weighting == "exponential" {
        return Err("PeakHold 保存历次最大值，不适用指数权重".into());
    }
    if !["amplitude", "prominence"].contains(&r.peak_priority.as_str())
        || !r.peak_min_prominence.is_finite()
        || r.peak_min_prominence < 0.0
    {
        return Err("谱峰优先方式为 amplitude/prominence，最小峰高为非负有限数".into());
    }
    if r.fundamental_hz
        .is_some_and(|f| !f.is_finite() || f <= 0.0 || f > r.sample_rate / 2.0)
        || !(2..=100).contains(&r.harmonic_count)
        || r.tone_half_width_bins > 262144
    {
        return Err("基频需在 (0,fs/2]；最高谐波次数为 2–100，谱峰半宽不能超过 262144".into());
    }
    if r.full_scale.is_some_and(|v| !v.is_finite() || v <= 0.0)
        || r.full_scales.iter().any(|v| !v.is_finite() || *v <= 0.0)
        || (!r.full_scales.is_empty() && r.full_scales.len() != r.samples.len())
    {
        return Err("满量程峰值必须为正有限数，逐通道量程数量需匹配".into());
    }
    let step = ((n as f64 * (1.0 - r.overlap)).round() as usize).max(1);
    let starts: Vec<_> = (0..=len - n).step_by(step).collect();
    if dual && starts.len() < 2 {
        return Err("FRF/相干分析需要至少两个记录块；减少 FFT 点数或增加连续样本".into());
    }
    let mut data = if r.raw_volts {
        convert_volts(&r.samples, &r.channels)?
    } else {
        r.samples.clone()
    };
    if !r.channels.is_empty() && r.channels.len() != data.len() {
        return Err("通道元数据数量与分析数据不一致".into());
    }
    let mut units: Vec<String> = (0..data.len())
        .map(|ch| r.channels.get(ch).map_or("V", |c| &c.unit).to_string())
        .collect();
    let mut unit_multiplier = vec![1.0; data.len()];
    let mut integrations = vec![r.integration; data.len()];
    if dual {
        integrations[r.reference] = r.reference_integration;
    }
    let mut inverse = false;
    if r.frf_form != "custom" {
        if !["h1", "h2", "h3"].contains(&r.mode.as_str()) {
            return Err("物理 FRF 形式只适用于 H1/H2/H3".into());
        }
        if units[r.reference] != "N" {
            return Err("物理 FRF 形式需要单位为 N 的力参考通道".into());
        }
        let target = match r.frf_form.as_str() {
            "inertance" | "apparent_mass" => 0,
            "mobility" | "impedance" => 1,
            "compliance" | "dynamic_stiffness" => 2,
            _ => return Err("未知物理 FRF 形式".into()),
        };
        inverse =
            ["apparent_mass", "impedance", "dynamic_stiffness"].contains(&r.frf_form.as_str());
        integrations[r.reference] = 0;
        for ch in 0..data.len() {
            if ch != r.reference {
                let current = match units[ch].as_str() {
                    "m/s²" | "m/s^2" | "m/s2" => 0,
                    "g" => {
                        for v in &mut data[ch] {
                            *v *= 9.80665
                        }
                        units[ch] = "m/s²".into();
                        unit_multiplier[ch] = 9.80665;
                        0
                    }
                    "m/s" => 1,
                    "m" => 2,
                    _ => return Err("物理 FRF 响应需为 m/s²、g、m/s 或 m".into()),
                };
                if current > target {
                    return Err("所选 FRF 形式需要微分；请提供对应运动量或选择可积分形式".into());
                }
                integrations[ch] = target - current;
            }
        }
    }
    let bins = n / 2 + 1;
    let df = r.sample_rate / n as f64;
    let frequencies: Vec<_> = (0..bins).map(|i| i as f64 * df).collect();
    let windows: Vec<_> = (0..data.len())
        .map(|ch| {
            if dual && ch == r.reference {
                r.reference_window.as_ref().unwrap_or(&r.window)
            } else {
                r.response_window.as_ref().unwrap_or(&r.window)
            }
            .weights(n)
        })
        .collect::<Result<_, _>>()?;
    let sums: Vec<f64> = windows.iter().map(|w| w.iter().sum()).collect();
    let powers: Vec<f64> = windows
        .iter()
        .map(|w| w.iter().map(|v| v * v).sum())
        .collect();
    if sums.iter().any(|s| s.abs() < 1e-15) {
        return Err("窗相干增益为零，无法计算幅值谱".into());
    }
    let mut mean_fft = vec![vec![C::new(0.0, 0.0); bins]; data.len()];
    let mut mean_psd = vec![vec![0.0; bins]; data.len()];
    let mut mean_amp2 = mean_psd.clone();
    let mut hold_fft = mean_fft.clone();
    let mut cross_psd = mean_fft.clone();
    let mut cross_power = mean_fft.clone();
    let mut metric_psd = mean_psd.clone();
    let mut metric_fft = mean_fft.clone();
    let metric_window = Window::Named("blackman_harris".into()).weights(n)?;
    let metric_window_power = metric_window.iter().map(|w| w * w).sum::<f64>();
    let mut planner = FftPlanner::<f64>::new();
    let fft = planner.plan_fft_forward(n);
    for (record, &start) in starts.iter().enumerate() {
        let weight = frac_weight(record, r);
        let mut coefficients = Vec::with_capacity(data.len());
        for ch in 0..data.len() {
            let slice = &data[ch][start..start + n];
            let mean = if r.detrend {
                slice.iter().sum::<f64>() / n as f64
            } else {
                0.0
            };
            let mut z: Vec<_> = slice
                .iter()
                .zip(&windows[ch])
                .map(|(v, w)| C::new((v - mean) * w, 0.0))
                .collect();
            fft.process(&mut z);
            z.truncate(bins);
            for k in 0..bins {
                z[k] = integrated(z[k], frequencies[k], integrations[ch]);
                let amplitude = z[k] * factor(k, n) / sums[ch];
                let psd = z[k].norm_sqr() * factor(k, n) / (r.sample_rate * powers[ch]);
                let a2 = amplitude.norm_sqr();
                mean_fft[ch][k] = mean_fft[ch][k] * (1.0 - weight) + amplitude * weight;
                mean_psd[ch][k] = mean_psd[ch][k] * (1.0 - weight) + psd * weight;
                mean_amp2[ch][k] = mean_amp2[ch][k] * (1.0 - weight) + a2 * weight;
                if record == 0 || a2 > hold_fft[ch][k].norm_sqr() {
                    hold_fft[ch][k] = amplitude;
                }
            }
            coefficients.push(z);
            let mut mz: Vec<_> = slice
                .iter()
                .zip(&metric_window)
                .map(|(v, w)| C::new((v - mean) * w, 0.0))
                .collect();
            fft.process(&mut mz);
            for k in 0..bins {
                let density =
                    mz[k].norm_sqr() * factor(k, n) / (r.sample_rate * metric_window_power);
                metric_psd[ch][k] = metric_psd[ch][k] * (1.0 - weight) + density * weight;
                metric_fft[ch][k] = metric_fft[ch][k] * (1.0 - weight) + mz[k] * weight;
            }
        }
        if dual {
            for ch in 0..data.len() {
                if ch != r.reference {
                    for k in 0..bins {
                        let raw = coefficients[r.reference][k].conj() * coefficients[ch][k];
                        // Separate reference/response windows use their geometric energy
                        // normalizer for cross PSD. H1 follows Pxy/Pxx explicitly.
                        let cp = raw * factor(k, n)
                            / (r.sample_rate * (powers[r.reference] * powers[ch]).sqrt());
                        let cs = raw * factor(k, n) / (sums[r.reference] * sums[ch]);
                        cross_psd[ch][k] = cross_psd[ch][k] * (1.0 - weight) + cp * weight;
                        cross_power[ch][k] = cross_power[ch][k] * (1.0 - weight) + cs * weight;
                    }
                }
            }
        }
    }
    let visible = frequencies
        .iter()
        .take_while(|&&f| f <= r.max_frequency.unwrap_or(r.sample_rate / 2.0))
        .count();
    let mut traces = vec![];
    for ch in 0..data.len() {
        if dual && ch == r.reference {
            continue;
        }
        let base_unit = unit_integral(&units[ch], integrations[ch]);
        let ref_unit = if dual {
            unit_integral(&units[r.reference], integrations[r.reference])
        } else {
            String::new()
        };
        let mut unit = match r.mode.as_str() {
            "psd" => format!("{base_unit}²/Hz"),
            "autospectrum" => format!("{base_unit}²"),
            "coherence" => "1".into(),
            "cop" => format!("{base_unit}²/Hz"),
            "cross_psd" => format!("{ref_unit}·{base_unit}/Hz"),
            "cross_spectrum" => format!("{ref_unit}·{base_unit}"),
            "h1" | "h2" | "h3" => {
                if inverse {
                    format!("{ref_unit}/{base_unit}")
                } else {
                    format!("{base_unit}/{ref_unit}")
                }
            }
            _ => base_unit.clone(),
        };
        if ["h1", "h2", "h3"].contains(&r.mode.as_str()) && r.frf_form != "custom" {
            unit = match r.frf_form.as_str() {
                "inertance" => "m/(N·s²)",
                "mobility" => "m/(N·s)",
                "compliance" => "m/N",
                "apparent_mass" => "kg",
                "impedance" => "N·s/m",
                "dynamic_stiffness" => "N/m",
                _ => unreachable!(),
            }
            .into();
        }
        let mut complex = Vec::with_capacity(bins);
        let mut coherence = Vec::with_capacity(bins);
        for k in 0..bins {
            let mut z = if dual {
                let xx = mean_psd[r.reference][k];
                let yy = mean_psd[ch][k];
                let xy = cross_psd[ch][k];
                let valid = xx > 0.0 && yy > 0.0;
                let coh = if valid {
                    (xy.norm_sqr() / (xx * yy)).clamp(0.0, 1.0)
                } else {
                    f64::NAN
                };
                coherence.push(coh);
                match r.mode.as_str() {
                    "h1" => {
                        if xx > 0.0 {
                            xy / xx
                        } else {
                            C::new(f64::NAN, f64::NAN)
                        }
                    }
                    "h2" => {
                        if xy.norm_sqr() > 0.0 {
                            C::new(yy, 0.0) / xy.conj()
                        } else {
                            C::new(f64::NAN, f64::NAN)
                        }
                    }
                    // QuickDAQ UM-25242-H p.216 explicitly defines the
                    // arithmetic estimator H3=(H1+H2)/2.
                    "h3" => {
                        if valid && xy.norm_sqr() > 0.0 {
                            (xy / xx + C::new(yy, 0.0) / xy.conj()) / 2.0
                        } else {
                            C::new(f64::NAN, f64::NAN)
                        }
                    }
                    "coherence" => C::new(coh, 0.0),
                    "cop" => C::new(coh * yy, 0.0),
                    "cross_psd" => xy,
                    "cross_spectrum" => cross_power[ch][k],
                    _ => unreachable!(),
                }
            } else {
                // Power/RMS averaging has no phase. Vector averaging retains
                // phase; peak hold retains the coefficient of the peak record.
                let amp = match r.average.as_str() {
                    "vector" => mean_fft[ch][k],
                    "peak_hold" => hold_fft[ch][k],
                    _ => C::new(mean_amp2[ch][k].sqrt(), 0.0),
                };
                match r.mode.as_str() {
                    "psd" => C::new(
                        match r.average.as_str() {
                            "vector" => {
                                amp.norm_sqr() * sums[ch].powi(2)
                                    / (factor(k, n) * r.sample_rate * powers[ch])
                            }
                            "peak_hold" => {
                                hold_fft[ch][k].norm_sqr() * sums[ch].powi(2)
                                    / (factor(k, n) * r.sample_rate * powers[ch])
                            }
                            _ => mean_psd[ch][k],
                        },
                        0.0,
                    ),
                    "autospectrum" => C::new(amp.norm_sqr() / factor(k, n), 0.0),
                    _ => amp,
                }
            };
            if inverse {
                z = if z.norm_sqr() > 0.0 {
                    C::new(1.0, 0.0) / z
                } else {
                    C::new(f64::NAN, f64::NAN)
                };
            }
            if ["amplitude", "spectrum"].contains(&r.mode.as_str()) {
                z *= match r.scale.as_str() {
                    "rms" if factor(k, n) == 2.0 => 1.0 / SQRT_2,
                    "pkpk" if k == 0 => 0.0,
                    "pkpk" => 2.0,
                    _ => 1.0,
                };
            }
            complex.push(z);
        }
        let power = [
            "psd",
            "autospectrum",
            "cross_psd",
            "cross_spectrum",
            "cop",
            "coherence",
        ]
        .contains(&r.mode.as_str());
        let values = complex[..visible]
            .iter()
            .map(|z| match r.display.as_str() {
                "phase" => z.arg() * 180.0 / PI,
                "real" => z.re,
                "imaginary" => z.im,
                "db" => {
                    if z.norm() > 0.0 {
                        (if power { 10.0 } else { 20.0 }) * (z.norm() / r.db_reference).log10()
                    } else {
                        f64::NEG_INFINITY
                    }
                }
                _ => z.norm(),
            })
            .collect();
        let quantity_unit = unit.clone();
        if r.display == "phase" {
            unit = "°".into();
        } else if r.display == "db" {
            unit = format!("dB re {} {unit}", r.db_reference);
        }
        let amplitudes: Vec<_> = mean_amp2[ch].iter().map(|v| v.sqrt()).collect();
        let peaks = spectral_peaks(
            &frequencies[..visible],
            &amplitudes[..visible],
            r.peak_count.min(10),
            &r.peak_priority,
            r.peak_min_prominence,
        );
        let mp = if r.average == "vector" {
            metric_fft[ch]
                .iter()
                .enumerate()
                .map(|(k, z)| z.norm_sqr() * factor(k, n) / (r.sample_rate * metric_window_power))
                .collect()
        } else {
            metric_psd[ch].clone()
        };
        let mut metrics = dynamic_metrics(
            &frequencies,
            &mp,
            r.sample_rate,
            r.fundamental_hz,
            r.harmonic_count,
            r.tone_half_width_bins,
        );
        metrics.set_full_scale(
            r.full_scales
                .get(ch)
                .copied()
                .or(r.full_scale)
                .map(|v| v * unit_multiplier[ch]),
        );
        metrics.unit = units[ch].clone();
        metrics.window = "blackman_harris";
        metrics.averaging = if r.average == "vector" {
            "vector"
        } else {
            "rms"
        };
        let band_density: Vec<_> = (0..bins)
            .map(|k| match r.average.as_str() {
                "vector" => {
                    mean_fft[ch][k].norm_sqr() * sums[ch].powi(2)
                        / (factor(k, n) * r.sample_rate * powers[ch])
                }
                "peak_hold" => {
                    hold_fft[ch][k].norm_sqr() * sums[ch].powi(2)
                        / (factor(k, n) * r.sample_rate * powers[ch])
                }
                _ => mean_psd[ch][k],
            })
            .collect();
        let band_power = r.band.map(|b| {
            band_density
                .iter()
                .zip(&frequencies)
                .filter(|(_, f)| **f >= b[0] && **f <= b[1])
                .map(|(p, _)| p * df)
                .sum()
        });
        traces.push(Trace {
            channel: ch,
            channel_index: r.channels.get(ch).map_or(ch, |c| c.index),
            point_number: r.channels.get(ch).and_then(|c| c.point_number),
            direction: r
                .channels
                .get(ch)
                .map_or_else(String::new, |c| c.direction.clone()),
            reference_point: if dual {
                r.channels.get(r.reference).and_then(|c| c.point_number)
            } else {
                None
            },
            reference_direction: if dual {
                r.channels
                    .get(r.reference)
                    .map_or_else(String::new, |c| c.direction.clone())
            } else {
                String::new()
            },
            reference_unit: ref_unit,
            name: r
                .channels
                .get(ch)
                .map_or_else(|| format!("通道 {}", ch), |c| c.name.clone()),
            unit,
            quantity_unit,
            values,
            real: complex[..visible].iter().map(|z| z.re).collect(),
            imaginary: complex[..visible].iter().map(|z| z.im).collect(),
            phase: complex[..visible]
                .iter()
                .map(|z| z.arg() * 180.0 / PI)
                .collect(),
            coherence: if dual {
                Some(coherence[..visible].to_vec())
            } else {
                None
            },
            band_power,
            band_power_unit: format!("{base_unit}²"),
            peaks,
            metrics,
        });
    }
    Ok(AnalysisResult {
        source: r.source.clone(),
        mode: r.mode.clone(),
        display: r.display.clone(),
        parameters: serde_json::json!({"sample_rate":r.sample_rate,"nfft":n,"mode":r.mode,"window":r.window,"reference_window":r.reference_window,"response_window":r.response_window,"reference":r.reference,"average":r.average,"weighting":r.weighting,"alpha":r.alpha,"overlap":r.overlap,"detrend":r.detrend,"integration":r.integration,"reference_integration":r.reference_integration,"scale":r.scale,"display":r.display,"db_reference":r.db_reference,"max_frequency":r.max_frequency,"band":r.band,"channels":r.channels,"frf_form":r.frf_form,"fundamental_hz":r.fundamental_hz,"full_scale":r.full_scale,"full_scales":r.full_scales,"harmonic_count":r.harmonic_count,"tone_half_width_bins":r.tone_half_width_bins}),
        frequencies: frequencies[..visible].to_vec(),
        traces,
        records: starts.len(),
        nfft: n,
        resolution_hz: df,
        record_starts: starts,
        averaging: r.average.clone(),
        weighting: r.weighting.clone(),
        phase_unit: "degrees",
        semantics: "continuous uniform samples; one-sided spectra; H3=(H1+H2)/2; null marks undefined bins; integration DC undefined; RMS averaging has zero phase",
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sine(n: usize, bin: usize, amplitude: f64) -> Vec<f64> {
        (0..n)
            .map(|i| amplitude * (TAU * bin as f64 * i as f64 / n as f64).sin())
            .collect()
    }
    fn request(data: Vec<f64>) -> AnalysisRequest {
        let n = data.len();
        AnalysisRequest {
            samples: vec![data],
            sample_rate: n as f64,
            nfft: n,
            window: Window::Named("rectangular".into()),
            tone_half_width_bins: 0,
            ..Default::default()
        }
    }
    fn close(a: f64, b: f64) {
        assert!(
            (a - b).abs() < 1e-9 * (1. + a.abs() + b.abs()),
            "{a} != {b}"
        );
    }
    fn noise(n: usize) -> Vec<f64> {
        let mut seed = 9143u64;
        (0..n)
            .map(|_| {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                ((seed >> 11) as f64 / (1u64 << 53) as f64) * 2. - 1.
            })
            .collect()
    }
    #[test]
    fn amplitude_peak_rms_pkpk_and_db() {
        for n in [1000, 1001] {
            for window in [
                "rectangular",
                "hann",
                "hamming",
                "blackman",
                "blackman_harris",
                "flattop",
            ] {
                let mut r = request(sine(n, 37, 2.4));
                r.window = Window::Named(window.into());
                let a = analyze(&r).unwrap();
                close(a.traces[0].values[37], 2.4);
                r.scale = "rms".into();
                close(analyze(&r).unwrap().traces[0].values[37], 2.4 / SQRT_2);
                r.scale = "pkpk".into();
                close(analyze(&r).unwrap().traces[0].values[37], 4.8);
                r.scale = "peak".into();
                r.display = "db".into();
                r.db_reference = 2.4;
                close(analyze(&r).unwrap().traces[0].values[37], 0.0);
            }
        }
    }
    #[test]
    fn all_windows_obey_psd_parseval_and_are_finite() {
        for n in [127, 128, 1024] {
            for name in [
                "rectangular",
                "hann",
                "hamming",
                "bartlett",
                "blackman",
                "blackman_harris",
                "flattop",
                "force",
                "exponential",
                "cosine_taper",
            ] {
                let x = noise(n);
                let mut r = request(x.clone());
                r.mode = "psd".into();
                r.window = Window::Named(name.into());
                let w = r.window.weights(n).unwrap();
                let expected = x.iter().zip(&w).map(|(x, w)| (x * w).powi(2)).sum::<f64>()
                    / w.iter().map(|w| w * w).sum::<f64>();
                let out = analyze(&r).unwrap();
                close(
                    out.traces[0].values.iter().sum::<f64>() * out.resolution_hz,
                    expected,
                );
            }
        }
    }
    #[test]
    fn dc_nyquist_and_odd_final_bin_are_not_mis_scaled() {
        let r = request(vec![3.5; 1024]);
        close(analyze(&r).unwrap().traces[0].values[0], 3.5);
        let r = request(
            (0..1024)
                .map(|i| if i % 2 == 0 { 1.7 } else { -1.7 })
                .collect(),
        );
        close(analyze(&r).unwrap().traces[0].values[512], 1.7);
        let r = request(sine(101, 50, 1.7));
        close(analyze(&r).unwrap().traces[0].values[50], 1.7);
        let mut dc = request(vec![3.5; 1024]);
        dc.scale = "rms".into();
        close(analyze(&dc).unwrap().traces[0].values[0], 3.5);
        dc.scale = "pkpk".into();
        close(analyze(&dc).unwrap().traces[0].values[0], 0.0);
        let mut nyquist = request(
            (0..1024)
                .map(|i| if i % 2 == 0 { 1.7 } else { -1.7 })
                .collect(),
        );
        nyquist.scale = "rms".into();
        close(analyze(&nyquist).unwrap().traces[0].values[512], 1.7);
        nyquist.scale = "pkpk".into();
        close(analyze(&nyquist).unwrap().traces[0].values[512], 3.4);
    }
    #[test]
    fn average_modes_and_exponential_weights() {
        let n = 256;
        let one = sine(n, 17, 1.);
        let mut data = one.clone();
        data.extend(one.iter().map(|v| -v));
        let mut r = request(data);
        r.nfft = n;
        r.overlap = 0.;
        close(analyze(&r).unwrap().traces[0].values[17], 1.);
        r.average = "vector".into();
        close(analyze(&r).unwrap().traces[0].values[17], 0.);
        r.average = "peak_hold".into();
        close(analyze(&r).unwrap().traces[0].values[17], 1.);
        r.samples[0] = one.clone();
        r.samples[0].extend(one.iter().map(|v| 3. * v));
        r.average = "rms".into();
        r.weighting = "exponential".into();
        r.alpha = 0.2;
        close(analyze(&r).unwrap().traces[0].values[17], 2.6f64.sqrt());
        r.average = "peak_hold".into();
        assert!(analyze(&r).is_err());
    }
    #[test]
    fn all_single_modes_displays_and_averages_are_usable() {
        for mode in ["amplitude", "spectrum", "autospectrum", "psd"] {
            for display in ["magnitude", "phase", "real", "imaginary", "db", "nyquist"] {
                for average in ["rms", "vector", "peak_hold"] {
                    let mut r = request(sine(1024, 13, 2.));
                    r.nfft = 256;
                    r.mode = mode.into();
                    r.display = display.into();
                    r.average = average.into();
                    let a = analyze(&r).unwrap();
                    assert_eq!(a.traces[0].values.len(), 129);
                    assert!(a.traces[0].values.iter().any(|x| x.is_finite()));
                }
            }
        }
    }
    #[test]
    fn frf_all_estimators_recover_signed_gain_phase() {
        let x = noise(4096);
        let y: Vec<_> = x.iter().map(|v| -2.5 * v).collect();
        for mode in ["h1", "h2", "h3"] {
            for weighting in ["linear", "exponential"] {
                let r = AnalysisRequest {
                    samples: vec![x.clone(), y.clone()],
                    sample_rate: 1000.,
                    nfft: 256,
                    mode: mode.into(),
                    weighting: weighting.into(),
                    ..Default::default()
                };
                let out = analyze(&r).unwrap();
                let t = &out.traces[0];
                for ((re, im), coh) in t
                    .real
                    .iter()
                    .zip(&t.imaginary)
                    .zip(t.coherence.as_ref().unwrap())
                {
                    close(*re, -2.5);
                    close(*im, 0.);
                    close(*coh, 1.);
                }
                close(t.phase[50].abs(), 180.);
            }
        }
    }
    #[test]
    fn cross_power_psd_coherence_cop_have_correct_normalization() {
        let x = sine(1024, 64, 1.);
        let y: Vec<_> = x.iter().map(|v| 2. * v).collect();
        let mut r = AnalysisRequest {
            samples: vec![x, y],
            sample_rate: 1024.,
            nfft: 256,
            window: Window::Named("rectangular".into()),
            mode: "cross_spectrum".into(),
            ..Default::default()
        };
        close(analyze(&r).unwrap().traces[0].real[16], 1.);
        r.mode = "cross_psd".into();
        close(analyze(&r).unwrap().traces[0].real[16], 0.25);
        r.mode = "coherence".into();
        close(analyze(&r).unwrap().traces[0].values[16], 1.);
        r.mode = "cop".into();
        close(analyze(&r).unwrap().traces[0].values[16], 0.5);
        for mode in [
            "h1",
            "h2",
            "h3",
            "cross_spectrum",
            "cross_psd",
            "coherence",
            "cop",
        ] {
            r.mode = mode.into();
            for display in ["magnitude", "phase", "real", "imaginary", "db", "nyquist"] {
                r.display = display.into();
                assert!(analyze(&r).is_ok());
            }
        }
    }
    #[test]
    fn response_delay_and_frequency_integrals() {
        let x = sine(4096, 256, 1.);
        let y: Vec<_> = (0..4096)
            .map(|i| 3. * (TAU * 64. * (i as f64 - 2.) / 1024.).sin())
            .collect();
        let mut r = AnalysisRequest {
            samples: vec![x, y],
            sample_rate: 1024.,
            nfft: 256,
            mode: "h1".into(),
            ..Default::default()
        };
        let t = &analyze(&r).unwrap().traces[0];
        close(t.values[16], 3.);
        close(t.phase[16], -45.);
        r.integration = 1;
        let out = analyze(&r).unwrap();
        close(out.traces[0].values[16], 3. / (TAU * 64.));
        close(out.traces[0].phase[16], -135.);
        assert!(out.traces[0].values[0].is_nan());
        r.integration = 2;
        close(
            analyze(&r).unwrap().traces[0].values[16],
            3. / (TAU * 64.).powi(2),
        );
    }
    #[test]
    fn physical_frf_forms_preserve_units_and_reciprocal() {
        let x = noise(2048);
        let y: Vec<_> = x.iter().map(|v| 2. * v).collect();
        let mut r = AnalysisRequest {
            samples: vec![x, y],
            nfft: 256,
            mode: "h1".into(),
            channels: vec![
                ChannelSettings {
                    unit: "N".into(),
                    ..Default::default()
                },
                ChannelSettings {
                    unit: "m/s²".into(),
                    ..Default::default()
                },
            ],
            ..Default::default()
        };
        for (form, integral, inverse) in [
            ("inertance", 0, false),
            ("mobility", 1, false),
            ("compliance", 2, false),
            ("apparent_mass", 0, true),
            ("impedance", 1, true),
            ("dynamic_stiffness", 2, true),
        ] {
            r.frf_form = form.into();
            let out = analyze(&r).unwrap();
            let expected = 2. / (TAU * out.frequencies[20]).powi(integral);
            close(
                out.traces[0].values[20],
                if inverse { 1. / expected } else { expected },
            );
        }
        r.channels[0].unit = "V".into();
        assert!(analyze(&r).is_err());
    }
    #[test]
    fn band_power_record_indices_and_json_null() {
        let mut r = request(sine(1024, 32, 2.));
        r.nfft = 256;
        r.sample_rate = 1024.;
        r.band = Some([28., 36.]);
        r.max_frequency = Some(100.);
        r.mode = "psd".into();
        let out = analyze(&r).unwrap();
        assert_eq!(out.records, 7);
        assert_eq!(out.record_starts, [0, 128, 256, 384, 512, 640, 768]);
        close(out.traces[0].band_power.unwrap(), 2.);
        assert_eq!(out.frequencies.last(), Some(&100.));
        let v = analyze_json(
            &serde_json::json!({"samples":[vec![0.;512],vec![0.;512]],"nfft":128,"mode":"h1"}),
        )
        .unwrap();
        assert!(v["traces"][0]["values"][5].is_null());
    }
    #[test]
    fn invalid_input_parameters_fail_explicitly() {
        let base = request(sine(256, 13, 1.));
        let changes = vec![
            serde_json::json!({"sample_rate":0}),
            serde_json::json!({"nfft":1024}),
            serde_json::json!({"overlap":1.0}),
            serde_json::json!({"mode":"unknown"}),
            serde_json::json!({"window":"unknown"}),
            serde_json::json!({"alpha":0}),
            serde_json::json!({"integration":3}),
            serde_json::json!({"scale":"unknown"}),
            serde_json::json!({"db_reference":0}),
            serde_json::json!({"display":"unknown"}),
            serde_json::json!({"band":[50,10]}),
            serde_json::json!({"raw_volts":true}),
            serde_json::json!({"mode":"h1"}),
            serde_json::json!({"max_frequency":1000}),
            serde_json::json!({"fundamental_hz":-1}),
        ];
        for change in changes {
            let mut v = serde_json::to_value(&base).unwrap();
            for (k, value) in change.as_object().unwrap() {
                v[k] = value.clone();
            }
            assert!(analyze_json(&v).is_err(), "{change}");
        }
        let mut r = base.clone();
        r.samples[0][0] = f64::NAN;
        assert!(analyze(&r).is_err());
        let mut r = base;
        r.samples.push(vec![0.; 2]);
        assert!(analyze(&r).is_err());
    }
    #[test]
    fn h3_matches_documented_arithmetic_estimator_under_noise() {
        let x = noise(4096);
        let n2 = noise(8192);
        let y: Vec<_> = x
            .iter()
            .enumerate()
            .map(|(i, v)| 2. * v + 0.8 * n2[i + 4096])
            .collect();
        let mut r = AnalysisRequest {
            samples: vec![x, y],
            nfft: 256,
            mode: "h1".into(),
            ..Default::default()
        };
        let h1 = analyze(&r).unwrap();
        r.mode = "h2".into();
        let h2 = analyze(&r).unwrap();
        r.mode = "h3".into();
        let h3 = analyze(&r).unwrap();
        assert!((h1.traces[0].values[20] - h2.traces[0].values[20]).abs() > 0.01);
        for k in 0..129 {
            close(
                h3.traces[0].real[k],
                (h1.traces[0].real[k] + h2.traces[0].real[k]) / 2.,
            );
            close(
                h3.traces[0].imaginary[k],
                (h1.traces[0].imaginary[k] + h2.traces[0].imaginary[k]) / 2.,
            );
        }
    }
    #[test]
    fn ibf_full_scale_sfdr_and_enob_follow_handbook_units() {
        let mut x = sine(4096, 200, 1.);
        for (v, h) in x.iter_mut().zip(sine(4096, 400, 0.01)) {
            *v += h;
        }
        let mut r = request(x);
        r.full_scale = Some(10.);
        r.tone_half_width_bins = 4;
        let result = analyze(&r).unwrap();
        let m = &result.traces[0].metrics;
        assert!((m.ibf_db.unwrap() + 20.).abs() < 1e-7);
        assert!((m.thd_percent.unwrap() - 1.).abs() < 1e-7);
        close(m.sfdr_dbfs.unwrap(), m.sfdr_db.unwrap() + 20.);
        close(m.enob.unwrap(), (m.sinad_db.unwrap() - 1.76 + 20.) / 6.02);
        r.full_scale = None;
        let result = analyze(&r).unwrap();
        let m = &result.traces[0].metrics;
        assert!(m.ibf_db.is_none() && m.sfdr_dbfs.is_none() && m.enob.is_none());
    }
    #[test]
    fn parameterized_windows_match_endpoints_and_reject_invalid() {
        let w = Window::Parameterized {
            name: "force".into(),
            fraction: 0.25,
            end_amplitude: 0.01,
        }
        .weights(100)
        .unwrap();
        assert_eq!(w.iter().sum::<f64>(), 25.);
        let w = Window::Parameterized {
            name: "exponential".into(),
            fraction: 0.1,
            end_amplitude: 0.001,
        }
        .weights(100)
        .unwrap();
        close(w[0], 1.);
        close(w[99], 0.001);
        for fraction in [0., -1., 1.01, f64::NAN] {
            assert!(
                Window::Parameterized {
                    name: "cosine_taper".into(),
                    fraction,
                    end_amplitude: 0.01
                }
                .weights(100)
                .is_err()
            );
        }
    }
    #[test]
    fn raw_voltage_conversion_uses_supplied_units_and_names() {
        let mut r = request(sine(512, 37, 0.1));
        r.raw_volts = true;
        r.channels = vec![ChannelSettings {
            name: "力".into(),
            unit: "N".into(),
            sensitivity_mv_per_unit: 10.,
            ..Default::default()
        }];
        let a = analyze(&r).unwrap();
        close(a.traces[0].values[37], 10.);
        assert_eq!(a.traces[0].unit, "N");
        assert_eq!(a.traces[0].name, "力");
        r.integration = 1;
        let a = analyze(&r).unwrap();
        assert_eq!(a.traces[0].unit, "N·s");
    }
    #[test]
    fn band_integral_follows_vector_and_peak_hold_display() {
        let mut x = sine(256, 13, 1.);
        x.extend(sine(256, 13, -1.));
        let mut r = request(x);
        r.nfft = 256;
        r.sample_rate = 256.;
        r.overlap = 0.;
        r.mode = "psd".into();
        r.band = Some([10., 16.]);
        for average in ["rms", "vector", "peak_hold"] {
            r.average = average.into();
            let a = analyze(&r).unwrap();
            let integrated = a.traces[0].values[10..=16].iter().sum::<f64>();
            close(a.traces[0].band_power.unwrap(), integrated);
        }
    }
    #[test]
    fn largest_supported_fft_resolves_known_tone_without_truncation() {
        let mut r = request(sine(262144, 901, 1.25));
        r.peak_count = 1;
        let result = analyze(&r).unwrap();
        assert_eq!(result.frequencies.len(), 131073);
        close(result.traces[0].values[901], 1.25);
        assert_eq!(result.traces[0].peaks[0].bin, 901);
        assert_eq!(result.parameters["nfft"], 262144);
        assert_eq!(result.display, "magnitude");
    }
    #[test]
    fn physical_frf_labels_and_g_full_scale_preserve_dimensions() {
        let x = sine(4096, 200, 1.);
        let y: Vec<_> = x.iter().map(|v| 2. * v).collect();
        let mut r = AnalysisRequest {
            samples: vec![x, y],
            sample_rate: 4096.,
            nfft: 1024,
            mode: "h1".into(),
            channels: vec![
                ChannelSettings {
                    unit: "N".into(),
                    ..Default::default()
                },
                ChannelSettings {
                    unit: "g".into(),
                    ..Default::default()
                },
            ],
            full_scales: vec![10., 20.],
            ..Default::default()
        };
        for (form, unit) in [
            ("inertance", "m/(N·s²)"),
            ("mobility", "m/(N·s)"),
            ("compliance", "m/N"),
            ("apparent_mass", "kg"),
            ("impedance", "N·s/m"),
            ("dynamic_stiffness", "N/m"),
        ] {
            r.frf_form = form.into();
            let result = analyze(&r).unwrap();
            assert_eq!(result.traces[0].quantity_unit, unit);
            assert_eq!(result.traces[0].metrics.unit, "m/s²");
            assert!((result.traces[0].metrics.ibf_db.unwrap() + 20.).abs() < 1e-7);
        }
    }
}
