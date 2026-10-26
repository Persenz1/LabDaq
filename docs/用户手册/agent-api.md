# LabDAQ 本机 Agent API 与自动化控制指南

[项目首页](../../README.md) / [文档中心](../README.md) / [用户手册](user-guide.md) / Agent API 指南

---

LabDAQ 面向自动化测试台架、无人值守监测系统以及大模型（LLM）Agent 提供了两套轻量级、无图形界面的本机控制接口：
1. **控制台命令行工具（CLI）**：`LabDAQ-api.exe`（安装版）或 `python -m labdaq.api`（源码环境）。
2. **Python 编程接口（SDK）**：`labdaq.api.LabDAQ` 类。

接口复用与桌面 GUI 相同的 Rust 采集核心（`labdaq-core.exe`），实现跨进程独占 DT9837C 仪器与原厂 Open Layers 驱动交互。客户端进程退出**完全不会中断底层的连续采集、流盘写盘或模拟输出**。

---

## 1. 架构原理与端点机制

### 1.1 无状态客户端与独立核心

LabDAQ 采用客户端-服务端（Client-Server）解耦设计：
* **核心服务（Server）**：Rust 采集核心运行在后台，维护 200,000 帧环形缓冲区、直接执行 USB/驱动 Bulk I/O、负责 `raw.f64` 文件的无锁原子写入。
* **Agent 客户端（Client）**：Python SDK 或 CLI 作为轻量客户端，通过 Localhost TCP 动态端口与核心交换换行定界的 JSON 数据包。
* **端点发现（Endpoint Rendezvous）**：
  * **安装版环境**：默认端点凭据文件位于 `%LOCALAPPDATA%\LabDAQ\.runtime\core.json`。
  * **源码开发环境**：默认端点凭据文件位于 `<项目根目录>/.runtime/core.json`。
  * `core.json` 包含动态 TCP 端口、随机通信 Token 与进程 PID。客户端在发起请求时自动解析凭据并完成认证。

```text
+------------------------------------+          +------------------------------------+
|        LabDAQ-api.exe (CLI)        |          |      Python SDK (labdaq.api)       |
+------------------------------------+          +------------------------------------+
                  \                                               /
                   \_______ 本地 TCP (127.0.0.1:<Port>) __________/
                                (Token 鉴权, JSON 协议)
                                           |
                                           v
                  +--------------------------------------------------+
                  |             Rust 核心 (labdaq-core.exe)          |
                  |  - 独占 Open Layers C-ABI 硬件会话               |
                  |  - 实时环形缓冲区 (200,000 帧)                   |
                  |  - 高速流盘写盘 (raw.f64 + metadata.json)        |
                  |  - 独立 DAC 模拟输出波形供料线程                 |
                  +--------------------------------------------------+
```

---

## 2. 命令行接口 (CLI Reference)

### 2.1 调用形式与通用协议规范

* **可执行程序路径**：
  * **安装版**：`%ProgramFiles%\LabDAQ\LabDAQ-api.exe`
  * **源码版**：`python -m labdaq.api`
* **退出码（Exit Codes）**：
  * `0`：命令成功执行。
  * `1`：执行失败或参数校验错误。
* **标准输出协议**：
  * 命令行所有输出均采用标准的单行 UTF-8 编码 JSON 字符串，便于外部脚本可靠解析。
  * **成功返回格式**：
    ```json
    {"ok": true, "result": { ... }}
    ```
  * **失败返回格式**：
    ```json
    {"ok": false, "error": "错误原因说明"}
    ```

### 2.2 CLI 命令速查表

| 指令 | 参数说明 | 功能描述 |
| :--- | :--- | :--- |
| `status` | 无 | 查询系统运行状态（核心是否运行、采集/记录/AO状态等） |
| `devices` | 无 | 枚举当前原厂驱动可见的物理数采硬件设备 |
| `connect` | 无 | 连接数采并初始化为待机就绪状态（不开始采集） |
| `start-acquisition` | `[--sample-rate RATE]`<br>`[--channels CH0 CH1 ...]` | 启动连续预览/采集，支持指定采样率与通道子集 |
| `read` | `[--seconds SECONDS]` | 截取最近 $N$ 秒的时域电压快照数组（默认 1.0 秒） |
| `start-recording` | `<PATH>`<br>`[--duration-seconds SEC]`<br>`[--experiment NAME]` | 在当前采集流上启动流式写盘，支持指定时长与实验名 |
| `pause-recording` | 无 | 暂停流盘写盘（硬件采集与示波继续保持） |
| `resume-recording`| 无 | 恢复流盘写盘，在元数据中追加新的记录分段 |
| `stop-recording` | 无 | 结束当前实验流盘并完成元数据封顶，保持连续采集 |
| `start-output` | `[--waveform {dc,sine,square,triangle}]`<br>`[--amplitude-v V]`<br>`[--offset-v V]`<br>`[--frequency-hz HZ]`<br>`[--sample-rate RATE]` | 启动 AO 模拟输出连续波形生成 |
| `stop-output` | 无 | 停止 AO 模拟输出，硬件电平安全归零至 0.0V |
| `stop-acquisition` | 无 | 停止连续采集，并收尾保存当前正在进行的记录组 |
| `disconnect` | 无 | 停止采集与 AO 输出，释放原厂驱动会话 |
| `export` | `<RECORDING_PATH>`<br>`<OUTPUT_PATH>`<br>`[--values {raw,engineering,both}]` | 将原生记录文件夹导出为 CSV 或 TXT 格式 |

