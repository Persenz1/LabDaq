# 原始数据、工程量与离线分析规范 (兼容性过渡索引)

[项目首页](../../README.md) / [文档中心](../README.md) / [功能实现过渡索引](README.md) / 数据分析过渡

---

> [!NOTE] 规范重构与迁移提示
> 本文档已重构并升格为正式工程技术规范：**[数据流管线与底层存储技术规范](../技术规范/data-pipeline-and-storage.md)**。
> 为保证既有文档超链接与外部引用持续有效，本文档保留核心数学模型与数据结构摘要。完整架构图、详细吞吐量计算及扩展 JSON Schema 请查阅最新规范。

---

## 核心技术事实与换算管线摘要

### 1. 全链路数据管线
数据采集遵循“**原始物理量固化、显示与派生按需换算**”的确定性原则：
1. **硬件量化**：24 位无符号偏移二进制（Unsigned Offset Binary）ADC 码值；
2. **群延迟消除**：底层驱动自动剔除前 39 个扫描周期的建立数据（`GROUP_DELAY = 39`）；
3. **物理电压换算**：
   $$V_{\text{phys}} = \text{code} \times \left(\frac{2 \times R}{2^{24}}\right) - R$$
   其中 $R \in \{10.0\text{ V}, 1.0\text{ V}\}$；
4. **原始流盘存储**：`raw.f64` 始终保存未经调零、未经缩放的真实物理电压；
5. **工程量与显示换算**：
   $$Y = \left(\frac{(V - V_{\text{zero}}) \times 1000.0}{S_{\text{sens}}}\right) \times (\text{Inverted} ? -1 : 1) + \text{Offset}_{\text{eng}}$$

---

## 2. 存储规范与数据格式

- **`raw.f64`**：IEEE 754 双精度浮点（Little-Endian，NumPy dtype `"<f8"`），样本主序多通道交织排列（Scan-major）；
- **`metadata.json`**：完整保存会话配置、实际物理采样率、通道灵敏度、零偏基准 $V_{\text{zero}}$、极性、暂停分段信息（`recording_segments`）及模拟输出事件（`analog_output_events`），采用两阶段原子重命名写盘；
- **CSV / TXT 导出**：第一行写入 `# labdaq_metadata:` JSON 会话头，第二行为列标题，兼容 Pandas、NumPy、Excel 解析。

---

## 3. 详细技术规范直达

- **[完整数据流管线与存储技术规范](../技术规范/data-pipeline-and-storage.md)**
- **[进程间通信与硬件控制规范](../技术规范/ipc-protocol-spec.md)**
- **[QuickDAQ 功能基线对照矩阵](../技术规范/quickdaq-feature-matrix.md)**
- [返回过渡主索引](README.md) · [技术规范主索引](../技术规范/README.md) · [项目首页](../../README.md)
