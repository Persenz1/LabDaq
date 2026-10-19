"""Idle connection, reconnect, and failed-start UI state without USB hardware."""
import os
from dataclasses import asdict
import threading
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6 import QtCore, QtWidgets as W

import labdaq.gui as gui
from labdaq.core_client import RemoteEngine
from labdaq.processing import ChannelSettings
from labdaq.protocol import AcquisitionConfig
from labdaq.signals import SimulationSettings
from test_daily_workflow import spin


def idle_engine(*, running=False):
    engine = RemoteEngine.__new__(RemoteEngine)
    engine._state = dict(
        profile=dict(source="usb", config=asdict(AcquisitionConfig(sample_rate=5000)),
                     actual_rate=5000, channels=[asdict(ChannelSettings(i, f"AI {i}")) for i in range(4)],
                     signal_settings=SimulationSettings().to_dict()),
        running=running, recording=False, recording_paused=False, device_connected=True,
        error="", termination_reason="user_stop", acquisition_started_at_utc=None,
        last_recording=None, total_scans=0, recorded_scans=0, recording_remaining_scans=None,
    )
    engine._lock = threading.Lock()
    engine._times = np.empty(0)
    engine._raw = np.empty((0, 4))
    engine._snapshot_end = 0
    engine.display_seconds = 10
    engine.display_revision = 0
    engine.connection_error = ""
    engine.stop = Mock()
    engine.detach = Mock()
    engine.shutdown = Mock()
    return engine


