# Preview 阅读器验收

[English](README.md) | [简体中文](README.zh-CN.md)

此目录用合成数据挂载正式 `TraceUI` / `Inspector`，不连接真实历史、执行工具或访问工作文件。

## 阅读行为

- Summary 保留事件概述；Preview 阅读已加载输入、输出和错误；“原文”保留当前块原始文本；Raw content 展示关联原始记录；Source 展示来源证据。
- 正文按内容和来源格式自动识别。阅读 / 原文及一个复制入口与分区标题共用一行；格式和代码语言修正收进“更多”。字号、换行、行号、宽度和展开阅读继续使用原有设置。
- JSON 使用保留源码区间的索引，只插入缩进，不经过对象解析再序列化。数字字面量、重复字段、键的转义和字符串转义均保留；普通对象和数组直接展开。已删除字段树、深度控制、字段目录、字段搜索和节点复制。
- `json-layout.ts` 集中定义长值阈值：值的原始 JSON 超过 **600 个 UTF-16 单元或 12 行**时自动折叠。对象显示 `{…}`，数组显示 `[…]`，字符串保留前 70 单元。根值从不自动折叠。没有新增用户设置项。
- 已确认来源为 `mcpToolCall.result`，且字段定义和实际结构均支持的纯文本结果可以直接阅读。未知字段、重复键、非文本块、错误结果和 `structuredContent` 仍完整显示为 JSON，不按字段名猜测包装。
- 条目级搜索覆盖全部已加载输入/输出/错误原文，包括视觉折叠和显示窗口外文本；最多列出 100 个结果，同时显示真实命中总数。JSON 命中展开对应长值并移动显示窗口，Markdown 等格式或跨 JSON 结构空白的命中使用原文精确定位。
- 每块仅一个复制入口，复制完整已加载原始正文，与折叠、格式及显示窗口无关。合成测试拦截 Clipboard API 的参数核对保真，不修改用户剪贴板。
- 普通、完整和未知来源状态不占提示行。可靠的来源截断、仅保存片段、仍未加载内容、未保存输出仍显示。只移除已知固定 explanation 文案，保留实际错误和限制。

## 状态与资源边界

- 后端每次读取至多 65,536 个 Unicode 字符（原为 16,384）；UTF-8 字节偏移与续读游标保持连续。

- 阅读状态只在页面会话中保留，最多 150 条目、600 个标签位置；不持久化正文和查询。JSON 对象以字段名及同名出现次序保持状态，数组以索引保持状态；数组插入或重排没有可靠身份映射。
- 单个有效正文默认展开；Input/Output 同时存在时 Call 展开 Input、Result 展开 Output；失败条目首次全部展开且 Error 最先。异步正文到达、分页、条目切换和刷新保留手动选择。
- JSON 解析限制 8,000,000 UTF-16 单元、200,000 节点、128 层；不完整、无效或超限时回退原文，读取入口由外层保留。
- JSON 每次至多 400 排版行，以约 200,000 字符预算在行边界停止；一个长值窗口至多 100,000 字符。前后窗口有显式导航。搜索直接定位到目标窗口，不挂载之前全部数据。
- 普通原文以 100,000 字符为窗口；搜索可直接跳到末尾。复制包含全部已加载正文。
- Markdown、代码、表格、列表继续使用各自的有界显示与阅读状态。

## 验证

```sh
npm run build
npm test
git diff --check
node tests/fixtures/reader-preview/build.mjs
python3 -m http.server 4175 --bind 127.0.0.1 --directory build/reader-preview
```

在 ego-browser 同一个任务空间打开 `http://127.0.0.1:4175` 后运行：

```js
const { runAcceptance, runNarrowAcceptance } = await import('file:///ABSOLUTE_REPO/tests/fixtures/reader-preview/acceptance.mjs');
console.log(await runAcceptance(page, '/ABSOLUTE_REPO/build/preview-unified-acceptance'));
console.log(await runNarrowAcceptance(page, '/ABSOLUTE_REPO/build/preview-unified-acceptance'));
```

默认折叠、分页和实时刷新回归：

```js
const { runCollapseAcceptance } = await import('file:///ABSOLUTE_REPO/tests/fixtures/reader-preview/collapse.mjs');
console.log(await runCollapseAcceptance(page, '/ABSOLUTE_REPO/build/preview-collapse-acceptance'));
```

窄屏流程会校准浏览器现有缩放，按 390 个 CSS 像素检查中英文、classic / soft / terminal 三种风格以及明暗主题。截图和检查结果保存到指定 build 目录。
