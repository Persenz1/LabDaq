# 数据流管线与底层存储技术规范 (Data Pipeline & Storage Specification)

[项目首页](../../README.md) / [文档中心](../README.md) / [核心技术规范](README.md) / 数据流管线与存储规范

---

## 1. 概述 (Overview)

本规范定义了 LabDAQ 动态信号采集与分析系统面向 Data Translation DT9837C-BNC 硬件的数据流管线架构、底层数学换算模型、二进制存储格式（`raw.f64`）以及原子会话元数据（`metadata.json`）的完整工程标准。

系统采用“**原始物理量固化、显示与派生按需换算**”的核心架构设计原则：
1. **原始数据纯净性**：底层文件流盘始终保存未经调零偏移扣除、未经传感器灵敏度缩放、未经正负极性反转的真实硬件物理电压（单位：$\text{V}$）；
2. **元数据完备性**：会话配置、通道电气属性、调零偏置、工程量换算参数、分段暂停索引及硬件事件日志完整持久化于 `metadata.json`，确保离线复现与跨平台可追溯；
3. **计算确定性**：所有工程量换算、滤波算法与频域分析均具备严格的数学闭环与确定性定义。

---

## 2. 全链路数据流管线 (End-to-End Data Pipeline)

数据从物理传感器产生至最终呈现与导出的全生命周期流程如下：

```text
[物理传感器 (压电/电桥/调理器)]
            │
            ▼ (BNC 模拟输入端点)
[DT9837C 前端模拟电路]
  ├── 输入耦合选择: DC 耦合 / AC 耦合 (1 Hz 高通截止)
  ├── 恒流源激励: 4 mA IEPE 恒流源 (用于压电加速度计/动态力传感器)
  └── 量程增益放大: ±10 V (×1 增益) / ±1 V (×10 增益)
            │
            ▼
[24-bit Delta-Sigma ADC] (每通道独立并行转换)
            │
            ▼ (USB Bulk 端点 0x82: 4 字节/样本，小端无符号偏移二进制)
[Rust 采集核心 (labdaq-core)]
  ├── 群延迟消除: 严格丢弃前 39 个扫描周期 (GROUP_DELAY = 39)
  └── ADC 码值转物理电压: V_phys = Code * (2 * Range / 2^24) - Range
            │
            ▼ (写入 200,000 扫描帧无锁环形缓冲区 Ring Buffer)
     ┌──────┴─────────────────────────────────┐
     ▼                                        ▼
[流式落盘写入器 (Recorder)]           [客户端交互与表现层 (GUI / API)]
  ├── 写入 raw.f64 (小端双精度浮点)      ├── 软件调零: V_corr = V_phys - V_zero
  └── 原子写 metadata.json (会话配置)    ├── 传感器换算: Y = (V_corr * 1000 / S) + Offset
                                         ├── 实时/离线数字滤波 (因果 SOS / 双向零相位)
                                         ├── 频域分析 (单边幅度谱 / PSD / Welch H1)
                                         └── 数据导出 (CSV / TXT / 长表)
```

---

## 3. 数学换算模型与算法规范 (Mathematical Models & Algorithms)

### 3.1 ADC 量化码值至物理电压换算

DT9837C 的 24 位 Delta-Sigma ADC 采用**无符号偏移二进制（Unsigned Offset Binary）**编码。在底层 USB 数据流中，每个样本存放于 32 位（4 字节）小端无符号整数（`u32`，Little-Endian）的低 24 位中，高 8 位固定为 `0x00`。

#### 1. 数据完整性校验
驱动在解析每个 4 字节字时，首先执行帧同步与码值校验：
$$\text{code} < 2^{24} \quad (16,777,216)$$
若检测到高 8 位非零（即 $\text{code} \ge 2^{24}$），系统立即判定为 USB 传输失步或硬件帧边界错位，终止组帧并报告硬件通信异常。

