use serde::{Deserialize, Serialize};
use std::f64::consts::TAU;

#[derive(Clone, Deserialize, Serialize)]
pub struct Signal {
    pub waveform: String,
    pub amplitude_v: f64,
    pub offset_v: f64,
    pub frequency_hz: f64,
    pub phase_deg: f64,
    pub noise_rms_v: f64,
    pub drift_v_per_s: f64,
    pub duty_cycle: f64,
    pub start_s: f64,
    pub duration_s: f64,
    pub end_frequency_hz: f64,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct Signals {
    pub channels: [Signal; 4],
    pub seed: u64,
}

impl Signals {
    pub fn validate(&self) -> Result<(), String> {
        for s in &self.channels {
            if ![
                s.amplitude_v,
                s.offset_v,
                s.frequency_hz,
                s.phase_deg,
                s.noise_rms_v,
                s.drift_v_per_s,
                s.duty_cycle,
                s.start_s,
                s.duration_s,
                s.end_frequency_hz,
            ]
            .iter()
            .all(|x| x.is_finite())
                || ![
                    "dc", "sine", "square", "triangle", "sawtooth", "step", "pulse", "chirp",
                    "noise",
                ]
                .contains(&s.waveform.as_str())
                || s.noise_rms_v < 0.0
                || s.frequency_hz < 0.0
                || s.end_frequency_hz < 0.0
                || !(0.0..=1.0).contains(&s.duty_cycle)
                || s.start_s < 0.0
                || s.duration_s <= 0.0
            {
                return Err("模拟信号参数无效".into());
            }
        }
        Ok(())
    }

    pub fn volts(&self, scan: u64, channel: usize, rate: f64) -> f64 {
        let s = &self.channels[channel];
        let t = scan as f64 / rate;
        let phase = s.frequency_hz * t + s.phase_deg / 360.0;
        let elapsed = t - s.start_s;
        let wave = match s.waveform.as_str() {
            "dc" => 1.0,
            "sine" => (TAU * phase).sin(),
            "square" => {
                if phase.rem_euclid(1.0) < s.duty_cycle {
                    1.0
                } else {
                    -1.0
                }
            }
            "triangle" => 1.0 - 4.0 * (phase.rem_euclid(1.0) - 0.5).abs(),
            "sawtooth" => 2.0 * phase.rem_euclid(1.0) - 1.0,
            "step" => {
                if t >= s.start_s {
                    1.0
                } else {
                    0.0
                }
            }
            "pulse" => {
                if t >= s.start_s && t < s.start_s + s.duration_s {
                    1.0
                } else {
                    0.0
                }
            }
            "chirp" if elapsed >= 0.0 && elapsed < s.duration_s => {
                let slope = (s.end_frequency_hz - s.frequency_hz) / s.duration_s;
                (TAU * (s.frequency_hz * elapsed
                    + 0.5 * slope * elapsed * elapsed
                    + s.phase_deg / 360.0))
                    .sin()
            }
            _ => 0.0,
        };
        let noise = if s.noise_rms_v == 0.0 {
            0.0
        } else {
            let index = scan.wrapping_mul(8).wrapping_add(channel as u64 * 2);
            let u1 = uniform(self.seed, index);
            let u2 = uniform(self.seed, index.wrapping_add(1));
            (-2.0 * (1.0 - u1).ln()).sqrt() * (TAU * u2).cos() * s.noise_rms_v
        };
        s.offset_v + s.drift_v_per_s * t + s.amplitude_v * wave + noise
    }
}

// Counter-based SplitMix64 + Box-Muller: stable across blocks and channel subsets.
// Deliberately versioned separately from the Python reference's PCG64 stream.
fn uniform(seed: u64, index: u64) -> f64 {
    let mut z = seed.wrapping_add(index.wrapping_add(1).wrapping_mul(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
    ((z ^ (z >> 31)) >> 11) as f64 / 9007199254740992.0
}

pub fn quantize(v: f64, range: f64) -> f64 {
    let code = ((v + range) * (16777216.0 / (2.0 * range)))
        .round_ties_even()
        .clamp(0.0, 16777215.0);
    code * (2.0 * range / 16777216.0) - range
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn adc_endpoints_and_ties() {
        assert_eq!(quantize(-12.0, 10.0), -10.0);
        assert_eq!(quantize(12.0, 10.0), 10.0 - 20.0 / 16777216.0);
        assert_eq!(quantize(0.0, 1.0), 0.0);
        assert_eq!(quantize(1.0 / 16777216.0, 1.0), 0.0);
    }
    #[test]
    fn noise_mean_rms_and_channel_independence() {
        let mut sum = 0.0;
        let mut square = 0.0;
        let mut cross = 0.0;
        for i in 0..100000 {
            let a = (-2.0 * (1.0 - uniform(77, i * 8)).ln()).sqrt()
                * (TAU * uniform(77, i * 8 + 1)).cos();
            let b = (-2.0 * (1.0 - uniform(77, i * 8 + 2)).ln()).sqrt()
                * (TAU * uniform(77, i * 8 + 3)).cos();
            sum += a;
            square += a * a;
            cross += a * b;
        }
        assert!((sum / 100000.0).abs() < 0.01);
        assert!((square / 100000.0 - 1.0).abs() < 0.02);
        assert!((cross / 100000.0).abs() < 0.02);
    }
}
