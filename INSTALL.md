# Codex Trace Installation Guide

[English](INSTALL.md) | [简体中文](INSTALL.zh-CN.md)

Codex Trace 0.2.1 primarily distributes through this GitHub repository's community plugin marketplace. It includes the built frontend, backend, contracts, and licenses; users need neither Node.js nor manual builds. See [PRIVACY.md](PRIVACY.md) for data access and storage, [SUPPORT.md](SUPPORT.md) for help, and [MARKETPLACE.md](MARKETPLACE.md) for maintainer instructions.

## Community marketplace prerequisites

- A local Codex desktop application and native CLI. Use CLI 0.153.0 or a later compatible version with plugin marketplace support and the read-only history APIs used by this plugin.
- Git and uv installed and discoverable from the process that starts Codex. `git --version`, `uv --version`, and `codex --version` should succeed in a terminal; Codex launched from the GUI must also find uv.
- GitHub access for registration and updates. The repository is currently **PRIVATE**; installers must have Git read access and valid local Git credentials. During initial startup, dependency changes, or cache misses, uv needs network access to obtain Python 3.14.2 and the dependencies in `requirements.lock`.
- Windows requires native Codex and local NTFS, without WSL. Start Codex and the terminal as an ordinary user; **Run as administrator is unsupported**. Consult the version's [Release validation records](https://github.com/sirius0414/codex-trace/releases/tag/v0.2.1) for actual platform results; macOS and simulated tests do not establish Windows success. See [WINDOWS.md](WINDOWS.md) for other limitations.

The community workflow does not require manually installing Python first; uv manages it and the dependencies. Do not start the plugin with `sudo` or manually change dependencies in its cached environment.

## Community marketplace installation

In a macOS terminal or Windows PowerShell:

```sh
codex plugin marketplace add https://github.com/sirius0414/codex-trace.git
codex plugin add codex-trace@codex-trace-community
```

The marketplace is `codex-trace-community` and the plugin is `codex-trace`. Registration locates `plugins/codex-trace/` through the repository catalog; Codex manages the installed cache. uv may take time to prepare the environment on first use. It manages caches outside the plugin directory; restarting is not guaranteed to avoid network access.

After installation, complete the runtime preparation below. Once successful, restart Codex or start a new session and open **New Tab → 执行轨迹 (Execution Trace)**. The global entry point first displays a task picker. Old panels may retain old resources; reconcile unconfirmed exports before closing them. These commands and repository manifests do not imply OpenAI public-directory approval: users explicitly register this community channel.

If `codex-trace@codex-trace` (ZIP installer) or `codex-trace@codex-trace-local` (development install) is already enabled, disable it first to avoid duplicate entry points. Reconcile retained data and pending exports through the original channel.

## First use and runtime preparation

Before first use, after dependency changes, or after clearing uv caches, run the corresponding installation and preparation block below before opening the plugin. Cold downloads may exceed the host's default MCP startup wait of approximately 30 seconds. Preparation obtains Python and dependencies separately in the terminal. It can replace the bare `plugin add` command above and can be repeated for an installed plugin. The CLI's `installedPath` identifies the current installation without manually finding its cache.

macOS (zsh or bash; no preinstalled Python or jq required):

```sh
(
  set -o pipefail
  trace_root="$(codex plugin add codex-trace@codex-trace-community --json | uv run --no-project --no-config --python 3.14.2 -- python -I -B -c 'import json,sys; print(json.load(sys.stdin)["installedPath"])')" &&
  cd "$trace_root" &&
  uv run --no-project --no-config --python 3.14.2 --with-requirements ./requirements.lock -- python -I -B -c 'import compression.zstd, mcp; print("Codex Trace runtime ready")'
)
```

Windows PowerShell:

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

After `Codex Trace runtime ready` appears, restart Codex or start a new session before opening the viewer. Use the same user and uv default cache/runtime locations as Codex. The host does not inherit every terminal `UV_*` setting by default; verify the actual host environment before using custom caches rather than assuming terminal preparation covers the host. Prepare again after changing users, cache locations, or dependencies. Resolve Git access, uv networking, or cache errors before considering the plugin ready. Each Release records dependency preparation, MCP/export, and real-host startup results separately. Success after preparation does not establish startup with an empty cache; the Windows commands alone do not establish native-platform or ordinary-user desktop acceptance.

## Community marketplace update

```sh
codex plugin marketplace upgrade codex-trace-community
codex plugin add codex-trace@codex-trace-community
```

