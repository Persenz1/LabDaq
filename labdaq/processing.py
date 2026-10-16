"""Voltage conversion and offline analysis, independent of the USB backend.

All multichannel arrays use rows for samples and columns for selected channels.
Filtering and conversion produce derived arrays; recorded voltage stays untouched.
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass
class ChannelSettings:
    index: int
    name: str
    unit: str = "N"
    sensitivity_mv_per_unit: float = 10.0
    zero_volts: float = 0.0
    inverted: bool = False


def convert_volts(volts2d, channels):
    """Convert raw V into selected engineering units, preserving the input.

    For physical units: value = (V - zero_V) / (sensitivity_mV_per_unit / 1000).
    A voltage channel (unit='V') bypasses sensitivity, but keeps zero and sign.
    Sensitivity must be positive; use ``inverted`` for polarity instead.
    """
    volts = np.asarray(volts2d, dtype=np.float64)
    if volts.ndim != 2 or volts.shape[1] != len(channels):
        raise ValueError("电压数组必须是 (采样点数, 通道数)，并与通道设置一致")
    result = volts.copy()
    for column, channel in enumerate(channels):
        result[:, column] -= channel.zero_volts
        if channel.unit != "V":
            if not np.isfinite(channel.sensitivity_mv_per_unit) or channel.sensitivity_mv_per_unit <= 0:
                raise ValueError("传感器灵敏度必须是正数；反向请使用通道极性设置")
            result[:, column] *= 1000.0 / channel.sensitivity_mv_per_unit
        if channel.inverted:
            result[:, column] *= -1.0
    return result


def _analysis_signal(data, sample_rate):
    data = np.asarray(data, dtype=np.float64)
    if data.ndim != 1 or data.size < 2:
        raise ValueError("频域分析至少需要两个一维采样点")
    if not np.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("采样率必须为正数")
    if not np.all(np.isfinite(data)):
        raise ValueError("频域分析不能使用 NaN 或无穷值；请先选择有效数据段")
    return data


def spectrum(data1d, fs, mode="amplitude", window="hann"):
    """Return (Hz, values), keeping DC and using a periodic analysis window.

    ``amplitude`` is a single-sided *peak*, not RMS, spectrum. Window coherent
    gain is corrected. An on-bin sine of amplitude A has a peak A; DC and the
    even-length Nyquist bin are not doubled. Off-bin tones spread over bins.

    ``psd`` is a one-sided periodogram in unit**2/Hz. Sum(PSD) * df equals the
    window-weighted mean square, sum((data*w)**2)/sum(w**2); with boxcar this is
    the original mean square. No mean removal is performed in either mode.
    """
    data = _analysis_signal(data1d, fs)
    weights = signal.get_window(window, data.size, fftbins=True)
    coefficients = np.fft.rfft(data * weights)
    frequencies = np.fft.rfftfreq(data.size, d=1.0 / fs)
    if mode == "amplitude":
        values = np.abs(coefficients) / np.sum(weights)
    elif mode == "psd":
        values = np.abs(coefficients) ** 2 / (fs * np.sum(weights ** 2))
    else:
        raise ValueError("频谱类型必须是 amplitude 或 psd")
    # rfft includes Nyquist only for even lengths; the last odd-length bin has
    # a negative-frequency partner and must therefore be doubled as well.
    values[1:-1 if data.size % 2 == 0 else None] *= 2.0
    return frequencies, values


def frequency_response(reference, response, fs, nperseg=1024, window="hann"):
    """Welch H1 estimate and magnitude-squared coherence.

    H1 = Pxy / Pxx, where Pxy = conj(FFT(reference))*FFT(response).
    Segments overlap by 50% and each segment has its mean removed. H1 assumes
    noise mainly on the response. At least two segments are required: coherence
    of one segment is trivially one and should not be shown as evidence.
    Zero-energy frequency bins return NaN rather than an invented response.
    """
    x = _analysis_signal(reference, fs)
    y = _analysis_signal(response, fs)
    if x.shape != y.shape:
        raise ValueError("参考信号与响应信号必须等长")
    if nperseg < 4 or x.size < nperseg + nperseg // 2:
        raise ValueError("H1/相干分析需要至少两个重叠段；请减少分段长度或增加数据")
    kwargs = dict(fs=fs, window=window, nperseg=nperseg,
                  noverlap=nperseg // 2, detrend="constant", scaling="density")
    freq, pxx = signal.welch(x, **kwargs)
    _, pyy = signal.welch(y, **kwargs)
    _, pxy = signal.csd(x, y, **kwargs)
    h1 = np.full(pxy.shape, np.nan + 1j * np.nan, dtype=np.complex128)
    np.divide(pxy, pxx, out=h1, where=pxx > 0)
    denominator = pxx * pyy
    coherence = np.full(pxx.shape, np.nan, dtype=np.float64)
    np.divide(np.abs(pxy) ** 2, denominator, out=coherence, where=denominator > 0)
    return freq, h1, np.clip(coherence, 0.0, 1.0)


def filter_window(data, fs, cutoff_hz, kind="lowpass", order=4):
    """Offline zero-phase Butterworth filter for a complete analysis window.

    Unlike StreamingFilter this runs forward and backward and is not causal.
    It avoids reintroducing a zero-state step on each sliding window. Endpoint
    extension still influences window edges. The combined amplitude response
    is the squared single-pass response (-6 dB at the given Butterworth cutoff),
    with twice the order. Original samples are never modified.
    """
    data = np.asarray(data, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError("窗口滤波输入必须是 (采样点数, 通道数)")
    sos = signal.butter(order, cutoff_hz, btype=kind, fs=fs, output="sos")
    padlen = 3 * (2 * len(sos) + 1)
    if len(data) <= padlen:
        raise ValueError(f"离线零相位滤波至少需要 {padlen + 1} 个采样点，请等待更多数据")
    return signal.sosfiltfilt(sos, data, axis=0, padlen=padlen)


class StreamingFilter:
    """Causal Butterworth SOS filter with state carried between sample blocks.

    Starts at zero state, so startup transients and filter delay are expected.
    This is a display/derived-data filter, never a substitute for antialiasing.
    Call reset only on a new stream, not after every block.
    """

    def __init__(self, sample_rate, channel_count, cutoff_hz, kind="lowpass", order=4):
        if channel_count < 1:
            raise ValueError("至少需要一个通道")
        self.sos = signal.butter(order, cutoff_hz, btype=kind, fs=sample_rate, output="sos")
        self.channel_count = channel_count
        self.reset()

    def reset(self):
        self._state = np.zeros((len(self.sos), 2, self.channel_count), dtype=np.float64)

    def process(self, volts2d):
        data = np.asarray(volts2d, dtype=np.float64)
        if data.ndim != 2 or data.shape[1] != self.channel_count:
            raise ValueError("滤波输入通道数与初始化设置不一致")
        if len(data) == 0:
            return data.copy()
        filtered, self._state = signal.sosfilt(self.sos, data, axis=0, zi=self._state)
        return filtered
