# 原生 Windows 适配与验收

目标环境为 Windows 10/11、本地 NTFS 卷及原生 Codex，无需 WSL。请以普通用户启动 Codex 和终端，不支持“以管理员身份运行”：提权令牌可能把新建文件的所有者设为 Administrators 组，插件会拒绝这类状态文件，而不会接管所有权或放宽保护。社区市场需要 Git、uv 和 Codex CLI 0.153.0 或更高兼容版本，由 uv 管理 Python 3.14.2；首次准备、依赖变化或缓存缺失时需要网络。可选 ZIP 安装器和下方源码开发流程则要求本机 Python 3.14+（含 `compression.zstd`）。普通用户按 [社区市场安装指南](INSTALL.md#社区市场安装) 操作，无需 Node.js/npm；下面的源码开发流程才需要 Node.js/npm。

社区渠道的原生测试结果见对应版本的 [GitHub Release 验证记录](https://github.com/sirius0414/codex-trace/releases/tag/v0.2.0)。以下实现说明、macOS 测试与模拟测试不能作为 Windows 通过的证据。

GitHub Windows runner 使用管理员令牌。CI 在独立复制的令牌中把新文件的默认所有者设为当前用户，再执行测试和合成安装／导出检查；它不改已有文件所有者、不放宽产品的 SID/DACL 检查，也不修改 runner 的原始令牌。这是原生 Windows API 验证，仍保留管理员权限，不等同于普通用户桌面实机验收；提权启动不在支持范围内。

## 从源码开发和本机组包

在 PowerShell 中准备已有依赖；路径可包含中文和空格，无需激活虚拟环境或修改执行策略：

```powershell
py -3.14 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
npm ci --ignore-scripts
npm run build

# 确认实际原生 Codex 路径；也可以让脚本识别标准 npm 安装的 codex.cmd。
$codexExe = 'C:\Program Files\Codex\codex.exe'
npm run plugin:update -- --prepare-only --python "$PWD\.venv\Scripts\python.exe" --codex "$codexExe"

# 浏览器开发服务；就绪后打开输出的本机地址。
npm run dev:browser:start -- --python "$PWD\.venv\Scripts\python.exe" --codex "$codexExe"
npm run dev:browser:status
npm run dev:browser:stop
```

`--prepare-only` 只生成和验证产物，后续安装按 [README](README.md#安装更新与卸载) 使用输出目录。该开发流程生成的本机包绑定解释器与 Codex 的绝对路径，不能直接在 macOS 和 Windows 之间复制使用；迁移运行环境后重新打包。面向其他用户的发布 ZIP 由安装器在目标机器生成这些路径，参见 [安装指南](INSTALL.md)。启动时不会下载依赖。

npm 开发入口通过 `scripts/run_python.mjs` 优先选择 `CODEX_TRACE_PYTHON` 或仓库 `.venv`，再查找系统 Python。Windows 包装器只识别标准 npm 安装布局并定位原生 `codex.exe`；自定义 `.cmd`/`.bat` 必须改为传入实际程序。npm 构建通过 Node 运行 npm CLI，用户目录和参数作为独立 argv 传递。后台开发服务需要 PowerShell/CIM 读取进程创建时间与命令行，身份不明或停止超时会保留记录并报错。

| 数据 | Windows 默认位置 |
| --- | --- |
| 只读任务来源 | `%USERPROFILE%\.codex\sessions`、`archived_sessions`；可由 `CODEX_HOME` 覆盖 |
| 派生索引、偏好、回执、临时 ZIP | `%LOCALAPPDATA%\codex-trace\index` |
| 最终导出 | `%USERPROFILE%\Downloads\Codex Trace` |

`Downloads` 父目录须已存在。状态和导出根目录可由本机 `CODEX_TRACE_STATE_ROOT`、`CODEX_TRACE_EXPORT_ROOT` 配置，工具参数和前端不能指定目的地。正式前端回执继续要求 `Downloads\Codex Trace` 后缀；隔离测试可注入临时目录。Windows 的文件占用可能导致操作失败，应先释放占用，再核对同一个请求，不能因超时或 `unknown` 另起保存请求。

## 平台边界和保障

`backend/codex_trace/platform/` 负责系统差异，原始轨迹分析、业务投影、导出状态机和前端继续共用。

| 边界 | macOS | Windows |
| --- | --- | --- |
| 来源读取与目录遍历 | `dir_fd`、`O_NOFOLLOW` | `CreateFileW` 逐层拒绝 reparse point；目录句柄保留祖先、限制写入和删除共享；内部使用卷 GUID 路径 |
| 私有状态 | 所有权及 0700/0600 | 核对 owner SID；受保护的当前用户/SYSTEM DACL；SQLite 边车允许同样受限的继承 ACL |
| 并发与幂等 | `flock` + SQLite/CAS 回执 | `LockFileEx` + 同一 SQLite/CAS 回执；锁文件持续保留 |
| 最终发布 | 无覆盖硬链接 | handle-relative `NtSetInformationFile(FileLinkInformation)`，禁止替换既有名称 |
| 内容确认 | 文件刷盘、哈希和身份读回 | 文件刷盘、哈希和身份读回；写句柄先关闭，随后只读重开冻结快照 |
| app-server 管道 | 非阻塞 selector | 有界队列和专属读写线程，统一请求截止时间；关闭时用保留的线程句柄取消同步 I/O，再回收连接 |
| 来源变化 | kqueue + 轮询 | 最长 250 ms 的轮询兜底；每次读取仍校验身份和范围 |

Windows 文件保护使用真实 ACL 和原生文件身份，不把 `chmod`、`Path.resolve()` 或去掉 Unix 标志当成等效保证。普通文件描述符保持二进制模式；按偏移读取使用 `ReOpenFile` 独立文件指针，不改变调用方或其 `dup` 的位置。原始来源只申请读取权限，不加写锁、不收紧来源 ACL。app-server 本身仍可能维护自己的运行缓存。

既有 POSIX 导出路径继续有效；Windows 默认目录为 `%USERPROFILE%\Downloads\Codex Trace\`，中文与空格路径示例为 `C:\Codex Trace 测试\导出记录\codex-trace-….zip`。Schema 和两个前端导出入口共用测试样例，拒绝相对盘符、UNC、设备命名空间、ADS、遍历、保留设备名及尾部点/空格。后端另核对实际目录与文件身份，不能仅靠前端语法确认安全。

不支持网络卷、UNC、junction、符号链接或其他重解析点上的来源/状态/导出目录，包括通过这些机制重定向的用户目录。未验证 FAT/exFAT、ReFS、云同步占位文件及所有长路径组合。Windows 没有本适配层可用的目录 `fsync` 等价物：文件内容刷盘与读回得到检查，但不承诺突然断电后的目录项持久性；未决回执仍须用原请求核对。管理员和 SYSTEM 保留操作系统本身的权限。

原生 API 依据：[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)、[FILE_LINK_INFORMATION](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_link_information)、[ReOpenFile](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-reopenfile)、[CancelSynchronousIo](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelsynchronousio)。

## 可执行回归检查

Windows PowerShell：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = "$PWD\backend;$PWD\tests"
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe contracts/generate_types.py --check
npm test
npm run build
git diff --check

# 只运行平台核心检查（仍使用真实当前宿主的进程和文件系统）。
& .\.venv\Scripts\python.exe -m unittest test_platform_filesystem test_platform_runtime test_platform_pipes test_platform_scripts -v
```

`test_platform_runtime` 覆盖中文/空格路径、分片 UTF-8 管道、读取超时、写入背压、线程/子进程回收、spawn 跨进程锁、两个进程竞争同一候选的无覆盖保存、原始 ZIP 逐字节核验及回执跨重启幂等恢复。`test_platform_pipes` 检查另一个自有进程继续占用管道时的阻塞 I/O 回收。`test_platform_filesystem` 增加 Windows 专有的 junction、目录固定、独占文件占用、SID/DACL 和 SQLite 边车检查。`test_platform_scripts` 的路径选择模拟测试会明确标注模拟；CIM、Windows argv 和 Job Object 用例只在 Windows 原生执行。

旧的 FIFO、Unix chmod 攻击用例仅在适用平台运行；Windows 对应检查使用原生占用/ACL/句柄语义。Windows 无符号链接创建权限时，仅跳过需要该权限的链接用例；junction 用例独立执行。任何跳过都必须保留在报告中。

## 本次验证边界（2026-10-28）

当前开发机为 macOS，未提供 Windows 实机或 Windows VM。实现基于本地提交 `b3a619a`；尝试拉取远端最新代码时 GitHub HTTPS 凭据不可用，无法确认远端是否另有提交。开始时工作区无未提交修改。

- 修改前基线：Python 478 项、前端 288 项通过，构建及契约生成检查通过。
- 修改后完整回归：Python 共 519 项，506 项通过、13 项 Windows 专属检查跳过；前端 291 项通过；构建、契约类型生成一致性、Python/JavaScript 语法及 `git diff --check` 通过。Python 回归无资源未关闭警告。完整日志保留在本机 `/tmp/codex-trace-windows-*.log`。
- 最终 macOS 组包与隔离缓存验证：45 项通过；报告为 `build/windows-compat-final-buqu_rz5/verification.json`，`desktopInstalled=false`、`hostUiVerified=false`、`taskDataRead=false`。验证使用合成任务及临时目录，没有真实插件安装或任务读取。
- 所有 macOS 测试、Windows 分支模拟、Python 编译检查都不能视为 Windows 实机通过。

仍需在 Windows 执行上面的完整检查，并验收：本地 NTFS 的 ACL/继承、junction/祖先替换、文件占用、同请求多进程保存及崩溃恢复；真实 Codex CLI 的发现/安装/更新/回滚；桌面 MCP 入口和中文路径导出；开发服务 start/status/restart/stop 与异常退出后的子进程回收。尚未进行真实插件更新或桌面入口操作。