#### 2. 电压换算公式
设通道量程为 $R$（$10.0\text{ V}$ 或 $1.0\text{ V}$），ADC 量化整型码值为 $\text{code}$（$0 \le \text{code} < 2^{24}$），计算得到的物理电压 $V_{\text{phys}}$ 遵循公式：

$$V_{\text{phys}} = \text{code} \times \left(\frac{2 \times R}{2^{24}}\right) - R$$

量化码值与物理电压映射对照表如下：

| 24 位原始码值 (Hex) | 24 位无符号整数 (Dec) | 对应物理电压 ($\pm 10\text{ V}$ 量程) | 对应物理电压 ($\pm 1\text{ V}$ 量程) | 物理电气意义 |
| :--- | :--- | :--- | :--- | :--- |
| `0x000000` | $0$ | $-10.000\,000\text{ V}$ | $-1.000\,000\text{ V}$ | 负满量程 (Negative Full Scale) |
| `0x400000` | $4,194,304$ | $-5.000\,000\text{ V}$ | $-0.500\,000\text{ V}$ | 负半满量程 |
| `0x800000` | $8,388,608$ | $0.000\,000\text{ V}$ | $0.000\,000\text{ V}$ | 模拟零电平 (Zero Center) |
| `0xC00000` | $12,582,912$ | $+5.000\,000\text{ V}$ | $+0.500\,000\text{ V}$ | 正半满量程 |
| `0xFFFFFF` | $16,777,215$ | $+9.999\,998\,8\text{ V}$ | $+0.999\,999\,88\text{ V}$ | 正满量程减 1 LSB ($+R - 2R/2^{24}$) |

---

### 3.2 Delta-Sigma 数字抽取滤波器群延迟 (Group Delay)

Delta-Sigma A/D 转换器内部集成了线性相位有限长单位冲激响应（FIR）抽取滤波器。该滤波器会产生固定的群延迟时间，等于 **39 个扫描周期（39 Scans）**。

#### 驱动建立与丢弃策略
在启动任何新的数据采集或硬件通道切换时，前 39 个扫描周期内转换器处于滤波建立过渡态。驱动层（`core/src/dt9837.rs`）在启动时执行精确的字节计数截断：
$$\text{DiscardBytes} = 39 \times M \times 4\text{ 字节}$$
其中 $M$ 为当前启用的物理通道数量。只有在前 39 个扫描周期的原始数据完全排出后，驱动才开始向环形缓冲区与文件写入器输出样本。此机制保证了传递给上层的所有数据均为稳态有效采样点。

---

### 3.3 软件调零机制 (Software Zeroing Offset)

#### 1. 工程原理与定义
软件调零旨在扣除测试现场因传感器安装预紧力、工装自重或温度漂移引入的静态直流偏置。
在设备处于无载稳态时，系统通过对各启用通道最近连续时间窗口（默认最多 $1.0\text{ 秒}$）内的原始物理电压进行算术平均，计算出通道静态零偏基准 $V_{\text{zero}}$：

$$V_{\text{zero}} = \frac{1}{K} \sum_{k=1}^{K} V_{\text{phys}}[k]$$

#### 2. 存储与呈现隔离规范
- **记录隔离原则**：$V_{\text{zero}}$ 作为浮点数值记录于当前会话的 `metadata.json` 中，**绝不反向修改 `raw.f64` 中的电压数值**；
- **显示修正**：界面显示和后续计算时，通道瞬态基准电压为：
  $$V_{\text{corr}} = V_{\text{phys}} - V_{\text{zero}}$$
- **无损撤销机制**：在非记录状态下，系统支持单步撤销调零，恢复先前的零点基准。

> [!NOTE] 调零与硬件校准的界限
> 软件调零是基于软件层面的静态偏移扣除，用于平衡测试工况；它不改变 DT9837C 仪器出厂的内部 ADC 增益校准电位器与基准源参数。

---

### 3.4 传感器工程量换算管线 (Engineering Unit Conversion)

