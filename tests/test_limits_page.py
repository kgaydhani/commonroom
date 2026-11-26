"""Limits on the page, using scratch homes, settings, projects, transcripts and ledger only."""
import json
import os
from pathlib import Path
from unittest import mock

from commonroom import dashboard, page
from tests import test_page
from tests.test_standings import Room


class LimitsPage(Room):
    def setUp(self):
        super().setUp()
        user = self.root / "user"
        self.settings = user / ".claude" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(user), "USERPROFILE": str(user),
                                           "CLAUDE_CONFIG_DIR": str(self.settings.parent)})
        env.start()
        self.addCleanup(env.stop)
        self.file = self.home / "limits.txt"

    def state(self):
        return page.page_state(self.ledger, test_page.PageState.cfg(self), self.now, transcripts=self.transcripts)

    def installed(self, rules, deny):
        self.settings.write_text(json.dumps({"permissions": {"deny": deny}}), encoding="utf-8")
        (self.home / "install.json").write_text(json.dumps({str(self.settings): {"deny_added": rules}}), encoding="utf-8")

    def test_missing_or_commented_list_is_explicitly_empty_and_writes_nothing(self):
        for text in (None, "# Bash(pkill *)\n\n"):
            if text is not None:
                self.file.write_text(text, encoding="utf-8")
            before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            self.assertEqual(self.state()["limits"], {"file": str(self.file), "rules": [], "error": None})
            self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertIn("No limits are in force.", dashboard.PAGE)

    def test_lists_only_active_lines_and_installed_room_rules_without_changing_projects(self):
        self.week()
        self.register()
        before = self.state()
        self.file.write_text("# example\n\n Bash(git push --force *) \nRead(~/.ssh/**)\n", encoding="utf-8")
        self.installed(["Read(~/.ssh/**)", "WebFetch"], ["Read(~/.ssh/**)", "WebFetch", "Edit"])
        original = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        after = self.state()
        self.assertEqual(after["limits"]["rules"], [
            {"rule": "Bash(git push --force *)", "line": 3, "deny": False},
            {"rule": "Read(~/.ssh/**)", "line": 4, "deny": True},
            {"rule": "WebFetch", "line": None, "deny": True}])
        self.assertIsNone(after["limits"]["error"])
        self.assertEqual({k: v for k, v in after.items() if k != "limits"},
                         {k: v for k, v in before.items() if k != "limits"})
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, original)

    def test_deleted_list_still_shows_installed_rules_but_removed_deny_rules_are_gone(self):
        self.installed(["Read", "WebFetch"], ["WebFetch", "Edit"])
        self.assertEqual(self.state()["limits"]["rules"], [{"rule": "WebFetch", "line": None, "deny": True}])

    def test_a_preexisting_deny_rule_is_shown_only_if_also_in_the_owners_list(self):
        self.file.write_text("Read\n", encoding="utf-8")
        self.installed([], ["Read", "Edit"])
        self.assertEqual(self.state()["limits"]["rules"], [{"rule": "Read", "line": 1, "deny": True}])

    def test_unreadable_state_says_unknown_instead_of_claiming_no_limits(self):
        for name in ("limits.txt", "install.json"):
            with self.subTest(name=name):
                broken = self.home / name
                broken.write_bytes(b"\xff")
                state = self.state()
                self.assertTrue(state["limits"]["error"])
                self.assertIn("projects", state)
                broken.unlink()
        self.installed(["Read"], ["Read"])
        self.settings.write_text("not json", encoding="utf-8")
        self.assertTrue(self.state()["limits"]["error"])

