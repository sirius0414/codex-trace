"""线程传输的资源边界；原生 Windows 才能验证 CancelSynchronousIo 的实际行为。"""
import os
import subprocess
import sys
import time
import unittest

from codex_display.platform.pipes import ThreadPipeTransport


class PipeCleanupTests(unittest.TestCase):
    def test_close_stops_blocked_workers_while_another_owned_child_retains_both_pipes(self):
        """模拟子孙进程继承 stdio；显式持有两个测试进程，清理不能靠强杀继承者。"""
        read_in, write_in = os.pipe()
        read_out, write_out = os.pipe()
        reader = os.fdopen(read_out, "rb", buffering=0)
        writer = os.fdopen(write_in, "wb", buffering=0)
        processes = []
        transport = None
        try:
            # 两个自有进程同时持有相同的内核管道端，等价于 app-server 的子孙进程
            # 保留 stdout/stdin。直接保留 Popen，避免按磁盘 PID 清理其他进程。
            for _ in range(2):
                processes.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                                   stdin=read_in, stdout=write_out, stderr=subprocess.DEVNULL,
                                                   close_fds=True))
            os.close(read_in)
            read_in = None
            os.close(write_out)
            write_out = None
            transport = ThreadPipeTransport(writer, reader)
            with self.assertRaises(TimeoutError):
                transport.write(b"x" * (4 * 1024 * 1024), time.monotonic() + .15)
            processes[0].terminate()
            processes[0].wait(timeout=3)
            started = time.monotonic()
            transport.close()
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(any(thread.is_alive() for thread in transport.threads))
            self.assertIsNone(processes[1].poll(), "cleanup must not terminate the pipe holder")
        finally:
            # 即使断言失败，也只终止此测试明确创建并持有的两个进程。
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
            if transport is not None:
                transport.close()
            writer.close()
            reader.close()
            for descriptor in (read_in, write_out):
                if descriptor is not None:
                    os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
