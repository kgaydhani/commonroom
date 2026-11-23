"""Run the test suite one module per process, in parallel (standard library only).

    python tests/parallel.py [--jobs N] [--start DIR]

Discovers the same test_*.py modules as `python -m unittest discover -s tests`,
runs each with unittest in its own process, and prints unittest-style output:
failures with tracebacks and module names, then `Ran N tests in Xs` and
`OK` or `FAILED (...)`. Exits 0 only when every module passed.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
COUNTS = re.compile(r"^FAILED \((.*)\)\s*$", re.M)


def run_module(start: Path, name: str, cwd: Path) -> dict:
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(start), "-p", name]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out, code = p.stderr + p.stdout, p.returncode
    except OSError as exc:
        out, code = f"could not start: {exc}\n", 1
    m = RAN.search(out)
    ran = int(m.group(1)) if m else 0
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
    return {"module": name, "ran": ran, "failures": fails, "errors": errs,
            "code": code, "output": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    ap.add_argument("--start", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args(argv)
    start = Path(args.start).resolve()
    cwd = Path.cwd()
    names = sorted(p.name for p in start.glob("test_*.py"))
    if not names:
        print(f"no test modules found in {start}", file=sys.stderr)
        return 1
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        results = list(ex.map(lambda n: run_module(start, n, cwd), names))
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
