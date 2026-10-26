# DT9837C 协议与初始化规范 (兼容性过渡索引)

[项目首页](../../README.md) / [文档中心](../README.md) / [功能实现过渡索引](README.md) / 协议细节过渡

---

> [!NOTE] 规范重构与迁移提示
> 本文档已重构并升格为正式工程技术规范：**[进程间通信与硬件控制技术规范](../技术规范/ipc-protocol-spec.md)**。
> 项目早期的 libuldaq 协议逆向笔记与工位准备日志已完整封存至 **[历史协议探索档案](../archive/protocol-investigation-20261011.md)**。
> 为保持既有超链接与外部引用持续有效，本文档保留核心协议架构与硬件寄存器控制摘要。

---

## 核心通信与控制规范摘要

### 1. 双进程解耦架构与 IPC 传输
- **架构设计**：Rust 独立后台采集核心（`labdaq-core`）负责高速多线程 Bulk I/O、200,000 扫描帧环形缓冲区维护、`raw.f64` 实时流盘与 32 KB DAC FIFO 供料；
- **IPC 传输层**：基于 Localhost TCP Socket（`127.0.0.1:<port>`），采用换行定界 JSON（JSON-Lines）文本协议，通过 `.runtime/core.json` 交换动态端口与 64 位 Token 认证凭据；
- **并发控制**：在 Windows 平台上通过命名互斥量 `Local\LabDAQ-core-<hash>` 实现单例互斥保护。

### 2. DT9837C 硬件寄存器与 USB 规范
- **USB 通信端点**：
  - `0x01`（Bulk OUT）：64 字节命令包
  - `0x81`（Bulk IN）：64 字节命令应答
  - `0x82`（Bulk IN）：ADC 连续采样流
  - `0x06`（Bulk OUT）：AO 模拟输出流
  - `0x88`（Bulk IN）：异步状态与溢出中断
- **控制寄存器 2 (Reg 0x02)**：
  - Bit 0～3：AC/DC 耦合控制（1=AC, 0=DC）
  - Bit 4～7：电压量程增益（0=$\pm 10\text{ V}$, 1=$\pm 1\text{ V}$）
  - Bit 8～11：4 mA IEPE 恒流源开关（1=使能, 0=关闭）
  - *安全时序*：切换至 DC 耦合前必须先关闭 4 mA 恒流源。
- **锁相环时钟综合 (CY22150)**：
  - 支持 $195.313\text{ Hz} \sim 105,469\text{ Hz}$ 采样率连续合成；
  - 采样率 $> 52,734\text{ Hz}$ 时自动切换高频 256 Ticks 模式。

---

## 3. 详细技术规范直达

- **[完整进程间通信与硬件控制规范](../技术规范/ipc-protocol-spec.md)**
- **[数据流管线与存储技术规范](../技术规范/data-pipeline-and-storage.md)**
- **[QuickDAQ 功能基线对照矩阵](../技术规范/quickdaq-feature-matrix.md)**
- **[历史协议探索归档 (2026-10-11)](../archive/protocol-investigation-20261011.md)**
- [返回过渡主索引](README.md) · [技术规范主索引](../技术规范/README.md) · [项目首页](../../README.md)
