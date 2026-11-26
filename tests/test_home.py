"""The home place (commonroom#579): made once in the room home, no project, shown by `room places`, own session start."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, home, hook, person, places, registry


class HomeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "roomhome"
        (self.root / "claude" / "projects").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home), "HOME": str(self.root),
                                           "USERPROFILE": str(self.root), "COMMONROOM_WORKER": ""})
        env.start()
        self.addCleanup(env.stop)
        patch = mock.patch("tempfile.gettempdir", return_value=str(self.root / "tmpdir"))
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_created_once_with_the_map_and_person_file_by_pointer(self):
        where, made = home.create()
        self.assertTrue(made)
        self.assertEqual(where, self.home / "home")
        text = (where / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(places.path_file().as_posix(), text)
        self.assertIn(person.path(self.home).as_posix(), text)
        self.assertIn("never to do project work", text)
        self.assertNotIn("\r", (where / "CLAUDE.md").read_bytes().decode())

    def test_idempotent_and_never_rewrites_the_file(self):
        where, _ = home.create()
        (where / "CLAUDE.md").write_text("owner edit\n", encoding="utf-8")
        again, made = home.create()
        self.assertFalse(made)
        self.assertEqual((where / "CLAUDE.md").read_text(encoding="utf-8"), "owner edit\n")
        self.assertEqual(len([p for p in places.load()["places"].values() if p.get("home")]), 1)

    def test_no_project_is_registered(self):
        home.create()
        self.assertFalse((self.home / "registry.toml").exists() and registry.load(check=False).projects)
        self.assertEqual(registry.load(check=False).projects, {})

    def test_room_places_shows_it_as_the_home_place_and_discovery_keeps_it(self):
        code, out = self.run_cli("home")
        self.assertEqual(code, 0)
        code, out = self.run_cli("home")
        self.assertIn("already there", out)
        code, out = self.run_cli("places")
        self.assertEqual(out.count("[home place]"), 1)
        self.assertTrue(places.load()["places"][next(iter(places.load()["places"]))]["home"])

    def test_session_start_in_the_home_place_says_the_job(self):
        where, _ = home.create()
        if True:
            text = hook.place_line({"cwd": str(where)})
        self.assertIn("home place", text)
        self.assertIn("never do project work", text)
        self.assertLessEqual(len(text.splitlines()), 2)

    def test_another_place_keeps_its_session_start(self):
        other = self.root / "other"
        other.mkdir()
        data = places.load()
        data["places"][places.sessions.path_key(other)] = {"path": str(other), "last": 1.0, "sessions": ["s"], "description": ""}
        places.save(data)
        text = hook.place_line({"cwd": str(other)})
        self.assertIn("a place Common Room knows", text)
        self.assertNotIn("home place", text)


if __name__ == "__main__":
    unittest.main()
