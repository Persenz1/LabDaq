# LabDAQ 文档

## 使用

- [用户指南](用户手册/user-guide.md)：连接、通道设置、采集、记录、AO、回看与导出。
- [Agent API](用户手册/agent-api.md)：命令行和 Python 接口。

这两份手册随安装包分发。安装包与各版本验证结果见 [GitHub Releases](https://github.com/Persenz1/LabDaq/releases)。源码依赖、启动及构建命令见[项目首页](../README.md)。

## 实现参考

- [技术规范索引](技术规范/README.md)。
- [数据流与存储](技术规范/data-pipeline-and-storage.md)：记录格式、换算与分析算法。
- [IPC 与硬件控制](技术规范/ipc-protocol-spec.md)：本机通信和直接 USB 开发协议。
- [QuickDAQ 功能对照](技术规范/quickdaq-feature-matrix.md)：1.0 可用功能与仓库保留代码。
- [旧入口](功能实现/README.md)：保留已有链接，转至对应规范。

## 后续开发

[未来功能开发规划](开发文档/未来功能开发规划.md)说明下一阶段的数据分析工作。开发文档目录只保留这份规划，旧开发记录与 archive 已移出仓库。
