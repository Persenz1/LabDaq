# QuickDAQ 功能基线与 LabDAQ 对照 (兼容性过渡索引)

[项目首页](../../README.md) / [文档中心](../README.md) / [功能实现过渡索引](README.md) / QuickDAQ过渡

---

> [!NOTE] 规范重构与迁移提示
> 本文档已重构并升格为正式工程技术规范：**[QuickDAQ 功能基线对照矩阵](../技术规范/quickdaq-feature-matrix.md)**。
> 原文档末尾包含的 2026-10-17 本机界面审查日记已完整封存至 **[QuickDAQ 本机界面核验归档](../archive/quickdaq-ui-survey-20261017.md)**。
> 为保持既有超链接与外部引用持续有效，本文档保留主要能力维度的对照概述。

---

## 核心能力维度对照概述

### 1. 模拟输入 (AI) 与通道调理
- **通道配置**：DT9837C 提供 4 路 24 位 Delta-Sigma 同步输入，LabDAQ 支持任意升序子集选择与硬件级解交织；
- **采样率与时钟**：支持 $195.313\text{ Hz} \sim 105,469\text{ Hz}$，驱动自动补偿 39 周期 FIR 滤波器群延迟；
- **前端调理**：逐通道独立配置 $\pm 10\text{ V} / \pm 1\text{ V}$ 量程、AC/DC 耦合及 4 mA IEPE 恒流源，具备安全切断时序；
- **硬件真实性界定**：DT9837C 硬件不包含物理 Tachometer 通道，LabDAQ 准确反映物理硬件边界。

### 2. 存储架构与数据管理
- **文件格式**：QuickDAQ 采用专有 HPF 格式；LabDAQ 采用开放 IEEE 754 双精度 `raw.f64` 存储真实物理电压，配合原子写入的 `metadata.json` 会话元数据；
- **双进程保护**：界面关闭或崩溃时后台守护核心持续无损写盘，重新打开界面自动热挂接恢复；
- **分段记录**：暂停与恢复在元数据中生成分段列表，时域回看中以断线保持真实物理时间跨度，不进行人工补样。

### 3. 信号发生与频域分析
- **模拟输出 (AO)**：1 路 24 位 DAC，$\pm 3\text{ V}$ 量程，支持直流、正弦、方波、三角波，硬件停机自动发送 Packet 53 归零至 $0.0\text{ V}$；
- **频域算法**：内置单边峰值幅度谱（相干增益补偿）、单边功率谱密度（PSD，Parseval 能量守恒）与 Welch H1 频响传递函数及相干函数；
- **自动化支持**：提供无状态 Python SDK（`labdaq.api.LabDAQ`）与控制台 CLI 工具（`LabDAQ-api.exe`），支持全自动化测试。

---

## 4. 详细技术规范直达

- **[完整 QuickDAQ 功能基线对照矩阵](../技术规范/quickdaq-feature-matrix.md)**
- **[数据流管线与存储技术规范](../技术规范/data-pipeline-and-storage.md)**
- **[进程间通信与硬件控制规范](../技术规范/ipc-protocol-spec.md)**
- **[QuickDAQ 本机界面核验归档 (2026-10-17)](../archive/quickdaq-ui-survey-20261017.md)**
- [返回过渡主索引](README.md) · [技术规范主索引](../技术规范/README.md) · [项目首页](../../README.md)
