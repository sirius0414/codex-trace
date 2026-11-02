#!/usr/bin/env python3
"""Verify a production package using an isolated cache and synthetic task data.

Never installs to the user's Codex home, opens a task, or calls a task mutation.
Synthetic export calls write only to this test's temporary directory.
"""
from __future__ import annotations

import argparse
import base64
import io
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import queue
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
import zipfile

from third_party_licenses import verify_documents


class HtmlAssetCheck(HTMLParser):
    """Inspect actual resource tags, not React/SDK strings embedded in scripts."""
    def __init__(self):
        super().__init__()
        self.remote_tags = []
        self.scripts = 0
        self.styles = 0
        self.root = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.scripts += tag == "script"
        self.styles += tag == "style"
        self.root = self.root or (tag == "div" and attrs.get("id") == "root")
        if (tag == "script" and "src" in attrs) or tag == "link" or (tag in ("img", "iframe", "video", "audio", "source") and "src" in attrs):
            self.remote_tags.append(tag)

    @property
    def passed(self):
        return self.root and self.scripts == 1 and self.styles == 1 and not self.remote_tags


class RpcClient:
    """串行发送探测请求，后台排空 stdout/stderr，避免子进程因管道写满卡住。"""
    def __init__(self, command: list[str], cwd: Path, env: dict[str, str]):
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, encoding="utf-8")
        self.incoming: queue.Queue = queue.Queue()
        self.stderr_contains_synthetic_body = False
        self.stderr_lines = 0
        self.readers = [threading.Thread(target=self._read, daemon=True), threading.Thread(target=self._stderr, daemon=True)]
        for reader in self.readers:
            reader.start()
        self.sequence = 0

    def _read(self):
        for line in self.process.stdout:
            try:
                self.incoming.put(json.loads(line))
            except json.JSONDecodeError:
                self.incoming.put({"protocolError": "non-JSON stdout"})

    def _stderr(self):
        for line in self.process.stderr:
            self.stderr_contains_synthetic_body |= SYNTHETIC_BODY in line
            self.stderr_lines += 1

    def send(self, method: str, params: dict, request_id: int | None = None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 15) -> dict:
        """仅接受当前请求 ID 的响应；服务端反向请求一律返回只读探测错误。"""
        self.sequence += 1
        request_id = self.sequence
        self.send(method, params, request_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.incoming.get(timeout=min(0.25, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(f"MCP process exited with {self.process.returncode}")
                continue
            if message.get("protocolError"):
                raise RuntimeError(message["protocolError"])
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"MCP error: {message['error']}")
                return message.get("result", {})
            if "id" in message and "method" in message:
                # This observer does not execute or approve server requests.
                response = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Read-only installation probe"}}
                self.process.stdin.write(json.dumps(response) + "\n")
                self.process.stdin.flush()
        raise TimeoutError(f"Timed out waiting for {method}")

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        for reader in self.readers:
            reader.join(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


SYNTHETIC_THREAD = "11111111-1111-4111-8111-111111111111"
SYNTHETIC_BODY = "PRIVATE_SYNTHETIC_INSTALL_CONTENT"
READ_METHODS = {"initialize", "thread/list", "thread/read", "thread/turns/list", "thread/items/list"}


def synthetic_history(root: Path, python: str) -> tuple[Path, Path, Path]:
    """Create a standalone fake read-only app-server; never open the user's data."""
    source = root / "codex-home/sessions" / f"rollout-2026-10-23T00-00-00-{SYNTHETIC_THREAD}.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes((json.dumps({"type": "session_meta", "payload": {"id": SYNTHETIC_THREAD}}) + "\n"
                       + json.dumps({"type": "unknown_future_record", "payload": SYNTHETIC_BODY})
                       + '\n{"type":"unfinished","text":"').encode() + b'\xe4\xb8')
    methods = root / "history-methods.jsonl"
    implementation = root / "synthetic-history.py"
    implementation.write_text(f'''import json,sys
from pathlib import Path
thread={{"id":{SYNTHETIC_THREAD!r},"name":"Synthetic installation task","source":"appServer","path":{str(source)!r}}}
for line in sys.stdin:
 r=json.loads(line); m=r.get("method")
 if "id" not in r: continue
 with Path({str(methods)!r}).open("a") as log: log.write(json.dumps(m)+"\\n")
 if m=="initialize": v={{}}
 elif m=="thread/read": v={{"thread":thread}} if r["params"].get("threadId")==thread["id"] else None
 elif m=="thread/list": v={{"data":[thread],"nextCursor":None}}
 elif m=="thread/items/list": v={{"data":[{{"turnId":"synthetic-turn","item":{{"type":"userMessage","id":"synthetic-message","content":[{{"type":"text","text":{SYNTHETIC_BODY!r}}}]}},"startedAtMs":1000,"completedAtMs":1000}}],"nextCursor":None,"backwardsCursor":None}}
 elif m=="thread/turns/list": v={{"data":[{{"id":"synthetic-turn","status":"completed","items":[]}}],"nextCursor":None,"backwardsCursor":None}}
 else: v=None
 if v is None: reply={{"id":r["id"],"error":{{"code":-32601,"message":"Read-only synthetic history"}}}}
 else: reply={{"id":r["id"],"result":v}}
 print(json.dumps(reply),flush=True)
''', encoding="utf-8")
    if sys.platform == "win32":
        # 正式管道适配器通过当前解释器运行 .py；无 shell 批处理，也不需要编译器。
        return implementation, source, methods
    launcher = root / "synthetic-codex"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(python) + " " + shlex.quote(str(implementation)) + ' "$@"\n', encoding="utf-8")
    launcher.chmod(0o700)
    return launcher, source, methods


