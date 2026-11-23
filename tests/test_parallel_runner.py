import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "parallel.py"

PASS = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\n"
FAIL = "import unittest\nclass F(unittest.TestCase):\n    def test_broken(self): self.fail('boom')\n"
BADIMPORT = "import no_such_module_xyz\n"


def run(files):
    with tempfile.TemporaryDirectory() as d:
        for name, body in files.items():
            (Path(d) / name).write_text(body, encoding="utf-8", newline="\n")
        p = subprocess.run([sys.executable, str(RUNNER), "--start", d, "--jobs", "2"],
                           cwd=d, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr


class ParallelRunnerTests(unittest.TestCase):
    def test_all_pass(self):
        code, out = run({"test_one.py": PASS, "test_two.py": PASS})
        self.assertEqual(code, 0, out)
        self.assertIn("Ran 4 tests", out)
        self.assertEqual(out.rstrip().splitlines()[-1], "OK")

    def test_failure_names_module_and_test(self):
        code, out = run({"test_one.py": PASS, "test_bad.py": FAIL})
        self.assertEqual(code, 1)
        self.assertIn("Ran 3 tests", out)
        self.assertIn("test_bad.py", out)
        self.assertIn("test_broken", out)
        self.assertIn("FAILED (failures=1)", out)

    def test_import_error_names_module(self):
        code, out = run({"test_one.py": PASS, "test_imp.py": BADIMPORT})
        self.assertEqual(code, 1)
        self.assertIn("test_imp.py", out)
        self.assertIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