### 2.3 CLI 命令与返回示例

#### 1. 查询系统状态 (`status`)
```powershell
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" status
```
* **核心未启动时的返回**：
  ```json
  {"ok": true, "result": {"core_running": false}}
  ```
* **核心运行时的典型返回**：
  ```json
  {
    "ok": true,
    "result": {
      "core_running": true,
      "pid": 12345,
      "device_connected": true,
      "running": true,
      "recording": true,
      "recording_paused": false,
      "total_scans": 125000,
      "recorded_scans": 125000,
      "recording_target_scans": null,
      "recording_remaining_scans": null,
      "last_recording": "C:\\Users\\persenz\\Documents\\LabDAQ\\recordings\\拉伸试验\\工况-01",
      "profile": {
        "actual_rate": 5000.0,
        "source": "vendor",
        "config": {
          "sample_rate": 5000.0,
          "channels": [0, 1, 2, 3]
        }
      },
      "output": {
        "running": true,
        "settings": {
          "waveform": "sine",
          "amplitude_v": 1.0,
          "offset_v": 0.0,
          "frequency_hz": 100.0,
          "sample_rate": 48000.0
        },
        "actual_rate": 48000.0,
        "zeroed": false,
        "completed": false
      }
    }
  }
  ```

#### 2. 枚举硬件设备 (`devices`)
```powershell
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" devices
```
* **返回示例**：
  ```json
  {
    "ok": true,
    "result": [
      {
        "name": "DT9837C(00)",
        "device_type": "DT9837C",
        "driver_type": "open-layers-vendor"
      }
    ]
  }
  ```

#### 3. 启动采集与时域读取 (`start-acquisition` / `read`)
```powershell
# 以 5000 S/s 启动通道 0 和 2
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" start-acquisition --sample-rate 5000 --channels 0 2

# 截取最近 0.05 秒快照
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" read --seconds 0.05
```
* **`read` 命令返回格式**：
  ```json
  {
    "ok": true,
    "result": {
      "sample_rate": 5000.0,
      "start_scan": 150000,
      "channel_indices": [0, 2],
      "volts": [
        [0.0123, -0.5432],
        [0.0125, -0.5430],
        ...
      ]
    }
  }
  ```
  > [!NOTE] 关于 `read` 快照机制
  > `read` 返回的是环形缓冲区内最近的有界时域采样数组，适用于 Agent 轮询监视与自适应状态判断。若需要无损完整的长时时域流，应通过 `start-recording` 进行原生流盘并调用 `export` 导出。

#### 4. 流盘记录与暂停恢复 (`start-recording` / `pause-recording`)
```powershell
# 启动定长 10 秒记录
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" start-recording "D:\测试记录\01" --duration-seconds 10.0 --experiment "疲劳试验"

# 暂停与恢复写盘
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" pause-recording
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" resume-recording

# 结束写盘
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" stop-recording
```

#### 5. 模拟信号输出 (`start-output` / `stop-output`)
```powershell
# 输出 48 kS/s、50 Hz、幅值 1.5 V、偏置 0.5 V 的正弦波
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" start-output --waveform sine --amplitude-v 1.5 --offset-v 0.5 --frequency-hz 50 --sample-rate 48000

# 停止输出并硬件安全归零至 0.0V
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" stop-output
```

#### 6. 数据格式导出 (`export`)
```powershell
& "C:\Program Files\LabDAQ\LabDAQ-api.exe" export "D:\测试记录\01" "D:\测试记录\01.csv" --values both
```

---

## 3. Python SDK 参考手册 (Python SDK Reference)

`labdaq.api` 模块提供了纯 Python 开发的控制类 `LabDAQ`。该模块无 Qt 运行依赖，可直接在标准 Python 脚本、Jupyter Notebook 或自动化框架中导入。

