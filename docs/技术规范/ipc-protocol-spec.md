# IPC 与硬件控制

## 运行结构

GUI 和 Agent API 通过本地 TCP 调用独立的 Rust 采集核心。核心持有驱动会话，负责采集、内存缓存、记录和 AO。关闭客户端不自动结束这些操作，重新打开 GUI 可以接回现有会话。硬件、驱动、磁盘或核心进程出错仍可能中断采集。

Windows 发布版使用原厂 Open Layers C 接口（`core/src/vendor_input.rs`）。直接 USB/WinUSB 后端仍保留用于开发研究；本文最后一节的 USB 寄存器及封包说明只适用于这个后端。

应用集成优先使用[Agent API](../用户手册/agent-api.md)。本页的 IPC 是内部接口，包含 API 和当前界面没有暴露的命令。

## 端点发现与连接

默认端点文件：

- 安装版：`%LOCALAPPDATA%\LabDAQ\.runtime\core.json`
- 源码版：`<repo>/.runtime/core.json`

文件包含当前 `port`、`pid` 和 `token`。客户端默认以端口 0 拉起核心，由系统分配监听端口；核心将实际端口写入端点文件，绑定地址为 `127.0.0.1`。

```json
{"port":54321,"pid":12345,"token":"这里是本次核心的鉴权字符串"}
```

Python 启动器通过 `secrets.token_hex(32)` 生成 64 个十六进制字符的 token，并传给核心。调用者从文件读取本次凭据，不复用文档中的示意值。

Windows 客户端在启动核心期间获取命名互斥量 `Local\LabDAQ-core-<hash>`，等待上限 10 秒。hash 为端点绝对路径经 `casefold()` 后的 UTF-8 SHA-256。这个锁只防止**同一个端点**被客户端同时拉起，不是核心整个生命周期的全局锁，也不阻止使用不同端点启动其它核心。

## 报文

每个请求建立一次 TCP 连接，发送一行 UTF-8 JSON，以 `\n` 结束；响应也是一行 JSON。

```json
{"token":"从端点文件读取","op":"poll","seconds":1.0}
```

成功响应为 `{"ok":true,"state":...}`，失败为 `{"ok":false,"error":"..."}`。`CoreClient.request()` 会解开响应包裹，返回 `state` 或抛出异常。

核心请求读取上限为 1 MiB；Python 客户端响应读取上限为 32 MiB。两者不能当作同一个帧大小限制。Python 序列化请求使用 `allow_nan=False`，不发送 NaN 或无穷值。

控制调用超时不等于命令没有执行。先查询状态，不自动重发启动记录或输出等控制命令。

## 内存缓存与 poll

缓存按扫描帧保存，容量为：

$$C=\operatorname{clamp}(\operatorname{round}(60f_s),1,200000)$$

4 通道、200,000 帧的电压数据约占 6.4 MB。它用于最近数据的显示和读取，不代替连续记录文件。

`poll` 参数：

| 参数 | 作用 |
| --- | --- |
| `seconds` | 读取最近多少秒，核心钳位到 0–60 秒；默认 0，只取状态 |
| `after_scan` | 可选，跳过早于该源帧号的数据 |

设累计帧数为 $T$，缓存中有 $B$ 帧，请求窗口帧数为 $N=\min(\operatorname{round}(f_s\operatorname{clamp}(seconds,0,60)),B)$。基本起点是 $T-N$。若 `after_scan` 不大于 $T$，起点再取它与 $T-N$ 的较大值；若大于 $T$，忽略它。

响应的 `snapshot_start` 是实际起点，`raw` 为从该点至 $T$ 的样本主序平铺电压数组。时间可按 `(snapshot_start + k) / sample_rate` 重建。即使缓存还保留着更早数据，`seconds` 也会限制返回窗口；不能把它当成保证无遗漏的增量订阅。

## 状态字段

