"""Installed application input boundaries and source-only development entry."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets as W

from labdaq import __version__
import labdaq.gui as gui


def test_default_channels_are_unscaled_voltage_and_zero_sensitivity_is_accepted(tmp_path, monkeypatch):
    import numpy as np
    from labdaq.processing import convert_volts

    app = W.QApplication.instance() or W.QApplication([])
    window = gui.MainWindow(core_endpoint=tmp_path / "unused.json")
    dialog = gui.ChannelDialog(window)
    messages = []
    monkeypatch.setattr(W.QMessageBox, "information", lambda *args: messages.append(args[-1]))
    try:
        assert [c.name for c in window.channels] == ["通道1", "通道2", "通道3", "通道4"]
        assert all(c.unit == "V" and c.sensitivity_mv_per_unit == 0 for c in window.channels)
        for row in dialog.cells:
            assert row[2].value() == 0 and row[2].text() == "0"
            assert row[3].currentText() == ""
            assert row[4].currentText() == "V" and row[5].value() == 0
            assert not row[5].isEnabled()
            assert row[6].value() == row[7].value() == 0
            assert not row[8].isChecked() and not row[11].isChecked()
            assert row[9].currentText() == "±10 V" and row[10].currentText() == "DC"
        dialog.apply()
        assert not messages
        raw = np.array([[0.125, -1.0, 3.0, -0.5]])
        np.testing.assert_array_equal(convert_volts(raw, window.channels), raw)
        dialog.cells[1][4].setCurrentText("N")
        assert dialog.cells[1][5].isEnabled()
        dialog.apply()
        assert "大于 0 的灵敏度" in messages[-1]
        assert window.channels[1].unit == "V"
    finally:
        dialog.close()
        dialog.deleteLater()
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_installed_window_has_only_hardware_input_and_rejects_developer_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "FROZEN", True)
    app = W.QApplication.instance() or W.QApplication([])
    window = gui.MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        assert [window.scenario.itemData(i) for i in range(window.scenario.count())] == ["usb"]
        assert window.windowTitle() == f"LabDAQ {__version__} · 实验数据采集"
        assert f"LabDAQ v{__version__}" in [label.text() for label in window.findChildren(W.QLabel)]
        profile = tmp_path / "profile.json"
        window.save_profile_to(profile)
        data = json.loads(profile.read_text(encoding="utf-8"))
        for source in ("drag", "sine", "step", "custom", "hpf", "rust"):
            data["scenario"] = source
            profile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with pytest.raises(ValueError, match="安装版仅支持 DT9837C 原厂驱动输入"):
                window.load_profile_from(profile)
            assert window.scenario.currentData() == "usb"
            assert window.engine is None
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("frozen", [False, True])
def test_command_line_help_exposes_demo_only_in_source(monkeypatch, capsys, frozen):
    monkeypatch.setattr(gui, "FROZEN", frozen)
    monkeypatch.setattr(gui.sys, "argv", ["LabDAQ", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        gui.main()
    assert exit_info.value.code == 0
    assert ("--demo" in capsys.readouterr().out) is not frozen


def test_installed_command_line_rejects_demo(monkeypatch, capsys):
    monkeypatch.setattr(gui, "FROZEN", True)
    monkeypatch.setattr(gui.sys, "argv", ["LabDAQ", "--demo"])
    with pytest.raises(SystemExit) as exit_info:
        gui.main()
    assert exit_info.value.code == 2
    assert "unrecognized arguments: --demo" in capsys.readouterr().err
