"""The director's prompt carries the project's code-built digest, capped by [director] digest_chars (commonroom#345)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dispatch, handshake, registry
from commonroom.dispatch import fill_way, read_way
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with, issue

SHARE = "Share: alpha holds 30% of the allowance."


class DirectorDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")
        self.addCleanup(self.ledger.close)
        self.logged = []

    def register(self, path=None):
        (path or self.root).mkdir(exist_ok=True)
        (path or self.root).joinpath("room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        reg = registry.Registry()
        reg.projects["alpha"] = registry.Project("alpha", str(path or self.root), 30)
        registry.save(reg, Path(self.tmp.name) / registry.FILENAME)

    def dispatcher(self, chars=None):
        cfg = cfg_with(self.root)
        if chars is not None:
            cfg["director"]["digest_chars"] = chars
        return dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=self.logged.append)

    def prompt(self, d, kind="direct", role="director"):
        return d.prompt(dispatch.Job(kind, role, reason="steer"), None)

    def plain(self, d):
        return fill_way(d.ways["director"], repo="o/t", milestone=d.milestone, reason="steer")

    def test_unset_is_byte_identical(self):
        self.register()
        d = self.dispatcher()
        with mock.patch.object(handshake, "build_digest", side_effect=AssertionError("not called")):
            self.assertEqual(self.prompt(d), self.plain(d))
        self.assertEqual(self.logged, [])

    def test_set_adds_the_section_with_the_share_text(self):
        self.register()
        d = self.dispatcher(4000)
        with mock.patch.object(handshake, "build_digest", return_value=f"# Digest\n{SHARE}\n"):
            text = self.prompt(d)
        self.assertTrue(text.startswith(self.plain(d)))
        self.assertIn("## Project digest (built in code, no model call)", text)
        self.assertIn(SHARE, text)
        self.assertNotIn("left out", text)

    def test_stamp_only_skips_the_digest_without_a_log_line(self):
        self.register()
        d = self.dispatcher(4000)
        d.stamp_only = True
        with mock.patch.object(handshake, "build_digest", side_effect=AssertionError("not called")):
            self.assertEqual(self.prompt(d), self.plain(d))
        self.assertEqual(self.logged, [])
        d.stamp_only = False
        with mock.patch.object(handshake, "build_digest", return_value=SHARE):
            self.assertIn(SHARE, self.prompt(d))

    def test_the_real_digest_reaches_the_prompt(self):
        self.register()
        d = self.dispatcher(4000)
        home = Path(self.tmp.name) / "home"
        with mock.patch.object(registry, "default_path", return_value=Path(self.tmp.name) / registry.FILENAME),                 mock.patch.dict("os.environ", {"HOME": str(home), "USERPROFILE": str(home)}):
            text = self.prompt(d)
        self.assertEqual(self.logged, [])
        self.assertIn("## Project digest (built in code, no model call)", text)
        self.assertIn("alpha", text.split("## Project digest", 1)[1])
        self.assertIn("30%", text)

    def test_a_long_digest_is_cut_with_a_closing_line(self):
        self.register()
        d = self.dispatcher(100)
        with mock.patch.object(handshake, "build_digest", return_value="x" * 350):
            text = self.prompt(d)
        tail = text[len(self.plain(d)):]
        body = tail.split("\n## Project digest (built in code, no model call)\n")[1]
        digest, closing = body.split("\n\n")
        self.assertEqual(digest, "x" * 100)
        self.assertEqual(closing, "(250 characters left out; `room.py digest alpha` prints all of it.)")

    def test_a_missing_project_gives_todays_prompt_and_one_log_line(self):
        self.register(path=Path(self.tmp.name) / "other")
        d = self.dispatcher(4000)
        self.assertEqual(self.prompt(d), self.plain(d))
        self.assertEqual(len(self.logged), 1)

    def test_an_unreadable_registry_gives_todays_prompt(self):
        (Path(self.tmp.name) / registry.FILENAME).write_text("[projects.a\n", encoding="utf-8")
        d = self.dispatcher(4000)
        self.assertEqual(self.prompt(d), self.plain(d))
        self.assertEqual(len(self.logged), 1)

    def test_a_raising_build_gives_todays_prompt_and_one_log_line(self):
        self.register()
        d = self.dispatcher(4000)
        with mock.patch.object(handshake, "build_digest", side_effect=RuntimeError("boom")):
            self.assertEqual(self.prompt(d), self.plain(d))
        self.assertEqual(len(self.logged), 1)
        self.assertIn("boom", self.logged[0])

    def test_only_direct_jobs_change(self):
        self.register()
        on, off = self.dispatcher(4000), self.dispatcher()
        job = dispatch.Job("build", "builder", issue=issue(5, ["status:ready"]), reason="ready")
        role = dispatch.Job("build", "scout", issue=issue(5, ["status:ready"]), reason="ready")
        with mock.patch.object(handshake, "build_digest", return_value=SHARE):
            for d in (on, off):
                d.board.pr_for_branch = lambda branch: None
            for j in (job, role):
                self.assertEqual(on.prompt(j, "i5-x"), off.prompt(j, "i5-x"))

    def test_the_old_names_still_import(self):
        from commonroom.dispatch import fill_way as f, read_way as r
        self.assertIs(f, fill_way)
        self.assertIs(r, read_way)


if __name__ == "__main__":
    unittest.main()
