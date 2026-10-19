//! DT9837C packet layout, clock planning and stream assembly.
//! Adapted from libuldaq 1.2.2, Copyright (c) 2018 Measurement Computing.
//! MIT license: see THIRD_PARTY_NOTICES.md. No vendor runtime is used.

pub const VENDOR: u16 = 0x0867;
pub const BOOT: u16 = 0x9839;
pub const RUNTIME: u16 = 0x3998;
pub const GROUP_DELAY: usize = 39;

pub fn packet(op: u32, payload: &[u8]) -> [u8; 64] {
    let mut data = [0; 64];
    data[..4].copy_from_slice(&op.to_le_bytes());
    data[4..4 + payload.len()].copy_from_slice(payload);
    data
}

pub fn modify(address: u16, mask: u16, value: u16) -> [u8; 64] {
    packet(
        24,
        &[
            address.to_be_bytes(),
            mask.to_be_bytes(),
            value.to_be_bytes(),
        ]
        .concat(),
    )
}

pub fn subsystem(start: bool) -> [u8; 64] {
    packet(if start { 30 } else { 31 }, &[0; 6]) // ADC, internal clock, immediate trigger
}

pub fn eeprom(offsets: &[u8]) -> [u8; 64] {
    let mut payload = vec![offsets.len() as u8];
    for offset in offsets {
        payload.extend([0x50, *offset]);
    }
    packet(12, &payload)
}

#[derive(Debug)]
pub struct ClockPlan {
    pub actual_rate: f64,
    pub divider_register: u16,
    pub high_speed: bool,
    pub registers: [(u8, u8); 9],
}

pub fn plan_clock(rate: f64) -> Result<ClockPlan, String> {
    if !(195.313..=105469.0).contains(&rate) {
        return Err("DT9837C 采样率需在 195.313–105469 Hz 之间".into());
    }
    let high_speed = rate > 52734.0;
    let ticks = if high_speed { 256.0 } else { 512.0 };
    for (index, divider) in [2.0, 4.0, 8.0, 16.0].iter().enumerate() {
        let target = rate * ticks * divider / 1e6;
        let mut div_n = (((100.0 + target / 2.0) / target) as i32).clamp(4, 128);
        let initial_vco = target * div_n as f64;
        if initial_vco < 100.0 {
            div_n += 1;
        }
        if initial_vco > 400.0 {
            div_n -= 1;
        }
        let vco = target * div_n as f64;
        let mut best = (i64::MAX, 0, 0, 0.0);
        for q in 3..=129 {
            let p = (vco / 48.0 * q as f64 + 0.5) as i32;
            if p > 2055 {
                break;
            }
            let frequency = 48.0 * p as f64 / q as f64;
            // Retain the original truncation and first-tie rule.
            let mut error = 10000.0 * (frequency - vco);
            let mut truncated = error as i64;
            if truncated < 0 {
                error = -error;
                truncated = error as i64;
            }
            if error - truncated as f64 >= 0.5 {
                truncated += 1;
            }
            if truncated < best.0 {
                best = (truncated, q, p, frequency);
            }
        }
        if div_n >= 128 && index < 3 {
            continue;
        }
        let (_, q, p, frequency) = best;
        let pv = (p - p % 2) / 2 - 4;
        let qv = p % 2 * 128 + q - 2;
        let pump = match p {
            45..480 => 1,
            480..640 => 2,
            640..800 => 3,
            800..1024 => 4,
            _ => 0,
        };
        return Ok(ClockPlan {
            actual_rate: frequency * 1e6 / div_n as f64 / (ticks * divider),
            divider_register: (index as u16) << 8,
            high_speed,
            registers: [
                (0x0c, div_n as u8),
                (0x47, (div_n << 1) as u8),
                (0x40, ((pv >> 8) | 0xc0 | (pump << 2)) as u8),
                (0x41, pv as u8),
                (0x42, qv as u8),
                (0x44, 0x3f),
                (0x45, 0xff),
                (0x46, 0xff),
                (0x09, 1),
            ],
        });
    }
    unreachable!()
}

/// USB transfers need not end at a sample/scan boundary. Never discard a tail.
pub struct Decoder {
    pending: Vec<u8>,
    discard: usize,
    ranges: Vec<f64>,
}