| 字段 | 含义 |
| --- | --- |
| `pid`、`profile` | 核心进程号和当前完整配置 |
| `device_connected`、`running` | 设备连接和 AI 运行状态 |
| `acquisition_started_at_utc` | 采集开始的主机 UTC 时间；无法确定时为 null |
| `total_scans` | 本轮采集累计有效帧数 |
| `capacity`、`buffered_scans` | 缓存容量和当前帧数 |
| `snapshot_start`、`raw` | 本次返回的源帧起点和交织电压 |
| `recording`、`recording_paused` | 是否记录及是否暂停 |
| `recorded_scans` | 本组已记录帧数 |
| `recording_target_scans`、`recording_remaining_scans` | 目标和剩余帧数；无定时目标时为 null |
| `last_recording` | 最近记录目录，或 null |
| `termination_reason`、`error` | 结束原因与错误文本；空错误文本表示没有已报告错误 |
| `output` | AO 状态，未连接等情况下可为 null |

`output` 包含 `running`、`settings`、`actual_rate`、`zeroed`、`completed`。静音状态在 `settings.muted` 中。AO 与 AI 分别控制，停止 AI 是否联动 AO 取决于该会话的联动设置。

## 常用命令

所有命令都需要 `token` 和 `op`。

| `op` | 参数 | 行为 |
| --- | --- | --- |
| `list_devices` | `source?` | 默认枚举原厂后端，返回 `devices` |
| `connect` | `profile` | 连接设备，尚不启动 AI |
| `disconnect` | 无 | 结束采集、记录和输出并释放连接 |
| `start` | `profile`、`recording_path?` | 开始采集，可同时记录到指定新目录 |
| `stop` | 无 | 停止 AI 并收尾记录；健康设备保持连接 |
| `poll` | `seconds?`、`after_scan?` | 查询状态并读取有限窗口 |
| `start_recording` | `path`、`channels`、`experiment`、`recording_options?` | 在当前采集流上开始记录；`channels` 要给全 4 个物理通道配置 |
| `pause_recording` / `resume_recording` | 无 | 暂停/继续文件写入，AI 继续 |
| `stop_recording` | 无 | 结束记录，AI 继续 |
| `set_channels` | `channels` | 更新全 4 个通道的换算配置；记录时禁止修改 |
| `start_output` / `update_output` | `settings` | 开始/更新 AO；运行中不能改更新率及启动触发 |
| `mute_output` | `muted` | 更新静音设置，保持输出流运行 |
| `preview_output` | `settings`、`count?` | 数学波形预览，默认 2048 点，最多 262144 点 |
| `stop_output` | 无 | 停止 AO 并尝试归零，失败会报告错误 |
| `shutdown` | 无 | 没有活动记录、也没有未完成触发时才允许退出核心；先停设备和输出再结束进程 |
| `shutdown_if_idle` | 无 | 仅空闲时退出，响应含 `shutdown` 结果 |

底层还保留 `start_collection`、`device_command`、`fire_trigger`、`accept_record`、`reject_record`、`cancel_trigger`。这些用于多设备和触发开发功能，当前 Agent API 和 1.0 用户流程未提供对应入口，参数以 `core/src/main.rs:Command` 为准。

### 构造合法的 start 报文

`Profile` 必须包含 `config`、`actual_rate`、`channels`、`signal_settings`、`experiment` 和 `recording_root`。`channels` 始终是 AI0–AI3 共 4 项；实际启用子集写在 `config.channels`，不能因此缩短 `profile.channels`。

下面在源码环境构造报文，只打印，不发送：

```python
from dataclasses import asdict
import json
from labdaq.core_client import DEFAULT_ENDPOINT
from labdaq.paths import RECORDING_ROOT
from labdaq.processing import ChannelSettings
from labdaq.protocol import AcquisitionConfig, plan_clock
from labdaq.signals import SimulationSettings

endpoint = json.loads(DEFAULT_ENDPOINT.read_text(encoding="utf-8"))
config = AcquisitionConfig(sample_rate=5000, channels=(0, 1))
profile = {
    "source": "vendor",
    "config": asdict(config),
    "actual_rate": plan_clock(config.sample_rate).actual_rate,
    "channels": [asdict(ChannelSettings(i, f"通道{i+1}", unit="V",
                                       sensitivity_mv_per_unit=0)) for i in range(4)],
    "signal_settings": SimulationSettings().to_dict(),
    "experiment": "测试",
    "recording_root": str(RECORDING_ROOT.resolve()),
    "recording_options": {},
}
request = {"token": endpoint["token"], "op": "start",
           "profile": profile, "recording_path": None}
print(json.dumps(request, ensure_ascii=False, allow_nan=False))
```

