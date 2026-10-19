# DT9837C 协议、初始化与模拟器

本页依据项目中归档的 **libuldaq 1.2.2** 源码编写，日期为 2026-10-11。所有寄存器和端点结论来自公开源码；今天没有连接实物。源码支持可以显著减少未知项，但不能替代 USB 抓包、电压基准和断电重连验证。

**2026-10-19 实现更新：** Rust 已实现独立实机输入，入口为 `core/src/usb_input.rs`；`dt9837.rs` 负责协议、PLL 和字节组帧，`usb_stream.rs` 负责 8 笔预提交 ADC 读取，`usb_boot.rs` 负责冷态 RAM 加载。采样流与状态端点分别处理，命令串行；停止时取消未完成读取并等待回调后释放内存，已接受的完整扫描进入原始记录。异步完成按提交次序组装，失败/取消之后的尾部不拼成连续数据。记录包含原始 V、真实来源、设备身份、寄存器回读及结束原因；没有设备扫描计数器，因此不能声称逐扫描验证了硬件无丢点。下文的“原型未开放”描述保留为早期协议研究记录，当前用法与实测范围以[开发环境](../开发文档/开发环境.md)为准。

当前原型的协议路径为：

```text
采集界面 / 工作线程
  → DaqSession.configure/start/read/stop
  → 64 字节二进制命令、端点地址
  → SimulatedTransport 解析命令 / 寄存器 / PLL
  → 模拟电压 → 24 位量化 → USB 字节分段
  → DaqSession 组帧 / 丢弃启动延迟 / 码值转电压
  → 通道换算、曲线、保存
```

模拟设备**没有接收主机配置对象或直接向界面返回浮点电压**。它只从通信命令推导通道、增益、耦合、时钟等设置。这样能检验主机和设备之间真正容易出错的字节序、状态、分段和通道映射。

## 1. 设备身份与冷启动

| 项目 | 源码值 | 含义 |
|---|---|---|
| USB VID | `0x0867` | Data Translation |
| 上电加载态 PID | `0x9839` | 等待 FX2 固件 |
| 运行态 PID | `0x3998` | A / B / C 共用，不能直接断言为 C |
| EEPROM I²C 地址 | `0x50` | 设备身份、序列号和默认设置 |
| EEPROM `0x05..0x06` | 16 位小端 subtype | A=`1`，B=`2`，C=`4` |
| EEPROM `0x08..0x0B` | 32 位小端整数 | 序列号 |
| EEPROM `0x20..0x23` | 每通道耦合默认值 | 原型不读取 / 写入真实 EEPROM |
| EEPROM `0x24..0x27` | 每通道电流源默认值 | 禁用、内部或外部 |
| EEPROM `0x40` | powerAlwaysOn | 与 Windows 上电重置问题有关 |

源码位置：`src/DaqDeviceId.h`、`src/usb/UsbDaqDevice.h`、`Usb9837x::readIdentifier/readSerialNumber/getVirtualProductId`。

`DtFx2FwLoader::downloadFirmware` 为 9837 A/B/C 选择同一 `DT_FX2LDR_FW_Image` 加载器和 `DT9837A_FW_Image` 固件映像。名字中的 A 不代表不支持 C。流程是：

1. 在加载态发现设备，claim interface 0。
2. 对 EZ-USB `0x7F92` 和 FX2 `0xE600` CPUCS 地址使用 vendor OUT request `0xA0` 写入 reset=1。
3. 加载辅助 loader，释放 8051 reset，等待 1 ms。
4. 把设备固件按 Intel HEX 记录写入 RAM：地址大于 `0x1B3F` 的外部记录用 `0xA3`，内部记录用 `0xA0`；内部写之前停止 8051。
5. 再 reset / release，关闭旧 USB handle，等待约 5 s，按运行态 PID 重新枚举。

这些是 RAM 固件加载步骤，不等于拥有固件源工程。源码中另外的独立 FPGA 下载段被注释，不能把它误认为本机型已实现的流程。当前原型没有执行或实现硬件固件下载，也没有调用真实设备 vendor control transfer。

`Usb9837x::initilizeHardware` 记录了一个具体 Windows 10 问题：若 EEPROM 的 always-on 标志未设置，`DATA_ACQ_POWER(true)` 会导致设备重置；旧 Windows 内核驱动的处理是先写 EEPROM 标志。长期维护版需要验证我们这台 C 的表现，并选择能维护、能恢复的上电流程，不能在第一次接机时静默修改 EEPROM。

