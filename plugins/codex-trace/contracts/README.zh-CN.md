# 本地数据载荷契约

[English](README.md) | [简体中文](README.zh-CN.md)

`trajectory.schema.json` 是权威 JSON Schema（Draft 2020-12），版本为 `1.0.0`。默认根定义校验 `ThreadSnapshot`；可复用定义还覆盖 `tools.json` 列出的其他服务载荷。这是拟定的应用契约，不代表宿主集成或 M1 已获认可。M0 的结果决定 MCP 信封形式。轨迹正文和导出字节只能放在应用可见的元数据中，不能进入模型可见的结果字段。

使用 `python3 contracts/generate_types.py` 生成前端类型。在 CI 中运行 `python3 contracts/generate_types.py --check`，检查 Schema 与类型是否发生偏差。TypeScript 无法表达所有 JSON Schema 约束（例如整数范围），因此服务边界仍需对载荷进行运行时校验。

## 契约设计

- 每个已选任务的响应都携带 `Binding`：Schema、来源实例、观察会话、选择 epoch 和线程 ID。仅线程 ID 相同不足以接受响应。列表和打开响应发生在任务选择之前。
- 缺失的测量值和标识符使用明确的 `null`，不能使用零或虚构标识。已记录的零用量与用量不可用不同。每份快照包含各项指标的可用性。
- 事件类型枚举没有 unknown 占位值。不受支持的来源记录保留在原始来源目录和下载中，不进入 UI 投影。
- 事件 revision 与快照的 generation/revision 相互独立。事件来源使用不透明的片段 ID 或 API 身份，不接受前端提供的文件路径。
- 执行、阅读和跟踪状态使用独立枚举。状态证据明确标注适用范围。输出完整性独立于执行状态。
- 正文片段区分来源截断和 UI 分块。缺失的原始记录关联在 `EventDetail` 中明确表示，不能从相邻行补全。
- `UsageSnapshot` 保留归属、范围、epoch、顺序、聚合方式、缓存语义，以及继承和子任务归因。它不代表模型请求次数，契约也不对快照求和。
- `Turn.usage` 和 `TraceEvent.usage` 可选地携带有明确范围的快照（旧载荷可以省略）。`modelUsage` 事件代表一个明确的响应身份，独立于工具和消息条目。压缩检查点可以恢复轮次快照，但不能创建新的请求事件。
- `Turn.executionIntervals` 可选地以 `Timing[]` 保留已记录的执行区间。原始 start/complete/abort 边界保留各个恢复执行阶段的独立性；API 的 `startedAt`/`completedAt` 值为 Unix 秒，只补充缺失的外层边界。已中止的执行阶段不能延伸到后续 API 完成时间。开放的开始记录保留 `endMs: null`；不能根据时长或当前时钟推算缺失的开始时间。没有可用证据时省略该字段。这些区间表示活跃执行窗口，不表示已测量的模型/工具阶段，也不改变生命周期状态或任务的实际经过时间统计。API 轮次时间使用 `source: null`，并在 `basis` 中写明字段名，因为 API 轮次没有可由现有 `SourceRef` 表达的条目身份。
- 分组关系保留类型和证据。通信关系和 fork 关系独立于子任务层级存在。
- 下载使用一个可判别的请求类型（`create/status/read/release/commit/result`），不携带 UI 筛选条件或列表游标。每次导出都有冻结的清单、来源范围、校验和与采集时间；`atomicSnapshot` 始终为 false。分块游标是不透明的，每个字节块都有独立的偏移、大小和校验和。
- 视图状态只包含偏好和标识符。正文载荷、来源路径和认证材料不能作为未声明属性加入。
- `ViewState.turnRange` 可选地以绝对实际时间 `TimeRange` 保留记录高亮范围；省略或 `null` 会清除它。UI 根据每条记录的实际时间戳或已记录区间，只高亮与范围相交的记录。没有时间或不匹配的记录仍然可见，但不高亮；轮次/分组标题标示其中包含的匹配。它不会筛掉记录或重排列表；现有 Agent 和事件筛选独立控制可见性。用户明确完成范围选择后，通过现有阅读锚点显示首个可见匹配，并在需要时只展开已加载的目标路径，同时保留详情选择。预览、取消、清除、视口导航和恢复已保存范围都不会请求跳转。正时长区间按半开区间判断相交；点记录包含左边界、不包含内部右边界，但包含坐标轴的最终端点。该字段不属于 `Filters`：它不改变已加载正文的查询请求或搜索缓存键。恢复偏好时保留原始实际时间位置，不把它们重新解释为新压缩的活跃时间轴坐标。
- `ViewState.timelineSpacing` 仍被接受，以兼容已保存视图。按 Turn 分段的顺序概览忽略此旧时间间距偏好。图中位置是布局单位，不是经过时间，也绝不作为 `TimeRange` 持久化。已有绝对 `turnRange` 仍是基于原始时间的高亮，轮次列表仍可显示其对应记录。