物理输入仍需提供 `signal_settings`，因为它是现有 Profile 的必填字段；原厂采集不使用其中的模拟波形。

### AO 参数

当前 GUI/API 提供 `dc`、`sine`、`square`、`triangle`，更新率为 10000、20000、48000、96000 S/s。底层可接受更多波形，但不据此扩展用户接口承诺。

```json
{
  "token": "从端点文件读取",
  "op": "start_output",
  "settings": {
    "waveform": "sine",
    "amplitude_v": 0.2,
    "offset_v": 0.0,
    "frequency_hz": 10.0,
    "sample_rate": 48000.0
  }
}
```

其它 `settings` 字段由 Rust 默认值补齐。非直流波形要求 `abs(offset_v) + amplitude_v <= 3`，周期波形频率需大于 0 且小于实际更新率的一半；直流电平由 `offset_v` 设置。即使直流不使用幅值，幅值参数仍须在 0–3 V 内。扫频使用 `waveform="sweep_linear"` 或 `"sweep_log"`，没有 `sweep_mode` 字段。

原厂后端停止输出后切为单值方式，通过 `olDaPutSingleValue` 写入 0 V，状态 `zeroed` 反映是否成功。设备断开或驱动失败时不能保证输出归零；应读取调用结果及状态。

## 直接 USB 后端资料

本节对应 `core/src/usb_input.rs`、`dt9837.rs`、`output.rs`，不描述 Windows 发布版的 SDK 调用流程。DT9837C 的 IEPE 为 2 mA。

### 端点

| 地址 | 方向 | 代码中的用途 |
| --- | --- | --- |
| `0x01` | OUT | 64 字节命令包 |
| `0x81` | IN | 64 字节命令应答 |
| `0x82` | IN | ADC 连续数据，4 字节/样本 |
| `0x06` | OUT | AO 数据；512 字节头后接小端 u32 样本 |
| `0x88` | IN | 异步状态消息 |

代码通过 Bulk 传输使用这些端点。AO 常用块有 896 点，数据负载为 3584 字节，加头后总长为 4096 字节。

### 前端配置

控制寄存器 `0x02` 的位分配：

| 位 | 功能 |
| --- | --- |
| 0–3 | AI0–AI3 耦合，1=AC，0=DC |
| 4–7 | AI0–AI3 增益，0=±10 V，1=±1 V |
| 8–11 | AI0–AI3 IEPE，1=启用，0=关闭 |

代码在 AC 配置时先设置耦合再设置激励，在 DC 配置时先写入所选激励状态再设置耦合。这两个写入之间没有额外等待；DC 配置也不会强制覆盖所选的 IEPE 状态。传感器是否需要激励，应按实际接线设置。

### 时钟规划

直接 USB 时钟规划以 48 MHz 为基准，输入配置范围为 195.313–105469 Hz。输入请求速率大于 52734 Hz 时使用 256 ticks，否则使用 512 ticks；AO 使用 512 ticks。

`dt9837::clock_plan` 按既定顺序选择 divider（2、4、8、16）和 DivN，并搜索 Q、P 的近似组合：

$$F_{VCO}=48P/Q\ \mathrm{MHz},\qquad
f_{actual}=\frac{F_{VCO}\times10^6}{DivN\times ticks\times divider}$$

搜索范围为 `Q=3..129`、`P<=2055`，保留实现中的截断及首次同误差选择规则。实际采样率由计算结果决定。原厂后端则由 SDK 设置采样率，再读取驱动确认的实际速率。

### DAC 编码与供料

直接 USB 的 DAC 电压编码为：

$$code=\operatorname{clamp}\left(\operatorname{round}\left((V+3)2^{24}/6\right),0,2^{24}-1\right)$$

零电平码为 `0x800000`。直接 USB 代码用 Packet 34 配置 32768 字节 FIFO，启动前预写 4 块、每块 896 点，停止时尝试用 Packet 53 写零。原厂后端不使用这段封包代码。

[数据流与存储](data-pipeline-and-storage.md) · [技术规范目录](README.md)
