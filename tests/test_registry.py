"""The registry of projects and their shares, and `room projects` (commonroom#225). Every test uses a temporary room
home through COMMONROOM_HOME, never ~/.commonroom."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import cli, registry
from commonroom.registry import Project, Registry, RegistryError


class Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.file = self.home / "projects.toml"

    def project_dir(self, name: str, room_toml: bool = True) -> str:
        folder = self.root / "work dir" / name
        folder.mkdir(parents=True)
        if room_toml:
            (folder / "room.toml").write_text("[project]\n", encoding="utf-8")
        return str(folder)   # on Windows a backslash path, which must round-trip

    def write(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(text.encode("utf-8"))


class RegistryFile(Home):
    def test_the_file_lives_in_the_room_home(self):
        self.assertEqual(registry.default_path(), self.home / "projects.toml")
        self.assertNotIn(str(Path.home() / ".commonroom"), str(registry.default_path()))

    def test_a_missing_file_is_an_empty_registry(self):
        reg = registry.load()
        self.assertEqual(reg.projects, {})
        self.assertEqual(reg.owner_share(), 100)
        self.assertFalse(self.file.exists())

    def test_round_trip_keeps_the_data_and_the_header(self):
        reg = Registry({"alpha": Project("alpha", self.project_dir("alpha"), 40, "fixed", "paced", True),
                        "beta": Project("beta", self.project_dir("beta"), 12.5, "open", "spare", False)},
                       {"reserve_5h": 10})
        registry.save(reg)
        raw = self.file.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith(registry.HEADER))
        loaded = registry.load()
        self.assertEqual(loaded, reg)
        self.assertEqual(loaded.owner_share(), 47.5)
        registry.save(loaded)
        self.assertEqual(self.file.read_bytes(), raw)   # stable: a second save writes the same bytes
        self.assertLess(text.index("[budget]"), text.index("[projects.alpha]"))
        self.assertLess(text.index("[projects.alpha]"), text.index("[projects.beta]"))

    def test_strings_with_quotes_and_backslashes_round_trip(self):
        reg = Registry({"alpha": Project("alpha", 'C:\\a "b"\\c\t', 10)})
        self.assertEqual(registry.parse(registry.dumps(reg)), reg)

    def test_each_rule_refuses_a_bad_file_and_names_the_problem(self):
        alpha, beta = self.project_dir("alpha"), self.project_dir("beta")
        bare = self.project_dir("bare", room_toml=False)

        def toml(**overrides):
            fields = {"path": alpha, "share": 40, "work": "open", "when": "paced", "strict": False} | overrides
            body = "\n".join(f"{k} = {registry._value(v)}" for k, v in fields.items())
            return f"[projects.alpha]\n{body}\n"

        cases = {
            f"project 'alpha': 'path' {self.root / 'nowhere'} does not exist": toml(path=str(self.root / "nowhere")),
            f"project 'alpha': 'path' {bare} holds no room.toml": toml(path=bare),
            "project 'alpha': 'share' must be a number from 0 to 100, not 101": toml(share=101),
            "project 'alpha': 'share' must be a number from 0 to 100, not -1": toml(share=-1),
            "project 'alpha': 'share' must be a number from 0 to 100, not 'ten'": toml(share="ten"),
            "project 'alpha': 'work' must be 'fixed' or 'open', not 'closed'": toml(work="closed"),
            "project 'alpha': 'when' must be 'paced' or 'spare', not 'now'": toml(when="now"),
            "project 'alpha': 'strict' must be true or false, not 'yes'": toml(strict="yes"),
            "shares sum to 110, more than 100": toml(share=60) + f"[projects.beta]\npath = {registry._value(beta)}\nshare = 50\n",
            "project 'alpha': the name is already used by 'Alpha'":
                f"[projects.Alpha]\npath = {registry._value(beta)}\nshare = 5\n" + toml(),
            "project 'alpha': unknown key 'colour'": toml(colour="red"),
            "project 'alpha': missing key 'share'": f"[projects.alpha]\npath = {registry._value(alpha)}\n",
            "not valid TOML": toml() + toml(),   # the same table twice
            "'reserve_5h' must be a number": "[budget]\nreserve_5h = 200\n",
        }
        for reason, text in cases.items():
            with self.subTest(reason=reason):
                self.write(text)
                with self.assertRaises(RegistryError) as caught:
                    registry.load()
                self.assertIn(reason, str(caught.exception))

    def test_save_refuses_a_bad_registry_and_leaves_the_file(self):
        registry.save(Registry({"alpha": Project("alpha", self.project_dir("alpha"), 40)}))
        before = self.file.read_bytes()
        with self.assertRaises(RegistryError):
            registry.save(Registry({"alpha": Project("alpha", self.project_dir("beta"), 140)}))
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(list(self.home.glob("*.tmp")), [])


class ProjectsCommand(Home):
    def room(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["projects", *argv])
        return code, out.getvalue() + err.getvalue()

    def test_add_set_list_and_remove(self):
        alpha, beta = self.project_dir("alpha"), self.project_dir("beta")
        self.assertEqual(self.room("add", "alpha", alpha, "--share", "40")[0], 0)
        self.assertEqual(self.room("add", "beta", beta, "--share", "20", "--when", "spare", "--strict")[0], 0)
        self.assertEqual(self.room("set", "alpha", "--share", "35", "--work", "fixed")[0], 0)
        self.assertEqual(self.room("set", "beta", "--no-strict")[0], 0)
        reg = registry.load()
        self.assertEqual(reg.projects["alpha"], Project("alpha", alpha, 35, "fixed", "paced", False))
        self.assertEqual(reg.projects["beta"], Project("beta", beta, 20, "open", "spare", False))
        code, out = self.room()
        self.assertEqual(code, 0)
        self.assertIn("alpha", out); self.assertIn("fixed/paced", out); self.assertIn(alpha, out)
        self.assertIn("owner's own slice: 45", out)
        self.assertEqual(self.room("remove", "beta")[0], 0)
        self.assertEqual(sorted(registry.load().projects), ["alpha"])

    def test_shared_folder_on_add_and_set_and_removed(self):
        from commonroom import handshake
        alpha, beta = self.project_dir("alpha"), self.project_dir("beta")
        plain = self.root / "plain"
        sync = self.root / "sync" / "alpha"
        (self.root / "sync").mkdir()
        self.assertEqual(self.room("add", "alpha", alpha, "--share", "10", "--shared", str(sync))[0], 0)
        self.assertEqual(self.room("add", "beta", beta, "--share", "10")[0], 0)
        reg = registry.load()
        self.assertEqual(reg.projects["alpha"].shared, str(sync))
        self.assertTrue(sync.is_dir())
        self.assertEqual(handshake.folder_of(reg.projects["alpha"]), sync)
        self.assertIsNone(reg.projects["beta"].shared)
        self.assertEqual(handshake.folder_of(reg.projects["beta"]), self.home / "projects" / "beta")
        code, out = self.room("set", "beta", "--shared", str(plain.parent / "b2"))
        self.assertEqual(code, 0)
        self.assertIn("created", out); self.assertIn("not copied", out)
        self.assertIn(str(self.home / "projects" / "beta"), out)
        self.assertEqual(registry.load().projects["beta"].shared, str(plain.parent / "b2"))
        self.assertEqual(self.room("set", "alpha", "--shared", "")[0], 0)
        reg = registry.load()
        self.assertIsNone(reg.projects["alpha"].shared)
        self.assertEqual(handshake.folder_of(reg.projects["alpha"]), self.home / "projects" / "alpha")

    def test_shared_relative_or_scheduled_is_refused(self):
        alpha = self.project_dir("alpha")
        self.room("add", "alpha", alpha, "--share", "10")
        before = self.file.read_bytes()
        for argv, reason in ((("set", "alpha", "--shared", "rel/dir"), "'shared' must be the full path"),
                             (("add", "b", alpha, "--share", "5", "--shared", "rel"), "'shared' must be the full path"),
                             (("set", "alpha", "--shared", str(self.root), "--at", "2030-01-01 10:00"), "can't be scheduled")):
            with self.subTest(argv=argv):
                code, out = self.room(*argv)
                self.assertEqual(code, 1)
                self.assertIn(reason, out)
                self.assertEqual(self.file.read_bytes(), before)

    def test_a_refused_command_leaves_the_file_unchanged(self):
        alpha, beta = self.project_dir("alpha"), self.project_dir("beta")
        self.room("add", "alpha", alpha, "--share", "60")
        before = self.file.read_bytes()
        cases = {
            "shares sum to 110": ("add", "beta", beta, "--share", "50"),
            "holds no room.toml": ("add", "beta", self.project_dir("bare", room_toml=False), "--share", "5"),
            "already exists": ("add", "alpha", beta, "--share", "5"),
            "must be a number from 0 to 100": ("set", "alpha", "--share", "120"),
            "no project 'gamma'": ("remove", "gamma"),
        }
        for reason, argv in cases.items():
            with self.subTest(reason=reason):
                code, out = self.room(*argv)
                self.assertEqual(code, 1)
                self.assertIn("refused", out)
                self.assertIn(reason, out)
                self.assertEqual(self.file.read_bytes(), before)

    def test_one_projects_missing_folder_does_not_block_a_change_to_another(self):
        alpha, beta = self.project_dir("alpha"), self.project_dir("beta")
        self.room("add", "alpha", alpha, "--share", "30")
        self.room("add", "beta", beta, "--share", "30")
        (Path(beta) / "room.toml").unlink()
        Path(beta).rmdir()
        code, out = self.room("set", "alpha", "--share", "40")
        self.assertEqual(code, 0)
        self.assertIn(f"warning: project 'beta': 'path' {beta} does not exist", out)
        self.assertEqual(registry.load(check=False).projects["alpha"].share, 40)
        gamma = self.project_dir("gamma")
        self.assertEqual(self.room("add", "gamma", gamma, "--share", "10")[0], 0)
        # The broken project itself, and a whole-registry rule, are still refused.
        before = self.file.read_bytes()
        code, out = self.room("set", "beta", "--share", "5")
        self.assertEqual((code, self.file.read_bytes()), (1, before))
        self.assertIn("refused: project 'beta': 'path'", out)
        code, out = self.room("set", "gamma", "--share", "50")
        self.assertEqual((code, self.file.read_bytes()), (1, before))
        self.assertIn("shares sum to 120", out)
        # A bare list still names every problem and fails; removing the broken project fixes it.
        code, out = self.room()
        self.assertEqual(code, 1)
        self.assertIn("problem: project 'beta'", out)
        self.assertEqual(self.room("remove", "beta")[0], 0)
        self.assertEqual(self.room()[0], 0)

    def test_bare_list_with_no_file(self):
        code, out = self.room()
        self.assertEqual(code, 0)
        self.assertIn("no projects", out)
        self.assertIn("owner's own slice: 100", out)
        self.assertFalse(self.file.exists())

    def test_the_command_calls_no_model_and_no_github(self):
        boom = mock.Mock(side_effect=AssertionError("a process was started"))
        with mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom):
            self.assertEqual(self.room("add", "alpha", self.project_dir("alpha"), "--share", "10")[0], 0)
            self.assertEqual(self.room()[0], 0)
        boom.assert_not_called()


if __name__ == "__main__":
    unittest.main()
