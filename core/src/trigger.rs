//! Sample-index trigger segmentation. Input is never resampled or gap-filled.
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct Settings {
    pub mode: String,
    pub channel: usize,
    pub level_v: f64,
    pub pretrigger_percent: f64,
    pub retrigger: bool,
    pub delay_seconds: f64,
    /// Accepted records; zero means continue until cancelled.
    pub record_count: u64,
    pub accept_reject: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            mode: "immediate".into(),
            channel: 0,
            level_v: 0.0,
            pretrigger_percent: 0.0,
            retrigger: false,
            delay_seconds: 0.0,
            record_count: 1,
            accept_reject: false,
        }
    }
}

impl Settings {
    pub fn segmented(&self) -> bool {
        self.mode != "immediate" || self.retrigger || self.accept_reject
    }

    pub fn validate(
        &self,
        channels: &[usize],
        ranges: &[f64; 4],
        rate: f64,
        target: Option<u64>,
        hardware: bool,
    ) -> Result<(), String> {
        if ![
            "immediate",
            "software",
            "analog_rising",
            "analog_falling",
            "external_rising",
        ]
        .contains(&self.mode.as_str())
        {
            return Err("未知触发方式".into());
        }
        if !self.level_v.is_finite()
            || !self.delay_seconds.is_finite()
            || self.delay_seconds < 0.0
            || !self.pretrigger_percent.is_finite()
            || !(0.0..=100.0).contains(&self.pretrigger_percent)
        {
            return Err("触发电平、延迟或预触发比例无效".into());
        }
        if self.mode.starts_with("analog_")
            && (!channels.contains(&self.channel)
                || self.level_v.abs() >= ranges[self.channel.min(3)])
        {
            return Err("模拟触发通道必须启用，阈值必须严格位于通道量程内".into());
        }
        if self.segmented() {
            let n = target.ok_or("触发记录需要设置有限记录时长")?;
            let pre = (n as f64 * self.pretrigger_percent / 100.0).round();
            if pre * channels.len() as f64 > 8_000_000.0
                || self.delay_seconds * rate > u64::MAX as f64
            {
                return Err("预触发缓存超过 8,000,000 个通道值，或延迟超出范围".into());
            }
        }
        if hardware
            && self.mode == "external_rising"
            && (self.pretrigger_percent != 0.0 || self.retrigger || self.accept_reject)
        {
            return Err("硬件 TTL 启动当前仅支持单次、无预触发；重复冲击请使用模拟阈值记录".into());
        }
        Ok(())
    }
}

#[derive(Debug)]
pub enum Event {
    Begin {
        source_start: u64,
        trigger_scan: u64,
        pretrigger: Vec<f64>,
        attempt: u64,
    },
    Samples {
        source_start: u64,
        values: Vec<f64>,
    },
    Complete,
}

pub struct Engine {
    pub settings: Settings,
    pub waiting: bool,
    pub pending_review: bool,
    pub accepted: u64,
    pub rejected: u64,
    pub attempts: u64,
    pub last_trigger_scan: Option<u64>,
    width: usize,
    column: usize,
    target: u64,
    pre: usize,
    history: VecDeque<f64>,
    previous: Option<f64>,
    remaining: u64,
    eligible_scan: u64,
    delay_scans: u64,
    fire: bool,
    hardware_start: bool,
    pub finished: bool,
}

impl Engine {
    pub fn new(
        settings: Settings,
        channels: &[usize],
        target: u64,
        rate: f64,
        start: u64,
        hardware_start: bool,
    ) -> Self {
        let pre = (target as f64 * settings.pretrigger_percent / 100.0).round() as usize;
        Self {
            column: channels
                .iter()
                .position(|&c| c == settings.channel)
                .unwrap_or(0),
            width: channels.len(),
            target,
            pre,
            delay_scans: (settings.delay_seconds * rate).round() as u64,
            settings,
            waiting: true,
            pending_review: false,
            accepted: 0,
            rejected: 0,
            attempts: 0,
            last_trigger_scan: None,
            history: VecDeque::new(),
            previous: None,
            remaining: 0,
            eligible_scan: start,
            fire: false,
            hardware_start,
            finished: false,
        }
    }

    pub fn fire(&mut self) -> Result<(), String> {
        if self.finished || self.pending_review || !self.waiting {
            return Err("当前没有等待中的触发记录".into());
        }
        if !["software", "external_rising"].contains(&self.settings.mode.as_str())
            || self.hardware_start
        {
            return Err("当前触发由输入信号或硬件决定".into());
        }
        self.fire = true;
        Ok(())
    }

