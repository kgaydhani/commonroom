"""commonroom#247: `room beat`, one beat over every registered project."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from commonroom import beat, registry
from commonroom.lock import claim_lock

REPO = Path(__file__).resolve().parent.parent
HEARTBEAT = {
    "heartbeat.cmd": "98f219b1321e5b3bc73e5c8b994d01bc146ad17ebf0165405bebbf1bf474da43",
    "heartbeat.vbs": "cdb0579aadc93c46a818ea27037f123a1cbd8adab38d27b109174a038965e3b4",
}


class FakeProc:
    def wait(self):
        pass


class Beat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.calls, self.lines = [], []

    def project(self, name, toml="", make=True):
        folder = Path(self.tmp.name) / name
        if make:
            folder.mkdir()
            (folder / "room.toml").write_text(f'[project]\nname = "{name}"\n{toml}', encoding="utf-8")
        return str(folder)

    def starter(self, argv, cwd, log, wait):
        self.calls.append((argv[2:], cwd, log.name, wait))
        return FakeProc()

    def run_beat(self, reg):
        return beat.beat(self.home, reg, starter=self.starter, log=self.lines.append, python="py")

    def test_two_projects(self):
        a, b = self.project("alpha", "[parallel]\nclaude_slots = 1\ncodex_slots = 1\n"), self.project("beta")
        reg = registry.Registry({"alpha": registry.Project("alpha", a, 30), "beta": registry.Project("beta", b, 20)})
        self.assertEqual(self.run_beat(reg), 0)
        run = lambda p, s=None: ["run", "--until-idle", "--provider", p] + (["--slot", s] if s else [])
        self.assertEqual(self.calls, [
            (["probe"], str(self.home), "beat-probe.log", True),
            (["services"], a, "beat-alpha-services.log", True), (["tick"], a, "beat-alpha-tick.log", True),
            (run("claude"), a, "beat-alpha-claude.log", False), (run("codex"), a, "beat-alpha-codex.log", False),
            (["housekeep"], a, "beat-alpha-housekeep.log", False),
            (["services"], b, "beat-beta-services.log", True), (["tick"], b, "beat-beta-tick.log", True),
            (run("claude"), b, "beat-beta-claude.log", False), (run("claude", "2"), b, "beat-beta-claude2.log", False),
            (run("codex"), b, "beat-beta-codex.log", False), (run("codex", "2"), b, "beat-beta-codex2.log", False),
            (["housekeep"], b, "beat-beta-housekeep.log", False),
        ])
        self.assertFalse((self.home / "locks" / "beat.lock").exists())

    def test_zero_share_has_no_dispatcher(self):
        a = self.project("alpha")
        self.run_beat(registry.Registry({"alpha": registry.Project("alpha", a, 0)}))
        self.assertEqual([c[0][0] for c in self.calls], ["probe", "services", "tick", "housekeep"])

    def test_missing_folder_is_skipped(self):
        gone = self.project("gone", make=False)
        b = self.project("beta")
        self.run_beat(registry.Registry({"gone": registry.Project("gone", gone, 10), "beta": registry.Project("beta", b, 10)}))
        self.assertEqual(sum("gone" in c[2] for c in self.calls), 0)
        self.assertTrue(any(c[1] == b for c in self.calls))
        self.assertEqual(len([l for l in self.lines if "skipping gone" in l]), 1)

    def test_missing_room_toml_is_skipped(self):
        folder = Path(self.tmp.name) / "bare"
        folder.mkdir()
        self.run_beat(registry.Registry({"bare": registry.Project("bare", str(folder), 10)}))
        self.assertEqual([c[0] for c in self.calls], [["probe"]])
        self.assertEqual(len([l for l in self.lines if "skipping bare" in l]), 1)

    def test_second_beat_while_locked_starts_nothing(self):
        a = self.project("alpha")
        lock = self.home / "locks" / "beat.lock"
        self.assertIsNone(claim_lock(lock))   # held by this (live) process
        self.assertEqual(self.run_beat(registry.Registry({"alpha": registry.Project("alpha", a, 10)})), 0)
        self.assertEqual(self.calls, [])
        self.assertTrue(lock.exists())

    def test_empty_registry_starts_nothing(self):
        self.assertEqual(self.run_beat(registry.Registry()), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_beat(None), 0)   # no projects.toml at all
        self.assertEqual(self.calls, [])
        self.assertTrue(any("nothing started" in l for l in self.lines))


class HeartbeatUnchanged(unittest.TestCase):
    def test_files_are_what_they_are_on_main(self):
        for name, digest in HEARTBEAT.items():
            data = (REPO / name).read_bytes().replace(b"\r\n", b"\n")   # the tree may show CRLF (core.autocrlf)
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()
