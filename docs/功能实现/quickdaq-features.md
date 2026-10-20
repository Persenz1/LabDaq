# QuickDAQ 功能基线与 LabDAQ 对照

更新：2026-10-19（补齐暂停记录、测量游标、时间轴、数值读数和批量导出五类基础缺项；保留 USB 输入及 2026-10-17 原厂界面核对）。目标是保留 QuickDAQ 在本实验室 DT9837C 上适用的能力，常用流程简化，高级功能保留入口。下表按能力归纳，不要求复制原版界面或使用原厂 SDK。

**当前 LabDAQ 已有 Rust 独立 USB 连续采集核心，并保留独立电信号模拟、旧 Python 协议模拟和 HPF 输入。** 核心通过 WinUSB/libusb 直接采集，不依赖原厂 SDK；GUI 退出后可继续记录并重新接回。本轮使用模拟信号开发分组命名、备注和定长记录，USB 共用核心实现，未做本轮实机验证。表中的“部分”表示仅完成列出的部分，不代表与原版行为等价。ManuWare/电荷放大器配置不属于 DAQ 软件范围。

**日常记录、回看、统计与导出流程已贯通，本轮继续使用模拟信号补齐五类基础缺项，未测硬件。** 新增真正的暂停/继续记录、实时与回看双游标、三种图横轴、近一秒读数模式与小数位设置、记录库多组批量导出。已有记录库搜索、精确定位、选区和通道选择、区间统计与导出、PNG 存图及撤销调零继续保留。暂停期间输入预览继续，恢复记录保留扫描时间缺口；记录列表仍只读元数据。正式使用准备、高级分析及硬件部分按用户安排暂不开发。**本轮五类缺项完成不等于全部 QuickDAQ Base 能力等价**，通道元数据、布局、打印等仍有下表列出的缺口。

