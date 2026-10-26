# LabDAQ 文档体系导航中心 (Documentation Hub)

欢迎查阅 **LabDAQ 数据采集与分析系统**（面向 Data Translation DT9837C-BNC）全局工程文档体系。

为了向最终用户、自动化系统集成商、软件开发人员与算法工程师提供清晰、专业、结构严密的参考资料，LabDAQ 建立了面向工业级软件标准的**四轨工程文档体系**，并设立了独立的**历史技术归档区**。

---

## 1. 文档体系全景架构 (Documentation Architecture)

```text
LabDaq/
├── README.md                           # 【Track 1: 产品概览与全局索引】
├── THIRD_PARTY_NOTICES.md              # 开源软件许可证与代码版权归属声明
│
└── docs/
    ├── README.md                       # 【全局文档导航中心（本文档）】
    │
    ├── 用户手册/                        # 【Track 2: 用户使用手册】（随安装包分发）
    │   ├── user-guide.md               # 桌面应用完整操作指南（设备连接、实时示波、调零、AO输出、游标、导出）
    │   └── agent-api.md                # 本机自动化控制接口参考（CLI 命令行与 Python SDK）
    │
    ├── 开发文档/                        # 【Track 3: 开发者指南】
    │   ├── 开发环境.md                 # 源码构建、Rust 采集核心编译、Qt 运行与自动化测试
    │   ├── 打包与发布.md               # PyInstaller 冻结打包、Inno Setup 安装包制作与全流程测试清单
    │   └── technical-notes.md          # DT9837C 硬件技术参考（绑定至界面“打开技术说明”按钮）
    │
    ├── 技术规范/                        # 【Track 4: 核心工程技术规范（正本）】
    │   ├── README.md                   # 技术规范目录索引与全局导引
    │   ├── data-pipeline-and-storage.md# 数据流管线、raw.f64 与 metadata.json 存储规范
    │   ├── ipc-protocol-spec.md        # DT9837C 通信协议、锁相环时钟推导与寄存器定义
    │   └── quickdaq-feature-matrix.md  # QuickDAQ 功能基线对照表与能力矩阵
    ├── 功能实现/                        # 【Track 4: 历史兼容过渡索引目录】
    │
    └── archive/                        # 【历史技术档案与逆向日记】
        ├── index.md                    # 历史档案总索引与技术背景说明
        ├── windows-and-firmware-analysis.md # 原厂安装包解包、固件比对、校准电位器地址分析
        ├── technical-notes-20261011.md # 早期硬件调研笔记与工位测试准备事项
        ├── protocol-investigation-20261011.md # 早期 USB 协议逆向与离线加载模拟测试记录
        └── quickdaq-ui-survey-20261017.md # QuickDAQ 3.7.0.49 本机界面逐项核验日记
```

---

## 2. 四大主脉络详细导读 (The Four Tracks)

### Track 1: 产品概览 (Product Overview)
面向初次了解或评估 LabDAQ 系统的工程师与决策者。
- [项目主页与产品概览](../README.md)：介绍软件定位、DT9837C 核心硬件参数、双进程解耦系统架构、安装与快速上手指南、默认存储目录规范及全局快速索引。
- [第三方开源许可证声明](../THIRD_PARTY_NOTICES.md)：开源依赖组件归属与许可证文本（包含 libuldaq 1.2.2 MIT 许可）。

### Track 2: 用户使用手册 (User Manual)
面向实验室操作人员、测试员以及需要编写自动化测控脚本的开发者。本目录被打包分发至发布安装包中，可在离线状态下查阅。
- [桌面端用户使用指南](用户手册/user-guide.md)：提供桌面 GUI 客户端的端到端操作说明，详细拆解设备连接、通道电气配置（量程/耦合/IEPE）、实时示波器监视、软件调零及撤销、模拟信号发生器输出、记录流盘与暂停恢复、数据回看分析、双游标测量及多通道分块导出全流程。
- [本机自动化接口 (Agent API)](用户手册/agent-api.md)：面向大模型 Agent、批处理脚本及自动化测试台，提供无头客户端 `LabDAQ-api.exe` 命令行参数规范与 Python SDK (`labdaq.api.LabDAQ`) 结构化调用范例与 JSON 通信 Schema。

### Track 3: 开发者指南 (Developer Guide)
面向参与 LabDAQ 源码维护、跨平台编译、核心扩展与发布包制作的工程技术人员。
- [开发环境与源码运行](开发文档/开发环境.md)：详述 Windows 10/11 x64 开发依赖（Rust 1.80+、Python 3.12+、MSVC 构建套件）、Rust 核心编译指令、Python 虚拟环境配置、桌面程序启动、`--demo` 模拟模式及自动化测试套件运行。
- [打包与发布指南](开发文档/打包与发布.md)：详述 PyInstaller 冻结打包配置、Inno Setup 安装包脚本制作流程、OEM 驱动静默注入与 4 阶段全流程验证清单。
- [硬件接口技术说明](开发文档/technical-notes.md)：记录面向 DT9837C 的硬件架构、信号调理链参数与原厂 Open Layers C-ABI 动态链接库接口说明（**被桌面客户端菜单按钮直接调用，永久保留**）。