### 3.1 核心类初始化

```python
from labdaq.api import LabDAQ

# 1. 默认连接（自动识别安装版或源码版对应的端点）
daq = LabDAQ()

# 2. 显式指定端点连接
from pathlib import Path
daq = LabDAQ(endpoint=Path("C:/Users/persenz/AppData/Local/LabDAQ/.runtime/core.json"))
```

### 3.2 完整自动化工作流范例

以下代码展示了从设备连接、参数配置、连续采集、AO 波形激励、流盘记录到数据导出的完整工程闭环：

```python
import time
from labdaq.api import LabDAQ

def main():
    daq = LabDAQ()

    # 1. 查询系统状态
    print("1. 系统当前状态:", daq.status())

    # 2. 枚举硬件设备并连接
    devices = daq.devices()
    print("2. 发现设备列表:", devices)
    daq.connect()

    # 3. 启动四通道 5000 S/s 数据采集
    # 支持自定义每通道量程(10V/1V)、耦合(DC/AC)及 IEPE(True/False)
    daq.start_acquisition(
        sample_rate=5000.0,
        channels=[0, 1, 2, 3],
        ranges=[10.0, 10.0, 1.0, 10.0],
        coupling=["DC", "DC", "AC", "DC"],
        iepe=[False, False, True, False]
    )
    print("3. 数据采集已启动...")

    # 4. 同步启动 AO 信号发生器：48 kS/s, 10 Hz, 1.0 V 正弦激励
    daq.start_output(
        waveform="sine",
        amplitude_v=1.0,
        offset_v=0.0,
        frequency_hz=10.0,
        sample_rate=48000.0
    )
    print("4. 模拟信号输出中...")

    # 5. 实时截取 0.2 秒波形快照并打印
    time.sleep(0.5)
    snapshot = daq.read(seconds=0.2)
    print(f"5. 快照信息: 采样率={snapshot['sample_rate']} Hz, 样本数={len(snapshot['volts'])}")

    # 6. 启动定长 5 秒的数据流盘
    recording_dir = "D:/experiment_data/run_001"
    print("6. 启动数据流盘...")
    daq.start_recording(recording_dir, duration_seconds=5.0, experiment="动力学特性测试")

    # 7. 轮询等待流盘完成
    while True:
        status = daq.status()
        if not status.get("recording", False):
            print("7. 流盘已顺利完成，流盘写入帧数:", status.get("recorded_scans"), "累计采集帧数:", status.get("total_scans"))
            break
        time.sleep(0.5)

    # 8. 停止模拟输出并安全归零
    daq.stop_output()

    # 9. 停止数据采集
    daq.stop_acquisition()

    # 10. 导出数据为 CSV（同时保留原始电压与工程量）
    export_csv = "D:/experiment_data/run_001.csv"
    daq.export(recording_dir, export_csv, values="both")
    print("10. 数据已成功导出至:", export_csv)

    # 11. 释放原厂驱动会话
    daq.disconnect()
    print("11. 设备会话已断开，流程结束。")

if __name__ == "__main__":
    main()
```

---

## 4. 与桌面 GUI 的协作机制

1. **会话共享与并发串行化**：
   * 同一运行环境下的桌面 GUI 与 Agent API 共享唯一的后台 Rust 核心。
   * 在 Windows 平台上，所有对核心发起的并发启动请求均由命名互斥锁（Named Mutex `Local\LabDAQ-core-<hash>`）串行化排队处理，避免端口抢占。
2. **状态自动同步**：
   * 当 Agent API 调用 `start-acquisition` 或更改通道配置后，已打开的桌面客户端通过核心事件轮询，界面上的波形图、通道卡片与读数自动无缝同步最新状态。
   * 同样地，若在桌面 GUI 中点击“当前载荷归零”，元数据更新将立即作用于核心，后续由 Agent API 导出的工程量数据自动应用该零点偏置。
3. **超时与异常重试规范**：
   * 所有网络请求均具备严格的超时防护（默认 5 秒）。若发生网络超时，客户端抛出 `RuntimeError: 调用超时，执行结果未知`。
   * **操作守则**：发生异常时，禁止盲目连续重发写命令；请先调用 `status()` 核验核心当前实际运行状态，再采取后续动作。

---

## 导航与相关文档

- **操作手册**：[桌面客户端用户操作指南 (user-guide.md)](user-guide.md)
- **底层协议参考**：[进程间通信与硬件控制规范](../技术规范/ipc-protocol-spec.md) · [数据流管线与存储技术规范](../技术规范/data-pipeline-and-storage.md)
- **全局核心入口**：[项目首页](../../README.md) · [全局文档中心](../README.md)
