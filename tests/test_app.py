"""Production SDK transport with synthetic history; bodies never escape UI metadata."""
import asyncio
import base64
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4
import io
import zipfile

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
THREAD = "11111111-1111-4111-8111-111111111111"
SECRET = "PRIVATE_SYNTHETIC_TEXT_ONLY_APP_MAY_READ"


class AppTests(unittest.TestCase):
    def test_production_read_and_error_envelopes_keep_bodies_private(self):
        with tempfile.TemporaryDirectory(prefix="codex-display-app-test-") as temp:
            root = Path(temp).resolve()
            source = root / "home/sessions" / f"rollout-2026-10-22T00-00-00-{THREAD}.jsonl"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD, "base_instructions": {"text": SECRET}}}) + "\n")
            executable = root / "fake-history"
            executable.write_text(f'''#!{sys.executable}
import json,sys
for line in sys.stdin:
 r=json.loads(line); m=r.get("method")
 if "id" not in r: continue
 if m=="initialize": v={{}}
 elif m=="thread/read": v={{"thread":{{"id":{THREAD!r},"name":"Synthetic","source":"appServer","path":{str(source)!r}}}}}
 elif m=="thread/items/list": v={{"data":[{{"turnId":"turn-1","item":{{"type":"userMessage","id":"message-1","content":[{{"type":"text","text":{SECRET!r}}}]}},"startedAtMs":1000,"completedAtMs":1000}}],"nextCursor":None,"backwardsCursor":None}}
 elif m=="thread/turns/list": v={{"data":[{{"id":"turn-1","status":"completed","items":[]}}],"nextCursor":None,"backwardsCursor":None}}
 elif m=="thread/list": v={{"data":[],"nextCursor":None}}
 else:
  print(json.dumps({{"id":r["id"],"error":{{"code":-32601,"message":"unsupported"}}}}),flush=True); continue
 print(json.dumps({{"id":r["id"],"result":v}}),flush=True)
''')
            executable.chmod(0o700)
            params = StdioServerParameters(command=sys.executable, args=["-m", "codex_display.app"], cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "backend"), "CODEX_HOME": str(root / "home"),
                     "CODEX_DISPLAY_STATE_ROOT": str(root / "index"), "CODEX_DISPLAY_CODEX_EXECUTABLE": str(executable)})

            async def run():
                with (root / "stderr.txt").open("w+") as err:
                    async with stdio_client(params, errlog=err) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=8)) as session:
                            await session.initialize()
                            listing = await session.list_tools()
                            self.assertEqual(len(listing.tools), 10)
                            self.assertTrue(all(t.meta["ui"]["visibility"] == ["app"] for t in listing.tools))
                            for tool in listing.tools:
                                if tool.name in ("open_codex_display", "open_codex_display_panel"):
                                    self.assertEqual(len(tool.icons), 1)
                                    icon = tool.icons[0]
                                    self.assertEqual(icon.mimeType, "image/svg+xml")
                                    prefix, data = icon.src.split(",", 1)
                                    self.assertEqual(prefix, "data:image/svg+xml;base64")
                                    self.assertEqual(base64.b64decode(data, validate=True),
                                                     (ROOT / "plugin/assets/icon.svg").read_bytes())
                                else:
                                    self.assertIsNone(tool.icons)
                            opened = await session.call_tool("open_codex_display_panel", {}, meta={"threadId": THREAD})
                            self.assertFalse(opened.isError)
                            view = opened.meta["codex-display/result"]
                            binding = {"schemaVersion": "1.0.0", "sourceInstanceId": view["sourceInstanceId"],
                                       "viewSessionId": view["viewSessionId"], "selectionEpoch": 1, "threadId": THREAD}
                            reply = await session.call_tool("read_trajectory", {"binding": binding, "cursor": None, "anchorEventId": None})
                            self.assertFalse(reply.isError)
                            self.assertIsNone(reply.structuredContent)
                            self.assertNotIn(SECRET, json.dumps([c.model_dump() for c in reply.content]))
                            snapshot = reply.meta["codex-display/result"]
                            self.assertTrue(snapshot["events"])
                            self.assertIn(SECRET, json.dumps(snapshot))
                            exported = await session.call_tool('download_records', {
                                'action': 'create', 'binding': binding, 'requestId': str(uuid4())})
                            self.assertFalse(exported.isError, exported.model_dump_json())
                            export = exported.meta['codex-display/result']
                            self.assertEqual(export['status'], 'ready')
                            self.assertEqual(export['save']['status'], 'notRequested')
                            downloaded = await session.call_tool('download_records', {
                                'action': 'read', 'binding': binding, 'exportId': export['exportId'], 'cursor': None})
                            self.assertFalse(downloaded.isError)
                            self.assertNotIn(SECRET, json.dumps([c.model_dump() for c in downloaded.content]))
                            chunk = downloaded.meta['codex-display/result']['chunk']
                            self.assertIsNone(chunk['nextCursor'])
                            with zipfile.ZipFile(io.BytesIO(base64.b64decode(chunk['base64']))) as archive:
                                records = [n for n in archive.namelist() if n.startswith('records/')]
                                self.assertEqual(len(records), 1)
                                self.assertEqual(archive.read(records[0]), source.read_bytes())
                            released = await session.call_tool('download_records', {
                                'action': 'release', 'binding': binding, 'exportId': export['exportId']})
                            self.assertEqual(released.meta['codex-display/result']['status'], 'released')
                            invalid = await session.call_tool("read_trajectory", {"binding": binding, "forbidden": SECRET})
                            self.assertTrue(invalid.isError)
                            self.assertNotIn(SECRET, invalid.model_dump_json())
                            self.assertIsNone(invalid.structuredContent)
                    err.flush(); err.seek(0)
                    self.assertNotIn(SECRET, err.read())
            asyncio.run(run())


if __name__ == "__main__": unittest.main()
