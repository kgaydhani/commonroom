"""The repo stores LF (commonroom#513): no tracked text file may hold a CR in the git index."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import outside_any_repository

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = (".py", ".md", ".toml", ".txt", ".json")
GIT = shutil.which("git")


def files_with_cr(repo):
    """Tracked files of the given suffixes whose index content holds a CR, as sorted paths."""
    listing = subprocess.run(["git", "-C", str(repo), "ls-files", "--eol"], capture_output=True, text=True, check=True)
    bad = []
    for line in listing.stdout.splitlines():
        info, _, path = line.partition("\t")
        if not path.endswith(SUFFIXES):
            continue
        index_eol = info.split()[0]
        if index_eol in ("i/crlf", "i/mixed", "i/cr"):
            bad.append(path)
    return sorted(bad)


@unittest.skipUnless(GIT, "git is not available")
class LineEndingTests(unittest.TestCase):
    def test_index_holds_no_cr(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        self.assertEqual(files_with_cr(ROOT), [], "tracked files with CR in the index")

    def test_gitattributes_sets_lf(self):
        text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("* text=auto eol=lf", text.splitlines())

    def test_check_flags_crlf_in_a_temporary_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            with outside_any_repository(repo):
                def git(*a):
                    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
                git("init", "-q")
                git("config", "core.autocrlf", "false")
                (repo / "bad.py").write_bytes(b"x = 1\r\ny = 2\r\n")
                (repo / "good.py").write_bytes(b"x = 1\n")
                (repo / "bad.bin").write_bytes(b"a\r\nb\r\n")
                git("add", ".")
                self.assertEqual(files_with_cr(repo), ["bad.py"])


if __name__ == "__main__":
    unittest.main()
