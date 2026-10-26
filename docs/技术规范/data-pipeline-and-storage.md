# 数据流与存储

## 数据来源与处理顺序

Windows 发布版由 Rust 核心调用原厂 Open Layers C 接口读取设备数据，按 SDK 返回的 ADC 位数、编码、量程及通道增益换算为电压。核心将电压写入记录文件，并保留一份有上限的内存缓存供界面和 API 读取。

```text
DT9837C → 原厂驱动缓冲区 → Rust 电压换算
                              ├→ raw.f64 + metadata.json
                              └→ 内存缓存 → GUI / API
                                           └→ 调零、工程量换算、显示或导出
```

`raw.f64` 保存物理电压，单位为 V。软件调零、灵敏度、极性、工程偏移和显示滤波不修改这个文件。原厂后端实现见 `core/src/vendor_input.rs`，记录器见 `core/src/main.rs`。

另一个开发用的直接 USB 后端读取端点 `0x82`，由 `core/src/dt9837.rs:Decoder` 解析数据。以下 ADC 编码及启动丢弃说明专指这一后端；原厂后端使用 SDK 缓冲区，不执行这里的手工 39 帧截断。

## 直接 USB 后端的 ADC 换算

每个样本为 4 字节小端 `u32`，低 24 位是无符号偏移二进制码。解码器拒绝大于等于 $2^{24}$ 的码值。

设量程为 $R$（10 V 或 1 V），则：

$$V = \mathrm{code}\frac{2R}{2^{24}}-R$$

| 码值 | ±10 V 量程 | ±1 V 量程 |
| --- | --- | --- |
| `0x000000` | −10 V | −1 V |
| `0x800000` | 0 V | 0 V |
| `0xFFFFFF` | $10-20/2^{24}$ V | $1-2/2^{24}$ V |

直接 USB 解码器启动时丢弃前 39 个扫描周期，即 `39 × 启用通道数 × 4` 字节，用于跳过启动建立数据。这一步不等于消除了 ADC 在整个信号带宽内的物理群延迟。

## 调零与工程量换算

界面调零使用最近最多 1 秒的原始电压均值作为各通道的 `zero_volts`。该参数随通道配置保存，记录中的原始电压保持原值。非记录状态下可撤销上一次调零。

对非 V 单位，`labdaq/processing.py:convert_volts` 按以下顺序换算：

$$Y = (V-V_0)\frac{1000}{S}\,p + O$$

其中 $V_0$ 是零点电压，$S$ 是 mV/工程单位的灵敏度，$p$ 在反向时为 −1，否则为 1，$O$ 为工程偏移。

单位为 `V` 时跳过灵敏度缩放：

$$Y = (V-V_0)p+O$$

V 模式允许灵敏度为 0；其它单位的灵敏度必须为有限正数。桥式传感器若标注 mV/V，需要结合实际激励电压及调理器增益，先算出进入 BNC 的总灵敏度，再填写 mV/工程单位。软件调零不改变仪器的硬件校准。

## 记录目录

GUI 默认按下列结构新建记录：

```text
<recording_root>/<experiment>/<condition>-<group_number>/
    raw.f64
    metadata.json
```

空实验名和空工况名分别使用“实验”和“未命名”。正常结束一组后，界面序号加一；若目标目录已经存在，界面提示修改工况或序号，不自动跳过冲突目录。API 的 `start_recording(path)` 直接使用调用者给出的新目录，不套用 GUI 的命名方式。

安装版默认根目录为 Windows 实际“文档”目录下的 `LabDAQ\recordings`，支持系统目录重定向，不能假定总是 `%USERPROFILE%\Documents`。源码版默认为仓库的 `recordings`。路径约定见[用户指南](../用户手册/user-guide.md)。

## raw.f64

文件没有头部，数据类型为小端 IEEE 754 `float64`（NumPy `<f8`）。按扫描帧排列，同一帧内的通道顺序由 `metadata.json` 的 `channels` 数组决定。

例如启用 AI0、AI2：

```text
Scan 0: AI0, AI2
Scan 1: AI0, AI2
...
```

每帧为 $8M$ 字节，$M$ 是已选通道数；$N$ 帧文件大小为 $8MN$ 字节。4 通道记录的数据量如下，使用十进制单位，不含少量元数据开销：

