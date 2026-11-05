# Codex Trace usage and licensing

[English](TERMS.md) | [简体中文](TERMS.zh-CN.md)

Updated: 2026-11-04. Codex Trace is independently maintained by Codex Trace contributors. This document describes the project's usage boundaries. Software licensing is governed by the repository's [MIT LICENSE](LICENSE); this document adds no restrictions on software use or distribution beyond that license. Bundled third-party components remain subject to their own licenses and attribution requirements; see [NOTICE](NOTICE) and the third-party license files.

Chinese reference translations are available for [LICENSE](LICENSE.zh-CN.md) and [NOTICE](NOTICE.zh-CN.md); the English originals remain unchanged.

## Features and support scope

Codex Trace displays saved execution records in supported local Codex desktop environments and exports local ZIPs at the user's request. Reading sources does not modify Codex tasks. Saving reading preferences, derived summaries, and exports writes to the plugin's own local files. See [PRIVACY.md](PRIVACY.md) for data scope, retention, and deletion.

The plugin depends on compatible Codex CLI/history APIs, a Python runtime, and a local filesystem. See [README.md](README.md) and [WINDOWS.md](WINDOWS.md) for current platform requirements and limitations. The project does not promise compatibility with every past or future host version, and does not provide cloud task execution or operation in a web-hosted environment.

Records can reflect only information saved by their sources. Interpret missing content, unknown states, usage, and time ranges in the UI alongside the source explanations. Search and filters mainly apply to loaded records. Exports include known sources for the selected task and confirmed child tasks, and different sources may be collected at different times. Successful ZIP verification means the saved bytes have been checked; it does not guarantee complete original sources or a snapshot taken at one instant. A save is confirmed only when the UI shows that the local export is verified and displays its actual path. After a timeout or an unconfirmed result, continue checking the original request.

The community repository marketplace distributes through users explicitly registering the GitHub repository. uv manages pinned Python and locked dependencies, and may use the network during initial preparation, dependency changes, or cache misses. Optional installers and bundled-runtime packages have their own prerequisites; see the [installation guide](INSTALL.md). Community publication does not establish OpenAI public-directory approval.

## Data and third-party services

Users should ensure they have the right to read, export, and share the task content involved, and review personal information, credentials, and third-party materials before sharing. This software's MIT license grants no additional rights to task content or third-party services.

Codex, ChatGPT, dependency-distribution services, and GitHub each have their own terms. This project does not represent OpenAI. Developer names and links in the manifests do not mean that official identity verification, directory review, or listing approval has been obtained.

## Warranty and support under the license

The software is provided as is under the MIT license. Its warranty disclaimer and liability terms are governed by the original [LICENSE](LICENSE). See [SUPPORT.md](SUPPORT.md) for feedback channels. The project promises no fixed response time or continuous availability.