## 本地 ZIP 交付

授权目标位置为当前操作系统用户的 `~/Downloads/Codex Trace/`。服务选择 ZIP 文件名并解析目标位置；请求不接受路径、目录、文件名或覆盖标志。`LocalExportPath` 是仅供输出的绝对路径。已有 POSIX 路径继续有效。Windows 回执使用原生本地盘符路径，例如 `C:\测试 用户\Downloads\Codex Trace\codex-trace-a.zip`。UNC 共享、相对盘符路径、设备命名空间、备用数据流、DOS 设备名，以及以点或空格结尾的路径分量均被排除。网络文件系统的锁与原子发布不在本地导出契约范围内。运行时检查必须落实准确的已解析用户主目录、拒绝符号链接和重解析点逃逸，并避免覆盖既有文件；仅靠 Schema 无法建立文件系统范围保证。该契约不包含上传或 HTTP 端点。

`download_records` 仍是唯一接口，UI 保留一个导出入口。操作顺序如下：

1. `create(binding, requestId)` 冻结来源成员及范围，并返回导出句柄。重复使用同一键会返回同一次导出，而不是采集另一份快照。create 响应丢失时，通过重复原请求恢复，不能分配新键。
2. `status(binding, exportId)` 报告采集进度。`ready` 表示冻结 ZIP 已准备好，不表示已保存。`read(binding, exportId, cursor)` 保留有界、可重复的分块访问；本地保存不要求全部 ZIP 字节经过 UI。
3. `commit(binding, exportId, requestId)` 请求在本地保存该冻结 ZIP。此键不同于 create 键。对尚未就绪的导出执行 commit 会返回 `exportNotReady`。服务在最终 ZIP 可发布之前记录操作，并在再次尝试写入前，根据操作所属的目标位置核对被中断的提交。
4. `result(binding, requestId)` 在响应延迟、丢失或超时后查询提交结果，即使调用方没有导出句柄也可以使用。它不会发起保存。以相同键重复 `commit` 也保持幂等。
5. `release(binding, exportId)` 释放临时快照/分块资源。它不能删除已保存 ZIP，也不能遗忘其提交回执。已经受理的 commit 在结果确定前保留所需临时资源，即使 UI 已释放句柄或切换选择；release 不能证明操作已取消。

`requestId` 绑定操作类型、来源实例、根线程，以及 commit 所对应的冻结导出。将同一键用于另一操作或导出，会返回 `idempotencyConflict`，不执行第二次写入。视图会话和选择 epoch 是响应路由保护，不代表允许以同一键创建另一份导出。在新视图绑定下恢复时，仍须获得对同一来源和任务的授权；每个响应都携带请求视图的绑定，因此过时响应仍会被拒绝。回执持久化与结果核对必须能跨 MCP 进程重启恢复。回执过期或缺失时必须返回 `unknown`，不能在同一键下静默执行新的 commit。回执/墓碑清理独立于临时导出过期；任何自动清理都不得移除用户已保存的文件。

`DownloadResult.save` 独立于快照 `status` 描述交付状态：

| 保存状态 | 含义 |
| --- | --- |
| `notRequested` | 这份已知导出没有受理过 commit，也没有附带请求键。 |
| `pending` | commit 已受理或正在核对中；不要创建替代请求。 |
| `verified` | 最终 ZIP 已写入，并在 `verifiedAt` 时读回核验；回执包含服务端文件名、绝对路径、字节数及 SHA-256。 |
| `failed` | 已确认失败，且本请求未发布最终文件；传输超时绝不等同于此状态。 |
| `unknown` | 无法确认结果，包括请求记录缺失的情况；文件可能已经存在。 |