@pytest.fixture
def workbench(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    gui.configure_application(app)
    window = gui.MainWindow(core_endpoint=tmp_path / "unused.json")
    window.timer.stop()
    window.show()
    app.processEvents()
    try:
        yield app, window
    finally:
        window.engine = None
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_startup_without_device_stays_idle_and_exposes_reconnect(workbench, monkeypatch):
    app, window = workbench
    start = Mock(side_effect=AssertionError("No device must not connect"))
    monkeypatch.setattr(gui, "enumerate_usb_devices", lambda: [])
    monkeypatch.setattr(RemoteEngine, "start_new", start)
    window.auto_connect_device()
    spin(app, lambda: not window._connecting)
    start.assert_not_called()
    assert window.engine is None and window.reconnect_button.isVisible()
    assert not window.preview_button.isEnabled() and not window.record_button.isEnabled()
    assert "未检测到" in window.notice.text() and not window.error_banner.isVisible()


def test_startup_connects_only_then_preview_reuses_connection(workbench, monkeypatch):
    app, window = workbench
    idle = idle_engine()
    acquiring = idle_engine(running=True)
    start = Mock(side_effect=[idle, acquiring])
    monkeypatch.setattr(gui, "enumerate_usb_devices", lambda: [{"product_id": 0x3998}])
    monkeypatch.setattr(RemoteEngine, "start_new", start)
    window.auto_connect_device()
    spin(app, lambda: not window._connecting)
    assert start.call_args.kwargs["connect_only"] is True
    assert window.engine is idle and not window.engine.running
    assert window.engine.total_scans == window.engine.recorded_scans == 0
    assert window.preview_button.isEnabled() and not window.reconnect_button.isVisible()
    window.start_preview()
    assert window.engine is acquiring
    idle.stop.assert_not_called()
    idle.detach.assert_called_once()
    assert start.call_args.kwargs["source"] == "usb"


def test_failed_preview_keeps_polling_and_disconnect_is_prominent(workbench, monkeypatch):
    app, window = workbench
    idle = idle_engine()
    window.engine = idle
    window._refresh_buttons()
    start = Mock(side_effect=RuntimeError("发送设备命令：Input/Output Error"))
    monkeypatch.setattr(RemoteEngine, "start_new", start)
    window.start_preview()
    assert window.engine is idle
    idle.detach.assert_not_called()
    idle.stop.assert_not_called()
    assert window.error_banner.isVisible()
    idle._state.update(device_connected=False, error="USB 已断开", termination_reason="device_disconnected")
    window.refresh()
    assert "已断开" in window.side_device_status.text()
    assert window.p.danger in window.side_device_status.styleSheet()
    assert window.status_label.property("tone") == "danger"
    assert window.mode_badge.property("tone") == "danger"
    assert window.reconnect_button.isVisible() and window.reconnect_button.isEnabled()
    assert not window.preview_button.isEnabled()

    recovered = idle_engine()
    monkeypatch.setattr(RemoteEngine, "start_new", Mock(return_value=recovered))
    window.reconnect_button.click()
    spin(app, lambda: not window._connecting)
    assert window.engine is recovered and not recovered.running
    assert recovered.total_scans == 0 and not recovered.recording
    assert not window.error_banner.isVisible() and not window.reconnect_button.isVisible()


@pytest.mark.parametrize("selected", [(0, 2), (1,)])
def test_channel_selection_keeps_usb_connection_and_discards_old_samples(workbench, monkeypatch, selected):
    app, window = workbench
    idle = idle_engine()
    idle._times = np.arange(64) / 5000
    idle._raw = np.tile([.1, .2, .3, .4], (64, 1))
    idle._state["total_scans"] = 64
    window.channels = [ChannelSettings(i, f"AI {i}", unit="V") for i in range(4)]
    window.engine = idle
    window._sync_channel_labels()
    window.refresh()
    assert len(window.live_curves[0].yData) == 64

    def apply_selection(dialog):
        for i, row in enumerate(dialog.cells):
            row[0].setChecked(i in selected)
        dialog.apply()
        return dialog.result()

    monkeypatch.setattr(gui.ChannelDialog, "exec", apply_selection)
    snapshot = Mock(wraps=idle.snapshot)
    monkeypatch.setattr(idle, "snapshot", snapshot)
    window.edit_channels()
    assert window.engine is idle and idle.device_connected
    assert window.active_channels == selected
    idle.stop.assert_not_called()
    idle.shutdown.assert_not_called()
    idle.detach.assert_not_called()
    assert window.preview_button.isEnabled() and not window.reconnect_button.isVisible()
    assert "设备保持连接" in window.notice.text()
    for i, button in enumerate(window.live_channel_checks):
        assert button.parentWidget() is window.metric_cards[i]
        assert button.isVisible() == (i in selected)
    window.refresh()
    window.refresh_analysis()
    snapshot.assert_not_called()
    assert all(curve.yData is None for curve in window.live_curves)
    assert window.analysis_curve.yData is None

    acquiring = idle_engine(running=True)
    acquiring._state["profile"]["config"]["channels"] = selected
    acquiring._times = idle._times.copy()
    acquiring._raw = idle._raw[:, selected].copy()
    acquiring._state["total_scans"] = 64
    start = Mock(return_value=acquiring)
    monkeypatch.setattr(RemoteEngine, "start_new", start)
    window.start_preview()
    assert start.call_args.args[0].channels == selected
    assert not start.call_args.kwargs.get("connect_only", False)
    idle.detach.assert_called_once()
    idle.shutdown.assert_not_called()
    window.refresh()
    for j, i in enumerate(selected):
        np.testing.assert_array_equal(window.live_curves[i].yData, acquiring._raw[:, j])


def test_display_selection_sends_no_usb_commands(workbench):
    app, window = workbench
    idle = idle_engine(running=True)
    idle._state["recording"] = True
    window.engine = idle
    for check in window.live_channel_checks:
        check.click()
        assert not check.isChecked() and check.text() == "隐藏"
        check.click()
        assert check.isChecked() and check.text() == "显示"
    assert window.engine is idle and idle.running and idle.device_connected and idle.recording
    assert window.active_channels == (0, 1, 2, 3)
    idle.stop.assert_not_called()
    idle.shutdown.assert_not_called()
    idle.detach.assert_not_called()
