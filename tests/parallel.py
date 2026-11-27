"""Run the test suite one module per process, in parallel (standard library only).

    python tests/parallel.py [--jobs N] [--start DIR]

Discovers the same test_*.py modules as `python -m unittest discover -s tests`,
runs each with unittest in its own process, and prints unittest-style output:
failures with tracebacks and module names, then `Ran N tests in Xs` and
`OK` or `FAILED (...)`. Exits 0 only when every module passed.
"""
import argparse
import ast
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
COUNTS = re.compile(r"^FAILED \((.*)\)\s*$", re.M)


def _base_name(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _kind(path: Path, name: str, seen=()) -> str:
    """'test' when the class `name` (defined in or imported into `path`) is a
    unittest.TestCase subclass, 'plain' when it is not, 'unknown' when its
    base can't be resolved from source. Follows `from sibling import X`."""
    key = (str(path), name)
    tree = _parse(path)
    if tree is None or key in seen:
        return "unknown"
    seen = seen + (key,)
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            result = "plain"
            for b in n.bases:
                bn = _base_name(b)
                if bn.endswith("TestCase"):
                    return "test"
                if bn in ("object", ""):
                    continue
                sub = _kind(path, bn, seen)
                if sub == "test":
                    return "test"
                if sub == "unknown":
                    result = "unknown"
            return result
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for al in n.names:
                if (al.asname or al.name) == name:
                    sib = path.parent / (n.module + ".py")
                    return _kind(sib, al.name, seen) if sib.is_file() else "unknown"
    return "unknown"


def split_module(path: Path) -> list:
    """Return [(class name, source lines)] for every TestCase class the loader
    would find in the module (defined there, or imported from a sibling test
    module), or [] when the module should run as one unit: it has fewer than
    two such classes, or a base that can't be resolved here (so a split could
    drop tests)."""
    tree = _parse(path)
    if tree is None:
        return []
    if any(isinstance(n, ast.FunctionDef) and n.name == "load_tests" for n in tree.body):
        return []
    tests = []
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            k = _kind(path, n.name)
            size = (n.end_lineno or n.lineno) - n.lineno + 1
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module                 and (path.parent / (n.module + ".py")).is_file():
            for al in n.names:
                if _kind(path, al.asname or al.name) == "test":
                    tests.append((al.asname or al.name, 1))
            continue
        else:
            continue
        if k == "unknown":
            return []
        if k == "test":
            tests.append((n.name, size))
    return tests if len(tests) > 1 else []


def plan_units(start: Path) -> list:
    """Units to run, slowest expected first: (module file, class or None, size).
    Size is the source lines of the class (or of the whole module when it is
    not split); ties break by name so the order is deterministic."""
    units = []
    for p in start.glob("test_*.py"):
        classes = split_module(p)
        if classes:
            units += [(p.name, cls, lines) for cls, lines in classes]
        else:
            try:
                size = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                size = 0
            units.append((p.name, None, size))
    units.sort(key=lambda u: (-u[2], u[0], u[1] or ""))
    return units


def run_module(start: Path, name: str, cwd: Path, cls: str = None) -> dict:
    env = None
    if cls:
        cmd = [sys.executable, "-m", "unittest", f"{name[:-3]}.{cls}"]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(start)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    else:
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(start), "-p", name]
    label = f"{name}::{cls}" if cls else name
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        out, code = p.stderr + p.stdout, p.returncode
    except OSError as exc:
        out, code = f"could not start: {exc}\n", 1
    m = RAN.search(out)
    ran = int(m.group(1)) if m else 0
    if cls and m and ran == 0 and code == 5:
        code = 0  # a base class with no tests of its own, nothing to run
    fails = errs = 0
    c = COUNTS.search(out)
    if c:
        for key, val in re.findall(r"(\w+)=(\d+)", c.group(1)):
            if key == "failures":
                fails += int(val)
            elif key == "errors":
                errs += int(val)
    if code != 0 and not (fails or errs):
        errs = 1  # crashed without a unittest summary
    return {"module": label, "ran": ran, "failures": fails, "errors": errs,
            "code": code, "output": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    ap.add_argument("--start", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args(argv)
    start = Path(args.start).resolve()
    cwd = Path.cwd()
    units = plan_units(start)
    if not units:
        print(f"no test modules found in {start}", file=sys.stderr)
        return 1
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        results = list(ex.map(lambda u: run_module(start, u[0], cwd, u[1]), units))
    elapsed = time.time() - t0
    ran = fails = errs = 0
    for r in results:
        ran += r["ran"]
        fails += r["failures"]
        errs += r["errors"]
        if r["code"] != 0:
            print("=" * 70)
            print(f"MODULE FAILED: {r['module']} (exit {r['code']})")
            print(r["output"].rstrip())
    print("-" * 70)
    print(f"Ran {ran} tests in {elapsed:.3f}s\n")
    bad = [r for r in results if r["code"] != 0]
    if not bad:
        print("OK")
        return 0
    parts = []
    if fails:
        parts.append(f"failures={fails}")
    if errs:
        parts.append(f"errors={errs}")
    print(f"FAILED ({', '.join(parts) or 'errors=1'})")
    print("Failing modules: " + ", ".join(r["module"] for r in bad))
    return 1


if __name__ == "__main__":
    sys.exit(main())
