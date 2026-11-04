# Preview 阅读器验收

此目录使用合成数据挂载正式 `TraceUI` / `Inspector`，不连接用户历史、执行工具或访问工作文件。`main.tsx` 包含长 Markdown、精确 JSON 数字、长字符串、命令、日志、部分 JSON、来源截断、未保存输出、失败、未知工具及 20,000 项数组。

## 设计与实现

- Summary 保留事件与用量概述；Preview 按输入/输出/错误块独立阅读；正文原文是该块的原始文本；Raw 是确认关联的原始记录；Source 保留来源证据。
- 可选 `reader` 契约传递来源格式、工具适配和字段解释。可靠来源优先，无来源时保守按可见语法识别；可手动选择阅读格式和代码语言。
- JSON 仅在可读取正文收齐后建树。AST 保存数值和字段源码区间，不用浮点数重序列化。重复字段、空值、JSON 转义和源码复制均保留。JSON 字符串的“复制字段值”保留 JSON 引号/转义，“复制字符串正文”明确表示已解码。
- Markdown 通过 marked 词法节点转 React；HTML 和上下文包装保持可见，不注入 HTML，不自动请求图片。代码高亮只解析已知高亮器的文本/样式，并校验高亮前后文本一致。
- 命令正文、文件 diff 使用可靠工具定义；命令数组保持 JSON。完整参数始终在下方 JSON 或正文原文中。
- 阅读状态仅在当前页面会话内记忆，最多 150 条目、600 个标签滚动位置。不会写入宿主 ViewState 或 localStorage。重新选择条目时后端重读之前的 UTF-8 字节窗口，重新校验版本，不缓存旧游标。
- 展开阅读覆盖面板，保留拖动/方向键调整宽度；展开时背景 inert，Tab 限于阅读面板，Escape 先退出展开。

## 字段解释覆盖

生产数据实际接入 exec_command 输入 10 项、结果 6 项，write_stdin 输入 4 项及相同结果定义；MCP 外层 content/isError/structuredContent 与已确认 text 内容块；fileChange 已记录 diff。说明包含可获得的类型、用途、单位、必填性、枚举、链接和版本。参见 `contracts/README.md` 的具体来源。

当前历史适配器没有保存每次调用的工具 schema 或协商协议版本，所有随包官方定义都标为 **reference / 历史版本无法匹配，仅供参考**。未定义字段没有说明按钮。浮层无需网络，支持悬浮、键盘聚焦、点击固定和来源链接。

## 数据与资源边界

- `nextCursor/readableBytes`、`sourceTruncated`、来源完整性、未保存输出分别展示。读齐可读取正文不等于来源完整。生产来源没有可验证截断证据时保持 unknown，不从日志里的 “truncated” 字样推测。
- 原始工具字段优先从同一物理记录取精确 JSON 子串；API 已解码值不能恢复原传输字节、历史缺失内容或已丢失的精度。`origin` 明确标记 API decoded projection。
- JSON 结构索引限制 8,000,000 UTF-16 单元、200,000 节点、128 层；超限回退原文。每层值按 100 项显示，大对象另有全部 key 的目录（超过 1,000 个 key 时窗口呈现）。树一次至多 2,000 可见节点。搜索覆盖完整已解析正文，结果列表最多 200 项。
- 全条目搜索仅覆盖已加载输入/输出/错误，最多列出 100 个结果并显示真实总数；不会搜索尚未加载内容或 Raw。
- 大 Markdown、代码、表格、列表有明确的延后呈现提示。视觉折叠/延后呈现不裁剪复制数据。普通原文每次显示 100,000 字符，可继续显示；复制包含全部已加载正文。
- 高亮支持 Bash、JavaScript、TypeScript、Python、JSON、diff、CSS、XML、SQL、YAML、Rust、Go、Swift；未知语言仍可原样阅读。未记录 shell 类型的命令不默认解释成 Bash，可手动选择。
- 宿主剪贴板拒绝访问时可使用浏览器回退；含 CR 的文本不经过会改变换行的 textarea 回退，改为明确提示使用原始记录导出。

## 自动化和浏览器流程

```sh
npm run build
npm test
PYTHONPATH=backend:tests .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python contracts/generate_types.py --check
git diff --check
node tests/fixtures/reader-preview/build.mjs
python3 -m http.server 4175 --bind 127.0.0.1 --directory build/reader-preview
```

按 ego-browser 技能创建一个任务空间并打开 `http://127.0.0.1:4175`，再在同一空间的页面运行：

```js
const { runAcceptance } = await import('file:///ABSOLUTE_REPO/tests/fixtures/reader-preview/acceptance.mjs');
console.log(await runAcceptance(page, '/ABSOLUTE_REPO/build/reader-acceptance'));
```

浏览器测试捕获传给 Clipboard API 的文本以核对保真，不修改用户剪贴板。截图和 `checks.json` 写入指定 build 目录。另需在 390px 视口检查中英文、三种风格、暗色、Tab 导航和说明卡；这些是实际浏览器交互验收，不以单元测试代替。

## 本次验收结果（2026-11-03）

- `npm run build` 成功，`npm test` **331/331** 通过。
- Python 完整检查运行 **580** 项，**17** 项按平台/环境条件跳过，其余通过。
- 契约生成一致性、第三方许可证检查及 `git diff --check` 通过。
- ego-browser / Chromium 主流程和补充流程共 **49** 个真实浏览器断言通过。补充流程由 `supplementary.mjs` 提供，覆盖三种主题的 390px 窄屏、Tab 焦点范围、参数数组、文件 diff、8M 以上正文回退及完整已加载内容复制。
- 截图和浏览器记录：`build/reader-acceptance/01-markdown.png`、`02-json-search.png`、`03-tool-definition.png`、`04-narrow-dark-zh.png`、`05-narrow-tool.png`、`checks.json`。
- 本次验证使用合成记录与隔离服务夹具；未修改真实任务、执行预览命令或更新已安装插件。
