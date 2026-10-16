import json

import numpy as np
import pytest

from labdaq.protocol import AcquisitionConfig
from labdaq.signals import SignalSpec, SimulationSettings, generate_signals
from labdaq.simulator import SimulatedTransport
from labdaq.transport import DaqSession


def settings(*specs, seed=17):
    return SimulationSettings(channels=tuple(specs) + (SignalSpec(waveform="dc", amplitude_v=0),) * (4 - len(specs)), seed=seed)


def test_settings_json_preserves_all_physical_channels():
    original = settings(SignalSpec(waveform="pulse", start_s=1.5, duration_s=0.05),
                        SignalSpec(phase_deg=90, noise_rms_v=0.01), seed=31)
    restored = SimulationSettings.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original
    with pytest.raises(ValueError, match="4 个"):
        SimulationSettings(channels=(SignalSpec(),))
    with pytest.raises(ValueError, match="RMS"):
        SignalSpec(noise_rms_v=-1)
    with pytest.raises(ValueError, match="持续"):
        SignalSpec(waveform="chirp", duration_s=0)


def test_periodic_shapes_peak_phase_frequency_and_duty():
    spec = settings(SignalSpec(amplitude_v=2, frequency_hz=1),
                    SignalSpec(amplitude_v=2, frequency_hz=1, phase_deg=90),
                    SignalSpec(waveform="triangle", amplitude_v=2, frequency_hz=1),
                    SignalSpec(waveform="sawtooth", amplitude_v=2, frequency_hz=1))
    result = generate_signals(spec, 0, 8, 4)
    np.testing.assert_allclose(result[:4, 0], [0, 2, 0, -2], atol=1e-14)
    np.testing.assert_allclose(result[:4, 1], [2, 0, -2, 0], atol=1e-14)
    np.testing.assert_allclose(result[:4, 2], [-2, 0, 2, 0])
    np.testing.assert_allclose(result[:4, 3], [-2, -1, 0, 1])
    np.testing.assert_allclose(result[4:], result[:4], atol=1e-14)
    square = generate_signals(settings(SignalSpec(waveform="square", amplitude_v=2,
                                                 frequency_hz=1, duty_cycle=0.25)), 0, 8, 8)
    np.testing.assert_array_equal(square[:, 0], [2, 2, -2, -2, -2, -2, -2, -2])
    sine = generate_signals(spec, 0, 4000, 1000)[:, 0]
    assert np.sqrt(np.mean(sine ** 2)) == pytest.approx(2 / np.sqrt(2))


def test_absolute_step_pulse_edges_and_dc_drift():
    spec = settings(SignalSpec(waveform="step", amplitude_v=2, start_s=0.5),
                    SignalSpec(waveform="pulse", amplitude_v=-2, start_s=0.5, duration_s=0.5),
                    SignalSpec(waveform="dc", amplitude_v=0.2, offset_v=-0.3, drift_v_per_s=0.4))
    result = generate_signals(spec, 0, 6, 4)
    np.testing.assert_array_equal(result[:, 0], [0, 0, 2, 2, 2, 2])
    np.testing.assert_array_equal(result[:, 1], [0, 0, -2, -2, 0, 0])
    np.testing.assert_allclose(result[:, 2], [-0.1, 0, 0.1, 0.2, 0.3, 0.4], atol=1e-15)
    pieces = np.vstack([generate_signals(spec, 0, 3, 4), generate_signals(spec, 3, 3, 4)])
    np.testing.assert_array_equal(pieces, result)