只有 `save.status == "verified"` 才允许显示保存成功。路径的 basename、文件名、字节数和哈希必须与实际保存的 ZIP 及冻结快照一致。回执记录某个时间点的核验结果，不保证用户此后不能移动或删除文件。来源清单不完整仍可能产生已校验 ZIP：保存成功绝不表示所有原始来源都可读。未知字节、无效 UTF-8、格式错误的行和尾部不完整记录都必须在归档中逐字节保留。

## 验证边界

Schema 校验只能证明结构、枚举成员和局部值约束。服务/领域测试还须验证身份授权、响应 epoch 匹配、revision 单调性、字节范围顺序、时间戳顺序、状态证据范围、明确父子关系、环拒绝、用量聚合和来源溯源。契约测试通过并不意味着这些条件已满足。本地导出服务测试还须证明持久幂等、无覆盖的原子发布、重启/超时核对、最终文件读回哈希，以及回执授权和跨 release 保留。契约夹具不会创建文件，也不能证明这些运行时属性。

`tests/fixtures/contracts/` 完全由合成示例组成。夹具目录说明每个示例对应哪一定义。场景输入覆盖延迟的跨任务响应、重复/下降的用量、已保存输出更新，以及通信关系与父子关系的区别。它们是供后续实现使用的测试规范，不代表相关 reducer 已经存在。

## Preview 阅读元数据

`ContentRef.reader` 和 `ContentChunk.reader` 是可选且向后兼容的 `ReaderMetadata`。它们描述单个正文部分，独立于分页、`sourceTruncated` 和 `OutputCompleteness`。JSON 格式提示不代表当前已加载块能被解析或包含全部字段。服务在每个块中返回相同元数据，不解释不完整 JSON。

投影为 commandExecution、fileChange、结构化 MCP/动态工具值，以及旧版 function 参数提供格式证据。消息采用 Markdown *阅读约定*，并在 `origin` 中明确标示；这不代表来源声明每条消息都是 Markdown。命令参数数组仍保留为 JSON 数组。已知命令若没有记录 shell 信息，就不能获得猜测的 Bash 语言。未知工具只获得结构提示，不虚构参数说明。只有已确认的调用/结果身份，才能把工具身份与结果关联。

`FieldDescription` 使用 JSON Pointer 路径，并区分 `scope: protocol` 与 `scope: tool`。说明可以携带类型、单位、必填标志、枚举、来源 URL/版本，以及值为 `exact` 或 `reference` 的 `versionMatch`。可选 `format`/`language` 提示只作用于所描述路径。说明文本和链接是数据，不是可执行内容。没有定义时省略。元数据随正文一起打包并返回；悬停不会获取 Schema。

当前外部参考覆盖范围（核对日期：2026-11-03）：

- `exec_command` 和 `functions.exec_command`：十个输入参数与六个输出字段；`write_stdin`/`functions.write_stdin`：四个输入参数及相同输出字段。依据为官方 [shell 工具定义](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs)。
- 已知 MCP 结果信封：`content`、`isError`、`structuredContent` 和已核实的文本内容块。依据为 [MCP 2025-06-18 Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。这些说明不描述工具的任意输入参数，也不描述 `structuredContent` 内的业务字段。
- 已知 fileChange 条目：其已记录的 `diff` 正文。依据为 [app-server Schema](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartResponse.json)。更新 diff 与新增/删除文件正文保留各自不同的格式提示。

本仓库读取的历史来源目前不保留工具 Schema 或协商后的 MCP 协议版本。因此，上述所有外部定义都使用 `versionMatch: reference`，即使恰好记录了生产者版本也是如此。仅有生产者版本不能证明工具 Schema 匹配。必填性和枚举值描述的是参考定义，不是对历史调用的校验。未知字段仍可阅读，只是不附说明。

对于原始工具字段，Preview 从关联物理记录中截取原始 JSON 值子串。字符串值只解码其 JSON 字符串包装；对象、数组和数值保留数字字面量、键顺序、空白和重复键。Raw 仍是整条关联记录。API 值到达时已由 API 客户端解码；`origin` 明确标示 `API decoded projection`。不能从该 API 投影恢复传输字节的原始写法；有可用关联 Raw 记录时，它们仍是证据。阅读元数据不会把未知来源完整性提升为已知，也不会根据日志措辞推断截断。
