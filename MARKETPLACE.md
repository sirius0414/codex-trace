# Codex Trace GitHub 社区 Marketplace

本项目的主要分发渠道是 GitHub 仓库市场 `codex-trace-community`，插件为 `codex-trace`，当前版本标识为 0.2.0。仓库目前为私有，安装者须拥有 Git 读取权限并配置有效凭据。仓库安装由用户主动注册，不表示获得 OpenAI 公共目录认证。推送、Release 是否公开以及原生平台验证状态，以对应版本的实际记录为准。

## 社区安装与更新

前置条件为 Git、uv 和 Codex CLI 0.153.0 或更高兼容版本。无需 Node.js、手工克隆或前端构建：

```sh
codex plugin marketplace add https://github.com/sirius0414/codex-trace.git
codex plugin add codex-trace@codex-trace-community
```

更新：

```sh
codex plugin marketplace upgrade codex-trace-community
codex plugin add codex-trace@codex-trace-community
```

首次使用、依赖变化或缓存缺失时，先按 [安装与依赖预热](INSTALL.md#首次使用与依赖预热) 准备运行环境；冷下载可能超过宿主默认的启动等待时间。成功后重启 Codex 或开启新会话，再打开执行轨迹。uv 需要联网准备 Python 3.14.2 和锁定依赖；uv 管理的运行时、环境及下载缓存位于插件目录外。完整卸载和缓存说明见 [INSTALL.md](INSTALL.md#社区市场卸载与缓存)。

English quickstart: Install Git, uv, and a compatible Codex CLI (0.153.0 or later), then run the installation commands above. The repository is currently private; installers need Git read access and valid Git credentials. No Node.js or manual frontend build is required. uv prepares Python 3.14.2 and the locked dependencies on first launch; initial setup, dependency changes, or missing caches may require network access. Follow the [runtime preparation steps](INSTALL.md#首次使用与依赖预热) before opening the plugin, since cold downloads may exceed the host startup timeout. Restart Codex or start a new session after preparation succeeds. This is a user-added GitHub community marketplace, not an OpenAI public-directory listing. Native Windows validation has not been established by the current local checks.

## 社区仓库发布内容与验证

仓库根的 `.agents/plugins/marketplace.json` 发现清单指向 `plugins/codex-trace/`。该生成目录包含插件清单、uv 启动配置、后端、构建好的前端、契约、依赖锁文件和许可证；它不是未经构建的 `plugin/` 源码目录。发布者生成并核对这些文件后，将它们与对应源码一同提交，用户安装时无需构建。

维护者按 [贡献指南](CONTRIBUTING.md) 准备开发依赖后生成并核对社区目录：

```sh
.venv/bin/python scripts/build_community_marketplace.py
.venv/bin/python scripts/build_community_marketplace.py --check
```

Windows 使用 `.venv\Scripts\python.exe`。默认入口重新构建前端并固定构建时间，输出到 `plugins/codex-trace/`；`--check` 比较应生成内容与已提交目录，发现缺失、多余或变更文件即失败。`--assets dist/ui` 仅用于明确复用已验证的前端产物；可用 `--output DIR` 选择独立候选目录。生成目录与 `.agents/plugins/marketplace.json` 均须包含在发布提交中。

发布前应从准备提交的 Git 树验证目录内容与源码一致，在隔离用户目录中通过真实 Codex CLI 注册市场、安装插件、检查 uv 启动配置并运行合成任务的读取与导出检查。测试报告记录实际平台、版本和未执行项。macOS 成功不能代替原生 Windows 检查；本次尚不能声称 Windows 已通过实际验证。已公开的 Release、校验和与生成目录应对应同一提交和版本。

当前 smoke 证据覆盖冷依赖准备（cold dependency preparation）及预热后的 MCP/导出流程（warm MCP），不等于宿主空缓存冷启动通过。现有 modern MCP 清单不接受 `startup_timeout_sec`，不得通过添加该字段声称已延长启动时间；发布说明应继续提供终端预热步骤。

真实桌面入口仍需验收：重启或新会话后打开面板、选择任务、读取详情、查看确认的子 Agent、增量观察和导出校验。GitHub 发布身份、推送权限、远端 URL 可访问性与 Release 发布状态须实际验证，不从本地构建结果推断。

## 三种分发方式

| 项目 | GitHub 社区市场 | 可选 ZIP 安装器 | 可选原生离线包 |
| --- | --- | --- | --- |
| 入口 | 仓库根市场清单、`plugins/codex-trace/` | `scripts/build_release.py` | `scripts/build_marketplace.py` |
| Python 与依赖 | uv 管理固定 Python 3.14.2 和锁定依赖 | 本机 Python，安装器创建环境 | 随系统／架构包内置 |
| 网络 | 注册／更新，以及首次准备、依赖变化或缺失缓存时需要 | 安装依赖时需要 | 完整运行包启动不安装依赖 |
| Codex CLI | 本机提供 | 本机提供 | 本机提供 |
| 市场选择器 | `codex-trace@codex-trace-community` | `codex-trace@codex-trace` | 由实际分发和安装方式决定 |

## 可选：原生离线插件包

以下保留带内置 Python 的候选包构建方法，供维护者需要按平台提供离线运行包时使用。它不是社区 uv 安装的前置步骤。每个原生系统／架构分别准备、构建和验证；包名如 `codex-trace-0.2.0-darwin-arm64.zip`。

## 准备独立运行时

构建机需要本项目开发依赖、Node.js/npm、Python 3.14+ 和兼容 Codex CLI。使用以下入口获取当前原生平台的独立运行时，无需手工寻找归档或拼凑许可证：

```sh
.venv/bin/python scripts/fetch_marketplace_runtime.py \
  --output build/marketplace-runtime
```

Windows 使用 `& .\.venv\Scripts\python.exe scripts/fetch_marketplace_runtime.py --output build/marketplace-runtime`。工具只选择当前主机对应的 `darwin-arm64`、`darwin-x64` 或 `win32-x64`，不提供跨平台验证声明。`--output` 必须是尚不存在的新目录；已存在的运行时不会被覆盖。

[marketplace-runtime.lock.json](marketplace-runtime.lock.json) 固定 Python Standalone Builds 的 `20260114` 发行构建与 CPython `3.14.2`，记录三个原生目标的完整归档 URL、大小和 SHA-256。下载默认缓存在 `build/marketplace-runtime-cache/`，可通过 `--cache-dir DIR` 改变。缓存及新下载文件都先核对锁定哈希，再进行提取；下载或校验失败不会生成可用运行时。

归档仅提取 `python/install/`、`python/licenses/` 和 `python/PYTHON.json`，使用 `tarfile` 的 `data` 过滤器并额外拒绝目录逃逸、外部链接、重复成员及特殊文件，解压数据总量限制为 512 MiB。工具核对 `PYTHON.json` 中的 Python 版本、目标平台和所有递归的 `license_path` / `license_paths` 引用，再把全部发行许可证与元数据交给准备器。

该发行版的部分归档没有附上元数据引用的 zstd / zlib-ng 许可。锁文件因此还固定对应的 zstd `1.5.7` 和 zlib-ng `2.2.4` 官方来源 URL、大小及 SHA-256，工具仅在对应引用缺失时从校验缓存或锁定地址补齐。补充来源单独记录在最终 `runtime-provenance.json` 的 `source.supplementalLicenses`；其版本依据为该 PBS 发行标签的构建依赖声明。无法补齐的引用会使准备失败，不能用单个 CPython LICENSE 代替整套运行时的授权材料。

已有归档时可复用而不重新下载：

```sh
.venv/bin/python scripts/fetch_marketplace_runtime.py \
  --archive build/marketplace-python/cpython-full.tar.zst \
  --cache-dir build/marketplace-runtime-cache \
  --output build/marketplace-runtime
```

`--archive` 仍必须匹配当前目标的锁定大小和 SHA-256。要在无网络环境执行此命令，还需提前把锁文件对应的 `LICENSE.zstd.txt`、`LICENSE.zlib-ng.txt` 放入 `--cache-dir` 目录；工具会核对其哈希。缺少补充许可缓存时仍会尝试从锁定地址下载，不能把单独提供归档视为已经准备好全部离线输入。

下载助手不执行上游安装脚本。它调用 `prepare_marketplace_runtime.py` 展开包内相对链接、排除字节码，对可安全重定位的 sysconfig 前缀作规范化，并在改名后的目录实测解释器、原生模块和 `compression.zstd`。`runtime-provenance.json` 记录来源 URL、归档及输入树哈希、补充许可出处、规范化动作、许可证和文件哈希。哈希记录用于追溯输入，不替代平台批准或许可证范围的维护审查。

维护者处理新的运行时发行版时，可直接使用底层准备器的 `--source`、`--output`、`--source-url`、`--source-archive` 和可重复的 `--license-file` 参数；许可输入必须包含同发行版的 `PYTHON.json` 及其全部引用文本。随后应更新并评审锁文件和原生测试证据。不要用 `.venv` 代替独立 CPython，也不要混用其他版本的许可。

## 准备锁定依赖并构建

在对应原生系统上，用将要打包的 Python 下载该平台的 wheel。macOS 准备目录示例：

```sh
build/marketplace-runtime/bin/python3 -I -B -m pip download \
  --only-binary=:all: --no-deps \
  -r requirements.lock \
  --dest build/marketplace-wheelhouse
```

Windows 将解释器改为 `build\marketplace-runtime\python.exe`，并使用 PowerShell 的路径和换行语法。不要混用另一系统、架构或 Python ABI 的 wheelhouse。下载阶段需要网络，随后组包只从指定 wheelhouse 离线安装依赖；缺少 wheel 时应补齐对应文件，不让插件启动时下载。

```sh
.venv/bin/python scripts/build_marketplace.py \
  --runtime build/marketplace-runtime \
  --wheelhouse build/marketplace-wheelhouse \
  --output build/marketplace
```

默认构建前端；`--assets dist/ui` 可显式使用已有前端产物，使用者需确认产物对应当前源码。输出包含平台 ZIP、`SHA256SUMS` 与同名 `.verification.json`。插件包根使用 portable `plugin.json` 和 `mcp.json`，同时保留旧宿主所需的 `.codex-plugin/plugin.json` 与 `.mcp.json`。配置中的 `command` 为 `./runtime/bin/python3`（macOS）或 `./runtime/python.exe`（Windows），由宿主按插件根目录解析；`args` 中的 `launch.py` 路径及 `cwd` 使用 `${PLUGIN_ROOT}`。配置不包含构建者的 Python/Codex 绝对路径。

包内 `runtime/` 保存独立 Python，`runtime-deps/` 保存锁定依赖，`package-manifest.json` 记录文件哈希；运行时来源记录在 `runtime/runtime-provenance.json`。包同时收录已构建界面、后端、契约、图标、隐私/支持/条款文档和第三方授权文本。不会收录真实 Codex 配置、凭据、会话、派生数据库或用户导出。

组包仅移除 CPython 自测数据和静态链接归档，保留运行所需模块、动态库及完整许可集合。被移除文件的哈希和准备阶段 provenance 哈希写入运行时来源记录，裁剪后的实际包仍须完成依赖导入、迁移和 MCP 检查。构建器还检查 ZIP 大小、解压总量、成员数与路径限制，超限时拒绝输出候选包。

`launch.py` 以隔离模式进入原有 MCP 服务；启动器在本机发现 Codex CLI，可通过 `CODEX_TRACE_CODEX_EXECUTABLE` 显式指定原生可执行文件的绝对路径。Windows 的配置最终须解析到 `codex.exe`。启动器不执行安装器、不下载依赖、不注册市场，也不修改轨迹读取范围或导出语义。

## 验证候选包

先完成 [贡献指南](CONTRIBUTING.md#验证) 中的构建、前后端测试和契约检查，再使用同一输入在第二个输出目录重建，比较两份 ZIP 的 SHA-256。重复构建时使用相同前端构建时间设置；默认采用固定 `SOURCE_DATE_EPOCH=0`。保留源码提交、工具链、运行时来源、wheelhouse 和验证报告，以便追溯候选包。

macOS arm64 的命令示例：

```sh
.venv/bin/python scripts/smoke_marketplace.py \
  --archive build/marketplace/codex-trace-0.2.0-darwin-arm64.zip \
  --report build/marketplace-smoke.json
```

该检查使用临时用户目录、合成会话和隔离的 Codex 配置。默认通过真实 Codex CLI 验证；需要指定程序时加 `--codex /path/to/codex`。`--skip-cli` 仅用于受限环境下的包级诊断，其结果不能记为真实 CLI 安装验收。macOS 可加 `--deny-network` 验证受限网络环境下的启动与主流程；该选项不适用于 Windows，也不表示其他系统已完成同等网络验证。

CLI/包级验证仍不能代替桌面验收。每个要公开支持的平台和架构应在干净桌面环境中记录安装、重开、任务选择、详情阅读、已确认子 Agent 跳转、增量观察和 ZIP 导出校验的结果，并核对用户可见的平台限制。缺失平台证据时，应缩小候选包支持范围或补测，不将其他平台的结果记为通过。

## 未来可选：OpenAI 公共目录

以下仅适用于未来向 OpenAI 公共目录提交的渠道，不是 GitHub 社区市场的验收门槛。社区包继续保留本地聊天/执行历史读取；不能将技术验收通过等同于未来公共目录的政策许可。

1. 按 [OpenAI 桌面插件提交要求](https://developers.openai.com/plugins/deploy/desktop-only) 确认提交组织已获准提交本地 MCP 插件，并完成实际要求的开发者或企业身份验证。清单中的名称及 `requires_local_executor` 不证明已获资格。
2. **本地聊天历史读取与公共目录准则的兼容性仍是未来公共目录提交的未解除阻塞。** 当前实现继续读取本地 Codex 的既有聊天/执行历史；需根据 [插件准则](https://developers.openai.com/plugins/plugin-guidelines) 获得平台对这一用途、范围和呈现方式的明确确认。新增内置运行时、隔离安装或通过自动测试均未改变这一产品行为，不能消除此政策阻塞，也不得据此宣称已经符合公共目录要求。
3. 在提交门户验证目标平台包的接受情况、内置运行时、MCP Apps 入口与宿主兼容性，并提交与候选包一致的测试证据。仓库自动检查无法证明门户已接受该包。
4. 将 [PRIVACY.md](PRIVACY.md)、[TERMS.md](TERMS.md)、[SUPPORT.md](SUPPORT.md) 及对应代码推送到公开声明的分支，逐个检查清单中的主页、支持、隐私和条款 URL 可公开访问。本地文件和预设 URL 不表示公开页面已经生效。
5. 核对版本、许可证集合、原生平台报告与桌面验收结果后，再按 [官方提交流程](https://developers.openai.com/plugins/deploy/submission) 完成审核与发布。不要把 GitHub 草稿 Release 或本地市场注册写成已在 OpenAI 公共目录上架。

已有 GitHub Release 工作流与本地检查只证明各自实际执行过的步骤。社区发布以对应 Git 提交、安装验证和 GitHub Release 状态为准；公共目录另需门户审核及其发布状态。
