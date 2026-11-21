"""commonroom#279: a failing tasklist must not make a live lock holder read as dead."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import lock
from commonroom.lock import alive, claim_lock


def run_result(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


DENIED = run_result(1, "ERROR: Access denied\n")


class AliveWhenTasklistFails(unittest.TestCase):
    def test_denied_tasklist_falls_back_to_the_ctypes_query(self):
        with mock.patch.object(lock.subprocess, "run", return_value=DENIED):
            self.assertTrue(alive(os.getpid()))

    def test_ctypes_query_failing_too_means_alive(self):
        with mock.patch.object(lock.subprocess, "run", return_value=DENIED), \
                mock.patch.object(lock, "_query_process", return_value=None):
            self.assertTrue(alive(123456))

    def test_ctypes_query_can_say_dead(self):
        with mock.patch.object(lock.subprocess, "run", return_value=DENIED), \
                mock.patch.object(lock, "_query_process", return_value=False):
            self.assertFalse(alive(123456))

    def test_tasklist_raising_means_ask_windows_then_alive(self):
        with mock.patch.object(lock.subprocess, "run", side_effect=OSError), \
                mock.patch.object(lock, "_query_process", return_value=None):
            self.assertTrue(alive(123456))

    def test_successful_tasklist_without_a_row_is_dead(self):
        with mock.patch.object(lock.subprocess, "run", return_value=run_result(0, "INFO: No tasks are running.\n")):
            self.assertFalse(alive(123456))

    def test_claim_lock_keeps_a_live_holders_lock_when_tasklist_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locks" / "x-suite.lock"
            path.parent.mkdir()
            holder = subprocess.Popen(["python", "-c", "import time; time.sleep(30)"])
            try:
                path.write_text(str(holder.pid))
                with mock.patch.object(lock.subprocess, "run", return_value=DENIED):
                    self.assertEqual(claim_lock(path), holder.pid)
                self.assertEqual(path.read_text(), str(holder.pid))
            finally:
                holder.kill()
                holder.wait()


if __name__ == "__main__":
    unittest.main()
