"""Windows CI wrapper 的失败关闭和恢复约束；不把模拟 API 测试当作原生验收。"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import diagnose_windows_permissions as diagnostic
finally:
    sys.path.pop(0)


class WindowsDiagnosticTests(unittest.TestCase):
    def exercise_child(self, *, parent_id="parent-token", parent_user="user", command_error=False,
                       command_code=0, alter_privileges=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            request_path = root / "request.json"
            request_path.write_text(json.dumps({"command": ["synthetic-python", "-m", "unittest"],
                "report": str(report_path), "parentToken": {"tokenId": parent_id, "userSid": parent_user}}))
            state = {"owner": "administrators", "privileges": [["synthetic-privilege", 2]]}
            updates = []
            token = SimpleNamespace(Close=lambda: None)
            def snapshot(unused=None):
                return {"userSid": "user", "tokenId": "child-token", "ownerSid": state["owner"],
                        "ownerEqualsUser": state["owner"] == "user", "defaultDacl": [],
                        "elevated": True, "elevationType": 1, "groups": [["administrators", 7]],
                        "privileges": [item[:] for item in state["privileges"]]}
            def update(handle, kind, owner):
                self.assertIs(handle, token)
                self.assertEqual(kind, 4)
                updates.append(owner)
                state["owner"] = owner
                if alter_privileges and owner == "user":
                    state["privileges"] = [["synthetic-privilege", 0]]
            security = SimpleNamespace(TOKEN_QUERY=8, TOKEN_ADJUST_DEFAULT=128, TokenOwner=4, TokenUser=1,
                OpenProcessToken=lambda *args: token,
                GetTokenInformation=lambda handle, kind: state["owner"] if kind == 4 else ("user", 0),
                SetTokenInformation=update)
            modules = {"win32api": SimpleNamespace(GetCurrentProcess=lambda: "own-process"), "win32security": security}
            with patch.dict(sys.modules, modules), patch.object(diagnostic, "token_snapshot", side_effect=snapshot), \
                    patch.object(diagnostic, "diagnostic", return_value={"ordinaryChildToken": {"ownerEqualsUser": True}}), \
                    patch.object(diagnostic.subprocess, "run") as command, patch("builtins.print"):
                if command_error:
                    command.side_effect = OSError("synthetic test process failed")
                else:
                    command.return_value = SimpleNamespace(returncode=command_code)
                code = diagnostic.normalized_child(request_path)
            return code, json.loads(report_path.read_text()), updates, command.call_count

    def test_refuses_shared_token_before_any_owner_change(self):
        code, report, updates, calls = self.exercise_child(parent_id="child-token")
        self.assertEqual(code, 1)
        self.assertEqual(updates, [])
        self.assertEqual(calls, 0)
        self.assertEqual(report["error"]["stage"], "verify_independent_child_token")

    def test_refuses_different_user_before_any_owner_change(self):
        code, report, updates, calls = self.exercise_child(parent_user="another-user")
        self.assertEqual(code, 1)
        self.assertEqual(updates, [])
        self.assertEqual(calls, 0)

    def test_child_owner_restores_after_failed_test_command(self):
        for raises, expected in ((False, 7), (True, 1)):
            with self.subTest(raises=raises):
                code, report, updates, calls = self.exercise_child(command_error=raises, command_code=7)
                self.assertEqual(code, expected)
                self.assertEqual(updates, ["user", "administrators"])
                self.assertEqual(calls, 1)
                self.assertTrue(report["onlyDefaultOwnerChanged"])
                self.assertTrue(report["childTokenRestored"])
                self.assertFalse(report["ordinaryUserValidation"])

    def test_changed_privileges_block_test_execution(self):
        code, report, updates, calls = self.exercise_child(alter_privileges=True)
        self.assertEqual(code, 1)
        self.assertEqual(updates, ["user", "administrators"])
        self.assertEqual(calls, 0)
        self.assertFalse(report["onlyDefaultOwnerChanged"])
        self.assertFalse(report["childTokenRestored"])


if __name__ == "__main__":
    unittest.main()
