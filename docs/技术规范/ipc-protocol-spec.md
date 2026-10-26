# 进程间通信与硬件控制技术规范 (IPC Protocol & Hardware Control Specification)

[项目首页](../../README.md) / [文档中心](../README.md) / [核心技术规范](README.md) / 进程间通信与硬件控制规范

---

## 1. 概述与架构全景 (Overview & Architecture)

LabDAQ 采用**双进程完全解耦的高可靠架构**，将底层高负载的实时硬件 I/O、流盘写入与上层交互图形界面严格隔离为两个独立的操作系统进程：

```text
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          表现与控制客户端 (Client Layer)                         │
│                                                                                 │
│   ┌──────────────────────────────┐          ┌───────────────────────────────┐   │
│   │    PySide6 / Qt 桌面主界面    │          │    Agent CLI / Python SDK     │   │
│   │ (pyqtgraph 渲染, 交互与示波) │          │  (labdaq.api, 无状态自动化脚本)│   │
│   └──────────────┬───────────────┘          └───────────────┬───────────────┘   │
└──────────────────┼──────────────────────────────────────────┼───────────────────┘
                   │                                          │
                   └───────────────────┬──────────────────────┘
                                       │ Localhost TCP (127.0.0.1:<port>)
                                       │ 换行定界 JSON (JSON-Lines, Token 认证)
                                       │ 凭据文件: .runtime/core.json
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     Rust 独立采集核心守护进程 (labdaq-core)                      │
│                                                                                 │
│  ┌───────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐  │
│  │   TCP Server 调度循环  │  │  200,000 扫描环形缓冲 │  │ 原始流盘写文件服务   │  │
│  │ (并发控制与 Token 鉴权)│  │ (无锁平铺 Ring Buffer)│  │ (raw.f64 + metadata) │  │
│  └───────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘  │
│              │                         │                         │              │
│              ▼                         ▼                         ▼              │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │                  DT9837C 硬件适配与流传输引擎 (Hardware Layer)            │  │
│  │  - Open Layers C-ABI / WinUSB Bulk 异步流 (端点 0x82, 0x06, 0x01, 0x81)  │  │
│  │  - 39 周期 Delta-Sigma FIR 群延迟剔除                                     │  │
│  │  - CY22150 锁相环时钟综合 (195.313 Hz ~ 105,469 Hz)                       │  │
│  │  - 32 KB 硬件 DAC FIFO 连续喂料线程与停机安全归零 (Packet 53)              │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 双进程解耦优势
1. **连续流盘免疫界面卡顿**：复杂的界面重绘、窗口拖拽、甚至 Python 运行时的垃圾回收或崩溃，**绝不影响**后台 Rust 核心对硬件 FIFO 的读取与磁盘无损写入；
2. **会话持久与热接回（Reattach）**：桌面界面因故关闭后，采集或 AO 信号输出在后台持续运行。用户重新启动 GUI 时，客户端通过检查端点凭据自动与正在运行的会话挂接，恢复示波监控；
3. **单例保护与资源独占**：通过操作系统级命名互斥量保证同一时刻仅有唯一的底层核心管理硬件，杜绝多进程冲突操作 USB 设备的隐患。

---

## 2. 传输层协议与端点发现 (Transport & Endpoint Rendezvous)

### 2.1 凭据发现文件 `.runtime/core.json`

采集核心（`labdaq-core.exe`）启动时，动态绑定系统空闲 TCP 端口，并在运行时目录生成认证凭据文件：
- **安装版路径**：`%LOCALAPPDATA%\LabDAQ\.runtime\core.json`
- **源码开发版路径**：`<repo_root>/.runtime/core.json`

凭据文件内容为单行 UTF-8 编码 JSON：
```json
{
  "port": 54321,
  "pid": 12345,
  "token": "4f9a8c7b6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a"
}
```

- `port`：当前核心监听的本地 TCP 端口（$1 \sim 65535$）；
- `pid`：核心进程在操作系统中的唯一 PID；
- `token`：由 32 字节密码学安全真随机数生成的 64 位十六进制鉴权字符串。

### 2.2 并发互斥控制 (Windows Named Mutex)

为避免多个客户端同时拉起多个核心实例造成端口冲突与硬件竞争，客户端在启动核心前，必须申请命名互斥量（`labdaq/core_client.py:_startup_lock`）：
- **互斥量名称**：`Local\LabDAQ-core-<sha256(endpoint_path)>`，其中哈希值为端点绝对路径规范化字符串的小写 SHA-256；
- **等待超时时间**：$10.0\text{ 秒}$。超时未获取则抛出并发等待异常，避免死锁。

### 2.3 报文封装与传输规则
1. **定界规则**：基于 TCP 流传输，所有请求与响应均采用**换行符（`\n`，`0x0A`）作为单帧报文定界符**；
2. **字符集编码**：严格采用 UTF-8 编码；
3. **帧大小上限**：单行报文长度上限为 $32\text{ MB}$（足够承载数秒的高频多通道浮点波形）；
4. **浮点数有效性**：严格禁止在 JSON 报文中包含 `NaN`、`Infinity` 或 `-Infinity`（Python 端序列化强制要求 `allow_nan=False`）。

---

## 3. 内存环形缓冲区规范 (In-Memory Ring Buffer Specification)

采集核心在内存中维护一个高性能的双精度浮点环形缓冲区（`core/src/main.rs:State.ring`）：

### 3.1 缓冲结构与容量
- **动态容量设计**：缓冲区容量根据物理采样率动态确定为最多 60 秒历史数据，并钳位在 1 至 200,000 帧之间：
  $$\text{Capacity} = \text{clamp}\left(\text{round}(f_{\text{actual}} \times 60.0),\, 1,\, 200000\right)$$
- **容量上限**：最高为 **200,000 个连续扫描周期（200,000 Scans）**（例如在高于 3.33 kS/s 时达到 200,000 帧上限）；
- **多通道连续排布**：数据在环形队列中按样本主序平铺，总存储浮点数容量为 $\text{CapacityFloats} = \text{Capacity} \times M$，其中 $M$ 为启用的物理通道数；
- **内存消耗**：4 通道在 200,000 帧满容量时，占用物理内存仅约 $200,000 \times 4 \times 8\text{ bytes} \approx 6.4\text{ MB}$。

### 3.2 游标推进与增量轮询模型
核心维护全局自增计数器 `total_scans`，记录自本次采集启动以来硬件产生的累计有效扫描帧数。

客户端通过 `poll` 指令拉取时域数据时，通过 `after_scan` 参数实现增量读取：
1. **客户端发起**：传入 `after_scan = S_last`（上次拉取到的末尾扫描帧号）以及所需的时间窗秒数 `seconds`；
2. **游标对齐**：
   - 若环形缓冲未溢出（$S_{\text{last}} \ge \text{total\_scans} - \text{Capacity}$），核心仅切片返回 $[S_{\text{last}}, \text{total\_scans})$ 区间的全新增量样本；
   - 若客户端拉取停顿过长导致缓冲被覆盖，核心返回当前最新窗口内的所有样本，并在响应中指示实际起点 `snapshot_start`；
3. **连续性重建**：客户端根据返回的 `snapshot_start` 与 `total_scans` 精确还原时间轴序列：
   $$t[k] = \frac{\text{snapshot\_start} + k}{f_s}$$

---

## 4. IPC 指令集与事件参考 (IPC Command Reference)

所有请求报文必须携带 `"token"` 字段与 `"op"` 操作标识。
核心返回的标准响应包裹格式为：
- **成功时**：`{"ok": true, "state": { ... }}`
- **失败时**：`{"ok": false, "error": "错误描述文本"}`

### 4.1 指令清单索引

| 操作标识 (`op`) | 主要参数 | 功能描述 | 返回核心字段 |
| :--- | :--- | :--- | :--- |
| `list_devices` | `source?` | 枚举当前连接的 DT9837 物理数采仪器 | `{"devices": [...]}` |
| `connect` | `profile` | 连接数采并初始化寄存器（不上电采集） | 全局状态快照 |
| `disconnect` | 无 | 断开设备连接并释放 Open Layers 驱动会话 | 全局状态快照 |
| `start` | `profile`, `recording_path?` | 启动连续采集预览（可附带启动流盘写文件） | 全局状态快照 |
| `stop` | 无 | 停止连续采集，保持设备连接与配置 | 全局状态快照 |
| `poll` | `seconds`, `after_scan?` | 核心状态轮询与最新时域波形增量拉取 | 全局状态快照 + `raw` 样本 |
| `start_recording` | `path`, `channels`, `experiment`, `recording_options` | 在当前连续采集流上启动流盘记录 | 全局状态快照 |
| `pause_recording` | 无 | 暂停记录流盘（硬件采集不停，仅暂停写文件） | 全局状态快照 |
| `resume_recording`| 无 | 恢复流盘记录，追加新的记录分段 | 全局状态快照 |
| `stop_recording` | 无 | 仅停止流盘记录并封顶元数据，硬件采集继续 | 全局状态快照 |
| `set_channels` | `channels` | 预览阶段动态更新通道标度与灵敏度配置 | 全局状态快照 |
| `start_output` | `settings` | 启动 AO 连续模拟信号输出 | 全局状态快照 |
| `update_output` | `settings` | 运行中动态平滑调整 AO 波形幅值、频率或偏置 | 全局状态快照 |
| `mute_output` | `muted` | 快速静音/取消静音（硬件输出保持 0V，时钟不停） | 全局状态快照 |
| `preview_output` | `settings`, `count?` | 纯离线数学计算 AO 波形点序列（最多 262,144 点） | `{"samples": [...]}` |
| `stop_output` | 无 | 停止 AO 信号发生，硬件自动归零至 0.0V | 全局状态快照 |
| `shutdown` | 无 | 安全终止 Rust 采集核心进程 | 核心连接关闭 |

---

### 4.2 核心指令交互报文详解

#### 1. `poll`（核心轮询与时域波形拉取）
客户端周期性（GUI 通常为 $30\text{ Hz}$）调用的核心接口。

- **请求示例**：
  ```json
  {
    "token": "4f9a8c7b6d...",
    "op": "poll",
    "seconds": 8.0,
    "after_scan": 154000
  }
  ```
- **响应载荷 (`state`) 结构**：
  ```json
  {
    "pid": 12345,
    "running": true,
    "recording": true,
    "recording_paused": false,
    "acquisition_started_at_utc": "2026-10-26T08:30:00.000000Z",
    "device_connected": true,
    "total_scans": 156000,
    "recorded_scans": 6000,
    "recording_target_scans": 10000,
    "recording_remaining_scans": 4000,
    "last_recording": "C:\\LabDAQ\\recordings\\exp_01\\run-1",
    "capacity": 200000,
    "buffered_scans": 16000,
    "snapshot_start": 154000,
    "error": "",
    "termination_reason": "user_stop",
    "output": {
      "running": true,
      "settings": {
        "waveform": "sine",
        "amplitude_v": 1.0,
        "offset_v": 0.0,
        "frequency_hz": 50.0,
        "sample_rate": 48000.0
      },
      "actual_rate": 48000.0,
      "zeroed": false,
      "completed": false
    },
    "raw": [
      0.1023, 0.0041,
      0.1025, 0.0042
    ]
  }
  ```
  - `raw`：展平的多通道交织浮点数组，长度等于 $(\text{total\_scans} - \text{snapshot\_start}) \times M$；
  - `snapshot_start`：本次切片首个样本在硬件时基上的绝对扫描帧索引。

#### 2. `start`（启动连续采集与流盘）
- **请求示例**：
  ```json
  {
    "token": "4f9a8c7b6d...",
    "op": "start",
    "recording_path": "C:\\LabDAQ\\recordings\\exp_01\\run-1",
    "profile": {
      "source": "vendor",
      "actual_rate": 2000.0,
      "config": {
        "sample_rate": 2000.0,
        "channels": [0, 1],
        "ranges": [10.0, 1.0, 10.0, 10.0],
        "coupling": ["DC", "AC", "DC", "DC"],
        "iepe": [false, true, false, false]
      },
      "channels": [
        {
          "index": 0,
          "name": "力传感器",
          "unit": "N",
          "sensitivity_mv_per_unit": 20.0,
          "zero_volts": 0.0,
          "inverted": false,
          "point_number": 1,
          "direction": "+Z",
          "engineering_offset": 0.0
        },
        {
          "index": 1,
          "name": "加速度计",
          "unit": "m/s2",
          "sensitivity_mv_per_unit": 10.0,
          "zero_volts": 0.0,
          "inverted": false,
          "point_number": 2,
          "direction": "+Z",
          "engineering_offset": 0.0
        }
      ],
      "experiment": "模态敲击试验",
      "recording_options": {
        "duration_seconds": 10.0,
        "condition": "工况A",
        "group_number": 1,
        "notes": "首次敲击测试"
      }
    }
  }
  ```

#### 3. `start_output` 与 `update_output`（模拟输出控制）
- **请求示例**：
  ```json
  {
    "token": "4f9a8c7b6d...",
    "op": "start_output",
    "settings": {
      "waveform": "sine",
      "amplitude_v": 1.5,
      "offset_v": 0.0,
      "frequency_hz": 100.0,
      "sample_rate": 48000.0,
      "end_frequency_hz": 1000.0,
      "sweep_seconds": 10.0,
      "sweep_mode": "linear",
      "sweep_repeat": true,
      "muted": false,
      "trigger": "immediate"
    }
  }
  ```
  - **参数合法性校验约束**：
    $$|V_{\text{offset}}| + V_{\text{amp}} \le 3.0\text{ V}$$
    直流（`dc`）时 $V_{\text{amp}}$ 忽略，且 $-3.0\text{ V} \le V_{\text{offset}} \le 3.0\text{ V}$。
    频率必须满足奈奎斯特上限：$0 < f < \frac{\text{sample\_rate}}{2}$。

#### 4. `stop_output`（AO 安全停机归零）
- **请求示例**：
  ```json
  {
    "token": "4f9a8c7b6d...",
    "op": "stop_output"
  }
  ```
- **硬件行为**：核心首先取消喂料线程，向 DT9837C 发送 Packet 53 强制将 DAC 电平锁定在 $0.0\text{ V}$（码值 `0x800000`），再关闭 DAC 子系统，防止探针端残留直流偏置。

---

## 5. DT9837C 硬件寄存器与底层控制规范 (DT9837C Hardware Control)

### 5.1 USB 通信端点架构
DT9837C 遵循 USB 2.0 高速（High-Speed 480 Mbps）规范，由 Cypress FX2 微控制器负责总线调度：

| 端点地址 (Endpoint) | 传输类型 | 传输方向 | 典型缓冲区大小 | 功能与用途 |
| :--- | :--- | :---: | :--- | :--- |
| **`0x01`** | Bulk OUT | 主机 $\to$ 设备 | 64 字节 | 命令下发包（64 字节定长报文） |
| **`0x81`** | Bulk IN | 设备 $\to$ 主机 | 64 字节 | 命令应答包（64 字节定长报文） |
| **`0x82`** | Bulk IN | 设备 $\to$ 主机 | 512 字节 / 块 | ADC 采样连续流（4 字节/样本，小端无符号偏移二进制） |
| **`0x06`** | Bulk OUT | 主机 $\to$ 设备 | 3,584 字节 / 块 | DAC 模拟输出波形流（每个块含 512 字节头 + 896 样本） |
| **`0x88`** | Bulk IN | 设备 $\to$ 主机 | 64 字节 | 硬件异步状态与 FIFO 溢出中断事件上报 |

---

### 5.2 前端电气控制寄存器定义 (Register 0x02)

控制寄存器 2（`Control Register 0x02`）为 16 位大端寄存器，专用于配置通道 0～3 的耦合、量程与 IEPE 供电：

```text
 15          12 11          8 7           4 3           0
