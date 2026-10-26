"""Basic pause, numeric displays, time axes and gapped replay integration."""
import json
import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets as W
from PySide6.QtTest import QTest

from labdaq.core_client import CORE_EXE, CoreClient
from labdaq.gui import MainWindow, configure_application
from labdaq.processing import ChannelSettings
from labdaq.recording import SessionRecorder, load_session
from labdaq.signals import SignalSpec, SimulationSettings
from test_daily_workflow import spin, dispose


def test_replay_time_axes_cursors_and_pause_gaps(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    path = tmp_path / "gapped"
    fs = 1000
    channel = ChannelSettings(3, "电压", unit="V")
    config = {"record_start_scan": 200, "acquisition_started_at_utc": "2026-10-19T04:00:00+00:00"}
    with SessionRecorder(path, fs, [channel], acquisition_config=config) as recorder:
        recorder.write(np.sin(np.arange(100) / 10)[:, None], source_start_scan=200)
        recorder.write(np.cos(np.arange(100) / 10)[:, None], source_start_scan=500)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        window.open_replay(path)
        reader = window.replay
        assert reader.duration == .2 and reader.elapsed_duration == .4
        assert np.isnan(window.replay_curves[0].yData).sum() == 1
        window.replay_measurements.enabled_checkbox.setChecked(True)
        for line, value in zip(window.replay_measurements.cursor_lines, (.05, .35)):
            line.setValue(value)
        assert window.replay_measurements.measurements["delta_t"] == pytest.approx(.3)
        assert window._replay_selection() == (0, 200, [3])
        window.replay_time_axis.setCurrentIndex(window.replay_time_axis.findData("absolute"))
        epoch = datetime.fromisoformat(config["acquisition_started_at_utc"]).timestamp() + .2
        assert window._replay_epoch() == epoch
        assert isinstance(window.replay_plot.getAxis("bottom"), pg.DateAxisItem)
        assert window.replay_curves[0].xData[0] == pytest.approx(epoch, abs=1e-6)
        assert window.replay_measurements.measurements["delta_t"] == pytest.approx(.3)
        window.replay_time_axis.setCurrentIndex(window.replay_time_axis.findData("samples"))
        assert not isinstance(window.replay_plot.getAxis("bottom"), pg.DateAxisItem)
        assert window.replay_measurements.measurements["delta_t"] == pytest.approx(.3)
        assert [line.value() for line in window.replay_measurements.cursor_lines] == pytest.approx((50, 350))
        window.replay_time_axis.setCurrentIndex(0)
        window.replay_seconds.setValue(.05)
        window.replay_plot.setXRange(.15, .20, padding=0)
        assert len(window._replay_window()[0]) == 0
        window.replay_seconds.setValue(1)
        window.analyze_replay()
        window.refresh_analysis()
        assert "暂停缺口" in window.analysis_info.text()
        # Missing source timestamps remain relative instead of using file mtime.
        reader.metadata.pop("acquisition_started_at_utc")
        reader.metadata.pop("started_at_utc")
        window.replay_time_axis.setCurrentIndex(window.replay_time_axis.findData("absolute"))
        assert window.replay_time_axis.currentData() == "relative"
        assert "没有可用的绝对起点" in window.notice.text()
    finally:
        dispose(app, window)


def test_gui_pause_reconnect_numeric_modes_and_profile(tmp_path, monkeypatch):
    if not CORE_EXE.exists():
        pytest.skip("Build the Rust core first")
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    client, _ = CoreClient.connect_or_start(tmp_path / "endpoint.json", port=0)
    first = MainWindow(core_endpoint=client.endpoint)
    second = None
    try:
        first.recording_root = tmp_path
        first.scenario.setCurrentIndex(first.scenario.findData("rust"))
        first.channels = [ChannelSettings(i, f"电压 {i}", unit="V") for i in range(4)]
        first.signal_settings = SimulationSettings(channels=(SignalSpec(waveform="dc", amplitude_v=.4),) * 4)
        first.live_time_axis.setCurrentIndex(first.live_time_axis.findData("absolute"))
        first._set_recording_options({"duration_seconds": .6})
        first.toggle_record()
        spin(app, lambda: first.engine.recorded_scans >= 200)
        first.pause_record_button.click()
        assert first.engine.recording and first.engine.recording_paused
        assert first.pause_record_button.text() == "继续记录"
        paused = first.engine.recorded_scans
        first.close()
        QTest.qWait(150)
        second = MainWindow(core_endpoint=client.endpoint)
        assert second.engine.recording_paused
        assert second.engine.recorded_scans == paused
        assert "记录已暂停" in second.status_label.text()
        second.live_time_axis.setCurrentIndex(second.live_time_axis.findData("absolute"))
        spin(app, lambda: second.live_curves[0].xData is not None)
        assert isinstance(second.live_plot.getAxis("bottom"), pg.DateAxisItem)
        # A fixed display buffer isolates every readout definition from polling timing.
        values = np.linspace(-.4, .6, 1000)
        sample_times = np.arange(1000) / second.engine.sample_rate
        monkeypatch.setattr(second.engine, "snapshot", lambda seconds=8: (sample_times, np.tile(values[:, None], (1, 4))))
        second.value_decimals.setValue(6)
        expected = {"last": values[-1], "first": values[0], "mean": np.mean(values),
                    "min": np.min(values), "max": np.max(values), "rms": np.sqrt(np.mean(values ** 2)),
                    "std": np.std(values), "delta": np.ptp(values)}
        for mode, truth in expected.items():
            second.metric_mode_combos[0].setCurrentIndex(second.metric_mode_combos[0].findData(mode))
            second.refresh()
            assert float(second.metric_values[0].text()) == pytest.approx(truth, abs=5.1e-7)
        modes = ["delta", "mean", "min", "max"]
        for combo, mode in zip(second.metric_mode_combos, modes):
            combo.setCurrentIndex(combo.findData(mode))
        second.refresh()
        assert float(second.metric_values[0].text()) == pytest.approx(expected["delta"], abs=5.1e-7)
        assert float(second.metric_values[1].text()) == pytest.approx(expected["mean"], abs=5.1e-7)
        assert float(second.metric_values[2].text()) == pytest.approx(expected["min"], abs=5.1e-7)
        assert float(second.metric_values[3].text()) == pytest.approx(expected["max"], abs=5.1e-7)
        profile = tmp_path / "display.json"
        second.save_profile_to(profile)
        saved = json.loads(profile.read_text(encoding="utf-8"))
        assert saved["display"]["value_modes"] == modes and saved["display"]["value_decimals"] == 6
        second.pause_plot.setChecked(True)
        second.pause_record_button.click()
        spin(app, lambda: not second.engine.recording)
        assert not second.engine.recording_paused
        record = load_session(second.latest_recording)
        assert len(record.recording_segments) == 2
        assert np.max(np.diff(record.sample_times())) > .1
        assert abs(record.duration - .6) <= .5 / record.sample_rate
        second.stop_acquisition()
        second.load_profile_from(profile)
        assert second.live_time_axis.currentData() == "absolute"
        assert [combo.currentData() for combo in second.metric_mode_combos] == modes
        assert second.value_decimals.value() == 6
        saved["display"].pop("value_modes")
        saved["display"]["value_mode"] = "rms"
        profile.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        second.load_profile_from(profile)
        assert [combo.currentData() for combo in second.metric_mode_combos] == ["rms"] * 4
    finally:
        for window in (first, second):
            if window is not None and window.engine is not None:
                if window.engine.running:
                    window.stop_acquisition()
        for window in (first, second):
            if window is not None:
                window.close()
        try:
            client.request("stop")
            client.request("shutdown")
        except OSError:
            pass
        dispose(app, *[w for w in (first, second) if w is not None])
