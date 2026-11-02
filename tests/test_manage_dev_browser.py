"""开发服务管理使用临时回执及合成进程，验证生命周期且不触碰真实监听服务。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("manage_dev_browser", ROOT / "scripts/manage_dev_browser.py")
    management = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(management)
finally:
    sys.path.pop(0)


@unittest.skipIf(sys.platform == "win32", "POSIX process-group fixtures; Windows manager has separate tests")
class ManageDevBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-trace-manager-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.manager = management.ServiceManager(self.directory, startup_timeout=0.05, stop_timeout=0.05)
        self.children = {}
        self.process_handles = []
        self.commands = []
        self.signals = []
        self.exit_on_spawn = False
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.stack.enter_context(mock.patch.object(management, "runtime_paths",
            return_value=(Path(sys.executable), Path(sys.executable))))
        self.spawn = self.stack.enter_context(mock.patch.object(management.subprocess, "Popen", side_effect=self._spawn))
        self.processes = self.stack.enter_context(mock.patch.object(management, "process_info", side_effect=self._process_info))
        self.health = self.stack.enter_context(mock.patch.object(management, "probe_health", side_effect=self._health))
        self.busy = self.stack.enter_context(mock.patch.object(management, "port_busy", return_value=False))
        self.kill = self.stack.enter_context(mock.patch.object(management.os, "kill", side_effect=self._signal))
        self.killpg = self.stack.enter_context(mock.patch.object(management.os, "killpg", side_effect=self._signal))

    def _spawn(self, command, **kwargs):
        pid = 47000 + len(self.children)
        self.commands.append((list(command), kwargs))
        self.children[pid] = {"command": list(command), "alive": not self.exit_on_spawn,
                              "started": f"Sat Oct 24 12:00:{pid % 60:02d} 2026"}
        process = mock.Mock(pid=pid)
        process.poll.side_effect = lambda: None if self.children[pid]["alive"] else 1
        self.process_handles.append(process)
        return process

    def _process_info(self, pid):
        child = self.children.get(pid)
        if not child or not child["alive"]:
            return None
        return {"started": child["started"], "command": shlex.join(child["command"]), "group": pid}

    def _health(self, port):
        for pid, child in self.children.items():
            command = child["command"]
            if child["alive"] and int(command[command.index("--port") + 1]) == port:
                return {"service": "codex-trace-dev-browser", "pid": pid,
                        "instanceId": command[command.index("--instance-id") + 1]}
        return None

    def _signal(self, pid, action):
        self.signals.append((pid, action))
        child = self.children.get(abs(pid))
        if child and action != 0:
            child["alive"] = False

    def test_start_waits_for_owned_health_and_duplicate_start_reuses_process(self):
        result = self.manager.start(port=18765, skip_build=True)
        self.assertEqual(result["state"], "running")
        record = self.manager._read_record()
        self.assertEqual(record["port"], 18765)
        self.assertTrue(record["skipBuild"])
        self.assertEqual(Path(record["root"]), ROOT)
        self.assertTrue(self.manager.owns(record))
        self.assertEqual(self.manager.status()["state"], "running")
        command, options = self.commands[0]
        self.assertIn(str(ROOT / "scripts/dev_browser.py"), command)
        self.assertEqual(command[command.index("--instance-id") + 1], record["instanceId"])
        self.assertTrue(options["start_new_session"])
        self.assertTrue(options["close_fds"])
        self.assertIn("--skip-build", command)
        self.assertEqual(self.manager.pid_path.read_text().strip(), str(record["pid"]))
        self.assertEqual(self.manager.start(port=18765)["state"], "running")
        self.assertEqual(self.spawn.call_count, 1)
        self.assertFalse(self.signals)

    def test_stop_only_signals_owned_process_and_clears_receipts(self):
        self.manager.start(port=18766, skip_build=True)
        record = self.manager._read_record()
        stopped = self.manager.stop()
        self.assertEqual(stopped["state"], "stopped")
        self.assertTrue(self.signals)
        self.assertTrue(all(abs(pid) == record["pid"] and action != signal.SIGKILL
                            for pid, action in self.signals))
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse(self.manager.pid_path.exists())
        self.assertEqual(self.manager.status()["state"], "stopped")
        count = len(self.signals)
        self.manager.stop()
        self.assertEqual(len(self.signals), count)

    def test_restart_uses_previous_port_and_launch_settings(self):
        self.manager.start(port=18767, skip_build=True)
        first = self.manager._read_record()
        restarted = self.manager.restart()
        second = self.manager._read_record()
        self.assertEqual(restarted["state"], "running")
        self.assertNotEqual(second["pid"], first["pid"])
        self.assertNotEqual(second["instanceId"], first["instanceId"])
        self.assertEqual(second["port"], first["port"])
        self.assertEqual(second["python"], first["python"])
        self.assertEqual(second["codex"], first["codex"])
        self.assertTrue(second["skipBuild"])
        self.assertEqual(self.spawn.call_count, 2)
        self.assertFalse(self.children[first["pid"]]["alive"])

    def test_restart_invalid_port_or_runtime_keeps_original_service_running(self):
        self.manager.start(port=18774, skip_build=True)
        record = self.manager._read_record()
        for port in (0, 65536):
            with self.subTest(port=port), self.assertRaises(management.ManagerError):
                self.manager.restart(port=port)
        with mock.patch.object(management, "runtime_paths", side_effect=ValueError("missing runtime")):
            with self.assertRaises(ValueError):
                self.manager.restart(python_arg="/missing/python")
        self.assertEqual(self.manager._read_record(), record)
        self.assertEqual(self.manager.status()["state"], "running")
        self.assertEqual(self.spawn.call_count, 1)
        self.assertFalse(self.signals)

    def test_process_inspection_failure_preserves_record_without_signalling(self):
        self.manager.start(port=18775, skip_build=True)
        record = self.manager._read_record()
        with mock.patch.object(management, "process_info", side_effect=management.ManagerError("ps unavailable")):
            for operation in (self.manager.status, self.manager.stop):
                with self.subTest(operation=operation.__name__), self.assertRaises(management.ManagerError):
                    operation()
        self.assertEqual(self.manager._read_record(), record)
        self.assertTrue(self.manager.pid_path.exists())
        self.assertFalse(self.signals)
        self.assertTrue(self.children[record["pid"]]["alive"])

    def test_management_lock_conflict_refuses_without_process_actions(self):
        competing = management.ServiceManager(self.directory)
        with self.manager.locked():
            for operation in (competing.start, competing.stop, competing.restart, competing.status):
                with self.subTest(operation=operation.__name__), self.assertRaises(management.ManagerError):
                    operation()
        self.spawn.assert_not_called()
        self.assertFalse(self.signals)
        self.assertEqual(competing.status()["state"], "stopped")

    def test_stop_timeout_preserves_receipt_for_the_still_owned_process(self):
        self.manager.start(port=18773, skip_build=True)
        record = self.manager._read_record()
        # 模拟 graceful shutdown 尚未结束；管理器不能升级强杀或丢失后续停止依据。
        self.kill.side_effect = lambda pid, action: self.signals.append((pid, action))
        self.killpg.side_effect = lambda pid, action: self.signals.append((pid, action))
        with self.assertRaises(management.ManagerError):
            self.manager.stop()
        self.assertEqual(self.manager._read_record(), record)
        self.assertTrue(self.manager.pid_path.exists())
        self.assertTrue(self.children[record["pid"]]["alive"])
        self.assertTrue(all(abs(pid) == record["pid"] and action != signal.SIGKILL
                            for pid, action in self.signals))

    def test_occupied_port_refuses_without_signalling_unknown_listener(self):
        self.busy.return_value = True
        with self.assertRaises(management.ManagerError):
            self.manager.start(port=18768, skip_build=True)
        self.spawn.assert_not_called()
        self.assertFalse(self.signals)
        self.assertFalse(self.manager.record_path.exists())

    def test_malformed_receipt_refuses_process_actions(self):
        for contents in ("{", '{"schemaVersion":1,"pid":47000}'):
            self.manager.record_path.write_text(contents)
            for operation in (self.manager.status, self.manager.start, self.manager.stop):
                with self.subTest(contents=contents, operation=operation.__name__):
                    with self.assertRaises(management.ManagerError):
                        operation()
        self.spawn.assert_not_called()
        self.assertFalse(self.signals)

    def test_stale_or_reused_pid_cannot_authorize_termination(self):
        self.manager.start(port=18769, skip_build=True)
        record = self.manager._read_record()
        original = self._process_info(record["pid"])
        for changed in (None, {**original, "started": "different process birth"},
                {**original, "group": record["pid"] + 1},
                {**original, "command": "unrelated process --instance-id " + record["instanceId"]},
                {**original, "command": original["command"].replace(str(management.SERVER), str(management.SERVER) + ".other")},
                {**original, "command": original["command"].replace(record["instanceId"], record["instanceId"] + "-other")}):
            with self.subTest(changed=changed), mock.patch.object(management, "process_info", return_value=changed):
                self.assertFalse(self.manager.owns(record))
        self.children[record["pid"]]["started"] = "PID has been reused"
        self.assertNotEqual(self.manager.status()["state"], "running")
        try:
            self.manager.stop()
        except management.ManagerError:
            pass  # 可以保留回执提示核对，但不能据旧 PID 终止另一个进程。
        self.assertFalse(self.signals)

    def test_startup_exit_does_not_report_running_or_leave_active_receipt(self):
        self.exit_on_spawn = True
        with self.assertRaises(management.ManagerError):
            self.manager.start(port=18770, skip_build=True)
        self.assertNotEqual(self.manager.status()["state"], "running")
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse(self.manager.pid_path.exists())

    def test_postspawn_record_write_failure_cleans_child_and_partial_receipt(self):
        write_record = self.manager._write_record

        def partial_write_then_fail(record):
            write_record(record)
            raise OSError("simulated metadata write failure")

        with mock.patch.object(self.manager, "_write_record", side_effect=partial_write_then_fail):
            with self.assertRaises(OSError):
                self.manager.start(port=18776, skip_build=True)
        self.assertFalse(self.children[47000]["alive"])
        self.process_handles[0].wait.assert_called_once_with(timeout=self.manager.stop_timeout)
        self.assertTrue(self.signals)
        self.assertTrue(all(abs(pid) == 47000 and action == signal.SIGTERM for pid, action in self.signals))
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse(self.manager.pid_path.exists())

    def test_startup_timeout_stops_only_the_child_started_by_this_attempt(self):
        self.health.side_effect = None
        self.health.return_value = None
        with self.assertRaises(management.ManagerError):
            self.manager.start(port=18772, skip_build=True)
        self.assertFalse(self.children[47000]["alive"])
        self.assertTrue(self.signals)
        self.assertTrue(all(abs(pid) == 47000 and action != signal.SIGKILL for pid, action in self.signals))
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse(self.manager.pid_path.exists())

    def test_wrong_health_identity_is_unhealthy_and_never_reused_as_running(self):
        self.manager.start(port=18771, skip_build=True)
        self.health.side_effect = None
        self.health.return_value = {"service": "different-service", "pid": 47000,
                                   "instanceId": str(uuid4())}
        self.assertEqual(self.manager.status()["state"], "unhealthy")
        with self.assertRaises(management.ManagerError):
            self.manager.start(port=18771)
        self.assertEqual(self.spawn.call_count, 1)

    def test_start_status_and_restart_print_private_instance_launcher(self):
        from platform_dev import private_browser_launch
        secret = "synthetic_private_browser_token_".ljust(43, "x")
        for action, reused in (("start", False), ("start", True), ("status", False), ("restart", False)):
            record = {"pid": 47000, "port": 8765, "instanceId": str(uuid4())}
            manager = mock.Mock(directory=self.directory, log_path=self.directory / "server.log")
            getattr(manager, action).return_value = {"state": "running", "record": record, "alreadyRunning": reused}
            output = io.StringIO()
            with self.subTest(action=action, reused=reused), \
                    private_browser_launch(self.directory, record["instanceId"], record["port"], secret) as launch, \
                    mock.patch.object(management, "ServiceManager", return_value=manager), \
                    mock.patch.object(sys, "argv", ["manage_dev_browser.py", action]), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(management.main(), 0)
                self.assertIn(launch.as_uri(), output.getvalue())
                self.assertNotIn(secret, output.getvalue())
                self.assertNotIn("#token=", output.getvalue())
            self.assertFalse(launch.exists())


if __name__ == "__main__":
    unittest.main()
