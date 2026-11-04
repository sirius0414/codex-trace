"""校验两种插件清单的公开元数据与发布版本，防止两种安装入口展示不同声明。"""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PluginMetadataTests(unittest.TestCase):
    def setUp(self):
        self.legacy = read_json(PLUGIN / ".codex-plugin/plugin.json")
        self.portable = read_json(PLUGIN / "plugin.json")

    def test_portable_and_compatibility_metadata_cannot_drift(self):
        self.assertEqual(self.portable["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        for field in ("name", "version", "description", "author", "license", "homepage", "repository"):
            with self.subTest(field=field):
                self.assertEqual(self.portable[field], self.legacy[field])
        extension = self.portable["extensions"]["com.openai"]
        self.assertEqual(extension["interface"], self.legacy["interface"])
        self.assertIs(extension["requires_local_executor"], True)
        self.assertEqual(extension["requires_local_executor"], self.legacy["requires_local_executor"])
        # Portable MCP 配置由打包器在包根生成；不把兼容路径混进根清单。
        self.assertNotIn("mcpServers", self.portable)
        self.assertNotIn("mcpServers", extension)
        self.assertEqual(self.legacy["mcpServers"], "./.mcp.json")

    def test_all_published_version_declarations_agree(self):
        from codex_trace import __version__
        package = read_json(ROOT / "package.json")
        lock = read_json(ROOT / "package-lock.json")
        self.assertEqual({package["version"], lock["version"], lock["packages"][""]["version"],
                          self.legacy["version"], self.portable["version"], __version__}, {package["version"]})

    def test_directory_fields_fit_published_limits_and_assets_exist(self):
        interface = self.legacy["interface"]
        limits = {"displayName": 30, "shortDescription": 30, "longDescription": 4000, "developerName": 80}
        for field, limit in limits.items():
            with self.subTest(field=field):
                self.assertIsInstance(interface[field], str)
                self.assertTrue(interface[field].strip())
                self.assertLessEqual(len(interface[field]), limit)
        self.assertNotIn("\n", interface["shortDescription"])
        self.assertEqual(interface["developerName"], self.legacy["author"]["name"])
        self.assertEqual(interface["category"], "Developer Tools")
        capabilities = interface["capabilities"]
        self.assertGreater(len(capabilities), 0)
        self.assertLessEqual(len(capabilities), 20)
        for capability in capabilities:
            self.assertIsInstance(capability, str)
            self.assertTrue(capability.strip())
            self.assertLessEqual(len(capability), 120)
        for field in ("composerIcon", "composerIconDark", "logo", "logoDark"):
            with self.subTest(asset=field):
                relative = interface[field]
                self.assertTrue(relative.startswith("./assets/"))
                path = (PLUGIN / relative).resolve()
                self.assertTrue(path.is_relative_to((PLUGIN / "assets").resolve()))
                self.assertTrue(path.is_file())

    def test_listing_links_are_https_and_local_policy_targets_exist(self):
        interface = self.legacy["interface"]
        base = self.legacy["repository"]
        self.assertEqual(interface["websiteURL"], self.legacy["homepage"])
        self.assertEqual(interface["supportURL"], base + "/issues")
        for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
            with self.subTest(field=field):
                value = interface[field]
                parts = urlsplit(value)
                self.assertEqual(parts.scheme, "https")
                self.assertTrue(parts.netloc)
                self.assertIsNone(parts.username)
                self.assertIsNone(parts.password)
                self.assertLessEqual(len(value), 1024)
        for field, name in (("privacyPolicyURL", "PRIVACY.md"), ("termsOfServiceURL", "TERMS.md")):
            self.assertEqual(interface[field], base + "/blob/main/" + name)
            self.assertTrue((ROOT / name).is_file())
        self.assertTrue((ROOT / "SUPPORT.md").is_file())


if __name__ == "__main__":
    unittest.main()
