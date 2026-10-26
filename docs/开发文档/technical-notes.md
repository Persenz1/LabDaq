# DT9837C：技术分析与后续测试准备

> 本文保留 2026-10-11 的历史研究与当时需求。当前交付已改为原厂驱动和 Open Layers C 后端，不再以摆脱原厂运行时为目标；实现和安装方式以[开发环境](开发环境.md)为准。

整理日期：2026-10-11。本文保存硬件背景与现场需求；本轮已形成 Python / PySide6 离线原型。最新实现入口见 [使用说明](../用户手册/user-guide.md)，完整协议及二进制分析见 [协议说明](../功能实现/protocol-details.md)和 [Windows/固件分析](windows-and-firmware.md)。

## 接口路线

| 路线 | 已获得的依据 | 对本项目的意义 |
| --- | --- | --- |
| Windows DataAcq SDK | 官方安装包、C/C++ 手册 | 作为对照参考；32 位和 64 位导入库分别为 `oldaapi32/olmem32` 与 `oldaapi64/olmem64`；不作为最终运行依赖 |
| Windows DT-Open Layers .NET | 官方安装包、完整类库手册 | 作为功能及行为对照参考，不作为最终运行依赖 |
| Linux/macOS libuldaq + Python uldaq | 已解开的官方源码、MIT 许可证、示例 | 独立协议层的主要研究依据；移植 Windows 需要处理系统依赖和 USB 绑定 |
| Windows 通用 USB 通信 | 已查阅 libusb Windows 与 Microsoft WinUSB 官方文档，并实现只读描述符枚举 | 候选路线：我们维护的协议实现 → libusb/WinUSB → 设备；真实采集后端尚未实现或实测 |