物理电压至工程单位物理量（如力 $\text{N}$、压力 $\text{Pa}$、加速度 $\text{m/s}^2$ 等）的换算管线依次执行：**扣零点 $\to$ 灵敏度增益缩放 $\to$ 极性翻转 $\to$ 工程单位偏移加法**。

#### 1. 换算数学模型
设原始电压为 $V$（伏特），通道配置包含零点偏置 $V_{\text{zero}}$（伏特）、传感器灵敏度 $S_{\text{sens}}$（毫伏/工程单位，$\text{mV/EU}$）、极性反转标志 $\text{Inverted}$（布尔值）及工程单位显示偏移 $\text{Offset}_{\text{eng}}$（工程单位，$\text{EU}$），最终工程量值 $Y$ 的计算公式为：

$$Y = \left(\frac{(V - V_{\text{zero}}) \times 1000.0}{S_{\text{sens}}}\right) \times \left(\begin{cases} -1 & \text{if Inverted} \\ +1 & \text{otherwise} \end{cases}\right) + \text{Offset}_{\text{eng}}$$

#### 2. 特殊场景规则
- **电压直读模式**：当工程单位设定为 `V` 或灵敏度 $S_{\text{sens}} = 0$ 时，跳过除以灵敏度的步骤，直接输出：
  $$Y = (V - V_{\text{zero}}) \times \left(\begin{cases} -1 & \text{if Inverted} \\ +1 & \text{otherwise} \end{cases}\right) + \text{Offset}_{\text{eng}}$$
- **灵敏度参数配置规范**：若传感器出厂检定证书给出的是基于激励电压的输出（如桥式传感器的 $2.0\text{ mV/V}$），而现场配合使用了外部桥路供电与调理放大器，工程师应先将其换算为系统最终进入 BNC 前端的总灵敏度（$\text{mV/N}$ 或 $\text{mV/MPa}$），再填入通道参数栏。

---

### 3.5 数字滤波算法规范 (Digital Filtering Specification)

#### 1. 实时连续流因果滤波 (Streaming Causal IIR)
实时示波器与连续流数据采用 Butterworth 二阶节（Second-Order Sections, SOS）因果滤波器（`labdaq/processing.py:StreamingFilter`）：
- **结构**：4 阶低通、高通、带通或带阻，采用 SOS Direct Form II 转置结构；
- **状态维持**：块与块之间严格保留延迟单元状态矢量，消除分块数据拼接处的阶跃瞬态；
- **因果特性**：实时连续滤波具有固有的非线性相位与群延迟，适合实时监视，不改变后台写入的 `raw.f64`。

#### 2. 离线/窗口双向零相位滤波 (Zero-Phase Filtering)
在数据回看与离线分析页面，系统提供双向滤波（`labdaq/processing.py:filter_window`）：
- **算法模型**：采用 `scipy.signal.sosfiltfilt`，先正向滤波再反向滤波；
- **频率响应特征**：双向滤波消除所有相位失真（零相位延迟）。总幅频响应为单次滤波响应的平方，因此界面所设截止频率处的衰减幅度严格为 **$-6\text{ dB}$**，总滤波阶数为单次设定的 2 倍；
- **端点延拓**：使用奇延拓填充边界点，有效抑制信号两端的建立暂态。

---

### 3.6 频域与谱分析算法规范 (Spectral Analysis Specification)

系统内置标准化的频域分析算法（`labdaq/processing.py`），所有谱分析均保留真实的物理量纲与数学依据：

#### 1. 单边峰值幅度谱 (Peak Amplitude Spectrum)
对于采样率 $f_s$、长度为 $N$ 的时域序列 $x[n]$，加窗序列为 $x_w[n] = x[n] \cdot w[n]$：
- **相干增益补偿**：窗函数相干加权因子 $W_1 = \sum_{n=0}^{N-1} w[n]$；
- **谱值计算**：
  $$X_k = \frac{|\text{FFT}(x_w)[k]|}{W_1}$$
