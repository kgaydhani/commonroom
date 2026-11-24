"""Every data file under commonroom/ must be in pyproject package-data, or an installed copy lacks it (commonroom#520)."""
import fnmatch
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "commonroom"


def data_files(package=PACKAGE):
    """Non-.py files under the package, as sorted posix paths relative to it."""
    return sorted(
        p.relative_to(package).as_posix()
        for p in package.rglob("*")
        if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
    )


def globs(pyproject=ROOT / "pyproject.toml"):
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]["commonroom"]


def unlisted(files, patterns):
    """Files matching none of the globs (`*` crosses no folder, as in setuptools)."""
    def hit(f):
        return any(fnmatch.fnmatchcase(f, pat) and f.count("/") == pat.count("/") for pat in patterns)
    return [f for f in files if not hit(f)]


class PackageDataTest(unittest.TestCase):
    def test_every_data_file_is_listed(self):
        self.assertEqual(unlisted(data_files(), globs()), [])

    def test_an_unlisted_file_is_named(self):
        files = data_files() + ["ways/new/thing.md"]
        self.assertEqual(unlisted(files, globs()), ["ways/new/thing.md"])

    def test_the_two_known_files_are_listed(self):
        self.assertEqual(unlisted(["static/dashboard.html", "ways/trials.toml"], globs()), [])
        self.assertEqual(unlisted(["static/dashboard.html", "ways/trials.toml"], globs()[:-2]),
                         ["static/dashboard.html", "ways/trials.toml"])


if __name__ == "__main__":
    unittest.main()
