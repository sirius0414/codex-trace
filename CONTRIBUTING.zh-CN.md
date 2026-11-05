# 代码规范与 CR 指南

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

本仓库使用 Python 后端、React/TypeScript 前端和 JSON Schema 契约。提交应聚焦一个目的，避免将行为修改、批量格式化和注释补充混在同一变更中。

## 注释要求

- 新增或修改复杂模块时，在模块入口说明职责、上下游和主要数据边界。Python 使用模块 docstring；TypeScript/JavaScript 使用简短的模块注释。
- 非显然的公共接口、状态归约器和有副作用的操作，需要说明输入约束、输出语义及失败／恢复方式。Python 使用 docstring，TypeScript/JavaScript 优先使用 JSDoc；类型已经表达的参数无需重复罗列。
- 在关键分支旁解释设计原因和不可破坏的约束，尤其是异步响应失效、身份校验、版本与游标、增量合并、证据不足、文件发布及幂等重试。审阅者应能在代码附近找到依据。
- 新增业务说明优先使用中文；保留协议字段、状态名和 API 名称的原文。已有准确的英文注释可以保留。
- 注释解释“为什么”和“边界是什么”，不逐行翻译代码。简单取值、直观渲染和普通循环无需凑注释数量；复杂分支也不能仅靠函数命名代替说明。
- 修改行为时同步更新相关注释。只描述当前代码能保证的事实，不把假设、降级结果或未验证的宿主行为写成保证。
- `contracts/types.ts` 是生成文件；契约形状在 JSON Schema 中修改，类型生成规则在 `contracts/generate_types.py` 中维护，运行生成器后检查一致性。

## 关键约束

CR 时重点检查这些约束是否能从实现及就近注释中确认：

- 正式入口的任务数据通过 app-only MCP `_meta` 传递；`content` 和 `structuredContent` 不携带任务正文。`server.py` 是保留的 M0 诊断入口，不作为正式数据通道。
- 已选择任务的异步响应核对完整 `Binding`；同一任务 ID 不能代替来源、会话和选择 epoch 的匹配。选择前的 open/list 响应校验各自的页面会话及请求代次。generation、revision 和游标各有自己的作用域。
- 已记录内容、运行状态和读取状态分别处理；证据不足保留未知，用量快照不直接相加，通信关系不自动成为父子关系。
- 原始来源读取校验身份、根目录及继承范围；导出范围独立于 UI 筛选、折叠和分页，多来源采集不能声称是原子快照。
- 保存超时保留原请求标识并核对结果。只有 `verified` 表示保存已读回校验；`unknown` 不能触发另一次新请求保存。释放临时资源不撤销已受理保存、不删除最终文件。

## CR 阅读入口

| 关注点 | 主要入口 |
| --- | --- |
| MCP 注册、宿主身份和私有结果 | `backend/codex_trace/app.py`、`service.py` |
| 历史／原始来源、证据投影、共享观察 | `backend/codex_trace/adapters/`、`domain/`、`observation.py` |
| 冻结导出、持久回执和本地发布 | `backend/codex_trace/raw_export.py`、`exports.py`、`export_store.py` |
| 前端请求调度、状态合并及恢复 | `frontend/src/main.tsx`、`bridge/`、`state/` |
| 列表分组、概览和阅读锚点 | `frontend/src/components/model.ts`、`EventList.tsx`、`Overview.tsx` |
| 构建、离线打包、更新及隔离验证 | `vite.config.ts`、`scripts/` |
| 请求／响应契约 | `contracts/README.md`、`trajectory.schema.json`、`tools.json` |

## 文档语言

项目公开文档使用常规文件名保存英文版，以 `.zh-CN.md` 结尾的文件保存简体中文版。修改说明或政策时应同步更新两种语言，包括语言切换链接、命令、示例和支持边界。这项要求针对文档，不要求源代码注释重复提供翻译。上游许可证原文保持不变。

## 验证

按变更范围运行相关检查；涉及多个层次时执行完整检查。使用已安装的依赖，环境准备见 [README](README.zh-CN.md#构建与验证)。

```sh
npm run build
npm test
PYTHONPATH=backend:tests .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python contracts/generate_types.py --check
git diff --check
```

注释变更也应核对注释与实际分支一致，并确认没有夹带执行逻辑修改。测试需使用合成任务及临时目录；真实插件更新和桌面入口验收按 README 的独立流程执行。
