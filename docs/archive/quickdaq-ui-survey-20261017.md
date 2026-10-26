# QuickDAQ 3.7.0.49 本机界面核对与参数基线日记 (2026-10-17)

[项目首页](../../README.md) / [文档中心](../README.md) / [历史技术档案](index.md) / QuickDAQ界面核验

---

> [!NOTE] 历史归档说明
> 本文归档于 2026-10-17。记录了开发团队在配备 DT9837C 硬件与原厂驱动的环境中，对 Data Translation QuickDAQ 3.7.0.49 软件展开的逐界面操作与参数基线核查记录。本记录作为 LabDAQ 软件功能定义与界面设计的权威参考。

---

## 1. 顶层菜单与功能入口

| 菜单栏分类 | 界面入口与功能选项 |
| :--- | :--- |
| **File** | New / Open / Close Hpf Data File；Export Data；Save Current Plot Image；Open Current Data In Excel；Load / Save Configuration (XML)；Open HPF / Export Folder；Page Setup、Print Preview、Print；Exit、Exit without saving current configuration |
| **Edit** | Copy plot data / image to clipboard |
| **Record** | View、Record、Pause (快捷键 Space)、Stop；未运行时 Pause / Stop 呈现禁用灰显状态 |
| **Plot** | Plot Area Mouse Mode: Select / Zoom / Cursor；Axis Mouse Mode: Scroll / Zoom |
| **Configuration** | Input Channel Configuration、Digital Filter Configuration、Device Selection |
| **Windows** | Acquisition Config、Plot and Data Config、Channel Plot、Channel Display、Channel Statistics；恢复上次保存布局或重置为默认布局 |
| **Mode / Help** | Data Logger / FFT Analyzer；User Manual、License Information、About；工具栏提供 UI Style 外观切换入口 |

---

## 2. 硬件设备、通道与数据采集配置

### 2.1 设备管理 (`Device Selection`)
- **设备族分类**：当前识别为 `OpenLayersDevices`；
- **设备列表参数**：展示 Name、Board ID、Device-ID、Driver Version（驱动版本为 `7.0.0.9`）、Master 同步标识；
- **空设备状态**：未发现或未选择物理硬件时，软件强制回退至 `File Reader Mode`（离线文件回放模式）。

### 2.2 模拟输入通道配置 (`Input Channel Configuration`)
- **通道数量**：4 路模拟输入，标识为 `Ain 0` ～ `Ain 3`；
- **配置属性**：
  - `Enable`：通道独立启停；
  - `Channel Name`：通道自定义标签；
  - `Range`：双量程切换，支持 **$-10\text{ V} \sim +10\text{ V}$** 及 **$-1\text{ V} \sim +1\text{ V}$**；
  - `Coupling`：AC 交流耦合 / DC 直流耦合；
  - `Current Source`：4 mA IEPE 恒流源开关；
  - `Engineering Unit`：工程量单位字符串；
  - `mV/EU`：传感器灵敏度（毫伏/工程单位）；
  - `EU Offset`：工程单位加法偏置；
  - `Point #` 与 `Dir`：测点编号与空间测向（例如 `X+`、`Y+`、`Z+`）；
- **批量操作**：支持一键批量启用/禁用所有通道，批量统一工程单位。

### 2.3 记录与采样时钟 (`Data Logger / Acquisition`)
- **采样频率范围**：标称为 **$195.313\text{ Hz} \sim 105,469\text{ Hz}$**；
- **触发源支持**：Software、External TTL Pos Edge、External TTL Neg Edge、Analog Threshold Pos/Neg Edge；
- **连续性设置**：支持连续不限时流盘或定长定时采集（格式为 `dd.hh:mm:ss`）。

---

## 3. 模拟输出发生器 (`Output Panel`)

- **通道配置**：1 路模拟输出，支持启停开关；
- **波形模式**：
  - `Fixed`（固定波形）：支持直流（DC）、正弦波（Sine）、方波（Square）、三角波（Triangle）；
  - `Sweep`（扫频波）：支持线性扫频与对数扫频，配置起始与终止频率；
  - `Noise`（噪声波）：支持白噪声与高斯噪声发生；
- **安全约束**：峰值幅值加偏置绝对值不得超出硬件物理上限 $\pm 3.0\text{ V}$。

---

## 4. 频谱分析与数字滤波配置

### 4.1 FFT 频域分析器 (`FFT Analyzer`)
- **分析点数 (FFT Size)**：支持 2 的整数次幂档位：
  $$128,\, 256,\, 512,\, 1024,\, 2048,\, 4096,\, 8192,\, 16384,\, 32768,\, 65536,\, 131072,\, 262144$$
- **窗函数支持**：Rectangular、Hamming、Hanning、Bartlett、Blackman、BlackmanHarris、FlatTop；
- **谱类型**：单边幅值谱（Spectrum）、自功率谱（AutoSpectrum）、功率谱密度（PSD）；
- **平均模式**：线性平均（RMS）、向量平均（Vector）、峰值保持（PeakHold）。

### 4.2 数字滤波配置 (`Digital Filter Configuration`)
- **滤波器类型**：Bessel、Butterworth、Chebyshev；
- **滤波响应**：高通（HighPass）、低通（LowPass）、带通（BandPass）、带阻（BandStop）；
- **阶数范围**：**1 ～ 10 阶**；
- **可视化特性**：提供幅频响应图、冲激响应图及阶跃响应图预览。

---

## 5. 官方 HPF 样例分析与参数边界

针对从 QuickDAQ 安装包提取的两个标准 HPF 样例进行了结构检验：

| 样例文件名 | 文件尺寸 | 通道定义与采样特征 |
| :--- | :---: | :--- |
| `QuickDAQ Data Logger Sample Data.hpf` | 393,216 字节 | 10 通道（电压/温度混合），采样时间间隔 $\Delta t = 0.1\text{ s}$（$10\text{ Hz}$） |
| `QuickDAQ FFT Analyzer Sample Data.hpf` | 655,360 字节 | 2 通道双精度电压，采样率 $15,000\text{ Hz}$，总帧数 20,480 点，5 次 FFT 平均 |

> [!NOTE] 边界启示
> 官方样例验证了 HPF 容器对多通道不同物理量及大规模数组的组织能力，为 LabDAQ 的高性能文件回放提供了结构基线。

---

## 归档导航

- [返回历史档案总索引](index.md) · [全局文档中心](../README.md) · [项目首页](../../README.md)
- 正式对照矩阵：[QuickDAQ 与 LabDAQ 功能基线对照矩阵](../技术规范/quickdaq-feature-matrix.md)
