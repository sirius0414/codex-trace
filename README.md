# codex-display

Codex 本地任务执行轨迹插件，当前版本 **0.1.0**。正式入口使用 Python MCP 服务和随包提供的 React 界面，不依赖开发服务器或远程 UI 资源。

## 使用

在聊天右上角 **New Tab → 执行轨迹** 打开面板。此入口的来源语义已在目标宿主验证，服务会先只读校验任务 ID。无法确认时显示任务选择器。全局侧栏也提供“执行轨迹”，按宿主规则全屏打开；全局来源语义未验证，因此始终要求选择任务，不根据最近任务或环境变量猜测来源。

界面提供紧凑时间概览、事件列表、输入／输出／原始记录详情、轮次和已确认子任务分组、token 用量与上下文压缩记录。只显示已识别内容，未知类型不会伪装成正常事件。搜索和筛选作用于已加载范围；历史可继续加载，重开时恢复阅读位置。两处 Demo 交互问题随正式界面修复，模拟推进和外层预览控件不进入产品包。

轮次标题显示该轮已记录的输入、输出和缓存 token；列表的「Token 入 / 出」列显示单次模型请求用量，悬浮可查看缓存和精确值，详情「概述」提供轮次与条目用量。带明确 `response_id` 的用量显示为独立「模型用量」条目，不重复分摊到工具或消息。缺失显示「未记录」或 `—`；旧 `token_count` 无法可靠归属到轮次或请求时只保留任务级观测。累计快照取最新值，多个 session 不相加，缓存不重复计入输入；消耗不代表当前上下文占用。

时间概览支持滚轮和双指捏合缩放，以光标或双指中心为锚点；拖动、横向滚动或 Shift＋滚轮平移。图内导航只改变可见范围，不筛掉列表记录；点击标记仍可查看详情。可用缩放按钮、底部平移滑杆或键盘 `+`／`−`、左右箭头导航，双击、`Home` 或「全貌」恢复全部范围。新增记录保留手动缩放位置；没有时间证据时按记录顺序导航。

三条轨道展示记录的时间证据，输入时间点及聚合簇不代表连续执行。下方覆盖条补齐整个可见范围：彩色为有起止时间或明确时长的记录区间并集，斜线为未归因时间，点纹为密集区间聚合。等待、调度、未记录或未加载的时间不会自动归给模型；任务创建时间也不表示本轮执行开始。缩放可查看聚合中的细节，未归因时长随当前范围更新。

工具栏的 **EN／中文** 按钮可切换界面语言；调色板按钮打开「语言与外观」，可选择 **经典、柔和、终端** 三种 UI 风格。首次使用依据浏览器语言选择中文或英文，偏好保存在本机浏览器中，刷新后保留；存储不可用时仍可在当前页面切换。明暗配色跟随宿主，切换语言或风格保留任务选择、筛选及阅读位置，原始记录与来源说明保留原文。

选择或展开子 Agent 时会直接读取其记录，不依赖阅读偏好保存成功；暂停跟随时仍可加载。后台发现子任务及其完成状态，并不表示当前页面已经读取了子任务内容。

“跟随”持续接收新增内容；暂停后保持阅读锚点并显示待查看更新。页面隐藏时暂停前端轮询，停止请求约 60 秒后后台观察租约到期；会话和缓存按闲置时间与容量策略回收。时间、token、关系和状态只有存在可靠来源时才确认；冷读取状态、暂时无新增或读取失败不代表任务完成。缺段、读取限制和不明状态会保留明确说明。

## 导出与恢复

点击“导出记录”会独立枚举当前任务的已知来源和已确认 spawn 子任务，生成冻结 ZIP；界面当前筛选、折叠和分页不缩小导出范围。未知类型、坏行和末尾半行原样保留。ZIP 的 manifest 记录来源、复制范围、SHA-256、继承范围及缺失／不可读部分。

导出不是跨文件原子快照：运行中的来源可能先后变化，每段都有自己的采集时间和冻结上界。成功保存不代表来源完整，应同时查看完整性说明。没有继承证明的父任务后续内容不会被当作本任务事件；物理来源与逻辑继承范围在 manifest 中分开记录。

保存目录固定为 `~/Downloads/Codex Display/`，前端不能指定路径。服务流式写入、无覆盖发布并读回验证；只有显示“本地导出已校验”和实际路径，才确认保存成功。目录为 `0700`，ZIP 为 `0600`。目录不可用或权限不足时会给出失败结果。

