# Codex Trace GitHub Community Marketplace

[English](MARKETPLACE.md) | [简体中文](MARKETPLACE.zh-CN.md)

The primary distribution channel is the public GitHub repository marketplace `codex-trace-community`, with plugin `codex-trace` and current version identifier 0.2.2. Installation does not require GitHub credentials. Users explicitly register the repository; this does not establish OpenAI public-directory approval. Actual remote records determine whether commits or Releases are accessible and which platforms have been validated.

## Community installation and updates

With Git, uv, and a compatible Codex CLI available, run the complete installer:

```sh
uv run --no-project --no-config --python 3.14.2 https://raw.githubusercontent.com/sirius0414/codex-trace/v0.2.2/install_community.py
```

This entry point pins the matching Release tag and combines registration, package verification, dependency preparation, installation, and offline startup checks. No separate warmup is required. Update using the command in the new version's guide; `marketplace upgrade` does not advance a pinned tag. Start a new chat or restart an already-running Codex afterward. See [INSTALL.md](INSTALL.md).

## Community repository contents and validation

The root `.agents/plugins/marketplace.json` catalog points to `plugins/codex-trace/`. This generated directory contains manifests, uv launch configuration, backend, built frontend, contracts, dependency lockfile, and licenses. It is distinct from the unbuilt `plugin/` source directory. Maintainers generate and verify it, then commit it alongside the corresponding source so installation requires no user build.

Modern `mcp.json` uses `${PLUGIN_ROOT}/requirements.lock` and `${PLUGIN_ROOT}/launch.py` as its resource arguments. The host expands them to installed paths; `cwd` also points to the plugin root. Legacy `.mcp.json` retains relative arguments and `cwd="."`, without relying on older hosts to expand placeholders in arguments. Neither configuration may contain developer-machine paths.

Prepare development dependencies according to the [contribution guide](CONTRIBUTING.md), then generate and check the community directory:

```sh
.venv/bin/python scripts/build_community_marketplace.py
.venv/bin/python scripts/build_community_marketplace.py --check
```

On Windows, use `.venv\Scripts\python.exe`. By default, the command rebuilds the frontend with a fixed build time and writes `plugins/codex-trace/`. `--check` compares the expected output with the committed directory and fails for missing, extra, or changed files. Use `--assets dist/ui` only to reuse explicitly verified frontend artifacts; `--output DIR` selects a separate candidate directory. Include the generated directory and `.agents/plugins/marketplace.json` in the release commit.

Before release, verify that the proposed Git tree's generated content matches its source. In an isolated user directory, use the real Codex CLI to register the marketplace, install the plugin, check uv configuration, and run synthetic reading/export checks. Reports must identify actual platforms, versions, and unexecuted checks. macOS success does not replace native Windows checks; consult the corresponding GitHub Release validation records. Published Releases, checksums, and generated directories must correspond to the same commit and version.

Record cold dependency preparation, direct MCP/export checks, and the real Codex app-server's startup and discovery of the installed plugin separately. `plugin add`, `mcp list`, or directly launching the service alone does not prove that the host started MCP. Each Release supplies actual evidence. Success after preparation is not an empty-cache host startup pass. The current modern MCP manifest does not accept `startup_timeout_sec`; adding that field does not establish a longer startup wait. CI uses `--integrated-install` to execute the user installer, then checks host startup without an additional warmup.

Desktop entry points still need acceptance: reopen after restart or a new session, select tasks, read details, navigate confirmed child agents, observe updates, and verify exports. Check GitHub publication identity, push access, URL accessibility, and Release state directly rather than inferring them from local builds.

## Three distribution formats

| Item | GitHub community marketplace | Optional ZIP installer | Optional native offline package |
| --- | --- | --- | --- |
| Entry point | Root marketplace catalog and `plugins/codex-trace/` | `scripts/build_release.py` | `scripts/build_marketplace.py` |
| Python and dependencies | uv-managed Python 3.14.2 and locked dependencies | Local Python; installer creates an environment | Bundled for each OS/architecture |
| Network | Registration/updates and initial preparation, dependency changes, or cache misses | Required when installing dependencies | A complete runtime package starts without dependency installation |
| Codex CLI | Provided locally | Provided locally | Provided locally |
| Marketplace selector | `codex-trace@codex-trace-community` | `codex-trace@codex-trace` | Depends on the actual distribution and installation channel |

## Optional: native offline plugin package

The following bundled-Python candidate workflow is retained for maintainers who need per-platform offline runtime packages. It is not a prerequisite for community uv installation. Prepare, build, and validate each native OS/architecture separately; an example filename is `codex-trace-0.2.2-darwin-arm64.zip`.

## Prepare a standalone runtime