来源缩写：**Q** 为 [QuickDAQ 手册 UM-25242-H](https://files.digilent.com/manuals/UMQuickDAQ.pdf)，**H** 为 [DT9837 系列手册 UM-22417-AK](https://files.digilent.com/manuals/UM9837.pdf)，页码均为手册印刷页码；**R** 为 [QuickDAQ 发布说明](https://files.digilent.com/downloads/DTSoftware/QuickDAQ/ReleaseNotes.txt)的本地归档 3.7.0.49 版本。Base、FFT、Advanced FFT 是手册的功能分档，后者包含前者；这里不依据旧手册判断当前授权政策。

**本机核对结论（2026-10-17）**：此前已记录主要能力族，但还不是逐界面核对后的完整清单。本次通过 Computer Use 检查了本机 QuickDAQ **3.7.0.49** 的 Data Logger / FFT Analyzer、通道配置、数字滤波、输出、显示与统计等界面。About 和 License Info 均显示 **FFT Analysis Option**，不是 Advanced FFT；本机出现单通道 FFT 新增入口，未出现双通道函数新增入口。因此，下表 FRF、互谱、相干、冲击专用窗、Nyquist、UFF 等 Advanced 能力仍保留手册依据，不标为本机已验证。界面核对细节见文末。

## 采集、配置与记录

| 能力族 | QuickDAQ 原版能力 | DT9837C 适用性 | LabDAQ 当前状态 | 来源 |
|---|---|---|---|---|
| 设备选择与配置 | Base：发现、添加/移除、重命名设备，显示身份/驱动信息；保存并恢复 XML 配置 | USB 设备适用；网络地址、网络密码不是 C 的接口 | **部分**：USB 枚举、单台 DT9837C 连接/断开与独立核心采集；JSON 保存通道/采样参数、输入来源、电信号或 HPF 选项、记录时长和备注，以及实时/回看横轴、实时读数模式和小数位设置；未兼容原版 XML 配置文件或完整设备管理 | Q 27–30、44；H 67 |
| 输入通道 | Base：启用通道、命名、量程、传感器单位、mV/EU、EU Offset、测点号与方向；部分设置可批量应用 | 4 路单端同步、24 bit；±10 V/±1 V；195.3 Hz–105.469 kHz；不能选择差分输入 | **部分**：USB 与模拟支持 1–4 路任意升序子集、名称/单位/灵敏度、极性与电压零点；缺测点号、方向元数据、批量操作和工程单位偏移入口 | Q 89–92、100–101、127；H 68–70、95、118 |
| 耦合与 IEPE | Base：逐通道 AC/DC、恒流源开关 | C 为 **2 mA**；AC 高通名义 1 Hz。不能照抄 Q 通用说明中的 4 mA | **已有配置与 USB 核心设置链路**，并保留协议模拟；HPF 输入从文件恢复并锁定。5073A 电压输出沿用已确认的 DC、IEPE 关闭设置；未将模拟 AC 模型视为实机响应验证 | Q 100；H 69、118 |
| 预览/记录/暂停/停止 | Base：View 不落盘，Record 落盘；Pause 暂停记录，Stop 停止接收 | 适用 | **已实现基本控制**：Rust 核心及旧 Python 模拟/HPF 输入支持预览、记录、暂停/继续、结束记录和停止采集。暂停只停止本组写盘，输入及预览继续，本组保持打开；恢复后写入同一组，保存源扫描缺口。暂停中也可结束本组或停止采集。定长结束记录后继续预览。“暂停滚动”独立控制曲线冻结 | Q 46 |
| 连续与定长记录 | Base：连续或指定天/时/分/秒；文件名固定、序号或日期时间；记录备注、时长与采样计数。FFT：按记录块/平均次数自动结束 | C 支持连续扫描；定长、分块和文件命名属于应用控制 | **部分**：按日期和实验名生成递增组号（如 `20261019_实验_001`），不覆盖已有目录，日期或实验名变化各自计数；保存单组备注、时长、帧数与结束原因。时长 0 为手动，正秒数按实际 fs 四舍五入到整数帧，达到目标后结束记录并继续预览，界面显示剩余秒数；暂停期间已记录帧数及定长剩余量不变。Rust 核心在 GUI 断开后仍按实际记录帧数结束。旧模拟、HPF 和 Rust 输入均接通；USB 共用核心逻辑，本轮仅模拟验证。HPF 可先到 EOF；目录可用空间以 GiB 每 5 秒只读刷新。缺平均次数结束和其他命名方式 | Q 121–127；H 71–72 |
| 启动触发 | Base：软件、外部 TTL、模拟阈值，实际选项随硬件 | H 确认 C 的软件、外部 TTL **上升沿**、任一选中 AI 的正/负阈值。本机 Data Logger 下拉框却同时列出 TTL Pos/Neg Edge，FFT 列表仅有 Pos Edge；**下降沿只确认界面选项，硬件行为未验证**，不能据此扩大 C 的能力范围 | **部分**：USB 与模拟支持软件启动；未实现外部 TTL、模拟阈值及等待状态 | Q 128、134–135；H 73、99、125；2026-10-17 本机 UI |
| 重复/冲击记录 | FFT：Free Run、Software Triggered、Auto Retrigger/延迟、User Accept-Reject；阈值记录的预触发比例与触发标记 | C 可做阈值前后采集；原厂 Windows 有 start/reference trigger，Linux 接口不直接提供 reference trigger。应用重触发不等于硬件无间隙逐触发扫描 | **未实现**：重复记录、接受/拒绝、预触发缓存与标记；现有模拟分块读取不能替代此流程 | Q 125–126、133–136；H 72–74、94 |
| 原始记录与回放 | Base：HPF 写入/无设备读取、长记录浏览；可修改文件内名称、单位、灵敏度和偏移后重处理 | 适用，但 HPF 中存储值与传感器缩放需按文件解释 | **部分**：自有原始 V 流式记录；`recording_segments` 保留暂停前后源扫描位置，回看以断线表示时间缺口，不补样本。根目录记录库按本地开始时间排序，可搜索实验名、备注、目录名及结束状态，双击打开。回看支持精确起点秒数、前后窗口和粗滑块，每次最多读 200,000 样本；列表只读元数据。原有 Double/V 子集及已核实 DT9837C 力 HPF 可读；显式 `waveform_only` 从原始块读 V、从完整配置取精确灵敏度/物理通道/采样率，辅助块元数据保留，未知事件拒绝。无通用 HPF 支持、HPF 写出及回放换算编辑 | Q 20、36–37、86；R：HPF/滤波修复 |
| 多设备与同步 | Base：多设备选择；R 增加 device collection；FFT 以主设备触发 | C 有 RJ45/LVDS 主从同步；建议 C 只与 C 组网，直连两台或经分配板最多四台；从机先启动，主机后启动；不得把“多 USB 设备”当作已同步 | **未实现**：多设备配置、身份关联、主从时钟、跨设备记录。当前 GUI 最多四路 | Q 27–30、134、142；R：3.7.0.32 后变更；H 87–89、153 |

## 显示、统计与交换文件

| 能力族 | QuickDAQ 原版能力 | DT9837C 适用性 | LabDAQ 当前状态 | 来源 |
|---|---|---|---|---|
| 波形与布局 | Base：多图/信号分组、显隐、颜色和线宽；坐标自动/手动、线性/对数、缩放/撤销、平移、全屏、轴联动、暂停跟踪；可调整刷新率、精度和图高 | 软件功能，适用 | **部分**：固定四条曲线、缩放/平移/适应窗口、暂停滚动；回看可按物理 AI 通道勾选显隐并用于统计/导出，混合单位回到原始 V。已有横轴模式、读数模式和小数位配置；无多图分组、轴联动、布局保存、对数轴、刷新率或图高配置 | Q 53–55、68–80、170–181、193–203 |
| 时间轴与游标 | Base：样本号、相对/绝对时间；XY 数值、周期、频率、峰峰游标及联动；FFT 增加频带功率游标 | 适用；系统绝对时间不能当作独立硬件同步时间 | **部分**：实时/回看均可切相对时间、绝对时间（电脑时钟）或样本号；无可用起点时间的 HPF 不伪造绝对时间。双游标可选通道、拖动 A/B，显示最近真实样本的时间和幅值、Δt、Δy、区间峰峰值及间隔倒数 1/\|Δt\|；间隔倒数不作测得基频。回看起点和选区字段始终使用相对秒，选区按最近样本边界取整、右端不含，暂停缺口不补点。缺跨图游标联动和频带积分 | Q 74–76、180、201 |
| 数字显示与统计 | Base：缓冲首值/平均/最小/最大显示，精度、布局、刷新率；Min/Max/Delta/Mean/Std Dev 实时和回放统计 | 适用 | **部分**：实时卡片可选近一秒最新值、首值、均值、最小、最大、RMS、总体标准差或峰峰值，小数位 0–9 可设；保留近一秒绝对峰值和量程提示。回看按所选通道及区间计算最小、最大、峰峰、均值、总体标准差和 RMS，支持原始 V/工程值及复制多通道表。跨暂停区间只聚合实际记录样本；未启用选区时统计全记录。缺布局和刷新率配置 | Q 81–83、184–188、232–246 |
| 时域导出 | Base：CSV、制表 TXT、全部/可见通道、全部/指定扫描范围；秒/毫秒/微秒/时间字符串/Excel 时间/样本号；可带统计、设备通道信息；文件序号或日期命名；打开 Excel | 适用 | **部分**：单组按勾选物理通道及全记录/选区后台分块导出 CSV/TXT，原始 V、工程值或两者可选。秒/毫秒/微秒/样本号均保留原记录位置与暂停缺口，不将选区起点归零；JSON 元数据保存原配置、区间、通道和标度。记录库 Ctrl/Shift 多选后可后台顺序批量导出，各组使用全部样本及自身全部通道、按组目录名生成文件；不覆盖同名目标，异常或未收尾组不能批量，出错报告组名并保留成功文件。缺时间字符串/Excel 时间、统计附表、其他自动命名方式和 Excel 入口 | Q 37–43 |
| 频域与模态导出 | FFT：时域记录或频域平均结果/指定源记录；Advanced：实数/复数 FRF、ASCII UFF | 适用；UFF 的测点、方向、单位与复数需一起保留 | **未实现**：频域导出、UFF 和复数 FRF 交换 | Q 40–42；R：UFF 单位/复数/方向及 Dataset 58 修复 |
| 图像、剪贴板与打印 | Base：图像保存、图像/数据复制、页面设置、打印预览和打印 | 适用 | **部分**：实时及回看波形可通过“保存图像”导出 PNG，回看统计表可复制；缺波形图像/数据复制、页面设置、打印预览和打印 | Q 43、45、76–77 |

## FFT、滤波与双通道分析

| 能力族 | QuickDAQ 原版能力 | DT9837C 适用性 | LabDAQ 当前状态 | 来源 |
|---|---|---|---|---|
| FFT 分块工作方式 | FFT：时域/块平均界面；最大分析频率、FFT 点数、谱线数/频率分辨率；添加多个原始/加窗/FFT 派生通道，逐记录查看并联动时域；参数变化后重处理 | 适用；分析带宽受采样与模拟信号链限制 | **部分**：实时或回放窗口切换并重新计算，最多尾部 65,536 点；含暂停缺口的回看窗口拒绝分析，需选择单个连续段。无用户 FFT 点数、记录块列表、多派生通道和平均进度 | Q 59–63、130–131、204–209、224、229 |
| 单通道谱 | FFT：Spectrum、Auto Spectrum、PSD；单/双积分；幅值/相位 | 适用；积分到速度/位移须有相应传感器单位 | **部分**：单边峰值幅度谱和 PSD；无自功率谱入口、相位及积分。现有 amplitude 不能直接宣称等同原版全部 Spectrum/平均定义 | Q 209–210 |
| 谱的缩放 | FFT：线性/dB、参考值、RMS/Peak/Pk-Pk；Advanced 加实部、虚部、Nyquist | 适用 | **部分**：峰值线性谱和线性 PSD；未实现 dB 参考、RMS/峰峰切换及复数图 | Q 210–213、216、218 |
| 窗函数 | FFT：矩形、Hann、Hamming、Bartlett、Blackman、Blackman-Harris、FlatTop；Advanced：响应指数窗，参考窗另有 Force、Cosine Taper，可与响应分别设置 | 适用 | **部分**：界面提供矩形、Hann、Hamming、Blackman、FlatTop；无独立参考/响应窗及冲击窗参数。底层 SciPy 接受其他窗不等于界面已完成 | Q 137–139 |
| 谱平均 | FFT：开关、线性/指数及权重；RMS、Vector、PeakHold；Advanced 的 FRF/互谱/相干也有平均控制 | 适用；向量平均涉及触发与相位对齐 | **未实现原版记录间平均流程**；H1 内部 Welch 的 50% 重叠分段平均只是现有估计方法 | Q 214、219、223、226 |
| FRF | Advanced：参考/响应配对、H1/H2/H3；Inertance、Mobility、Compliance、Apparent Mass、Impedance、Dynamic Stiffness、Custom；幅值、相位、实部、虚部、Nyquist | 适用；物理模式取决于实际传感器，三路力输入不能直接声称测得惯性或柔度 | **部分**：Welch H1 和单位比值，仅显示幅值；后端返回复数；缺 H2/H3、物理模式、积分及复数显示 | Q 90、206、215–219 |
| 互谱与相干 | Advanced：Cross Spectrum、Cross PSD、Coherence、Coherent Output Power；两侧可分别积分 | 适用 | **部分**：幅值平方相干随 H1 显示；互谱只作内部计算，无独立图/输出；无 COP 或积分 | Q 220–226 |
| 谱峰与动态性能 | FFT：最多 10 个谱峰、幅值/峰高优先、灵敏度；IBF、THD、SFDR、SINAD、SNR、ENOB，含 RMS/Vector 平均选项 | 软件分析适用；仪器性能指标需匹配输入及量程，不能从任意力曲线解释为 ADC 精度 | **未实现**；实时近一秒峰值不是频谱峰检测 | Q 227–228、238–246 |
| 数字滤波 | FFT/Advanced：逐通道 Bessel/Butterworth/Chebyshev；低/高/带通/带阻，1–10 阶、截止频率/纹波；频率/冲激/阶跃响应与稳定性显示 | 适用；不替代 C 的采样前抗混叠，也不等于外置 5073A 的 200 Hz 低通 | **部分**：窗口零相位 Butterworth 低通；另有未接入界面的 StreamingFilter。缺完整滤波配置与响应图；原始记录保持不变，现行为不同于 R 所述“滤后数据写 HPF” | Q 164–168；R：3.6.1.1 后变更；H 68、118 |
| 采集页移动平均 | Base 通用采集设置：硬件支持时可选 16 点移动平均/None | 本机 3.7.0.49 + DT9837C 的 Data Logger / Acquisition、FFT / Freq 页面**均未显示该设置**；不列作本机已确认能力，也不推断其他版本或设备没有 | **未实现该设置**；不能与上面的软件 IIR 混为一项 | Q 128、132；2026-10-17 本机 UI |
| 倍频程/阶次 | 所读手册与 R **未找到倍频程带分析、1/3 倍频程或阶次跟踪功能依据**；octave 只用于对数扫频速率、粉红噪声说明 | 倍频程可作为后续软件扩展；C 没有原生转速计输入，阶次跟踪需要另定转速来源 | **未实现，列为待确认扩展**，不写成已确认的原版缺失功能 | Q 147、157、336；H 82、95 |

## 信号输出与硬件边界

| 能力族 | QuickDAQ 原版能力 | DT9837C 适用性 | LabDAQ 当前状态 | 来源 |
|---|---|---|---|---|
| 固定波形 | Base：正弦、方波、锯齿、三角；峰值、偏置、频率；连续/单次或重复 Burst、通断时间、幅度渐入渐出；运行中改参数 | C 有 **1 路、24 bit、±3 V AO**，输出采样率 10–96 kS/s。输出采样率不是可无失真输出的波形频率 | **部分**：Rust 实机 AO 支持直流、正弦、50% 方波、三角及峰值/偏置/频率/独立更新率设置，2026-10-20 已做低幅值 AI0 回环短测。修改参数需先停止；锯齿、Burst、渐入渐出尚未实现 | Q 19、140–145；H 76–79、121 |
| 扫频与噪声 | Base：线性/对数升降扫频、循环/单次、通断与渐入渐出；均匀随机、高斯随机、粉红噪声，True/Pseudo Random 重复方式、带宽 | 适用，受 ±3 V、采样率和输出滤波限制 | **未实现此类 AO 波形**；软件已有线性扫频/白噪声输入模拟，不代表具有对应硬件输出能力 | Q 146–160；H 76–79 |
| 输出时序与停止 | Base：手动或随采集启停、软件/TTL 启动、Mute；FFT：锁定采集时长、分析帧时长/频率；多设备时只启用主机一路 AO | 适用；需处理 ADC 39 点、DAC 29 点群延迟及触发组合；QuickDAQ 明确限制“随采集输出”与阈值触发组合 | **部分**：真实 USB 预览后手动启动；可单独停止 AO 并下发 0 V，停止采集同时停止 AO。GUI 关闭后核心继续输出；记录含 AO 状态和软件启停事件。未实现同步启动、TTL、定长/Burst 或群延迟补偿 | Q 142、144、148、159–163；H 70、77、80 |
| 其他设备专属能力 | 原版还支持部分设备的热电偶/RTD/热敏电阻/电阻/电流、桥路/应变、TEDS、零点/分流校准、转速计/门控、正交编码器、LXI/LAN 触发 | **C 不具备这些专用采集子系统**；外部调理成电压后可测量，不能称作原生支持。C 也没有 A 型的 DAC 内部回读通道，需物理回接验证输出 | 不作为 C 版本遗漏功能；未来换设备时另加对应后端。当前单位菜单里有 °C 等字符串不代表已实现温度转换/冷端补偿 | Q 90–119、134；H 67、80、82、85、95、98 |
| 仪器校准/上电设置 | 原厂生态另提供 ADC/AO 校准、上电耦合/IEPE 等维护能力；不全属于 QuickDAQ 本身 | C 适用；与传感器软件调零分开，不能照用 A 型 ±10 V AO 校准值 | **部分**：已有软件扣零点，非记录时可单步撤销上次调零；独立 RAM 固件加载及冷启动后连续采集，冷态与运行态均使用 WinUSB，固件需单独准备。未实现仪器校准读写或用户可配置的上电设置 | H 36、111–116、142–143 |

## 实现时需要保留的差别

1. **记录内容与显示处理分开。** LabDAQ 保留原始 V 的做法可以继续；增加滤波和换算时保存参数及派生结果，让用户能复现原版结果，不能静默重复应用灵敏度或滤波。
2. **已核实力 HPF 的输入链已打通，通用格式兼容仍有限。** `read()` 返回 0x3000 原始 V；力换算采用 0x7000 完整配置中的 `MilliVoltsPerUnit`，不以略低精度的 `SensorScale` 代替。0x8000/0x9000/0xA000 只保留辅助元数据，不解作波形；回放记录标为 `file_replay`。约 20 秒、20,002 × 3 样本已完成引擎记录回读，V 与工程量逐值相等。后续只用原始 HPF、历史第 1/2 次，不再读 CSV；其他单位、事件/触发扩展仍需分别支持。
3. **手册与 3.7 发布说明有差别。** Q 179/201 仍列“采集时禁绘图”，R 说明已移除该按钮；不把旧按钮列为必需保留。R 还记录 CSV 末值被置零、绝对时间、UFF 复数/方向/单位、首次触发拒绝后记录及大 HPF 修复，说明这些结果语义需要保留，不能只做相似菜单。

## 2026-10-17 本机界面核对

本节记录实际打开的页面和展开的选项，补充上面的能力表。**看到设置入口不等于完成采集、算法或输出验证。** 本次没有启动 View / Record / AO，没有改动通道量程、耦合、恒流源或灵敏度，没有应用数字滤波。

### 顶层入口

| 菜单 | 本机入口 |
|---|---|
| File | New / Open / Close Hpf Data File；Export Data；Save Current Plot Image；Open Current Data In Excel；Load / Save Configuration（XML）；打开 HPF / 导出目录；Page Setup、Print Preview、Print；Exit、Exit without saving current configuration |
| Edit | Copy plot data / image to clipboard |
| Record | View、Record、Pause（Space）、Stop；未运行时 Pause / Stop 灰显 |
| Plot | Plot Area Mouse Mode：Select / Zoom / Cursor；Axis Mouse Mode：Scroll / Zoom |
| Configuration | Input Channel Configuration、Digital Filter Configuration、Device Selection |
| Windows | Acquisition Config、Plot and Data Config、Channel Plot、Channel Display、Channel Statistics；恢复上次保存布局或默认布局 |
| Mode / Help | Data Logger / FFT Analyzer；User Manual、License Information、About；工具栏另有 Style 外观入口 |

### 设备、输入、记录与输出

| 页面 | 本次确认的细节 |
|---|---|
| Device Selection | Device Family 当前为 OpenLayersDevices；Available / Selected Devices；Add、Remove、Refresh；Model、Device-ID、Driver Version；选中设备表含 Name、Board ID、Device-ID、Master。刷新读到 DT9837-C、驱动 **7.0.0.9**；无选中设备时提示只能进入 File Reader Mode |
| Input Channel Configuration | 四路 Ain 0–3；Enable、Channel Name、Range（**−10–10 V / −1–1 V**）、Coupling、Current Source、Engineering Unit、mV/EU、EU Offset、Point #、Comment、Dir。Actions 可批量启用/禁用全部或选中通道、批量设置全部或选中电压通道的单位 |
| 进入时的实验配置 | Ain 0–2 启用，Ain 3 禁用；四路均 DC、Current Source 未勾选、±10 V。前三路单位 N、方向 X+ / Y+ / Z+；灵敏度显示 500、500、333.33… mV/EU，第三路在表格中被截断，未把显示精度当作完整参数 |
| Data Logger / Recording | 文件名生成方式、Start #、基础文件名/路径、剩余磁盘空间、连续采集、定长 dd.hh:mm:ss、独立 X Axis Span、Notes。原配置为 20 秒，实际采样率状态显示 **1000.116 Hz**，间隔 **0.000999884 s**；采样频率输入框显示 1000.12，存在显示精度差别 |
| Data Logger / Acquisition | 每通道采样频率范围标为 **195.3–105469 Hz**；Software、External TTL Pos Edge、External TTL Neg Edge、Analog Threshold Pos/Neg Edge。阈值页含通道、工程单位阈值及对应 V（原配置 1 N 对应 0.5 V）。SingleEnded 和全局 Input Voltage Range 灰显，量程应从逐通道页设置 |
| Output | 一路输出，Enable 原为关闭。Waveform 可切换 Fixed / Sweep / Noise；表头含 Channel Name、Peak Voltage、Offset。Fixed 页可见 Signal Type、Frequency、Output Mode、Repeat burst、Burst On/Off Time；Sweep 页可见 Sweep Mode、Freq Change、起止频率、Sweep On/Off Time；Noise 页可见 Noise Type、Repeat Method、起止频率和 Lock to analysis frequency。共有 Ramp Up/Down、Lock to acquisition time、Start/Stop Method、Trigger Source、扬声器图标和 Start。输出未启用，因此未展开灰显的子选项，具体可选波形仍以上表手册记录为依据 |

### 显示、FFT 与数字滤波

| 页面 | 本次确认的细节 |
|---|---|
| Plot and Data Config | 每通道分别控制 Plot / Display / Statistics 可见性、Show Cursor、Plot Column、Signal Group、Color；Actions 对选中通道批量操作；Hide/show columns 只控制配置表列的显隐 |
| Channel Plot / Control、Style | 刷新率、Smooth scrolling、样本号/相对/绝对时间、12/24 小时、Display Day、图高；背景/坐标轴/网格颜色、线宽、标题文字与可见性、标题及轴标签字体/颜色。工具栏含轴/游标联动、缩放/平移、图例、数据点标记、复制、图像保存、打印相关入口 |
| Channel Display | Hide Unused Channels、小数位精度、列数、刷新速度；Data Reduction Method 为 First Val / Average / Min / Max |
| Stats | Data Logger 可见 Show Min/Max/Mean Stats；FFT 新增 Show FFT Peaks、1–10 个峰、绝对幅值或峰高优先、灵敏度。Dynamic Performance Stats 带 RMS / Vector Averaging，是否可编辑取决于所选数据类型 |
| FFT / Recording | 按 Number of Averages 或连续采集控制；显示 Run length，X Axis Span 可勾选 Same as run length；文件名、备注等与 Data Logger 分开保存。本次进入时为 50 次、12.8 s，并非改写原 20 s 记录配置 |
| FFT / Freq | Maximum frequency to analyze；FFT Size 为 **128、256、512、1024、2048、4096、8192、16384、32768、65536、131072、262144**；显示谱线数和频率分辨率。进入时 2000 Hz 最大分析频率、4000 Hz 采样、1024 点、512 谱线、3.906 Hz 分辨率，符合界面采用的 fs/N 关系 |
| FFT / Trigger | Free Run、External TTL Pos Edge、Analog Threshold Pos/Neg Edge、Software Triggered；阈值工程量与对应 V、通道、**0–100%** Pre Trigger Data Percent、触发标记；Trigger Mode 为 Auto Retrigger / User Accept-Reject。底部另有 Record Number、Accept / Reject、Link Time Series Data |
| FFT / Window | 本机列表为 Rectangular、Hamming、Hanning、Bartlett、Blackman、BlackmanHarris、FlatTop；没有出现独立参考/响应窗或 Force / Exponential 参数 |
| FFT 派生通道 | 可按原始通道添加 single channel FFT function、windowed time domain channel、time domain channel；可移除所选显示行。本次临时添加 Ain 0 的 FFT 行查看参数，随后撤销该行 |
| FFT / Data | Function：Spectrum / AutoSpectrum / PSD；Integration：None / Single / Double；Display Function 入口。Scaling：dB/Linear、RMS/Peak/PkPk、dB Reference。Averaging：类型、权重、启用开关；模式为 RMS / Vector / PeakHold。支持 Auto-reprocess on parameter change 与 Apply settings to current data |
| Digital Filter Configuration | 每通道 None / Bessel / Butterworth / Chebyshev；HighPass / LowPass / BandPass / BandStop；**1–10 阶**，Cutoff Freq 1/2、DbRipple；Frequency / Impulse / Step Response 三图、状态标志、Apply / Close。原四路均为 None，本次仅展开选项，没有改变或应用滤波 |

### 官方示例读取与导出

实际打开项目内原厂提供的 `resources/vendor/extracted/quickdaq/LocalAppData/Data Translation/QuickDAQ/Data/QuickDAQ FFT Analyzer Sample Data.hpf`。界面显示 Processing complete，两个来源为 DT9837-B(02) 和 DT9837-A(00)，15000 Hz、20480 扫描、5 次平均；时域加窗曲线和频谱正常显示。它不是本机 DT9837C 的新采集数据。

| 页面 | 本次确认的细节 |
|---|---|
| 文件读取状态 | 采集启动、记录和硬件采集参数灰显；保留分析参数重处理、记录编号与时域联动入口；File / Export Data、Open Current Data In Excel 变为可用。关闭 HPF 后自动返回此前的 DT9837C Data Logger 配置 |
| File Export | Time History Data / Frequency Domain Data；All enabled channels / Visible channels only；TXT / CSV。时域可保存全部数据或记录编号范围；频域可保存平均结果或源记录编号范围。本 FFT 样例的范围为 Record 0–4；不要将这里的“记录编号”当成 Data Logger 的单个扫描编号 |
| 时间列与附表 | Seconds、milliSeconds、microSeconds、Date-Time String、Time String、Excel Time Format、Sample Number；Include statistics、Include device and channel info；文件命名方式、路径、Export、Open in Excel |
| 高级导出边界 | 当前 FFT 示例的 Universal File Format-ASCII、Save Real data / Save Complex data 灰显；本次未执行 Export，也未验证导出数值 |
| Y 轴右键 | Y Axis Setup、Zoom to Fit in View、Zoom to Fit All。设置页含主/次网格、Autoscale / Manual、Min / Max、Precision、Format、Lin/Log；示例频谱轴为 Log10。打开检查后原值关闭 |

本次还观察到设备选择的流程边界：从主菜单进入 Device Selection 后原主界面退出，首次刷新显示 Device is in use；点 Cancel 后程序退出。重新打开后设备显示 Device is idle，仍选中原 DT9837-C，原采集配置恢复。**这只记录本次现象，尚未定位占用原因。** 重启清除了进入时屏幕上停留的 6912 扫描预览曲线，未改动已有 HPF 文件。

结束时已关闭示例并回到 Data Logger：三路力通道、20 秒记录时长、1000.116 Hz 状态采样率、Software 触发和关闭的 Fixed 输出；状态为 Idle。本轮是功能入口盘点，没有执行全功能实测；硬件触发、AO 波形、真实记录暂停/续录、长记录、多设备同步及 Advanced FFT 仍不能据此标为已验证。

LabDAQ 对照入口：[gui.py](../../labdaq/gui.py)、[core_client.py](../../labdaq/core_client.py)、[Rust 核心](../../core/src/main.rs)、[engine.py](../../labdaq/engine.py)、[inputs.py](../../labdaq/inputs.py)、[signals.py](../../labdaq/signals.py)、[transport.py](../../labdaq/transport.py)、[processing.py](../../labdaq/processing.py)、[recording.py](../../labdaq/recording.py)、[plot_measurements.py](../../labdaq/plot_measurements.py)、[data_dialog.py](../../labdaq/data_dialog.py)、[recording_library.py](../../labdaq/recording_library.py)、[hpf.py](../../labdaq/hpf.py)。实现状态来自当前代码与已有验证；2026-10-17 的原厂界面观察不代表对应能力已完成实机验证，2026-10-19 的软件开发与验证使用模拟信号。
