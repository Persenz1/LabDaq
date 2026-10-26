# LabDAQ 本机 Agent API

Python 接口与 JSON 命令行复用界面使用的 Rust 核心，由核心独占 DT9837C 并调用原厂 Open Layers C API。安装包内置 Python 运行环境和原厂 OEM 安装程序，安装版命令行不需要配置开发环境、DLL 路径或另外安装原厂 Python/.NET SDK。Python 接口需能导入项目源码及其 Python 依赖，不需要 Qt 界面。

## 安装版命令行

默认安装目录为 `%ProgramFiles%\LabDAQ`，包含 `LabDAQ.exe`（界面）和 `LabDAQ-api.exe`（控制台接口），共用内部运行环境。以下按默认安装位置举例，目录更改后使用实际路径。当前用户配置与运行状态位于 `%LOCALAPPDATA%\LabDAQ`，默认记录根目录为 Windows“文档”目录下的 `LabDAQ\recordings`；显式传入的记录路径按调用参数保存，不会重定向到默认根目录。

```powershell
$daq = Join-Path $env:ProgramFiles 'LabDAQ\LabDAQ-api.exe'
& $daq status
& $daq devices
& $daq connect
& $daq start-acquisition --sample-rate 5000 --channels 0 1 2 3
& $daq read --seconds 0.1
& $daq start-recording 'D:\实验记录\测试-1' --duration-seconds 10 --experiment '接口测试'
& $daq status
```

`start-recording` 立即返回，不等待记录完成。定长完成后 `status` 的 `recording` 为 false；还应检查 `termination_reason` 为 `duration_complete` 且 `error` 为空，确认正常完成。AI 仍继续预览。完整结束与导出：

```powershell
& $daq stop-acquisition
& $daq export 'D:\实验记录\测试-1' 'D:\实验记录\测试-1.csv' --values both
```

若是手动记录，先执行 `stop-recording`。使用 AO 时显式启动，结束后显式停止：

```powershell
& $daq start-output --waveform sine --amplitude-v 0.2 --offset-v 0 --frequency-hz 10 --sample-rate 48000
& $daq stop-output
& $daq disconnect
```

`--help` 查看全部参数。实际硬件操作需使用者安排好接线及 IEPE 设置；AO 与 AI 独立启停。不带参数的 `start-output` 使用 10 Hz 正弦、0.2 V 峰值、0 V 偏置、48 kS/s 更新率；波形支持 `dc`、`sine`、`square`、`triangle`，更新率支持 10000、20000、48000、96000 S/s。

## 命令与结果

| 命令 | 行为 |
| --- | --- |
| `status` | 查询状态，核心未运行时返回 `core_running: false`，不启动核心或采集 |
| `devices` | 列举原厂可见设备，需要时启动默认核心，不开始采集 |
| `connect` | 连接后待机；已有连接或采集时返回现状 |
| `start-acquisition` | 开始 AI，支持采样率及物理通道编号；已有采集时拒绝覆盖 |
| `read --seconds N` | 返回最近 N 秒的有界原始电压快照 |
| `start-recording PATH` | 在已有采集中建立原生记录组，已有目录不覆盖 |
| `pause-recording` / `resume-recording` | 暂停/继续写盘，AI 继续，保留时间缺口 |
| `stop-recording` | 结束当前组，继续预览 |
| `stop-acquisition` | 停止 AI 并保存活动记录，AO 不变 |
| `start-output` / `stop-output` | 启动 AO / 停止并下发 0 V |
| `disconnect` | 停止采集及 AO 并释放设备会话 |
| `export RECORDING OUTPUT` | 分块导出完整记录为 CSV/TXT，已有输出文件不覆盖 |

帮助为普通文本；操作结果向 stdout 写一条 UTF-8 JSON。成功退出码为 0，失败为 1：

```json
{"ok": true, "result": {"core_running": false}}
```

```json
{"ok": false, "error": "采集核心未运行，请先连接设备或开始采集"}
```

状态返回当前配置、是否采集/记录/暂停、扫描计数、记录路径、结束原因和 AO 状态，不返回连接令牌。`devices` 每项的字段为 `vendor_name`、`name`、`driver`、`model` 和 `backend`；其中 `backend` 为 `dt-open-layers-c-abi`，名称及型号来自原厂枚举结果。

`read` 返回 `sample_rate`、`start_scan`、`channel_indices` 和二维 `volts`（样本×通道）。时间按 `(start_scan + 行号) / sample_rate` 计算；通道号是 AI0–AI3 的 0–3，不是名称中的 1–4。窗口必须大于 0 且不超过 60 秒，另受每通道 200,000 帧核心缓存限制。它是显示快照，不能用多次调用拼成无损数据流；完整数据应通过原生记录和导出获取。

## Python SDK

在源码项目目录和现有 Python 环境中：

```python
from labdaq.api import LabDAQ

daq = LabDAQ()
print(daq.status())
daq.connect()
daq.start_acquisition(sample_rate=5000, channels=(0, 1, 2, 3))
snapshot = daq.read(seconds=0.1)
daq.start_recording(r'D:\实验记录\测试-2', duration_seconds=10, experiment='接口测试')
# 稍后查询状态，确认定长记录正常完成，再停止采集和导出。
```

SDK 方法与表中的命令对应，方法名用下划线，例如 `stop_acquisition()`、`stop_output()`。Python 额外可在 `start_acquisition` 传入四路的 `channel_settings`、`ranges`、`coupling` 和 `iepe`；`ranges`、`coupling` 和 `iepe` 均按 AI0–AI3 提供四项，不能只提供启用通道的值；`channel_settings` 使用四个 `ChannelSettings` 或完整通道字典。

新会话默认通道1–4、V、灵敏度 0、±10 V、DC、IEPE 关闭。V 模式不使用灵敏度，显示和工程值仍应用零点、极性及工程偏移；原始电压记录不变。已有原厂会话未指定的配置会沿用当前值。Python 导出为 `daq.export(recording_path, output_path, values='both')`，也可选 `raw` 或 `engineering`。

源码 SDK 默认使用源码 `.runtime/core.json`；安装版 CLI 默认使用 `%LOCALAPPDATA%\LabDAQ\.runtime\core.json`。普通 Python 中的 `LabDAQ()` 不会自动发现安装实例。从外部 Python 接回安装实例时，先用安装版 CLI 或 GUI 启动核心，再明确指定端点：

```python
import os
from pathlib import Path
from labdaq.api import LabDAQ

daq = LabDAQ(Path(os.environ['LOCALAPPDATA']) / 'LabDAQ' / '.runtime' / 'core.json')
print(daq.status())
```

指定其他端点仅接回现有核心，不会在该位置启动开发核心。源码命令行入口为 `python -m labdaq.api`；显式端点参数放在子命令前，例如 `python -m labdaq.api --endpoint PATH status`。

## 与界面协作

同一运行模式和端点共用一个核心。Windows 命名互斥锁用于避免重复启动核心；控制命令由核心按接收顺序处理并检查状态。API 改变采集配置或记录组后，已打开的界面会更新实际状态。不同客户端控制的是同一设备，实验步骤应由一个控制端安排。

Python 客户端没有退出时自动停止的逻辑，CLI 进程结束也不会停止采集、记录或 AO。控制调用超时会返回执行结果未知，不自动重发；先查询 `status` 再决定后续动作。接口只在本机使用，端点凭据由客户端内部读取。建立 TCP 连接的等待上限为 3 秒；连接和启动采集请求为 30 秒，其他请求为 5 秒。

[返回用户指南](user-guide.md)
