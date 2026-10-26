# LabDAQ 数据采集与分析系统

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](THIRD_PARTY_NOTICES.md)
[![Platform: Windows 10/11 x64](https://img.shields.io/badge/Platform-Windows%2010%2F11%20x64-brightgreen.svg)]()
[![Hardware: DT9837C-BNC](https://img.shields.io/badge/Hardware-DT9837C--BNC-orange.svg)]()

LabDAQ 是一套专为 **Data Translation DT9837C-BNC** 动态信号采集仪量身打造的工业级桌面数据采集、实时分析与自动化控制系统。系统采用 **Rust 独立采集核心 + Python/Qt 现代化桌面端** 的高性能双进程架构，兼具纳秒级硬件 I/O 吞吐稳定性与现代桌面人机交互体验，提供高精度动态信号监测、高保真无损流盘、多维度测量游标、任意波形生成与无头自动化（Agent API）能力。

---

## 核心特性矩阵

- **高精度同步动态采集 (AI)**：
  - 4 路独立 24 位 Delta-Sigma A/D 转换器，严格同步采样；
  - 硬件采样率支持 $195.313\text{ Hz} \sim 105,469\text{ Hz}$ 宽频连续可调；
  - 每通道独立支持双量程切换（$\pm 10\text{ V}$ 及 $\pm 1\text{ V}$）；
  - 每通道独立支持 AC/DC 耦合选择与 **4 mA IEPE** 恒流源激励（适配压电加速度计与动态力传感器）。
- **连续模拟信号发生器 (AO)**：
  - 1 路 24 位 DAC，双极性输出范围 **$-3.0\text{ V} \sim +3.0\text{ V}$**；
  - 硬件更新率支持 $10\text{ kS/s}$、$20\text{ kS/s}$、$48\text{ kS/s}$ 及 $96\text{ kS/s}$；
  - 支持直流偏压（DC）、高纯度正弦波（Sine）、方波（Square，50% 占空比）及三角波（Triangle）；
  - 具备停机硬件电平自动安全归零（$0.0\text{ V}$）机制，消除残余偏置。
- **高可靠双进程解耦架构**：
  - Rust 核心（`labdaq-core`）独占驱动会话，维护 200,000 扫描帧环形缓冲区并执行连续流式落盘；
  - 前端桌面界面通过本地 TCP IPC 与核心通信，界面的动态缩放、卡顿甚至异常退出重启，底层流盘与输出**零丢样不中断**，前端重启后自动挂接（Reattach）现有会话。
- **高保真流式存储与完整会话追踪**：
  - 纯净二进制原始数据存储：`raw.f64` 采用 IEEE 754 小端双精度浮点格式，按样本主序多通道交织排列，持久化记录未经软件修改的硬件原始物理电压；
  - 原子元数据固化：`metadata.json` 实时记录完整的传感器灵敏度、零点偏移、采集参数、时间戳与暂停分段区间；
  - 支持 QuickDAQ 历史 HPF 数据文件只读解析与 CSV/TXT 多通道分块导出。
- **专业级示波器与多维度测量交互**：
  - 基于 PyQtGraph 的实时高帧率时域波形渲染，支持相对时间、绝对时间（时钟同步）与采样点计数横轴；
  - 双测量游标系统（Cursor A/B）：具备 18 像素碰撞把柄、微秒级时间输入吸附，实时解算采样点工程量、时间差 $\Delta t$、幅值差 $\Delta y$、闭区间峰峰值及频率倒数 $1/|\Delta t|$；
  - 软件载荷调零：一键计算当前稳定窗口均值偏置并扣除，支持单步无损撤销；
  - 连续记录支持随时暂停与恢复，保留真实时间轴缺口。
- **本机自动化与无头控制 (Agent API)**：
  - 内置无依赖的命令行工具（`LabDAQ-api.exe` / `python -m labdaq.api`）与轻量 Python SDK (`labdaq.api.LabDAQ`)；
  - 全面基于换行定界 JSON 文本协议通信，便于大模型智能体（LLM Agent）、自动化测试工装及外部控制系统集成。

---

## 硬件规格与系统参数

系统面向 Data Translation DT9837C-BNC 硬件设计的技术指标如下：

| 分类 | 参数项 | 硬件规格与系统指标 | 代码与协议出处 |
| :--- | :--- | :--- | :--- |
| **模拟输入 (AI)** | 通道数与接口 | 4 通道单端同步采样，标准 BNC 接口（AI0～AI3） | `core/src/dt9837.rs` |
| | A/D 转换器 | 24 位 Delta-Sigma 架构，内置抗混叠抽取滤波器 | `docs/开发文档/technical-notes.md` |
| | 输入电压量程 | $\pm 10\text{ V}$（增益 $\times 1$）与 $\pm 1\text{ V}$（增益 $\times 10$），逐通道可设 | `core/src/usb_input.rs:332` |
| | 硬件采样率范围 | $195.313\text{ Hz} \sim 105,469\text{ Hz}$（受 Cypress CY22150 PLL 步进调节） | `labdaq/protocol.py:184` |
| | 滤波器群延迟 | 39 个扫描周期（39 Scans），底层驱动启动时自动丢弃前置暂态 | `core/src/dt9837.rs:8` |
| | 输入耦合方式 | AC 耦合（$-3\text{ dB}$ 截止频率为 $1\text{ Hz}$）/ DC 耦合 | `core/src/usb_input.rs:323` |
| | 传感器激励源 | **4 mA** 内置 IEPE 恒流源，开路顺从电压为 $18\text{ V}$ | `core/src/usb_input.rs:325` |
| | 输入阻抗 | $1\text{ M}\Omega \,\|\, 20\text{ pF}$ | 官方硬件手册 UM9837 |
| **模拟输出 (AO)** | 通道数与接口 | 1 通道独立输出，标准 BNC 接口（AO） | `core/src/output.rs` |
| | D/A 转换器 | 24 位 DAC，采用 Offset Binary 编码 | `core/src/output.rs:366` |
| | 输出电压范围 | **$-3.0\text{ V} \sim +3.0\text{ V}$** 双极性范围 | `labdaq/output_panel.py:14` |
| | 硬件更新率档位 | $10\text{ kS/s}$、$20\text{ kS/s}$、$48\text{ kS/s}$（默认）、$96\text{ kS/s}$ | `core/src/dt9837.rs:63` |
| | 停机保护 | 停止输出、会话结束或核心退出时，硬件电平自动安全归零（$0.0\text{ V}$） | `core/src/output.rs:376` |
| **系统架构** | 通信总线 | USB 2.0 高速（480 Mbps），USB VID `0x0867`，PID `0x9839` / `0x3998` | `core/src/dt9837.rs:5-7` |
| | 驱动支持 | 原厂 Open Layers C-ABI 动态链接库（`oldaapi64.dll`）/ WinUSB | `labdaq/core_client.py` |
| | 存储物理格式 | `raw.f64`（小端 IEEE 754 双精度浮点）+ `metadata.json`（原子会话元数据） | `core/src/main.rs:220` |

---

## 系统拓扑架构

LabDAQ 采用模块化分层与双进程解耦设计，系统运行拓扑如下：

```text
+------------------------------------------------------------------------------------+
|                               物理仪器: DT9837C-BNC                                |
|  - 4× AI (24-bit Delta-Sigma, 195Hz-105kHz, ±10V/±1V, AC/DC, 4mA IEPE)            |
|  - 1× AO (24-bit DAC, ±3.0V, 10k-96kS/s)                                          |
|  - Cypress EZ-USB FX2 (Cold PID 0x9839 -> Runtime PID 0x3998)                     |
+------------------------------------------------------------------------------------+
                                         ▲
                                         │ USB 2.0 Bulk 传输管道 (EP 0x01/0x81/0x82/0x06/0x88)
                                         ▼
+------------------------------------------------------------------------------------+
|                             操作系统内核与设备驱动层                               |
|  - 原厂 Open Layers x64 内核驱动 (DT9837aK_x64.sys) 与 C-ABI (oldaapi64.dll)       |
|  - 备选通用驱动 WinUSB (Winusb.sys)                                                |
+------------------------------------------------------------------------------------+
                                         ▲
                                         │ 高速 C 动态链接调用 / IOCTL
                                         ▼
+------------------------------------------------------------------------------------+
|                         采集与流盘核心进程: labdaq-core (Rust)                      |
|  - 硬件 I/O 调度与 200,000 扫描帧无锁环形缓冲区 (Ring Buffer)                      |
|  - 硬件群延迟修正 (丢弃启动建立期 39 Scans) 与 24-bit 偏移二进制量化换算           |
|  - 零丢失流式磁盘写入器 (raw.f64 连续落盘 + metadata.json 原子状态固化)            |
|  - AO 信号发生器喂料线程与 32 KB 硬件 FIFO 状态维护                                |
|  - 本地 TCP 服务端 (127.0.0.1 动态端口, core.json 凭据鉴权, 命名互斥量防并发)      |
+------------------------------------------------------------------------------------+
                                         ▲
                                         │ 本地 TCP 套接字 (换行定界 JSON-Lines 协议)
                    ┌────────────────────┴────────────────────┐
                    ▼                                         ▼
+---------------------------------------+ +------------------------------------------+
|      桌面表现层: PySide6 / Qt 应用    | |       自动化接口: Agent API / CLI        |
|  - PyQtGraph 高性能示波图表渲染       | |  - LabDAQ-api.exe (无控制台依赖命令行)   |
|  - 交互式双测量游标 (Δt, Δy, 峰峰值)  | |  - labdaq.api.LabDAQ (轻量 Python SDK)   |
|  - 传感器量程配置与载荷调零控制       | |  - 供大模型智能体与自动化测试工装集成    |
|  - 历史记录回看、对比与多格式数据导出 | |  - 统一结构化 JSON 请求与响应            |
+---------------------------------------+ +------------------------------------------+
```

---

## 快速启动指南

### 1. Windows 安装包运行（推荐生产使用）
1. 下载并运行 `LabDAQ-1.0.0-windows-x64-setup.exe` 安装向导；
2. 安装器将自动静默配置 Data Translation 原厂驱动包与 Open Layers 运行库，内置全套 Python 与 Rust 运行环境；
3. 安装完成后，从 Windows“开始菜单”或桌面快捷方式启动 **LabDAQ**；
4. 插入 DT9837C USB 线缆，软件自动识别设备并建立通信。

### 2. 源码开发与构建启动
适用于参与功能开发与测试的工程师（环境要求：Windows 10/11 x64、Rust 1.80+、Python 3.12+、MSVC 构建工具）：

```powershell
# 1. 克隆代码仓库
git clone https://github.com/Persenz1/LabDaq.git
cd LabDaq

# 2. 激活开发环境
. .\tools\dev-env.ps1

# 3. 编译 Rust 高性能采集核心
cargo build --release -p labdaq-core

# 4. 启动 LabDAQ 桌面客户端
.\launch.ps1

# 备选：无物理硬件时启动离线信号模拟模式
python -m labdaq --demo
```

### 3. 自动化命令行 (CLI) 快速上手
LabDAQ 提供免界面的命令行工具，便于批处理脚本调用：

```powershell
# 查询当前核心与设备状态
python -m labdaq.api status

# 枚举已连接的物理数采硬件
python -m labdaq.api devices

# 连接硬件并以 5000 Hz 启动通道 0 和通道 1 采集
python -m labdaq.api start-acquisition --sample-rate 5000 --channels 0 1

# 启动模拟输出：48 kS/s、100 Hz、幅值 1.5 V 正弦波
python -m labdaq.api start-output --waveform sine --amplitude-v 1.5 --frequency-hz 100 --sample-rate 48000

# 启动数据流盘至指定目录（记录 10 秒后自动收尾）
python -m labdaq.api start-recording ./recordings/exp_01 --duration-seconds 10.0 --experiment "结构模态初测"

# 导出记录数据为双列（原始电压与工程量并存）CSV 文件
python -m labdaq.api export ./recordings/exp_01 ./exports/exp_01.csv --values both
```

---

## 存储目录与文件排布规范

LabDAQ 遵循严格的“**一次实验独立成组、原始文件不修改**”存储规范：

### 目录环境规范

| 用途分类 | Windows 标准安装环境路径 | 源码开发调试环境路径 | 说明 |
| :--- | :--- | :--- | :--- |
| **程序与运行环境** | `%ProgramFiles%\LabDAQ` | `<repo_root>` | 可执行文件、动态库与用户手册 |
| **运行时状态配置** | `%LOCALAPPDATA%\LabDAQ\.runtime\` | `<repo_root>\.runtime\` | `core.json` 端口凭据与动态会话配置 |
| **实验记录根目录** | `%USERPROFILE%\Documents\LabDAQ\recordings\` | `<repo_root>\recordings\` | 默认实验组存储根路径（跟随系统重定向） |

### 实验数据组文件构成

每次数据记录均生成独立的子目录：`<recordings>/<experiment_name_or_timestamp>/`：
- **`raw.f64`**：原始物理采样电压二进制文件。IEEE 754 小端双精度浮点格式（NumPy dtype `"<f8"`），样本主序交错排布，保留最高测量保真度。
- **`metadata.json`**：会话元数据描述文件。采用原子写入机制，记录包含采样率、通道增益与耦合、传感器单位与灵敏度系数、调零偏移量（`zero_volts`）、启停时间戳及暂停分段（`recording_segments`）。

---

## 全局文档体系导航

LabDAQ 建立了结构清晰的工程文档体系，详细资料请查阅：

| 文档分类 | 包含核心内容 | 主要入口 |
| :--- | :--- | :--- |
| **全局导航索引** | 文档体系全景图、各工程角色检索指南与行文标准 | [文档导航中心](docs/README.md) |
| **Track 2: 用户使用手册** | 桌面端端到端操作步骤、传感器调零、游标分析与 Agent API 参考 | [用户使用指南](docs/用户手册/user-guide.md) · [自动化接口 (Agent API)](docs/用户手册/agent-api.md) |
| **Track 3: 开发者指南** | 源码编译构建流程、驱动适配、自动化测试及安装包制作规范 | [开发环境与构建运行](docs/开发文档/开发环境.md) · [打包与发布指南](docs/开发文档/打包与发布.md) · [硬件接口技术说明](docs/开发文档/technical-notes.md) |
| **Track 4: 核心技术规范** | 底层存储规范、换算公式、数字滤波数学模型与 USB 通信协议 | [技术规范主索引](docs/技术规范/README.md) · [数据流与存储规范](docs/技术规范/data-pipeline-and-storage.md) · [通信与控制规范](docs/技术规范/ipc-protocol-spec.md) · [QuickDAQ 对照](docs/技术规范/quickdaq-feature-matrix.md)（兼容过渡: [功能实现索引](docs/功能实现/README.md)） |
| **历史归档 (Archive)** | 原厂驱动解包逆向、固件加载比对日志及早期研究备忘 | [历史档案总索引](docs/archive/index.md) · [固件逆向分析](docs/archive/windows-and-firmware-analysis.md) |

---

## 开源协议与第三方声明

本项目遵循开源软件许可协议。DT9837C 底层通信协议参考了开源驱动 libuldaq 1.2.2（采用 MIT 许可）；项目使用的 Python、Rust 及 Qt 运行组件版权声明详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
