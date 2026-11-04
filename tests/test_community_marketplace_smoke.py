"""社区市场验收使用真实临时 Git 仓库，检查源隔离、校验与更新证据边界。"""
import hashlib
import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import smoke_community_marketplace as smoke
finally:
    sys.path.pop(0)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_repository(root: Path, env: dict, git: Path) -> Path:
    repository = root / "original source"
    plugin = repository / smoke.PLUGIN_PATH
    plugin.mkdir(parents=True)
    write_json(repository / ".agents/plugins/marketplace.json", {
        "name": smoke.MARKETPLACE, "interface": {"displayName": "Synthetic community market"},
        "plugins": [{"name": "codex-trace", "source": {"source": "local", "path": "./plugins/codex-trace"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Developer Tools"}]})
    identity = {"name": "codex-trace", "version": "0.2.0", "description": "Synthetic CLI fixture",
                "mcpServers": "./.mcp.json"}
    write_json(plugin / "plugin.json", identity)
    write_json(plugin / ".codex-plugin/plugin.json", identity)
    launch = {"command": "uv", "args": ["run", "--no-project", "--no-config", "--python", "3.14.2",
              "--with-requirements", "./requirements.lock", "--", "python", "-I", "-B",
              "./launch.py"], "cwd": "."}
    for name in (".mcp.json", "mcp.json"):
        write_json(plugin / name, {"mcpServers": {"codex-trace": {
            **launch, "cwd": "." if name == ".mcp.json" else "${PLUGIN_ROOT}"}}})
    (plugin / "SUPPORT.md").write_text("Synthetic support information.\n", encoding="utf-8")
    (plugin / "launch.py").write_text("# Synthetic fixture; runtime is never invoked by unit tests.\n", encoding="utf-8")
    (plugin / "requirements.lock").write_text("", encoding="utf-8")
    write_json(plugin / "package-manifest.json", {
        "name": "codex-trace", "version": "0.2.0", "distribution": "github-repository-marketplace",
        "runtimeManagement": "uv", "runtimeBundled": False, "files": smoke.fingerprints(plugin)})
    smoke.run([str(git), "init", "-b", "main"], cwd=repository, env=env)
    smoke.run([str(git), "add", "."], cwd=repository, env=env)
    smoke.run([str(git), "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
               "-c", "commit.gpgsign=false", "commit", "-m", "Create synthetic marketplace"], cwd=repository, env=env)
    return repository


class CommunitySmokeInputTests(unittest.TestCase):
    def test_github_helper_restores_profile_only_inside_credential_subprocess(self):
        original_home = Path(tempfile.gettempdir()).resolve() / "original home"
        gh = original_home.parent / "tools" / ("gh.exe" if os.name == "nt" else "gh")
        isolated = {"HOME": "/temporary/home", "CODEX_HOME": "/temporary/codex",
                    "GH_CONFIG_DIR": "/temporary/gh", "GIT_CONFIG_GLOBAL": "/temporary/gitconfig"}
        before = dict(isolated)
        original = {"HOME": str(original_home), "GH_CONFIG_DIR": "/original gh", "GH_TOKEN": "never-copy-this-token"}
        with patch.dict(os.environ, original, clear=True), patch.object(smoke.shutil, "which", return_value=str(gh)), \
                patch.object(smoke.Path, "home", return_value=original_home), patch.object(smoke, "run") as run:
            smoke.configure_github_auth(isolated, Path("/temporary"), Path("/tools/git"))
        self.assertEqual(isolated, before)
        command = run.call_args.args[0]
        self.assertEqual(command[1:5], ["config", "--file", "/temporary/gitconfig", "credential.https://github.com.helper"])
        tokens = shlex.split(command[5][1:])
        self.assertIn("HOME=" + str(original_home), tokens)
        self.assertIn("GH_CONFIG_DIR=/original gh", tokens)
        self.assertEqual(tokens[-3:], [gh.as_posix(), "auth", "git-credential"])
        self.assertNotIn("never-copy-this-token", command[5])
        self.assertEqual(run.call_args.kwargs["env"], before)

    def test_github_helper_uses_original_windows_config_locations(self):
        profile = {"APPDATA": "C:/Users/Test User/AppData/Roaming", "USERPROFILE": "C:/Users/Test User",
                   "LOCALAPPDATA": "C:/Users/Test User/AppData/Local"}
        helper = smoke.github_credential_helper(PureWindowsPath("C:/Program Files/GitHub CLI/gh.exe"),
                                                PureWindowsPath("C:/Users/Test User"), profile, windows=True)
        tokens = shlex.split(helper[1:])
        self.assertIn("GH_CONFIG_DIR=" + str(Path(profile["APPDATA"]) / "GitHub CLI"), tokens)
        for key, value in profile.items():
            self.assertIn(key + "=" + value, tokens)
        self.assertEqual(tokens[-3:], ["C:/Program Files/GitHub CLI/gh.exe", "auth", "git-credential"])
        explicit = smoke.github_credential_helper(Path("/tools/gh"), Path("/home"),
            {"GH_CONFIG_DIR": "/explicit/gh", "XDG_CONFIG_HOME": "/xdg"}, windows=False)
        self.assertIn("GH_CONFIG_DIR=/explicit/gh", shlex.split(explicit[1:]))
        xdg = smoke.github_credential_helper(Path("/tools/gh"), Path("/home"), {"XDG_CONFIG_HOME": "/xdg"}, windows=False)
        self.assertIn("GH_CONFIG_DIR=" + str(Path("/xdg/gh")), shlex.split(xdg[1:]))

    @unittest.skipUnless(os.name == "posix", "This regression executes the POSIX helper directly")
    def test_github_helper_quotes_paths_and_keeps_codex_home_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gh = root / "gh 'quoted $(touch unsafe)"
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$HOME" "$GH_CONFIG_DIR" "$CODEX_HOME" "$@"\n', encoding="utf-8")
            gh.chmod(0o700)
            original_home = root / "home 'quoted $(touch unsafe-home)"
            original_config = root / "gh config 'quoted $(touch unsafe-config)"
            env = {**os.environ, "HOME": str(root / "isolated"), "CODEX_HOME": str(root / "isolated/.codex")}
            helper = smoke.github_credential_helper(gh, original_home, {"GH_CONFIG_DIR": str(original_config)}, windows=False)
            result = subprocess.run(["/bin/sh", "-c", helper[1:] + " get"], cwd=root, env=env,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [str(original_home), str(original_config), env["CODEX_HOME"],
                                                         "auth", "git-credential", "get"])
            self.assertEqual(sorted(path.name for path in root.iterdir()), [gh.name])

    def test_profile_and_dependency_configuration_are_isolated(self):
        contaminated = {"HOME": "/real/home", "USERPROFILE": "/real/home", "CODEX_HOME": "/real/codex",
                        "CODEX_TRACE_STATE_ROOT": "/real/state", "PYTHONPATH": "/real/python",
                        "UV_PYTHON_INSTALL_DIR": "/real/uv", "UV_INDEX_URL": "https://private.invalid",
                        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.clone", "PATH": "/system/bin"}
        with patch.dict(os.environ, contaminated, clear=True):
            env = smoke.isolated_environment(Path("/temporary/home"), Path("/temporary/uv-cache"))
        for key in ("CODEX_TRACE_STATE_ROOT", "PYTHONPATH", "UV_INDEX_URL", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CODEX_HOME"], str(Path("/temporary/home/.codex")))
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(Path("/temporary/python-managed")))
        self.assertEqual(env["UV_PYTHON_PREFERENCE"], "only-managed")
        self.assertEqual(env["PATH"], "/system/bin")

    def test_https_source_rejects_credentials_protocols_and_control_characters(self):
        valid = "https://github.com/example/project.git"
        self.assertEqual(smoke.repository_source(valid, "main"), (valid, False))
        for value in ("https://token@github.com/example/repo", "https://github.com/example/repo?token=x",
                      "http://github.com/example/repo", "file:///tmp/repo", "git@example.com:repo",
                      "https://github.com/\nrepo", "https://github.com/example/repo#ref"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                smoke.repository_source(value, None)
        for ref in ("--config=evil", "main\nother", ""):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                smoke.repository_source(valid, ref)

    def test_cli_paths_cannot_escape_private_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertEqual(smoke.contained_path(str(root / "cache/plugin"), root), root / "cache/plugin")
            for value in ("relative/path", str(root.parent), str(root)):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    smoke.contained_path(value, root)

    def test_command_failures_never_copy_child_stderr_into_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "failed with exit code 4") as error:
                smoke.run([sys.executable, "-c", "import sys;print('SECRET_TOKEN',file=sys.stderr);sys.exit(4)"],
                          cwd=Path(temporary), env=dict(os.environ))
            self.assertNotIn("SECRET_TOKEN", str(error.exception))

    def test_git_readonly_objects_can_be_removed_on_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "private git copy"
            tree.mkdir()
            file = tree / "readonly-object"
            file.write_bytes(b"synthetic Git object")
            file.chmod(0o400)
            smoke.remove_tree(tree)
            self.assertFalse(tree.exists())


@unittest.skipUnless(shutil.which("git"), "Git is required for synthetic repository checks")
class CommunitySmokeGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="community-tests-中文 空格-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        home = self.root / "home"
        home.mkdir()
        self.env = smoke.isolated_environment(home, self.root / "uv-cache")
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text("", encoding="utf-8")
        self.git = Path(shutil.which("git")).absolute()
        self.repository = make_repository(self.root, self.env, self.git)

    def clone(self):
        destination = self.root / "private clone"
        details = smoke.prepare_repository(str(self.repository), True, "main", destination, self.env, self.git)
        return destination, details

    def test_local_source_is_classified_without_modifying_it(self):
        source, local = smoke.repository_source(str(self.repository), None)
        self.assertTrue(local)
        self.assertEqual(source, str(self.repository))
        original = smoke.fingerprints(self.repository / smoke.PLUGIN_PATH)
        clone, (registered, ref, commit) = self.clone()
        self.assertEqual(registered, smoke.LOCAL_GIT_ALIAS)
        self.assertEqual(ref, smoke.FIXTURE_BRANCH)
        self.assertEqual(len(commit), 40)
        self.assertEqual(smoke.fingerprints(clone / smoke.PLUGIN_PATH), original)
        self.assertEqual(smoke.fingerprints(self.repository / smoke.PLUGIN_PATH), original)

    def test_dirty_local_repository_is_rejected_before_clone(self):
        (self.repository / "uncommitted.txt").write_text("not a release input", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must-be-clean"):
            self.clone()
        self.assertFalse((self.root / "private clone").exists())

    def test_explicit_non_default_git_branch_is_fetched(self):
        smoke.run([str(self.git), "branch", "release-candidate"], cwd=self.repository, env=self.env)
        destination = self.root / "alternate clone"
        registered, ref, commit = smoke.prepare_repository(
            str(self.repository), True, "release-candidate", destination, self.env, self.git)
        self.assertEqual(registered, smoke.LOCAL_GIT_ALIAS)
        self.assertEqual(ref, smoke.FIXTURE_BRANCH)
        self.assertEqual(commit, smoke.run([str(self.git), "rev-parse", "release-candidate"],
                                         cwd=self.repository, env=self.env).stdout.strip())

    def test_same_version_update_changes_only_private_support_and_manifest(self):
        clone, (_, _, initial_commit) = self.clone()
        original = smoke.fingerprints(self.repository / smoke.PLUGIN_PATH)
        updated_commit = smoke.commit_update_probe(clone, self.env, self.git)
        updated = smoke.fingerprints(clone / smoke.PLUGIN_PATH)
        self.assertNotEqual(updated_commit, initial_commit)
        self.assertEqual(smoke.validate_repository(clone), clone / smoke.PLUGIN_PATH)
        self.assertEqual({name for name in original if original[name] != updated[name]},
                         {"SUPPORT.md", "package-manifest.json"})
        self.assertEqual(smoke.validate_plugin(clone / smoke.PLUGIN_PATH)["version"], "0.2.0")
        self.assertEqual(smoke.fingerprints(self.repository / smoke.PLUGIN_PATH), original)
        # URL 映射也经过真实 Git 解析，证明更新分支指向私有 clone 的新 commit。
        head = smoke.run([str(self.git), "ls-remote", smoke.LOCAL_GIT_ALIAS, smoke.FIXTURE_BRANCH],
                         cwd=self.root, env=self.env).stdout.split()[0]
        self.assertEqual(head, updated_commit)

    def test_package_rejects_modified_and_undeclared_files(self):
        plugin = self.repository / smoke.PLUGIN_PATH
        smoke.validate_plugin(plugin)
        (plugin / "unexpected.py").write_text("print('unexpected')", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory-or-checksum"):
            smoke.validate_plugin(plugin)
        (plugin / "unexpected.py").unlink()
        (plugin / "SUPPORT.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory-or-checksum"):
            smoke.validate_plugin(plugin)

    def test_package_rejects_symlinks_before_reading_target(self):
        plugin = self.repository / smoke.PLUGIN_PATH
        link = plugin / "external.py"
        try:
            link.symlink_to(self.root / "missing-private-file")
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaisesRegex(ValueError, "links"):
            smoke.fingerprints(plugin)

    def test_wrong_uv_entrypoint_is_rejected_even_with_valid_file_checksum(self):
        plugin = self.repository / smoke.PLUGIN_PATH
        launch_path = plugin / ".mcp.json"
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        launch["mcpServers"]["codex-trace"]["command"] = "/developer/.venv/bin/python"
        write_json(launch_path, launch)
        manifest_path = plugin / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][".mcp.json"] = hashlib.sha256(launch_path.read_bytes()).hexdigest()
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "uv-command"):
            smoke.validate_plugin(plugin)

    @unittest.skipUnless(shutil.which("codex"), "Optional integration check needs a real Codex CLI")
    def test_real_cli_git_update_and_relocation_without_runtime_or_real_tasks(self):
        def synthetic_verify(installed, root, checks, **options):
            self.assertTrue(options["community"])
            self.assertFalse((installed.parent / "source repository").exists())
            self.assertFalse((installed.parent / "cli-home/.codex/plugins/cache").exists())
            smoke.validate_plugin(installed)
            checks.append({"operation": "synthetic_test_runtime_stub", "passed": True})
            return {"runtimeWasStubbedForUnitTest": True}
        with patch.object(smoke, "verify_cache", side_effect=synthetic_verify):
            result = smoke.smoke(str(self.repository), codex=smoke.executable("codex"), uv=Path(sys.executable))
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["sameVersionUpdateVerified"])
        self.assertFalse(result["crossVersionUpgradeVerified"])
        self.assertTrue(result["updatePayloadChanged"])
        self.assertEqual(smoke.run([str(self.git), "status", "--porcelain"],
                                  cwd=self.repository, env=self.env).stdout, "")


if __name__ == "__main__":
    unittest.main()
