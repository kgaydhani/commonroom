"""A registered project's extra slot and background role are judged on its own line in the pie, and `room tap` prints the
verdict its dispatcher acts on (commonroom#272). Temporary ledger and registry in a temporary room home."""
import contextlib
import io
import json
import time
from unittest import mock

from commonroom import cli, dispatch, gate
from commonroom.registry import Registry
from tests.test_dispatch import FakeBoard, cfg_with
from tests.test_owner_reserve import Gate, H


class PieHeadroom(Gate):
    """alpha (share 30, line 17.4 at half the week with the default slack) and beta (55); the owner's slice is 15."""

    def spend(self, alpha, beta, owner):
        now = self.now
        self.add_run("alpha-site", now - 6 * H, now - 4 * H)
        self.add_run("beta-site", now - 4 * H, now - 2 * H)
        for at, pct in ((now - 6 * H, 0), (now - 4 * H, alpha), (now - 2 * H, alpha + beta), (now - 60, alpha + beta + owner)):
            self.reading(at, pct)
        self.five_hour()

    def slot2(self, root):
        return dispatch.Dispatcher(self.cfg() if root is None else cfg_with(root), self.ledger, board=FakeBoard([]),
                                   log=lambda *_: None, only_provider="claude", slot=2)

    def test_a_registered_project_is_judged_on_its_own_line_not_the_plan_wide_pace(self):
        self.registry()
        self.spend(0, 30, 30)                       # plan-wide 60%, far over the old pace; alpha has spent nothing
        v = self.verdict()
        self.assertTrue(v.allowed, v.reason)
        self.assertGreater(v.detail["weekly_used"], v.detail["weekly_pace"] - 15)   # the old test would say no
        self.assertEqual(v.detail["pie_headroom"], 17.4)
        self.assertTrue(self.slot2(None).pace_headroom_ok("claude", v))

    def test_a_registered_project_over_its_line_gets_no_extra_slot(self):
        self.registry()
        self.spend(10, 20, 0)                       # alpha spent 10 of its 17.4: 7.4 left, under the 15 needed
        v = self.verdict()
        self.assertEqual(v.detail["pie_headroom"], 7.4)
        self.assertFalse(self.slot2(None).pace_headroom_ok("claude", v))

    def test_an_unregistered_project_is_judged_on_the_plan_wide_pace_as_before(self):
        self.registry()
        self.spend(0, 30, 30)
        v = dispatch.verdicts(self.ledger, cfg_with(self.root), self.now)["claude"]
        self.assertNotIn("pie_headroom", v.detail)
        d = self.slot2(self.root)
        self.assertEqual(d.pace_headroom_ok("claude", v), v.detail["weekly_pace"] - v.detail["weekly_used"] >= 15)
        plain = gate.Verdict(True, "ok", {"weekly_pace": 50, "weekly_used": 36})
        self.assertFalse(d.pace_headroom_ok("claude", plain))
        self.assertTrue(d.pace_headroom_ok("claude", gate.Verdict(True, "ok", {"weekly_pace": 50, "weekly_used": 35})))

    def test_an_unreadable_pie_leaves_the_old_test(self):
        self.registry()
        v = self.verdict()                          # no weekly reading: today's gate with a note
        self.assertNotIn("pie_headroom", v.detail)

    def tap(self, key):
        folder = self.root / "work" / key
        (folder / "room.toml").write_text('[project]\nname = "alpha-site"\n[budget.claude]\nreserve_weekly = 65\n',
                                          encoding="utf-8")
        payload = {"model": {"display_name": "Opus"}, "cwd": str(folder),
                   "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": int(self.now + H)},
                                   "seven_day": {"used_percentage": self.used, "resets_at": int(self.resets)}}}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("time.time", return_value=self.now), \
                contextlib.redirect_stdout(out):
            cli.cmd_tap(None)
        return out.getvalue()

    def test_tap_prints_the_dispatchers_verdict_for_a_registered_project(self):
        self.registry()
        self.spend(0, 30, 30)
        self.used = 60
        self.assertIn("room: go", self.tap("alpha"))      # the host-style reserve of 65 would hold; the pie says go
        self.spend(0, 30, 66)                             # the week nearly gone: the pie's reserve holds
        self.used = 96
        self.assertIn("room: hold", self.tap("alpha"))

    def test_tap_says_hold_when_the_pie_holds_though_the_reserve_gate_allows(self):
        self.registry()
        self.spend(30, 0, 0)                              # alpha spent 30 of its 17.4 line: the pie holds it
        self.used = 30
        self.add_run("beta-site", self.now - 600)         # beta (under its line) is running now, so it is beta's turn
        out = self.tap("alpha")
        self.assertIn("room: hold", out)
        self.assertNotIn("room: go", out)