官方 [OMNI 参考入口](https://digilent.com/reference/software/dt-omni/start)与 [QuickDAQ 参考页](https://digilent.com/reference/software/quickdaq/start)供后续安装时查阅。QuickDAQ 当前参考页列出 Windows 11/10；本机具体系统构建、旧安装器和驱动能否配合仍需实测。

用户已明确最终软件需要摆脱原厂驱动，因此只封装官方 SDK 不满足目标。已据公开代码实现独立协议与模拟层，采用 Python / PySide6 构建原型；Windows 真实异步传输、绑定与物理时序仍需后续接机验证。

## 独立通信层的可行性初查（2026-10-11）

下面保留最初源码审查的线索。后续已离线提取 Windows 驱动，并完成部分函数反汇编、固件记录比较及校准映射分析，详见 [更新结果](windows-and-firmware.md)。通用 USB 驱动未安装。

- **识别和协议已有源码**：`src/DaqDeviceId.h` 定义启动 PID `0x9839`、运行 PID `0x3998`，DT VID 为 `0x0867`。A/B/C 共用 PID，`Usb9837x::getVirtualProductId()` 继续读取 EEPROM 识别 C 型号。`Usb9837xDefs.h` 给出命令、AI/AO 数据、异步消息端点及打包结构。
- **冷启动路径可研究**：`DtFx2FwLoader::downloadFirmware()` 使用包内 `DTFX2LDR_FW.c` 和 `DT9837A_FW.c` 的 Intel HEX 映像，经 USB 请求写入 RAM。PC 端驱动独立的同时，设备仍使用这些原厂固件映像；没有取得完整固件源工程。
- **主要采集功能已有实现**：输入、输出、IEPE、耦合、时钟、缓冲和触发分布在 `Usb9837x`、`AiUsb9837x`、`AoUsb9837x` 和 `DaqIUsb9837x` 中。功能与 Windows 版本是否完全等价尚未确认，例如 AO 触发/从模式能力有具体限制。
- **存在真实的平台差异**：`Usb9837x.cpp` 的 `initilizeHardware()` 注释记录 Windows 10 上电复位及原厂驱动的 EEPROM `powerAlwaysOn` 处理；Linux 对应调用被注释。当前只记录问题，不改写设备 EEPROM。
- **校准需单独厘清**：AI/AO 的 `load*Coefficients()` 在此版本设软件斜率为 1、偏移为 0。这不能证明设备没有校准，需要查明校准由硬件、固件还是 Windows 驱动的其他环节完成。
- **移植不是直接重新编译**：构建脚本只接受 Linux/macOS，代码使用 pthread、POSIX 时间/等待等接口。需要逐项适配线程、取消传输、设备重新枚举和错误恢复。

[libusb Windows 文档](https://github.com/libusb/libusb/wiki/Windows)说明其可使用 WinUSB 后端；[Microsoft WinUSB 安装文档](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation)说明可为设备绑定系统提供的 `Winusb.sys`。因此可先评估使用系统通用驱动、在用户态维护专用协议的方案，无须预先决定自行编写内核驱动。设备是否适配仍需实测；安装包绑定与签名也是实际交付工作。

后续对照分析聚焦原厂设备相关 `.sys` / `.dll` 的启动顺序、USB 请求、数据格式、校准和 Windows 特殊处理。原厂采集链路先提供参考记录，再验证独立实现；切换为 WinUSB 绑定后，原厂软件可能无法继续直接访问同一设备接口。

独立性的关键检查是：在没有安装原厂驱动/SDK 的 Windows 环境，设备完全断电后重新连接，仍能完成初始化、连续采集、停止和再次采集。该检查尚未进行。

## 实物信号链及关键参数

用户照片显示：输入 → IEPE/耦合/增益 → 四路独立 ADC → FPGA → 输入 FIFO → USB；输出由另一条 DAC 链路完成。

设备面板照片仅在本地 `resources/hardware/DT9837C-front-panel.jpg` 保留，不随代码仓库发布。

下列参数来自 [DT9837 官方手册](https://files.digilent.com/manuals/UM9837.pdf)第 19–20、68–70、118–127 页及相应开源实现。须查 **C 型号**对应列。

| 项目 | DT9837C |
| --- | --- |
| 模拟输入 | 4 路，单端、同时采样，24 位 Σ-Δ ADC |
| 输入量程 | ±10 V（×1）；±1 V（×10） |
| 连续采样率 | 约 195.3–105,469 S/s/通道；实际时钟值应从 API 读取 |
| 输入耦合 | 每路可选 AC / DC；AC 高通 −3 dB 点为 1 Hz |
| IEPE | 每路可启停的 2 mA 激励；手册列 compliance voltage 为 18 V |
| 输入阻抗 | 1 MΩ，20 pF |
| ADC 编码与延迟 | Offset binary；滤波群延迟 39/fs 秒；手册说明厂商软件已处理有效数据对齐 |
| 模拟输出 | 1 路、24 位、±3 V；流式更新约 10–96 kS/s |
| 转速计 | C 型号没有 A/B 型号的转速计输入 |
| 触发/同步 | 软件、外部数字、模拟阈值；RJ45 用于时钟/触发同步，不是网络端口 |
| 校准工具 | 原厂工具在 Windows 下提供；接线和步骤见手册第 111–116 页 |

机身照片中的电容/开关和 ×1/×10 分别对应耦合与增益。框图足以帮助理解软件设置，本项目不需要复刻电路或硬件。

对慢速力测量，应先核实传感器/调理器输出类型。调理后的电压信号通常以 DC、关闭 IEPE 测试；真正的 IEPE 传感器按其规格设置。4–20 mA、桥式 mV/V、普通电压和 IEPE 是不同接口，当前照片不能确定接入的是哪一种。不能把手册中的过压保护值当作正常测量量程。

## 已下载源码从哪里读

根目录：`resources/opensource/libuldaq-1.2.2/`。

| 文件或目录 | 内容 |
| --- | --- |
| `src/usb/Usb9837x.cpp`、`.h` | 设备初始化、命令交换、时钟与寄存器操作 |
| `src/usb/ai/AiUsb9837x.cpp`、`.h` | ADC 能力、量程、采样、耦合、IEPE 与缩放 |
| `src/usb/ao/AoUsb9837x.cpp`、`.h` | C 型号模拟输出 |
| `src/usb/daqi/DaqIUsb9837x.cpp` | 扫描数据组织 |
| `src/usb/dt/Usb9837xDefs.h` | USB 端点、命令结构、寄存器和 EEPROM 定义 |
| `src/DaqDeviceId.h`、`src/usb/UsbDtDevice.cpp` | 型号识别；A/B/C 共用部分 USB ID，不能只凭 PID 判断 C 型号 |
| `src/usb/fw/DT9837A_FW.c` | 分发包附带的固件映像数据；文件名不等于完整固件工程 |
| `src/uldaq.h` | 公共 C API |
| `examples/AInScan.c`、`AInScanIepe.c`、`AInScanWithTrigger.c`、`AInScanWithEvents.c` | 连续采样、IEPE、触发与事件示例 |

Python 包：`resources/opensource/uldaq-1.2.3/`。优先读 `examples/a_in_scan.py`、`a_in_scan_iepe.py`、`a_in_scan_with_trigger.py`、`a_in_scan_with_events.py`，以及 `docs/hwov.rst`、`docs/hwref.inc`。

发现一个具体的示例适配问题：Python 通用 `a_in_scan_iepe.py` 默认 `rate = 100`，而 DT9837C 的硬件扫描下限约为 195.3 S/s。该示例还默认启用 IEPE 和 AC。它适合学习调用流程，实际测试前需要按本设备及传感器调整。原始源码保持原样。

官方在线能力表：[C API](https://files.digilent.com/manuals/UL-Linux/c/dt9837.html)、[Python API](https://files.digilent.com/manuals/UL-Linux/python/hwov.html)。公开表与版本实现有细节差异时，应结合安装版本能力查询和实机确认。

## Windows 示例与 QuickDAQ 学习内容

Windows 示例已离线提取至 `resources/vendor/extracted/sdk-reference/`，尚未编译或运行。以下是手册给出的正常安装后路径和重点例程；提取文件保持相似目录结构，但没有写到系统安装目录：

- `C:\Program Files (x86)\Data Translation\Win32\SDK\Examples\`：`DtConsole`、`ContAdc`、`IepContAdc`。头文件 `OLDAAPI.H`、`OLMEM.H` 定义真实调用约定和类型。
- `C:\Program Files (x86)\Data Translation\DotNet\OLClassLib\Examples\AnalogInput\`：`ReadBufferedDataAsVolts`、`ReadBufferedIepeDataAsRaw`。学习设备发现、缓冲完成事件、重新入队、停止和释放。路径随安装架构可能变化。

依据：[DataAcq SDK 官方手册](https://files.digilent.com/manuals/UMDataAcq.pdf)第 13–18 页、[.NET 官方手册](https://files.digilent.com/manuals/UMOpenLayers.pdf)第 23–31 页。

QuickDAQ 可以无硬件读取已有文件；采集模式需要硬件。现已从安装包离线取得 Data Logger 和 FFT Analyzer 两个 HPF 样例，存于 `resources/vendor/extracted/quickdaq/LocalAppData/Data Translation/QuickDAQ/Data/`。独立原型已经实现限定子集的只读导入，未把原厂样例视作本实验室测量数据。

后续观察原软件时，先记录通道命名、灵敏度/单位、耦合、量程、采样率、开始/停止、波形、保存与导出行为；FFT/PSD 和双通道频响在确定用途后再讨论。保留 `.hpf` 及同一段 CSV，可以帮助确认导出的时间轴、单位和数据是否经过过滤。`.hpf` 是数据文件格式名，不表示使用了高通滤波。

## 回工位后需要补齐的材料与测试条件

明天（2026-10-12）继续时，优先取得：

1. 设备及 USB 数据线、至少一根 BNC 线。软件安装成功后先用原厂程序确认型号和通道。
2. 力传感器、放大器/调理器的型号、说明书、供电和输出规格，灵敏度及单位换算依据。
3. 旧电脑上实际使用的 QuickDAQ 配置、代表性 `.hpf` 文件和同段 CSV；已知实验时长、采样率及异常现象。
4. 如果工位已有信号发生器和万用表，可用于已知直流/正弦输入；也可讨论 DT9837C 自身输出回接输入作初步连通性检查。回接共用硬件与时钟，不能代替独立精度校准。

第一轮实测以原厂软件建立基线：输入类型匹配后，从小信号、±10 V 量程开始，查看实际采样率、已知电压、时间序列长度及四通道记录。采集有限时长后保存 HPF 和 CSV。连接多台设备、最高速长时间采集和写校准参数，不是第一轮必须做的工作。

**已确认的需求**：最终运行不依赖原厂 Windows 专用驱动或 SDK；完整保留现有能力，常用操作简化、高级功能集中管理；中文界面，提升交互、易用性和美观程度；当前主要测力，通常使用 3–4 路，未来可能增加其他传感器。用户目前指出界面陈旧和英文门槛，具体操作痛点留待原型讨论。

**还需细化的事项**：主要实验及信号带宽；是否只做 Windows；连续记录时长；力传感器的单位、灵敏度及标定方式；FFT/PSD/FRF 等保留功能的入口与常用程度；旧 HPF 读取需求；运动平台/STM32 同步的范围。功能保留目标已定，具体实现与交付顺序仍需讨论。
