import numpy as np
import pytest
from scipy import signal

from labdaq.processing import (
    ChannelSettings,
    StreamingFilter,
    convert_volts,
    filter_window,
    frequency_response,
    spectrum,
)


def test_force_and_voltage_conversion_preserves_raw_and_uses_mv():
    raw = np.array([[0.12, 0.10, 2.0], [0.02, -0.10, -1.0]])
    snapshot = raw.copy()
    channels = [
        ChannelSettings(0, "拉力", sensitivity_mv_per_unit=10, zero_volts=0.02,
                        engineering_offset=2.5, point_number=7, direction="X+"),
        ChannelSettings(2, "反向", sensitivity_mv_per_unit=20, inverted=True, engineering_offset=-1.5),
        ChannelSettings(3, "电压", unit="V", sensitivity_mv_per_unit=0, zero_volts=0.5,
                        engineering_offset=.25),
    ]
    np.testing.assert_allclose(convert_volts(raw, channels), [[12.5, -6.5, 1.75], [2.5, 3.5, -1.25]])
    np.testing.assert_array_equal(raw, snapshot)


def test_engineering_offset_must_be_finite():
    with pytest.raises(ValueError, match="工程单位偏移"):
        convert_volts([[1.0]], [ChannelSettings(0, "力", engineering_offset=float("nan"))])


def test_zero_from_voltage_mean_removes_force_offset():
    raw = np.array([[0.02], [0.03], [0.01]])
    channel = ChannelSettings(1, "力", zero_volts=float(raw.mean()))
    converted = convert_volts(raw, [channel])
    assert abs(converted.mean()) < 1e-14
    np.testing.assert_allclose(converted[:, 0], [0, 1, -1], atol=1e-14)


def test_force_rejects_zero_sensitivity():
    with pytest.raises(ValueError, match="灵敏度"):
        convert_volts([[1.0]], [ChannelSettings(0, "力", sensitivity_mv_per_unit=0)])


@pytest.mark.parametrize("count", [1000, 1001])
def test_sine_peak_amplitude_and_rms(count):
    fs = 1000.0
    bin_index = 37
    data = 2.4 * np.sin(2 * np.pi * bin_index * np.arange(count) / count)
    frequencies, peaks = spectrum(data, fs)
    assert frequencies[bin_index] == pytest.approx(bin_index * fs / count)
    assert peaks[bin_index] == pytest.approx(2.4, rel=1e-12)
    assert np.sqrt(np.mean(data ** 2)) == pytest.approx(peaks[bin_index] / np.sqrt(2))


@pytest.mark.parametrize("window", ["hann", "boxcar"])
def test_psd_integral_has_voltage_squared_units(window):
    fs = 2048.0
    data = 3.0 + 2.0 * np.sin(2 * np.pi * 71 * np.arange(2048) / 2048)
    frequencies, psd = spectrum(data, fs, mode="psd", window=window)
    # DC contributes 3^2, sinusoid contributes peak^2/2.
    assert psd.sum() * (frequencies[1] - frequencies[0]) == pytest.approx(11.0)


@pytest.mark.parametrize("count", [1000, 1001])
def test_psd_parseval_for_general_signal(count):
    data = np.random.default_rng(42).normal(size=count)
    frequencies, psd = spectrum(data, 2048, mode="psd")
    weights = signal.get_window("hann", count)
    expected = np.sum((data * weights) ** 2) / np.sum(weights ** 2)
    assert psd.sum() * frequencies[1] == pytest.approx(expected, rel=1e-12)


def test_dc_and_even_nyquist_are_not_doubled():
    _, dc = spectrum(np.full(1024, 3.5), 1024)
    assert dc[0] == pytest.approx(3.5)
    _, nyquist = spectrum(1.7 * (-1.0) ** np.arange(1024), 1024, window="boxcar")
    assert nyquist[-1] == pytest.approx(1.7)


def test_invalid_fft_segment_is_reported():
    with pytest.raises(ValueError, match="NaN"):
        spectrum([0, np.nan, 1], 1000)


def test_h1_recovers_known_gain_phase_and_coherence():
    reference = np.random.default_rng(52).normal(size=8192)
    _, h1, coherence = frequency_response(reference, -2.5 * reference, 1000, nperseg=512)
    np.testing.assert_allclose(h1, -2.5, atol=1e-12)
    np.testing.assert_allclose(coherence, 1, atol=1e-12)
    assert abs(abs(np.angle(h1[50])) - np.pi) < 1e-12


def test_h1_response_delay_has_negative_phase():
    fs = 1024
    time = np.arange(4096) / fs
    reference = np.sin(2 * np.pi * 64 * time)
    response = 3 * np.sin(2 * np.pi * 64 * (time - 2 / fs))
    frequencies, h1, coherence = frequency_response(reference, response, fs, nperseg=256)
    target = np.flatnonzero(frequencies == 64)[0]
    assert h1[target] == pytest.approx(3 * np.exp(-1j * np.pi / 4), abs=1e-12)
    assert coherence[target] == pytest.approx(1)


def test_h1_does_not_present_single_segment_as_coherent_evidence():
    with pytest.raises(ValueError, match="至少两个"):
        frequency_response(np.ones(1024), np.ones(1024), 1000)


def test_h1_zero_energy_is_undefined():
    _, h1, coherence = frequency_response(np.zeros(2048), np.zeros(2048), 1000)
    assert np.all(np.isnan(h1))
    assert np.all(np.isnan(coherence))


def test_continuous_filter_matches_single_pass_even_for_tiny_chunks():
    raw = np.random.default_rng(10).normal(size=(1003, 3))
    filter_ = StreamingFilter(2000, 3, 100, order=4)
    expected = signal.sosfilt(filter_.sos, raw, axis=0)
    edges = [0, 1, 3, 8, 64, 500, 501, 1003]
    filtered = np.vstack([filter_.process(raw[a:b]) for a, b in zip(edges[:-1], edges[1:])])
    np.testing.assert_allclose(filtered, expected, rtol=0, atol=0)
    assert filter_.process(np.empty((0, 3))).shape == (0, 3)
    filter_.reset()
    np.testing.assert_allclose(filter_.process(raw), expected, rtol=0, atol=0)


def test_window_filter_preserves_constant_load_without_zero_state_transient():
    raw = np.tile([10.0, -5.0, 0.0], (16000, 1))
    snapshot = raw.copy()
    # A slow cutoff would make zero-state restart contaminate most of this
    # eight-second window. Offline steady endpoint extension preserves DC.
    filtered = filter_window(raw, 2000, 0.1)
    np.testing.assert_allclose(filtered, raw, rtol=2e-8, atol=1e-10)
    np.testing.assert_array_equal(raw, snapshot)
    with pytest.raises(ValueError, match="至少需要"):
        filter_window(raw[:10], 2000, 100)
