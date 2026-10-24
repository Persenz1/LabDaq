"""AO boundary checks, GUI lifecycle and profile persistence without hardware."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from labdaq.output_panel import DEFAULT_SETTINGS, validate_settings
from test_device_interaction import workbench, idle_engine
from test_core import core, profile


@pytest.mark.parametrize("change", [dict(offset_v=2.9), dict(frequency_hz=24000),
                                     dict(sample_rate=9999), dict(amplitude_v=float("nan"))])
def test_invalid_output_is_rejected_by_ui_and_core(core, change):
    settings = DEFAULT_SETTINGS | change
    with pytest.raises(ValueError):
        validate_settings(settings)
    core.request("start", profile=profile())
    with pytest.raises((RuntimeError, ValueError)):
        core.request("start_output", settings=settings)
    state = core.request("poll")
    assert state["running"] and state["output"] is None and not state["error"]


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
    engine.connection_error = ""
    window._refresh_buttons()
    panel.stop_button.click()
    engine.stop_output.assert_called_once()
    assert not engine.running and panel.start_button.isEnabled() and "0 V" in panel.status.text()

    engine._state["running"] = True
    window._refresh_buttons()
    panel.start_button.click()
    assert engine.running and engine.start_output.call_count == 2
    panel.stop_button.click()
    assert engine.running and engine.stop_output.call_count == 2


def test_closing_gui_detaches_idle_ai_with_active_output(workbench):
    _, window = workbench
    engine = idle_engine()
    engine._state["output"] = dict(running=True, settings=DEFAULT_SETTINGS,
                                   actual_rate=48000, zeroed=False)
    window.engine = engine
    window.close()
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
