# 功能实现与技术规范过渡索引 (Transition Index)

[项目首页](../../README.md) / [文档中心](../README.md) / 功能实现过渡索引

---

> [!NOTE] 目录迁移提示
> 本目录（`docs/功能实现/`）原包含项目早期的功能实现说明文档。为建立严整的四轨工程文档体系，相关技术标准与架构定义已全面重构并升格为正式的 **[核心技术规范 (Track 4)](../技术规范/README.md)**。
> 为保持既有超链接与外部引用的无缝兼容，本目录各文档均已配置对应的过渡指引与重构对照。

---

## 规范迁移对照表

| 原文档路径 | 目标技术规范路径 | 核心规范内容 |
| :--- | :--- | :--- |
| **[data-and-analysis.md](data-and-analysis.md)** | **[数据流管线与存储技术规范](../技术规范/data-pipeline-and-storage.md)** | 24 位 ADC 码值换算、FIR 39 周期群延迟剔除、软件调零偏置、`raw.f64` 二进制格式与 `metadata.json` 原子 Schema。 |
| **[protocol-details.md](protocol-details.md)** | **[进程间通信与硬件控制规范](../技术规范/ipc-protocol-spec.md)** | 双进程解耦架构、Localhost TCP 换行定界 JSON IPC、200,000 扫描环形缓冲、DT9837C 寄存器与 CY22150 PLL 时钟综合。 |
| **[quickdaq-features.md](quickdaq-features.md)** | **[QuickDAQ 功能基线对照矩阵](../技术规范/quickdaq-feature-matrix.md)** | QuickDAQ 3.7.0.49 与 LabDAQ 1.0.0 在 AI 采集、调理调零、流盘存储、游标测量、AO 输出与频域分析维度的全面工程比对。 |
| **[signal-input.md](signal-input.md)** | **[数据流管线与存储技术规范](../技术规范/data-pipeline-and-storage.md)** | 传感器输入特性、灵敏度换算公式、IEPE 恒流源配置与测试信号生成规范。 |

---

## 全局文档导航

- **项目主页**：[根目录 README](../../README.md)
- **文档中心**：[全局文档中心](../README.md)
- **正式技术规范**：[技术规范主索引](../技术规范/README.md)
- **历史归档档案**：[历史逆向与调研记录](../archive/README.md)