impl Decoder {
    pub fn new(ranges: Vec<f64>) -> Self {
        Self {
            pending: Vec::new(),
            discard: GROUP_DELAY * ranges.len() * 4,
            ranges,
        }
    }

    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<f64>, String> {
        let skip = self.discard.min(bytes.len());
        self.discard -= skip;
        self.pending.extend_from_slice(&bytes[skip..]);
        let width = self.ranges.len();
        let count = self.pending.len() / (4 * width) * width;
        let mut values = Vec::with_capacity(count);
        for (i, word) in self.pending[..count * 4].chunks_exact(4).enumerate() {
            let code = u32::from_le_bytes(word.try_into().unwrap());
            if code >= 1 << 24 {
                return Err("ADC 数据高 8 位非零，无法确认帧边界".into());
            }
            let range = self.ranges[i % width];
            values.push(code as f64 * (2.0 * range / (1u32 << 24) as f64) - range);
        }
        self.pending.drain(..count * 4);
        Ok(values)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragments_keep_channels_and_discard_exactly_39_scans() {
        let ranges = vec![10.0, 1.0, 10.0];
        let mut bytes = vec![0xff; GROUP_DELAY * 3 * 4];
        let codes: [u32; 6] = [0, 1 << 23, (1 << 24) - 1, 1 << 23, 0, 1 << 22];
        bytes.extend(codes.iter().flat_map(|v| v.to_le_bytes()));
        for fragment in 1..=53 {
            let mut decoder = Decoder::new(ranges.clone());
            let values: Vec<_> = bytes
                .chunks(fragment)
                .flat_map(|b| decoder.push(b).unwrap())
                .collect();
            assert_eq!(
                values,
                vec![-10.0, 0.0, 10.0 - 20.0 / (1 << 24) as f64, 0.0, -1.0, -5.0]
            );
            assert!(decoder.pending.is_empty());
        }
    }

    #[test]
    fn invalid_adc_word_is_not_silently_masked() {
        let mut decoder = Decoder::new(vec![10.0]);
        decoder.push(&vec![0; GROUP_DELAY * 4]).unwrap();
        assert!(decoder.push(&[0, 0, 0, 1]).is_err());
    }

    #[test]
    fn clock_is_decodable_and_close_across_modes() {
        for rate in [
            195.313, 200.0, 1000.0, 2000.0, 44100.0, 52734.0, 52735.0, 96000.0, 105469.0,
        ] {
            let p = plan_clock(rate).unwrap();
            let div_n = p.registers[0].1;
            let pv = ((p.registers[2].1 as u32 & 3) << 8) | p.registers[3].1 as u32;
            let qv = p.registers[4].1 as u32;
            let decoded = 48e6 * (2 * (pv + 4) + (qv >> 7)) as f64
                / ((qv & 0x7f) + 2) as f64
                / div_n as f64
                / (if p.high_speed { 256.0 } else { 512.0 })
                / (2 << (p.divider_register >> 8)) as f64;
            assert!((decoded - p.actual_rate).abs() < 1e-8);
            assert!((p.actual_rate / rate - 1.0).abs() < 0.0005);
        }
        assert!(plan_clock(f64::NAN).is_err());
    }

    #[test]
    fn packets_and_exact_48khz_match_public_definitions() {
        assert_eq!(
            &modify(2, 0x0f00, 0x0500)[..10],
            &[24, 0, 0, 0, 0, 2, 15, 0, 5, 0]
        );
        assert_eq!(&eeprom(&[5, 6])[..9], &[12, 0, 0, 0, 2, 0x50, 5, 0x50, 6]);
        assert_eq!(&subsystem(true)[..10], &[30, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
        let p = plan_clock(48000.0).unwrap();
        assert_eq!(p.actual_rate, 48000.0);
        assert_eq!(
            p.registers,
            [
                (0x0c, 4),
                (0x47, 8),
                (0x40, 0xc8),
                (0x41, 0xfc),
                (0x42, 0x7b),
                (0x44, 0x3f),
                (0x45, 0xff),
                (0x46, 0xff),
                (0x09, 1)
            ]
        );
        assert_eq!(plan_clock(96000.0).unwrap().registers, p.registers);
    }
}