| 实际采样率 | 每秒 | 每小时 |
| --- | --- | --- |
| 1,000 Hz | 32 kB | 115.2 MB |
| 10,000 Hz | 320 kB | 1.152 GB |
| 48,000 Hz | 1,536 kB | 5.5296 GB |
| 105,468.75 Hz | 3,375 kB | 12.15 GB |

## metadata.json

Rust 记录器先写同目录的 `metadata.json.tmp`，再通过重命名替换元数据文件。这个方式避免读者看到写到一半的 JSON；代码没有对文件和目录执行持久化同步，不能据此保证突然断电后数据完整。

元数据在创建、暂停/恢复及结束等节点写入；不是每个采样块都重新写一次 JSON。运行中的文件长度可能已超过元数据中的 `sample_count`。

| 字段 | 含义 |
| --- | --- |
| `format` / `format_version` | `labdaq-raw-voltage` / `1` |
| `dtype` / `layout` / `voltage_unit` | `<f8` / `sample-major` / `V` |
| `sample_rate` | 实际采样率 |
| `source` | Rust 核心来源：`vendor`、`usb` 或 `simulation` |
| `channels` | 实际记录通道的配置，顺序与文件列对应；不是固定 4 项 |
| `acquisition_config` | 采样配置、`record_start_scan`、同步方式及后端信息 |
| `experiment` / `recording_options` | 实验名、目标时长、备注、试件、工况和组号 |
| `sample_count` | 已记录的帧数；已收尾文件应与文件长度一致 |
| `target_sample_count` | 定时记录的目标帧数，手动结束模式为 null |
| `duration_seconds` | Rust 记录器结束时写入的有效记录时长，即 `sample_count / sample_rate` |
| `acquisition_started_at_utc` | 采集起始的主机 UTC 时间；外部硬件触发等无法确定时为 null |
| `started_at_utc` / `stopped_at_utc` | 写盘开始和结束的主机 UTC 时间；结束前可能没有 `stopped_at_utc` |
| `finalized` | 是否执行过记录收尾，不代表采集无错误 |
| `recording_paused` / `active_segment` | 暂停标志和当前写入段的起始映射 |
| `recording_segments` | 已记录各段在文件和源采样序列中的对应关系 |
| `termination_reason` / `error` | 结束原因及错误文本；运行中可能没有这些字段 |
| `analog_output_at_start` | 记录开始时的 AO 状态，或 null |
| `analog_output_events` | 记录期间的 AO 操作及观察到的输入扫描位置 |

常见正常结束原因为 `user_stop`、`duration_complete`；触发等开发功能还有其它原因代码。硬件或写盘错误会保留相应原因和 `error`，不能只检查 `finalized`。

通道配置包含 `index`、`name`、`unit`、`sensitivity_mv_per_unit`、`zero_volts`、`inverted`、`point_number`、`direction`、`engineering_offset`。其中 `index` 是物理 AI 编号。

原厂后端的 `acquisition_config.input` 示例：

```json
{
  "backend": "dt-open-layers-c-abi",
  "device": {"model": "DT9837C", "vendor_name": "DT9837-C(00)"},
  "adc_encoding": "open-layers-24-bit-to-f64-volts",
  "timebase": "hardware-sample-index",
  "continuity": "open-layers-buffer-stream"
}
```

`device` 来自实际枚举结果，可以包含其它字段。这里的 `timebase` 说明样本按硬件采样顺序计数；不表示设备提供了每点 UTC 时间戳。

AO 状态沿用核心的 `output::Status`：包括 `running`、`settings`、`actual_rate`、`zeroed`、`completed` 等字段。静音参数位于 `settings.muted`。事件中的 `requested_at_utc` 和 `observed_input_scan` 是主机操作记录，不是硬件同步的 AO 生效时刻。

### 暂停分段与时间轴

每段包含：

```json
{"file_start_scan": 6000, "source_start_scan": 15000, "sample_count": 4000}
```

`file_start_scan` 是该段在文件中的帧号，`source_start_scan` 是该段在整次采集中的帧号。暂停时不写数据，恢复后新建一段，文件中不会补零。

