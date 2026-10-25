use serde::Serialize;

#[derive(Clone, Debug, Serialize)]
pub struct Peak {
    pub bin: usize,
    pub frequency_hz: f64,
    pub amplitude: f64,
    pub prominence: f64,
}

/// Local spectral maxima, ordered by amplitude or two-sided prominence.
/// Plateau maxima use their first bin. DC and Nyquist are not local peaks.
pub fn spectral_peaks(
    frequencies: &[f64],
    amplitudes: &[f64],
    count: usize,
    priority: &str,
    min_prominence: f64,
) -> Vec<Peak> {
    if frequencies.len() != amplitudes.len() || amplitudes.len() < 3 || count == 0 {
        return vec![];
    }
    // Nearest-higher boundaries and a range-minimum tree keep long spectra
    // O(n log n), including monotonically shrinking rippled spectra.
    let n = amplitudes.len();
    let size = n.next_power_of_two();
    let mut tree = vec![f64::INFINITY; 2 * size];
    for (i, &v) in amplitudes.iter().enumerate() {
        tree[size + i] = if v.is_finite() { v } else { f64::INFINITY };
    }
    for i in (1..size).rev() {
        tree[i] = tree[i * 2].min(tree[i * 2 + 1]);
    }
    let minimum = |mut a: usize, mut b: usize| {
        a += size;
        b += size;
        let mut answer = f64::INFINITY;
        while a < b {
            if a % 2 == 1 {
                answer = answer.min(tree[a]);
                a += 1;
            }
            if b % 2 == 1 {
                b -= 1;
                answer = answer.min(tree[b]);
            }
            a /= 2;
            b /= 2;
        }
        answer
    };
    let mut left_edge = vec![0; n];
    let mut right_edge = vec![n; n];
    let mut stack: Vec<usize> = vec![];
    for i in 0..n {
        while stack
            .last()
            .is_some_and(|&j| !amplitudes[j].is_finite() || amplitudes[j] <= amplitudes[i])
        {
            stack.pop();
        }
        left_edge[i] = stack.last().map_or(0, |j| j + 1);
        stack.push(i);
    }
    stack.clear();
    for i in (0..n).rev() {
        while stack
            .last()
            .is_some_and(|&j| !amplitudes[j].is_finite() || amplitudes[j] <= amplitudes[i])
        {
            stack.pop();
        }
        right_edge[i] = stack.last().copied().unwrap_or(n);
        stack.push(i);
    }
    let mut peaks = vec![];
    for k in 1..amplitudes.len().saturating_sub(1) {
        let a = amplitudes[k];
        if !a.is_finite() || a <= amplitudes[k - 1] || a < amplitudes[k + 1] {
            continue;
        }
        let left = minimum(left_edge[k], k + 1);
        let right = minimum(k, right_edge[k]);
        let prominence = a - left.max(right);
        if prominence >= min_prominence {
            peaks.push(Peak {
                bin: k,
                frequency_hz: frequencies[k],
                amplitude: a,
                prominence,
            });
        }
    }
    peaks.sort_by(|a, b| {
        let (a, b) = if priority == "prominence" {
            (a.prominence, b.prominence)
        } else {
            (a.amplitude, b.amplitude)
        };
        b.total_cmp(&a)
    });
    peaks.truncate(count);
    peaks
}

