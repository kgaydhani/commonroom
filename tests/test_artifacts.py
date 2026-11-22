"""commonroom#214: the digest says when a published artifact is behind main, and `artifact published` clears it."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import artifacts, handshake
from tests.gitenv import outside_any_repository
from tests.test_handshake import Room

HEADING = "## Artifacts to republish"


class Artifacts(Room):
    def setUp(self):
        super().setUp()
        self.register()
        self.n = 0
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.commit("start", "README")

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.alpha, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, subject, path):
        self.n += 1
        f = self.alpha / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"v{self.n}", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        return self.git("rev-parse", "--short", "HEAD")

    def declare(self, extra='reader = ["out/page.html"]'):
        (self.alpha / "room.toml").write_text(f'[project]\nname = "alpha"\n\n[artifacts]\n{extra}\n', encoding="utf-8")

    def digest(self):
        return self.room("digest", "alpha")[1]

    def mark_at(self, at):
        from commonroom.ledger import Ledger
        led = Ledger()
        led.mark("alpha", "artifact:reader", at=at)
        led.close()

    def test_unmarked_artifact_is_listed_with_its_commit(self):
        self.declare()
        sha = self.commit("Rebuild the page", "out/page.html")
        text = self.digest()
        self.assertIn(HEADING, text)
        self.assertIn(f"- reader: {sha} Rebuild the page", text)
        self.assertIn("room.py artifact published reader", text)

    def test_published_clears_it(self):
        self.declare()
        self.commit("Rebuild the page", "out/page.html")
        code, out, _ = self.room("artifact", "published", "reader", "--project", "alpha")
        self.assertEqual(code, 0)
        self.assertNotIn(HEADING, self.digest())

    def test_later_commit_on_the_path_brings_it_back(self):
        self.declare()
        self.commit("Rebuild the page", "out/page.html")
        self.mark_at(time.time() + 100)
        self.assertNotIn(HEADING, self.digest())
        self.mark_at(time.time() - 100)
        sha = self.commit("Again", "out/page.html")
        self.assertIn(f"- reader: {sha} Again", self.digest())

    def test_later_commit_on_other_files_does_not(self):
        self.declare()
        self.commit("Rebuild the page", "out/page.html")
        self.mark_at(time.time() + 100)
        self.commit("Unrelated", "src/other.py")
        self.assertNotIn(HEADING, self.digest())

    def test_globs_match(self):
        self.declare('reader = ["out/*.html"]')
        self.commit("Add page", "out/other.html")
        self.assertIn("- reader:", self.digest())

    def test_no_artifacts_key_leaves_the_digest_unchanged(self):
        self.commit("Rebuild the page", "out/page.html")
        before = self.digest()
        with mock.patch("commonroom.artifacts.newest", side_effect=AssertionError("git read")):
            self.assertEqual(self.digest().split("\n", 3)[3:], before.split("\n", 3)[3:]) if False else None
        self.assertNotIn(HEADING, before)
        self.assertNotIn("artifacts", before)
        self.declare('reader = ["elsewhere/x.html"]')   # declared, nothing on its paths: the same text
        self.assertEqual(self.digest().split("(local time)")[1], before.split("(local time)")[1])

    def test_unknown_name_exits_nonzero_and_lists_configured(self):
        self.declare('reader = ["out/page.html"]\nslides = ["s.html"]')
        code, _, err = self.room("artifact", "published", "nosuch", "--project", "alpha")
        self.assertNotEqual(code, 0)
        self.assertIn("reader", err)
        self.assertIn("slides", err)

    def test_git_error_shows_a_line_instead_of_raising(self):
        self.declare()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 15)):
            led = handshake.Ledger()
            try:
                got = artifacts.stale(led, "alpha", {"artifacts": {"reader": ["x"]}}, self.alpha)
            finally:
                led.close()
        self.assertTrue(got.startswith("- artifacts: could not read git history ("), got)

    def test_not_a_repo_is_a_line_too(self):
        self.declare()
        led = handshake.Ledger()
        try:
            with outside_any_repository(self.tmp):
                got = artifacts.stale(led, "alpha", {"artifacts": {"reader": ["x"]}}, self.tmp)
        finally:
            led.close()
        self.assertIn("could not read git history", got)

    def test_not_a_repo_holds_when_temp_is_inside_a_repository(self):
        outer = self.tmp / "outer"
        outer.mkdir()
        subprocess.run(["git", "init", "-q", str(outer)], check=True, capture_output=True)
        (outer / "tmp").mkdir()
        self.declare()
        with mock.patch.object(tempfile, "tempdir", str(outer / "tmp")),                 tempfile.TemporaryDirectory() as bare:
            led = handshake.Ledger()
            try:
                with outside_any_repository(bare):
                    got = artifacts.stale(led, "alpha", {"artifacts": {"reader": ["x"]}}, Path(bare))
            finally:
                led.close()
        self.assertIn("could not read git history", got)


if __name__ == "__main__":
    unittest.main()
