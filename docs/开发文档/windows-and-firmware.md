# Windows 原厂包、固件与独立 USB 接入分析

分析日期：2026-10-11。对象：OMNI 7.8.9、QuickDAQ 3.7.0.49、libuldaq 1.2.2、DT9837A/B/C 共用通信链路。本机没有硬件。本轮执行了**离线解包、PE 元数据和导入表读取、选定函数的反汇编、固件记录比对、HPF 样例结构读取**；没有执行原厂安装器、安装驱动、修改设备或完成全部二进制反编译。

## 1. 可以直接使用的结论

- Windows 与 Linux 包中的两段 FX2 固件记录逐字节相同。独立 Windows 实现不需要猜测或重写设备固件即可研究冷启动；设备内部 FPGA、ADC、DAC 固件/逻辑仍沿用已有硬件设计。
- 原厂 Windows 包确实是“冷设备固件加载驱动 + 热设备采集驱动 + 用户态库”的两阶段设计。WinUSB 方案也需要同时考虑两个 PID，不能只让已被原厂初始化的热设备工作。
- 硬件校准通过专门的电位器读写命令完成。Linux 软件层的斜率 1、偏移 0，不代表仪器未经校准；软件调零、传感器换算、硬件校准应分开。
- Linux 源码所述 Windows 10 上电复位 workaround 仍需要实机核实。我们已定位 2019 驱动的上电函数和 EEPROM 读取，但未在这条局部调用链中证明自动写 `powerAlwaysOn=1`。
- 原厂 HPF 有公开格式说明；两个官方样例包含明文 XML 元数据。后续无需把“兼容 HPF”与“依赖原厂驱动”绑定。但本轮原型不把试验性解析当成已完成兼容。

## 2. 已提取的文件及位置

全部位于 `resources/vendor/extracted/`。此目录保留原厂资料供本地学习，不是新程序的运行依赖，也不是原厂文件的重新授权。`program files`、`LocalAppData` 是从 MSI 表恢复的目录名，并没有写入本机对应系统位置；OEM 根目录的 DLL 原目标是系统 DLL 目录。

| 目录/文件 | 用途与核实结果 |
| --- | --- |
| `oem-x64/`、`oem-x86/` | 相应位数的 DT9837A 系列用户态 DLL、驱动 INF/SYS/CAT、Open Layers API/内存库 |
| `oem-*/program files/Data Translation/DeviceDrivers/DT9837A/` | 原厂常规签名包 |
| `oem-*/program files/Data Translation/DeviceDriversEV/DT9837A/` | 原厂 EV 目录中的对应包，保留与普通版的区别 |
| `oem-*/DT9837A.dll` | 7.0.0.15；包含 `Dt9837CDlg` 和 `Power Always On` 字符串，C 型号确实在该 A 系列库中处理 |
| `oem-*/Dt9837ACollection.dll` | 7.0.0.0；设备集合层，不能按文件名误判为仅支持 A |
| `sdk-reference/program files/Data Translation/Win32/SDK/Include/` | 完整 SDK 头文件，包括 `OLDAAPI.H`、`OLDADEFS.H`、`DtIoctl.h`、`DtDeviceInterface.h` |
| `sdk-reference/program files/Data Translation/Win32/SDK/Examples/ContAdc/` | 官方 C 连续采集源码及项目文件 |
| `sdk-reference/program files/Data Translation/Win32/SDK/Examples/IepContAdc/` | 官方 IEPE 连续采集例程 |
| `sdk-reference/program files/Data Translation/DotNet/OLClassLib/Examples/AnalogInput/` | `ReadBufferedDataAsVolts`、`ReadBufferedIepeDataAsRaw` 的 C#、VB 源码及项目 |
| `sdk-reference/program files/Data Translation/Calibration/DT9837 Calibration.exe` | 32 位、6.7.4.7；原厂仪器校准工具，仅静态分析，未运行 |
| `quickdaq/program files/Data Translation/QuickDAQ/` | QuickDAQ.exe、信号分析/波形发生程序集、原厂说明；其他依赖按 MSI 目标目录另存 |
| `quickdaq/LocalAppData/Data Translation/QuickDAQ/Data/` | 两个官方 HPF 样例；不是本实验室或 DT9837C 的实测数据 |
| `hpf-reference/DT-HPF-format-spec-22760-RevA-mirror.pdf` | Data Translation 2007 年格式说明的第三方保存副本；来源见第 7 节 |

