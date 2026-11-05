# Codex Trace 安装指南

[English](INSTALL.md) | [简体中文](INSTALL.zh-CN.md)

Codex Trace 0.2.1 的主要分发渠道是本 GitHub 仓库的社区插件市场。它提供已构建的前端、后端、契约和许可证；用户无需 Node.js 或手工构建。插件读取和保存的数据范围见 [PRIVACY.md](PRIVACY.zh-CN.md)，维护者发布说明见 [MARKETPLACE.md](MARKETPLACE.zh-CN.md)。

## 社区市场前置条件

- 本机 Codex 桌面应用与原生 CLI；CLI 需为 0.153.0 或更高兼容版本，支持插件市场和本插件使用的只读 history API。
- Git 与 uv 已安装，并能从启动 Codex 的进程环境中找到。终端中的 `git --version`、`uv --version`、`codex --version` 应正常返回；GUI 启动的 Codex 也须能找到 uv。
- 注册和更新仓库时可访问 GitHub。仓库目前为私有，安装者必须拥有 Git 读取权限，并为本机 Git 配置有效凭据。首次启动、依赖变化或缓存缺失时，uv 需要联网获取固定 Python 3.14.2 与 `requirements.lock` 中的依赖。
- Windows 需要原生 Codex 和本地 NTFS，无需 WSL；以普通用户启动 Codex 和终端，不支持“以管理员身份运行”。实际平台结果以对应版本的 [Release 验证记录](https://github.com/sirius0414/codex-trace/releases/tag/v0.2.1) 为准，不能把 macOS 或模拟测试记为 Windows 通过。其他限制见 [WINDOWS.md](WINDOWS.zh-CN.md)。

社区方式无需预先手工安装 Python；uv 管理运行时和依赖。不要用 `sudo` 启动插件，也不要手工修改其缓存环境中的依赖。

## 社区市场安装

macOS 终端或 Windows PowerShell：

```sh
codex plugin marketplace add https://github.com/sirius0414/codex-trace.git
codex plugin add codex-trace@codex-trace-community
```

市场名为 `codex-trace-community`，插件名为 `codex-trace`。注册后从仓库清单定位 `plugins/codex-trace/`，Codex 管理其安装缓存。首次打开时 uv 可能需要较长时间准备运行环境；缓存由 uv 在插件目录外管理，不承诺重启后必定无需网络。

安装后先按下节完成依赖预热，成功后重启 Codex 或开启新会话，再在聊天的 **New Tab → 执行轨迹** 打开；全局入口会先显示任务选择器。旧面板可能仍使用旧资源，关闭前先核对未确认导出。安装命令和仓库内的清单不代表已经通过 OpenAI 公共目录审核；该社区渠道由用户主动注册。

若已有 `codex-trace@codex-trace`（ZIP 安装器）或 `codex-trace@codex-trace-local`（开发安装），先停用旧插件，避免多个同名入口同时启用。需要保留的数据与未决导出先按原渠道核对。

## 首次使用与依赖预热

首次使用、依赖变化或 uv 缓存被清理后，建议在打开插件前运行以下对应平台的安装与预热块。冷下载可能超过宿主默认约 30 秒的 MCP 启动等待时间；预热在终端中独立完成 Python 和依赖准备。它可替代上方裸 `plugin add` 命令；已安装时也可重复执行，从 CLI 的 `installedPath` 自动定位本次安装，无需手工寻找缓存目录。

macOS（zsh 或 bash，无需预先安装 Python 或 jq）：

```sh
(
  set -o pipefail
  trace_root="$(codex plugin add codex-trace@codex-trace-community --json | uv run --no-project --no-config --python 3.14.2 -- python -I -B -c 'import json,sys; print(json.load(sys.stdin)["installedPath"])')" &&
  cd "$trace_root" &&
  uv run --no-project --no-config --python 3.14.2 --with-requirements ./requirements.lock -- python -I -B -c 'import compression.zstd, mcp; print("Codex Trace runtime ready")'
)
```

Windows PowerShell：

```powershell
$traceJson = codex plugin add codex-trace@codex-trace-community --json
if ($LASTEXITCODE -ne 0) { throw "Plugin installation failed" }
$traceInstall = $traceJson | ConvertFrom-Json
Push-Location -LiteralPath $traceInstall.installedPath -ErrorAction Stop
try {
  uv run --no-project --no-config --python 3.14.2 --with-requirements ./requirements.lock -- python -I -B -c "import compression.zstd, mcp; print('Codex Trace runtime ready')"
  if ($LASTEXITCODE -ne 0) { throw "Runtime preparation failed" }
} finally { Pop-Location }
```

显示 `Codex Trace runtime ready` 后，重启 Codex 或开启新会话，再打开执行轨迹。预热应使用启动 Codex 的同一用户及 uv 默认缓存／运行时目录。宿主不会默认继承终端中的全部 `UV_*` 设置；使用自定义缓存前须核对宿主实际环境，不能假定终端预热已经覆盖宿主。更换用户、缓存位置或依赖后需要重新准备。命令失败时先处理 Git 访问权限、uv 网络或缓存问题，不能把失败的准备步骤当成插件已就绪。各版本的依赖准备、MCP／导出及真实宿主启动结果见 Release 验证记录；预热后的成功不证明宿主空缓存冷启动通过，Windows 命令本身也不代表原生平台或普通用户桌面验收已通过。

## 社区市场更新

```sh
codex plugin marketplace upgrade codex-trace-community
codex plugin add codex-trace@codex-trace-community
```

更新后按 [首次使用与依赖预热](#首次使用与依赖预热) 执行同一安装与预热块，成功后重启 Codex 或开启新会话。仓库更新与依赖变化可能需要网络；失败时先修复 Git、uv、代理或证书问题，再重复同一流程。`install.py recover` 仅适用于下方的 ZIP 安装器，不用于社区插件缓存恢复。

## 社区市场卸载与缓存

```sh
codex plugin remove codex-trace@codex-trace-community
codex plugin marketplace remove codex-trace-community
```

插件删除与市场移除分别处理程序安装和市场注册。派生索引、偏好、导出回执与最终 ZIP 保留；uv 下载及运行环境缓存由 uv 管理，插件卸载不会主动清理它们。共享缓存可能被其他程序使用。先完成未决导出核对，停止插件服务并关闭页面，再按 [数据清理说明](PRIVACY.zh-CN.md#卸载与删除数据) 处理本插件数据，勿删除 Codex 的原始会话目录。

自定义运行配置使用 `CODEX_HOME`、`CODEX_TRACE_CODEX_EXECUTABLE`、`CODEX_TRACE_STATE_ROOT` 和 `CODEX_TRACE_EXPORT_ROOT`。须在启动 Codex 应用的进程环境中提前设置这些变量，并在安装或更改配置后重启 Codex。随包的 legacy overlay 会显式向 MCP 进程透传这四项，不透传整个环境；上文的 uv 默认缓存要求与 `UV_*` 继承限制仍适用。

常见启动问题：找不到 uv 时检查 Codex 的 PATH 并重启应用；首次准备失败时检查 uv 的网络、代理、证书和磁盘空间；无法发现 Codex 时可设置 `CODEX_TRACE_CODEX_EXECUTABLE` 为实际原生程序的绝对路径。不要把 CLI 包装器目录或应用目录当作可执行文件，也不要通过忽略 TLS 校验解决下载失败。

## 可选：发布 ZIP 安装器

以下原有流程用于下载并执行 `codex-trace-0.2.1.zip` 安装器，与上方社区市场分开管理。该方式需要本机 Python 3.14+，注册 `codex-trace@codex-trace`；Python 依赖在安装阶段准备。原生离线包的构建与验证另见 [MARKETPLACE.md](MARKETPLACE.zh-CN.md#可选原生离线插件包)。

## 支持的平台和前置条件

| 项目 | 要求 |
| --- | --- |
| macOS | 本机可运行 Codex 桌面应用及其配套原生 CLI；Python 3.14+ |
| Windows | Windows 10/11，原生 Codex 与 Python 3.14+，本地 NTFS 卷；不需要 WSL |
| Python | `venv`、`pip` 和 `compression.zstd` 可用；安装器会实际检查 |
| Codex | 支持 `plugin marketplace`、`plugin add/remove` 和本插件使用的只读 app-server API |
| 网络 | 首次创建环境需要下载锁定的 Python 依赖；插件日常启动不会安装依赖 |

不支持 Linux、浏览器端／云端执行、网络卷或 Windows UNC、junction、重解析点上的状态和导出目录。Windows 后端已有原生适配；CI 的安装测试不能替代真实 Windows Codex 桌面入口及导出验收。已完成与待完成的系统验证见仓库中的 [WINDOWS.md](WINDOWS.zh-CN.md)。

先安装并正常启动 Codex；若终端找不到其 CLI，macOS 安装器会尝试常见桌面安装位置；Windows 请把原生 CLI 加入 PATH，或指定原生可执行文件。Python 可从 [Python 官方下载页](https://www.python.org/downloads/) 安装 3.14 或更高版本。Windows 安装时启用 Python Launcher 或加入 PATH，完成后重新打开终端。无需管理员权限，也不要使用 `sudo` 运行本安装器。

## 下载和校验

从本项目的 [GitHub Releases](https://github.com/sirius0414/codex-trace/releases) 下载同一版本的 `codex-trace-版本.zip` 与 `SHA256SUMS`。请使用 Release 附件中的 ZIP。GitHub 自动生成的 Source code ZIP 虽可包含 `plugins/` 中已编译的界面，但不提供独立安装器所需的目录布局。尚无已发布 Release 时，普通用户的下载入口尚未开放。

以 `0.2.1` 为例，先在下载目录校验，再解压：

macOS 终端：

```sh
shasum -a 256 -c SHA256SUMS
unzip codex-trace-0.2.1.zip
cd codex-trace-0.2.1
```

Windows PowerShell：

```powershell
(Get-FileHash .\codex-trace-0.2.1.zip -Algorithm SHA256).Hash
Get-Content .\SHA256SUMS
# 确认输出的 SHA-256 与文件中该 ZIP 对应的值一致后再解压。
Expand-Archive .\codex-trace-0.2.1.zip -DestinationPath .
Set-Location .\codex-trace-0.2.1
```

校验不一致时重新下载，不要继续安装。校验和用于发现内容变化，不能代替对下载来源的信任。

## 首次安装

macOS：

```sh
sh ./install.sh
```

Windows PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

PowerShell 命令只为本次进程设置脚本执行策略，不修改系统策略。如果组织策略禁止运行脚本，可直接使用 Python：

```powershell
py -3.14 .\install.py install
```

安装器检查前置条件，创建独立虚拟环境，按 `requirements.lock` 安装并验证依赖，然后生成本机 MCP 配置、注册 Codex Trace 的本地插件市场并安装插件。本机解释器与 Codex 的绝对路径只写入本机安装目录，不存在于下载包中。源码目录、下载包目录都不是运行时依赖。

安装完成后重新加载插件资源；如果菜单仍显示旧内容，在方便时重启 Codex。安装器不会重启桌面应用或结束会话。

自动发现失败时显式指定路径（替换示例路径）：

```sh
python3.14 install.py install --python "/path/to/python3.14" --codex "/path/to/codex"
```

```powershell
py -3.14 .\install.py install --python "C:\Python314\python.exe" --codex "C:\Program Files\Codex\codex.exe"
```

`--codex` 要指向原生程序，标准 npm `codex.cmd` 安装可以自动识别；自定义批处理包装器需要改用实际 `codex.exe`。也可用 `CODEX_TRACE_PYTHON` 为启动脚本指定 Python。

安装器默认操作 `CODEX_HOME` 指向的 Codex 配置，未设置时使用用户主目录下的 `.codex`。它只管理 `codex-trace@codex-trace` 及同名独立市场，不改写其他市场、插件或会话。原开发流程的 `codex-trace-local` 是另一个市场；若曾安装开发版本，请通过 Codex 插件管理停用旧版本，避免两个同名工具同时启用。

| 数据 | macOS | Windows |
| --- | --- | --- |
| 安装回执、版本及独立环境 | `~/.local/share/codex-trace/installations/<标识>/` | `%LOCALAPPDATA%\codex-trace\installations\<标识>\` |
| 索引、偏好及导出回执 | `~/.local/share/codex-trace/index/` | `%LOCALAPPDATA%\codex-trace\index\` |
| 用户导出的 ZIP | `~/Downloads/Codex Trace/` | `%USERPROFILE%\Downloads\Codex Trace\` |

安装标识根据目标 `CODEX_HOME` 计算，让不同 Codex 配置使用独立的安装记录。高级用户可通过 `--root` 指定安装管理目录，后续更新、恢复、状态和卸载须继续使用同一参数。不要移动正在使用的安装目录或其中的 Python 环境。

## 从 Codex Display 切换

Codex Trace 使用插件标识 `codex-trace`，社区市场为 `codex-trace-community`，可选 ZIP 安装市场为 `codex-trace`，源码开发市场为 `codex-trace-local`。这是新标识下的首次安装，不会自动替换原来的 `codex-display@codex-display` 或 `codex-display@codex-display-local`。

1. 在旧版本中先核对尚未确认的导出结果，再关闭旧面板，通过 Codex 插件管理停用旧插件。
2. 按上文首次安装流程安装 Codex Trace；源码开发安装使用 README 中的构建、组包和安装流程。为新版本使用独立的安装管理目录，不向旧版本的安装目录传入 `--root`。
3. 重新加载插件资源，打开“执行轨迹”。确认新版本可用后，可按旧版本的卸载说明移除旧插件及市场注册。

新版本使用 `codex-trace` 状态目录和 `Downloads/Codex Trace/` 导出目录。旧的 `codex-display` 状态目录、运行环境和 `Downloads/Codex Display/` 导出文件保留原位；索引、阅读状态、界面偏好及导出回执不自动迁移。不要移动仍被旧面板使用的 Python 环境，或将旧版本未完成的导出回执复制到新目录。

自定义运行配置同步改用 `CODEX_TRACE_*` 环境变量，Python 入口为 `codex_trace.app`；旧的 `CODEX_DISPLAY_*` 配置和 `codex_display.app` 入口仅适用于旧版本。

## 更新与重复安装

下载并校验新版本，解压后运行同样的安装命令即可更新，也可以明确使用：

```sh
python3.14 install.py update
```

Windows 使用 `py -3.14 .\install.py update`。无需先卸载；重复安装也是恢复缺失或损坏安装的入口。不要在已安装的虚拟环境中自行执行 `pip install --upgrade`，这会使依赖与锁文件不一致。

若 Codex 或 Python 安装位置改变，重新运行安装器生成本机配置。更新期间保留旧版本运行环境，避免破坏尚未重载资源的面板。用户任务记录和最终导出的 ZIP 不属于更新清理范围。

## 安装失败和恢复

依赖安装、验证和市场切换分阶段执行。安装目录中的 `journal.json` 记录最近一次操作，`current.json` 记录最近一次确认成功的安装，`transactions/` 保存每次尝试及 `pip.log`、`verification.log`。安装失败不等于旧版本已损坏；切换失败时安装器尝试重新安装之前确认的版本，并报告恢复是否成功。

先查看状态：

```sh
python3.14 install.py status
```

修复报错指出的问题后恢复：

```sh
python3.14 install.py recover
```

Windows 把命令前缀改成 `py -3.14 .\install.py`。已有确认版本时，恢复会尝试重新安装该版本；首次安装若已完成准备，则重试安装已验证的产物。若准备阶段未完成，恢复只结束未完成的事务，并提示重新运行 `install`。之后从已校验的完整发布目录再次运行 `install`。保留安装记录和旧环境直到恢复完成，不要删除整个 Codex 目录、重建其他插件配置或终止运行中的任务。

## 卸载

在完整解压的发布目录执行：

```sh
python3.14 install.py uninstall
```

Windows 使用 `py -3.14 .\install.py uninstall`。卸载移除本插件的安装和市场注册；重复执行可重试未完成的卸载。也可在 Codex 插件管理中卸载，然后运行上述命令清理本插件的市场注册。

卸载保留独立运行环境、安装记录、索引和导出文件，以免破坏仍在使用旧环境的面板或尚待核对的导出。关闭相关面板并确认不再需要恢复后，可以自行删除本插件安装目录；索引与导出目录分别管理。**用户导出的 `Downloads/Codex Trace/` 永远不由安装器删除。** 不要删除 Codex 会话目录或其他插件缓存。

## 故障排查

| 现象 | 处理方式 |
| --- | --- |
| 找不到 Python，版本过低，或缺少 `compression.zstd` | 安装完整的 Python 3.14+，重新打开终端；用 `--python` 指定实际解释器。极简发行版可能没有 zstd，需要更换完整发行版。 |
| 找不到 Codex／`plugin` 子命令不可用 | 安装或更新 Codex 桌面配套 CLI；用 `--codex` 指定原生程序，不要把桌面应用目录当作可执行文件。 |
| `pip` 下载、代理或证书失败 | 根据事务目录 `pip.log` 检查网络和本机 Python 的证书／代理配置；修复后重试。不要取消依赖锁定或忽略 TLS 校验。 |
| 缺少当前平台依赖包 | 锁文件可能尚无该 Python／架构的可安装构建；优先使用 Python 3.14，保留日志并向项目反馈。不能把其他电脑的虚拟环境直接复制过来。 |
| 市场同名但来源不属于本安装器 | 安装器会停止，避免接管另一来源。检查 Codex 插件管理中的 `codex-trace` 市场，确认归属后由用户处理冲突。 |
| 提示另一个安装正在运行 | 等待该操作结束。不要并发安装、更新或卸载同一目标；如果安装进程已异常退出，按错误中的恢复指引处理。 |
| 新版本菜单或界面未刷新 | 先重载插件资源；在方便时重启 Codex。已打开的面板可能仍使用旧资源。 |
| 无法生成导出 | 确认用户的 `Downloads` 父目录存在且可写；Windows 使用本地 NTFS，避免目录重定向和占用。未确认的保存用原请求核对，不要连续创建新请求。 |
| 使用自定义安装目录后状态为空 | 为所有命令传入同样的 `--root`，并确认 `CODEX_HOME` 与安装时一致。 |

反馈问题时提供系统、Python／Codex 版本、失败阶段和去除个人信息的错误摘要。日志和本机回执可能包含本机路径，不要公开上传 Codex 凭据、会话或完整用户配置。

## 插件市场与公开上架

本 GitHub 社区市场通过用户主动注册仓库安装，不等于 OpenAI 公共目录上架。可选 ZIP 安装器则准备本机运行环境并注册另一独立的本地市场。两种方式均保留现有轨迹读取、缓存与导出行为。

未来如果提交 OpenAI 公共目录，需要另行确认本地 MCP 提交资格、开发者身份、门户兼容性，以及读取本地 Codex 聊天/执行历史与平台准则的兼容性。那些条件只用于公共目录渠道，不能代替或混入社区仓库市场的技术验收。详见 [MARKETPLACE.md](MARKETPLACE.zh-CN.md#未来可选openai-公共目录)。

## 维护者：生成发布包

在源码仓库安装 Node.js/npm、Python 3.14+ 后运行：

```sh
npm ci
npm run release:build
```

也可以直接运行 `python3.14 scripts/build_release.py`。默认生成 `build/releases/codex-trace-版本.zip` 和 `build/releases/SHA256SUMS`；包版本取自项目清单。构建使用文件白名单，不打包 `.venv`、`node_modules`、Codex 配置、凭据、会话、索引或导出文件。构建默认使用固定的 `SOURCE_DATE_EPOCH=0`；同一工具链、源码、依赖及该时间值产生相同字节的发布 ZIP。可以显式设置 `SOURCE_DATE_EPOCH`，但两次比较时必须相同。发布包不包含构建机器的运行路径。

GitHub Release 工作流在版本标签触发时先运行 macOS/Windows 验证，再创建带 ZIP 和校验和的草稿 Release；在分支上手动触发仅验证和保存构建产物；在 `v*` 标签上手动运行也会执行草稿 Release 步骤。检查平台测试结果与限制后，由维护者决定是否公开草稿。配置工作流、在本机构建都不会自动向 GitHub 推送或上架公共插件目录。

验证发布包时可运行 `python3.14 scripts/smoke_release.py --archive build/releases/codex-trace-0.2.1.zip --report build/release-smoke.json`。该命令只在临时用户目录执行真实 Codex CLI 与新建虚拟环境的安装、重装、更新、失败恢复、启动及卸载；不会打开真实会话。CI 在 macOS 与 Windows 分别执行它。单元测试中的故障注入另行覆盖缓存损坏与自动回滚。
