//! Native Rust engineering conversion and derived signal analysis.
//!
//! Inputs are channel-major, uniformly sampled, continuous data. A caller must
//! split acquisition pauses into continuous segments before frequency analysis.
//! Raw recording voltages are never mutated. Undefined spectral bins serialize
//! as JSON null, not fabricated zeros.
pub mod filter;
pub mod metrics;
pub mod spectrum;

pub use filter::{FilterSpec, Sos, StreamingFilter, design_filter, filter_json};
use serde::{Deserialize, Serialize};
pub use spectrum::{AnalysisRequest, AnalysisResult, Window, analyze, analyze_json};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ChannelSettings {
    pub index: usize,
    pub name: String,
    pub unit: String,
    pub sensitivity_mv_per_unit: f64,
    pub zero_volts: f64,
    pub inverted: bool,
    pub engineering_offset: f64,
    pub point_number: Option<u32>,
    pub direction: String,
}

impl Default for ChannelSettings {
    fn default() -> Self {
        Self {
            index: 0,
            name: String::new(),
            unit: "V".into(),
            sensitivity_mv_per_unit: 1000.0,
            zero_volts: 0.0,
            inverted: false,
            engineering_offset: 0.0,
            point_number: None,
            direction: String::new(),
        }
    }
}

pub fn convert_volts(
    samples: &[Vec<f64>],
    channels: &[ChannelSettings],
) -> Result<Vec<Vec<f64>>, String> {
    if samples.len() != channels.len() {
        return Err("电压通道数与通道设置不一致".into());
    }
    samples
        .iter()
        .zip(channels)
        .map(|(data, c)| {
            if !c.zero_volts.is_finite() || !c.engineering_offset.is_finite() {
                return Err("零点和工程偏移必须为有限数".into());
            }
            let scale = if c.unit == "V" {
                1.0
            } else {
                if !c.sensitivity_mv_per_unit.is_finite() || c.sensitivity_mv_per_unit <= 0.0 {
                    return Err("传感器灵敏度必须是正有限数".into());
                }
                1000.0 / c.sensitivity_mv_per_unit
            } * if c.inverted { -1.0 } else { 1.0 };
            Ok(data
                .iter()
                .map(|v| (v - c.zero_volts) * scale + c.engineering_offset)
                .collect())
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn conversion_preserves_voltage_and_operation_order() {
        let raw = vec![vec![0.12, 0.02], vec![0.1, -0.1], vec![2.0, -1.0]];
        let snapshot = raw.clone();
        let channels = vec![
            ChannelSettings {
                unit: "N".into(),
                sensitivity_mv_per_unit: 10.0,
                zero_volts: 0.02,
                engineering_offset: 2.5,
                ..Default::default()
            },
            ChannelSettings {
                unit: "N".into(),
                sensitivity_mv_per_unit: 20.0,
                inverted: true,
                engineering_offset: -1.5,
                ..Default::default()
            },
            ChannelSettings {
                zero_volts: 0.5,
                engineering_offset: 0.25,
                ..Default::default()
            },
        ];
        let out = convert_volts(&raw, &channels).unwrap();
        for (a, b) in out
            .iter()
            .flatten()
            .zip([12.5, 2.5, -6.5, 3.5, 1.75, -1.25])
        {
            assert!((a - b).abs() < 1e-12);
        }
        assert_eq!(raw, snapshot);
    }
    #[test]
    fn conversion_rejects_invalid_contract() {
        assert!(convert_volts(&[vec![1.0]], &[]).is_err());
        for sensitivity in [0.0, -1.0, f64::NAN] {
            assert!(
                convert_volts(
                    &[vec![1.0]],
                    &[ChannelSettings {
                        unit: "N".into(),
                        sensitivity_mv_per_unit: sensitivity,
                        ..Default::default()
                    }]
                )
                .is_err()
            );
        }
    }
}
