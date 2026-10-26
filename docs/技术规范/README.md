# LabDAQ 核心技术规范 (Technical Specifications)

[项目首页](../../README.md) / [文档中心](../README.md) / 核心技术规范

---

本目录汇集了 LabDAQ 数据采集系统的核心工程技术规范（Track 4: Technical Specifications），面向系统架构师、底层驱动开发者、数据分析研究人员以及自动化集成工程师。

技术规范对全链路数据模型、进程间通信协议、物理文件存储格式以及与原厂基线的对照进行了严谨的标准化定义。

---

## 规范文档清单

| 规范文档 | 核心技术主题 | 适用受众 |
| :--- | :--- | :--- |
| **[数据流管线与存储技术规范](data-pipeline-and-storage.md)** | • ADC 24 位无符号偏移二进制至物理电压换算数学模型<br>• Delta-Sigma ADC 39 周期 FIR 群延迟建立消除<br>• 软件调零偏置与传感器工程量换算管线<br>• `raw.f64` 物理排布与样本主序多通道交织标准<br>• `metadata.json` 两阶段原子写 Schema 与分段记录机制<br>• 实时因果 IIR 与离线双向零相位滤波算法规范<br>• Welch H1 传递函数与单边功率谱密度 (PSD) 能量守恒 | 算法工程师<br>数据分析师<br>存储开发人员 |
| **[进程间通信与硬件控制规范](ipc-protocol-spec.md)** | • 双进程解耦架构设计与守护核心生命周期<br>• Localhost TCP JSON-Lines 协议与 Token 鉴权模型<br>• 200,000 扫描帧无锁环形缓冲区与增量轮询设计<br>• 完整 IPC 指令集参考字典 (`poll`, `start`, `ao` 等)<br>• DT9837C 5 大 USB Bulk 通信端点规范<br>• 控制寄存器 2 (Reg 0x02) 耦合、量程与 4 mA IEPE 保护时序<br>• CY22150 锁相环分数分频与高速 256/512 Ticks 综合模型<br>• 32 KB DAC FIFO 供料与停机安全归零机制 | 驱动开发人员<br>系统工程师<br>API 开发者 |
| **[QuickDAQ 功能基线对照矩阵](quickdaq-feature-matrix.md)** | • 基于 DT9837C-BNC 仪器的原厂能力与 LabDAQ 逐项对照<br>• 模拟输入、采样率档位与前端调理特性比对<br>• 传感器配置与软件调零机制差异<br>• 开放存储格式 vs 专有 HPF 格式对比<br>• 示波呈现、双游标测量与多组历史对比工作台<br>• 模拟输出波形发生器与电气安全约束对照<br>• 频域分析与数字滤波算法特性对比<br>• 本机 Agent CLI / Python SDK 自动化能力评估 | 系统评估人员<br>测试工程师<br>技术负责人 |

---

## 全局文档体系导航

- **产品总览 (Track 1)**：[项目根目录 README](../../README.md) · [全局文档中心](../README.md) · [开源许可证声明](../../THIRD_PARTY_NOTICES.md)
- **用户手册 (Track 2)**：[桌面界面操作指南](../用户手册/user-guide.md) · [Agent API 自动化指南](../用户手册/agent-api.md)
- **开发者指南 (Track 3)**：[开发环境与构建](../开发文档/开发环境.md) · [硬件接口技术说明](../开发文档/technical-notes.md)
- **历史归档 (Archive)**：[历史技术档案索引](../archive/README.md)
- **历史兼容入口**：[功能实现目录兼容索引](../功能实现/README.md)