    pub fn review(&mut self, accepted: bool, scan: u64) -> Result<(), String> {
        if !self.pending_review {
            return Err("当前没有等待接受或拒绝的记录".into());
        }
        self.pending_review = false;
        if accepted {
            self.accepted += 1;
        } else {
            self.rejected += 1;
        }
        self.advance(scan, accepted);
        Ok(())
    }

    fn advance(&mut self, scan: u64, accepted: bool) {
        self.finished = (accepted && !self.settings.retrigger)
            || (self.settings.record_count > 0 && self.accepted >= self.settings.record_count);
        self.waiting = !self.finished;
        self.eligible_scan = scan.saturating_add(self.delay_scans);
        self.fire = false;
    }

    pub fn process(&mut self, values: &[f64], start: u64) -> Vec<Event> {
        let mut events = Vec::new();
        let mut write_start = None;
        let mut write_values = Vec::new();
        for (i, frame) in values.chunks_exact(self.width).enumerate() {
            let scan = start + i as u64;
            let value = frame[self.column];
            if self.waiting
                && !self.finished
                && !self.pending_review
                && scan >= self.eligible_scan
                && self.history.len() == self.pre * self.width
            {
                let hit = match self.settings.mode.as_str() {
                    "immediate" => true,
                    "software" => self.fire,
                    "external_rising" => self.hardware_start || self.fire,
                    "analog_rising" => self.previous.is_some_and(|p| {
                        p < self.settings.level_v && value >= self.settings.level_v
                    }),
                    "analog_falling" => self.previous.is_some_and(|p| {
                        p > self.settings.level_v && value <= self.settings.level_v
                    }),
                    _ => false,
                };
                if hit {
                    self.waiting = false;
                    self.attempts += 1;
                    self.last_trigger_scan = Some(scan);
                    self.remaining = self.target - self.pre as u64;
                    self.fire = false;
                    events.push(Event::Begin {
                        source_start: scan - self.pre as u64,
                        trigger_scan: scan,
                        pretrigger: self.history.iter().copied().collect(),
                        attempt: self.attempts,
                    });
                }
            }
            if !self.waiting && !self.finished && !self.pending_review {
                if self.remaining > 0 {
                    write_start.get_or_insert(scan);
                    write_values.extend_from_slice(frame);
                    self.remaining -= 1;
                }
                if self.remaining == 0 {
                    if let Some(source_start) = write_start.take() {
                        events.push(Event::Samples {
                            source_start,
                            values: std::mem::take(&mut write_values),
                        });
                    }
                    events.push(Event::Complete);
                    if self.settings.accept_reject {
                        self.pending_review = true;
                    } else {
                        self.accepted += 1;
                        self.advance(scan + 1, true);
                    }
                }
            }
            self.previous = Some(value);
            if self.pre > 0 {
                if self.history.len() == self.pre * self.width {
                    self.history.drain(..self.width);
                }
                self.history.extend(frame.iter().copied());
            }
        }
        if let Some(source_start) = write_start {
            events.push(Event::Samples {
                source_start,
                values: write_values,
            });
        }
        events
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pretrigger_and_cross_block_edge_preserve_samples() {
        let mut e = Engine::new(
            Settings {
                mode: "analog_rising".into(),
                pretrigger_percent: 50.0,
                ..Settings::default()
            },
            &[0, 2],
            4,
            100.0,
            0,
            false,
        );
        assert!(e.process(&[-2., 20., -1., 21.], 0).is_empty());
        let events = e.process(&[0., 22., 1., 23., 2., 24.], 2);
        match &events[0] {
            Event::Begin {
                source_start,
                trigger_scan,
                pretrigger,
                ..
            } => {
                assert_eq!((*source_start, *trigger_scan), (0, 2));
                assert_eq!(pretrigger, &[-2., 20., -1., 21.]);
            }
            _ => panic!(),
        }
        match &events[1] {
            Event::Samples {
                source_start,
                values,
            } => {
                assert_eq!(*source_start, 2);
                assert_eq!(values, &[0., 22., 1., 23.]);
            }
            _ => panic!(),
        }
        assert!(matches!(events[2], Event::Complete));
        assert!(e.finished);
        assert_eq!(e.accepted, 1);
    }
    #[test]
    fn review_rejection_does_not_advance_accepted_count() {
        let mut e = Engine::new(
            Settings {
                mode: "software".into(),
                accept_reject: true,
                retrigger: true,
                record_count: 2,
                ..Settings::default()
            },
            &[0],
            2,
            10.,
            0,
            false,
        );
        e.fire().unwrap();
        assert_eq!(e.process(&[1., 2., 3.], 0).len(), 3);
        assert!(e.pending_review);
        assert!(e.fire().is_err());
        e.review(false, 3).unwrap();
        assert_eq!(e.rejected, 1);
        e.fire().unwrap();
        e.process(&[4., 5.], 3);
        e.review(true, 5).unwrap();
        assert!(!e.finished);
        e.fire().unwrap();
        e.process(&[6., 7.], 5);
        e.review(true, 7).unwrap();
        assert!(e.finished);
        assert_eq!(e.accepted, 2);
    }
    #[test]
    fn hundred_percent_pretrigger_and_retrigger_delay() {
        let mut e = Engine::new(
            Settings {
                mode: "immediate".into(),
                pretrigger_percent: 100.,
                retrigger: true,
                delay_seconds: 0.2,
                record_count: 2,
                ..Settings::default()
            },
            &[0],
            2,
            10.,
            0,
            false,
        );
        let out = e.process(&[1., 2., 3., 4., 5., 6.], 0);
        assert_eq!(
            out.iter().filter(|x| matches!(x, Event::Complete)).count(),
            2
        );
        assert!(e.finished);
        assert_eq!(e.last_trigger_scan, Some(5));
    }

    #[test]
    fn falling_edge_uses_selected_physical_channel_and_requires_crossing() {
        let settings = Settings {
            mode: "analog_falling".into(),
            channel: 3,
            level_v: 1.,
            ..Settings::default()
        };
        let mut e = Engine::new(settings, &[1, 3], 2, 100., 0, false);
        assert!(e.process(&[99., 0.5, 99., 2.], 0).is_empty());
        let out = e.process(&[-99., 1., -99., 0.], 2);
        assert!(matches!(
            out[0],
            Event::Begin {
                trigger_scan: 2,
                ..
            }
        ));
        assert!(e.finished);
    }

    #[test]
    fn capture_is_independent_of_input_block_boundaries() {
        fn captures(chunk: usize) -> Vec<Vec<f64>> {
            let settings = Settings {
                mode: "analog_rising".into(),
                channel: 0,
                pretrigger_percent: 25.,
                retrigger: true,
                record_count: 3,
                ..Settings::default()
            };
            let mut e = Engine::new(settings, &[0], 4, 100., 0, false);
            let values = [-1., -1., 1., 2., 3., -1., 1., 2., 3., -1., 1., 2., 3., 4.];
            let mut current = Vec::new();
            let mut records = Vec::new();
            let mut start = 0;
            for input in values.chunks(chunk) {
                for event in e.process(input, start) {
                    match event {
                        Event::Begin { pretrigger, .. } => current = pretrigger,
                        Event::Samples { values, .. } => current.extend(values),
                        Event::Complete => records.push(std::mem::take(&mut current)),
                    }
                }
                start += input.len() as u64;
            }
            records
        }
        let expected = captures(14);
        assert_eq!(expected.len(), 3);
        for chunk in 1..14 {
            assert_eq!(captures(chunk), expected);
        }
    }

    #[test]
    fn configuration_rejects_invalid_ranges_memory_and_external_pretrigger() {
        let mut s = Settings {
            mode: "analog_rising".into(),
            channel: 2,
            ..Settings::default()
        };
        assert!(
            s.validate(&[0], &[10.; 4], 1000., Some(1000), false)
                .is_err()
        );
        s.channel = 0;
        s.level_v = 10.;
        assert!(
            s.validate(&[0], &[10.; 4], 1000., Some(1000), false)
                .is_err()
        );
        s.level_v = 0.;
        s.pretrigger_percent = 100.;
        assert!(
            s.validate(&[0, 1, 2, 3], &[10.; 4], 1000., Some(2_000_001), false)
                .is_err()
        );
        s.mode = "external_rising".into();
        assert!(s.validate(&[0], &[10.; 4], 1000., Some(100), true).is_err());
        s.pretrigger_percent = 0.;
        assert!(s.validate(&[0], &[10.; 4], 1000., Some(100), true).is_ok());
        assert!(s.validate(&[0], &[10.; 4], 1000., None, true).is_err());
    }
}
