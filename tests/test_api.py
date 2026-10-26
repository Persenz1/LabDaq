"""Public API and JSON CLI against an isolated software core, without hardware."""
from copy import deepcopy
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import numpy as np
import pytest

from labdaq.api import LabDAQ
from labdaq.core_client import CORE_EXE, CoreClient
from labdaq.recording import load_session


def until(function):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError("Timed out waiting for the isolated API core")


@pytest.fixture
def api(tmp_path, monkeypatch):
    if not CORE_EXE.exists():
        pytest.skip("Build the Rust core first")
    daq = LabDAQ(tmp_path / "endpoint.json")
    CoreClient.connect_or_start(daq.endpoint)
    software_profile = daq._default_profile()
    software_profile["source"] = "simulation"
    monkeypatch.setattr(daq, "_default_profile", lambda: deepcopy(software_profile))
    yield daq
    existing = CoreClient.existing(daq.endpoint)
    if existing:
        client = existing[0]
        client.request("stop")
        client.request("disconnect")
        client.request("shutdown")


def cli(endpoint, *arguments):
    return subprocess.run([sys.executable, "-m", "labdaq.api", "--endpoint", str(endpoint), *arguments],
                          capture_output=True, text=True, encoding="utf-8", timeout=15)


def test_status_is_read_only_and_cli_errors_are_json(tmp_path):
    endpoint = tmp_path / "absent.json"
    assert LabDAQ(endpoint).status() == {"core_running": False}
    result = cli(endpoint, "status")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True, "result": {"core_running": False}}
    assert not endpoint.exists()
    for arguments in (("unknown",), ("start-recording",), ("start-output", "--waveform", "noise")):
        result = cli(endpoint, *arguments)
        assert result.returncode != 0
        assert json.loads(result.stdout)["ok"] is False
    result = subprocess.run([sys.executable, "-c", "import sys; import labdaq.api; print('PySide6' in sys.modules)"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stdout.strip() == "False"


def test_default_api_profile_matches_voltage_defaults():
    profile = LabDAQ._default_profile()
    assert profile["source"] == "vendor"
    assert [channel["name"] for channel in profile["channels"]] == [f"通道{i}" for i in range(1, 5)]
    assert all(channel["unit"] == "V" and channel["sensitivity_mv_per_unit"] == 0
               for channel in profile["channels"])
    assert profile["config"]["ranges"] == (10, 10, 10, 10)
    assert profile["config"]["coupling"] == ("DC",) * 4


def test_explicit_external_endpoint_never_starts_a_different_core(tmp_path, monkeypatch):
    start = Mock(side_effect=AssertionError("Must not start a source core at an external endpoint"))
    monkeypatch.setattr(CoreClient, "connect_or_start", start)
    with pytest.raises(RuntimeError, match="核心未运行"):
        LabDAQ(tmp_path / "installed-endpoint.json").connect()
    start.assert_not_called()


def test_api_record_pause_read_export_and_ao_keep_one_core(api, tmp_path):
    started = api.start_acquisition(sample_rate=1000, channels=(0, 2))
    until(lambda: api.status()["total_scans"] >= 100)
    state = api.connect()
    assert state["pid"] == started["pid"] and state["running"]
    api.start_output(amplitude_v=.1)
    path = tmp_path / "record"
    api.start_recording(path, experiment="Agent 测试")
    until(lambda: api.status()["recorded_scans"] >= 100)
    paused = api.pause_recording()
    until(lambda: api.status()["total_scans"] >= paused["total_scans"] + 100)
    assert api.status()["recorded_scans"] == paused["recorded_scans"]
    api.resume_recording()
    until(lambda: api.status()["recorded_scans"] > paused["recorded_scans"] + 100)
    snapshot = api.read(.1)
    assert snapshot["channel_indices"] == [0, 2]
    assert 0 < len(snapshot["volts"]) <= 101
    assert np.asarray(snapshot["volts"]).shape[1] == 2
    assert np.isfinite(snapshot["volts"]).all()
    result = cli(api.endpoint, "read", "--seconds", ".01")
    assert result.returncode == 0
    assert "token" not in json.loads(result.stdout)["result"]
    api.stop_recording()
    stopped = api.stop_acquisition()
    assert stopped["output"]["running"] and not stopped["running"]
    api.stop_output()
    session = load_session(path)
    assert session.metadata["finalized"]
    assert session.metadata["experiment"] == "Agent 测试"
    assert len(session.recording_segments) == 2
    output = Path(api.export(path, tmp_path / "export.csv"))
    table = np.loadtxt(output, delimiter=",", skiprows=2, encoding="utf-8-sig")
    np.testing.assert_array_equal(table[:, 1:3], session.read())
    np.testing.assert_array_equal(table[:, 3:5], session.read())
    np.testing.assert_allclose(table[:, 0], session.sample_indices() / session.sample_rate, rtol=0, atol=1e-15)
    with pytest.raises(FileExistsError):
        api.export(path, output)


def test_api_duration_is_exact_and_existing_session_is_not_restarted(api, tmp_path):
    api.start_acquisition(sample_rate=2000)
    with pytest.raises(RuntimeError, match="正在采集"):
        api.start_acquisition(sample_rate=1000)
    path = tmp_path / "fixed"
    api.start_recording(path, duration_seconds=.137)
    until(lambda: not api.status()["recording"])
    session = load_session(path)
    assert session.sample_count == math.floor(session.sample_rate * .137 + .5)
    assert session.metadata["termination_reason"] == "duration_complete"
    assert api.status()["running"]


def test_control_timeout_is_unknown_and_never_retried(monkeypatch, tmp_path):
    client = Mock()
    client.request.side_effect = TimeoutError("timed out")
    monkeypatch.setattr(CoreClient, "existing", lambda endpoint: (client, {}))
    with pytest.raises(RuntimeError, match="执行结果未知"):
        LabDAQ(tmp_path / "endpoint.json").stop_acquisition()
    client.request.assert_called_once_with("stop")


@pytest.mark.parametrize("seconds", [0, -1, 61, float("nan"), float("inf")])
def test_read_rejects_invalid_windows(tmp_path, seconds):
    with pytest.raises(ValueError, match="读取窗口"):
        LabDAQ(tmp_path / "unused.json").read(seconds)