┌──────────────┬─────────────┬─────────────┬─────────────┐
│ 保留 (置 0)  │ IEPE 4mA使能│ 量程增益选择 │ AC/DC 耦合  │
│              │ (bit 11..8) │ (bit 7..4)  │ (bit 3..0)  │
└──────────────┴─────────────┴─────────────┴─────────────┘
```

#### 寄存器位映射表：

| 位范围 (Bits) | 控制通道 | 逻辑电平 | 功能定义 |
| :---: | :---: | :---: | :--- |
| **Bit 0** | AI0 | `1` = AC 耦合, `0` = DC 耦合 | 交流耦合切入 1 Hz 高通电容；直流直通 |
| **Bit 1** | AI1 | `1` = AC 耦合, `0` = DC 耦合 | 同上 |
| **Bit 2** | AI2 | `1` = AC 耦合, `0` = DC 耦合 | 同上 |
| **Bit 3** | AI3 | `1` = AC 耦合, `0` = DC 耦合 | 同上 |
| **Bit 4** | AI0 | `0` = $\pm 10\text{ V}$, `1` = $\pm 1\text{ V}$ | 增益切换（`0` 对应增益 $\times 1$；`1` 对应增益 $\times 10$） |
| **Bit 5** | AI1 | `0` = $\pm 10\text{ V}$, `1` = $\pm 1\text{ V}$ | 同上 |
| **Bit 6** | AI2 | `0` = $\pm 10\text{ V}$, `1` = $\pm 1\text{ V}$ | 同上 |
| **Bit 7** | AI3 | `0` = $\pm 10\text{ V}$, `1` = $\pm 1\text{ V}$ | 同上 |
| **Bit 8** | AI0 | `1` = 开启, `0` = 关闭 | 内置 4 mA IEPE 恒流源开关 |
| **Bit 9** | AI1 | `1` = 开启, `0` = 关闭 | 同上 |
| **Bit 10**| AI2 | `1` = 开启, `0` = 关闭 | 同上 |
| **Bit 11**| AI3 | `1` = 开启, `0` = 关闭 | 同上 |

#### 硬件安全时序规范
为保护高灵敏度传感器与模拟前端运算放大器，避免大电流瞬态冲击，驱动层在修改寄存器时严格执行保护时序（`core/src/usb_input.rs:322-330`）：
- **切换到 DC 耦合前**：必须**先切断该通道的 IEPE 恒流源（写入 bit = 0）**，经过建立延迟后再关闭 AC 耦合开关；
- **开启 IEPE 激励前**：硬件建议仅在配置为 AC 耦合时启用 4 mA 恒流源，避免直流电平饱和。

---

### 5.3 CY22150 锁相环采样时钟综合规范 (PLL Synthesis)

DT9837C 采用板载 $48\text{ MHz}$ 晶振配合 Cypress CY22150 可编程锁相环合成采样时钟。

#### 1. 高速模式与 Ticks 分频切换
控制寄存器 1（`Control Register 0x01`）的 Bit 0 控制 ADC 转换时钟的 Ticks 分频比：
- **常规模式（$\text{Rate} \le 52,734\text{ Hz}$）**：`Bit 0 = 0`，ADC 转换周期 `Ticks = 512`；
- **高速模式（$\text{Rate} > 52,734\text{ Hz}$）**：`Bit 0 = 1`，ADC 转换周期 `Ticks = 256`。

#### 2. PLL 分数分频计算模型
时钟规划算法通过搜索满足误差最小的分频组合（`core/src/dt9837.rs:plan_clock`）：
- 目标分频频率：
  $$\text{Target} = \frac{\text{Rate} \times \text{Ticks} \times \text{Divider}}{10^6}$$
  其中 $\text{Divider} \in \{2.0, 4.0, 8.0, 16.0\}$；
- 压控振荡器频率：
  $$VCO = \text{Target} \times \text{DivN} \quad (100.0\text{ MHz} \le VCO \le 400.0\text{ MHz})$$
- 晶振反解公式（以 $48\text{ MHz}$ 为基准，搜索 $Q \in [3, 129]$ 与 $P \le 2055$）：
  $$F_{\text{actual}} = \frac{48.0 \times P}{Q}$$
- 标称精准采样率：
  - $48,000.00\text{ Hz}$（整数无误差）
  - $96,000.00\text{ Hz}$（整数无误差）
  - $105,468.75\text{ Hz}$（极限最高采样率）

---

### 5.4 模拟输出 (AO) DAC 硬件量化与 32 KB FIFO

#### 1. DAC 24 位偏移二进制量化公式
DAC 量程严格固定为 **$-3.0\text{ V} \sim +3.0\text{ V}$**（$\pm 3\text{ V}$ 双极性）。量化公式（`core/src/output.rs:code`）：

$$\text{Code} = \text{clamp}\left(\text{round}\left((V + 3.0) \times \frac{2^{24}}{6.0}\right), 0, 2^{24} - 1\right)$$

- $-3.0\text{ V} \to \text{Code } 0$ (`0x000000`)
- $0.0\text{ V} \to \text{Code } 8,388,608$ (`0x800000`)
- $+3.0\text{ V} \to \text{Code } 16,777,215$ (`0xFFFFFF`)

#### 2. 硬件 FIFO 流水与安全停机
- **硬件 FIFO 容量**：板载专用 FIFO 深度为 **32,768 字节**（通过下发 Packet 34 配置）；
- **预填充与喂料机制**：启动前，核心主动向端点 `0x06` 预写入 4 块数据（每块包含 896 个 24-bit 样本），随后启动 DAC 子系统，避免底层欠载（Underflow）；
- **停机强制安全归零**：任何时候停止输出或核心退出，驱动通过下发 Packet 53 写入 $0.0\text{ V}$，确保硬件端口安全。

---

## 6. 相关技术规范与索引 (Related Specifications)

- [数据流管线与存储技术规范](data-pipeline-and-storage.md)：包含 `raw.f64` 物理排布与 `metadata.json` 完整 Schema；
- [QuickDAQ 功能基线对照矩阵](quickdaq-feature-matrix.md)：对比系统能力与 QuickDAQ 原厂功能基线；
- [技术规范主索引](README.md)：查阅 Track 4 全部规范清单；
- [开发者环境指南](../开发文档/开发环境.md)：包含 Rust 核心编译与多平台调试说明；
- [历史协议研究档案](../archive/protocol-investigation-20261011.md)：早期 libuldaq 逆向考证与工位日记备份；
- [全局文档导航中心](../README.md)：查阅全局四轨文档体系全景；
- [项目根目录 README](../../README.md)：返回系统首页与产品概览。