Run the same [installation and runtime preparation block](#first-use-and-runtime-preparation) after updating, then restart Codex or start a new session. Repository updates and dependency changes may require network access. Resolve Git, uv, proxy, or certificate failures before repeating the workflow. `install.py recover` applies only to the optional ZIP installer below, not to community plugin cache recovery.

## Community uninstall and caches

```sh
codex plugin remove codex-trace@codex-trace-community
codex plugin marketplace remove codex-trace-community
```

Plugin removal and marketplace removal handle the installed program and marketplace registration separately. Derived indexes, preferences, export receipts, and final ZIPs remain. uv manages download and runtime caches, which the plugin does not actively delete; shared caches may be used by other programs. Reconcile pending exports, stop the plugin service, and close its pages before following the [data cleanup instructions](PRIVACY.md#uninstallation-and-data-deletion). Do not delete Codex's original session directories.

Custom runtime configuration uses `CODEX_HOME`, `CODEX_TRACE_CODEX_EXECUTABLE`, `CODEX_TRACE_STATE_ROOT`, and `CODEX_TRACE_EXPORT_ROOT`. Set these variables in the environment that starts Codex before launching the application, then restart Codex after installation or configuration changes. The bundled legacy overlay explicitly forwards these four variables to the MCP process; it does not forward the whole environment. The uv default-cache requirements and `UV_*` inheritance limits above still apply.

Common startup issues: if uv cannot be found, check Codex's PATH and restart the application. If preparation fails, check uv's network, proxy, certificates, and free disk space. If Codex cannot be discovered, set `CODEX_TRACE_CODEX_EXECUTABLE` to the absolute path of the actual native program. A wrapper directory or application directory is not an executable. Do not disable TLS verification to address download failures.

## Optional: release ZIP installer

The existing workflow below downloads and runs `codex-trace-0.2.1.zip` separately from the community marketplace. It requires local Python 3.14+, registers `codex-trace@codex-trace`, and prepares Python dependencies during installation. See [MARKETPLACE.md](MARKETPLACE.md#optional-native-offline-plugin-package) for native offline package builds and validation.

## Supported platforms and prerequisites

| Item | Requirements |
| --- | --- |
| macOS | A working local Codex desktop application and its companion native CLI; Python 3.14+ |
| Windows | Windows 10/11, native Codex and Python 3.14+, and local NTFS volumes; WSL is not required |
| Python | Working `venv`, `pip`, and `compression.zstd`; the installer checks them directly |
| Codex | Support for `plugin marketplace`, `plugin add/remove`, and the read-only app-server APIs used by this plugin |
| Network | Locked Python dependencies must be downloaded when first creating the environment; normal plugin startup does not install dependencies |

Linux, browser/cloud execution, and state or export directories on network volumes, Windows UNC paths, junctions, or reparse points are unsupported. The Windows backend has native support, but CI installation tests do not replace acceptance of the actual Windows Codex desktop entry point and export flow. See the repository's [WINDOWS.md](WINDOWS.md) for completed and outstanding platform validation.

Install and start Codex first. If its CLI cannot be found in the terminal, the macOS installer tries common desktop installation locations. On Windows, add the native CLI to PATH or specify the native executable. Install Python 3.14 or later from the [official Python download page](https://www.python.org/downloads/). On Windows, enable Python Launcher or add Python to PATH, then reopen the terminal. Administrator privileges are not required; do not run this installer with `sudo`.

## Download and verify

Download `codex-trace-<version>.zip` and `SHA256SUMS` for the same version from this project's [GitHub Releases](https://github.com/sirius0414/codex-trace/releases). Use the ZIP in the release attachments. GitHub's automatically generated Source code ZIP can contain the compiled UI under `plugins/`, but does not provide the layout expected by the standalone installer. Until a release is published, the end-user download is not available.

For version `0.2.1`, verify from the download directory before extracting:

macOS terminal:

```sh
shasum -a 256 -c SHA256SUMS
unzip codex-trace-0.2.1.zip
cd codex-trace-0.2.1
```

Windows PowerShell:

```powershell
(Get-FileHash .\codex-trace-0.2.1.zip -Algorithm SHA256).Hash
Get-Content .\SHA256SUMS
# Confirm that the SHA-256 matches this ZIP's entry in the file before extracting.
Expand-Archive .\codex-trace-0.2.1.zip -DestinationPath .
Set-Location .\codex-trace-0.2.1
```

If verification fails, download the files again rather than continuing installation. Checksums detect content changes; they do not replace trust in the download source.

## First installation

macOS:

```sh
sh ./install.sh
```

Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The PowerShell command sets the execution policy for this process only; it does not change the system policy. If organizational policy prevents scripts from running, use Python directly:

```powershell
py -3.14 .\install.py install
```

The installer checks prerequisites, creates an isolated virtual environment, installs and verifies dependencies from `requirements.lock`, generates local MCP configuration, registers Codex Trace's local plugin marketplace, and installs the plugin. Absolute paths to the local interpreter and Codex are written only to the local installation directory, not included in the download. Neither the source directory nor the downloaded package directory is a runtime dependency.

Reload plugin resources after installation. If menus still show old content, restart Codex when convenient. The installer does not restart the desktop application or end sessions.

If automatic discovery fails, specify paths explicitly, replacing these examples:

```sh
python3.14 install.py install --python "/path/to/python3.14" --codex "/path/to/codex"
```

```powershell
py -3.14 .\install.py install --python "C:\Python314\python.exe" --codex "C:\Program Files\Codex\codex.exe"
```

`--codex` must point to a native program. Standard npm `codex.cmd` installations can be detected automatically; custom batch wrappers must instead use the actual `codex.exe`. You can also use `CODEX_TRACE_PYTHON` to select Python for the launcher scripts.

By default, the installer operates on the Codex configuration selected by `CODEX_HOME`, or `.codex` in the user's home directory if it is unset. It manages only `codex-trace@codex-trace` and its separate marketplace of the same name, without rewriting other marketplaces, plugins, or sessions. The original development workflow's `codex-trace-local` is a different marketplace. If you installed a development version, disable the old version through Codex plugin management to avoid enabling two tools with the same name.

| Data | macOS | Windows |
| --- | --- | --- |
| Installation receipts, versions, and isolated environments | `~/.local/share/codex-trace/installations/<id>/` | `%LOCALAPPDATA%\codex-trace\installations\<id>\` |
| Indexes, preferences, and export receipts | `~/.local/share/codex-trace/index/` | `%LOCALAPPDATA%\codex-trace\index\` |
| User-exported ZIPs | `~/Downloads/Codex Trace/` | `%USERPROFILE%\Downloads\Codex Trace\` |

The installation identifier is derived from the target `CODEX_HOME`, giving different Codex configurations separate installation records. Advanced users can select the installation management directory with `--root`; use the same argument for later updates, recovery, status checks, and uninstallation. Do not move an active installation directory or its Python environment.

## Migrating from Codex Display

Codex Trace uses the plugin identifier `codex-trace`, community marketplace `codex-trace-community`, optional ZIP installer marketplace `codex-trace`, and source development marketplace `codex-trace-local`. This is a first installation under a new identity. It does not automatically replace `codex-display@codex-display` or `codex-display@codex-display-local`.

1. Check any unconfirmed export results in the old version, close its panels, and disable the old plugin through Codex plugin management.
2. Follow the first-installation steps above to install Codex Trace. For a source development installation, use the README's build, packaging, and installation workflow. Use a separate installation management directory for the new version; do not pass the old version's installation directory as `--root`.
3. Reload plugin resources and open “执行轨迹” (“Execution Trace”). After confirming that the new version works, follow the old version's uninstall instructions to remove its plugin and marketplace registration.

The new version uses the `codex-trace` state directory and `Downloads/Codex Trace/` export directory. The old `codex-display` state directory, runtime environment, and `Downloads/Codex Display/` export files remain in place. Indexes, reading state, interface preferences, and export receipts are not migrated automatically. Do not move a Python environment still used by an old panel or copy unfinished export receipts from the old version into the new directory.

Update custom runtime configuration to use `CODEX_TRACE_*` environment variables and the `codex_trace.app` Python entry point. The old `CODEX_DISPLAY_*` configuration and `codex_display.app` entry point apply only to the old version.

## Updates and repeated installation

Download and verify the new version, extract it, and run the same installation command to update. You can also use:

```sh
python3.14 install.py update
```

On Windows, use `py -3.14 .\install.py update`. There is no need to uninstall first. Reinstallation is also the entry point for restoring a missing or damaged installation. Do not run `pip install --upgrade` yourself in the installed virtual environment, because that would make dependencies inconsistent with the lockfile.

If the Codex or Python installation path changes, rerun the installer to regenerate local configuration. Older runtime environments are retained during updates so that panels that have not reloaded their resources keep working. User task records and final exported ZIPs are outside the update cleanup scope.

## Installation failures and recovery

Dependency installation, verification, and marketplace switching run as separate stages. In the installation directory, `journal.json` records the latest operation, `current.json` records the most recently confirmed installation, and `transactions/` retains each attempt with `pip.log` and `verification.log`. A failed installation does not mean the old version is damaged. If switching fails, the installer attempts to reinstall the previously confirmed version and reports whether recovery succeeded.

First check status:

```sh
python3.14 install.py status
```

After addressing the reported problem, recover:

```sh
python3.14 install.py recover
```

On Windows, change the command prefix to `py -3.14 .\install.py`. If a confirmed version exists, recovery tries to reinstall it. If this is a first installation and preparation completed, recovery retries installation of the verified artifact. If preparation was incomplete, recovery only ends the unfinished transaction and asks you to rerun `install`. Then run `install` again from the complete, verified release directory. Keep installation records and old environments until recovery is complete. Do not delete the entire Codex directory, recreate other plugins' configuration, or terminate active tasks.

## Uninstall

Run from the fully extracted release directory:

```sh
python3.14 install.py uninstall
```

On Windows, use `py -3.14 .\install.py uninstall`. Uninstallation removes this plugin's installation and marketplace registration. Running it again can retry an incomplete uninstall. You can also uninstall through Codex plugin management, then run the command above to remove this plugin's marketplace registration.

Uninstallation retains isolated runtime environments, installation records, indexes, and exports so that panels still using older environments and exports awaiting verification are not disrupted. After closing the relevant panels and confirming that recovery is no longer needed, you may delete this plugin's installation directory yourself. Manage index and export directories separately. **The installer never deletes user exports in `Downloads/Codex Trace/`.** Do not delete Codex session directories or other plugins' caches.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Python cannot be found, is too old, or lacks `compression.zstd` | Install a complete Python 3.14+ distribution and reopen the terminal. Use `--python` to specify the actual interpreter. Minimal distributions may omit zstd; use a complete distribution instead. |
| Codex cannot be found or the `plugin` subcommand is unavailable | Install or update the CLI accompanying Codex desktop. Use `--codex` to specify the native program, not the desktop application's directory. |
| `pip` download, proxy, or certificate failure | Inspect the transaction's `pip.log` and the local Python network, certificate, and proxy configuration, then retry after fixing the cause. Do not remove dependency locking or disable TLS verification. |
| No dependency package is available for the current platform | The lockfile may not yet have an installable build for that Python version or architecture. Prefer Python 3.14, keep the logs, and report the issue. Do not copy another computer's virtual environment directly. |
| A marketplace has the same name but belongs to another source | The installer stops rather than taking over another source. Inspect the `codex-trace` marketplace in Codex plugin management, establish ownership, and resolve the conflict yourself. |
| Another installation is reported as running | Wait for it to finish. Do not install, update, or uninstall the same target concurrently. If the installation process exited abnormally, follow the recovery instructions in the error. |
| The new version's menu or interface has not refreshed | Reload plugin resources first, then restart Codex when convenient. Open panels may still use old resources. |
| Export cannot be created | Ensure that the user's `Downloads` parent directory exists and is writable. On Windows, use local NTFS and avoid redirected or locked directories. Check unconfirmed saves using the original request rather than creating repeated requests. |
| Status is empty after using a custom installation directory | Pass the same `--root` to every command and confirm that `CODEX_HOME` matches the value used at installation. |

When reporting a problem, include OS and Python/Codex versions, the failing stage, and an error summary with personal information removed. Logs and local receipts may contain local paths. Do not publicly upload Codex credentials, sessions, or complete user configuration.

## Plugin marketplaces and public listing

Users register this GitHub community marketplace explicitly; it does not constitute an OpenAI public-directory listing. The optional ZIP installer prepares a local environment and registers a separate local marketplace. Both retain the existing trajectory-reading, caching, and export behavior.

Any future OpenAI public-directory submission separately requires confirmation of local MCP submission eligibility, developer identity, portal compatibility, and policy compatibility for reading local Codex chat/execution history. Those conditions apply only to that future channel and do not replace or form part of community repository technical acceptance. See [MARKETPLACE.md](MARKETPLACE.md#future-option-openai-public-directory).

## Maintainers: build a release package

After installing Node.js/npm and Python 3.14+ in a source checkout, run:

```sh
npm ci
npm run release:build
```

You can also run `python3.14 scripts/build_release.py` directly. The default output is `build/releases/codex-trace-<version>.zip` and `build/releases/SHA256SUMS`; the package version comes from the project manifests. The build uses a file allowlist and excludes `.venv`, `node_modules`, Codex configuration, credentials, sessions, indexes, and exports. It defaults to a fixed `SOURCE_DATE_EPOCH=0`; identical toolchains, source, dependencies, and that time value produce byte-identical release ZIPs. You may explicitly set `SOURCE_DATE_EPOCH`, but use the same value for both builds when comparing them. Release packages do not contain the build machine's runtime paths.

When triggered by a version tag, the GitHub Release workflow runs macOS/Windows validation before creating a draft release with the ZIP and checksums. Manual runs on branches only validate and retain build artifacts; manual runs on `v*` tags also run the draft-release step. Maintainers decide whether to publish the draft after reviewing platform test results and limitations. Configuring the workflow or building locally does not automatically push to GitHub or list the plugin in the public directory.

To verify a release package, run `python3.14 scripts/smoke_release.py --archive build/releases/codex-trace-0.2.1.zip --report build/release-smoke.json`. This command uses a real Codex CLI and a new virtual environment only in a temporary user directory to check installation, reinstallation, updates, failure recovery, startup, and uninstallation. It does not open real sessions. CI runs it separately on macOS and Windows. Fault-injection unit tests separately cover cache corruption and automatic rollback.
