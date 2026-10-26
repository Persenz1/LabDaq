"""Rust/Python numerical comparison and actual GUI-process crash/reconnect."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import asdict

import numpy as np
import pytest

from labdaq.core_client import CORE_EXE, ROOT, CoreClient, RemoteEngine
from labdaq.processing import ChannelSettings, convert_volts
from labdaq.protocol import AcquisitionConfig, plan_clock, encode_adc, decode_adc
from labdaq.recording import load_session, export_csv
from labdaq.signals import SignalSpec, SimulationSettings, generate_signals


def until(function, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError("Timed out waiting for the core/GUI")


@pytest.fixture
def core(tmp_path):
    if not CORE_EXE.exists():
        pytest.skip("Build core/Cargo.toml in release mode first")
    client, _ = CoreClient.connect_or_start(tmp_path / "endpoint.json", port=0)
    try:
        yield client
    finally:
        try:
            client.request("stop")
            client.request("shutdown")
        except (OSError, ValueError):
            pass


def profile(settings=None, selected=(0, 1, 2, 3), rate=2000, coupling=("DC",) * 4, ranges=(10.0,) * 4):
    config = AcquisitionConfig(sample_rate=rate, channels=selected, coupling=coupling, ranges=ranges)
    return {"config": asdict(config), "actual_rate": plan_clock(rate).actual_rate,
            "channels": [asdict(ChannelSettings(i, f"输入 {i}", zero_volts=.13, inverted=i == 2)) for i in range(4)],
            "signal_settings": (settings or SimulationSettings()).to_dict(),
            "experiment": "核心测试", "recording_root": str(ROOT / "recordings")}


@pytest.mark.parametrize("contents", ["{broken", '{"port":47638}'])
def test_invalid_endpoint_is_replaced_by_new_core(tmp_path, contents):
    endpoint = tmp_path / "invalid.json"
    endpoint.write_text(contents, encoding="utf-8")
    client, state = CoreClient.connect_or_start(endpoint)
    try:
        assert client.address["port"] > 0
        assert not state["running"] and not state["recording"]
    finally:
        client.request("shutdown")


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI/API startup lock")
def test_simultaneous_clients_share_one_new_core(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    endpoint = tmp_path / "shared.json"
    barrier = threading.Barrier(2)

    def start():
        barrier.wait(timeout=5)
        return CoreClient.connect_or_start(endpoint)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: start(), range(2)))
    try:
        assert first.address == second.address
    finally:
        first.request("shutdown")


def test_open_gui_tracks_external_acquisition_channel_changes(core, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets as W
    from labdaq.gui import MainWindow, configure_application

    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    core.request("start", profile=profile(rate=1000))
    window = MainWindow(core_endpoint=core.endpoint)
    try:
        until(lambda: window.engine.total_scans >= 100)
        window.refresh()
        core.request("stop")
        updated = profile(selected=(0, 2), rate=2000)
        updated["channels"][2]["name"] = "Agent 通道"
        updated["experiment"] = "Agent 实验"
        core.request("start", profile=updated)
        until(lambda: tuple(window.engine.config.channels) == (0, 2)
              and window.engine.total_scans >= 100)
        window.refresh()
        assert window.active_channels == (0, 2)
        assert window.channels[2].name == "Agent 通道"
        assert window.experiment.text() == "Agent 实验"
        assert window.rate.currentData() == 2000
        assert not window.engine.connection_error
        assert window.engine.snapshot()[1].shape[1] == 2
        path = tmp_path / "agent-record"
        core.request("start_recording", path=str(path), channels=updated["channels"], experiment="Agent 记录")
        until(lambda: window.engine.recording)
        window.refresh()
        assert window.latest_recording == path
        assert window.experiment.text() == "Agent 记录"
        core.request("stop_recording")
        core.request("stop")
        same_profile = core.request("poll")["profile"]
        previous_start = window.engine.acquisition_started_at_utc
        window._preview_invalidated = True
        core.request("start", profile=same_profile)
        until(lambda: window.engine.acquisition_started_at_utc != previous_start)
        window.refresh()
        assert not window._preview_invalidated
        next_path = tmp_path / "agent-record-2"
        core.request("start_recording", path=str(next_path), channels=same_profile["channels"],
                     experiment=same_profile["experiment"], recording_options=same_profile["recording_options"])
        until(lambda: window.engine.last_recording == next_path)
        window.refresh()
        assert window.latest_recording == next_path
        core.request("stop_recording")
        core.request("stop")
    finally:
        window.engine.detach()
        window.engine = None
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_stale_credentials_do_not_change_another_live_recording(core, tmp_path):
    path = tmp_path / "active-recording"
    core.request("start", profile=profile(), recording_path=str(path))
    until(lambda: core.request("poll")["recorded_scans"] >= 100)
    before = core.request("poll")
    endpoint = tmp_path / "stale.json"
    endpoint.write_text(json.dumps({**core.address, "token": "old-credential"}), encoding="utf-8")
    alternate, state = CoreClient.connect_or_start(endpoint)
    try:
        assert alternate.address["port"] != core.address["port"]
        assert not state["running"] and not state["recording"]
        reconnected, current = CoreClient.connect_or_start(core.endpoint)
        assert reconnected.address == core.address
        assert current["running"] and current["recording"]
        assert current["last_recording"] == before["last_recording"]
        assert current["recorded_scans"] >= before["recorded_scans"]
        assert current["output"] == before["output"]
    finally:
        alternate.request("shutdown")


@pytest.mark.parametrize("waveform", ["dc", "sine", "square", "triangle", "sawtooth", "step", "pulse", "chirp"])
def test_rust_waveforms_and_recording_match_python(core, tmp_path, waveform):
    specs = tuple(SignalSpec(waveform=waveform, amplitude_v=.7 + i * .2, offset_v=.17,
                            phase_deg=-33 + i * 21, frequency_hz=13.7, start_s=.03,
                            duration_s=.075, end_frequency_hz=37, drift_v_per_s=.01) for i in range(4))
    settings = SimulationSettings(channels=specs)
    p = profile(settings, selected=(0, 2, 3), ranges=(10, 10, 1, 10))
    path = tmp_path / waveform
    core.request("start", profile=p, recording_path=str(path))
    until(lambda: core.request("poll")["recorded_scans"] >= 300)
    final = core.request("stop")
    result = load_session(path)
    expected = generate_signals(settings, 0, result.sample_count, result.sample_rate)[:, [0, 2, 3]]
    expected = decode_adc(encode_adc(expected, (10, 1, 10)), (10, 1, 10))
    np.testing.assert_allclose(result.read(), expected, rtol=0, atol=20 / 2**24)
    np.testing.assert_allclose(convert_volts(result.read(), result.channels), convert_volts(expected, result.channels),
                               rtol=0, atol=2e-4)
    assert result.sample_count == final["recorded_scans"] == final["total_scans"]
    assert result.metadata["acquisition_config"]["record_start_scan"] == 0
    assert result.metadata["finalized"] and result.metadata["termination_reason"] == "user_stop"
    assert result.metadata["source"] == "simulation"
    assert [c.index for c in result.channels] == [0, 2, 3]


def test_ac_model_matches_python_transport(core, tmp_path):
    from labdaq.simulator import SimulatedTransport
    from labdaq.transport import DaqSession
    signals = SimulationSettings(channels=(SignalSpec(offset_v=.4),) * 4)
    p = profile(signals, coupling=("AC", "DC", "AC", "DC"))
    path = tmp_path / "ac"
    core.request("start", profile=p, recording_path=str(path))
    until(lambda: core.request("poll")["total_scans"] >= 300)
    core.request("stop")
    recording = load_session(path)
    config = AcquisitionConfig(**p["config"])
    with DaqSession(SimulatedTransport(signal_settings=signals)) as reference:
        reference.configure(config)
        reference.start()
        expected = reference.read(recording.sample_count)
    np.testing.assert_allclose(recording.read(), expected, rtol=0, atol=20 / 2**24)


def test_noise_is_repeatable_across_selected_channels(core, tmp_path):
    signals = SimulationSettings(channels=(SignalSpec(waveform="noise", noise_rms_v=.3),) * 4, seed=77)
    results = []
    for selected in ((0, 1, 2, 3), (0, 2, 3)):
        path = tmp_path / f"noise-{len(selected)}"
        core.request("start", profile=profile(signals, selected=selected, rate=100000), recording_path=str(path))
        until(lambda: core.request("poll")["recorded_scans"] >= 100000)
        core.request("stop")
        results.append(load_session(path).read(count=100000))
    np.testing.assert_array_equal(results[0][:, [0, 2, 3]], results[1])
    assert np.max(np.abs(np.mean(results[0], axis=0))) < .003
    np.testing.assert_allclose(np.std(results[0], axis=0), .3, atol=.003, rtol=0)
    assert abs(np.corrcoef(results[0][:, :2], rowvar=False)[0, 1]) < .02


def test_mid_preview_record_no_overwrite_and_invalid_request(core, tmp_path):
    p = profile(selected=(1, 3))
    core.request("start", profile=p)
    until(lambda: core.request("poll")["total_scans"] >= 200)
    path = tmp_path / "group"
    core.request("start_recording", path=str(path), channels=p["channels"], experiment="组一")
    with pytest.raises(RuntimeError, match="先结束"):
        core.request("shutdown")
    with pytest.raises(RuntimeError, match="不能修改"):
        core.request("set_channels", channels=p["channels"])
    until(lambda: core.request("poll")["recorded_scans"] >= 200)
    core.request("stop_recording")
    first = load_session(path)
    first_bytes = (path / "raw.f64").read_bytes()
    assert first.metadata["acquisition_config"]["record_start_scan"] >= 200
    assert core.request("poll")["running"]
    with pytest.raises(RuntimeError):
        core.request("start_recording", path=str(path), channels=p["channels"], experiment="覆盖")
    assert (path / "raw.f64").read_bytes() == first_bytes
    second = tmp_path / "group2"
    core.request("start_recording", path=str(second), channels=p["channels"], experiment="组二")
    until(lambda: core.request("poll")["recorded_scans"] >= 100)
    core.request("stop")
    assert load_session(second).metadata["experiment"] == "组二"
    bad = profile(selected=(0, 2))
    bad["config"]["channels"] = [2, 0]
    with pytest.raises(RuntimeError, match="配置无效"):
        core.request("start", profile=bad)
    assert not core.request("poll")["running"]


def test_high_rate_ring_is_bounded_and_record_keeps_every_scan(core, tmp_path):
    path = tmp_path / "high-rate"
    core.request("start", profile=profile(rate=105469), recording_path=str(path))
    until(lambda: core.request("poll")["total_scans"] >= 240000)
    state = core.request("poll", seconds=12)
    assert state["capacity"] == state["buffered_scans"] == 200000
    assert len(state["raw"]) == 200000 * 4
    final = core.request("stop")
    result = load_session(path)
    assert result.sample_count == final["total_scans"] > 200000
    np.testing.assert_allclose(result.read(state["snapshot_start"], 200000),
                               np.asarray(state["raw"]).reshape(-1, 4), atol=0, rtol=0)


def test_incremental_preview_keeps_exact_samples_through_ring_rollover(core):
    p = profile(rate=105469, selected=(0, 2, 3))
    state = core.request("start", profile=p)
    engine = RemoteEngine(core, state)
    try:
        until(lambda: engine.total_scans >= 240000)
        engine.stop()
        until(lambda: engine._snapshot_end == engine.total_scans)
        full = core.request("poll", seconds=8)
        times, raw = engine.snapshot()
        assert raw.shape == (200000, 3)
        np.testing.assert_array_equal(raw, np.asarray(full["raw"]).reshape(-1, 3))
        np.testing.assert_allclose(times, np.arange(full["snapshot_start"], full["total_scans"]) / engine.sample_rate,
                                   rtol=0, atol=0)
        # A stopped buffer sends no repeated samples, while the GUI keeps its cache.
        unchanged = core.request("poll", seconds=8, after_scan=full["total_scans"])
        assert unchanged["raw"] == []
        np.testing.assert_array_equal(engine.snapshot()[1], raw)
        start = full["total_scans"] - 37
        delta = core.request("poll", seconds=8, after_scan=start)
        assert delta["snapshot_start"] == start
        np.testing.assert_array_equal(np.asarray(delta["raw"]).reshape(-1, 3), raw[-37:])
        # Missed history is bounded by the requested window; a reset cursor gets a full window.
        bounded = core.request("poll", seconds=.01, after_scan=0)
        count = round(.01 * engine.sample_rate)
        assert bounded["snapshot_start"] == full["total_scans"] - count
        np.testing.assert_array_equal(np.asarray(bounded["raw"]).reshape(-1, 3), raw[-count:])
        reset = core.request("poll", seconds=8, after_scan=full["total_scans"] + 1)
        np.testing.assert_array_equal(np.asarray(reset["raw"]).reshape(-1, 3), raw)
    finally:
        engine.detach()


def test_stopped_display_window_can_expand_and_shrink_without_new_samples(core):
    state = core.request("start", profile=profile(rate=1000))
    engine = RemoteEngine(core, state)
    engine.display_seconds = .1
    try:
        until(lambda: engine.total_scans >= 700)
        engine.stop()
        until(lambda: engine._snapshot_end == engine.total_scans)
        count = engine.total_scans
        revision = engine.display_revision
        engine.display_seconds = .5
        until(lambda: len(engine.snapshot()[0]) == round(.5 * engine.sample_rate))
        assert engine.total_scans == count and engine.display_revision > revision
        full = np.asarray(core.request("poll", seconds=.5)["raw"]).reshape(-1, 4)
        np.testing.assert_array_equal(engine.snapshot()[1], full)
        engine.display_seconds = .2
        until(lambda: len(engine.snapshot()[0]) == round(.2 * engine.sample_rate))
        np.testing.assert_array_equal(engine.snapshot()[1], full[-round(.2 * engine.sample_rate):])
    finally:
        engine.detach()


@pytest.mark.parametrize("during_preview", [False, True])
def test_fixed_duration_exact_frames_and_next_group(core, tmp_path, during_preview):
    p = profile(selected=(0, 3))
    options = {"duration_seconds": .137, "specimen": "试样 A", "condition": "室温",
               "notes": "正弦模拟 · 定长", "group_number": 1}
    p["channels"][3].update(point_number=12, direction="Z-", engineering_offset=1.25)
    first = tmp_path / "first"
    if during_preview:
        core.request("start", profile=p)
        until(lambda: core.request("poll")["total_scans"] >= 200)
        state = core.request("start_recording", path=str(first), channels=p["channels"],
                             experiment=p["experiment"], recording_options=options)
    else:
        p["recording_options"] = options
        state = core.request("start", profile=p, recording_path=str(first))
    target = max(1, int(np.floor(state["profile"]["actual_rate"] * .137 + .5)))
    assert state["recording_target_scans"] == target
    until(lambda: not core.request("poll")["recording"])
    final = core.request("poll")
    assert final["running"] and final["recording_remaining_scans"] == 0
    assert final["termination_reason"] == "duration_complete"
    result = load_session(first)
    assert result.sample_count == result.metadata["target_sample_count"] == target
    assert result.metadata["recording_options"] == options
    assert result.channels[1].point_number == 12
    assert result.channels[1].direction == "Z-"
    assert result.channels[1].engineering_offset == 1.25
    start = result.metadata["acquisition_config"]["record_start_scan"]
    assert (start > 0) == during_preview
    expected = generate_signals(SimulationSettings(), start, target, result.sample_rate)[:, [0, 3]]
    np.testing.assert_allclose(result.read(), expected, rtol=0, atol=20 / 2**24)
    second = tmp_path / "second"
    state = core.request("start_recording", path=str(second), channels=p["channels"], experiment="下一组")
    assert state["recording_target_scans"] is None and state["termination_reason"] == "user_stop"
    until(lambda: core.request("poll")["recorded_scans"] > target)
    core.request("stop_recording")
    assert load_session(second).metadata["termination_reason"] == "user_stop"


def test_pause_reconnect_duration_and_export_preserve_source_gaps(core, tmp_path):
    p = profile(selected=(0, 3))
    p["recording_options"] = {"duration_seconds": .4}
    path = tmp_path / "paused"
    started = core.request("start", profile=p, recording_path=str(path))
    until(lambda: core.request("poll")["recorded_scans"] >= 100)
    paused = core.request("pause_recording")
    captured, acquired = paused["recorded_scans"], paused["total_scans"]
    until(lambda: core.request("poll")["total_scans"] >= acquired + 1000)
    current = core.request("poll")
    assert current["running"] and current["recording"] and current["recording_paused"]
    assert current["recorded_scans"] == captured
    assert current["recording_remaining_scans"] == paused["recording_remaining_scans"]
    assert (path / "raw.f64").stat().st_size == captured * 2 * 8
    assert load_session(path).metadata["recording_paused"]
    rejoined = RemoteEngine.reconnect(core.endpoint)
    try:
        assert rejoined.recording and rejoined.recording_paused
        assert rejoined.acquisition_started_at_utc == started["acquisition_started_at_utc"]
        rejoined.resume_recording()
        until(lambda: not core.request("poll")["recording"])
        final = core.request("poll")
        assert final["running"] and not final["recording_paused"]
        assert final["termination_reason"] == "duration_complete"
        session = load_session(path)
        assert session.sample_count == 800
        assert abs(session.duration - .4) <= .5 / session.sample_rate
        assert session.elapsed_duration > .8
        assert len(session.recording_segments) == 2
        scans = session.sample_indices()
        expected = generate_signals(SimulationSettings(), 0, int(scans[-1]) + 1, session.sample_rate)[scans][:, [0, 3]]
        expected = decode_adc(encode_adc(expected, (10, 10)), (10, 10))
        np.testing.assert_allclose(session.read(), expected, rtol=0, atol=20 / 2**24)
        destination = export_csv(session, tmp_path / "paused.csv", time_format="samples", values="raw")
        exported = np.loadtxt(destination, delimiter=",", skiprows=2, encoding="utf-8-sig")
        np.testing.assert_array_equal(exported[:, 0], scans)
        second = tmp_path / "manual-paused"
        core.request("start_recording", path=str(second), channels=p["channels"], experiment="手动停止暂停组")
        until(lambda: core.request("poll")["recorded_scans"] >= 100)
        core.request("pause_recording")
        final = core.request("stop")
        assert not final["running"] and not final["recording"] and not final["recording_paused"]
        assert load_session(second).metadata["termination_reason"] == "user_stop"
    finally:
        rejoined.detach()


@pytest.mark.parametrize("duration", [None, .9])
def test_killed_qt_process_keeps_recording_and_new_gui_rejoins(tmp_path, duration):
    if not CORE_EXE.exists():
        pytest.skip("Build the Rust core first")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    endpoint, ready = tmp_path / "endpoint.json", tmp_path / "ready.json"
    source = '''
import json, os, sys
from pathlib import Path
from dataclasses import replace
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtWidgets as W
from labdaq.gui import MainWindow, configure_application
from labdaq.core_client import CoreClient
from labdaq.signals import SimulationSettings, SignalSpec
endpoint, ready = map(Path, sys.argv[1:3])
CoreClient.connect_or_start(endpoint, port=0)
app = W.QApplication([])
configure_application(app)
w = MainWindow(core_endpoint=endpoint)
w.recording_root = endpoint.parent
w.scenario.setCurrentIndex(w.scenario.findData("rust"))
w.active_channels = (0, 2, 3)
w.channels[2] = replace(w.channels[2], name="重连通道", zero_volts=.123, inverted=True)
w.signal_settings = SimulationSettings(channels=(SignalSpec(frequency_hz=13.7),) * 4)
w.experiment.setText("GUI崩溃续录")
w._set_recording_options({"duration_seconds": json.loads(sys.argv[3]),
                          "condition": "重连验证", "group_number": 7})
w.toggle_record()
assert w.engine and w.engine.recording, w.notice.text()
w.show()
ready.write_text(json.dumps({"path": str(w.latest_recording)}), encoding="utf-8")
app.exec()
'''
    process = subprocess.Popen([sys.executable, "-c", source, str(endpoint), str(ready), json.dumps(duration)], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client = None
    window = None
    try:
        until(ready.exists, timeout=15)
        path = Path(json.loads(ready.read_text())["path"])
        client = CoreClient(endpoint)
        before = client.request("poll")
        process.kill()  # TerminateProcess on Windows; closeEvent cannot run.
        process.wait(5)
        until(lambda: client.request("poll")["recorded_scans"] >= before["recorded_scans"] + 600)
        assert (path / "raw.f64").stat().st_size > before["recorded_scans"] * 3 * 8
        if duration is not None:
            until(lambda: not client.request("poll")["recording"])
        from PySide6 import QtCore, QtWidgets as W
        from labdaq.gui import MainWindow, configure_application
        app = W.QApplication.instance() or W.QApplication([])
        configure_application(app)
        window = MainWindow(core_endpoint=endpoint)
        assert window.engine.recording == (duration is None) and window.latest_recording == path
        assert window.recording_duration_seconds == duration
        assert window.condition.text() == "重连验证"
        assert window.recording_sequence.value() == (7 if duration is None else 8)
        assert window.active_channels == (0, 2, 3)
        assert window.channels[2].name == "重连通道" and window.channels[2].zero_volts == .123
        assert window.channels[2].inverted and window.experiment.text() == "GUI崩溃续录"
        assert window.scenario.currentData() == "rust" and not window.rate.isEnabled()
        window.pause_plot.setChecked(True)
        window.stop_acquisition()
        recording = load_session(path)
        settings = SimulationSettings(channels=(SignalSpec(frequency_hz=13.7),) * 4)
        truth = generate_signals(settings, 0, recording.sample_count, recording.sample_rate)[:, [0, 2, 3]]
        truth = decode_adc(encode_adc(truth, (10, 10, 10)), (10, 10, 10))
        np.testing.assert_allclose(recording.read(), truth, rtol=0, atol=20 / 2**24)
        assert recording.sample_count > before["recorded_scans"] + 600
        assert recording.metadata["termination_reason"] == ("duration_complete" if duration is not None else "user_stop")
        if duration is not None:
            assert recording.sample_count == int(np.floor(recording.sample_rate * duration + .5))
        assert list(tmp_path.glob("*/*/metadata.json")) == [path / "metadata.json"]
        assert path == tmp_path / "GUI崩溃续录" / "重连验证-7"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)
        if process.stderr:
            errors = process.stderr.read().decode("utf-8", errors="replace")
            process.stderr.close()
            if not ready.exists():
                print(errors)
        if client is None and endpoint.exists():
            client = CoreClient(endpoint)
        if client:
            try:
                client.request("stop")
                client.request("shutdown")
            except (OSError, ValueError):
                pass
        if window:
            window.engine.detach()
            window.engine = None
            window.close()
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()


def test_gui_zero_pause_and_normal_close_preserve_core_session(core, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets as W
    from labdaq.gui import MainWindow, configure_application
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    first = MainWindow(core_endpoint=core.endpoint)
    second = None
    try:
        first.scenario.setCurrentIndex(first.scenario.findData("rust"))
        first.recording_root = tmp_path
        first.signal_settings = SimulationSettings(channels=(SignalSpec(waveform="dc", amplitude_v=.4),) * 4)
        first.start_preview()
        until(lambda: len(first.engine.snapshot()[0]) >= 200)
        first.zero_channels()
        assert first.channels[0].zero_volts == pytest.approx(.4, abs=20 / 2**24)
        first.toggle_record()
        path = first.latest_recording
        first.pause_plot.setChecked(True)
        initial = core.request("poll")["recorded_scans"]
        first.close()
        until(lambda: core.request("poll")["recorded_scans"] >= initial + 200)
        second = MainWindow(core_endpoint=core.endpoint)
        assert second.engine.recording and second.latest_recording == path
        assert second.channels[0].zero_volts == first.channels[0].zero_volts
        second.stop_acquisition()
        result = load_session(path)
        np.testing.assert_allclose(result.read(), .4, rtol=0, atol=20 / 2**24)
        np.testing.assert_allclose(convert_volts(result.read(), result.channels), 0, rtol=0, atol=1e-12)
    finally:
        for window in (first, second):
            if window:
                if window.engine:
                    window.engine.detach()
                window.engine = None
                window.close()
                window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_usb_profile_load_is_offline_and_preserves_physical_channel(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets as W
    from labdaq.gui import MainWindow, configure_application
    from labdaq.core_client import RemoteEngine
    def no_hardware(*args, **kwargs):
        raise AssertionError("Loading a saved configuration must not connect hardware")
    monkeypatch.setattr(RemoteEngine, "start_new", no_hardware)
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        window.scenario.setCurrentIndex(window.scenario.findData("usb"))
        window.active_channels = (3,)
        window.rate.setCurrentIndex(window.rate.findData(1000.0))
        path = tmp_path / "usb.json"
        window.save_profile_to(path)
        window.scenario.setCurrentIndex(window.scenario.findData("rust"))
        window.load_profile_from(path)
        assert window.scenario.currentData() == "usb" and window.active_channels == (3,)
        assert window.engine is None and not window.input_button.isVisible()
        assert "DT9837C" in window.mode_badge.text() and "未连接" in window.mode_badge.text()
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
