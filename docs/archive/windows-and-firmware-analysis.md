# Windows 原厂驱动、固件加载与 USB 逆向分析档案 (2026-10-11)

[项目首页](../../README.md) / [文档中心](../README.md) / [历史技术档案](index.md) / 原厂驱动与固件逆向

---

> [!NOTE] 历史归档说明
> 本文归档于 2026-10-11 早期逆向工程阶段。分析对象包含 Data Translation OMNI 7.8.9、QuickDAQ 3.7.0.49、libuldaq 1.2.2 及 DT9837 系列驱动包。本文记录了离线解包、PE 导入表逆向、FX2 固件记录逐字节比对、硬件校准电位器 I2C 映射推导以及 HPF 格式解析全过程。

---

## 1. 核心技术结论

1. **固件逐字节一致性**：Windows 原厂驱动与开源 Linux 驱动（libuldaq）内嵌的两段 FX2 固件记录逐字节完全一致。这证明在独立实现中可以直接使用该固件镜像实现冷启动枚举。
2. **两阶段驱动加载模型**：原厂驱动遵循“冷设备固件加载驱动（Loader） + 运行态采集驱动（Client Driver）”架构。冷态 PID 为 `0x9839`，固件载入后重枚举为运行态 PID `0x3998`。
3. **硬件级校准与软件调零解耦**：硬件校准由专用的电位器读写指令（Command 50/51）通过 I2C 总线配置内部数字电位器；软件层面的调零仅为零点偏移补偿，不改变硬件电位器持久化槽位。
4. **HPF 容器格式开放可读**：原厂 HPF 数据文件采用分块（Chunk）存储架构，元数据以明文 XML 呈现，双精度浮点数据按 64 KiB 边界对齐存放，具备无原厂环境只读解析能力。

---

## 2. 原厂资源提取与离线分析

提取工作基于 `resources/vendor/extracted/` 目录组织，包含驱动 INF/SYS/CAT、Open Layers API 运行库及头文件。

### 提取文件清单与组件定位

| 目录/文件 | 组件性质 | 技术说明与用途 |
| :--- | :--- | :--- |
| `oem-x64/`、`oem-x86/` | 驱动与运行库 | DT9837A 系列用户态 DLL、驱动 INF/SYS/CAT、Open Layers API |
| `oem-*/program files/Data Translation/DeviceDrivers/DT9837A/` | 内核驱动 | 原厂常规代码签名驱动包 |
| `oem-*/DT9837A.dll` (v7.0.0.15) | 用户态 DLL | 包含 `Dt9837CDlg` 与 `Power Always On` 逻辑，证实 DT9837C 属于 A 系列统一管理 |
| `oem-*/Dt9837ACollection.dll` (v7.0.0.0) | 设备集合层 | DT9837 全系列（A/B/C）通用的统一设备发现与集合管理组件 |
| `sdk-reference/.../Win32/SDK/Include/` | 官方 SDK 头文件 | 包含 `OLDAAPI.H`、`OLDADEFS.H`、`DtIoctl.h`、`DtDeviceInterface.h` |
| `sdk-reference/.../ContAdc/`、`IepContAdc/` | 官方 C 范例 | 官方连续采样与 IEPE 采样例程 |
| `sdk-reference/.../DT9837 Calibration.exe` | 32 位校准工具 | 原厂出厂与现场校准程序（v6.7.4.7） |
| `quickdaq/.../QuickDAQ/` | 官方采集软件 | QuickDAQ.exe 主程序及信号分析程序集 |
| `quickdaq/.../QuickDAQ/Data/` | 官方 HPF 样例 | Data Logger 与 FFT Analyzer 官方测试样例文件 |
| `hpf-reference/.../DT-HPF-format-spec-22760-RevA-mirror.pdf` | 技术文档 | Data Translation 2007 年发布的 HPF 格式白皮书镜像 |

### 离线解包流程

