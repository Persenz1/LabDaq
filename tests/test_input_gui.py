"""Minimal user workflow for selectable voltage and HPF input sources."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets as W
import numpy as np

from labdaq.gui import MainWindow, configure_application
from labdaq.input_dialog import SignalDialog
from labdaq.recording import load_session
from labdaq.signals import SignalSpec, SimulationSettings
from test_hpf import _force_hpf


def _dispose_widgets(app, *widgets):
    # close() only hides these windows. Destroy their pyqtgraph/Qt children
    # while QApplication is alive, rather than during interpreter shutdown.
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_hpf_selected_in_gui_records_all_samples_and_restores_profile(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    path = tmp_path / "force.hpf"
    expected = _force_hpf(path)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    window.recording_root = tmp_path
    try:
        window.load_input_hpf(path)
        assert window.active_channels == (0, 2)
        assert window.channels[2].sensitivity_mv_per_unit == 333.3330078125
        assert not window.rate.isEnabled() and not window.channel_button.isEnabled()
        window.toggle_record()
        window.engine._thread.join(3)
        window.refresh()
        assert window.status_label.text() == "HPF 已播完"
        assert not window.fault_button.isEnabled()
        recording = load_session(window.latest_recording)
        np.testing.assert_array_equal(recording.read(count=100), expected)
        assert recording.metadata["source"] == "file_replay"
        assert "HPF" in window.mode_badge.text()
        window.refresh_protocol()
        assert "不产生 USB" in window.protocol_stats.text()
        window.open_replay(path)
        assert window.replay.voltage_compatible
        assert window.replay.channels[1].index == 2
        profile = tmp_path / "hpf-input.json"
        window.save_profile_to(profile)
        window.scenario.setCurrentIndex(window.scenario.findData("sine"))
        window.load_profile_from(profile)
        assert window.scenario.currentData() == "hpf"
        assert window.input_hpf.path == path
        assert window.channels[2].sensitivity_mv_per_unit == 333.3330078125
    finally:
        _dispose_widgets(app, window)


def test_custom_signal_editor_and_profile_keep_channel_parameters(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    settings = SimulationSettings(channels=(SignalSpec(waveform="pulse", amplitude_v=-.7),
                                            SignalSpec(phase_deg=90), SignalSpec(waveform="noise", noise_rms_v=.03),
                                            SignalSpec(waveform="chirp", end_frequency_hz=73)), seed=42)
    dialog = SignalDialog(settings)
    dialog.apply()
    assert dialog.settings == settings
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        window.scenario.setCurrentIndex(window.scenario.findData("custom"))
        window.signal_settings = dialog.settings
        profile = tmp_path / "voltage-input.json"
        window.save_profile_to(profile)
        window.signal_settings = SimulationSettings()
        window.load_profile_from(profile)
        assert window.signal_settings == settings
        window.start_preview()
        assert window.engine.transport.signal_settings == settings
        assert window.engine.source == "simulation"
    finally:
        _dispose_widgets(app, window, dialog)
