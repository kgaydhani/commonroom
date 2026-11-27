import importlib.util
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


TWO = (
    "import unittest\n"
    "class A(unittest.TestCase):\n    def test_a(self): pass\n"
    "class B(unittest.TestCase):\n    def test_b(self): pass\n"
    "    def test_c(self): self.fail('boom')\n"
)
BIG = ("class Big(unittest.TestCase):\n"
       + "".join(f"    def test_{i}(self): pass\n" for i in range(9)))


def load_runner():
    spec = importlib.util.spec_from_file_location("parallel_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plan(files):
    with tempfile.TemporaryDirectory() as d:
        for name, body in files.items():
            (Path(d) / name).write_text(body, encoding="utf-8", newline="\n")
        return load_runner().plan_units(Path(d))


class SplitTests(unittest.TestCase):
    def test_failure_in_one_class_names_module_and_class(self):
        code, out = run({"test_two.py": TWO, "test_one.py": PASS})
        self.assertEqual(code, 1, out)
        self.assertIn("Ran 5 tests", out)
        self.assertIn("MODULE FAILED: test_two.py::B", out)
        self.assertNotIn("test_two.py::A", out)
        self.assertIn("FAILED (failures=1)", out)

    def test_units_start_largest_first_and_single_class_stays_whole(self):
        units = plan({"test_two.py": TWO + BIG, "test_single.py": PASS})
        sizes = [u[2] for u in units]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(units[0][:2], ("test_two.py", "Big"))
        self.assertEqual(len([u for u in units if u[0] == "test_two.py"]), 3)
        self.assertEqual([u[:2] for u in units if u[0] == "test_single.py"],
                         [("test_single.py", None)])

    def test_unresolved_base_keeps_module_whole(self):
        body = ("import unittest\nfrom elsewhere import Base\n"
                "class A(unittest.TestCase):\n    def test_a(self): pass\n"
                "class B(Base):\n    pass\n")
        self.assertEqual([u[:2] for u in plan({"test_x.py": body})], [("test_x.py", None)])

    def test_local_subclass_of_testcase_is_a_unit(self):
        body = ("import unittest\nclass Base(unittest.TestCase):\n    def test_a(self): pass\n"
                "class Sub(Base):\n    def test_b(self): pass\n")
        self.assertEqual(sorted(u[1] for u in plan({"test_x.py": body})), ["Base", "Sub"])

    def test_classes_built_on_a_sibling_modules_base_are_split(self):
        base = "import unittest\nclass Base(unittest.TestCase):\n    def test_a(self): pass\n"
        user = ("import unittest\nfrom test_base import Base\n"
                "class A(Base):\n    def test_b(self): pass\n"
                "class B(unittest.TestCase):\n    def test_c(self): pass\n")
        units = plan({"test_base.py": base, "test_user.py": user})
        self.assertEqual(sorted(u[1] for u in units if u[0] == "test_user.py"),
                         ["A", "B", "Base"])
        code, out = run({"test_base.py": base, "test_user.py": user})
        self.assertEqual(code, 0, out)
        self.assertIn("Ran 5 tests", out)  # same count as one process per module


if __name__ == "__main__":
    unittest.main()