1. 使用 7-Zip 解压 OMNI 安装包，提取 `SetupOem.exe`；
2. 识别外层 PE Overlay 为 `ISSetupStream`（Type 2），采用开源工具 [ISx v0.3.11](https://github.com/lifenjoiner/ISx) 解包提取嵌套 MSI 与 x64 OEM EXE；
3. 解压 MSI 内部 `Data1.cab`，通过 Windows Installer COM 接口 `OpenDatabase(path, 0)` 只读查询 `File`、`Component`、`Directory` 表，恢复真实文件名与目录结构。

---

## 3. 冷启动与固件加载机制

### 3.1 USB 硬件标识与设备枚举

根据 `DT9837a_Kmdf.inf` 声明：

| 设备阶段 | 硬件 ID (VID & PID) | 驱动处理服务 | 职能定义 |
| :--- | :--- | :--- | :--- |
| **冷启动未加载态** | `USB\VID_0867&PID_9839` | `DT9837ALd_x64.sys` | Firmware Loader（固件加载器） |
| **热态运行中** | `USB\VID_0867&PID_3998` | `DT9837Ak_x64.sys` | USB Client Driver（数据采集驱动） |

> [!NOTE] 型号识别注意
> DT9837 全系列（A、B、C）共用上述 USB VID/PID。热态建立后，驱动通过 I2C 读取内部 EEPROM 地址 `0x05..0x06` 的 Subtype 字段进一步确认型号（A=1, B=2, C=4）。

### 3.2 固件记录逐字节比对

`IntelHexRec.h` 定义固件记录结构为 `Length(u8)`、`Address(u16)`、`Type(u8)`、`Data[16]`（对齐后每条记录 22 字节）。比对 libuldaq 源码数组与 Windows loader SYS 二进制：

| 固件映像 | 总记录数 | 对应字节数 | x64 SYS 偏移 | x86 SYS 偏移 | 比对结果 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `DT9837A_FW.c` | 1035 | 22770 | `0x1540` | `0x13D0` | 逐字节完全匹配 |
| `DTFX2LDR_FW.c` | 60 | 1320 | `0x6E50` | `0x0E80` | 逐字节完全匹配 |

### 3.3 固件下载时序

1. 主机枚举到冷态 PID `0x9839`，绑定 Interface 0；
2. 对 EZ-USB 控制寄存器 `0x7F92` 及 `0xE600` 发送 Vendor OUT Request `0xA0`，置 8051 于复位状态；
3. 下载二级辅助引导固件（`DT_FX2LDR_FW_Image`），释放 8051 复位，等待 1 ms；
4. 下载设备主固件（`DT9837A_FW_Image`）：
   - 地址 $> 0x1B3F$ 的外部 RAM 记录通过 Request `0xA3` 写入；
   - 内部 RAM 记录停止 8051 后通过 Request `0xA0` 写入；
5. 再次对 8051 执行复位并释放，关闭连接句柄，设备自动断开并重新枚举为运行态 PID `0x3998`。

---

## 4. 硬件校准机制与数字电位器映射

### 4.1 校准控制链路

原厂校准工具 `DT9837 Calibration.exe` 动态链接 `oldaapi32.dll`：

| 导出序号 | API 函数 | 底层 IOCTL | USB 传输对应 |
| :---: | :--- | :--- | :--- |
| 304 | `olDiagWriteCalPot` | `WRITE_CAL_POT` | Command 50: `[ChipNum, PotNum, RegNum, DataVal]` |
| 305 | `olDiagReadCalPot` | `READ_CAL_POT` | Command 51: `[ChipNum, PotNum, RegNum]` $\to$ 回读 1 字节 |
| 309 | `olDaSetCouplingType` | `SET_COUPLING` | 配置输入通道 AC/DC 耦合 |
| 311 | `olDaSetExcitationCurrentSource`| `SET_CURRENT_SRC` | 配置 4 mA IEPE 恒流源 |

### 4.2 数字电位器物理地址映射

通过对校准程序数据段分析，还原出硬件校准芯片与通道映射关系：

| 逻辑参数项 | 物理芯片号 (`ChipNum`) | 电位器通道 (`PotNum`) | 调节对象 |
| :--- | :---: | :---: | :--- |
| `AD0_OFFSET` ～ `AD3_OFFSET` | 0 | 0 ～ 3 | 模拟输入 AI0～AI3 硬件直流零偏微调 |
| `AD0_GAIN` ～ `AD3_GAIN` | 1 | 0 ～ 3 | 模拟输入 AI0～AI3 硬件增益微调 |
| `DA0_OFFSET` | 2 | 0 | 模拟输出 AO 硬件直流零偏微调 |
| `DA0_GAIN` | 2 | 1 | 模拟输出 AO 硬件输出增益微调 |

> [!NOTE] 校准保护机制
> 硬件校准值直接烧录在硬件寄存器与 EEPROM 槽位中。LabDAQ 软件调零仅在数据流层面扣除实时静态均值偏置，绝不调用 Command 50 写入硬件电位器，确保出厂校准安全。

---

## 5. HPF 格式规范与解析逻辑

### 5.1 Chunk 分块文件结构

HPF 文件由连续的 64 KiB 对齐数据块构成，各块头部包含 8 字节 `chunkID` 与 8 字节 `chunkSize`：

| 块标识 (`chunkID`) | 块类型 | 内容定义 |
| :---: | :--- | :--- |
| `0x1000` | Header Chunk | ASCII 签名 `datx`，创建时间 XML 元数据 |
| `0x2000` | Channel Information | 通道名称、工程单位、采样率、量程、灵敏度 XML |
| `0x3000` | Data Chunk | 原始连续采样数据（Double / Int32 / Int16） |
| `0x6000` | Index Chunk | 全局各数据块文件内绝对偏移索引表 |
| `0x7000` | QuickDAQ Settings | QuickDAQ 界面布局、显示范围与图表颜色 XML |

### 5.2 离线只读实现特性

LabDAQ 在 `labdaq/hpf.py` 中实现了 HPF 规范的只读解析子集：
- 支持 `RandomDataChannel / Double` 格式波形直接读取；
- 按数据时间窗动态 `seek` 寻址，无需一次性加载数吉字节文件；
- 导出时保留完整的通道元数据与原厂采样率参数。

---

## 归档导航

- [返回历史档案总索引](index.md) · [全局文档中心](../README.md) · [项目首页](../../README.md)
- 正式通信规范：[进程间通信与硬件控制技术规范](../技术规范/ipc-protocol-spec.md)