### 已实现的冷启动离线验证

`labdaq/firmware.py` 现在可以读取归档 `DTFX2LDR_FW.c` 和 `DT9837A_FW.c` 的 `INTEL_HEX_RECORD` C 数组，生成 `BootOperation` 序列，并交给 `BootloaderSimulator` 验证传输顺序。它只维护内存中的 RAM、CPUCS 和阶段，不调用 USB、不安装驱动、不写 EEPROM，也不执行固件机器码或 FPGA。

这与 `.hex` 文本文件不同：C 记录包含 Length、Address、Type 和 Data[16]；只有 Length 个字节被传输，省略的 C 数组元素按零补齐，type=1 记录终止遍历。`0x1B3F` 是源码使用的**记录起始地址分界**，高于它的记录先 A3 写入外部 RAM，然后停止 8051，再 A0 写入内部 RAM。两个 CPUCS 地址的重复 reset 操作也按源码保留，没有为了缩短步骤而合并。

实际归档资源的离线结果：

| 项目 | 结果 |
|---|---:|
| loader 记录数，含终止 | 60 |
| runtime 固件记录数，含终止 | 1035 |
| 实际数据记录 | 1093 |
| CPUCS reset 控制传输 | 12 |
| 全部控制传输 | 1105 |
| 全部操作，含等待/再枚举 | 1108 |
| 内部 RAM 写入数据字节，含先后覆盖 | 7543 |
| 外部 RAM 写入数据字节 | 7794 |
| 等待时间记账 | 1 + 5000 ms，不实际 sleep |
| 模拟 PID | `0x9839 → 0x3998` |

内部 RAM 最后保留 6879 个地址；它少于写入字节数，因为 loader 和运行固件覆盖了部分相同地址。该差异是两阶段加载的正常结果。两份 C 数组与归档 Windows loader 的独立二进制比对结果见 [Windows 与固件分析](../开发文档/windows-and-firmware.md)。

模拟器对第一次释放复位后“loader 已运行”、第二次释放后“runtime 等待再枚举”的判断是**已知启动流程的状态模型**，不是执行 8051 指令得到的结果。模拟等待不会验证真实时序、供电、USB 再枚举稳定性或 FPGA 加载成功。真实 transport 尚未启用。

程序接口：`run_boot_simulation(loader_source_path, device_source_path)` 返回便于设备页显示的摘要字典，包括 `hardware_access=False`、记录数、传输数、RAM 字节数、PID 和阶段。API 不内置或复制原固件映像；归档文件路径需显式传入。

## 2. USB 端点和包格式

来源：`src/usb/dt/Usb9837xDefs.h`、`Usb9837x::Cmd_*`、`UsbDtDevice::startMsgReader`。

| 端点 | 方向 | 内容 |
|---|---|---|
| `0x01` | Host → Device | 64 字节命令 |
| `0x81` | Device → Host | 读命令返回的裸数据 |
| `0x82` | Device → Host | 连续 A/D 数据流 |
| `0x06` | Host → Device | D/A 流 |
| `0x88` | Device → Host | 异步状态消息；源码实际用 bulk transfer |

`USB_CMD` 使用 `#pragma pack(1)`。开头为 4 字节 opcode，后面是最大 60 字节的联合体，整个命令 64 字节，未使用字段清零。Windows / 当前 Linux x86 的 opcode 和子系统号是小端 uint32；**Local Bus 地址、数值和掩码是大端 uint16**。不能把整包统一按一种字节序解码。

| 命令 | 十进制 opcode | payload |
|---|---:|---|
| READ I²C multiple | 12 | `count:u8` + N × `device:u8, offset:u8` |
| WRITE I²C multiple | 13 | `count:u8` + N × `device:u8, offset:u8, value:u8` |
| READ local word | 20 | `address:u16 BE`；回复 `value:u16 BE` |
| WRITE local word | 21 | `address:u16 BE, value:u16 BE` |
| MODIFY local word | 24 | `address:u16 BE, changed_bits:u16 BE, new_value:u16 BE` |
| START subsystem | 30 | `subsystem:u32 LE, trigger_flags:u16 LE` |
| STOP subsystem | 31 | 同上 |
| READ single ADC | 32 | `channel:u8, gain:u8`；回复 `code:u32 LE` |
| DAC FIFO size | 34 | `size:u32`，具体处理见源码，当前未实现 |
| WRITE PLL multiple | 40 | `subsystem:u32 LE, device:u8, count:u8` + N × `(reg:u8, value:u8)` |
| POWER | 52 | `enabled:u8` |
| WRITE single DAC | 53 | `subsystem:u32 LE, channel:u8, value:u32 LE` |

