"""`room install` / `room uninstall` (commonroom#566): scratch room home and scratch Claude config dir only."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from commonroom import install, wire

OTHER = ('{\n    "model": "opus",\n    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},\n'
         '    "enabledPlugins": {"other@m": true},\n    "zeta": 1,\n    "alpha": 2\n}\n')


class FakeScheduler:
    """Stands in for schtasks / crontab: nothing real is registered. `refuse` makes create fail."""
    def __init__(self, refuse=False):
        self.calls, self.registered, self.refuse, self.cron = [], False, refuse, ""
        self.table = ""

    def __call__(self, command, stdin=None):
        self.calls.append(command)
        if command.startswith("schtasks /create") or command.startswith("(crontab -l"):
            if self.refuse:
                return 1, "ERROR: Access is denied."
            self.registered = True
            self.cron = (install.checkout() / "room.py").as_posix() + " beat\n"
            return 0, ""
        if command == "schtasks /query /fo csv /v":
            return 0, self.table
        if command.startswith("schtasks /query"):
            return (0, "") if self.registered else (1, "ERROR: The system cannot find the file specified.")
        if command == "crontab -l":
            return (0, self.cron) if self.registered else (1, "no crontab")
        if command.startswith("schtasks /delete") or command == "crontab -":
            self.registered = False
            return 0, ""
        raise AssertionError(command)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.sched = FakeScheduler()
        patch = mock.patch.object(wire, "run", self.sched)
        patch.start()
        self.addCleanup(patch.stop)
        self.codex = t / "codex"
        self.codex_hooks = self.codex / "hooks.json"
        self.home, self.claude = t / "home", t / "claude"
        self.settings = self.claude / "settings.json"
        old = {k: os.environ.get(k) for k in ("COMMONROOM_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        os.environ["COMMONROOM_HOME"], os.environ["CLAUDE_CONFIG_DIR"] = str(self.home), str(self.claude)
        os.environ["CODEX_HOME"] = str(self.codex)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        self.addCleanup(self.tmp.cleanup)

    def run_cmd(self, fn, dry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(argparse.Namespace(dry_run=dry))
        return code, out.getvalue()

    def put(self, text):
        self.claude.mkdir(parents=True, exist_ok=True)
        self.settings.write_bytes(text.encode())

    def test_adds_plugin_and_status_line_and_keeps_other_content(self):
        self.put(OTHER)
        code, out = self.run_cmd(install.cmd_install)
        self.assertEqual(code, 0)
        data = json.loads(self.settings.read_text())
        before = json.loads(OTHER)
        for k, v in before.items():
            if k != "enabledPlugins":
                self.assertEqual(data[k], v)
        self.assertEqual(data["enabledPlugins"], {"other@m": True, "commonroom@commonroom": True})
        self.assertIn("room.py tap", data["statusLine"]["command"])
        self.assertEqual(data["extraKnownMarketplaces"]["commonroom"]["source"]["source"], "directory")
        self.assertEqual(list(data)[:5], list(before))          # existing key order survives
        self.assertIn("    ", self.settings.read_text())        # indent kept
        self.assertEqual((self.home / "room-path").read_text().strip(), (install.checkout() / "room.py").as_posix())
        self.assertIn("add    statusLine", out)

    def test_dry_run_prints_and_writes_nothing(self):
        self.put(OTHER)
        code, out = self.run_cmd(install.cmd_install, dry=True)
        self.assertEqual(self.settings.read_bytes(), OTHER.encode())
        self.assertFalse(self.home.exists())
        self.assertIn("add    enabledPlugins.commonroom@commonroom", out)
        self.assertIn("Dry run", out)

    def test_twice_makes_no_second_change(self):
        self.put(OTHER)
        self.run_cmd(install.cmd_install)
        first = self.settings.read_bytes()
        manifest = (self.home / "install.json").read_bytes()
        code, out = self.run_cmd(install.cmd_install)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertEqual((self.home / "install.json").read_bytes(), manifest)
        self.assertIn("Nothing to change", out)

    def test_dry_run_lists_room_path_and_writes_none(self):
        _, out = self.run_cmd(install.cmd_install, dry=True)
        self.assertIn(f"Room home: {self.home / 'room-path'}", out)
        self.assertIn("write  " + (install.checkout() / "room.py").as_posix(), out)
        self.assertFalse((self.home / "room-path").exists())

    def test_moved_checkout_rewrites_room_path_and_uninstall_removes_it(self):
        self.run_cmd(install.cmd_install)
        moved = Path(self.tmp.name) / "moved"
        with mock.patch.object(install, "checkout", return_value=moved):
            _, out = self.run_cmd(install.cmd_install)
        self.assertEqual((self.home / "room-path").read_text(encoding="utf-8").strip(), (moved / "room.py").as_posix())
        self.assertIn("write  " + (moved / "room.py").as_posix(), out)
        self.run_cmd(install.cmd_uninstall)
        self.assertFalse((self.home / "room-path").exists())

    def test_twice_leaves_room_path_alone(self):
        self.run_cmd(install.cmd_install)
        f = self.home / "room-path"
        before = f.stat().st_mtime_ns
        _, out = self.run_cmd(install.cmd_install)
        self.assertEqual(f.stat().st_mtime_ns, before)
        self.assertNotIn("Room home", out)

    def test_round_trip_restores_bytes(self):
        for original in (OTHER, "{}", OTHER.replace("\n", "\r\n"), None):
            with self.subTest(original=original):
                if original is None:
                    self.settings.unlink(missing_ok=True)
                else:
                    self.put(original)
                self.run_cmd(install.cmd_install)
                self.run_cmd(install.cmd_uninstall)
                if original is None:
                    self.assertFalse(self.settings.exists())
                else:
                    self.assertEqual(self.settings.read_bytes(), original.encode())
                self.assertFalse((self.home / "room-path").exists())
                self.assertFalse((self.home / "install.json").exists())

    def test_uninstall_after_the_user_edited_removes_only_ours(self):
        self.put(OTHER)
        self.run_cmd(install.cmd_install)
        data = json.loads(self.settings.read_text())
        data["newkey"] = 3
        self.settings.write_text(json.dumps(data, indent=4))
        self.run_cmd(install.cmd_uninstall)
        after = json.loads(self.settings.read_text())
        want = json.loads(OTHER)
        want["newkey"] = 3
        self.assertEqual(after, want)

    def test_second_install_after_a_user_edit_keeps_the_edit_on_uninstall(self):
        self.put(OTHER)
        self.run_cmd(install.cmd_install)
        data = json.loads(self.settings.read_text())
        data["newkey"] = "user edit"
        del data["enabledPlugins"]["commonroom@commonroom"]
        self.settings.write_text(json.dumps(data, indent=4))
        self.run_cmd(install.cmd_install)
        self.run_cmd(install.cmd_uninstall)
        after = json.loads(self.settings.read_text())
        self.assertEqual(after["newkey"], "user edit")
        self.assertNotIn("commonroom@commonroom", after.get("enabledPlugins", {}))

    def test_uninstall_leaves_room_path_install_did_not_write(self):
        self.home.mkdir(parents=True)
        want = (install.checkout() / "room.py").as_posix() + "\n"
        (self.home / "room-path").write_text(want, encoding="utf-8")
        self.run_cmd(install.cmd_install)
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual((self.home / "room-path").read_text(encoding="utf-8"), want)

    def test_uninstall_restores_a_room_path_install_overwrote(self):
        self.home.mkdir(parents=True)
        (self.home / "room-path").write_text("/elsewhere/room.py\n", encoding="utf-8")
        self.run_cmd(install.cmd_install)
        self.assertNotEqual((self.home / "room-path").read_text(encoding="utf-8"), "/elsewhere/room.py\n")
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual((self.home / "room-path").read_text(encoding="utf-8"), "/elsewhere/room.py\n")

    def test_uninstall_without_manifest_leaves_room_path(self):
        self.home.mkdir(parents=True)
        (self.home / "room-path").write_text("x\n", encoding="utf-8")
        _, out = self.run_cmd(install.cmd_uninstall)
        self.assertIn("not installed", out)
        self.assertTrue((self.home / "room-path").exists())

    def test_uninstall_keeps_a_parent_object_that_existed_before(self):
        self.put('{"enabledPlugins": {}, "a": 1}\n')
        self.run_cmd(install.cmd_install)
        data = json.loads(self.settings.read_text())
        data["b"] = 2
        self.settings.write_text(json.dumps(data))
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(json.loads(self.settings.read_text()), {"enabledPlugins": {}, "a": 1, "b": 2})

    def test_existing_status_line_is_left_alone(self):
        self.put('{"statusLine": {"type": "command", "command": "mine"}}\n')
        _, out = self.run_cmd(install.cmd_install)
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"]["command"], "mine")
        self.assertIn("skip", out)
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(self.settings.read_text(), '{"statusLine": {"type": "command", "command": "mine"}}\n')

    def test_uninstall_when_not_installed(self):
        code, out = self.run_cmd(install.cmd_uninstall)
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)

    def test_bad_json_is_refused_and_untouched(self):
        self.put("{nope")
        code, _ = self.run_cmd(install.cmd_install)
        self.assertEqual(code, 2)
        self.assertEqual(self.settings.read_text(), "{nope")

    def test_codex_hooks_added_shown_and_removed(self):
        other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}],
                           "SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}]}, "x": 1}
        self.codex.mkdir()
        self.codex_hooks.write_text(json.dumps(other))
        _, out = self.run_cmd(install.cmd_install, dry=True)
        self.assertIn("add    hooks.SessionStart", out)
        self.assertEqual(json.loads(self.codex_hooks.read_text()), other)
        self.run_cmd(install.cmd_install)
        data = json.loads(self.codex_hooks.read_text())
        self.assertEqual(data["x"], 1)
        self.assertEqual(data["hooks"]["Stop"], other["hooks"]["Stop"])
        for event, arg in (("SessionStart", "session-start"), ("UserPromptSubmit", "prompt-submit")):
            cmds = [h["command"] for g in data["hooks"][event] for h in g["hooks"]]
            self.assertTrue(any(c.endswith(f"run.py\" {arg}") for c in cmds), cmds)
        self.assertIn("theirs", [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]])
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(json.loads(self.codex_hooks.read_text()), other)

    def test_codex_file_install_made_is_deleted_and_twice_is_idempotent(self):
        self.run_cmd(install.cmd_install)
        first = self.codex_hooks.read_bytes()
        _, out = self.run_cmd(install.cmd_install)
        self.assertEqual(self.codex_hooks.read_bytes(), first)
        self.assertIn("Nothing to change", out)
        self.assertEqual(len([c for c in self.sched.calls if "create" in c or c.startswith("(crontab")]), 1)
        self.run_cmd(install.cmd_uninstall)
        self.assertFalse(self.codex_hooks.exists())

    def test_beat_registered_with_the_schedule_command_and_removed(self):
        want, _ = wire.beat_command(install.checkout())
        self.run_cmd(install.cmd_install)
        self.assertIn(want, self.sched.calls)
        self.assertTrue(self.sched.registered)
        self.run_cmd(install.cmd_uninstall)
        self.assertFalse(self.sched.registered)

    def test_dry_run_runs_no_create(self):
        self.run_cmd(install.cmd_install, dry=True)
        self.assertFalse(self.sched.registered)
        self.assertFalse([c for c in self.sched.calls if "create" in c or c.startswith("(crontab")])
        self.assertFalse(self.codex.exists())

    def test_beat_already_registered_by_the_user_is_not_removed(self):
        self.sched.registered = True
        self.run_cmd(install.cmd_install)
        self.run_cmd(install.cmd_uninstall)
        self.assertTrue(self.sched.registered)

    def test_beat_that_cannot_be_registered_prints_the_command_and_does_not_fail(self):
        self.sched.refuse = True
        code, out = self.run_cmd(install.cmd_install)
        self.assertEqual(code, 0)
        self.assertIn("Could not register the beat", out)
        self.assertIn(wire.beat_command(install.checkout())[0], out)
        self.assertTrue(self.codex_hooks.exists())
        self.run_cmd(install.cmd_uninstall)
        self.assertFalse([c for c in self.sched.calls if "delete" in c])

    def old_task(self, name, state="Ready", script="heartbeat.vbs magazine"):
        run = f'wscript.exe "{(install.checkout() / script.split()[0])}" {" ".join(script.split()[1:])}'.strip()
        head = '"HostName","TaskName","Status","Scheduled Task State","Task To Run"'
        row = ",".join(f'"{c}"' for c in ("PC", "\\" + name, state, state, run.replace('"', '""')))
        self.sched.table = "\n".join((head, row)) + "\n"

    def install_on_windows(self):
        with mock.patch("sys.platform", "win32"):
            return self.run_cmd(install.cmd_install)

    def creates(self):
        return [c for c in self.sched.calls if c.startswith("schtasks /create")]

    def test_enabled_old_heartbeat_task_blocks_the_beat_and_names_the_retire_command(self):
        self.old_task("MagazineHeartbeat")
        code, out = self.install_on_windows()
        self.assertEqual(code, 0)
        self.assertEqual(self.creates(), [])
        self.assertIn("MagazineHeartbeat", out)
        self.assertIn('schtasks /delete /tn "MagazineHeartbeat" /f', out)
        self.assertTrue(self.codex_hooks.exists())
        self.assertTrue(self.settings.exists())
        self.run_cmd(install.cmd_uninstall)
        self.assertFalse([c for c in self.sched.calls if "delete" in c])

    def test_enabled_beat_script_task_under_another_name_blocks_the_beat(self):
        self.old_task("MyRoomBeat", script="beat.vbs")
        code, out = self.install_on_windows()
        self.assertEqual(code, 0)
        self.assertEqual(self.creates(), [])
        self.assertIn('schtasks /delete /tn "MyRoomBeat" /f', out)

    def test_no_old_task_registers_the_beat_once(self):
        self.install_on_windows()
        self.assertEqual(len(self.creates()), 1)

    def test_disabled_old_task_does_not_block_the_beat(self):
        self.old_task("MagazineHeartbeat", state="Disabled")
        self.install_on_windows()
        self.assertEqual(len(self.creates()), 1)


if __name__ == "__main__":
    unittest.main()

class PersonImportTest(InstallTest):
    """commonroom#575: one line per vendor's user-level instruction file, nothing per prompt."""
    def files(self):
        return self.claude / "CLAUDE.md", self.codex / "AGENTS.md"

    def test_line_added_shown_first_and_the_rest_untouched_then_removed(self):
        claude_md, agents_md = self.files()
        self.claude.mkdir(parents=True)
        mine = "# Me\r\n\r\nBe brief.\r\n- no trailing newline"
        claude_md.write_bytes(mine.encode())
        _, out = self.run_cmd(install.cmd_install, dry=True)
        want = "@" + (self.home / "person.md").as_posix()
        self.assertIn(f"add    {want}", out)
        self.assertEqual(claude_md.read_bytes(), mine.encode())
        self.assertFalse(agents_md.exists())
        self.run_cmd(install.cmd_install)
        self.assertEqual(claude_md.read_bytes(), (mine + "\r\n" + want + "\r\n").encode())
        self.assertIn((self.home / "person.md").as_posix(), agents_md.read_text())
        _, out = self.run_cmd(install.cmd_uninstall)
        self.assertIn(f"remove {want}", out)
        self.assertEqual(claude_md.read_bytes(), mine.encode())
        self.assertFalse(agents_md.exists())

    def test_twice_adds_one_line_and_uninstall_keeps_edits_made_after(self):
        claude_md, _ = self.files()
        self.claude.mkdir(parents=True)
        claude_md.write_text("top\n")
        self.run_cmd(install.cmd_install)
        self.run_cmd(install.cmd_install)
        self.assertEqual(claude_md.read_text().count("person.md"), 1)
        claude_md.write_text(claude_md.read_text() + "later\n")
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(claude_md.read_text(), "top\nlater\n")

    def test_a_line_the_user_already_had_is_not_ours_to_remove(self):
        claude_md, _ = self.files()
        self.claude.mkdir(parents=True)
        mine = "@" + (self.home / "person.md").as_posix() + "\n"
        claude_md.write_text(mine)
        self.run_cmd(install.cmd_install)
        self.assertEqual(claude_md.read_text(), mine)
        self.run_cmd(install.cmd_uninstall)
        self.assertEqual(claude_md.read_text(), mine)

    def test_the_hooks_add_none_of_the_person_file(self):
        from commonroom import hook, person
        person.save(["2026-11-01 prefers plain words zebra-marker"], self.home)
        for name, event in (("UserPromptSubmit", hook.PROMPT), ("SessionStart", hook.EVENT)):
            out = io.StringIO()
            data = {"session_id": "s", "cwd": str(self.home), "hook_event_name": name, "prompt": "hi"}
            hook.main(stdin=io.StringIO(json.dumps(data)), stdout=out, event=event)
            self.assertNotIn("zebra-marker", out.getvalue())
