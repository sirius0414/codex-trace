"""开发浏览器入口的 HTTP 边界与真实 MCP 转发；全部任务和导出均为临时合成数据。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4
import zipfile

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # 开发脚本按 CLI 布局复用同级打包和运行环境发现模块。
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


development = load_script("dev_browser")
verification = load_script("verify_install")


class DevBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-trace-browser-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "app.js").write_text('document.getElementById("root").textContent="synthetic UI";')
        (self.assets / "style.css").write_text("body{margin:0}")
        codex, self.source, self.methods = verification.synthetic_history(self.root, sys.executable)
        self.export_root = self.root / "Downloads/Codex Trace"
        self.app = development.create_app(Path(sys.executable), codex, 8765,
            assets=self.assets, state_root=self.root / "index", export_root=self.export_root,
            home=self.root / "codex-home")

    def client(self):
        return TestClient(self.app, base_url="http://localhost:8765")

    def test_shutdown_requires_separate_capability_and_preserves_origin_boundary(self):
        stopped = []
        app = development.create_app(Path(sys.executable), Path(sys.executable), 8765,
            shutdown_token="separate-shutdown-capability", shutdown_callback=lambda: stopped.append(True))
        # 这些路由不需要启动 MCP lifespan，单独验证退出能力与同源边界。
        client = TestClient(app, base_url="http://localhost:8765")
        self.assertEqual(client.post("/_dev/shutdown").status_code, 403)
        self.assertEqual(client.post("/_dev/shutdown", headers={
            "X-Codex-Trace-Shutdown": "separate-shutdown-capability", "Origin": "https://elsewhere.invalid"}).status_code, 403)
        self.assertFalse(stopped)
        self.assertEqual(client.post("/_dev/shutdown", headers={
            "X-Codex-Trace-Shutdown": "separate-shutdown-capability"}).status_code, 202)
        self.assertEqual(stopped, [True])

    def call(self, client, name, arguments):
        response = client.post("/rpc", json={"name": name, "arguments": arguments},
            headers={"X-Codex-Trace-Token": self.app.state.token, "Origin": "http://localhost:8765"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertFalse(result.get("isError"), result)
        self.assertNotIn(verification.SYNTHETIC_BODY, json.dumps(result.get("content")))
        self.assertIsNone(result.get("structuredContent"))
        return result["_meta"]["codex-trace/result"]

    def test_real_mcp_selection_trajectory_and_local_export(self):
        with self.client() as client:
            opened = self.call(client, "open_codex_trace", {})
            second = self.call(client, "open_codex_trace", {})
            self.assertEqual(opened["identity"]["state"], "selectionRequired")
            self.assertNotEqual(opened["viewSessionId"], second["viewSessionId"])
            listing = self.call(client, "list_threads", {"viewSessionId": opened["viewSessionId"],
                "query": "", "sourceScope": "user", "cursor": None})
            self.assertEqual([row["threadId"] for row in listing["threads"]], [verification.SYNTHETIC_THREAD])
            binding = {"schemaVersion": "1.0.0", "sourceInstanceId": opened["sourceInstanceId"],
                "viewSessionId": opened["viewSessionId"], "selectionEpoch": 1,
                "threadId": verification.SYNTHETIC_THREAD}
            snapshot = self.call(client, "read_trajectory", {"binding": binding,
                "cursor": None, "anchorEventId": None})
            self.assertEqual(snapshot["binding"], binding)
            self.assertTrue(snapshot["events"])
            self.assertIn(verification.SYNTHETIC_BODY, json.dumps(snapshot))

            export = self.call(client, "download_records", {"action": "create",
                "binding": binding, "requestId": str(uuid4())})
            saved = self.call(client, "download_records", {"action": "commit",
                "binding": binding, "exportId": export["exportId"], "requestId": str(uuid4())})
            self.assertEqual(saved["save"]["status"], "verified")
            path = Path(saved["save"]["path"])
            self.assertEqual(path.parent, self.export_root)
            with zipfile.ZipFile(path) as archive:
                records = [name for name in archive.namelist() if name.startswith("records/")]
                self.assertEqual(len(records), 1)
                self.assertEqual(archive.read(records[0]), self.source.read_bytes())

            invalid = client.post("/rpc", json={"name": "read_trajectory", "arguments": {
                "binding": binding, "cursor": None, "anchorEventId": None,
                "forbidden": verification.SYNTHETIC_BODY}},
                headers={"X-Codex-Trace-Token": self.app.state.token})
            self.assertEqual(invalid.status_code, 200)
            self.assertTrue(invalid.json()["isError"])
            self.assertNotIn(verification.SYNTHETIC_BODY, invalid.text)
        observed = {json.loads(line) for line in self.methods.read_text().splitlines()}
        self.assertTrue(observed)
        self.assertLessEqual(observed, verification.READ_METHODS)

    def test_http_access_requires_local_host_origin_and_startup_token(self):
        request = {"name": "open_codex_trace", "arguments": {}}
        token = {"X-Codex-Trace-Token": self.app.state.token}
        with self.client() as client:
            self.assertEqual(client.post("/rpc", json=request).status_code, 403)
            self.assertEqual(client.post("/rpc", json=request,
                headers={"X-Codex-Trace-Token": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/rpc", json=request,
                headers={**token, "Origin": "https://example.com"}).status_code, 403)
            self.assertEqual(client.post("/rpc", json=request,
                headers={**token, "Host": "example.com:8765"}).status_code, 403)
            self.assertEqual(client.get("/", headers={"Host": "example.com:8765"}).status_code, 403)
            self.assertEqual(client.post("/rpc", content=json.dumps(request),
                headers={**token, "Content-Type": "text/plain"}).status_code, 415)
            self.assertEqual(client.post("/rpc", content="x" * (1024 * 1024 + 1),
                headers={**token, "Content-Type": "application/json"}).status_code, 413)
            self.assertEqual(client.post("/rpc", content="{",
                headers={**token, "Content-Type": "application/json"}).status_code, 400)
            for payload in (None, [], {"name": "open_codex_trace"},
                    {"name": [], "arguments": {}}, {"name": "open_codex_trace", "arguments": []},
                    {**request, "meta": {"threadId": verification.SYNTHETIC_THREAD}}):
                with self.subTest(payload=payload):
                    self.assertEqual(client.post("/rpc", content=json.dumps(payload),
                        headers={**token, "Content-Type": "application/json"}).status_code, 400)
            for name in ("thread/start", "__class__", "open_codex_trace_panel"):
                with self.subTest(name=name):
                    self.assertEqual(client.post("/rpc", json={"name": name, "arguments": {}},
                        headers=token).status_code, 404)

    def test_only_explicit_assets_are_served_and_ui_rebuild_is_visible(self):
        with self.client() as client:
            health = client.get("/_dev/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["service"], "codex-trace-dev-browser")
            self.assertIsInstance(health.json()["pid"], int)
            self.assertTrue(health.json()["instanceId"])
            self.assertNotIn(self.app.state.token, health.text)
            self.assertFalse(self.methods.exists())  # 健康检查不启动历史读取进程。
            host = client.get("/")
            self.assertEqual(host.status_code, 200)
            self.assertIn(self.app.state.token, host.text)
            self.assertNotIn("__CODEX_TRACE_DEV_TOKEN__", host.text)
            self.assertEqual(client.get("/host.js").status_code, 200)
            first = client.get("/app.html")
            self.assertEqual(first.status_code, 200)
            self.assertIn("synthetic UI", first.text)
            self.assertIn("no-store", first.headers["cache-control"])
            (self.assets / "app.js").write_text('document.getElementById("root").textContent="rebuilt UI";')
            self.assertIn("rebuilt UI", client.get("/app.html").text)
            for path in ("/backend/codex_trace/app.py", "/contracts/tools.json", "/runtime.json", "/.env"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
