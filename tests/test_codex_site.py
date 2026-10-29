"""Codex workers load a sitecustomize that makes `tempfile.mkdtemp` folders writable under the sandbox's restricted
token (commonroom#182). Claude workers don't get it."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import workers

SHIM = str(workers.CODEX_SITE)


def python(code: str, pythonpath: str, *flags: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=pythonpath)
    return subprocess.run([sys.executable, *flags, "-c", code], env=env, capture_output=True, text=True, timeout=60)


class WorkerEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "i182-x"
        self.wt.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(shutil.rmtree, workers.worker_tmp(self.wt), True)

    def test_codex_gets_the_shim_first_and_keeps_an_existing_pythonpath(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join(["A", "B"])}):
            env = workers.worker_env("codex", self.wt)
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [SHIM, "A", "B"])
        self.assertTrue((workers.CODEX_SITE / "sitecustomize.py").is_file())

    def test_codex_without_a_pythonpath_gets_just_the_shim(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONPATH", None)
            self.assertEqual(workers.worker_env("codex", self.wt)["PYTHONPATH"], SHIM)

    def test_claude_env_is_unchanged(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "A"}):
            self.assertEqual(workers.worker_env("claude", self.wt)["PYTHONPATH"], "A")
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONPATH", None)
            self.assertNotIn("PYTHONPATH", workers.worker_env("claude", self.wt))


class Shim(unittest.TestCase):
    def test_mkdtemp_folders_take_a_child_file_and_delete(self):
        code = ("import tempfile, os, shutil, sys\n"
                "d = tempfile.mkdtemp()\n"
                "open(os.path.join(d, 'x'), 'w').write('1')\n"
                "with tempfile.TemporaryDirectory() as t: open(os.path.join(t, 'y'), 'w').write('1')\n"
                "shutil.rmtree(d)\n"
                "print(os.path.exists(d), type(tempfile._os).__name__ if sys.platform == 'win32' else 'n/a')\n")
        done = python(code, SHIM)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["False", "_TempfileOs" if sys.platform == "win32" else "n/a"])

    def test_only_a_0o700_mkdir_changes_mode(self):
        code = ("import sys, unittest.mock as m, tempfile, os\n"
                "with m.patch('os.mkdir') as mk:\n"
                "    tempfile._os.mkdir('a', 0o700); tempfile._os.mkdir('b', 0o755); tempfile._os.mkdir('c')\n"
                "print([c.args for c in mk.call_args_list])\n"
                "print(tempfile._os.path is os.path, os.mkdir is not tempfile._os.mkdir)\n")
        done = python(code, SHIM)
        self.assertEqual(done.returncode, 0, done.stderr)
        if sys.platform == "win32":
            self.assertEqual(done.stdout.splitlines(), ["[('a', 511), ('b', 493), ('c', 511)]", "True True"])

    def test_it_is_a_no_op_off_windows(self):
        # -S: no automatic site import, so the platform can be patched before the shim loads
        code = ("import sys; sys.platform = 'linux'; sys.path.insert(0, " + repr(SHIM) + ")\n"
                "import os, tempfile, sitecustomize\n"
                "sitecustomize.install('darwin')\n"
                "print(tempfile._os is os)\n")
        done = python(code, "", "-S")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "True")

    def test_a_projects_own_sitecustomize_still_runs(self):
        with tempfile.TemporaryDirectory() as other:
            Path(other, "sitecustomize.py").write_text("import builtins\nbuiltins.PROJECT_SITE = 1\n")
            done = python("import builtins; print(getattr(builtins, 'PROJECT_SITE', 0))",
                          os.pathsep.join([SHIM, other]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