- **单边加倍**：直流分量（$k=0$）与 Nyquist 频点（$k = N/2$）保持原值，其余正频率分量乘 $2.0$。
- **物理特性**：在整周期无泄漏正弦波输入下，单边峰值谱的最大频点幅值严格等于时域正弦波的峰值幅度 $A$。

#### 2. 单边功率谱密度 (Power Spectral Density, PSD)
- **功率增益补偿**：能量加权因子 $W_2 = \sum_{n=0}^{N-1} w[n]^2$；
- **谱密度计算**：
  $$\text{PSD}[k] = \frac{|\text{FFT}(x_w)[k]|^2}{f_s \times W_2} \times \left(\begin{cases} 1 & k=0 \text{ or } k=N/2 \\ 2 & 1 \le k < N/2 \end{cases}\right)$$
  单位为 $\text{EU}^2/\text{Hz}$；
- **能量守恒（Parseval 定理）**：对单边全频段进行矩形离散数值积分满足 Parseval 定理：
  $$\sum_{k=0}^{N/2} \text{PSD}[k] \times \Delta f = \frac{1}{W_2} \sum_{n=0}^{N-1} x_w[n]^2$$
  当使用矩形窗时，积分结果严格等于时域信号的均方值（Mean Square Value）。

#### 3. Welch H1 频响传递函数与相干函数
- **分段估计**：采用 Welch 方法对参考通道 $x[n]$ 和响应通道 $y[n]$ 进行 50% 重叠加窗分段；
- **频响函数**：
  $$H_1(f) = \frac{P_{yx}(f)}{P_{xx}(f)}$$
  其中 $P_{yx}(f)$ 为互功率谱密度，$P_{xx}(f)$ 为输入自功率谱密度；
- **幅值平方相干函数 (Magnitude-Squared Coherence)**：
  $$\gamma_{xy}^2(f) = \frac{|P_{xy}(f)|^2}{P_{xx}(f) \cdot P_{yy}(f)}$$
  数值区间为 $[0.0, 1.0]$。系统规定至少需要 2 个重叠数据段方可输出相干系数，当输入通道信噪比过低或无激励能量时输出 `NaN`。

---

## 4. 底层存储规范与文件格式 (Storage Architecture & Binary Specifications)

### 4.1 目录组织与原子命名规范

记录系统严格遵循“**一实验一独立文件夹、永不覆盖既有数据**”的原子性安全规则：

```text
<recording_root>/
└── <experiment_name>/
    └── <condition>-<group_number>/
        ├── raw.f64           # 二进制原始采样流 (物理电压)
        └── metadata.json      # 会话配置与完整元数据
```

- **路径生成规则**（`labdaq/paths.py`）：
  - `experiment`（实验名）：用户自定义字符串（例如 `悬臂梁模态试验`）；
  - `condition`（工况）：工况标识（例如 `工况A`，未填写时默认为 `未命名`）；
  - `group_number`（组号）：整数序号，自 1 递增。若当前目录已存在，系统自动尝试下一个递增序号，禁止覆写任何已有历史记录。
