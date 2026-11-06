"""Exercise the actual Python MCP SDK over stdio with synthetic data only."""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from uuid import uuid4
import zipfile
from platform_fixtures import assert_private_mode

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import AnyUrl


ROOT = Path(__file__).resolve().parents[1]
URI = "ui://codex-trace/trajectory-v1.html"
PANEL_URI = "ui://codex-trace/trajectory-panel-v1.html"
THREAD_A = "11111111-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"
SECRET = "SYNTHETIC_PRIVATE_VALUE_MUST_NOT_BE_EXPORTED"
HTML = '<!doctype html><html><head><style>body{margin:0}</style></head><body>M0 fixture</body></html>'


class ServerTests(unittest.TestCase):
    def run_session(self, scenario):
        with tempfile.TemporaryDirectory(prefix="codex-trace-sdk-test-") as temporary:
            root = Path(temporary).resolve()
            ui = root / "ui.html"
            ui.write_text(HTML)
            audit = root / "diagnostics" / "audit.jsonl"
            home = root / "synthetic-home"
            sessions = home / "sessions"
            sessions.mkdir(parents=True)
            source = sessions / "fixture.jsonl"
            source.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": THREAD_A, "history_mode": "paginated", "base_instructions": SECRET,
            }}) + "\n" + json.dumps({"type": "unknown", "payload": SECRET}) + "\n")
            # A temporary fake app-server answers only initialization and a
            # single metadata read. The real Codex executable is never used.
            executable = root / "fake-codex.py"
            executable.write_text(textwrap.dedent(f'''\
                import json, sys
                from pathlib import Path
                Path({str(root / 'history-invoked')!r}).write_text("synthetic history started")
                for line in sys.stdin:
                    request = json.loads(line)
                    method = request.get("method")
                    if method == "initialized":
                        continue
                    if method == "initialize":
                        result = {{"userAgent": "synthetic"}}
                    elif method == "thread/read" and request["params"] == {{"threadId": {THREAD_A!r}, "includeTurns": False}}:
                        result = {{"thread": {{"id": {THREAD_A!r}, "name": "Synthetic SDK task", "path": {str(source)!r}, "preview": {SECRET!r}}}}}
                    else:
                        print(json.dumps({{"id": request["id"], "error": {{"code": -32601, "message": "denied"}}}}), flush=True)
                        continue
                    print(json.dumps({{"id": request["id"], "result": result}}), flush=True)
            '''))
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "codex_trace.server"], cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "backend"), "PYTHONDONTWRITEBYTECODE": "1",
                     "CODEX_TRACE_UI_PATH": str(ui), "CODEX_TRACE_AUDIT_PATH": str(audit),
                     "CODEX_TRACE_EXPORT_ROOT": str(root / "exports"),
                     "CODEX_TRACE_CODEX_EXECUTABLE": str(executable), "CODEX_HOME": str(home)},
            )

            async def run():
                with (root / "stderr.txt").open("w+") as errors:
                    async with stdio_client(params, errlog=errors) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                            self.server_version = (await session.initialize()).serverInfo.version
                            await scenario(session, root, audit)
                    errors.flush()
                    errors.seek(0)
                    self.assertNotIn(SECRET, errors.read())

            asyncio.run(run())

    def test_app_only_tools_and_separate_global_and_thread_entrypoints(self):
        async def scenario(session, *_):
            listing = await session.list_tools()
            tools = {tool.name: tool for tool in listing.tools}
            self.assertEqual(set(tools), {"open_codex_trace", "open_codex_trace_panel", "probe_host_observation", "probe_diagnostic_event", "probe_thread_identity", "probe_local_download", "probe_export_records"})
            self.assertFalse(tools["probe_export_records"].annotations.readOnlyHint)
            for tool in tools.values():
                self.assertEqual(tool.meta["ui"]["visibility"], ["app"])
                self.assertEqual(tool.meta["openai/visibility"], "private")
            entrypoint = tools["open_codex_trace"]
            self.assertEqual(entrypoint.meta["openai/ui"]["entrypoints"], [{"type": "global"}])
            self.assertEqual(entrypoint.meta["ui"]["resourceUri"], URI)
            self.assertTrue(entrypoint.annotations.readOnlyHint)
            panel = tools["open_codex_trace_panel"]
            self.assertEqual(panel.title, "Codex Trace")
            self.assertEqual(panel.meta["openai/ui"]["entrypoints"], [{"type": "thread"}])
            self.assertEqual(panel.meta["ui"]["resourceUri"], PANEL_URI)
            self.assertNotEqual(panel.meta["ui"]["resourceUri"], entrypoint.meta["ui"]["resourceUri"])
            self.assertTrue(panel.annotations.readOnlyHint)
            self.assertEqual(panel.inputSchema["properties"], {})
            self.assertFalse(panel.inputSchema["additionalProperties"])
        self.run_session(scenario)

    def test_each_request_has_its_own_identity_and_private_metadata_is_not_audited(self):
        async def scenario(session, root, audit):
            cases = [
                ({"thread_id": THREAD_A, "privateValue": SECRET}, THREAD_A, "unverified-global-semantics"),
                ({"threadId": THREAD_B, "nested": {"privateValue": SECRET}}, THREAD_B, "unverified-global-semantics"),
                ({"openai/session": THREAD_A, "privateValue": SECRET}, None, "missing-identity"),
                ({"thread_id": THREAD_A, "threadId": THREAD_B}, None, "conflicting-identities"),
                ({"thread_id": "invalid-" + SECRET}, None, "invalid-identity"),
                ({}, None, "missing-identity"),
            ]
            activation_ids = []
            server_ids = []
            for metadata, expected_id, reason in cases:
                reply = await session.call_tool("open_codex_trace", {}, meta=metadata)
                self.assertFalse(reply.isError)
                result = reply.structuredContent
                self.assertEqual(result["candidateThreadId"], expected_id)
                self.assertEqual(result["identityReason"], reason)
                self.assertEqual(result["entrypoint"], "global")
                self.assertEqual(result["serverVersion"], self.server_version)
                self.assertFalse(result["automaticIdentityVerified"])
                self.assertEqual(reply.meta["ui"]["resourceUri"], URI)
                activation_ids.append(result["activationId"])
                server_ids.append(result["serverInstanceId"])
                self.assertNotIn(SECRET, reply.model_dump_json())
            self.assertEqual(len(set(activation_ids)), len(cases))
            self.assertEqual(len(set(server_ids)), 1)
            observation = await session.call_tool("probe_host_observation", {
                "activationId": activation_ids[-1], "uiInstanceId": str(uuid4()),
                "contextKeys": ["theme", "displayMode"], "displayMode": "fullscreen",
                "hostCapabilityKeys": ["serverTools", "downloadFile"],
            })
            self.assertFalse(observation.isError)
            self.assertTrue(observation.structuredContent["recorded"])
            self.assertEqual(observation.structuredContent["hostCapabilityKeys"], ["serverTools", "downloadFile"])
            written = audit.read_text()
            self.assertNotIn(SECRET, written)
            self.assertIn('"privateValue"', written)  # key name only, never its value
            assert_private_mode(self, audit, 0o600)
        self.run_session(scenario)

    def test_opening_surface_is_selected_by_tool_name_and_never_reads_history(self):
        async def scenario(session, root, audit):
            for name, surface, uri in (("open_codex_trace", "global", URI),
                                       ("open_codex_trace_panel", "thread", PANEL_URI)):
                opposite = "thread" if surface == "global" else "global"
                reply = await session.call_tool(name, {}, meta={"thread_id": THREAD_A, "entrypoint": opposite,
                                                                "serverVersion": "fake", "privateValue": SECRET})
                self.assertFalse(reply.isError)
                result = reply.structuredContent
                self.assertEqual(result["entrypoint"], surface)
                self.assertEqual(result["serverVersion"], self.server_version)
                self.assertEqual(result["candidateThreadId"], THREAD_A)
                self.assertEqual(result["identityReason"], f"unverified-{surface}-semantics")
                self.assertFalse(result["automaticIdentityVerified"])
                self.assertEqual(reply.meta["ui"]["resourceUri"], uri)
                self.assertNotIn(SECRET, reply.model_dump_json())
                baseline = audit.read_text()
                invalid = await session.call_tool(name, {"entrypoint": opposite})
                self.assertTrue(invalid.isError)
                self.assertEqual(audit.read_text(), baseline)
            self.assertFalse((root / "history-invoked").exists())
            rows = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual([row["entrypoint"] for row in rows], ["global", "thread"])
            self.assertTrue(all(row["serverVersion"] == self.server_version for row in rows))
            self.assertTrue(all(row["kind"] == "activation" for row in rows))
            self.assertNotIn(SECRET, audit.read_text())
        self.run_session(scenario)

    def test_sdk_resource_read_preserves_html_mime_and_csp(self):
        async def scenario(session, *_):
            resources = await session.list_resources()
            self.assertEqual(len(resources.resources), 2)
            self.assertEqual({str(resource.uri) for resource in resources.resources}, {URI, PANEL_URI})
            for uri in (URI, PANEL_URI):
                reply = await session.read_resource(AnyUrl(uri))
                self.assertEqual(len(reply.contents), 1)
                content = reply.contents[0]
                self.assertEqual(str(content.uri), uri)
                self.assertEqual(content.mimeType, "text/html;profile=mcp-app")
                self.assertEqual(content.text, HTML)
                self.assertEqual(content.meta["ui"]["csp"], {"connectDomains": [], "resourceDomains": []})
                self.assertNotIn("displayModes", content.meta["ui"])
                self.assertEqual(content.meta["openai/widgetCSP"], {"connect_domains": [], "resource_domains": []})
        self.run_session(scenario)

    def test_download_hash_and_raw_fixture_bytes_roundtrip(self):
        async def scenario(session, *_):
            opened = (await session.call_tool("open_codex_trace", {})).structuredContent
            downloaded = await session.call_tool("probe_local_download", {"offset": 0})
            self.assertFalse(downloaded.isError)
            result = downloaded.structuredContent
            data = base64.b64decode(result["data"], validate=True)
            self.assertEqual(hashlib.sha256(data).hexdigest(), result["chunkSha256"])
            self.assertEqual(result["sha256"], opened["downloadExpectedSha256"])
            self.assertEqual(result["totalBytes"], opened["downloadExpectedBytes"])
            self.assertEqual(result["nextOffset"], len(data))
            self.assertTrue(result["done"])
            retry = (await session.call_tool("probe_local_download", {"offset": 0})).structuredContent
            self.assertEqual(retry, result)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                raw = archive.read("records/synthetic.jsonl")
                manifest = json.loads(archive.read("manifest.json"))
                self.assertIn(b"\r\n\xffbad\n", raw)
                self.assertTrue(raw.endswith(b'{"partial":'))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["sha256"])
                self.assertFalse(manifest["realTaskRecords"])
            empty = (await session.call_tool("probe_local_download", {"offset": len(data)})).structuredContent
            self.assertEqual(empty["data"], "")
            self.assertTrue(empty["done"])
            invalid = await session.call_tool("probe_local_download", {"offset": len(data) + 1})
            self.assertTrue(invalid.isError)
        self.run_session(scenario)

    def test_local_export_is_verified_private_and_idempotent_without_history_reads(self):
        async def scenario(session, root, audit):
            opened = (await session.call_tool("open_codex_trace_panel", {}, meta={"thread_id": THREAD_A})).structuredContent
            arguments = {"activationId": opened["activationId"], "uiInstanceId": str(uuid4()), "requestId": str(uuid4())}
            reply = await session.call_tool("probe_export_records", arguments)
            self.assertFalse(reply.isError)
            self.assertIsNone(reply.structuredContent)
            result = reply.meta["codex-trace/result"]
            self.assertEqual(result["kind"], "local-export")
            self.assertEqual(result["requestId"], arguments["requestId"])
            self.assertTrue(result["verified"])
            self.assertTrue(result["synthetic"])
            saved = Path(result["path"])
            self.assertEqual(saved.parent, root / "exports")
            self.assertEqual(saved.name, result["filename"])
            self.assertEqual(len(saved.read_bytes()), opened["downloadExpectedBytes"])
            self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(), opened["downloadExpectedSha256"])
            self.assertNotIn(result["path"], json.dumps([item.model_dump() for item in reply.content]))
            retry = await session.call_tool("probe_export_records", arguments)
            self.assertEqual(retry.meta["codex-trace/result"], result)
            self.assertEqual(list(saved.parent.iterdir()), [saved])
            invalid = await session.call_tool("probe_export_records", {**arguments, "path": str(root / "unapproved")})
            self.assertTrue(invalid.isError)
            wrong_instance = await session.call_tool("probe_export_records", {**arguments, "uiInstanceId": str(uuid4())})
            self.assertTrue(wrong_instance.isError)
            self.assertFalse((root / "history-invoked").exists())
            records = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual(sum(row["kind"] == "export-saved" for row in records), 1)
            self.assertNotIn(result["path"], audit.read_text())
        self.run_session(scenario)

    def test_diagnostic_tool_is_bounded_and_schema_errors_do_not_echo_private_values(self):
        async def scenario(session, root, audit):
            opened = (await session.call_tool("open_codex_trace", {})).structuredContent
            required = {"activationId": opened["activationId"], "uiInstanceId": str(uuid4()), "phase": "page-ready"}
            valid = await session.call_tool("probe_diagnostic_event", {
                **required, "code": "save-requested", "elapsedMs": 0, "visibility": "visible",
                "contextKeys": ["theme", "displayMode"], "hostCapabilityKeys": ["serverTools", "downloadFile"],
            })
            self.assertFalse(valid.isError)
            self.assertTrue(valid.structuredContent["recorded"])
            self.assertEqual(valid.structuredContent["elapsedMs"], 0)
            baseline = audit.read_text()
            for invalid in (
                {"phase": SECRET}, {"code": SECRET}, {"message": SECRET},
                {"contextKeys": [{"payload": SECRET}]}, {"hostCapabilityKeys": ["body " + SECRET]},
                {"contextKeys": ["field"] * 101}, {"elapsedMs": -1}, {"elapsedMs": True},
                {"elapsedMs": 3_600_001}, {"visibility": SECRET}, {"activationId": str(uuid4())},
                {"uiInstanceId": SECRET},
            ):
                with self.subTest(invalid=invalid):
                    reply = await session.call_tool("probe_diagnostic_event", {**required, **invalid})
                    self.assertTrue(reply.isError)
                    self.assertNotIn(SECRET, reply.model_dump_json())
                    self.assertEqual(audit.read_text(), baseline)
            self.assertNotIn(SECRET, audit.read_text())
        self.run_session(scenario)

    def test_actual_download_reads_emit_only_byte_counts_and_hashes(self):
        async def scenario(session, root, audit):
            result = (await session.call_tool("probe_local_download", {"offset": 0})).structuredContent
            await session.call_tool("probe_local_download", {"offset": 0})
            await session.call_tool("probe_local_download", {"offset": result["totalBytes"]})
            rows = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertEqual([row["bytes"] for row in rows], [result["totalBytes"], result["totalBytes"], 0])
            self.assertTrue(all(row["kind"] == "download-read" for row in rows))
            self.assertTrue(all(set(row) == {"atMs", "kind", "offset", "bytes", "sha256", "chunkSha256"} for row in rows))
            self.assertNotIn(result["data"], audit.read_text())
        self.run_session(scenario)

    def test_internal_diagnostic_failure_returns_no_original_exception_message(self):
        sys.path.insert(0, str(ROOT / "backend"))
        from codex_trace import server as module
        with mock.patch.object(module.service, "audit", side_effect=OSError(SECRET)):
            reply = asyncio.run(module.call_tool("probe_local_download", {"offset": 0}))
        self.assertTrue(reply.isError)
        self.assertNotIn(SECRET, reply.model_dump_json())
        self.assertIsNone(reply.structuredContent)

    def test_explicit_identity_uses_only_fake_history_and_source_body_never_escapes(self):
        async def scenario(session, root, audit):
            before = (root / "synthetic-home" / "sessions" / "fixture.jsonl").read_bytes()
            reply = await session.call_tool("probe_thread_identity", {"threadId": THREAD_A})
            self.assertFalse(reply.isError, reply.model_dump_json())
            result = reply.structuredContent
            self.assertEqual(result["threadId"], THREAD_A)
            self.assertTrue(result["identityMatches"])
            self.assertEqual(result["title"], "Synthetic SDK task")
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["source"]["readable"])
            self.assertNotIn(SECRET, reply.model_dump_json())
            self.assertNotIn(SECRET, audit.read_text())
            self.assertEqual(before, (root / "synthetic-home" / "sessions" / "fixture.jsonl").read_bytes())
        self.run_session(scenario)


if __name__ == "__main__":
    unittest.main()