例如把 control register 2 的 `0x0F00` 位替换成 `0x0500`：

```text
18 00 00 00  00 02  0F 00  05 00  [54 个 00]
opcode=24   addr   mask   value
```

原函数参数名叫 `AndMask`，但注释明确定义为“要修改的位”：`new = (old & ~mask) | (value & mask)`。按普通 AND 保留掩码理解会改坏其他功能。

**没有发现命令序号、校验和、通用响应状态头或写命令 ACK。** 读命令返回指定长度的裸数据；写命令以 USB transfer 的完成状态判断传送结果。不能设计一个虚构 ACK / sequence 包再声称与设备兼容。运行期间命令和响应应成对串行处理，异步消息使用单独端点。

## 3. 通道、量程、耦合、IEPE

来源：`DaqIUsb9837x::configureCGL`、`AiUsb9837x::configureIepe`。

| 寄存器 | 位 | 含义 |
|---|---|---|
| control 0 `0x00` | `[1:0]` | 独立=`00`；slave=`01`；master=`10` |
| control 0 `0x00` | `[9:8]` | FPGA 时钟除数：2 / 4 / 8 / 16 |
| control 1 `0x01` | bit 0 | ADC 高速模式 |
| control 1 `0x01` | bit 1 | ADC 同步脉冲 |
| control 2 `0x02` | bits 0–3 | AI0–AI3 耦合，1=AC，0=DC |
| control 2 `0x02` | bits 4–7 | AI0–AI3 增益，1=×10/±1 V，0=×1/±10 V |
| control 2 `0x02` | bits 8–11 | AI0–AI3 内部恒流源，1=启用 |
| control 5 `0x10` | bits 0–3 | 选择进入 A/D 流的物理通道 |
| control 6 `0x20` | bits 0 / 2 | ADC overrun enable / error；初始化行为待实机核实 |
| FIFO `0x82` | word | 输入 FIFO 阈值 |

DT9837C 的输入流可以是任意升序、不重复通道子集，例如 `(0, 2, 3)`；**不是永远输出四路再在上层筛选**。流里没有通道 ID，必须按已配置的通道顺序解交织。C 不暴露 A / B 型中的三路 tach/counter；不能从同系列的示例搬出不存在的功能。

设置 AC 时先改耦合再开电流源；设置 DC 时先设置电流源再改耦合。代码保留这个官方时序顺序。当前软件默认 DC、IEPE 关闭。力传感器若经电压调理器输出，是否打开 IEPE 取决于传感器/调理器本身，不能把 IEPE 当成普通桥式测力传感器的供电。静态或准静态力应保留 DC 分量；AC 耦合不能通过后期算法找回已消失的静态力。

## 4. 采样时钟与实际采样率

来源：`Usb9837x::programClock/optimumPQF/CalculatePQValues`、`DaqIUsb9837x::configureClock`。

输入可请求范围在库中为 **195.313–105469 Hz / 通道**。不是把请求浮点值原样作为实际采样率。CY22150 PLL 使用 48 MHz 基准，按 P/Q、有源分频 DIV1 和 FPGA 除数生成采样时钟：

```text
Fs = 48,000,000 × Ptotal / Qtotal / DIV1 / ADC_ticks / FPGA_divider
ADC_ticks = 512（请求率 ≤ 52734 Hz）或 256（更高）
FPGA_divider = 2, 4, 8, 16
```

库搜索 Q=3..129，按整数误差评分选第一个最优解，再编码 P 的高低位和 Q 的奇偶位。原型移植同一算法，保存请求率与实际名义率；模拟设备则**从收到的 PLL 寄存器反解实际率**，没有直接复制主机的 `actual_rate`。

PLL 设备地址为 `0x69`；本路径使用寄存器 `0x0C, 0x47, 0x40, 0x41, 0x42, 0x44, 0x45, 0x46, 0x09`。48 kHz 的可手算向量是 P=512、Q=125、DIV1=4、ADC_ticks=512、FPGA_divider=2。编码 `P_B=0xFC`、`P_OQ=0x7B`、`ChargePump=0xC8`。96 kHz 使用相同 PLL 配置并把 ADC_ticks 改为 256。

