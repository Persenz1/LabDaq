"""File-backed voltage input for the acquisition engine, without hardware access."""

from copy import deepcopy

import numpy as np

from .protocol import AcquisitionConfig
from .transport import TransportError


class HpfReplaySession:
    """Stream an already-opened HPF reader at its original sample rate.

    The reader supplies volts and channel conversion settings. No ADC
    quantization, coupling filter, engineering conversion or startup discard is
    applied here. The engine owns wall-clock pacing and recording offsets.
    A finite replay returns its final short block, then an empty voltage block.
    Looping repeats all samples; its seam is not physical signal continuity.
    """

    source = "file_replay"
    transport = None

    def __init__(self, reader, *, loop=False):
        self.reader = reader
        self.loop = loop
        self.config: AcquisitionConfig | None = None
        self.actual_rate = 0.0
        self.running = False
        self.closed = False
        self.samples_read = 0
        self._channel_indices = tuple(channel.index for channel in reader.channels)
        # Retain conversion/configuration and waveform-only limitations, but not
        # a duplicate of the full XML text or the file's chunk/index directories.
        original_metadata = {
            key: value for key, value in reader.metadata.items()
            if key not in {"vendor_configuration_xml", "chunks", "index_entries"}
        }
        self.input_metadata = {
            "path": str(reader.path.resolve()),
            "original_source": "vendor_hpf",
            "sample_count": reader.sample_count,
            "sample_rate": reader.sample_rate,
            "loop": loop,
            "start_scan": 0,
            "original_metadata": deepcopy(original_metadata),
        }

    def configure(self, config: AcquisitionConfig) -> float:
        if self.closed:
            raise TransportError("HPF 回放会话已关闭")
        if self.running:
            raise TransportError("HPF 回放中不能更改配置")
        if tuple(config.channels) != self._channel_indices:
            raise ValueError("回放通道必须与 HPF 物理通道顺序一致")
        self.config = config
        # Hardware settings remain available as configuration, never operations
        # to reapply to an already acquired waveform. Do not plan a new PLL.
        self.actual_rate = self.reader.sample_rate
        return self.actual_rate

    def start(self):
        if self.closed:
            raise TransportError("HPF 回放会话已关闭")
        if self.config is None:
            raise TransportError("请先配置 HPF 回放")
        if self.running:
            raise TransportError("HPF 回放已经开始")
        self.samples_read = 0
        self.running = True

    @property
    def source_position(self):
        """Source index of the next sample; global samples_read never wraps."""
        count = self.reader.sample_count
        return self.samples_read % count if self.loop and count else self.samples_read

    def read(self, scans: int) -> np.ndarray:
        if not self.running:
            raise TransportError("HPF 回放尚未开始")
        if not isinstance(scans, int) or scans <= 0:
            raise ValueError("读取扫描数需为正整数")
        if not self.loop or self.reader.sample_count == 0:
            block = self.reader.read(self.samples_read, scans)
            self.samples_read += len(block)
            return block

        remaining = scans
        blocks = []
        while remaining:
            position = self.source_position
            count = min(remaining, self.reader.sample_count - position)
            block = self.reader.read(position, count)
            blocks.append(block)
            self.samples_read += len(block)
            remaining -= len(block)
        return blocks[0] if len(blocks) == 1 else np.concatenate(blocks, axis=0)

    def stop(self):
        self.running = False

    def close(self):
        self.stop()
        self.closed = True
