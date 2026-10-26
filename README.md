# LabDAQ

LabDAQ 是面向 Data Translation DT9837C-BNC 的中文桌面数据采集软件，采用原厂 Open Layers 驱动、Rust 采集核心和 Python / Qt 界面。

1.0 的界面提供实时采集、AO 输出和数据回看三个页面，支持原始电压记录、传感器单位换算、软件调零、暂停与继续记录、测量游标，以及 CSV/TXT 导出。本机 Python API 和 JSON 命令行可与界面共用同一采集核心。分析、记录库和多组比较的代码仍保留在项目中，1.0 暂未开放这些界面入口。

已完成 Windows x64 安装包构建、清理旧驱动后的本机安装，以及安装版 API 的短记录、导出和停止后重新预览。1.0 只发行安装包，不提供便携版；各版本验证结果见 [GitHub Releases](https://github.com/Persenz1/LabDaq/releases)。

## 设备与采集

当前面向单台 DT9837C-BNC：

| 项目 | 参数 |
| --- | --- |
| 模拟输入 | 4 路同步、24 位，AI0–AI3 |
| 输入量程 | 每通道可选 ±10 V 或 ±1 V |
| 输入耦合 | AC / DC |
| IEPE 激励 | 2 mA，可按通道开关 |
| AI 采样率设置范围 | 195.313–105469 Hz，实际采样率由驱动回读 |
| 模拟输出 | 1 路、24 位、±3 V |
| AO 波形 | 直流、正弦、方波、三角波 |
| AO 更新率 | 10、20、48、96 kS/s，默认 48 kS/s |

硬件参数见原厂 `UM9837` 手册，其中第 14 页区分了 DT9837 各型号的规格；DT9837C 的 IEPE 为 2 mA。

正式设备路径使用原厂 Open Layers C 后端，不需要安装 QuickDAQ 或配置 WinUSB/Zadig。直接 USB 后端保留用于开发研究。

Rust 核心负责采集、记录和输出，界面负责配置与显示。关闭界面后，正在进行的采集、记录和 AO 可继续运行；重开界面可接回原有核心。AI 与 AO 独立启停，停止采集不等于停止输出。正常停止 AO 时，核心会尝试写入 0 V，并报告归零状态。

## 运行与安装

安装包包含 Python/Qt 运行环境和 Rust 核心，并调用完整的原厂 OEM 安装程序部署驱动与 Open Layers 运行库。安装需要管理员权限，日常启动使用普通用户权限。本机已确认安装成功、设备识别及安装版 API 采集；安装程序尚未代码签名，Windows 可能显示“未知发布者”或 SmartScreen 提示。安装步骤见[用户指南](docs/用户手册/user-guide.md)。

源码运行需要已有的 Python 3.12 或更高版本环境、支持 Rust 2024 版的工具链，以及可用的 C/C++ 构建工具。先用已有 Python 环境安装 `requirements.txt` 和项目的 `test`、`build` 扩展。完成配置后，在项目根目录执行：

```powershell
. .\tools\dev-env.ps1
cargo build --release -p labdaq-core
.\launch.ps1 -PythonPath "C:\path\to\python.exe"
```

依赖安装使用所选解释器的 `-m pip install -r requirements.txt` 和 `-m pip install -e ".[test,build]"`。发布构建还需 Inno Setup 6，以及本地 `resources/vendor/installers/DataAcqOMNI_V7.8.9.zip` 原厂归档：

```powershell
.\tools\build-release.ps1 -PythonPath "C:\path\to\python.exe" -IsccPath "C:\path\to\ISCC.exe"
```

`launch.ps1` 不传 `-PythonPath` 时，会查找已登记的 `agents-py312` Conda 环境。源码中的 `--demo` 和模拟/HPF 采集入口用于开发测试；安装版只开放原厂设备采集，仍可只读回看 HPF 文件。

## 配置与记录目录

| 用途 | 安装版默认位置 | 源码运行位置 |
| --- | --- | --- |
| 程序及内置运行环境 | `%ProgramFiles%\LabDAQ` | 项目目录 |
| 当前用户配置及运行状态 | `%LOCALAPPDATA%\LabDAQ\.runtime` | 项目内 `.runtime` |
| 默认记录根目录 | Windows“文档”目录下的 `LabDAQ\recordings` | 项目内 `recordings` |

“文档”目录通过 Windows 接口取得，跟随用户迁移或重定向，不固定为 `%USERPROFILE%\Documents`。核心配置文件为 `.runtime\core.profile.json`，端口凭据为 `.runtime\core.json`。

界面按“记录根目录/实验名/工况-序号”创建记录组。自选根目录当前只在本次会话及接回已有后台时恢复；完全退出核心后重开界面，使用默认目录。更换程序安装目录不改变用户数据位置，卸载保留配置和记录。

每组原生记录包含 `raw.f64` 和 `metadata.json`：前者按样本主序交织保存各通道的小端双精度电压，后者保存采集配置、换算参数、时间和记录分段。软件调零、单位换算和显示滤波不覆盖原始电压。回看导出所勾选通道的完整记录，时间范围条和游标不改变导出范围。

## 自动化接口

安装版使用 `LabDAQ-api.exe`；源码环境可运行 `python -m labdaq.api`，或调用 `labdaq.api.LabDAQ`。接口可查询状态、连接设备、控制采集与记录、读取最近数据、控制 AO 和导出记录。

外部 Python 不会自动发现安装版核心，需先通过安装版界面或 CLI 启动核心，再显式指定安装版端点。调用示例和行为约定见 [Agent API](docs/用户手册/agent-api.md)。

## 文档

- [用户指南](docs/用户手册/user-guide.md)与 [Agent API](docs/用户手册/agent-api.md)：操作步骤和自动化调用。
- [技术规范](docs/技术规范/README.md)：后端、通信、记录格式和算法。
- [未来功能开发规划](docs/开发文档/未来功能开发规划.md)：下一阶段的数据分析和后续扩展。
- [完整文档索引](docs/README.md)。

第三方代码和依赖声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
