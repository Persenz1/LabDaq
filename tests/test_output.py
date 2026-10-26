"""AO boundary checks, GUI lifecycle and profile persistence without hardware."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
import numpy as np

from labdaq.output_panel import DEFAULT_SETTINGS, OUTPUT_RATES, validate_settings, preview_waveform
from test_device_interaction import workbench, idle_engine
from test_core import core, profile


@pytest.mark.parametrize("change", [dict(offset_v=2.9), dict(frequency_hz=24000),
                                     dict(sample_rate=9999), dict(amplitude_v=float("nan")),
                                     dict(offset_v=-2.9), dict(waveform="dc", amplitude_v=3.1),
                                     dict(waveform="dc", offset_v=3.0001)])
def test_invalid_output_is_rejected_by_ui_and_core(core, change):
    settings = DEFAULT_SETTINGS | change
    with pytest.raises(ValueError):
        validate_settings(settings)
    core.request("start", profile=profile())
    with pytest.raises((RuntimeError, ValueError)):
        core.request("start_output", settings=settings)
    state = core.request("poll")
    assert state["running"] and state["output"] is None and not state["error"]


@pytest.mark.parametrize("rate", OUTPUT_RATES)
def test_output_rate_presets_limit_frequency(workbench, rate):
    _, window = workbench
    panel = window.output_panel
    assert tuple(panel.rate.itemData(i) for i in range(panel.rate.count())) == OUTPUT_RATES
    panel.rate.setCurrentIndex(panel.rate.findData(rate))
    assert panel.controls["frequency_hz"].maximum() == rate / 2 - .001
    assert f"{rate / 2:,.0f} Hz" in panel.frequency_hint.text()
    panel.controls["frequency_hz"].setValue(rate)
    assert panel.settings()["frequency_hz"] < rate / 2
    with pytest.raises(ValueError):
        validate_settings(DEFAULT_SETTINGS | dict(sample_rate=rate, frequency_hz=rate / 2))


def test_voltage_combination_blocks_start_and_dc_uses_offset(workbench):
    _, window = workbench
    panel = window.output_panel
    window.engine = idle_engine()
    window._refresh_buttons()
    panel.controls["amplitude_v"].setValue(1)
    panel.controls["offset_v"].setValue(2)
    assert panel.start_button.isEnabled()
    panel.controls["offset_v"].setValue(2.0001)
    assert not panel.start_button.isEnabled() and "不能超过 3 V" in panel.voltage_hint.text()
    panel.controls["offset_v"].setValue(-2.0001)
    assert not panel.start_button.isEnabled()
    panel.waveform.setCurrentIndex(panel.waveform.findData("dc"))
    panel.controls["offset_v"].setValue(-3)
    assert panel.start_button.isEnabled()
    assert not panel.controls["amplitude_v"].isEnabled()
    assert panel.settings()["offset_v"] == -3
    panel.waveform.setCurrentIndex(panel.waveform.findData("sine"))
    assert not panel.start_button.isEnabled()


def test_output_buttons_follow_device_and_core_state(workbench):
    _, window = workbench
    panel = window.output_panel
    assert not panel.start_button.isEnabled() and not panel.stop_button.isEnabled()
    engine = idle_engine()
    window.engine = engine
    window._refresh_buttons()
    assert panel.start_button.isEnabled() and panel.stop_button.isEnabled()
    assert "独立输出" in panel.status.text()

    def start(settings):
        engine._state["output"] = dict(running=True, settings=settings, actual_rate=48000, zeroed=False)

    def stop():
        engine._state["output"].update(running=False, zeroed=True)

    engine.start_output, engine.stop_output = Mock(side_effect=start), Mock(side_effect=stop)
    panel.start_button.click()
    engine.start_output.assert_called_once_with(DEFAULT_SETTINGS)
    assert not engine.running
    assert not panel.waveform.isEnabled() and not panel.start_button.isEnabled()
    assert "输出中" in panel.status.text()
    assert panel.start_button.text() == "正在输出" and panel.start_button.property("active")
    assert "正在输出" in window.output_monitor_status.text()
    window.nav.setCurrentRow(1)
    window.nav.setCurrentRow(0)
    assert engine.output["running"] and not engine.running
    engine.start_output.assert_called_once()
    engine.stop_output.assert_not_called()
    window.stop_acquisition()
    engine.stop.assert_called_once()
    engine.stop_output.assert_not_called()
    assert engine.output["running"]
    engine.connection_error = "lost"
    window._refresh_buttons()
    assert not panel.stop_button.isEnabled() and "未知" in panel.status.text()
    assert not panel.start_button.property("active") and panel.start_button.text() == "状态未知"
    assert window.output_curve.getData()[0] is None
    engine.connection_error = ""
    window._refresh_buttons()
    panel.stop_button.click()
    engine.stop_output.assert_called_once()
    assert not engine.running and panel.start_button.isEnabled() and "0 V" in panel.status.text()
    assert panel.start_button.text() == "开始输出" and not panel.start_button.property("active")

    engine._state["running"] = True
    window._refresh_buttons()
    panel.start_button.click()
    assert engine.running and engine.start_output.call_count == 2
    panel.stop_button.click()
    assert engine.running and engine.stop_output.call_count == 2


@pytest.mark.parametrize("waveform", ["dc", "sine", "square", "triangle"])
def test_preview_agrees_with_core_and_stays_in_voltage_range(core, waveform):
    settings = DEFAULT_SETTINGS | dict(waveform=waveform, amplitude_v=1.2, offset_v=-.4, frequency_hz=120)
    times, volts = preview_waveform(settings)
    result = core.request("preview_output", settings=settings, count=len(times))
    np.testing.assert_allclose(volts, result["samples"], atol=1e-10)
    assert times[-1] == (1 if waveform == "dc" else 3 / settings["frequency_hz"])
    assert np.max(np.abs(volts)) <= 3
    assert not core.request("poll")["running"]


def test_output_monitor_tracks_edits_and_rejects_invalid_parameters(workbench):
    _, window = workbench
    panel = window.output_panel
    panel.controls["amplitude_v"].setValue(.8)
    panel.controls["offset_v"].setValue(.3)
    times, volts = window.output_curve.getData()
    assert times[-1] == .3
    assert np.min(volts) == pytest.approx(-.5)
    assert np.max(volts) == pytest.approx(1.1)
    panel.controls["offset_v"].setValue(2.5)
    assert window.output_curve.getData()[0] is None
    assert "参数无效" in window.output_monitor_status.text()

    engine = idle_engine()
    window.engine = engine
    engine.start_output = Mock(side_effect=RuntimeError("output failed"))
    panel.set_settings(DEFAULT_SETTINGS)
    window._refresh_buttons()
    panel.start_button.click()
    assert not panel.start_button.property("active")
    assert panel.start_button.text() == "开始输出"


def test_closing_gui_detaches_idle_ai_with_active_output(workbench):
    _, window = workbench
    engine = idle_engine()
    engine._state["output"] = dict(running=True, settings=DEFAULT_SETTINGS,
                                   actual_rate=48000, zeroed=False)
    window.engine = engine
    window.close()
    engine.shutdown_if_idle.assert_called_once_with()
    engine.shutdown.assert_not_called()
    engine.detach.assert_called_once()


def test_output_config_load_never_starts_output(workbench, tmp_path):
    _, window = workbench
    settings = DEFAULT_SETTINGS | dict(waveform="triangle", amplitude_v=.4, frequency_hz=23, sample_rate=96000)
    window.output_panel.set_settings(settings)
    path = tmp_path / "profile.json"
    window.save_profile_to(path)
    window.output_panel.set_settings(DEFAULT_SETTINGS)
    window.load_profile_from(path)
    assert window.output_panel.settings() == settings
    assert window.engine is None


def test_running_output_is_restored_read_only_on_reconnect(workbench, tmp_path):
    _, window = workbench
    engine = idle_engine(running=True)
    engine._state["profile"].update(experiment="AO", recording_options={}, recording_root=str(tmp_path))
    settings = DEFAULT_SETTINGS | dict(waveform="dc", offset_v=-.2)
    engine._state["output"] = dict(running=True, settings=deepcopy(settings), actual_rate=48000, zeroed=False)
    engine.start_output = Mock()
    window.engine = engine
    window._restore_core_session()
    assert window.output_panel.settings() == settings
    assert not window.output_panel.controls["offset_v"].isEnabled()
    assert "输出中" in window.output_panel.status.text()
    engine.start_output.assert_not_called()