PE Machine 字段核实 x86 为 `0x014C`，x64 为 `0x8664`。原厂采集 SYS 和 loader SYS 均为 7.0.0.9；INF `DriverVer=01/14/2019,7.0.0.9`，`KmdfLibraryVersion=1.9`。两个 SYS 导入 `ntoskrnl.exe` 和 `WDFLDR.SYS`；设备 DLL 导入 `oldaapi32/64.dll` 等原厂及系统组件。校准程序还依赖 `MFC71.DLL`、`MSVCR71.dll`，不能假定提取后即可在新系统完整运行。

对 EV x64 采集 SYS 执行 Windows `Get-AuthenticodeSignature`，本机返回 `Valid`，签名主体为 Measurement Computing Corporation。该结果只说明该文件的本机签名验证结果，不代表所有文件均已逐一验签或已通过新系统安装测试。

### 离线提取方法

1. 7-Zip 25.01 解开 OMNI ZIP，取出 `OEM Setup/SetupOem.exe`、`Ol.NET/setup.exe`。
2. 三个原厂外层 EXE 的 PE overlay 都是 `ISSetupStream`（格式类型 2），直接 `7z l` 无法打开。
3. 使用作者官方发布的 [ISx v0.3.11](https://github.com/lifenjoiner/ISx/releases/tag/v0.3.11) 只读取文件，解出 MSI 及嵌套 x64 OEM EXE；不执行厂商 EXE。工具 [源码和说明](https://github.com/lifenjoiner/ISx) 使用 MIT 许可证。
4. 再对嵌套 x64 OEM EXE 使用 ISx；7-Zip 解 MSI 内 `Data1.cab`，继续解 CAB。
5. Windows Installer COM `OpenDatabase(path, 0)` 只读查询 `File`、`Component`、`Directory` 表，把 CAB 中经过缩短/带 GUID 的内部标识恢复为真实文件名和目录。没有执行 MSI 安装事务或自定义动作。

本方法绕开了原厂启动器 1152 解包错误，已经取得所需文件；**不等于修复或验证了原厂安装器**。复现入口见 `tools/extract_vendor.ps1`。工具下载、原始 CAB、反汇编文本等工作文件属于 `.work/windows-analysis/` 暂存，可在本轮结束后转入用户指定的 Delete 目录；正式提取文件不依赖暂存运行。

## 3. 从冷插入到采集：Windows 与 Linux 对照

### USB 身份和原厂分层

`DT9837a_Kmdf.inf` 对 x86/x64 均声明：

| 状态 | 硬件 ID | 原厂处理者 |
| --- | --- | --- |
| 未加载固件 | `USB\VID_0867&PID_9839` | `DT9837ALd_x86.sys` / `DT9837ALd_x64.sys`；服务描述 Firmware Loader |
| 固件运行中 | `USB\VID_0867&PID_3998` | `DT9837Ak.sys` / `DT9837Ak_x64.sys`；服务描述 USB Client Driver |

A/B/C 共用这些 ID；热态需读 EEPROM 型号字段进一步区分。Linux 源码对应 `DaqDeviceId.h`、`Usb9837x::getVirtualProductId()`。不能仅凭 `3998` 把一台 A/B 当作 C，也不能把不带后缀的原始 DT9837 驱动当作 A/B/C 驱动。

### 固件同一性：已完成的具体比对

`IntelHexRec.h` 定义每条记录为 Length(u8)、Address(u16)、Type(u8)、Data[16]。按原厂 PE 内存布局补入对齐字节后，每条是 22 字节。解析 Linux 两份 C 数组，把完整记录序列（含末尾 type 1 终止记录）与原厂 loader SYS 的原始字节搜索比对。

| 映像 | 总记录数（含终止） | 整段字节数 | x64 SYS 文件偏移 | x86 SYS 文件偏移 |
| --- | ---: | ---: | ---: | ---: |
| `DT9837A_FW.c` | 1035 | 22770 | `0x1540` | `0x13D0` |
| `DTFX2LDR_FW.c` | 60 | 1320 | `0x6E50` | `0x0E80` |

`DeviceDrivers` 与 `DeviceDriversEV` 两个目录的对应 x86/x64 loader 中，上述整段都精确匹配。这比“文件名相同”强，证明这些版本所带的两段记录相同；不是对未来包或所有历史版本的推断。

固件文件在 Linux 项目总体 MIT 分发树内，但 `DTFX2LDR_FW.c` 仍带早期 Anchor Chips 版权提示。保留原始文件及版权文字，发布固件副本前单独核实适用许可；无需为本地协议分析改写或删除任何许可声明。

### Linux 实际加载顺序

依据 `src/usb/fw/DtFx2FwLoader.cpp`：

1. 枚举 VID `0x0867`、冷 PID `0x9839`，打开并 claim interface 0。
2. `reset8051(1)`：使用 vendor request `0xA0` 写 `0x7F92` 和 `0xE600` 两个 CPUCS 地址。
3. 下载 `DT_FX2LDR_FW_Image`，释放复位，等待 1 ms。
4. 下载 `DT9837A_FW_Image`：地址高于 `0x1B3F` 的记录先通过 `0xA3` 写外部 RAM；再停止 8051，通过 `0xA0` 写其余内部 RAM 记录。
5. 再复位/释放 8051；关闭旧句柄，等待重新枚举。原库整体等 5 秒后继续设备发现。

这是 RAM 固件加载路径，不是 EEPROM 固件升级。本实现里厂商 control transfer 是设备接收者，不需要通过“USB总线复位”来写 CPUCS。Windows WinUSB 对总线复位的限制不能直接推出冷启动无法实现，也不能推出实际设备必然成功。

独立实现需检查每条传输的返回码和实际写入长度，再通过状态/枚举完成而不是盲目重用旧句柄。原库 `downloadFirmware()` 中多处返回值未一路向上传递，不能原样照搬成“任何情况都成功”的实现。热 PID 已出现时跳过下载，只做型号和端点核实。

### 热态端点

固件 USB 描述符与 `Usb9837xDefs.h` 一致：一个配置、一个 interface，五个 bulk 端点：`0x01` 命令发送、`0x81` 命令返回、`0x82` ADC 数据、`0x06` DAC 数据、`0x88` 异步状态。高速模式流端点最大包 512 字节，命令端点 64 字节；最终仍以实机 descriptor 为准。`0x88` 是 bulk，不应凭“状态消息”就实现成 USB interrupt 端点。

## 4. Windows 上电差异：已经看到什么

Linux `Usb9837x.cpp:77` 的注释记载 Windows 10 在 EEPROM powerAlwaysOn 未设时发送 power-on 可能复位，原厂 kernel driver 曾通过先写 EEPROM workaround。对应源码函数读写 I2C 地址 `0x50`、偏移 `0x40`；当前 Linux 初始化将那段写入调用注释掉，只发命令 52，等 100 ms，再启动消息线程。

UM9837 第 142–143 页独立确认 A/B/C 上电配置：耦合 `0x20..0x23`、电流源 `0x24..0x27`、high-power override `0x40`，Windows API 映射基址为 `0x100000`。旧版无后缀 DT9837 在手册前一节使用不同 EEPROM 定义，不能混用。

对 `DeviceDrivers/DT9837A/DT9837ak_x64.sys` 作局部反汇编（PE ImageBase `0x10000`）：

- VA `0x1D234` 的函数在 `0x1D270` 构造 32 位 command `0x34`，下一字节为 power 状态，发送 64 字节。与开源 `DATA_ACQ_POWER=52` 对应。
- VA `0x1DEB8` 的上电包装函数检查缓存的 power 状态，置 1 后调用上函数，再通过 `KeDelayExecutionThread` 等约 100 ms；与 Linux 等待值对应。
- VA `0x1E2DB..0x1E31A` 可看到对 I2C `0x50` / register `0x40` 的读取和据返回值处理默认耦合/电流源的分支。
- DLL 中存在 `Power Always On`，SYS 中存在 `DontPowerDown` 字符串。

**尚未证实**：当前 2019 SYS 在所有初始化路径上会自动写 `0x40=1`。上述已定位上电包装函数中未出现该写入；可能是版本差异、其他初始化路径或设备已保存设置，需要抓取真实冷启动解决。不能把源码注释直接说成“已反编译证实这版 Windows 驱动就是如此”。

首轮独立连接只读这个 EEPROM 字节并记录结果。常规启动不自动修改 EEPROM；若命令52后发生再枚举，保持明确状态并终止当前采集请求。确需 workaround 时，应先保存原值，在专门设备维护操作中处理。软件测试能够模拟“上电导致断开/再枚举”，不能证明某个真实 USB 控制器不会出现问题。

## 5. 校准命令与槽位：从 EXE 到 SYS 的证据

### 已证实的 API/命令链

原厂 `DT9837 Calibration.exe` 为32位：读取其导入表并对照 `oldaapi32.dll` 导出表：

| 导入序号 | 函数 | 校准 EXE IAT VA |
| ---: | --- | --- |
| 304 | `olDiagWriteCalPot` | `0x407488` |
| 305 | `olDiagReadCalPot` | `0x40748C` |
| 309 | `olDaSetCouplingType` | `0x4074A8` |
| 311 | `olDaSetExcitationCurrentSource` | `0x4074A4` |

公开 `OLDAAPI.H` 原型为 `WriteCalPot(hDev, ChipNum, PotNum, RegNum, Data)`，读函数末参数为数据指针。`DtIoctl.h` 定义对应 `WRITE_CAL_POT` / `READ_CAL_POT` IOCTL；这只是原厂用户态到内核态接口，我们的新 USB 层不必保留原厂 IOCTL。

原厂 x64 SYS 中 VA `0x1CE91` 写 command `50`，payload 连续为 ChipNum、PotNum、RegNum、DataVal 四个单字节；VA `0x1CD7E` 写 command `51`，payload 为前三字节，随后读取一字节。与开源 `Usb9837xDefs.h` 的枚举和结构吻合。

### 已还原的电位器映射表

校准 EXE 的 `.data` 表保留了名称指针，以下不是凭模拟信号推测：

| 表 VA | 逻辑项 | ChipNum | PotNum |
| --- | --- | ---: | ---: |
| `0x40B040` 起4项，每项8字节 | `AD0_OFFSET` … `AD3_OFFSET` | 0 | 0…3 |
| `0x40B060` 起4项，每项8字节 | `AD0_GAIN` … `AD3_GAIN` | 1 | 0…3 |
| `0x40B080` | `DA0_OFFSET` | 2 | 0 |
| `0x40B088` | `DA0_GAIN` | 2 | 1 |
| `0x40B090` | `TACK_ADJ`（程序原拼写） | 2 | 2 |

最后一项来自通用系列工具，不能据此给没有转速计输入的 C 型号添加此功能。

手动调节路径（例如 EXE VA `0x401296`、`0x4012BF`）先写 `RegNum=0xFFFFFFFF`，再写 `RegNum=0`，值被限制在0…255；传到 USB 的寄存器字段为一字节，因此前者是 `0xFF`。恢复路径（`0x401D92..0x401DCB`）先读 `RegNum=1`，再把得到的值写到 `0xFF` 和 `0`。这些观察支持“当前电位器值、用户保存值、工厂保存值”的槽位解释，但物理电位器型号、每个槽位的精确持久性和写等待时间尚未由芯片资料/实机证实。

手册第111–116页说明出厂已校准、每路有独立增益/偏移调整；AI校准用精密直流源，C型号AO校准是约±2.75 V，与A型号±9.375 V不同。不能把系列工具里的9.375 V通用提示用于±3 V的C输出。

本轮独立原型只做原始电压→传感器单位换算和软件调零；不发送命令50。下一步可在真实设备上只读工厂/用户/当前槽位，并用原厂与独立路径采同一已知输入，比对实际码值/电压。仪器校准应保留为维护功能，普通“调零”不能偷偷写这些槽位。

## 6. WinUSB 长期维护方案

建议运行链：中文应用 → 自有协议与采集状态机 → libusb 的 Windows 后端（或原生 WinUSB API）→ Windows 自带 `Winusb.sys` → DT9837C。这里摆脱的是原厂专用驱动/SDK，仍使用操作系统通用USB驱动。

- **绑定两种硬件 ID**：发行 INF同时声明冷 PID9839 和热 PID3998，并注册新程序的设备接口 GUID。Windows没有原厂 loader时，必须先能打开冷设备，才能进入热态。不能假定老固件自带WinUSB自动安装所需的Microsoft OS描述符。
- **再枚举就是新实例**：保留USB物理端口路径辅助关联，释放旧handle，按目标VID/PID及端口找回；启动PID可能没有稳定序列号，多设备时避免只选“列表第一台”。型号/序列号在热态再读取确认。
- **用户态单一拥有者**：WinUSB不支持多个应用同时使用同一设备。由一个采集进程拥有接口并向界面/分析分发数据；QuickDAQ与独立采集不能同时操作该接口。恢复原厂绑定的路径应保留，驱动切换时先结束采集并物理断电重接。
- **不依赖总线复位**：WinUSB不支持真正设备USB reset，且只能使用第一配置。本固件描述符当前只有一个配置；CPUCS控制请求复位8051与USB总线reset不是一件事。发生无法恢复的真实状态时，第一版提示重新插拔，不用无限reset重试掩盖故障。
- **读取/取消**：ADC、消息端点分别持续读，控制命令序列化；buffer 生命周期跨越异步回调，取消后等待完成回调再释放。USB传输完成时间只用于诊断，样本时间轴使用硬件采样率和样本序号。
- **分发**：Windows自带Winusb.sys无需我们再写内核二进制；自有INF驱动包仍需要正确的CAT签名和管理员安装。正式发布使用Microsoft硬件开发者签名流程或合适的既有通用绑定工具；具体签名路线在交付时按当时要求确定。不能把“DLL免安装”说成“USB驱动绑定免安装”。

依据：[Microsoft WinUSB安装与INF说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation)、[libusb Windows限制](https://github.com/libusb/libusb/wiki/Windows)、[Microsoft驱动目录签名](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/catalog-files)、[驱动代码签名要求](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/code-signing-reqs)。本轮未下载/安装绑定工具、未改驱动、未关闭签名强制或Secure Boot。

拿到硬件后的最短核实顺序：原厂已知输入基线与设置 → 独立热设备识别/只读信息 → 小信号4路短采样 → 停止/重启 → 物理断电后的双PID冷启动 → 相同输入下原厂/独立码值对照 → 持续采样及拔线取消。能通过模拟协议的部分今天先验证；USB时序、电源复位、校准精度仍保留为实机工作。

## 7. HPF：可读元数据、样例与格式边界

官方样例从QuickDAQ MSI直接提取：

| 样例 | 大小 | 静态可读内容 |
| --- | ---: | --- |
| `QuickDAQ Data Logger Sample Data.hpf` | 393216 B | 10路，DT987x/DT9871电压/温度通道，TimeIncrement 0.1 s |
| `QuickDAQ FFT Analyzer Sample Data.hpf` | 655360 B | 两路，DT9837-B(02).Ain 0和DT9837-A(00).Ain 0，Double/V，TimeIncrement约1/15000 s |

两个文件都不是本机DT9837C数据。头`0x10`处是ASCII `datx`，`0x24`开始有RecordingDate XML；`0x10018`开始是`ChannelInformationData`，包含名称、单位、DataType、DataIndex、StartTime、TimeIncrement、量程、DataScale/DataOffset、SensorScale/SensorOffset。`0x20017`处是另一个UTF-8 XML文档，保存QuickDAQ布局和配置，根标签原拼写为`PersitantObjects`。

原厂格式说明确认：所有chunk以小端Int64 chunkID、Int64 chunkSize起始，chunkSize包含头和填充，大小对齐64KiB；不是每个chunk固定64KiB。头`0x1000`、通道`0x2000`、数据`0x3000`、事件定义`0x4000`、事件数据`0x5000`、索引`0x6000`。本样例还有`0x7000`的QuickDAQ设置扩展。Data chunk包含groupID、dataStartIndex、channelDataCount，以及各通道相对chunk起点的offset/byte length；数据块内部是按通道描述定位，不能直接当USB交错帧读取。

样例索引位置与头一致：Logger `0x50000`，FFT `0x90000`。Logger的设置chunk占2×64KiB，FFT的数据chunk占6×64KiB，这些都按头中chunkSize跳转核对到文件末尾。

另外按原厂说明离线定位Double通道数据：Logger每路101点，FFT每路20480点，所有值有限、描述偏移/长度均落在对应chunk范围内。FFT第一路出现−10 V及接近+10 V的值，已接近量程端点；不要把该样例当作精密无失真标定信号。此检查证明这两个样例的基础数据布局可读，尚未与QuickDAQ导出CSV逐点核对，也没有验证全部HPF变体。

`QuickDAQ.exe`包含`HPFInterface`、`HpfManager`、`ReadChannelInformationChunk`、`ChannelInfoChunkId`等托管符号；本次提取包中未发现独立命名的HPF reader DLL。这里仅报告PE/字符串观察，没有声称已反编译这些类或验证全部读写语义。

格式资料：Data Translation《DT High Performance File Format Specification》，Document22760 RevA，2007。旧官网链接本轮不可访问；本地PDF来自[作者说明及镜像仓库](https://github.com/abougouffa/m2hpf)，[PDF保存位置](https://github.com/abougouffa/m2hpf/blob/master/high_performance_file_format_spec.pdf)。这是原厂文档的镜像，不能把镜像项目的LGPL当作PDF或QuickDAQ的许可。[NI官方HPF DataPlugin页面](https://www.ni.com/en/support/downloads/dataplugins/download.data-translation-dataplugin-for-hpf.html)也确认存在独立导入路径。

### 本轮已实现的只读导入子集

`labdaq/hpf.py` 的 `open_hpf(path)` 已实现 HPF版本 `0x10001`、单一同步通道组、`RandomDataChannel/Double`、连续样本序号的多数据块、通道XML、索引及QuickDAQ设置XML。打开时读取块目录和元数据；`read(start,count)` 按所需窗口seek到各通道的字节区间，不反复加载完整文件，也不运行原厂DLL。通道按XML顺序输出，并通过DataIndex定位真实描述项。

同步电压回放仅接受单位V且DataScale/DataOffset/SensorScale/SensorOffset分别为1/0/1/0的已验证子集，返回原始V，不创建力标定。`voltage_compatible`、`voltage_replay_error`供界面先检查；对于Logger样例，读取器保留全部10路及原单位，`read_native()`可读原始数值，`read()`明确拒绝把温度/电压混合文件送入同步电压或力换算。软件没有丢弃多于4路的数据；界面通道数量上限单独提示。

`export_hpf_csv(path,output_path,chunk_size=4096)` 对兼容电压文件分块导出相对时间和所有通道原始V，首行JSON保留 `source=vendor_hpf`、原通道元数据和记录日期。不会把来源记成simulation，不添加原厂文件不具备的finalized/termination_reason，也不覆盖原HPF。

本子集会拒绝不支持的数值类型、多个group/不同采样率、独立时间通道、各通道长度不一、样本空洞、截断/错索引、事件/触发及其他未知扩展块。非恒等传感器换算保留为元数据、拒绝电压回放；这不是全格式HPF兼容实现。

验证：`tests/test_hpf.py` 的14项针对性测试通过，包括两个真正官方样例的名称/采样率/点数、若干固定数值及独立struct读取对照、跨块seek、DataIndex重排、截断和不支持格式拒绝、相对时间CSV及原文件不变。官方包未随源码分发时，两个厂商fixture测试自动skip；自造且符合公开格式的最小样例覆盖基础读写对照。仍未与QuickDAQ原程序导出的同段CSV逐点对照，不能由这14项测试推断所有历史HPF文件均兼容。

新采集会话仍保存自有原始数据与JSON元数据，不伪装成HPF；旧文件通过上述只读路径导入。