def verify_cache(installed: Path, root: Path, checks: list[dict]) -> dict:
    """Exercise the real cache-local app/SDK against only the fake app-server."""
    from jsonschema import Draft202012Validator, FormatChecker

    manifest = json.loads((installed / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    launch = json.loads((installed / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["codex-trace"]
    cwd = (installed / launch.get("cwd", ".")).resolve()
    def check(operation, passed):
        checks.append({"operation": operation, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(operation + " failed")
    check("cache_copy_backend_exists", (installed / "backend/codex_trace/app.py").is_file())
    check("relative_cwd_resolves_inside_cache", cwd == installed / "backend")
    check("absolute_python_and_app_server", Path(launch["command"]).is_absolute()
          and Path(launch["env"]["CODEX_TRACE_CODEX_EXECUTABLE"]).is_absolute())
    check("module_entrypoint", launch["args"] == ["-m", "codex_trace.app"])
    runtime = json.loads((installed / "runtime.json").read_text(encoding="utf-8"))
    legal_manifest = (installed / "third-party-manifest.json").read_bytes()
    check("third_party_manifest_matches_checksum", runtime.get("thirdPartyManifestSha256") == hashlib.sha256(legal_manifest).hexdigest())
    verify_documents(installed, json.loads(legal_manifest))
    check("third_party_license_documents_verified", True)
    check("project_license_and_notice_present", all((installed / name).is_file() and (installed / name).read_bytes().strip() for name in ("LICENSE", "NOTICE")))
    check("runtime_matches_current_platform", runtime.get("platform", sys.platform) == sys.platform)
    check("runtime_paths_match_manifest", runtime.get("runtimePython") == launch["command"]
          and runtime.get("appServerExecutable") == launch["env"]["CODEX_TRACE_CODEX_EXECUTABLE"])
    check("startup_never_installs_dependencies", runtime.get("startupNetworkInstallation") is False
          and runtime.get("runtimeBundled") is False and runtime.get("entrypoint") == "codex_trace.app")
    fake, source, methods = synthetic_history(root, launch["command"])
    (root / "Downloads").mkdir(mode=0o700)
    original_source = source.read_bytes()
    source_before = source.stat()
    # Override every local-data path after applying the package environment.
    # Do not override HOME: only documented constructor configuration is used.
    runtime_env = {**os.environ, **launch.get("env", {}), "CODEX_HOME": str(root / "codex-home"),
                   "CODEX_TRACE_CODEX_EXECUTABLE": str(fake), "CODEX_TRACE_STATE_ROOT": str(root / "index"),
                   "CODEX_TRACE_EXPORT_ROOT": str(root / "Downloads/Codex Trace"), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("PYTHONPATH", "PYTHONHOME", "CODEX_TRACE_UI_PATH", "CODEX_TRACE_AUDIT_PATH"):
        runtime_env.pop(key, None)
    module = subprocess.run([launch["command"], "-c", "import codex_trace.app;print(codex_trace.app.__file__)"],
                            cwd=cwd, env=runtime_env, capture_output=True, text=True, timeout=15, check=True, encoding="utf-8")
    check("imports_backend_from_installed_cache", Path(module.stdout.strip()).resolve() == installed / "backend/codex_trace/app.py")
    contract = json.loads((installed / "contracts/trajectory.schema.json").read_text(encoding="utf-8"))
    tool_contracts = json.loads((installed / "contracts/tools.json").read_text(encoding="utf-8"))["tools"]
    client = RpcClient([launch["command"], *launch["args"]], cwd, runtime_env)
    try:
        initialized = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "codex-trace-install-smoke", "version": "0.1.0"}})
        check("installed_mcp_initialize", initialized.get("serverInfo", {}).get("name") == "codex-trace")
        check("server_matches_package_version", initialized["serverInfo"].get("version") == manifest["version"])
        client.send("notifications/initialized", {})
        tools = client.request("tools/list", {}).get("tools", [])
        check("production_tool_allowlist", {t["name"] for t in tools} == set(tool_contracts) | {"open_codex_trace_panel"})
        check("app_only_tool_visibility", all(t.get("_meta", {}).get("ui", {}).get("visibility") == ["app"]
              and t.get("_meta", {}).get("openai/visibility") == "private" for t in tools))
        resources = client.request("resources/list", {}).get("resources", [])
        bundled = (installed / "ui/trajectory-v1.html").read_text(encoding="utf-8")
        uris = {"global": "ui://codex-trace/trajectory-v1.html", "thread": "ui://codex-trace/trajectory-panel-v1.html"}
        expected_icon = "data:image/svg+xml;base64," + base64.b64encode((installed / "assets/icon.svg").read_bytes()).decode("ascii")
        for surface, name in (("global", "open_codex_trace"), ("thread", "open_codex_trace_panel")):
            tool = next(t for t in tools if t["name"] == name)
            metadata = tool["_meta"]
            check(surface + "_entrypoint_icon_embedded", tool.get("icons") == [
                {"src": expected_icon, "mimeType": "image/svg+xml", "sizes": ["any"]}])
            check(surface + "_app_entrypoint", metadata.get("openai/ui", {}).get("entrypoints") == [{"type": surface}]
                  and metadata.get("ui", {}).get("resourceUri") == uris[surface])
            check(surface + "_ui_resource_registered", any(r.get("uri") == uris[surface] for r in resources))
            contents = client.request("resources/read", {"uri": uris[surface]}).get("contents", [])
            html = next((c.get("text") for c in contents if c.get("uri") == uris[surface]), None)
            check(surface + "_ui_served_from_cache", html == bundled and len(bundled) > 100)
        assets = HtmlAssetCheck(); assets.feed(bundled)
        check("single_offline_html", assets.passed)
        check("bundled_assets_match_build_checksums", runtime.get("uiSha256") == hashlib.sha256(bundled.encode()).hexdigest()
              and runtime.get("requirementsSha256") == hashlib.sha256((installed / "requirements.lock").read_bytes()).hexdigest())

        called = set()
        def call(name, arguments, meta=None, *, error=False):
            # 每次工具调用同时检查公开信封与私有契约，防止正文泄漏被功能成功掩盖。
            params = {"name": name, "arguments": arguments}
            if meta is not None: params["_meta"] = meta
            result = client.request("tools/call", params)
            if bool(result.get("isError")) != error:
                raise RuntimeError(name + " returned an unexpected error status")
            if result.get("structuredContent") is not None:
                raise RuntimeError(name + " exposed structuredContent")
            expected = "Local observation request could not be completed." if error else "Local observation result is available in the app."
            if result.get("content") != [{"type": "text", "text": expected}]:
                raise RuntimeError(name + " exposed non-static model-visible content")
            data = result.get("_meta", {}).get("codex-trace/result")
            definition = "ErrorResult" if error else tool_contracts["open_codex_trace" if name == "open_codex_trace_panel" else name]["output"]
            Draft202012Validator({**contract, "$ref": "#/$defs/" + definition}, format_checker=FormatChecker()).validate(data)
            called.add(name)
            return data

        global_open = call("open_codex_trace", {}, {"threadId": SYNTHETIC_THREAD})
        check("global_entry_requires_selection", global_open["identity"]["state"] == "selectionRequired")
        empty_open = call("open_codex_trace_panel", {})
        check("missing_identity_requires_selection", empty_open["identity"]["state"] == "selectionRequired")
        opened = call("open_codex_trace_panel", {}, {"threadId": SYNTHETIC_THREAD})
        check("thread_entry_verifies_synthetic_identity", opened["identity"]["state"] == "verified"
              and opened["identity"].get("threadId") == SYNTHETIC_THREAD)
        binding = {"schemaVersion": "1.0.0", "sourceInstanceId": opened["sourceInstanceId"],
                   "viewSessionId": opened["viewSessionId"], "selectionEpoch": 1, "threadId": SYNTHETIC_THREAD}
        listing = call("list_threads", {"viewSessionId": opened["viewSessionId"], "query": "", "sourceScope": "user", "cursor": None})
        check("synthetic_user_task_is_listed", any(t["threadId"] == SYNTHETIC_THREAD and t["source"] == "user" for t in listing["threads"]))
        snapshot = call("read_trajectory", {"binding": binding, "cursor": None, "anchorEventId": None})
        check("synthetic_body_stays_in_private_result", bool(snapshot["events"]) and SYNTHETIC_BODY in json.dumps(snapshot))
        preferences = call("read_view_state", {"binding": binding})
        call("read_event", {"binding": binding, "generation": snapshot["generation"],
             "eventId": snapshot["events"][0]["eventId"], "part": "input", "chunkCursor": None})
        queried = call("query_loaded_events", {"binding": binding, "loadedSetId": snapshot["coverage"]["loadedSetId"],
             "scopeRevision": snapshot["coverage"]["scopeRevision"], "filters": {**preferences["state"]["filters"], "text": SYNTHETIC_BODY},
             "agentScope": preferences["state"]["agentScope"]})
        check("synthetic_loaded_search_matches", snapshot["events"][0]["eventId"] in queried["eventIds"])
        call("read_changes", {"binding": binding, "generation": snapshot["generation"],
             "afterRevision": snapshot["revision"], "continuationCursor": None})
        preferences["state"]["follow"] = "paused"
        saved_preferences = call("save_view_state", {"binding": binding, "expectedPreferenceRevision": preferences["preferenceRevision"], "state": preferences["state"]})
        check("preferences_saved_in_isolated_store", saved_preferences["saved"] is True and (root / "index").is_dir())
        invalid = call("read_trajectory", {"binding": binding, "forbidden": SYNTHETIC_BODY}, error=True)
        check("invalid_request_does_not_echo_body", SYNTHETIC_BODY not in json.dumps(invalid))
        create_request = {"action": "create", "binding": binding, "requestId": str(uuid4())}
        export = call("download_records", create_request)
        check("synthetic_export_snapshot_ready", export["status"] == "ready" and export["save"]["status"] == "notRequested")
        duplicate = call("download_records", create_request)
        check("create_retry_reuses_frozen_snapshot", duplicate["exportId"] == export["exportId"])
        read_request = {"action": "read", "binding": binding, "exportId": export["exportId"], "cursor": None}
        first = call("download_records", read_request)
        repeated = call("download_records", read_request)
        check("chunk_retry_is_stable", repeated["chunk"] == first["chunk"])
        archive_bytes = bytearray()
        current = first
        for _ in range(16):
            chunk = current["chunk"]
            part = base64.b64decode(chunk["base64"], validate=True)
            if hashlib.sha256(part).hexdigest() != chunk["sha256"]:
                raise RuntimeError("chunk digest mismatch")
            archive_bytes.extend(part)
            if chunk["nextCursor"] is None: break
            current = call("download_records", {**read_request, "cursor": chunk["nextCursor"]})
        else:
            raise RuntimeError("synthetic export exceeded chunk limit")
        # 校验原始字节而非解析后的 JSON，覆盖未知类型、坏 UTF-8 和末尾半行。
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            records = [n for n in archive.namelist() if n.startswith("records/")]
            check("raw_unknown_and_partial_bytes_preserved", len(records) == 1 and archive.read(records[0]) == original_source)
        commit_request = {"action": "commit", "binding": binding, "exportId": export["exportId"], "requestId": str(uuid4())}
        committed = call("download_records", commit_request)
        receipt = committed["save"]
        saved = Path(receipt.get("path", ""))
        check("synthetic_export_saved_only_in_temporary_destination", receipt["status"] == "verified" and saved.parent == root / "Downloads/Codex Trace"
              and saved.is_file() and saved.read_bytes() == archive_bytes)
        # 从缓存后端检查权限：Windows 的 Path.stat() 不表达真实 owner SID/DACL。
        permissions = subprocess.run([launch["command"], "-c",
            "import sys;from codex_trace.platform import filesystem as fs;print(fs.stat(sys.argv[1],follow_symlinks=False).st_mode & 0o777)", str(saved)],
            cwd=cwd, env=runtime_env, capture_output=True, text=True, timeout=15, check=True, encoding="utf-8")
        check("synthetic_export_permissions_are_private", int(permissions.stdout.strip()) == 0o600)
        check("saved_bytes_match_receipt", receipt["bytes"] == len(archive_bytes) and receipt["sha256"] == hashlib.sha256(archive_bytes).hexdigest())
        retry = call("download_records", commit_request)
        check("commit_retry_reuses_verified_file", all(retry["save"].get(k) == receipt[k] for k in ("status", "requestId", "filename", "path", "bytes", "sha256")) and len(list((root / "Downloads/Codex Trace").glob("*.zip"))) == 1)
        result = call("download_records", {"action": "result", "binding": binding, "requestId": commit_request["requestId"]})
        check("saved_result_is_recoverable", all(result["save"].get(k) == receipt[k] for k in ("status", "requestId", "filename", "path", "bytes", "sha256")))
        call("download_records", {"action": "release", "binding": binding, "exportId": export["exportId"]})
        check("release_preserves_final_zip", saved.read_bytes() == archive_bytes)
        check("all_tool_results_are_private_and_schema_valid", called == {tool["name"] for tool in tools})
        summary = {"serverVersion": initialized["serverInfo"]["version"], "pluginVersion": manifest["version"],
                   "uiBytes": len(bundled.encode()), "uiSha256": hashlib.sha256(bundled.encode()).hexdigest(),
                   "entrypointResources": uris, "toolNames": [t["name"] for t in tools], "runtimeBoundToLocalInterpreter": True}
    finally:
        client.close()
    check("source_bytes_and_metadata_unchanged", source.read_bytes() == original_source
          and (source.stat().st_ino, source.stat().st_size, source.stat().st_mtime_ns) == (source_before.st_ino, source_before.st_size, source_before.st_mtime_ns))
    observed = {json.loads(line) for line in methods.read_text(encoding="utf-8").splitlines()}
    check("history_calls_are_read_only", bool(observed) and observed <= READ_METHODS)
    check("stderr_does_not_contain_task_body", not client.stderr_contains_synthetic_body)
    summary["historyMethods"] = sorted(observed)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-copy-only", action="store_true", help="Copy into a disposable cache without invoking Codex plugin installation; still exercise the complete production MCP app")
    args = parser.parse_args()
    checks = []
    report = {"generatedAt": datetime.now(timezone.utc).isoformat(), "isolatedHome": True,
              "desktopInstalled": False, "hostUiVerified": False, "taskDataRead": False, "syntheticTaskDataRead": True,
              "installationMode": "syntheticCacheCopy" if args.cache_copy_only else "isolatedCodexCli",
              "python": sys.version.split()[0], "checks": checks}
    try:
        package = args.package.resolve()
        manifest = json.loads((package / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        if manifest["name"] != "codex-trace": raise ValueError("Only a codex-trace package may be tested")
        with tempfile.TemporaryDirectory(prefix="codex-trace-install-") as temporary:
            root = Path(temporary).resolve()
            codex_home = root / "codex-home"
            codex_home.mkdir(mode=0o700)
            if args.cache_copy_only:
                # 删除安装源后再启动，确保服务和离线 UI 真正从缓存副本独立工作。
                installed = codex_home / "plugins/cache/synthetic/codex-trace" / manifest["version"]
                source = root / "input-package"
                shutil.copytree(package, source)
                shutil.copytree(source, installed)
                shutil.rmtree(source)
                checks.append({"operation": "copied_into_disposable_cache", "passed": True})
                checks.append({"operation": "install_source_removed_after_cache_copy", "passed": not source.exists()})
            else:
                market = root / "market"
                catalog = market / ".agents/plugins/marketplace.json"
                catalog.parent.mkdir(parents=True)
                source = market / "plugins/codex-trace"
                shutil.copytree(package, source)
                catalog.write_text(json.dumps({"name": "codex-trace-install-test", "interface": {"displayName": "Codex Trace Isolated Test"},
                    "plugins": [{"name": "codex-trace", "source": {"source": "local", "path": "./plugins/codex-trace"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Developer Tools"}]}), encoding="utf-8")
                env = {**os.environ, "CODEX_HOME": str(codex_home), "PYTHONDONTWRITEBYTECODE": "1"}
                env.pop("PYTHONPATH", None)
                report["codexVersion"] = subprocess.run([args.codex, "--version"], env=env, capture_output=True, text=True, timeout=15, check=True, encoding="utf-8").stdout.strip()
                for label, command in [
                    ("marketplace_registered_in_isolated_home", [args.codex, "plugin", "marketplace", "add", str(market)]),
                    ("plugin_installed_in_isolated_home", [args.codex, "plugin", "add", "codex-trace@codex-trace-install-test", "--json"]),
                ]:
                    run = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30, encoding="utf-8")
                    checks.append({"operation": label, "passed": run.returncode == 0, "exitCode": run.returncode})
                    if run.returncode: raise RuntimeError(label + " failed")
                manifests = [p for p in (codex_home / "plugins/cache").rglob(".codex-plugin/plugin.json") if json.loads(p.read_text(encoding="utf-8")).get("name") == "codex-trace"]
                if len(manifests) != 1: raise RuntimeError("Expected exactly one installed cache package")
                installed = manifests[0].parent.parent.resolve()
                shutil.rmtree(source)
                configured = subprocess.run([args.codex, "mcp", "list", "--json"], cwd=root, env=env, capture_output=True, text=True, timeout=15, check=True, encoding="utf-8")
                server = next((s for s in json.loads(configured.stdout) if s.get("name") == "codex-trace"), {})
                launch_cwd = server.get("transport", {}).get("cwd")
                checks.append({"operation": "cli_resolves_cwd_into_installed_cache", "passed": isinstance(launch_cwd, str) and Path(launch_cwd).resolve() == installed / "backend"})
            report.update(verify_cache(installed, root, checks))
    except Exception as error:
        # Diagnostics retain only fixed operation/error classes, never task data.
        checks.append({"operation": "smoke_completed", "passed": False, "errorType": type(error).__name__})
    report["passed"] = bool(checks) and all(check.get("passed") for check in checks)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