#[derive(Clone, Debug, Serialize)]
pub struct DynamicMetrics {
    pub unit: String,
    pub fundamental_hz: Option<f64>,
    pub fundamental_rms: Option<f64>,
    pub ibf_db: Option<f64>,
    pub thd_percent: Option<f64>,
    pub thd_db: Option<f64>,
    pub sfdr_db: Option<f64>,
    pub sfdr_dbfs: Option<f64>,
    pub sinad_db: Option<f64>,
    pub snr_db: Option<f64>,
    pub enob: Option<f64>,
    pub noise_rms: Option<f64>,
    pub harmonics: usize,
    pub tone_half_width_bins: usize,
    pub definition: &'static str,
    pub window: &'static str,
    pub averaging: &'static str,
}
impl DynamicMetrics {
    pub fn set_full_scale(&mut self, full_scale_peak: Option<f64>) {
        if let (Some(full), Some(signal)) = (full_scale_peak, self.fundamental_rms) {
            if full > 0.0 && full.is_finite() && signal > 0.0 {
                let ibf = 20.0 * (signal * std::f64::consts::SQRT_2 / full).log10();
                self.ibf_db = finite(ibf);
                self.sfdr_dbfs = self.sfdr_db.and_then(|v| finite(v - ibf));
                self.enob = self.sinad_db.and_then(|v| finite((v - 1.76 - ibf) / 6.02));
            }
        }
    }
}
fn finite(v: f64) -> Option<f64> {
    if v.is_finite() { Some(v) } else { None }
}
fn ratio_db(a: f64, b: f64) -> Option<f64> {
    if a > 0.0 && b > 0.0 {
        finite(10.0 * (a / b).log10())
    } else {
        None
    }
}

