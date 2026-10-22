# codex-display

面向 Codex 的任务执行轨迹插件，采用已确认的紧凑分栏界面。

当前需求与设计基准：

- [当前需求 v0.8（P0，含补齐状态的 UI 截图与交互规则）](docs/requirements.md)
- [实现方案 v0.3（架构、数据契约、开发阶段与验收）](docs/implementation-plan.md)
- [UI 原型代码附件（HTML，可直接在浏览器打开）](docs/attachments/trajectory-ui.html)
- [后续演进计划](docs/roadmap.md)

当前 P0 面向自己的本地 Codex，需求包括运行时持续观察、回看历史、输入／输出／缓存 token 统计，以及上下文压缩记录和子任务／子代理分组查看。

主入口采用 Sidebar App，在 Codex 全局侧边栏点击“执行轨迹”，打开并查看当前任务。UI 按已确认的紧凑分栏方案实现：顶部跟随控制、紧凑瀑布图、单行操作列表，以及点击后展开的右侧详情。只绘制应用自身界面，不包含 Codex 客户端外壳。

UI 只展示能够识别的记录，通过一个统一入口下载当前任务的原始记录，下载中保留未识别内容。任务是否结束、耗时与调用次数均按可靠记录显示。

- [运行中只读订阅与延迟实测](docs/validation/realtime-2026-10-22.md)
- [前置验证报告与字段映射](docs/validation/preflight-2026-10-21.md)
- [可复现安装探针](probes/README.md)
- [设计与开发阶段备忘](docs/design-inputs.md)

