import sys
import os
import tempfile
import unittest
from pathlib import Path

from commonroom import doctor

TOML = '[project]\nname = "{n}"\n[roles]\ncodex_parallel_builds = false\nbuilder = ["{p}:sonnet"]\n'


CHECKOUT = Path("D:/rooms/main")
HEAD = '"HostName","TaskName","Status","Task To Run","Scheduled Task State"\n'
BS = chr(92)
VBS = ("wscript.exe \"D:/rooms/main/beat.vbs\"").replace("/", BS)


def sched(action=VBS, state="Enabled", code=0, rows=None):
    text = HEAD + (rows if rows is not None else f'"PC","{BS}Beat","Ready","{action}","{state}"' + chr(10))
    return lambda args: (code, text)


HB = ("wscript.exe \"D:/rooms/main/heartbeat.vbs\" D:/proj").replace("/", BS)


def tree(root: Path):
    return sorted(str(p) for p in root.rglob("*"))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.reg = self.home / "projects.toml"
        self.lines = []

    def project(self, name, provider="claude", toml=True, agent=True):
        root = self.home / name
        root.mkdir()
        if toml:
            (root / "room.toml").write_text(TOML.format(n=name, p=provider), encoding="utf-8")
        if agent:
            (root / ".claude" / "agents").mkdir(parents=True)
            (root / ".claude" / "agents" / "builder.md").write_text("---\nname: builder\n---\nx", encoding="utf-8")
        return root

    def registry(self, *projects, share=10):
        text = "".join(f'[projects.{r.name}]\npath = "{r.as_posix()}"\nshare = {share}\n' for r in projects)
        self.reg.write_text(text, encoding="utf-8")

    def run_doctor(self, tools=("claude", "gh", "codex"), gh_code=0, runner=None):
        code = doctor.run(self.reg, which=lambda n: n if n in tools else None, gh=lambda a: gh_code,
                          version=(3, 12), out=self.lines.append, runner=runner or sched(), windows=True,
                          checkout=CHECKOUT)
        return code

    def test_all_present(self):
        self.registry(self.project("alpha"))
        before = tree(self.home)
        code = self.run_doctor(tools=("claude", "gh"))
        self.assertEqual(code, 0, self.lines)
        self.assertTrue(all(l.endswith(": ok") for l in self.lines), self.lines)
        self.assertEqual(tree(self.home), before)   # wrote nothing

    def test_missing_gh(self):
        self.registry(self.project("alpha"))
        self.assertEqual(self.run_doctor(tools=("claude",)), 1)
        self.assertTrue(any("gh on PATH: missing - install the GitHub CLI" in l for l in self.lines))

    def test_gh_not_logged_in(self):
        self.registry(self.project("alpha"))
        self.assertEqual(self.run_doctor(tools=("claude", "gh"), gh_code=1), 1)
        self.assertTrue(any("gh auth login" in l for l in self.lines))

    def test_registry_over_100(self):
        self.registry(self.project("alpha"), self.project("beta"), share=60)
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("registry valid: missing" in l and "more than 100" in l for l in self.lines))

    def test_project_without_room_toml(self):
        self.registry(self.project("alpha", toml=False, agent=False))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("project alpha room.toml: missing - add a room.toml" in l for l in self.lines))

    def test_role_without_agent_file(self):
        self.registry(self.project("alpha", agent=False))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("alpha roles: missing" in l and "builder.md" in l for l in self.lines))

    def test_codex_only_needed_when_named(self):
        self.registry(self.project("alpha"))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 0)
        self.assertFalse(any("codex" in l for l in self.lines))
        self.lines.clear()
        self.reg.unlink()
        self.registry(self.project("beta", provider="codex"))
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("codex on PATH: missing" in l for l in self.lines))

    def write_toml(self, root, extra="", roles_extra=""):
        (root / "room.toml").write_text(
            f'[project]\nname = "{root.name}"\n{extra}[roles]\n{roles_extra}builder = ["claude:sonnet"]\n', encoding="utf-8")

    def test_codex_parallel_builds_default_counts(self):
        root = self.project("alpha")
        self.write_toml(root)
        self.registry(root)
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 1)
        self.assertTrue(any("codex on PATH: missing" in l and "codex_parallel_builds" in l for l in self.lines))

    def test_codex_opt_outs(self):
        root = self.project("alpha")
        self.write_toml(root, extra="[parallel]\ncodex_slots = 0\n")
        self.registry(root)
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 0, self.lines)
        self.assertFalse(any("codex" in l for l in self.lines))
        self.lines.clear()
        self.write_toml(root)
        self.registry(root, share=0)
        self.assertEqual(self.run_doctor(tools=("claude", "gh")), 0, self.lines)

    def test_codex_named_hint_gives_reason(self):
        self.registry(self.project("beta", provider="codex"))
        self.run_doctor(tools=("claude", "gh"))
        self.assertTrue(any("codex on PATH: missing" in l and "[roles] list names codex" in l for l in self.lines))

    def test_old_python(self):
        self.registry(self.project("alpha"))
        code = doctor.run(self.reg, which=lambda n: n, gh=lambda a: 0, version=(3, 10), out=self.lines.append,
                          runner=sched(), windows=True, checkout=CHECKOUT)
        self.assertEqual(code, 1)

    def beat_line(self, **kw):
        if not self.reg.exists():
            self.registry(self.project("alpha"))
        code = self.run_doctor(tools=("claude", "gh"), runner=sched(**kw))
        return code, [l for l in self.lines if l.startswith("scheduled beat")]

    def test_beat_ok(self):
        code, line = self.beat_line()
        self.assertEqual((code, line), (0, ["scheduled beat: ok"]))
        self.assertEqual(self.beat_line(action="D:/rooms/main/beat.cmd")[0], 0)

    def test_beat_missing_cases(self):
        for kw in ({"rows": ""}, {"state": "Disabled"}, {"action": VBS.replace("main", "other")}, {"code": 1}, {"code": 127}):
            self.lines.clear()
            code, line = self.beat_line(**kw)
            self.assertEqual(code, 1, kw)
            self.assertTrue(line and "missing" in line[0], (kw, line))

    def test_lone_heartbeat_task_is_ok_with_note(self):
        code, line = self.beat_line(action=HB)
        self.assertEqual(code, 0, self.lines)
        self.assertEqual(line, ["scheduled beat: ok (per-project heartbeat; beat.vbs replaces it (see room schedule))"])
        self.lines.clear()
        self.assertEqual(self.beat_line(action=HB, state="Disabled")[0], 1)
        self.lines.clear()
        self.assertEqual(self.beat_line(action=HB.replace("main", "other"))[0], 1)

    def test_beat_plus_heartbeat_conflicts(self):
        nl = chr(10)
        beat = f'"PC","{BS}Beat","Ready","{VBS}","Enabled"' + nl
        hb = lambda n, st="Enabled": f'"PC","{BS}{n}","Ready","{HB}","{st}"' + nl
        code, line = self.beat_line(rows=beat + hb("HB alpha") + hb("HB beta"))
        self.assertEqual(code, 1)
        self.assertTrue("conflict" in line[0] and "HB alpha" in line[0] and "HB beta" in line[0], line)
        self.lines.clear()
        self.assertEqual(self.beat_line(rows=beat + hb("HB alpha", "Disabled"))[0], 0)

    def test_missing_fix_names_heartbeat_and_beat(self):
        self.beat_line(rows="")
        line = [l for l in self.lines if l.startswith("scheduled beat")][0]
        self.assertIn("beat.vbs", line)
        self.assertIn("disable any per-project heartbeat tasks", line)

    def test_run_cmd_survives_non_utf8_output(self):
        code, text = doctor.run_cmd([sys.executable, "-c",
                                     "import sys; sys.stdout.buffer.write(bytes([97, 98, 148, 99, 100])); sys.stdout.flush()"])
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("ab") and text.endswith("cd"), text)

    def test_beat_cron(self):
        self.registry(self.project("alpha"))
        def run(text, code=0):
            self.lines.clear()
            return doctor.run(self.reg, which=lambda n: n, gh=lambda a: 0, version=(3, 12), out=self.lines.append,
                              runner=lambda a: (code, text), windows=False, checkout=CHECKOUT)
        self.assertEqual(run("*/5 * * * * python D:/rooms/main/room.py beat"), 0, self.lines)
        self.assertEqual(run("# */5 * * * * python D:/rooms/main/room.py beat"), 1)
        self.assertEqual(run("", code=127), 1)


if __name__ == "__main__":
    unittest.main()