The build machine needs this project's development dependencies, Node.js/npm, Python 3.14+, and a compatible Codex CLI. Use the following entry point to fetch a standalone runtime for the current native platform, without manually locating archives or assembling license files:

```sh
.venv/bin/python scripts/fetch_marketplace_runtime.py \
  --output build/marketplace-runtime
```

On Windows, use `& .\.venv\Scripts\python.exe scripts/fetch_marketplace_runtime.py --output build/marketplace-runtime`. The tool selects only the current host's `darwin-arm64`, `darwin-x64`, or `win32-x64` target and makes no cross-platform validation claim. `--output` must name a new directory that does not already exist; an existing runtime is never overwritten.

[marketplace-runtime.lock.json](https://github.com/sirius0414/codex-trace/blob/main/marketplace-runtime.lock.json) pins the Python Standalone Builds `20260114` release and CPython `3.14.2`, recording the full archive URL, size, and SHA-256 for each of the three native targets. Downloads are cached in `build/marketplace-runtime-cache/` by default; use `--cache-dir DIR` to change this location. Cached and newly downloaded files are checked against the pinned hashes before extraction. A failed download or verification does not produce a usable runtime.

Only `python/install/`, `python/licenses/`, and `python/PYTHON.json` are extracted. Extraction uses `tarfile`'s `data` filter and additionally rejects directory escapes, external links, duplicate members, and special files; total extracted data is capped at 512 MiB. The tool checks the Python version, target platform, and every recursive `license_path` / `license_paths` reference in `PYTHON.json`, then passes all distribution license files and metadata to the preparer.

Some archives in this release omit the zstd / zlib-ng license texts referenced by their metadata. The lockfile therefore also pins official source URLs, sizes, and SHA-256 hashes for zstd `1.5.7` and zlib-ng `2.2.4`. The tool fills a missing referenced file only from a verified cache or the pinned URL. Supplemental sources are recorded separately under `source.supplementalLicenses` in the final `runtime-provenance.json`; their versions come from the build dependency declarations for that PBS release tag. Unresolved references cause preparation to fail. A single CPython LICENSE cannot replace the runtime's complete license collection.

An existing archive can be reused without downloading it again:

```sh
.venv/bin/python scripts/fetch_marketplace_runtime.py \
  --archive build/marketplace-python/cpython-full.tar.zst \
  --cache-dir build/marketplace-runtime-cache \
  --output build/marketplace-runtime
```

`--archive` must still match the pinned size and SHA-256 for the current target. To run this command without network access, also place the lockfile's matching `LICENSE.zstd.txt` and `LICENSE.zlib-ng.txt` in `--cache-dir` in advance; their hashes will be checked. Missing supplemental license files still trigger an attempted download from the pinned URLs. Supplying the archive alone does not mean that every offline input is ready.

The download helper does not execute upstream installation scripts. It calls `prepare_marketplace_runtime.py` to expand package-internal relative links, exclude bytecode, normalize safely relocatable sysconfig prefixes, and test the interpreter, native modules, and `compression.zstd` after renaming the directory. `runtime-provenance.json` records the source URL, archive and input-tree hashes, supplemental license sources, normalization actions, license files, and file hashes. Hash records support input traceability; they do not replace platform approval or maintainer review of license coverage.

When handling a new runtime release, maintainers can use the underlying preparer's `--source`, `--output`, `--source-url`, `--source-archive`, and repeatable `--license-file` arguments directly. License inputs must include `PYTHON.json` from the same distribution and all texts it references. Update and review the lockfile and native test evidence afterward. Do not substitute a `.venv` for standalone CPython or mix license files from different versions.

## Prepare locked dependencies and build

On the corresponding native OS, use the Python interpreter that will be bundled to download wheels for that platform. Example preparation on macOS:

```sh
build/marketplace-runtime/bin/python3 -I -B -m pip download \
  --only-binary=:all: --no-deps \
  -r requirements.lock \
  --dest build/marketplace-wheelhouse
```

On Windows, use `build\marketplace-runtime\python.exe` and PowerShell path and line-continuation syntax. Do not use a wheelhouse for a different OS, architecture, or Python ABI. Downloading requires network access; packaging afterward installs dependencies offline from the specified wheelhouse only. If a wheel is missing, supply the corresponding file instead of allowing the plugin to download it at startup.

```sh
.venv/bin/python scripts/build_marketplace.py \
  --runtime build/marketplace-runtime \
  --wheelhouse build/marketplace-wheelhouse \
  --output build/marketplace
```

The frontend is built by default. `--assets dist/ui` explicitly uses existing frontend artifacts; the caller must ensure that they match the current source. Outputs include the platform ZIP, `SHA256SUMS`, and a matching `.verification.json`. The plugin root uses portable `plugin.json` and `mcp.json` while retaining `.codex-plugin/plugin.json` and `.mcp.json` for older hosts. The configured `command` is `./runtime/bin/python3` on macOS or `./runtime/python.exe` on Windows, resolved by the host relative to the plugin root. The `launch.py` path in `args` and the `cwd` use `${PLUGIN_ROOT}`. The configuration contains no absolute Python/Codex paths from the build machine.

Inside the package, `runtime/` holds standalone Python, `runtime-deps/` holds the locked dependencies, and `package-manifest.json` records file hashes. Runtime provenance is stored in `runtime/runtime-provenance.json`. The package also includes the built UI, backend, contracts, icons, privacy/support/terms documents, and third-party license texts. It excludes real Codex configuration, credentials, sessions, derived databases, and user exports.

Packaging removes only CPython's self-test data and static-link archives, retaining the modules, dynamic libraries, and complete license collection needed at runtime. Hashes of removed files and the preparation-stage provenance hash are recorded in the runtime provenance file. The actual trimmed package must still pass dependency-import, relocation, and MCP checks. The builder also checks ZIP size, total extracted size, member count, and path limits, and refuses to produce a candidate package if a limit is exceeded.

`launch.py` enters the existing MCP service in isolated mode. The launcher discovers the Codex CLI locally; `CODEX_TRACE_CODEX_EXECUTABLE` can explicitly specify the absolute path to the native executable. On Windows, the configuration must ultimately resolve to `codex.exe`. The launcher does not run an installer, download dependencies, register a marketplace, or change trajectory-reading scope or export semantics.

## Validate the candidate package

First complete the build, backend/frontend tests, and contract checks in the [contribution guide](CONTRIBUTING.md#verification). Then rebuild from the same inputs into a second output directory and compare the ZIPs' SHA-256 hashes. Use the same frontend build-time settings for both builds; the default is the fixed `SOURCE_DATE_EPOCH=0`. Retain the source commit, toolchain, runtime provenance, wheelhouse, and validation reports so the candidate can be traced to its inputs.

Example for macOS arm64:

```sh
.venv/bin/python scripts/smoke_marketplace.py \
  --archive build/marketplace/codex-trace-0.2.2-darwin-arm64.zip \
  --report build/marketplace-smoke.json
```

This check uses a temporary user directory, synthetic sessions, and isolated Codex configuration. It validates through the real Codex CLI by default; add `--codex /path/to/codex` to select an executable. `--skip-cli` is only for package-level diagnostics in restricted environments and cannot be counted as installation validation with the real CLI. On macOS, `--deny-network` can verify startup and primary flows under restricted network access. That option does not apply to Windows or establish equivalent network validation on other systems.

CLI/package-level validation does not replace desktop validation. For every platform and architecture to be publicly supported, record results in a clean desktop environment for installation, reopening, task selection, detail reading, navigation to confirmed child Agents, incremental observation, and ZIP export verification. Check the platform limitations shown to users as well. If platform evidence is missing, narrow the candidate's supported scope or perform the missing checks; do not record another platform's results as a pass.

## Future option: OpenAI public directory

The following requirements apply only to a future OpenAI public-directory submission. They are not acceptance gates for the GitHub community marketplace. The community plugin retains local chat/execution-history access; technical success does not establish policy approval for a future public listing.

1. Follow the [OpenAI desktop plugin submission requirements](https://developers.openai.com/plugins/deploy/desktop-only) to confirm that the submitting organization is permitted to submit local MCP plugins and has completed the required developer or business identity verification. A manifest name or `requires_local_executor` does not establish eligibility.
2. **Compatibility between local chat-history reading and the public directory guidelines remains an unresolved blocker for a future public-directory submission.** The implementation continues to read existing local Codex chat/execution history. Obtain explicit platform confirmation of this use, scope, and presentation under the [plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines). Bundling a runtime, isolating installation, and passing automated tests have not changed this product behavior. They neither remove this policy blocker nor justify a claim of compliance with public directory requirements.
3. Verify in the submission portal that the target platform package, bundled runtime, MCP Apps entry point, and host compatibility are accepted, and submit test evidence matching the candidate. Repository checks cannot prove portal acceptance.
4. Push [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md), [SUPPORT.md](SUPPORT.md), and the corresponding code to the publicly declared branch. Check each homepage, support, privacy, and terms URL in the manifest for public accessibility. Local files and preconfigured URLs do not establish that public pages are live.
5. After checking the version, license collection, native platform reports, and desktop validation results, complete review and publication through the [official submission process](https://developers.openai.com/plugins/deploy/submission). Do not describe a GitHub draft Release or local marketplace registration as publication in the OpenAI public directory.

The existing GitHub Release workflow and local checks establish only the steps they actually performed. Community publication is established by the corresponding Git commit, installation evidence, and GitHub Release status. Public-directory publication separately requires portal review and its publication state.