### Track 4: 核心技术规范 (Technical Specifications)
面向系统架构师、算法研究员与数据分析工程师，提供严格、无歧义的工程实现参考规范。
- [技术规范主索引](技术规范/README.md)：查阅 Track 4 核心工程技术规范全景图与规范编制标准。
- [数据流管线与存储规范](技术规范/data-pipeline-and-storage.md)：严格定义双精度浮点小端二进制原始数据 `raw.f64` 物理排布、`metadata.json` 原子写入与会话元数据 Schema、数据换算管线、因果 IIR / 双向零相位滤波数学模型及 Welch 功率谱与相干函数算法。
- [进程间通信与硬件控制规范](技术规范/ipc-protocol-spec.md)：详述 DT9837C 的 5 大 USB Bulk 通信端点、64 字节控制命令字编码、CY22150 锁相环分数分频计算公式、24 位偏移二进制 ADC/DAC 码值转换公式及双进程 TCP IPC 本地通信协议。
- [QuickDAQ 兼容性对照矩阵](技术规范/quickdaq-feature-matrix.md)：客观呈现 LabDAQ 相对 Data Translation QuickDAQ 在输入调理、记录管理、信号发生、图表交互及频域分析维度的功能基线对照矩阵。
- [功能实现过渡索引（兼容目录）](功能实现/README.md)：维护向后兼容的跳转小页（`data-and-analysis.md` 等），供已有书签无缝重定向。

### 历史归档区 (Historical Archive)
- [历史技术档案索引](archive/index.md)：归档封存项目研发初期（2026-10-11 至 2026-10-17）针对原厂安装包解包、PE 导入表逆向、FX2 固件加载机制比对、硬件校准寄存器解析及 QuickDAQ 本机界面逐项核验的详尽技术日志。为深度底层排查提供历史佐证，不干扰面向生产的工程文档。

---

## 3. 按角色快速检索指南 (Role-Based Reading Guide)

| 读者角色 | 推荐阅读路径 | 关注核心 |
| :--- | :--- | :--- |
| **试验员 / 实验室用户** | 1. [产品概览](../README.md)<br>2. [用户使用指南](用户手册/user-guide.md) | 设备接线、通道量程设置、4 mA IEPE 启用、当前载荷调零、数据记录与 CSV/TXT 导出。 |
| **自动化测试 / 脚本开发者** | 1. [Agent API 手册](用户手册/agent-api.md)<br>2. [数据流与存储规范](技术规范/data-pipeline-and-storage.md) | `LabDAQ-api.exe` 命令行参数、`labdaq.api` Python SDK、JSON 状态轮询、`raw.f64` 离线批量读取。 |
| **系统开发 / 维护工程师** | 1. [开发环境与构建](开发文档/开发环境.md)<br>2. [打包与发布指南](开发文档/打包与发布.md)<br>3. [硬件接口技术说明](开发文档/technical-notes.md)<br>4. [通信与硬件控制规范](技术规范/ipc-protocol-spec.md) | Rust 核心编译、PyInstaller 封装、Inno Setup 打包、Open Layers C 动态库加载、IPC 指令集、硬件错误处理。 |
| **信号分析 / 算法研究人员** | 1. [技术规范主索引](技术规范/README.md)<br>2. [数据流与存储规范](技术规范/data-pipeline-and-storage.md)<br>3. [通信与时钟规范](技术规范/ipc-protocol-spec.md) | 24-bit ADC 码值换算、FIR 滤波器 39 点群延迟修正、实时 SOS 因果滤波、离线零相位滤波、Welch 功率谱密度。 |

---

## 4. 文档维护与行文规范 (Style & Maintenance Guidelines)

为确保 LabDAQ 文档体系始终保持工业级软件的技术严谨性与高可读性，后续文档编写与更新须严格遵循以下规范：

1. **客观陈述与杜绝消极自辩**：
   - 严禁在正文中使用机械免责自辩句式、消极否定推脱或将调试对话强行写入正式文档；
   - 避免使用个人工作日志或工位流水账（如“今天没有硬件”、“明天继续时优先做…”）；
   - 工程边界与注意事项统一采用 GitHub 标准提示块（`> [!NOTE]`、`> [!TIP]`、`> [!WARNING]`）进行正向、客观的技术陈述。
2. **术语与技术事实严格一致**：
   - 采样率范围统一表述为：$195.313\text{ Hz} \sim 105,469\text{ Hz}$；
   - 模拟输出量程统一表述为：$\pm 3.0\text{ V}$（更新率 10/20/48/96 kS/s）；
   - 模拟输入量程统一表述为：$\pm 10\text{ V}$（增益 $\times 1$）与 $\pm 1\text{ V}$（增益 $\times 10$）；
   - 原始二进制文件物理格式统一表述为：小端 IEEE-754 双精度浮点（`<f8`）、样本主序多通道交织排列（Sample-major interleaved）。
3. **超链接与引用有效性**：
   - 所有 Markdown 文件中的相对路径链接必须确保指向有效文件或目录，严格杜绝死链；
   - 若引用的模块位于其他目录，必须遵循标准的相对路径跳转规则。

---

## 全局导航

- [返回项目产品总览 (README.md)](../README.md)