- 等待或超时后的“结果未确认”表示可能已经保存。使用原保存请求核对，不能把它当作失败而自动换名再存一份。
- 页面恢复且原会话的恢复标识仍在时，可核对持久化保存记录；完全关闭后新建标签不保证保留这些标识，界面暂不提供旧回执列表。明确重试仅恢复原请求、原候选文件；已经开始发布但结果不明的请求只做核验。
- 只有明确失败表示该请求未发布最终文件。需要新的导出时由用户重新发起。
- 释放临时导出不会撤销已接受的保存，不删除最终 ZIP，也不删除回执。

## 构建与验证

开发与 CR 请遵循 [代码规范与 CR 指南](CONTRIBUTING.md)，其中列出注释要求、关键约束和分层阅读入口。

运行需要 **Python ≥3.14 且 `compression.zstd` 可用**，用于读取压缩来源；打包会实际检测压缩能力和锁文件中的依赖版本。本地前端开发基线为 Node.js 25。使用本机桌面配套的 Codex app-server 可执行文件。

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --ignore-scripts
npm run build
npm test
PYTHONPATH=backend:tests .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python contracts/generate_types.py --check
```

安装阶段准备独立运行环境并固定绝对路径。以下首次打包命令要求运行环境与输出目录尚不存在；已有同版本环境时去掉 `--prepare-runtime`，把 `--python` 指向该环境中的 `bin/python`。重建包使用新的输出目录，脚本不会覆盖旧包。

```sh
.venv/bin/python scripts/package_plugin.py \
  --output build/plugin-0.1.0 \
  --python "$PWD/.venv/bin/python" \
  --prepare-runtime "$HOME/.local/share/codex-display/runtime/0.1.0" \
  --codex /absolute/path/to/desktop/codex

.venv/bin/python scripts/verify_install.py \
  --package build/plugin-0.1.0 --cache-copy-only \
  --out docs/validation/evidence/m5/cache-install.json
```

包内包含后端、契约和单个离线 HTML，启动模块为 `codex_display.app`。Python 和 Codex 可执行文件不随包复制，GUI 不依赖终端 PATH；必须保留 `runtime.json` 记录的绝对路径，移动后重新打包安装。依赖仅在准备环境时安装，插件启动不下载依赖。版本与依赖锁定可重复使用，构建时间等元数据不保证逐字节相同。

`--cache-copy-only` 只复制到一次性缓存，使用正式 MCP 服务和全合成 app-server 验证身份、app-only 私有结果、原始 ZIP、幂等保存、回执及只读调用。测试覆盖 `CODEX_HOME`、状态目录和导出目录，所有数据写入临时目录，不读真实会话，不写真实 Downloads 或索引。去掉该选项并传入 `--codex`，可在临时 Codex home 验证 CLI 市场安装及缓存路径解析；这仍不修改真实安装。两种检查都不代表桌面入口验收。

## 安装、更新与卸载

已有安装和前端依赖时，可以在仓库目录执行一键更新：

```sh
npm run plugin:update
```

脚本 `scripts/update_plugin.py` 自动复用已安装插件记录的 Python 环境和 Codex 可执行文件，依次构建、检查契约、打包、隔离验证、生成本地市场、切换注册并重新安装。每次输出到独立的 `build/update-时间戳-随机后缀/`；安装后逐文件比对缓存，并核对本插件的 MCP 启动配置。安装结果保存在该目录的 `installed.json`。保留当前注册的市场目录及其引用的 Python 环境。

界面右下角常驻显示发布版本和 8 位源码指纹，例如 `v0.1.0 · 12ab34cd`；悬浮可查看完整指纹及构建时间。指纹包含当前前后端、契约和构建配置的实际内容，包括未提交的修改。同一源码重复构建保持相同指纹，构建时间单独记录；安装包的 `runtime.json` 中保留对应的 `frontendBuild` 元数据。

可用 `npm run plugin:update -- --python /absolute/path/to/python --codex /absolute/path/to/codex` 覆盖自动查找的路径。脚本不会安装依赖、创建运行环境或自动重启桌面应用；首次安装仍需完成上面的环境准备。默认操作当前 `CODEX_HOME`，未设置时使用 `~/.codex`。

只准备安装包时运行 `npm run plugin:update -- --prepare-only`，随后用 `npm run plugin:update -- --install-prepared /absolute/path/to/build/update-...` 安装该目录。准备和安装必须使用相同的 `CODEX_HOME`。安装阶段失败会尝试恢复原市场注册，并写入 `install-failed.json`；这不代表插件缓存已经回滚，修复后可重新运行更新。更新不删除任务记录、索引或已导出的 ZIP。

也可以分步生成可检查的本地市场：

```sh
.venv/bin/python scripts/prepare_marketplace.py \
  --package build/plugin-0.1.0 --output build/marketplace-0.1.0 \
  --codex /absolute/path/to/desktop/codex