- **存储根目录默认位置**：
  - **安装版**：`%USERPROFILE%\Documents\LabDAQ\recordings\`
  - **源码开发版**：`<repo_root>/recordings/`

---

### 4.2 二进制原始数据文件 `raw.f64` 物理排布

`raw.f64` 是采集系统核心流式落盘的二进制数据载体。

#### 1. 物理编码规格
- **数值类型**：IEEE 754 双精度浮点（Double Precision Float, 64-bit Little-Endian，对应 NumPy `<f8`）；
- **样本大小**：每个数据点占用 $8\text{ 字节}$；
- **文件结构**：**无任何自定义文件头、Magic Number 或尾部填充**，纯粹由浮点数据序列连续平铺构成。

#### 2. 通道交织布局 (Sample-Major Interleaved Layout)
数据严格按照**采样时间步优先（Scan-major）**进行多通道交织排列。
假设当前会话启用了通道子集 $[0, 2]$（共 2 个通道，即 AI0 与 AI2）：

| 字节偏移 (Bytes) | 对应数据类型 | 逻辑内容 | 时间步 | 通道 |
| :--- | :--- | :--- | :--- | :--- |
| `0x00000000..0x00000007` | Little-Endian `f64` | 物理电压 $V$ (Volts) | Scan 0 ($t=0$) | AI0 |
| `0x00000008..0x0000000F` | Little-Endian `f64` | 物理电压 $V$ (Volts) | Scan 0 ($t=0$) | AI2 |
| `0x00000010..0x00000017` | Little-Endian `f64` | 物理电压 $V$ (Volts) | Scan 1 ($t=\Delta t$) | AI0 |
| `0x00000018..0x0000001F` | Little-Endian `f64` | 物理电压 $V$ (Volts) | Scan 1 ($t=\Delta t$) | AI2 |
| ... | ... | ... | ... | ... |
| `(k*2)*8..(k*2+1)*8-1` | Little-Endian `f64` | 物理电压 $V$ (Volts) | Scan $k$ | AI0 |
| `(k*2+1)*8..(k*2+2)*8-1`| Little-Endian `f64` | 物理电压 $V$ (Volts) | Scan $k$ | AI2 |

- **单帧字节数**：
  $$\text{BytesPerScan} = M \times 8\text{ bytes}$$
- **总文件大小与采样帧数对应关系**：
  $$\text{FileSizeBytes} = \text{TotalScans} \times M \times 8\text{ bytes}$$

#### 3. 常见采样率下的数据吞吐量与磁盘占用参考表

| 采样通道数 | 标称采样率 (S/s) | 每秒数据率 (kB/s) | 每小时磁盘写入量 | 典型适用场景 |
| :---: | :---: | :---: | :---: | :--- |
| 4 通道 | $1,000\text{ Hz}$ | $32.00\text{ kB/s}$ | $115.20\text{ MB/h}$ | 准静态拉压、温度监测、慢速过程 |
| 4 通道 | $2,000\text{ Hz}$ | $64.00\text{ kB/s}$ | $230.40\text{ MB/h}$ | 机械低频振动、结构模态测试 |
| 4 通道 | $10,000\text{ Hz}$ | $320.00\text{ kB/s}$ | $1.152\text{ GB/h}$ | 常用工业旋转机械振动分析 |
| 4 通道 | $48,000\text{ Hz}$ | $1,536.00\text{ kB/s}$ | $5.530\text{ GB/h}$ | 声学与音频频段高精分析 |
| 4 通道 | $105,468.75\text{ Hz}$ | $3,375.00\text{ kB/s}$ | $12.150\text{ GB/h}$ | 冲击响应、高频动态断裂试验 |

---

### 4.3 会话元数据文件 `metadata.json` 规范

元数据文件完整记录了与 `raw.f64` 配套的设备状态、电气配置、通道参数与事件日志。

#### 1. 原子写入与断电保护机制
为防止操作系统在写盘时因异常断电导致 JSON 文件内容损坏，底层核心采用**两阶段原子写替换机制**（`core/src/main.rs:Recorder::save_metadata`）：
1. 核心首先将最新的 JSON 文本完整写入同目录下的临时文件 `metadata.json.tmp`；
2. 调用系统级文件刷新（`flush`）确保数据落盘；
3. 执行原子重命名操作（`fs::rename`）将临时文件替换为目标 `metadata.json`。

#### 2. 完整 JSON 结构定义与字段说明

```json
{
  "format": "labdaq-raw-voltage",
  "format_version": 1,
  "dtype": "<f8",
  "layout": "sample-major",
  "voltage_unit": "V",
  "sample_rate": 2000.0,
  "experiment": "悬臂梁模态测试",
  "source": "vendor",
  "target_sample_count": 100000,
  "sample_count": 100000,
  "duration_seconds": 50.0,
  "started_at_utc": "2026-10-26T08:30:00.123456Z",
  "acquisition_started_at_utc": "2026-10-26T08:29:45.000000Z",
  "stopped_at_utc": "2026-10-26T08:30:50.123456Z",
  "finalized": true,
  "recording_paused": false,
  "active_segment": null,
  "termination_reason": "duration_completed",
  "error": null,
  "channels": [
    {
      "index": 0,
      "name": "激振力",
      "unit": "N",
      "sensitivity_mv_per_unit": 20.0,
      "zero_volts": 0.00512,
      "inverted": false,
      "point_number": 1,
      "direction": "+Z",
      "engineering_offset": 0.0
    },
    {
      "index": 1,
      "name": "梁端加速度",
      "unit": "m/s2",
      "sensitivity_mv_per_unit": 10.2,
      "zero_volts": 0.0,
      "inverted": false,
      "point_number": 2,
      "direction": "+Z",
      "engineering_offset": 0.0
    }
  ],
  "acquisition_config": {
    "sample_rate": 2000.0,
    "channels": [0, 1],
    "ranges": [10.0, 1.0, 10.0, 10.0],
    "coupling": ["DC", "AC", "DC", "DC"],
    "iepe": [false, true, false, false],
    "record_start_scan": 30000,
    "sync_mode": "independent",
    "input": {
      "backend": "dt-open-layers-c-abi",
      "device": "DT9837-C(00)",
      "adc_encoding": "open-layers-24-bit-to-f64-volts",
      "timebase": "hardware-sample-index",
      "continuity": "open-layers-buffer-stream"
    }
  },
  "recording_options": {
    "duration_seconds": 50.0,
    "notes": "第一组标准激励测试",
    "specimen": "TC4-Titanium",
    "condition": "工况A",
    "group_number": 1
  },
  "recording_segments": [
    {
      "file_start_scan": 0,
      "source_start_scan": 30000,
      "sample_count": 60000
    },
    {
      "file_start_scan": 60000,
      "source_start_scan": 95000,
      "sample_count": 40000
    }
  ],
  "analog_output_at_start": {
    "running": true,
    "settings": {
      "waveform": "sine",
      "amplitude_v": 1.5,
      "offset_v": 0.0,
      "frequency_hz": 50.0,
      "sample_rate": 48000.0
    },
    "actual_sample_rate": 48000.0,
    "muted": false
  },
  "analog_output_events": [
    {
      "action": "update",
      "requested_at_utc": "2026-10-26T08:30:25.000000Z",
      "observed_input_scan": 80000,
      "output": {
        "running": true,
        "settings": {
          "waveform": "sine",
          "amplitude_v": 2.0,
          "offset_v": 0.0,
          "frequency_hz": 50.0,
          "sample_rate": 48000.0
        },
        "actual_sample_rate": 48000.0,
        "muted": false
      }
    }
  ]
}
```

#### 3. 字段语义与数据字典

| 字段路径 | 数据类型 | 描述与语义约束 |
| :--- | :--- | :--- |
| `format` | string | 固定字符串 `"labdaq-raw-voltage"`，标识文件格式族 |
| `format_version` | integer | 格式主版本号，当前规范为 `1` |
| `dtype` | string | 原始数据编码，固定为 `"<f8"`（小端 IEEE 754 双精度） |
| `layout` | string | 排布方式，固定为 `"sample-major"`（采样点主序多通道交织） |
| `voltage_unit` | string | 物理电压工程单位，固定为 `"V"` |
| `sample_rate` | number | 硬件实际执行的采样率（S/s），反映 PLL 分频后的实际物理时钟 |
| `experiment` | string | 所属实验名称 |
| `source` | string | 数据源类型：`"vendor"`（原厂驱动）、`"usb"`（WinUSB/直接 USB）、`"simulation"`（离线信号仿真）、`"file_replay"`（HPF 文件回放） |
| `target_sample_count` | integer \| null | 预设目标采样总帧数；手动停止模式下为 `null` |
| `sample_count` | integer | `raw.f64` 中实际写入的完整采样帧数 |
| `duration_seconds` | number | 有效记录总时长（秒），等于 `sample_count / sample_rate` |
| `started_at_utc` | string | 本次会话开始写盘的绝对时间戳（ISO-8601 UTC 格式） |
| `acquisition_started_at_utc` | string \| null | 底层连续硬件采集启动时间（先预览后记录时有区别） |
| `stopped_at_utc` | string \| null | 本次记录结束写盘的绝对时间戳；运行中为 `null` |
| `finalized` | boolean | 会话是否已正常关闭封顶（`true` 表示元数据已固化，文件句柄正常释放） |
| `recording_paused` | boolean | 当前记录是否处于暂停状态 |
| `active_segment` | object \| null | 当前处于活动写入状态的分段映射；暂停或已收尾时为 `null` |
| `termination_reason` | string | 终止原因代码：`"user_stop"`、`"duration_completed"`、`"acquisition_error"` |
| `error` | string \| null | 终止时若捕获到硬件错误，记录详细报错信息；正常结束为 `null` |
| `channels[]` | array | 已启用物理通道配置快照（包含名称、单位、灵敏度、零偏、极性、测点） |
| `acquisition_config` | object | 硬件前端电气设置快照（采样率、通道列表、各通道量程、耦合、IEPE 及后端接口） |
| `recording_options` | object | 实验备忘信息（时长、工况、试件、组号、备注） |
| `recording_segments[]` | array | 记录分段映射清单，支持记录中途暂停与恢复 |
| `analog_output_at_start` | object \| null | 记录启动时刻 AO 的参数与输出状态快照 |
| `analog_output_events[]` | array | 记录期间发生的所有 AO 参数调节、启停或静音事件日志 |

#### 4. 分段记录机制 (`recording_segments`)
当用户在采集过程中点击“暂停记录”并随后“继续记录”时，硬件连续时基并不中断，但文件写盘暂时挂起：
- `file_start_scan`：该分段在 `raw.f64` 内部的数据起始扫描帧索引；
- `source_start_scan`：该分段对应硬件时钟源上的全局物理扫描计数；
- **缺口重现**：回看与分析时，系统根据前后两段的 `source_start_scan` 差值，精确计算并呈现暂停期间经过的物理时间跨度，在波形绘制中保持时间轴严格对齐并以断线表示缺口，不进行人工补样。

---

## 5. 数据导出与交互规范 (Data Export & Interoperability)

### 5.1 CSV 与制表符 TXT 导出格式

导出的文本文件专为通用数据分析设计，兼顾人工可读性与自动化解析：

```csv
# labdaq_metadata: {"format":"labdaq-raw-voltage","sample_rate":2000.0,"experiment":"悬臂梁模态测试","source":"vendor",...}
time_s,力[AI0]_raw_V,加速度[AI1]_raw_V,力[AI0]_N,加速度[AI1]_m/s2
0.000000,0.105120,0.020400,5.000000,2.000000
0.000500,0.115240,0.025500,5.506000,2.500000
0.001000,0.125360,0.030600,6.012000,3.000000
```

1. **第 1 行（元数据注记行）**：以 `# labdaq_metadata:` 为前缀，随后紧跟单行完整序列化的 JSON 会话元数据，便于数据处理脚本全自动读取通道标度与采样率；
2. **第 2 行（列标题行）**：
   - 时间列名称根据所选格式确定：`time_s`（秒）、`time_ms`（毫秒）、`time_us`（微秒）或 `sample_index`（硬件采样帧序号）；
   - 原始电压列命名规则：`{channel_name}[AI{index}]_raw_V`（例如 `力[AI0]_raw_V`，默认通道名时为 `通道1[AI0]_raw_V`）；
   - 工程量列命名规则：`{channel_name}[AI{index}]_{unit}`（例如 `力[AI0]_N`，默认通道名时为 `通道1[AI0]_N`）；
   - 列排布规则：当导出模式为“双重输出（both）”时，时间列后先紧随所有活动通道的原始电压列，其后再紧随所有活动通道的工程量列。
