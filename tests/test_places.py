"""Places (commonroom#570): folders worked in, found from session records, kept in the room home."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, places, registry


def line(cwd, session, ts):
    return json.dumps({"type": "assistant", "sessionId": session, "cwd": str(cwd), "timestamp": ts,
                       "message": {"id": "m" + ts, "usage": {"input_tokens": 1}}}) + "\n"


class PlacesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.records, self.work = self.root / "home", self.root / "claude" / "projects", self.root / "work"
        for d in (self.home, self.records, self.work):
            d.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home), "HOME": str(self.root),
                                           "USERPROFILE": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        # temp folders are left out, so the scratch work area stands in for the owner's folders via a fake temp dir
        patch = mock.patch("tempfile.gettempdir", return_value=str(self.root / "tmpdir"))
        patch.start()
        self.addCleanup(patch.stop)
        self.proj = self.work / "proj"
        self.proj.mkdir()
        (self.proj / "room.toml").write_text("[project]\nname='proj'\n")
        (self.work / "proj-wt" / "room" / "b").mkdir(parents=True)
        self.reg = registry.Registry(projects={"proj": registry.Project("proj", str(self.proj), 50)})

    def session(self, folder, sid, ts, name=None):
        d = self.records / (name or str(folder).replace(os.sep, "-").replace(":", "-"))
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{sid}.jsonl", "a", encoding="utf-8", newline="") as f:
            f.write(line(folder, sid, ts))

    def make(self, name):
        d = self.work / name
        d.mkdir(exist_ok=True)
        return d

    def test_found_counted_and_ordered(self):
        a, b = self.make("a"), self.make("b")
        self.session(a, "s1", "2026-11-01T10:00:00+00:00")
        self.session(a, "s2", "2026-11-02T10:00:00+00:00")
        self.session(b, "s3", "2026-11-05T10:00:00+00:00")
        rows = places.listing(places.discover(self.reg, self.records))
        self.assertEqual([r["name"] for r in rows], ["b", "a"])
        self.assertEqual([r["sessions"] for r in rows], [1, 2])
        self.assertTrue(all(r["exists"] and r["description"] == "" for r in rows))

    def test_left_out(self):
        self.session(self.proj, "p1", "2026-11-01T10:00:00+00:00")
        self.session(self.proj / "sub", "p2", "2026-11-01T10:00:00+00:00")
        self.session(self.work / "proj-wt" / "room" / "b", "w1", "2026-11-01T10:00:00+00:00")
        self.session(self.root / "tmpdir" / "x", "t1", "2026-11-01T10:00:00+00:00")
        other = self.make("other")
        (other / "room.toml").write_text("")
        self.session(other, "o1", "2026-11-01T10:00:00+00:00")
        self.assertEqual(places.listing(places.discover(self.reg, self.records)), [])

    def test_kept_after_records_pruned_and_gone_marked(self):
        a = self.make("a")
        self.session(a, "s1", "2026-11-01T10:00:00+00:00")
        places.discover(self.reg, self.records)
        for f in self.records.rglob("*.jsonl"):
            f.unlink()
        a.rmdir()
        rows = places.listing(places.discover(self.reg, self.records))
        self.assertEqual((len(rows), rows[0]["sessions"], rows[0]["exists"]), (1, 1, False))

    def test_rediscovery_does_not_double_count(self):
        a = self.make("a")
        self.session(a, "s1", "2026-11-01T10:00:00+00:00")
        places.discover(self.reg, self.records)
        self.session(a, "s1", "2026-11-03T10:00:00+00:00")
        self.assertEqual(places.listing(places.discover(self.reg, self.records))[0]["sessions"], 1)

    def test_describe_survives_discovery_and_cli(self):
        a = self.make("a")
        self.session(a, "s1", "2026-11-01T10:00:00+00:00")
        with mock.patch.object(places.sessions, "default_dir", return_value=self.records), \
                mock.patch.object(places.registry, "load", return_value=self.reg):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["places", "describe", "a", "  my  notes "]), 0)
                self.assertEqual(cli.main(["places"]), 0)
            self.assertIn("my notes", out.getvalue())
            self.assertNotIn("[gone]", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["places", "describe", "nope", "x"]), 1)
                self.assertEqual(cli.main(["places", "describe", "a"]), 1)

    def test_scratch_folder_unchanged(self):
        a = self.make("a")
        (a / "f.txt").write_text("x")
        self.session(a, "s1", "2026-11-01T10:00:00+00:00")
        before = sorted((str(p), p.stat().st_mtime) for p in a.rglob("*")) + [(str(a), a.stat().st_mtime)]
        places.discover(self.reg, self.records)
        after = sorted((str(p), p.stat().st_mtime) for p in a.rglob("*")) + [(str(a), a.stat().st_mtime)]
        self.assertEqual(before, after)

    def test_registry_and_pie_unchanged_with_places(self):
        reg_path = self.home / "projects.toml"
        reg_path.write_text("[projects.proj]\npath = " + registry._string(str(self.proj)) + "\nshare = 40\n",
                            encoding="utf-8")
        before = (registry.load(reg_path), )
        self.session(self.make("a"), "s1", "2026-11-01T10:00:00+00:00")
        places.discover(self.reg, self.records)
        after = registry.load(reg_path)
        self.assertEqual((after,), before)
        self.assertEqual((list(after.projects), after.owner_share(), registry.problems(after)), (["proj"], 60, []))
        self.assertEqual(self.reg.owner_share(), 50)
        self.assertNotIn("a", self.reg.projects)


if __name__ == "__main__":
    unittest.main()