```

此命令只生成文件并打印注册、安装命令，不自动修改 Codex 配置。检查后执行输出中的命令，安装 `codex-display@codex-display-local`。更新时用新版本包重新生成市场并安装；目标宿主需要重新加载插件资源，必要时在方便时重启 Codex，单纯关闭重开标签不保证更新 New Tab 菜单。脚本不会重启桌面或停止原任务。

如果 `codex-display-local` 已注册在旧目录，新目录不能直接用同名 `marketplace add` 覆盖。先执行 `codex plugin marketplace remove codex-display-local`，再执行新市场生成器打印的 `marketplace add` 和 `plugin add` 两条命令。移除市场注册会保留已安装缓存；无需先卸载插件，也不会清理索引或最终 ZIP。最后用 `codex mcp list --json` 核对本插件的 `cwd` 指向目标版本缓存下的 `backend/`。

安装后分别检查：聊天 A 打开、重复点击、切到 B、关闭重开、全局入口选择任务；确认任务身份、阅读位置和跟随行为。用一次导出核对实际路径、文件和完整性说明。开发验收材料只保存在本地忽略目录，不进入插件包。

本插件的默认本地数据：

| 位置 | 内容与保留方式 |
| --- | --- |
| `~/.local/share/codex-display/index/` | 派生索引、阅读偏好、持久导出回执和临时 ZIP；目录仅用户可读 |
| `~/Downloads/Codex Display/` | 已保存的最终 ZIP；插件不会自动删除 |
| `~/.local/share/codex-display/runtime/0.1.0/` | 独立 Python 运行环境；插件运行期间必须保留 |

未被已接受保存请求保留的临时 ZIP 默认 30 分钟过期，每 60 秒检查清理。pending／unknown 保存所需的材料会保留；回执与锁文件没有自动过期清理策略。不要在尚待核对保存结果时删除索引目录，否则会丢失恢复依据。派生索引包含任务摘要，最终 ZIP 保留原始记录内容。

卸载使用 Codex 插件管理或本机 CLI 的 `plugin remove codex-display@codex-display-local`。停止本插件后，可按需移除其本地市场和专属运行环境；确认没有待核对导出，再清理本插件状态目录。**保留 `~/Downloads/Codex Display/` 中的最终 ZIP**，由用户自行决定何时删除。不要删除 Codex 会话文件、运行数据库或其他插件缓存。

## 只读与资源边界

后端只允许初始化及 `thread/list`、`thread/read`、`thread/turns/list`、`thread/items/list`。任务数据只放在 app-only MCP 结果 `_meta` 中，不进入模型可见的 `content` 或 `structuredContent`。原始来源仅从配置的 sessions／archived_sessions 根目录读取，并校验身份、继承边界与文件变化；不允许任意路径或符号链接绕过。

插件不调用任务启动、恢复、停止、压缩或审批接口。独立 app-server 可能维护自己的运行缓存；插件只关闭自己持有的读取进程，不控制原任务。增量读取、macOS 文件通知与轮询兜底用于观察，故障时保留已有合法内容并退避重试。

默认资源上限会显示为未读取范围或明确失败，不据此报告“没有内容”或“完整导出”：

| 范围 | 默认上限 |
| --- | --- |
| 原始投影 | 每来源 64 MiB，单行 4 MiB；每逻辑任务 100,000 条记录、400,000 物理行、64 个继承段 |
| 来源目录扫描 | 20,000 项，目录深度 8 |
| 导出枚举 | 64 个任务（含根任务）、子任务深度 16、256 个可复制来源；每任务历史最多 256 页、关系条目最多 8,192 |
| 导出采集 | 总复制源字节 512 MiB、120 秒预算；该字节限制不是压缩后 ZIP 大小 |
| 临时导出 | 最多 8 份；已接受但结果未确认的保存仍占用保留资源 |

原始投影与导出走独立路径；投影上限不自动裁剪最终 ZIP。关联枚举不足会写入 manifest，容量或时间预算耗尽可能直接失败。实际可读取的字段受本机 Codex 版本影响。

## 本地设计与验收

以下需求、原型、截图和验证报告按现有 `.gitignore` 保留在本地，不随可分发源码或插件包发布：

- [需求（当前版本）](docs/requirements.md)
- [实现方案（当前版本）](docs/implementation-plan.md)
- [HTML 原型](docs/attachments/trajectory-ui.html)
- [本地验证材料](docs/validation/)

`backend/codex_display/server.py`、旧 M0 测试和 `scripts/check_sources.py` 保留为接入诊断材料；正式安装不使用 M0 入口或合成导出按钮。