3. **第 3 行及后续行**：连续数值数据，使用 UTF-8 BOM 编码保存，直接兼容 Microsoft Excel、Python、MATLAB 等工具。

### 5.2 数据科学分析工具集成示例

#### Python / Pandas 读取示例
```python
import json
import pandas as pd
from pathlib import Path

csv_path = Path("recordings/exp_01/run-1/export.csv")

# 1. 提取首行元数据
with open(csv_path, "r", encoding="utf-8-sig") as f:
    first_line = f.readline()
    meta_json = first_line.split("# labdaq_metadata:")[1].strip()
    metadata = json.loads(meta_json)

print(f"实验名称: {metadata['experiment']}, 采样率: {metadata['sample_rate']} Hz")

# 2. 直接以 Pandas DataFrame 读取数值矩阵 (自动忽略 # 注释行)
df = pd.read_csv(csv_path, comment="#", encoding="utf-8-sig")
print(df.head())
```

#### NumPy 原生快速读取 `raw.f64`
```python
import json
import numpy as np
from pathlib import Path

session_dir = Path("recordings/exp_01/run-1")

# 读取通道数
with open(session_dir / "metadata.json", "r", encoding="utf-8") as f:
    meta = json.load(f)
ch_count = len(meta["channels"])
sample_rate = meta["sample_rate"]

# 读取原始二进制物理电压
data = np.fromfile(session_dir / "raw.f64", dtype="<f8")
scans = len(data) // ch_count
data_matrix = data.reshape((scans, ch_count))  # 形状: [采样点数, 通道数]

print(f"成功载入 {scans} 帧物理电压，总时长: {scans / sample_rate:.3f} 秒")
```

