//! Analog prototype -> prewarped bilinear IIR design and transposed-form SOS.
//! Bessel uses magnitude normalization (-3 dB at the specified cutoff).
//! Chebyshev is type I: cutoff denotes the passband edge and ripple is in dB.
use num_complex::Complex64 as C;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::f64::consts::{PI, TAU};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct FilterSpec {
    pub sample_rate: f64,
    pub family: String,
    pub kind: String,
    pub order: usize,
    pub cutoff_hz: f64,
    pub cutoff_high_hz: Option<f64>,
    pub ripple_db: f64,
}
impl Default for FilterSpec {
    fn default() -> Self {
        Self {
            sample_rate: 1000.0,
            family: "butterworth".into(),
            kind: "lowpass".into(),
            order: 4,
            cutoff_hz: 100.0,
            cutoff_high_hz: None,
            ripple_db: 1.0,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Sos {
    pub b: [f64; 3],
    pub a: [f64; 3],
}
impl Sos {
    pub fn response(&self, frequency_hz: f64, sample_rate: f64) -> C {
        let z = C::from_polar(1.0, -TAU * frequency_hz / sample_rate);
        (self.b[0] + self.b[1] * z + self.b[2] * z * z)
            / (self.a[0] + self.a[1] * z + self.a[2] * z * z)
    }
}

fn factorial(n: usize) -> f64 {
    (1..=n).fold(1.0, |a, b| a * b as f64)
}
fn polynomial(coefficients: &[f64], z: C) -> C {
    coefficients
        .iter()
        .rev()
        .fold(C::new(0., 0.), |v, c| v * z + c)
}
fn bessel_poles(n: usize) -> Result<Vec<C>, String> {
    if n == 1 {
        return Ok(vec![C::new(-1.0, 0.0)]);
    }
    // Roots of the reverse Bessel polynomial, scaled before root finding to
    // avoid the large coefficient dynamic range of the delay-normalized form.
    let coefficients: Vec<_> = (0..=n)
        .map(|k| {
            factorial(2 * n - k) / (2f64.powi((n - k) as i32) * factorial(k) * factorial(n - k))
        })
        .collect();
    let scale = coefficients[0].powf(1.0 / n as f64);
    let scaled: Vec<_> = coefficients
        .iter()
        .enumerate()
        .map(|(k, c)| c * scale.powi(k as i32) / coefficients[0])
        .collect();
    let derivative: Vec<_> = scaled
        .iter()
        .enumerate()
        .skip(1)
        .map(|(k, c)| *c * k as f64)
        .collect();
    let mut roots: Vec<_> = (0..n)
        .map(|i| C::from_polar(1.2, TAU * (i as f64 + 0.25) / n as f64))
        .collect();
    let mut converged = false;
    for _ in 0..2000 {
        let previous = roots.clone();
        let mut max_change: f64 = 0.0;
        for i in 0..n {
            let repulsion = previous
                .iter()
                .enumerate()
                .filter(|(j, _)| *j != i)
                .fold(C::new(0., 0.), |a, (_, z)| {
                    a + C::new(1., 0.) / (previous[i] - z)
                });
            let newton = polynomial(&scaled, previous[i]) / polynomial(&derivative, previous[i]);
            let change = newton / (C::new(1., 0.) - newton * repulsion);
            roots[i] = previous[i] - change;
            max_change = max_change.max(change.norm());
        }
        if max_change < 2e-11 {
            converged = true;
            break;
        }
    }
    if !converged
        || roots
            .iter()
            .any(|p| !p.re.is_finite() || !p.im.is_finite() || p.re >= 0.)
    {
        return Err("Bessel 原型求根未收敛".into());
    }
    // Clean conjugate roundoff and normalize the -3 dB frequency to 1 rad/s.
    for p in &mut roots {
        if p.im.abs() < 1e-10 {
            p.im = 0.0;
        }
    }
    let dc = roots.iter().fold(C::new(1., 0.), |a, p| a * (-p)).norm();
    let magnitude = |w: f64| {
        dc / roots
            .iter()
            .fold(C::new(1., 0.), |a, p| a * (C::new(0., w) - p))
            .norm()
    };
    let mut low = 0.0;
    let mut high = 10.0;
    for _ in 0..80 {
        let mid = (low + high) / 2.;
        if magnitude(mid) > std::f64::consts::FRAC_1_SQRT_2 {
            low = mid
        } else {
            high = mid
        }
    }
    let cutoff = (low + high) / 2.;
    for p in &mut roots {
        *p /= cutoff;
    }
    Ok(roots)
}

fn prototype(spec: &FilterSpec) -> Result<Vec<C>, String> {
    let n = spec.order;
    match spec.family.as_str() {
        "butterworth" | "butter" => Ok((0..n)
            .map(|i| {
                let theta = PI * (2 * i + 1) as f64 / (2 * n) as f64;
                C::new(-theta.sin(), theta.cos())
            })
            .collect()),
        "chebyshev" | "chebyshev1" | "cheby1" => {
            if !spec.ripple_db.is_finite() || spec.ripple_db <= 0.0 || spec.ripple_db > 60.0 {
                return Err("Chebyshev 通带纹波需在 (0,60] dB".into());
            }
            let epsilon = (10f64.powf(spec.ripple_db / 10.) - 1.).sqrt();
            let mu = (1. / epsilon).asinh() / n as f64;
            Ok((0..n)
                .map(|i| {
                    let theta = PI * (2 * i + 1) as f64 / (2 * n) as f64;
                    C::new(-mu.sinh() * theta.sin(), mu.cosh() * theta.cos())
                })
                .collect())
        }
        "bessel" => bessel_poles(n),
        _ => Err(format!("未知滤波器类型：{}", spec.family)),
    }
}

fn pairs(mut roots: Vec<C>) -> Result<Vec<[C; 2]>, String> {
    let mut result = vec![];
    while let Some(p) = roots.pop() {
        if roots.is_empty() {
            if p.im.abs() > 1e-7 {
                return Err("滤波器共轭极点不完整".into());
            }
            result.push([C::new(p.re, 0.), C::new(0., 0.)]);
            break;
        }
        let target = if p.im.abs() > 1e-7 {
            p.conj()
        } else {
            C::new(p.re, 0.)
        };
        let index = roots
            .iter()
            .enumerate()
            .filter(|(_, z)| p.im.abs() > 1e-7 || z.im.abs() < 1e-7)
            .min_by(|(_, a), (_, b)| {
                (**a - target)
                    .norm_sqr()
                    .total_cmp(&(**b - target).norm_sqr())
            })
            .map(|(i, _)| i);
        if let Some(index) = index {
            let q = roots.remove(index);
            if p.im.abs() > 1e-7 && (q - p.conj()).norm() > 1e-6 {
                return Err("滤波器共轭配对误差".into());
            }
            let (p, q) = if p.im.abs() > 1e-7 {
                let z = (p + q.conj()) / 2.;
                (z, z.conj())
            } else {
                (C::new(p.re, 0.), C::new(q.re, 0.))
            };
            result.push([p, q]);
        } else {
            result.push([C::new(p.re, 0.), C::new(0., 0.)]);
        }
    }
    Ok(result)
}

pub fn design_filter(spec: &FilterSpec) -> Result<Vec<Sos>, String> {
    if !spec.sample_rate.is_finite() || spec.sample_rate <= 0.0 || !(1..=10).contains(&spec.order) {
        return Err("采样率需为正有限数；滤波器原型阶数为 1–10".into());
    }
    if !spec.cutoff_hz.is_finite()
        || spec.cutoff_hz <= 0.0
        || spec.cutoff_hz >= spec.sample_rate / 2.0
    {
        return Err("截止频率需满足 0 < fc < fs/2".into());
    }
    let fs = spec.sample_rate;
    let warp = |f: f64| 2.0 * fs * (PI * f / fs).tan();
    let w1 = warp(spec.cutoff_hz);
    let proto = prototype(spec)?;
    let n = spec.order;
    let (poles, zeros, reference_frequency) = match spec.kind.as_str() {
        "lowpass" => (
            proto.iter().map(|p| p * w1).collect::<Vec<_>>(),
            vec![],
            0.0,
        ),
        "highpass" => (
            proto.iter().map(|p| w1 / p).collect(),
            vec![C::new(0., 0.); n],
            fs / 2.0,
        ),
        "bandpass" | "bandstop" => {
            let high = spec.cutoff_high_hz.ok_or("带通/带阻需要上截止频率")?;
            if !high.is_finite() || high <= spec.cutoff_hz || high >= fs / 2.0 {
                return Err("带通/带阻需满足 0 < f低 < f高 < fs/2".into());
            }
            let w2 = warp(high);
            let center = (w1 * w2).sqrt();
            let bandwidth = w2 - w1;
            let mut p = vec![];
            for root in proto {
                let a = if spec.kind == "bandpass" {
                    root * bandwidth
                } else {
                    bandwidth / root
                };
                let disc = (a * a - 4.0 * center * center).sqrt();
                p.extend([(a + disc) / 2., (a - disc) / 2.]);
            }
            let z = if spec.kind == "bandpass" {
                vec![C::new(0., 0.); n]
            } else {
                (0..n)
                    .flat_map(|_| [C::new(0., center), C::new(0., -center)])
                    .collect()
            };
            (
                p,
                z,
                if spec.kind == "bandpass" {
                    fs / PI * (center / (2. * fs)).atan()
                } else {
                    0.0
                },
            )
        }
        _ => return Err("滤波类型需为 lowpass/highpass/bandpass/bandstop".into()),
    };
    let map = |p: C| (2. * fs + p) / (2. * fs - p);
    let poles: Vec<_> = poles.into_iter().map(map).collect();
    if poles.iter().any(|p| p.norm() >= 1.) {
        return Err("数字滤波器不稳定".into());
    }
    let mut zeros: Vec<_> = zeros.into_iter().map(map).collect();
    while zeros.len() < poles.len() {
        zeros.push(C::new(-1., 0.));
    }
    let pp = pairs(poles)?;
    let zz = pairs(zeros)?;
    let mut sections: Vec<_> = pp
        .iter()
        .zip(zz)
        .map(|(p, z)| Sos {
            b: [1., -(z[0] + z[1]).re, (z[0] * z[1]).re],
            a: [1., -(p[0] + p[1]).re, (p[0] * p[1]).re],
        })
        .collect();
    let current = response(&sections, reference_frequency, fs).norm();
    let target =
        if ["chebyshev", "chebyshev1", "cheby1"].contains(&spec.family.as_str()) && n % 2 == 0 {
            10f64.powf(-spec.ripple_db / 20.)
        } else {
            1.
        };
    let gain = (target / current).powf(1. / sections.len() as f64);
    if !gain.is_finite() {
        return Err("滤波器增益归一化失败".into());
    }
    for s in &mut sections {
        for b in &mut s.b {
            *b *= gain;
        }
    }
    Ok(sections)
}

pub fn response(sos: &[Sos], frequency_hz: f64, sample_rate: f64) -> C {
    sos.iter().fold(C::new(1., 0.), |a, s| {
        a * s.response(frequency_hz, sample_rate)
    })
}

#[derive(Clone, Debug)]
pub struct StreamingFilter {
    pub sos: Vec<Sos>,
    state: Vec<Vec<[f64; 2]>>,
}
impl StreamingFilter {
    pub fn new(sos: Vec<Sos>, channel_count: usize) -> Result<Self, String> {
        if channel_count == 0 || sos.is_empty() {
            return Err("滤波器需要至少一个通道和一个二阶节".into());
        }
        Ok(Self {
            state: vec![vec![[0., 0.]; sos.len()]; channel_count],
            sos,
        })
    }
    pub fn reset(&mut self) {
        for ch in &mut self.state {
            ch.fill([0., 0.]);
        }
    }
    fn steady(&mut self, ch: usize, mut input: f64) {
        for (s, state) in self.sos.iter().zip(&mut self.state[ch]) {
            let sum_a = s.a.iter().sum::<f64>();
            let gain = s.b.iter().sum::<f64>() / sum_a;
            let output = gain * input;
            state[0] = output - s.b[0] * input;
            state[1] = s.b[2] * input - s.a[2] * output;
            input = output;
        }
    }
    pub fn process(&mut self, samples: &[Vec<f64>]) -> Result<Vec<Vec<f64>>, String> {
        if samples.len() != self.state.len()
            || samples.iter().any(|c| c.iter().any(|v| !v.is_finite()))
        {
            return Err("滤波数据通道数不符或包含非有限值".into());
        }
        let mut out = samples.to_vec();
        for (ch, data) in out.iter_mut().enumerate() {
            for x in data {
                for (s, z) in self.sos.iter().zip(&mut self.state[ch]) {
                    let y = s.b[0] * (*x) + z[0];
                    z[0] = s.b[1] * (*x) - s.a[1] * y + z[1];
                    z[1] = s.b[2] * (*x) - s.a[2] * y;
                    *x = y;
                }
            }
        }
        Ok(out)
    }
}

pub fn zero_phase(sos: &[Sos], samples: &[Vec<f64>]) -> Result<Vec<Vec<f64>>, String> {
    let pad = 3 * (2 * sos.len() + 1);
    if samples.is_empty() || samples.iter().any(|c| c.len() <= pad) {
        return Err(format!("零相位滤波每通道至少需要 {} 个样本", pad + 1));
    }
    let mut padded = vec![];
    for c in samples {
        let mut p = Vec::with_capacity(c.len() + 2 * pad);
        p.extend((1..=pad).rev().map(|i| 2. * c[0] - c[i]));
        p.extend(c);
        p.extend((1..=pad).map(|i| 2. * c[c.len() - 1] - c[c.len() - 1 - i]));
        padded.push(p);
    }
    let mut filter = StreamingFilter::new(sos.to_vec(), samples.len())?;
    for (ch, p) in padded.iter().enumerate() {
        filter.steady(ch, p[0]);
    }
    let mut forward = filter.process(&padded)?;
    for c in &mut forward {
        c.reverse();
    }
    filter.reset();
    for (ch, p) in forward.iter().enumerate() {
        filter.steady(ch, p[0]);
    }
    let mut backward = filter.process(&forward)?;
    for c in &mut backward {
        c.reverse();
        *c = c[pad..c.len() - pad].to_vec();
    }
    Ok(backward)
}

pub fn filter_json(request: &Value) -> Result<Value, String> {
    let spec: FilterSpec = serde_json::from_value(request.clone()).map_err(|e| e.to_string())?;
    let sos = design_filter(&spec)?;
    let samples: Vec<Vec<f64>> =
        serde_json::from_value(request.get("samples").cloned().unwrap_or(json!([])))
            .map_err(|e| e.to_string())?;
    let zero = request["zero_phase"].as_bool().unwrap_or(false);
    let filtered = if samples.is_empty() {
        vec![]
    } else if zero {
        zero_phase(&sos, &samples)?
    } else {
        StreamingFilter::new(sos.clone(), samples.len())?.process(&samples)?
    };
    let frequencies: Vec<_> = (0..=512)
        .map(|k| spec.sample_rate * k as f64 / 1024.)
        .collect();
    let h: Vec<_> = frequencies
        .iter()
        .map(|f| response(&sos, *f, spec.sample_rate))
        .collect();
    let mut impulse = vec![0.; 512];
    impulse[0] = 1.;
    let mut stream = StreamingFilter::new(sos.clone(), 2)?;
    let transient = stream.process(&[impulse, vec![1.; 512]])?;
    Ok(
        json!({"samples":filtered,"sos":sos,"stable":true,"frequencies":frequencies,
        "magnitude":h.iter().map(|z|if zero{z.norm_sqr()}else{z.norm()}).collect::<Vec<_>>(),
        "phase":h.iter().map(|z|if zero{0.0}else{z.arg()*180./PI}).collect::<Vec<_>>(),
        "impulse":transient[0],"step":transient[1],"response_sample_rate":spec.sample_rate,
        "zero_phase":zero,"transient_response":"causal single pass",
        "cutoff_definition":if spec.family=="bessel"{"magnitude normalized -3 dB"}else if spec.family.starts_with("cheb"){"passband edge at -ripple_db"}else{"-3 dB"},
        "effective_order":spec.order*(if spec.kind.starts_with("band"){2}else{1})*(if zero{2}else{1})}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_prototype_order_and_band_has_valid_stable_response() {
        for family in ["butterworth", "bessel", "chebyshev"] {
            for order in 1..=10 {
                for kind in ["lowpass", "highpass", "bandpass", "bandstop"] {
                    let spec = FilterSpec {
                        family: family.into(),
                        order,
                        kind: kind.into(),
                        sample_rate: 2000.,
                        cutoff_hz: 100.,
                        cutoff_high_hz: Some(400.),
                        ..Default::default()
                    };
                    let sos = design_filter(&spec)
                        .unwrap_or_else(|e| panic!("{family} {order} {kind}: {e}"));
                    for f in [0., 50., 100., 250., 400., 700., 1000.] {
                        assert!(
                            response(&sos, f, 2000.).norm().is_finite(),
                            "{family} {order} {kind}"
                        );
                    }
                    if family != "chebyshev" {
                        assert!(
                            (response(&sos, 100., 2000.).norm() - std::f64::consts::FRAC_1_SQRT_2)
                                .abs()
                                < 2e-7,
                            "{family} {order} {kind}"
                        );
                    }
                    let mut f = StreamingFilter::new(sos, 1).unwrap();
                    let mut impulse = vec![0.; 20000];
                    impulse[0] = 1.;
                    let out = f.process(&[impulse]).unwrap();
                    assert!(out[0].iter().all(|x| x.is_finite() && x.abs() < 5.0));
                    assert!(
                        out[0][19000..].iter().all(|x| x.abs() < 1e-6),
                        "{family} {order} {kind}"
                    );
                }
            }
        }
    }
    #[test]
    fn streaming_blocks_match_whole_and_reset() {
        let s = design_filter(&FilterSpec::default()).unwrap();
        let x: Vec<_> = (0..1003).map(|i| (i as f64 * 0.31).sin()).collect();
        let expected = StreamingFilter::new(s.clone(), 1)
            .unwrap()
            .process(&[x.clone()])
            .unwrap();
        let mut f = StreamingFilter::new(s, 1).unwrap();
        let mut actual = vec![];
        for c in x.chunks(7) {
            actual.extend(f.process(&[c.to_vec()]).unwrap().remove(0));
        }
        assert_eq!(actual, expected[0]);
        f.reset();
        assert_eq!(f.process(&[x]).unwrap(), expected);
    }
    #[test]
    fn zero_phase_preserves_constant_and_phase_of_interior_tone() {
        let spec = FilterSpec {
            sample_rate: 2000.,
            cutoff_hz: 100.,
            ..Default::default()
        };
        let s = design_filter(&spec).unwrap();
        let c = vec![vec![10.; 16000], vec![-5.; 16000]];
        let out = zero_phase(&s, &c).unwrap();
        for (a, b) in out.iter().flatten().zip(c.iter().flatten()) {
            assert!((a - b).abs() < 1e-10);
        }
        let tone: Vec<_> = (0..16000)
            .map(|i| (TAU * 50. * i as f64 / 2000.).sin())
            .collect();
        let out = zero_phase(&s, &[tone.clone()]).unwrap();
        let gain = response(&s, 50., 2000.).norm_sqr();
        for i in 1000..15000 {
            assert!((out[0][i] - gain * tone[i]).abs() < 1e-10);
        }
        assert!(zero_phase(&s, &[vec![0.; 10]]).is_err());
    }
    #[test]
    fn filter_rejects_invalid_parameters() {
        for spec in [
            FilterSpec {
                order: 0,
                ..Default::default()
            },
            FilterSpec {
                order: 11,
                ..Default::default()
            },
            FilterSpec {
                cutoff_hz: 500.,
                ..Default::default()
            },
            FilterSpec {
                kind: "bandpass".into(),
                ..Default::default()
            },
            FilterSpec {
                family: "unknown".into(),
                ..Default::default()
            },
            FilterSpec {
                family: "cheby1".into(),
                ripple_db: 0.,
                ..Default::default()
            },
        ] {
            assert!(design_filter(&spec).is_err());
        }
        let mut f =
            StreamingFilter::new(design_filter(&FilterSpec::default()).unwrap(), 1).unwrap();
        assert!(f.process(&[vec![f64::NAN]]).is_err());
        assert!(f.process(&[]).is_err());
    }
    #[test]
    fn edge_cutoffs_and_json_response_have_declared_order() {
        for family in ["butterworth", "bessel", "chebyshev"] {
            for order in [1, 5, 10] {
                for cutoff in [0.1, 1., 100., 450.] {
                    let spec = FilterSpec {
                        sample_rate: 1000.,
                        family: family.into(),
                        order,
                        cutoff_hz: cutoff,
                        ..Default::default()
                    };
                    let sos = design_filter(&spec).unwrap();
                    let expected = if family == "chebyshev" {
                        10f64.powf(-spec.ripple_db / 20.)
                    } else {
                        std::f64::consts::FRAC_1_SQRT_2
                    };
                    assert!(
                        (response(&sos, cutoff, 1000.).norm() - expected).abs() < 3e-6,
                        "{family} {order} {cutoff}"
                    );
                }
            }
        }
        let out=filter_json(&json!({"family":"butterworth","kind":"bandpass","order":4,"cutoff_hz":50,"cutoff_high_hz":150,"zero_phase":true,"samples":[vec![0.;1000]]})).unwrap();
        assert_eq!(out["effective_order"], 16);
        assert_eq!(out["frequencies"].as_array().unwrap().len(), 513);
        assert_eq!(out["samples"][0].as_array().unwrap().len(), 1000);
        assert_eq!(out["stable"], true);
    }
}
