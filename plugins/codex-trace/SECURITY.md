# Security policy

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

## Supported versions

The currently maintained release line is `0.2.x`. Security fixes target its latest patch version. Include the actual version or commit SHA when reporting an issue. If you use an older patch version, first check whether the issue still occurs in the latest version. Other versions are outside the current security maintenance scope.

## Platform and data boundaries

Codex Trace runs locally on macOS or native Windows with a compatible Codex CLI. It does not provide cloud-task or web-hosted execution. Platform support and version-specific validation are documented in [INSTALL.md](INSTALL.md) and [WINDOWS.md](WINDOWS.md); this policy is not evidence that a particular platform check passed.

The plugin reads existing Codex task records without modifying those sources. It writes its own derived summaries, reading preferences, export receipts, temporary snapshots, and user-requested ZIP exports. See [PRIVACY.md](PRIVACY.md) for storage and deletion details. Reports about source access, path containment, unintended disclosure, or unsafe writes fall within this policy's scope.

The community Git marketplace package uses uv to prepare Python and locked dependencies outside the plugin directory. Initial preparation, changed dependencies, or missing caches may require network access. The optional installer and bundled-runtime artifacts have different preparation steps; follow their installation instructions. Local processing does not mean dependency preparation is always offline.

## Report a vulnerability privately

Report issues that may involve unauthorized reads, disclosure of task content, code execution, or other security boundaries through the [GitHub private vulnerability reporting form](https://github.com/sirius0414/codex-trace/security/advisories/new). Maintainers will follow up within the private report. Do not post vulnerability details, reproduction payloads, or unpublished fixes in public issues, pull requests, or discussions.

This form requires private vulnerability reporting to be enabled on a public repository. If the repository is private, the form is unavailable, or there is no **Report a vulnerability** button, use an existing private channel with the maintainers or wait for one to become available. Do not submit sensitive details in a public issue. Before public release, maintainers should enable the feature using the [GitHub configuration instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) and check the form as an ordinary visitor. This file does not enable any GitHub settings.

Where possible, include:

- The affected version or commit SHA, operating system, and Python/Codex versions.
- Trigger conditions, minimal reproduction steps, expected behavior, and actual behavior.
- Data or permissions that may be affected, and the confirmed scope of impact.
- A minimal example using synthetic data, along with any proposed fix.

Do not upload real task traces, complete export ZIPs, session logs, access tokens, API keys, or other people's personal information. Even in a private report, prefer synthetic data and a reviewed, minimal error summary. If credentials have already been exposed, have their owner revoke or rotate them first; do not paste their values again in the report.

Maintainers will assess the issue and coordinate a fix and public disclosure within the private report. Response and resolution times depend on the actual impact and available maintenance resources; no fixed time frame is promised. Coordinate with maintainers before public disclosure to avoid exposing sensitive information belonging to users who have not yet received a fix.

## Ordinary issues

Use [GitHub Issues](https://github.com/sirius0414/codex-trace/issues) for feature issues that do not involve security or privacy. Before submitting screenshots or error summaries, check them for task bodies, local paths, email addresses, and credentials, and remove real private content.