---

## 6. 异常恢复与完整性保护 (Failure Recovery & Integrity)

若由于意外断电或客户端进程异常退出，会话在未调用正常收尾程序时中断：
1. **可用采样帧安全恢复**：二进制原始文件 `raw.f64` 采用块写入。恢复器读取文件磁盘长度 $L$（字节），并计算可用帧数：
   $$\text{RecoveredFrames} = \left\lfloor \frac{L}{M \times 8} \right\rfloor$$
   末尾若残留不足一帧的数据碎片，数据读取器将其安全截断丢弃，并标记 `finalized: false`；
2. **异常原因追溯**：在元数据中清晰保留异常收尾标记，禁止对未收尾且包含非稳态断裂的记录执行静默批量导出，强制要求工程师单组打开复核。

---

## 7. 相关技术规范与索引 (Related Specifications)

- [进程间通信与硬件寄存器规范](ipc-protocol-spec.md)：详述 Rust 核心服务与 UI 间的 TCP JSON IPC 通信协议及 DT9837C 寄存器配置；
- [QuickDAQ 兼容性对照矩阵](quickdaq-feature-matrix.md)：对比 LabDAQ 与 Data Translation QuickDAQ 在输入管线、流盘存储与算法实现的工程差异；
- [技术规范主索引](README.md)：查阅 Track 4 全部规范清单；
- [用户操作指南](../用户手册/user-guide.md)：介绍桌面 UI 中通道换算、调零与数据导出的实操指引；
- [开发者环境与构建](../开发文档/开发环境.md)：介绍开发环境搭建与测试套件执行；
- [全局文档导航中心](../README.md)：查阅全局四轨文档体系全景；
- [项目根目录 README](../../README.md)：返回系统首页与产品概览。