原代码的初始化不是“寄存器写完立刻采”：

- 切换同步状态后等待 `128/Fs + 3 ms` 再发 ADC 同步脉冲。
- 高/低速模式切换后等待 `12288 / (Fs × ADC_ticks)`。
- 外触发配置还等待 `256/Fs`。
- 增益改变时要重设时钟，源码说明否则扫描开始会返回坏样本。

模拟器不等待这些墙钟延迟；真实实现必须补上，并检查 USB 缓冲预提交顺序。PLL 计算得到的是名义数字频率，仍包含实际晶振误差，不能代替实际时间基准校准。

## 5. ADC 字节流、启动延迟与物理量

来源：`DaqIUsb9837x::processScanData32_dbl`、`AiDevice::getCalCoef`。

每个样本是 **4 字节小端无符号数，24 位有效值右对齐，offset binary**。依据源码，读入 uint32 后直接按 24 位分辨率换算，没有算术右移或二进制补码符号扩展。±R V 的换算为：

```text
volts = raw_code × (2R / 2^24) - R
code 0x000000 → -R
code 0x800000 → 0
code 0xFFFFFF → R - 2R/2^24
```

这意味着 ±10 V 范围的 1 LSB 是约 1.1920929 µV，±1 V 是约 0.1192093 µV。有效精度、噪声和校准误差并不等于 24 位理想量化精度。原型将上 8 位非零视作协议异常，不静默截断掉潜在的帧错位。

纯模拟输入扫描默认丢掉每通道最初 **39 个扫描样本**。这与 ΔΣ 数字滤波群延迟有关；C 使用模拟阈值触发上升/下降沿时，源码明确设置丢弃数为 0。这一例外尚未实现，当前 session 只支持软件启动。USB 包边界不保证对齐扫描边界，原型支持在任意字节处分段，包括 7 字节片段；超时前已经收到的半帧保留，重新读取不会移动通道位置。

AI 校准系数在公开库的 `loadAdcCoefficients()` 被初始化为 slope=1、offset=0。**这不证明真实设备没有出厂校准**：校准可能已由固件/模拟硬件处理，也可能和 Windows 层有差异。真实设备需要用已知电压对比 QuickDAQ 和我们的原始码值，确认比例和偏置。

传感器换算应放在电压之后，保存原始电压以及灵敏度、零偏和符号。例如灵敏度为 S V/N 时，力 `F=(V-V_zero)/S`。这里的传感器标定、每次实验的调零、仪器自身的电气校准是三个不同操作。记录必须能恢复当时的配置。

## 6. 缓冲、消息和连续性

来源：`DaqIUsb9837x::configureFifoPacketSize`、`Usb9837x::messageHandler`。

库中 C 的 FIFO 大小为 2048 samples，允许的 FIFO 读取阈值是 2–1024 samples。FIFO flag word 大于 `0x200` 时认为支持可变阈值，设置值为 `2048 - threshold`。真实通道聚合、USB 传输调度和吞吐需要实测；当前原型选择约 20 ms 的二次幂阈值并限定范围，并不宣称与 QuickDAQ 的延迟一致。

异步消息开头是 uint32 code：1=计数溢出，2=数字变化，3=DAC threshold，4=ADC oversample，5=ADC overrun，6=输出欠载，7=DAC oversample，8=输出结束。最大消息管线包长定义为 64 字节，C++ reader 提交 512 字节 read，结构中的基础消息长度为 5 字节。

原型模拟 code 5 并将其抛为 `DataOverrun`，超时和拔出分别抛 `TransportTimeout`、`DeviceDisconnected`。每次读取都检查独立状态队列。真实设备必须持续提交 `0x88` 的异步 read；不能只在点击刷新时查询。停止采集后需停止/取消所有 in-flight transfer，再释放 interface 和旧 handle。

流里没有序号或样本计数，不能仅依赖帧解析证明“绝无丢样”。发生溢出时必须将记录标为中断，保留已收到的有效前段；不能填零后继续声称连续。应把 USB/应用丢包、ADC 超量程、用户停止等不同状态区分开。

## 7. 尚未实现的高级硬件功能

### 触发与同步