/// Single-tone spectral estimates. A supplied fundamental is preferred.
/// Harmonics beyond Nyquist are folded to their sampled alias. Each bin is
/// counted once, including overlapping harmonic neighborhoods. The DC region
/// is excluded. ENOB uses the conventional full-scale-sine SINAD relation and
/// is not an instrument calibration result.
pub fn dynamic_metrics(
    frequencies: &[f64],
    psd: &[f64],
    sample_rate: f64,
    fundamental: Option<f64>,
    harmonic_count: usize,
    half_width: usize,
) -> DynamicMetrics {
    let mut result = DynamicMetrics {
        unit: String::new(),
        fundamental_hz: None,
        fundamental_rms: None,
        ibf_db: None,
        thd_percent: None,
        thd_db: None,
        sfdr_db: None,
        sfdr_dbfs: None,
        sinad_db: None,
        snr_db: None,
        enob: None,
        noise_rms: None,
        harmonics: 0,
        tone_half_width_bins: half_width,
        definition: "single-tone band powers; harmonics alias-folded; DC excluded; sfdr_db is dBc, sfdr_dbfs references full scale; ENOB=(SINAD-1.76-IBF)/6.02 requires full scale",
        window: "supplied PSD",
        averaging: "supplied PSD",
    };
    if frequencies.len() != psd.len() || psd.len() < 4 {
        return result;
    }
    let df = frequencies[1] - frequencies[0];
    let fs = sample_rate;
    if !df.is_finite() || df <= 0.0 || !fs.is_finite() || fs <= 0.0 {
        return result;
    }
    let first = half_width.saturating_add(1).min(psd.len() - 1);
    let index = if let Some(f) = fundamental {
        if !f.is_finite() || f <= 0.0 || f > fs / 2.0 {
            return result;
        }
        (f / df).round() as usize
    } else {
        (1..psd.len())
            .filter(|&i| psd[i].is_finite())
            .max_by(|&a, &b| psd[a].total_cmp(&psd[b]))
            .unwrap_or(0)
    };
    if index < first || index >= psd.len() || !psd[index].is_finite() || psd[index] <= 0.0 {
        return result;
    }
    // Three-point logarithmic interpolation estimates the isolated base tone.
    let delta =
        if index > 0 && index + 1 < psd.len() && psd[index - 1] > 0.0 && psd[index + 1] > 0.0 {
            let (a, b, c) = (psd[index - 1].ln(), psd[index].ln(), psd[index + 1].ln());
            let d = a - 2.0 * b + c;
            if d.abs() > 1e-15 {
                (0.5 * (a - c) / d).clamp(-0.5, 0.5)
            } else {
                0.0
            }
        } else {
            0.0
        };
    let f0 = fundamental.unwrap_or((index as f64 + delta) * df);
    result.fundamental_hz = Some(f0);
    let mut mask = vec![0u8; psd.len()];
    for m in mask.iter_mut().take(first) {
        *m = 3;
    }
    let mut mark = |bin: usize, tag: u8| {
        for k in bin.saturating_sub(half_width)..=bin.saturating_add(half_width).min(psd.len() - 1)
        {
            if mask[k] == 0 {
                mask[k] = tag;
            }
        }
    };
    mark(index, 1);
    for h in 2..=harmonic_count.clamp(2, 100) {
        let folded = (h as f64 * f0).rem_euclid(fs);
        let alias = folded.min(fs - folded);
        let bin = (alias / df).round() as usize;
        if bin < psd.len() {
            mark(bin, 2);
            result.harmonics += 1;
        }
    }
    let mut p = [0.0; 4];
    for (&v, &m) in psd.iter().zip(&mask) {
        if v.is_finite() && v > 0.0 {
            p[m as usize] += v * df;
        }
    }
    let signal = p[1];
    let harmonic = p[2];
    let noise = p[0];
    if signal <= 0.0 {
        return result;
    }
    result.fundamental_rms = finite(signal.sqrt());
    result.noise_rms = finite(noise.sqrt());
    result.thd_percent = finite(100.0 * (harmonic / signal).sqrt());
    result.thd_db = ratio_db(harmonic, signal);
    result.sinad_db = ratio_db(signal, harmonic + noise);
    result.snr_db = ratio_db(signal, noise);
    let largest_spur = (first..psd.len())
        .filter(|&k| mask[k] != 1)
        .map(|k| {
            (k.saturating_sub(half_width)..=k.saturating_add(half_width).min(psd.len() - 1))
                .filter(|&j| mask[j] != 1 && mask[j] != 3)
                .map(|j| {
                    if psd[j].is_finite() {
                        psd[j].max(0.0) * df
                    } else {
                        0.0
                    }
                })
                .sum::<f64>()
        })
        .fold(0.0, f64::max);
    result.sfdr_db = ratio_db(signal, largest_spur);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn synthetic_tone_harmonics_noise_have_known_ratios() {
        let f: Vec<_> = (0..=500).map(|k| k as f64).collect();
        let mut p = vec![0.000001; 501];
        p[50] = 0.5;
        p[100] = 0.005;
        p[150] = 0.00125;
        let m = dynamic_metrics(&f, &p, 1000., Some(50.0), 3, 0);
        assert!((m.fundamental_rms.unwrap() - 0.5f64.sqrt()).abs() < 1e-12);
        assert!((m.thd_percent.unwrap() - 100.0 * (0.00625f64 / 0.5).sqrt()).abs() < 1e-12);
        assert!((m.sfdr_db.unwrap() - 20.0).abs() < 0.001);
        assert!(m.sinad_db.unwrap() < m.snr_db.unwrap());
    }
    #[test]
    fn peak_order_prominence_and_limits() {
        let f = vec![0., 1., 2., 3., 4., 5., 6., 7., 8.];
        let a = vec![0., 5., 4., 4.5, 0., 2., 0., 1., 0.];
        let p = spectral_peaks(&f, &a, 2, "amplitude", 0.0);
        assert_eq!(p.iter().map(|p| p.bin).collect::<Vec<_>>(), [1, 3]);
        let p = spectral_peaks(&f, &a, 2, "prominence", 1.0);
        assert_eq!(p.iter().map(|p| p.bin).collect::<Vec<_>>(), [1, 5]);
    }
    #[test]
    fn undefined_metrics_do_not_invent_zero_noise_infinity() {
        let f = vec![0., 1., 2., 3.];
        assert!(
            dynamic_metrics(&f, &[0.; 4], 6., None, 10, 0)
                .fundamental_hz
                .is_none()
        );
        let m = dynamic_metrics(&f, &[0., 1., 0., 0.], 6., Some(1.), 2, 0);
        assert!(m.sinad_db.is_none());
    }
}