回看与导出的相对采样点为：

$$s = s_{\text{source}}-s_{\text{record start}},\qquad t=s/f_s$$

因此记录开始前的预览时间被扣除，记录期间暂停造成的缺口保留。有效记录时长是帧数除以采样率，包含暂停缺口的跨度可能更长。

## 导出

CSV 和制表符 TXT 都使用 UTF-8 BOM。首行是 `# labdaq_metadata: ` 加 JSON，第二行是标题。元数据中的 `export` 记录所选帧区间、通道、数值和时间格式，原始 `sample_count` 不会被改成导出点数。

时间列支持 `time_s`、`time_ms`、`time_us` 或 `sample_index`。原始列名为 `{name}[AI{index}]_raw_V`，工程量列名为 `{name}[AI{index}]_{unit}`。`both` 模式先列出全部原始列，再列出全部工程量列。默认 V 通道的工程量列后缀为 `_V`。

GUI 导出回看记录中勾选通道的完整数据，不按图中当前窗口截取。底层 `export_csv` 函数支持帧区间选择；当前 Agent API 暴露数值选择，不暴露全部底层参数。

```python
import pandas as pd

# 首行 JSON 注记由 comment 参数跳过。
data = pd.read_csv("export.csv", comment="#", encoding="utf-8-sig")
```

只读取完整原生记录的电压，可以使用：

```python
import json
from pathlib import Path
import numpy as np

folder = Path("recordings/实验/工况A-1")
meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
raw = np.fromfile(folder / "raw.f64", dtype="<f8")
volts = raw.reshape(-1, len(meta["channels"]))
```

这个示例不重建暂停时间轴。需要分段时间轴和格式校验时，使用 `labdaq.recording.load_session` 及其 `sample_times()`。

## 未收尾与损坏记录

`load_session` 检查文件长度。未收尾记录若仍包含完整帧，可根据文件长度及已发布的活动分段恢复可读帧数，保留 `finalized: false`。如果末尾不足一帧，或分段映射不完整，会报错，不静默截断或改写原始文件。已经收尾的记录若长度与元数据不一致，也会报错。

GUI 退出时后台核心可继续采集和记录；核心崩溃、系统断电、磁盘错误和主机队列溢出是另外的故障。是否完成正常记录，应结合 `finalized`、`termination_reason` 和 `error` 判断。

## 仓库保留的分析算法

以下算法在 `labdaq/processing.py` 中，相关分析页当前没有 1.0 界面入口，Agent API 也未提供这些分析方法。

- `StreamingFilter`：Butterworth SOS 因果滤波，默认设计阶数为 4，块间保留状态。带通、带阻变换后的实际阶数与低通、高通不同。
- `filter_window`：`sosfiltfilt` 双向滤波，默认奇延拓。有效区间内为零相位，总幅频响应是单次响应的平方；单次 −3 dB 截止点对应约 −6 dB，短数据及边缘仍受暂态影响。
- `spectrum`：保留 DC，不去均值，使用周期分析窗。对 $x_w[n]=x[n]w[n]$，峰值谱基础量为 $|\mathrm{FFT}(x_w)[k]|/\sum w[n]$；PSD 基础量为 $|\mathrm{FFT}(x_w)[k]|^2/(f_s\sum w[n]^2)$。单边谱将有负频率配对的频点乘 2，DC 及偶数长度的 Nyquist 点不乘 2。

PSD 满足：

$$\sum_k \mathrm{PSD}[k]\Delta f = \frac{\sum_n (x[n]w[n])^2}{\sum_n w[n]^2}$$

矩形窗下等于时域均方值。峰值谱对落在频点上的整周期正弦补偿窗函数相干增益；非整周期信号会出现谱泄漏。

`frequency_response` 用 Welch 方法，50% 重叠，每段去均值。定义 $P_{xy}=\overline{X}Y$，则：

$$H_1=P_{xy}/P_{xx},\qquad \gamma^2=|P_{xy}|^2/(P_{xx}P_{yy})$$

至少需要两个重叠段。分母为零的频点输出 NaN；代码不根据一个“低信噪比阈值”自动筛选频点。

[IPC 与硬件控制](ipc-protocol-spec.md) · [技术规范目录](README.md)