def test_chirp_time_window_and_integrated_frequency():
    spec = settings(SignalSpec(waveform="chirp", amplitude_v=1, frequency_hz=10,
                               end_frequency_hz=20, start_s=0.25, duration_s=1))
    result = generate_signals(spec, 0, 15000, 10000)[:, 0]
    np.testing.assert_array_equal(result[:2500], 0)
    np.testing.assert_array_equal(result[12500:], 0)
    active = result[2500:12500]
    # A linear 10->20 Hz sweep lasts 15 complete cycles in one second.
    rising = np.flatnonzero((active[:-1] < 0) & (active[1:] >= 0))
    assert len(rising) == 14  # Initial zero and the excluded endpoint are not crossings.
    assert np.diff(rising)[0] > np.diff(rising)[-1]
    assert active[5000] == pytest.approx(1.0)  # 6.25 integrated cycles at t=0.5 s.


def test_noise_rms_and_absolute_sample_reproducibility():
    spec = settings(SignalSpec(waveform="noise", amplitude_v=9, noise_rms_v=0.3),
                    SignalSpec(waveform="dc", amplitude_v=1, noise_rms_v=0.2), seed=77)
    data = generate_signals(spec, 0, 100000, 2000)
    assert np.mean(data[:, 0]) == pytest.approx(0, abs=0.003)
    assert np.sqrt(np.mean(data[:, 0] ** 2)) == pytest.approx(0.3, abs=0.003)
    assert np.std(data[:, 1]) == pytest.approx(0.2, abs=0.003)
    assert abs(np.corrcoef(data[:, :2], rowvar=False)[0, 1]) < 0.02
    split = np.vstack([generate_signals(spec, 0, 31, 2000),
                       generate_signals(spec, 31, 4000, 2000),
                       generate_signals(spec, 4031, 95969, 2000)])
    np.testing.assert_array_equal(data, split)
    np.testing.assert_array_equal(data[800:1700], generate_signals(spec, 800, 900, 2000))


def acquire(spec, channels=(0, 1, 2, 3), fragment_size=None, coupling=("DC",) * 4, ranges=(10.0,) * 4):
    device = SimulatedTransport(scenario="custom", signal_settings=spec, fragment_size=fragment_size)
    session = DaqSession(device)
    session.configure(AcquisitionConfig(sample_rate=2000, channels=channels, coupling=coupling, ranges=ranges))
    session.start()
    return session


@pytest.mark.parametrize("coupling", [("DC",) * 4, ("AC", "DC", "AC", "DC")])
def test_transport_noise_channel_identity_fragmentation_and_restart(coupling):
    spec = settings(SignalSpec(amplitude_v=0.7, frequency_hz=13, noise_rms_v=0.03),
                    SignalSpec(waveform="dc", amplitude_v=0.2),
                    SignalSpec(waveform="pulse", start_s=0.125, duration_s=0.15, noise_rms_v=0.01),
                    SignalSpec(waveform="noise", noise_rms_v=0.2))
    with acquire(spec, coupling=coupling) as reference, acquire(spec, channels=(0, 2, 3),
                                                              fragment_size=7, coupling=coupling) as fragmented:
        expected = reference.read(1500)
        received = np.vstack([fragmented.read(3), fragmented.read(301), fragmented.read(1196)])
        np.testing.assert_array_equal(received, expected[:, [0, 2, 3]])
        reference.stop()
        reference.start()
        np.testing.assert_array_equal(reference.read(1500), expected)
        if coupling == ("DC",) * 4:
            truth = generate_signals(spec, 0, 1500, reference.actual_rate)
            np.testing.assert_allclose(expected, truth, rtol=0, atol=10 / 2 ** 24)


def test_adc_clipping_still_uses_selected_ranges_and_24_bit_codes():
    spec = settings(SignalSpec(waveform="dc", amplitude_v=0, offset_v=-12),
                    SignalSpec(waveform="dc", amplitude_v=12),
                    SignalSpec(waveform="dc", amplitude_v=2),
                    SignalSpec(waveform="dc", amplitude_v=0, offset_v=-2))
    with acquire(spec, ranges=(10, 10, 1, 1)) as session:
        expected = [-10, 10 - 20 / 2 ** 24, 1 - 2 / 2 ** 24, -1]
        np.testing.assert_array_equal(session.read(3), np.tile(expected, (3, 1)))
