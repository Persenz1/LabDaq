"""Voltage replay behavior using a small in-memory HPF reader substitute."""

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from labdaq.inputs import HpfReplaySession
from labdaq.processing import ChannelSettings
from labdaq.protocol import AcquisitionConfig
from labdaq.transport import TransportError


class MemoryHpfReader:
    def __init__(self, values, indices=(0, 2)):
        self.values = np.asarray(values, dtype=np.float64)
        self.path = Path("in-memory-reference.hpf")
        self.sample_rate = 1000.1159554729508
        self.sample_count = len(self.values)
        self.channels = [ChannelSettings(i, f"Force {i}", sensitivity_mv_per_unit=500,
                                         zero_volts=0.25, inverted=True) for i in indices]
        self.metadata = {
            "source": "vendor_hpf",
            "channels": [asdict(channel) for channel in self.channels],
            "vendor_configuration_xml": "<large-configuration/>",
            "chunks": [{"chunk_id": 0x3000}],
            "index_entries": [[0, len(self.values)]],
            "waveform_only": True,
            "ignored_chunks": [0x8000],
            "acquisition_config": {"coupling": ["DC", "DC"]},
        }
        self.calls = []

    def read(self, start, count):
        self.calls.append((start, count))
        return self.values[start:start + count].copy()


def configured(reader, *, loop=False, **settings):
    session = HpfReplaySession(reader, loop=loop)
    config = AcquisitionConfig(channels=tuple(channel.index for channel in reader.channels),
                               **settings)
    session.configure(config)
    session.start()
    return session


def test_finite_replay_preserves_tail_and_exact_file_clock():
    values = np.arange(10, dtype=float).reshape(5, 2) / 7
    reader = MemoryHpfReader(values)
    session = configured(reader, sample_rate=2000)

    assert session.actual_rate == reader.sample_rate
    np.testing.assert_array_equal(session.read(3), values[:3])
    np.testing.assert_array_equal(session.read(3), values[3:])
    assert session.read(3).shape == (0, 2)
    assert session.samples_read == session.source_position == 5
    assert reader.calls == [(0, 3), (3, 3), (5, 3)]
    session.stop()


def test_loop_repeats_all_samples_across_multiple_seams():
    values = np.array([[0.11, -1.1], [0.22, -2.2], [0.33, -3.3]])
    session = configured(MemoryHpfReader(values), loop=True)
    np.testing.assert_array_equal(session.read(2), values[:2])
    np.testing.assert_array_equal(session.read(8), values[[2, 0, 1, 2, 0, 1, 2, 0]])
    assert session.samples_read == 10
    assert session.source_position == 1
    np.testing.assert_array_equal(session.read(2), values[1:])
    assert session.samples_read == 12
    assert session.source_position == 0


def test_file_volts_are_not_quantized_filtered_zeroed_or_converted():
    # Values intentionally include a sub-LSB quantity and values beyond the
    # requested replay range, with nonzero engineering zero/inverted settings.
    values = np.array([[1e-12, -1e-12], [2.234567890123, -3.123456789012],
                       [0.123456789876, 0.987654321234]])
    reader = MemoryHpfReader(values)
    session = configured(reader, sample_rate=4000, ranges=(1.0,) * 4,
                         coupling=("AC",) * 4, iepe=(True,) * 4)
    np.testing.assert_array_equal(session.read(10), values)
    np.testing.assert_array_equal(reader.values, values)
    assert session.config.coupling == ("AC",) * 4
    assert session.config.ranges == (1.0,) * 4
    assert session.input_metadata["original_metadata"]["acquisition_config"] == {
        "coupling": ["DC", "DC"]}


def test_configure_requires_physical_channel_order_not_just_width():
    reader = MemoryHpfReader([[1.0, 2.0]], indices=(0, 2))
    session = HpfReplaySession(reader)
    with pytest.raises(ValueError, match="物理通道顺序"):
        session.configure(AcquisitionConfig(channels=(0, 1)))
    reversed_reader = MemoryHpfReader([[1.0, 2.0]], indices=(2, 0))
    with pytest.raises(ValueError, match="物理通道顺序"):
        HpfReplaySession(reversed_reader).configure(AcquisitionConfig(channels=(0, 2)))
    assert session.configure(AcquisitionConfig(channels=(0, 2))) == reader.sample_rate


def test_metadata_retains_source_conversion_and_limits_as_snapshot():
    reader = MemoryHpfReader([[1.0, 2.0]])
    session = HpfReplaySession(reader, loop=True)
    info = session.input_metadata
    assert session.source == "file_replay" and session.transport is None
    assert info["path"] == str(reader.path.resolve())
    assert info["original_source"] == "vendor_hpf"
    assert info["sample_count"] == 1 and info["sample_rate"] == reader.sample_rate
    assert info["loop"] is True and info["start_scan"] == 0
    original = info["original_metadata"]
    assert original["ignored_chunks"] == [0x8000]
    assert original["channels"][0]["zero_volts"] == 0.25
    assert not {"vendor_configuration_xml", "chunks", "index_entries"} & original.keys()
    reader.metadata["channels"][0]["zero_volts"] = 99
    assert original["channels"][0]["zero_volts"] == 0.25


def test_stop_restart_resets_position_but_close_is_final():
    values = np.arange(6, dtype=float).reshape(3, 2)
    session = configured(MemoryHpfReader(values))
    session.read(2)
    session.stop()
    session.start()
    assert session.samples_read == 0
    np.testing.assert_array_equal(session.read(1), values[:1])
    session.close()
    session.close()
    assert session.closed and not session.running
    with pytest.raises(TransportError, match="已关闭"):
        session.start()
    with pytest.raises(TransportError, match="已关闭"):
        session.configure(AcquisitionConfig(channels=(0, 2)))


def test_empty_looping_file_finishes_without_padding_or_busy_loop():
    session = configured(MemoryHpfReader(np.empty((0, 2))), loop=True)
    assert session.read(20).shape == (0, 2)
    assert session.samples_read == 0