启动 flags：`0x01`=外部正沿，`0x02`=阈值上升，`0x04`=外部负沿，`0x08`=阈值下降。C 的阈值通道在 `0x80`，16 位 offset-binary 阈值在 `0x81`，其量程必须使用当前输入通道实际增益。触发通道必须在扫描列表中；slave 不独立配置主机的阈值。停止触发/模拟参考触发在源码注释中属于软件圆形缓冲处理。

### 模拟输出

`AoUsb9837x` 给 C 的参数是 **一路、24 位、±3 V、10–96 kS/s**；A 的 ±10 V / 52.734 kS/s 不能套用到 C。端点为 `0x06`，每个输出 USB 传输有 **512 字节信息块**，前四字节表示大小，采样数据从偏移 512 开始；它与没有包头的 ADC 流不同。启动前需要 FIFO reset、output enable、FIFO 大小、时钟、数据预填充、arm，停止时处理欠载和 transfer 取消。

这部分已有源码可继续移植，但当前模拟器不接收输出端点、外触发或同步命令；未实现路径会明确报错，界面高级项不能被标作已能用于真实实验。

## 8. 本轮实现和验证范围

- `labdaq/protocol.py`：命令编码、消息/ADC 解码、配置约束、PLL 规划及寄存器反解。
- `labdaq/transport.py`：协议 session，USB 描述符只读枚举；拒绝把未验证 session 连接到真实 transport。
- `labdaq/simulator.py`：寄存器状态机、二进制回复和流、启动停止、分段、超时、拔出、溢出。
- `labdaq/firmware.py`：C 数组解析、冷启动 RAM 操作计划、仅在内存运行的加载态/辅助 loader/运行态模拟。`tests/test_firmware.py` 的 4 项检查已通过：固定小记录的 A0/A3 顺序和边界、两个 CPUCS 的值/长度、错误阶段拒绝、归档 60/1035 条记录的完整离线执行。
- `tests/test_protocol.py` 与 `tests/test_simulator.py`：测试包含来自 C++ 定义的固定十六进制向量、ADC 两端和零点、48 kHz 手算值、上下限时钟、3/4 路一致、7 字节片段、半帧超时恢复、39-scan 延迟和步阶时间。AC 测试另外验证 1 Hz 一阶模型的阶跃指数衰减、DC 恒定载荷保持，以及跨 USB 分片和多次读取的滤波状态一致性。

在项目根目录运行，`$DaqPython` 使用[开发环境说明](../开发文档/开发环境.md)中选定的现有解释器：

```powershell
& $DaqPython -m pytest tests/test_protocol.py tests/test_simulator.py -q
```

模拟 `sine` 是每物理通道 `ch` 的 `0.02×ch + (0.20+0.05×ch)×sin(2π×10×(ch+1)×t)` V；`step` 在每 4 s 的 0.5–2.5 s 为已知幅度；`drag` 是可重复的上升、保持、滑动纹波和卸载，仅供界面/记录流程演示。AC 采用 DT9837C 手册的名义 1 Hz 截止频率，离散递推为 `y[n] = α(y[n−1]+x[n]−x[n−1])`，`α=1/(1+2π×1 Hz/Fs)`；这是一阶演示模型，未拟合设备真实模拟滤波器、阶数或频响。IEPE 设置只验证寄存器状态，不模拟传感器恒流供电物理过程。模拟器不模拟真实 FIFO 增长、USB 带宽竞争或晶振漂移，因此不能以长时间模拟无错证明硬件吞吐稳定。

## 9. 回到工位后能直接接着做的事

先保留原厂软件作为行为对照，读取 USB 描述符并确认加载态/运行态；用已知稳定电压在 ±10 / ±1 V 两种量程、3 / 4 通道组合上核对真实字节格式和码值。随后验证独立初始化、IEPE/耦合顺序、首帧、停采重启、完全断电重连与异步溢出消息，再实现真实异步 USB transport。此时才能让采集页打开实机入口。

达到实机采集后，再做同一已知输入的 QuickDAQ 对照，以确认出厂校准是否已在设备中完成，并验证实际采样率及通道相位。力传感器型号、调理器输出、灵敏度和接线方式仍需现场补齐。这些是真实测量正确性需要的信息，不应该从外壳照片推断。

公开源码遵循 MIT，移植文件保留 Measurement Computing 归属及完整许可证。上述分析没有依赖原厂 DLL 的反编译结果；Windows 二进制分析用于核对剩余差异，不能把暂时找不到差异当作已经验证一致。
