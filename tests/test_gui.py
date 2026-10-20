"""One offscreen end-to-end workflow, using real application controls."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import time
import pytest

from PySide6 import QtWidgets as W, QtGui, QtCore
from PySide6.QtTest import QTest

from labdaq.gui import MainWindow, configure_application, ROOT
from labdaq.recording import load_session, export_csv


def spin(app, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        QTest.qWait(20)
    app.processEvents()
    assert predicate()


def test_chinese_workbench_record_analyze_replay(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    window.scenario.setCurrentIndex(window.scenario.findData("drag"))
    window.recording_root = tmp_path
    window.active_channels = (0, 1, 3)
    window._sync_channel_labels()
    window.show()
    try:
        window.preview_button.click()
        spin(app, lambda: window.engine is not None and window.engine.total_scans >= 200)
        assert window.engine.running
        window.zero_button.click()
        profile = tmp_path / "profile.json"
        window.save_profile_to(profile)
        window.record_button.click()
        assert window.engine.recording
        assert not window.zero_button.isEnabled()
        window.pause_plot.setChecked(True)
        spin(app, lambda: window.engine.recorded_scans >= 800)
        window.record_button.click()
        assert not window.engine.recording
        session = load_session(window.latest_recording)
        assert session.sample_count >= 800 and len(session.channels) == 3
        window.nav.setCurrentRow(2)
        for index in range(3):
            window.analysis_mode.setCurrentIndex(index)
            window.refresh_analysis()
            assert not window.analysis_info.text().startswith("分析设置：")
        window.stop_button.click()
        window.open_last_recording()
        assert window.pages.currentIndex() == 3
        assert window.replay.sample_count == session.sample_count
        names = [text.text for _, text in window.replay_plot.plotItem.legend.items]
        assert names[-1].startswith("AI 3") and len(names) == 3
        export_csv(window.latest_recording, tmp_path / "export.csv")
        window.load_profile_from(profile)
        assert window.active_channels == (0, 1, 3)
        # A changed profile must not reinterpret old samples from another map.
        assert window.engine is None
        assert window.analysis_curve.xData is None or len(window.analysis_curve.xData) == 0
        window.start_preview()
        spin(app, lambda: window.engine.total_scans >= 200)
        window.stop_acquisition()
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_official_hpf_and_cold_boot_offline_views(tmp_path):
    sample = ROOT / "resources/vendor/extracted/quickdaq/LocalAppData/Data Translation/QuickDAQ/Data/QuickDAQ FFT Analyzer Sample Data.hpf"
    if not sample.exists():
        pytest.skip("Official reference files are local-only")
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        window.open_vendor_sample()
        assert window.replay_is_hpf and window.replay.sample_count == 20480
        assert "HPF" in window.mode_badge.text()
        window.analyze_replay()
        assert window.analysis_channel.count() == 2
        assert window.analysis_curve.xData is not None
        assert len(window.analysis_curve.xData) > 100
        assert "回看窗口" in window.analysis_info.text()
        window.analysis_mode.setCurrentIndex(2)
        window.refresh_analysis()
        assert not window.analysis_info.text().startswith("分析设置：")
        window.nav.setCurrentRow(4)
        window.simulate_boot()
        assert "1,105" in window.boot_result.text()
        assert "未访问硬件" in window.boot_result.text()
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_fixed_groups_inline_duration_profile_and_replay(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        window.scenario.setCurrentIndex(window.scenario.findData("drag"))
        window.recording_root = tmp_path
        window.experiment.setText("顺序实验")
        window.duration_mode.setCurrentIndex(window.duration_mode.findData(30))
        assert window.recording_duration_seconds == 30
        window.live_seconds.setValue(20)
        window._set_recording_options({"duration_seconds": .137, "notes": "加载 1 N\n模拟验证"})
        profile = tmp_path / "profile.json"
        window.save_profile_to(profile)
        window._set_recording_options({})
        window.load_profile_from(profile)
        assert window.recording_duration_seconds == .137
        assert window.duration_mode.currentData() == "custom"
        assert window.duration_input.value() == .137
        assert window.live_seconds.value() == 20
        assert "notes" not in window._recording_options()
        for group in (1, 2):
            window.toggle_record()
            path = window.latest_recording
            assert path.name.endswith(f"_顺序实验_{group:03d}")
            assert not window.duration_mode.isEnabled() and not window.duration_input.isEnabled()
            spin(app, lambda: not window.engine.recording and window.duration_mode.isEnabled())
            assert window.engine.running and window.duration_mode.isEnabled()
            recording = load_session(path)
            assert recording.sample_count == int(recording.sample_rate * .137 + .5)
            assert recording.metadata["recording_options"]["group_number"] == group
            window.open_replay(path)
            assert recording.metadata["recording_options"]["notes"] == ""
            assert "定长记录完成" in window.replay_info.text()
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
