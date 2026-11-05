# Codex Trace

[English](README.md) | [简体中文](README.zh-CN.md)

在 Codex 中查看本地任务的执行轨迹，浏览模型与工具的输入输出、追踪子 Agent，并导出可校验的原始记录。

Codex Trace 通过本地 Python MCP 服务和随包提供的 React 界面运行。安装后，在聊天的 **New Tab → 执行轨迹** 打开面板；无法确认当前任务时，会显示任务选择器。全局入口也会先让你选择任务。

[社区安装](INSTALL.zh-CN.md#社区市场安装) · [可选发行附件](https://github.com/sirius0414/codex-trace/releases) · [安装指南](INSTALL.zh-CN.md) · [Windows 说明](WINDOWS.zh-CN.md) · [贡献指南](CONTRIBUTING.zh-CN.md)

## 功能

- **查看执行过程**：按轮次浏览消息、工具调用和输入输出，查看概述及原始记录。
- **定位感兴趣的内容**：搜索、筛选、展开详情，使用执行概览缩放、平移和定位条目。搜索与筛选作用于已加载记录，较早历史可继续加载。
- **跟踪 Agent**：主 Agent 和已确认的子 Agent 各有独立页面，可沿创建和通信记录跳转。
- **观察任务进展**：持续读取新记录，支持暂停跟踪和恢复阅读位置；暂停查看不会暂停 Agent 的执行。
- **查看用量与导出**：展示有明确来源的 token 用量，将当前任务及已确认子任务的原始记录导出为带来源清单和 SHA-256 的 ZIP。

界面只确认记录能够支持的时间、用量、关系和状态。缺失信息会明确标注，不推测未知阶段或任务完成状态。执行概览按轮次排列，图中距离不代表实际经过时间。

## 支持的平台

| 项目 | 要求与边界 |
| --- | --- |
| macOS | 本机 Codex 桌面应用及可用的配套 CLI |
| Windows | Windows 10/11、原生 Codex、本地 NTFS 卷，无需 WSL |
| Python | 社区市场由 uv 管理固定的 3.14.2；可选安装器／源码方式需本机 3.14+ |
| Codex CLI | 0.153.0 或更高兼容版本，支持 `plugin marketplace`、`plugin add/remove` 和所需的只读 app-server API |
| Git / uv | 社区市场注册需要 Git；插件启动需要可从 Codex 进程 PATH 找到 uv |
| 网络 | 社区市场首次准备、依赖变化或缓存缺失时由 uv 获取 Python 与锁定依赖；注册和更新市场需要访问 GitHub |

社区市场用户无需 Node.js、手工克隆源码或构建前端；Git 与 uv 为前置条件。可选安装器的前置条件另见安装指南。Linux、WSL、云端执行，以及 Windows 网络卷、UNC 和重解析点上的数据目录不在当前支持范围内。

Windows 已有原生实现和 CI 验证流程；是否通过特定版本的原生测试，应以该版本的 CI 报告和桌面验收结果为准，不能用 macOS 测试代替。详细边界见 [Windows 适配与验收](WINDOWS.zh-CN.md)。

## 安装、更新与卸载

社区分发通过本 GitHub 仓库的插件市场 `codex-trace-community` 安装 `codex-trace`。仓库目前为私有，安装者须具备 Git 读取权限并配置有效凭据。需要 **Git、uv 和支持插件市场的 Codex CLI 0.153.0 或更高兼容版本**；无需 Node.js、手工克隆源码或构建前端。安装步骤与网络、缓存说明见 [INSTALL.md](INSTALL.zh-CN.md)，维护发布流程见 [MARKETPLACE.md](MARKETPLACE.zh-CN.md)。

### 社区市场首次安装

```sh
codex plugin marketplace add https://github.com/sirius0414/codex-trace.git
codex plugin add codex-trace@codex-trace-community
```

插件目录已包含前端、后端、契约和授权文本。首次启动时，uv 准备固定的 Python 3.14.2 及 `requirements.lock` 依赖；首次准备、依赖变化或缓存缺失时需要网络。Python、环境及下载缓存由 uv 在插件目录外管理。

首次使用或依赖变化时，先按 [安装与依赖预热](INSTALL.zh-CN.md#首次使用与依赖预热) 准备运行环境；冷下载可能超过宿主默认的启动等待时间。预热成功后重启 Codex 或开启新会话，再通过 **New Tab → 执行轨迹** 打开。保留未确认导出的原请求，确认结果后再关闭旧面板。仓库市场由用户主动注册，与 OpenAI 公共目录审核和上架是不同渠道。

### 社区市场更新与卸载

```sh
codex plugin marketplace upgrade codex-trace-community
codex plugin add codex-trace@codex-trace-community
```

更新后完成 [依赖预热](INSTALL.zh-CN.md#首次使用与依赖预热)，再重启 Codex 或开启新会话。卸载插件并移除该市场注册：

```sh
codex plugin remove codex-trace@codex-trace-community
codex plugin marketplace remove codex-trace-community
```

移除插件不自动删除派生索引、阅读偏好、导出回执或最终 ZIP，也不负责清理 uv 的共享缓存。清理前先核对未决导出、停止插件服务并关闭相关页面，详见 [隐私与数据清理说明](PRIVACY.zh-CN.md)。

### 可选安装方式

需要独立安装器的用户可按 [发布 ZIP 安装器流程](INSTALL.zh-CN.md#可选发布-zip-安装器) 使用 `codex-trace-0.2.1.zip` 和校验和；该方式要求本机 Python，注册的是 `codex-trace` 市场。维护者也可构建按系统和架构分发的[内置运行时离线候选包](MARKETPLACE.zh-CN.md#可选原生离线插件包)。不要同时启用社区、安装器和开发市场中的同名插件。下载及实际发布状态以 [GitHub Releases](https://github.com/sirius0414/codex-trace/releases) 为准。

### 从 Codex Display 切换

Codex Trace 是新标识下的安装，不会自动替换旧的 Codex Display。新旧插件、状态目录和导出目录相互独立，索引、阅读状态和导出回执不会自动迁移。

请停用旧插件，安装并确认新版本可用后，再按旧版本的卸载流程处理旧安装；保留原有导出文件。自定义环境变量改用 `CODEX_TRACE_*`，Python 入口改为 `codex_trace.app`。具体步骤见[迁移说明](INSTALL.zh-CN.md#从-codex-display-切换)。

## 数据与导出

Codex Trace 只读观察 Codex 已保存的任务记录，不通过 app-server 创建、续写或中断任务。索引、阅读偏好和导出回执保存在插件自己的目录，最终导出保存在 Downloads 中。

| 数据 | macOS 默认位置 | Windows 默认位置 |
| --- | --- | --- |
| Codex 来源 | `~/.codex` | `%USERPROFILE%\.codex` |
| 可选安装器的记录与独立环境 | `~/.local/share/codex-trace/installations/<标识>/` | `%LOCALAPPDATA%\codex-trace\installations\<标识>\` |
| 索引、偏好与回执 | `~/.local/share/codex-trace/index/` | `%LOCALAPPDATA%\codex-trace\index\` |
| 最终导出 | `~/Downloads/Codex Trace/` | `%USERPROFILE%\Downloads\Codex Trace\` |

`CODEX_HOME` 可指定 Codex 来源；可选安装器的目录标识由其路径计算。社区插件程序由 Codex 缓存，Python 和依赖由 uv 在外部管理。不要移动正在使用的运行环境。

导出独立枚举当前任务的已知来源和已确认子任务，当前页面的搜索、筛选、折叠及分页不会缩小导出范围。ZIP 的 manifest 记录来源、采集范围、哈希及缺失部分；未知类型和不完整原始行按来源保留。运行中任务的多个文件可能在不同时间采集，因此成功保存不等于来源完整。

只有界面显示“本地导出已校验”和实际路径，才确认保存成功。等待超时或“结果未确认”时，应核对原请求，避免重复创建导出。导出可能含有任务中的私密内容，分享前请检查。

## 构建与验证

以下流程面向贡献者和维护者。准备 Python 3.14+、Node.js/npm 和 Codex CLI；CI 使用 Node.js 24 与 Python 3.14。开发环境与普通用户的安装环境分别管理。

### 准备开发环境

```sh
git clone https://github.com/sirius0414/codex-trace.git
cd codex-trace
```

**macOS：**

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci
npm run build
npm test
PYTHONPATH=backend:tests .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python contracts/generate_types.py --check
```

**Windows PowerShell：**

```powershell
py -3.14 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
npm ci
npm run build
npm test
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "backend;tests"
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe contracts/generate_types.py --check
```

Python 依赖由 `requirements.lock` 锁定，包含仅在 Windows 安装的 `pywin32`；前端依赖由 `package-lock.json` 锁定。npm 中的 Python 入口优先使用 `CODEX_TRACE_PYTHON` 指定的解释器或仓库 `.venv`。

### 浏览器调试

```sh
npm run dev:browser:start
npm run dev:browser:status
npm run dev:browser:stop
```

启动后打开命令输出的 `file://` 本地入口文件；`status` 可再次显示入口。该文件仅当前 OS 用户可读，通过不发送到 HTTP 的 URL fragment 将启动凭据交给页面。直接访问 `http://localhost:8765/` 不会取得凭据；重启服务后须重新打开入口文件。同一标签页刷新会保留凭据，请勿分享入口文件。该入口使用正式前后端，任务来源遵循 `CODEX_HOME`，默认读取本机 Codex 记录；仅监听 `127.0.0.1`。调试索引、日志及导出位于 `build/dev-browser/`。

修改前端后执行 `npm run build` 并刷新页面；修改后端后执行 `npm run dev:browser:restart`。也可用 `npm run dev:browser` 在前台运行。需要指定环境时：

```sh
npm run dev:browser:start -- --port 8766 --python /path/to/python --codex /path/to/codex
```

### 从源码更新开发插件

已有锁定依赖环境时，可以先准备并检查本机开发包：

```sh
npm run plugin:update -- --prepare-only
```

确认输出目录中的验证结果后，用实际目录替换下面的示例：

```sh
npm run plugin:update -- --install-prepared build/update-实际目录
```

此流程使用 `codex-trace@codex-trace-local`，生成的包绑定本机运行路径。面向其他用户的分发请使用下一节的发布构建。详细的原生 Windows 开发流程见 [WINDOWS.md](WINDOWS.zh-CN.md)。

## 发布流程

社区 GitHub 仓库市场的目录生成、验证与发布见 [MARKETPLACE.md](MARKETPLACE.zh-CN.md)。以下是仍保留的可选发布 ZIP 安装器流程，不表示相应版本已经公开发布。

### 生成安装包

完成开发环境准备后执行：

```sh
npm run release:build
```

也可以直接使用 Python 运行 `scripts/build_release.py`。默认产物为：

```text
build/releases/
├── codex-trace-0.2.1.zip
└── SHA256SUMS
```

构建会编译前端、校验版本和授权清单，按白名单收录安装器、已构建界面、后端、契约、依赖锁文件和许可证，并生成包内逐文件校验清单。开发环境、Codex 配置、凭据、会话、索引和导出文件不进入发布包。

版本必须在 `package.json`、`package-lock.json`、插件清单及后端 `__version__` 中保持一致。发布构建默认使用 `SOURCE_DATE_EPOCH=0` 和固定 ZIP 元数据；相同源码、依赖、工具链与时间值可生成相同字节的产物。同版本已有产物内容不同时会拒绝覆盖，正式发版应更新版本号。更新依赖后需刷新[第三方授权快照](third_party/README.zh-CN.md)。

### 验证安装包

先在另一目录重复构建，比较两份同版本 ZIP 的 SHA-256：

```sh
npm run release:build -- --output build/reproduce
```

然后执行安装验证。该脚本在临时用户目录中使用真实 Codex CLI、新建 Python 环境和锁定依赖，检查首次安装、重复安装、更新、失败恢复、MCP/导出及卸载。macOS 示例：

```sh
.venv/bin/python scripts/smoke_release.py \
  --archive build/releases/codex-trace-0.2.1.zip \
  --report build/release-smoke.json
```

Windows 使用 `& .\.venv\Scripts\python.exe scripts/smoke_release.py --archive build/releases/codex-trace-0.2.1.zip --report build/release-smoke.json`。需要指定 CLI 时加 `--codex`；离线测试可用 `--wheelhouse DIR` 提供预先下载的依赖。验证不会读取真实会话或修改真实插件配置，但 CLI/MCP 测试不能代替桌面入口的人工验收。

### GitHub Release

[Release 工作流](https://github.com/sirius0414/codex-trace/blob/main/.github/workflows/release.yml) 在 macOS 和 Windows 分别安装依赖、运行测试、检查契约、执行两次构建并比较产物，再验证解压包的安装生命周期。

| 触发方式 | 结果 |
| --- | --- |
| Pull Request 或推送到 `main` | 验证并保存构建产物与安装报告 |
| 在分支上手动运行 `workflow_dispatch` | 验证并保存产物，不创建 Release |
| 推送与清单版本一致的 `v*` 标签 | 验证通过后创建或更新草稿 Release，附安装 ZIP 和校验和 |

如果在 `v*` 标签上手动运行工作流，也会执行草稿 Release 步骤。维护者核对平台报告、桌面验收结果及已知限制后，再公开草稿。工作流不会覆盖已经公开的 Release，也不会上架 OpenAI 公共插件目录。仓库中提供了发布流程，并不表示某个版本已经公开发布。

## 文档与贡献

常规文件名提供英文文档，`*.zh-CN.md` 提供简体中文版本。修改时须同步两种语言的步骤、命令、支持边界与链接。

| 文档 | 内容 |
| --- | --- |
| [INSTALL.md](INSTALL.zh-CN.md) | 社区市场安装与更新、可选安装器、迁移及故障排查 |
| [MARKETPLACE.md](MARKETPLACE.zh-CN.md) | GitHub 社区市场发布、原生离线候选包与验证边界 |
| [PRIVACY.md](PRIVACY.zh-CN.md)、[SUPPORT.md](SUPPORT.zh-CN.md)、[TERMS.md](TERMS.zh-CN.md) | 数据保存与清理、支持渠道和许可边界 |
| [WINDOWS.md](WINDOWS.zh-CN.md) | 原生 Windows 实现、文件系统边界与验收方法 |
| [CONTRIBUTING.md](CONTRIBUTING.zh-CN.md) | 开发约束、代码规范和评审入口 |
| [SECURITY.md](SECURITY.zh-CN.md) | 安全支持范围与私密漏洞报告 |
| [contracts/README.md](contracts/README.zh-CN.md) | MCP 数据契约与导出语义 |
| [third_party/README.md](third_party/README.zh-CN.md) | 跨平台依赖授权快照维护 |

普通问题反馈与改进建议请提交到 [GitHub Issues](https://github.com/sirius0414/codex-trace/issues)，附系统版本、Python/Codex 版本、复现步骤和去除个人信息的错误摘要。安全或隐私漏洞请按 [安全策略](SECURITY.zh-CN.md) 私密报告。

## 来源与许可

本项目由作者独立维护。接入可行性研究参考了 [Codex Trajectory](https://github.com/icesixgod/codex-trajectory) 的本地轨迹读取及原生插件入口；早期界面设计参考了 [DeepSeek Harness 的 Trajectory 组件](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/client/ui-trajectory)。

Codex Trace 与上述项目、DeepSeek、OpenAI 无隶属或背书关系。项目自身代码采用 [MIT 许可证](LICENSE)，参考来源见 [NOTICE](NOTICE)。另有 [LICENSE 中文参考译文](LICENSE.zh-CN.md) 与 [NOTICE 中文参考译文](NOTICE.zh-CN.md)，许可证以英文原文为准。第三方组件保留各自的许可条款，完整文本随发布包提供，见[第三方授权清单](https://github.com/sirius0414/codex-trace/blob/main/third_party/THIRD_PARTY_NOTICES.txt)和 [LICENSES](https://github.com/sirius0414/codex-trace/tree/main/third_party/LICENSES/)。
