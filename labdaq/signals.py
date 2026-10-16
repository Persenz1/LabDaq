"""Reproducible voltage waveforms, independent of acquisition block boundaries.

Periodic amplitudes are signed peak volts (not peak-to-peak or RMS); a negative
amplitude reverses polarity. ``dc`` uses
amplitude_v as its constant level; ``step`` and ``pulse`` use it as the height
above baseline. ``noise`` uses noise_rms_v and ignores amplitude_v. Noise on
other waveforms is additive. This is an electrical test signal model, not a
model of a force sensor, amplifier, or the measured DT9837C response. Frequencies
above Nyquist are allowed for aliasing demonstrations; there is no anti-alias
filter model here.
"""

from dataclasses import asdict, dataclass, field
import math

import numpy as np


WAVEFORMS = ("dc", "sine", "square", "triangle", "sawtooth", "step", "pulse", "chirp", "noise")


@dataclass(frozen=True)
class SignalSpec:
    waveform: str = "sine"
    amplitude_v: float = 0.5
    offset_v: float = 0.0
    frequency_hz: float = 10.0
    phase_deg: float = 0.0
    noise_rms_v: float = 0.0
    drift_v_per_s: float = 0.0
    duty_cycle: float = 0.5
    start_s: float = 0.0
    duration_s: float = 1.0
    end_frequency_hz: float = 100.0

    def __post_init__(self):
        if self.waveform not in WAVEFORMS:
            raise ValueError(f"未知模拟波形：{self.waveform}")
        if not all(math.isfinite(value) for name, value in asdict(self).items() if name != "waveform"):
            raise ValueError("模拟信号参数需为有限数值")
        if self.noise_rms_v < 0:
            raise ValueError("噪声 RMS 不能为负")
        if self.frequency_hz < 0 or self.end_frequency_hz < 0:
            raise ValueError("信号频率不能为负")
        if not 0 <= self.duty_cycle <= 1:
            raise ValueError("占空比需在 0–1 之间")
        if self.start_s < 0 or self.duration_s <= 0:
            raise ValueError("开始时间不能为负，持续时间需大于零")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict) -> "SignalSpec":
        return cls(**values)


@dataclass(frozen=True)
class SimulationSettings:
    """Channel entries always correspond to physical AI0, AI1, AI2, AI3."""

    channels: tuple[SignalSpec, ...] = field(default_factory=lambda: (SignalSpec(),) * 4)
    seed: int = 2026

    def __post_init__(self):
        if len(self.channels) != 4:
            raise ValueError("模拟信号需包含 AI0–AI3 共 4 个物理通道")
        if not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("随机种子需为非负整数")

    def to_dict(self) -> dict:
        return {"channels": [spec.to_dict() for spec in self.channels], "seed": self.seed}

    @classmethod
    def from_dict(cls, values: dict) -> "SimulationSettings":
        return cls(channels=tuple(SignalSpec.from_dict(spec) for spec in values["channels"]),
                   seed=values.get("seed", 2026))


def generate_signals(settings: SimulationSettings, start_sample: int, count: int,
                     sample_rate: float) -> np.ndarray:
    """Return unquantized volts for all four channels at absolute sample times.

    Sample zero is t=0 after the simulator's 39 settling scans. ``start_s`` is
    used by step/pulse/chirp. Pulse and chirp have a half-open active interval
    [start_s, start_s + duration_s); a step remains high after start_s. A chirp
    integrates a linear frequency sweep over its duration and is zero outside
    that interval. Offset, drift and additive noise remain active everywhere.
    Square starts high, triangle and sawtooth start at negative peak at phase 0.
    """
    t = np.arange(start_sample, start_sample + count, dtype=np.float64) / sample_rate
    values = np.empty((count, 4), dtype=np.float64)
    for channel, spec in enumerate(settings.channels):
        phase = spec.frequency_hz * t + spec.phase_deg / 360.0
        if spec.waveform == "dc":
            wave = np.ones(count)
        elif spec.waveform == "sine":
            wave = np.sin(2 * np.pi * phase)
        elif spec.waveform == "square":
            wave = np.where(phase % 1 < spec.duty_cycle, 1.0, -1.0)
        elif spec.waveform == "triangle":
            wave = 1.0 - 4.0 * np.abs(phase % 1 - 0.5)
        elif spec.waveform == "sawtooth":
            wave = 2.0 * (phase % 1) - 1.0
        elif spec.waveform == "step":
            wave = (t >= spec.start_s).astype(np.float64)
        elif spec.waveform == "pulse":
            wave = ((t >= spec.start_s) & (t < spec.start_s + spec.duration_s)).astype(np.float64)
        elif spec.waveform == "chirp":
            elapsed = t - spec.start_s
            slope = (spec.end_frequency_hz - spec.frequency_hz) / spec.duration_s
            cycles = spec.frequency_hz * elapsed + 0.5 * slope * elapsed ** 2 + spec.phase_deg / 360.0
            wave = np.where((elapsed >= 0) & (elapsed < spec.duration_s), np.sin(2 * np.pi * cycles), 0.0)
        else:  # noise: amplitude is defined exclusively by noise_rms_v.
            wave = np.zeros(count)
        values[:, channel] = spec.offset_v + spec.drift_v_per_s * t + spec.amplitude_v * wave

    if any(spec.noise_rms_v for spec in settings.channels):
        # Exactly eight PCG64 draws per physical scan (two per channel) make
        # random access independent of block sizes and enabled channel subsets.
        # Box-Muller gives normal noise with the requested population RMS.
        bit_generator = np.random.PCG64(settings.seed)
        bit_generator.advance(start_sample * 8)
        uniform = np.random.Generator(bit_generator).random((count, 4, 2))
        noise = np.sqrt(-2.0 * np.log(1.0 - uniform[:, :, 0])) * np.cos(2 * np.pi * uniform[:, :, 1])
        values += noise * np.asarray([spec.noise_rms_v for spec in settings.channels])
    return values
