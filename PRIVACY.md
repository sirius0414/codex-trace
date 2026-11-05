# Codex Trace privacy notice

[English](PRIVACY.md) | [简体中文](PRIVACY.zh-CN.md)

Updated: 2026-11-04. Applies to Codex Trace 0.2.0 maintained in this repository. The project is maintained under the name Codex Trace contributors; see [SUPPORT.md](SUPPORT.md) for the project homepage and contact options.

Codex Trace reads, displays, and exports task records that Codex has saved locally. The plugin stores derived local data and exported files. “Read-only” means it does not modify the Codex task sources being observed; it does not mean that no data is stored.

## What is read and why

The plugin reads task lists, metadata, turns, and entries through the local Codex CLI's read-only app-server history API, and reads `sessions/` and `archived_sessions/` under `CODEX_HOME`. By default, `CODEX_HOME` is `.codex` in the user's home directory. These records may contain task titles and identifiers, timestamps, message bodies, code, tool arguments and outputs, saved reasoning fragments, file paths, usage, and Agent relationships.

This data supports task selection, trace browsing, search, incremental observation, source verification, and export. Viewing a confirmed child Agent reads the corresponding child task. Export independently enumerates the known raw sources of the selected task and its confirmed child tasks; its scope is not narrowed by the current page's search, filters, folded sections, or pagination. Export does not automatically redact credentials, personal information, or business content from raw records. Review a ZIP before sharing it.

The plugin validates task identity, source roots, and inherited scope. It does not create, continue, or interrupt tasks through the history API, and does not write to the Codex session database. Missing sources, format changes, or read limits may leave displays and exports incomplete.

## Local storage and retention

| Data | Default location | Retention and cleanup |
| --- | --- | --- |
| Derived SQLite database | macOS: `~/.local/share/codex-trace/index/derived.sqlite3`; Windows: `%LOCALAPPDATA%\codex-trace\index\derived.sqlite3` | Stores task projection summaries, source references, reading preferences, source instance identifiers, and export receipts. Summaries may contain task excerpts; the current projection cache does not duplicate complete detail fields. No automatic deletion period is set. |
| Temporary export ZIPs | `index/exports/` under the directory above | Expire 30 minutes after creation by default. While running, the service periodically cleans up snapshots whose ownership it can establish; it does not perform scheduled cleanup while stopped. A save in `pending` or `unknown` state retains the materials needed for recovery until the result is known. Releasing a temporary snapshot does not delete persistent receipts or the final ZIP. |
| Final export ZIPs | macOS: `~/Downloads/Codex Trace/`; Windows: `%USERPROFILE%\Downloads\Codex Trace\` | Contain collected raw records, a source manifest, and hashes. Managed by the user, with no automatic deletion period. |
| Installation records and runtime environments | macOS: `~/.local/share/codex-trace/installations/<id>/`; Windows: `%LOCALAPPDATA%\codex-trace\installations\<id>\` | Store installation transactions, configuration, diagnostic logs, and isolated Python environments for updates and recovery. The installation identifier is derived from the Codex source-directory path. Uninstallation retains these materials. |
| Runtime and dependencies in an optional native offline package | The plugin cache managed by the host that installs the package | Contains the program, runtime, dependencies, and licenses; it is not a task-data cache. The host determines package-cache retention and removal. |
| Community marketplace Python, dependencies, and download caches | External directories managed by uv, according to its local configuration | Initial preparation obtains Python 3.14.2 and locked dependencies. Other applications may reuse these caches. uv manages retention and cleanup; plugin uninstallation does not proactively delete them. |
| UI preferences | Page `localStorage`, when allowed by the host | Stores display options such as language, style, font size, wrapping, and line numbers. The plugin sets no automatic deletion period. Task bodies are not stored here. |
| Page export-recovery identifiers | Page `sessionStorage`, when allowed by the host | Stores source/task IDs, export request IDs, export IDs, and phases; not bodies, manifests, or final paths. Its lifetime is controlled by the host's page storage. |

The `installations/` directories above apply to the release ZIP installer channel. The host caches the community plugin program, while uv manages Python and dependencies outside that directory. Optional native offline packages keep their runtime and dependencies in the host's plugin cache. All channels retain the same task-state and final-export directories. `CODEX_TRACE_STATE_ROOT`, `CODEX_TRACE_EXPORT_ROOT`, or custom installer paths can change the corresponding locations. Development browser mode also stores debugging indexes, logs, launch entry points, and exports under `build/dev-browser/`; use the actual launch configuration to locate them. Operating-system backups, synchronization tools, and files copied separately by users are managed by those tools and are not deleted by plugin cleanup.

## Data transfer and network access

The production UI receives task data through the local MCP service and the host's app-private result `_meta`. Ordinary tool-result `content` and `structuredContent` do not carry task bodies. This describes the plugin's data channel; it does not replace the host's permissions or data-handling rules.

The current plugin has no feature for uploading traces to servers operated by the project maintainers, and the project operates no analytics or advertising service. Community marketplace registration and updates access GitHub. During initial preparation, dependency changes, or cache misses, uv obtains pinned Python 3.14.2 and locked dependencies from its configured distribution sources. The release ZIP installer may also use the network to obtain Python dependencies. Only the optional native offline package includes its runtime and dependencies, so its launcher does not download them. Codex itself, external links opened by users, and issue submissions may also use the network; those services have their own data-handling rules. Text and attachments voluntarily submitted to GitHub Issues are sent to GitHub and may be displayed publicly. Do not submit raw sessions, complete export ZIPs, credentials, or unredacted logs.

## Uninstallation and data deletion

The installer's uninstall operation removes this plugin and its corresponding local marketplace registration. It retains indexes, preferences, receipts, installation records, old runtime environments, and final exports. Uninstallation does not delete original Codex task records.

For the community marketplace, uninstall with `codex plugin remove codex-trace@codex-trace-community` and remove registration with `codex plugin marketplace remove codex-trace-community`. Other host-installed channels use their corresponding uninstall entry point. Codex Trace has no uninstall hook that automatically deletes its state directories or final ZIPs when the host uninstalls it. The host controls removal of its program cache, while uv manages its shared caches separately. Removing the program cache does not mean all derived task data has been deleted.

To clean up local data, first check all outstanding exports in the UI and confirm the paths and verification results of saved files. Then disable the plugin, stop its local service, and close the relevant pages. You can then use the system file manager to delete the plugin state directory, installation directories, and final ZIPs that are no longer needed, using their actual configured locations. Use the host's page-storage controls to clear UI preferences and recovery identifiers. Do not delete `.codex/sessions` or `.codex/archived_sessions` to clear this plugin's cache.

Deleting snapshots, the database, or page recovery identifiers while a save is `pending`, `unknown`, or otherwise unconfirmed removes the materials needed to check that same request. It does not prove that a final ZIP was never created, and it cannot cancel a save that has already been accepted. Resolve outstanding requests before cleanup. Deleting the derived database resets reading preferences, source instance identifiers, and historical receipts; later browsing may recreate summary caches.

The project holds no remote copy of files stored on your computer, and maintainers cannot delete those files remotely on your behalf. For privacy questions, use the [support options](SUPPORT.md) to submit a description that contains no sensitive materials. Report security or privacy vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
