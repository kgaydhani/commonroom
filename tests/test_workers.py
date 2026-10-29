import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import workers
from commonroom.ledger import Ledger

# A worker-style command: it starts a grandchild that sleeps 60 s, records the grandchild's pid, reports a finished
# turn as Codex does, and exits at once (or sleeps too, so the room has to stop it).
WORKER = r"""
import json, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(grandchild.pid))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
if sys.argv[2] == "hang":
    time.sleep(60)
"""


def alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259                                      # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pid: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(.1)
    return not alive(pid)


class WorkerProcessTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ledger = Ledger(self.base / "room.sqlite3", synchronous="OFF")
        self.wt = self.base / "i50-x"
        self.wt.mkdir()
        self.script = self.base / "worker.py"
        self.script.write_text(WORKER, encoding="utf8")
        self.pid_file = self.base / "grandchild.pid"
        self.orig = workers.codex_command
        self.addCleanup(self.cleanup)

    def cleanup(self):
        workers.codex_command = self.orig
        if self.pid_file.exists() and alive(int(self.pid_file.read_text())):
            os.kill(int(self.pid_file.read_text()), signal.SIGTERM)  # a failed test leaves no sleeper behind
        shutil.rmtree(workers.worker_tmp(self.wt), ignore_errors=True)
        self.ledger.close()
        self.tmp.cleanup()

    def run_script(self, mode, **kwargs):
        workers.codex_command = lambda *a, **k: [sys.executable, str(self.script), str(self.pid_file), mode]
        return workers.run_worker("codex", workers.Role("builder", None, [], "x"), "y", self.wt, self.ledger,
                                  self.base / "logs", **kwargs)

    def test_a_finished_runs_leftover_grandchild_ends_with_it(self):
        started = time.monotonic()
        result = self.run_script("exit")
        self.assertTrue(result.ok, result.errors)
        self.assertLess(time.monotonic() - started, 30)       # the grandchild's inherited pipes didn't hold the run
        self.assertTrue(gone_within(int(self.pid_file.read_text()), 5))

    def test_the_stop_path_still_ends_a_running_worker_and_its_children(self):
        started = time.monotonic()
        result = self.run_script("hang", stop_check=lambda: "gate closed" if self.pid_file.exists() else None)
        self.assertEqual(result.interrupted, "gate closed")
        self.assertFalse(result.ok)
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(gone_within(int(self.pid_file.read_text()), 5))

    @unittest.skipUnless(os.name == "nt", "Job Objects are Windows-only")
    def test_only_the_worker_is_in_the_job_not_the_dispatcher(self):
        tree = workers.ProcessTree()
        process = tree.start([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertIsNotNone(tree.job)
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            inside = ctypes.c_int()
            kernel32.IsProcessInJob(ctypes.c_void_p(int(process._handle)), ctypes.c_void_p(tree.job), ctypes.byref(inside))
            self.assertTrue(inside.value)
            kernel32.IsProcessInJob(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.c_void_p(tree.job),
                                    ctypes.byref(inside))
            self.assertFalse(inside.value)
        finally:
            tree.end()
        process.wait(timeout=5)
        self.assertIsNone(tree.job)


if __name__ == "__main__":
    unittest.main()
