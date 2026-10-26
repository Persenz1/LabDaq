LabDAQ · Windows x64

运行安装程序完成安装，再从开始菜单打开 LabDAQ。
安装包附带 Python、PySide6/Qt、NumPy/SciPy 和 Rust 采集核心，无需安装 Python 或配置开发环境路径。
安装程序包含并调用原厂 DT9837C 驱动及 Open Layers 运行库安装器，需要管理员授权。
如安装程序提示重启，请先保存其他工作，重启后再连接设备并使用。

程序默认安装到 %ProgramFiles%\LabDAQ，通常为 C:\Program Files\LabDAQ。
当前用户配置与运行状态保存在 %LOCALAPPDATA%\LabDAQ。
自动核心配置为 .runtime\core.profile.json，连接端点为 .runtime\core.json，主题缓存为 .runtime\theme。
默认记录保存在 Windows“文档”目录下的 LabDAQ\recordings，跟随系统实际的文档位置。
可以在界面选择其他记录根目录；当前选择在本次会话及接回已有后台时恢复，完全退出核心后重开使用默认目录。
更换程序安装目录不改变配置和默认记录位置。
关闭界面会保留正在进行的采集或 AO 输出，重开可重新接回。
更新或卸载前，请先停止记录、采集和 AO，再关闭窗口。
卸载 LabDAQ 保留用户配置和已有记录，也不自动卸载可能被其他软件共用的原厂驱动。

本机 Agent 接口：安装目录内的 LabDAQ-api.exe，使用 --help 查看命令。
CLI 输出 UTF-8 JSON，使用与界面相同的后台核心；不需要外部 Python 环境。
Python 接口及示例位于 _internal\docs\用户手册\agent-api.md。

AO 更新率：10、20、48、96 kS/s，默认 48 kS/s，与 AI 采样率独立。
周期波形频率必须低于更新率的一半；实际上限还受设备回读时钟限制。
峰值幅度为 0–3 V，偏置为 -3–3 V，|偏置| + 峰值幅度不得超过 3 V。
直流电平由偏置设置。输出运行时先停止并归零，再修改参数。
