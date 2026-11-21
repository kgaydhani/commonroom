"""Tell the owner when his own session takes a project over its share (commonroom#243). Every test uses a Ledger and a
registry in a temporary room home through COMMONROOM_HOME, never ~/.commonroom, and a transcripts folder of its own."""
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from commonroom import cli, dispatch, pie, pie_notice, raises, registry, standings, usage
from commonroom.registry import Project, Registry
from tests.test_dispatch import FakeBoard, cfg_with
from tests.test_standings import H, Room


class Phone:
    def __init__(self):
        self.sent = []

    def send(self, title, message, url=None, priority=None):
        self.sent.append((title, message))


def report(spent, line=23.5, share=40, own_last=0.0, rows=None):
    win = standings.Window("claude", 1_000_000.0, 0.5, 30.0, 0.0)
    return standings.Report(win, rows or [pie.Standing("alpha", share, spent, line)], 10.0, own_last={"alpha": own_last})


class Flicker(Room):
    def test_the_estimate_flickering_over_the_line_gives_exactly_one_notice(self):
        # The owner's own use read 24.1, 23.1, 24.1 within an hour against a line of 23.5.
        sent = []
        for spent in (24.1, 23.1, 24.1):
            r = report(spent, own_last=self.now)
            if pie_notice.due(self.ledger, "alpha", r, "alpha", self.now, margin=0.5):
                sent.append(spent)
                pie_notice.sent(self.ledger, "alpha", r, "alpha", self.now)
        self.assertEqual(sent, [24.1])

    def test_it_tells_again_after_dropping_below_the_line_by_the_margin(self):
        sent = []
        for step, spent in enumerate((24.1, 22.9, 24.1)):
            at = self.now + step * 60
            r = report(spent, own_last=at)
            if pie_notice.due(self.ledger, "alpha", r, "alpha", at, margin=0.5):
                sent.append(spent)
                pie_notice.sent(self.ledger, "alpha", r, "alpha", at)
        self.assertEqual(sent, [24.1, 24.1])

    def test_inside_the_margin_over_the_line_says_nothing(self):
        self.assertFalse(pie_notice.due(self.ledger, "alpha", report(23.9, own_last=self.now), "alpha", self.now, margin=0.5))


class Housekeeping(Room):
    """The fixture's week (alpha-site spent 4.5): at share 5 alpha's line is 2.9 (over by 1.6), at share 40 it is 23.2."""

    def setUp(self):
        super().setUp()
        self.week()
        self.ledger.record_usage([usage.observation("claude", "seven_day", 20, self.resets, "test")], now=self.now)
        self.dirs = {key: self.project_dir(key, f"{key}-site") for key in ("alpha", "beta")}
        cfg = cfg_with(self.dirs["alpha"])
        cfg["project"]["name"] = "alpha-site"
        self.phone = Phone()
        self.d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None, notifier=self.phone)

    def projects(self, alpha=5, beta=20):
        registry.save(Registry({"alpha": Project("alpha", self.dirs["alpha"], alpha),
                                "beta": Project("beta", self.dirs["beta"], beta)}))

    def own_session(self, ago=60):
        at = datetime.fromtimestamp(self.now - ago, timezone.utc).isoformat()
        line = {"type": "assistant", "timestamp": at, "sessionId": "s1", "cwd": self.dirs["alpha"],
                "message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 5}}}
        folder = self.transcripts / "alpha"
        folder.mkdir(exist_ok=True)
        (folder / "s1.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    def run_pass(self):
        with mock.patch("commonroom.dispatch.time.time", return_value=self.now):
            return self.d.notify_pie_over(self.transcripts)

    def test_over_its_line_with_an_active_own_session_sends_one_notice_with_the_command_and_the_cost(self):
        self.projects()
        self.own_session()
        self.assertEqual(len(self.run_pass()), 1)
        (title, message), = self.phone.sent
        self.assertEqual(title, "alpha is over its share")
        self.assertIn("alpha is over its share on claude: an estimated 4.5% spent against its line of 2.9%", message)
        self.assertIn("estimate", message)
        self.assertRegex(message, r"room pie raise alpha \d+(\.\d)?\n")
        self.assertRegex(message, r"That takes [\d.]+ from beta")
        self.assertEqual(self.run_pass(), [])                       # a second pass sends nothing
        self.assertEqual(len(self.phone.sent), 1)

    def test_under_its_line_sends_nothing(self):
        self.projects(alpha=40)
        self.own_session()
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.phone.sent, [])

    def test_over_its_line_with_only_room_runs_sends_nothing(self):
        self.projects()
        self.assertEqual(self.run_pass(), [])
        self.own_session(ago=3600)                                  # an own session, but not in the last 20 minutes
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.phone.sent, [])

    def test_it_sends_again_once_a_raise_is_used_up(self):
        self.projects()
        self.own_session()
        self.run_pass()
        rep = standings.standings(self.ledger, registry.load(), "claude", self.now, self.transcripts)
        entry = raises.transfer(registry.load(), rep, [], "alpha", 1, None)
        raises.save([entry])                                        # alpha's share 5 -> 6: line 3.5, spent 4.5: over again
        self.assertEqual(len(self.run_pass()), 1)
        self.assertEqual(len(self.phone.sent), 2)
        self.assertEqual(self.run_pass(), [])

    def test_with_an_empty_registry_nothing_is_sent_and_no_transcript_is_read(self):
        registry.save(Registry())
        self.own_session()
        with mock.patch("commonroom.sessions.spans") as spans:
            self.assertEqual(self.run_pass(), [])
        spans.assert_not_called()
        self.assertEqual(self.phone.sent, [])

    def test_no_notifier_no_notice(self):
        self.projects()
        self.own_session()
        self.d.notifier = None
        self.assertEqual(self.run_pass(), [])

    def tap(self, folder):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"model": {"display_name": "Opus"}, "cwd": folder}))), \
                mock.patch("time.time", return_value=self.now), contextlib.redirect_stdout(out):
            cli.cmd_tap(None)
        return out.getvalue()

    def test_tap_in_the_projects_folder_shows_the_over_share_part(self):
        self.projects()
        line = self.tap(self.dirs["alpha"])
        self.assertIn("Opus", line)
        self.assertIn("alpha over its share on claude", line)
        self.assertNotIn("over its share", self.tap(self.dirs["beta"]))
        self.assertNotIn("over its share", self.tap(str(self.root)))

    def test_tap_still_prints_a_line_when_the_registry_is_broken_or_missing(self):
        self.assertEqual(self.tap(self.dirs["alpha"]).strip(), "Opus")           # no registry
        (self.home / registry.FILENAME).write_text("not [valid", encoding="utf-8")
        self.assertEqual(self.tap(self.dirs["alpha"]).strip(), "Opus")

    def test_room_pie_prints_the_notice_for_a_project_over_its_line(self):
        self.projects()
        out = io.StringIO()
        with mock.patch("time.time", return_value=self.now), contextlib.redirect_stdout(out):
            cli.cmd_pie(mock.Mock(json=False))
        self.assertIn("OVER ITS SHARE: alpha is over its share on claude", out.getvalue())
        self.assertNotIn("OVER ITS SHARE: beta", out.getvalue())


if __name__ == "__main__":
    unittest.main()
