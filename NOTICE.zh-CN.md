# Codex Trace 声明（中文参考译文）

[English](NOTICE) | [简体中文](NOTICE.zh-CN.md)

本文件是 [NOTICE](NOTICE) 的中文参考译文，供阅读使用；英文原文保持不变。

Codex Trace
版权所有 (c) 2026 Siyue Zhang

## 研究与设计参考

由 icesixgod 维护的 [Codex Trajectory](https://github.com/icesixgod/codex-trajectory)：
在本地 Codex 轨迹访问及原生插件入口的可行性研究中作为参考。

由 DeepSeek 维护的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/client/ui-trajectory)：
在早期轨迹界面设计中作为参考，包括时间概览、紧凑事件列表和按需详情检查器。

Codex Trace 独立维护，与 Codex Trajectory、DeepSeek 或 OpenAI 无隶属关系，未获其背书，也不是其官方发布版本。参考来源致谢不授予使用其名称或标志的权利。

## 第三方软件

项目的 MIT 许可证适用于 Codex Trace 自身代码。第三方组件保留各自的许可证和版权声明。

依赖清单及完整上游文本见插件包中的 `THIRD_PARTY_NOTICES.txt` 和 `LICENSES/`，或源码仓库中的 `third_party/`。

GitHub 社区市场包不包含 Python 或 Python 依赖代码；uv 会获取并在外部缓存中管理固定运行时与依赖。这些组件保留其上游许可条款。

便携安装器 ZIP 不包含 Python 或 Python 依赖代码；它会准备独立的本地环境并安装锁定依赖。

可选的特定平台原生离线插件 ZIP 包含独立 CPython 运行时和 Python 依赖代码。其中的 `runtime/LICENSES/cpython-runtime/` 目录包含运行时发行包提供的许可证与声明文本，`runtime/runtime-provenance.json` 记录这些文件的哈希及发行来源。

运行时发行包可能包含采用独立条款的库，因此发布该包时必须附带完整许可文件集合。外部 Codex 可执行程序来自用户的本机安装，所有分发包均不包含它。
