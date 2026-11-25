"""Trials, part 3 (commonroom#353): code computes each trial's result; `room trials` prints it; the digest shows it;
the steward wakes the director on a final verdict with no `ended` date. Temporary ledgers, homes and trials files."""
import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from commonroom import dispatch, handshake, registry, steward, trials
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with
from tests.test_handshake import Room

STARTED = date.today() - timedelta(days=3)
NOW = time.time()
BODY = f"""[[trial]]
name = "t1"
area = "review"
started = "{STARTED}"
share = 0.5
measure = "review_runs_per_landing"
min_landings = 2
max_days = 10
[trial.variant]
policy = {{ rework_resume_seconds = 7 }}
"""


def declared(folder, body=BODY):
    path = Path(folder) / "trials.toml"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def add_task(ledger, project, issue, arm, reviews, at=None):
    """A landed task: a builder run (with its arm), `reviews` review runs of its PR, and the merge."""
    at = at or NOW - 3600
    pr = 100 + issue
    outputs = [{"kind": "trial", "ref": f"t1={arm}"}] if arm else []
    run = ledger.start_run(project, f"#{issue}", "builder", "claude", now=at)
    ledger.finish_run(run, "done", outputs + [{"kind": "pr", "ref": f"PR #{pr}"}], now=at + 60)
    for k in range(reviews):
        r = ledger.start_run(project, f"PR #{pr}", "reviewer", "claude", now=at + 100 + k)
        ledger.finish_run(r, "done", [{"kind": "review", "ref": f"PR #{pr} CHANGES"}], now=at + 120 + k)
    m = ledger.start_run(project, f"#{issue}", "merge", "none", now=at + 500)
    ledger.finish_run(m, "done", [{"kind": "merge", "ref": f"PR #{pr}"}], now=at + 501)


class Verdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self.addCleanup(self.ledger.close)
        self.n = 0

    def land(self, arm, reviews, count=2):
        for _ in range(count):
            self.n += 1
            add_task(self.ledger, "p", self.n, arm, reviews)

    def verdict(self, body=BODY, now=NOW):
        (trial,) = trials.load(declared(self.tmp.name, body), None, None)
        return trials.result(trial, self.ledger.runs_since("p", 0), now)

    def test_running_until_each_arm_has_min_landings(self):
        self.land("control", 2)
        self.land("variant", 1, count=1)
        res = self.verdict()
        self.assertEqual(res["verdict"], "running")
        self.assertEqual((res["arms"]["control"]["landings"], res["arms"]["variant"]["landings"]), (2, 1))

    def test_variant_better(self):
        self.land("control", 3)
        self.land("variant", 1)
        res = self.verdict()
        self.assertEqual(res["verdict"], "variant better")
        self.assertEqual((res["arms"]["control"]["value"], res["arms"]["variant"]["value"]), (3, 1))

    def test_variant_worse(self):
        self.land("control", 1)
        self.land("variant", 3)
        self.assertEqual(self.verdict()["verdict"], "variant worse")

    def test_no_clear_difference_under_twenty_percent(self):
        self.land("control", 5)
        self.land("variant", 5)
        self.assertEqual(self.verdict()["verdict"], "no clear difference")

    def test_exactly_twenty_percent_is_a_clear_difference(self):
        self.land("control", 5)
        self.land("variant", 4)
        self.assertEqual(self.verdict()["verdict"], "variant better")

    def test_too_few_landings_after_max_days(self):
        self.land("control", 2)
        self.land("variant", 1, count=1)
        late = NOW + 20 * 86400
        self.assertEqual(self.verdict(now=late)["verdict"], "too few landings")

    def test_a_landing_with_no_trial_output_counts_in_neither_arm(self):
        self.land(None, 4, count=5)
        res = self.verdict()
        self.assertEqual((res["arms"]["control"]["landings"], res["arms"]["variant"]["landings"]), (0, 0))
        self.assertEqual(res["verdict"], "running")


class Reports(Room):
    def setUp(self):
        super().setUp()
        self.register()
        self.file = declared(self.tmp)
        patch = mock.patch.object(trials, "FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self):
        ledger = Ledger()
        try:
            for i, arm in enumerate(("control", "control", "variant", "variant")):
                add_task(ledger, "alpha", i + 1, arm, 3 if arm == "control" else 1)
        finally:
            ledger.close()

    def test_room_trials_prints_name_arms_and_verdict(self):
        self.seed()
        code, out, _ = self.room("trials", "alpha")
        self.assertEqual(code, 0)
        self.assertIn("t1", out)
        self.assertIn("control 2 landings", out)
        self.assertIn("variant 2 landings", out)
        self.assertIn("variant better", out)

    def test_the_digest_shows_the_active_trial_and_nothing_without_trials(self):
        self.seed()
        _, out, _ = self.room("digest", "alpha")
        self.assertIn("## Trials", out)
        self.assertIn("t1 (review)", out)
        declared(self.tmp, "")
        _, out, _ = self.room("digest", "alpha")
        self.assertNotIn("Trials", out)


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3")
        self.addCleanup(self.ledger.close)

    def sweep(self, body):
        file = declared(self.tmp.name, body)
        with mock.patch.object(dispatch, "TRIALS_FILE", file):
            d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard([]), log=lambda *_: None)
            d.sweep([], [])
        return self.ledger.findings("t")

    def test_a_final_verdict_with_no_ended_date_wakes_the_director_and_a_running_one_does_not(self):
        self.assertEqual(self.sweep(BODY), [])   # running: no runs yet
        for i, arm in enumerate(("control", "control", "variant", "variant")):
            add_task(self.ledger, "t", i + 1, arm, 3 if arm == "control" else 1)
        (found,) = self.sweep(BODY)
        self.assertEqual(found["invariant"], "trial-final")
        self.assertIn("variant better", found["detail"])
        self.assertIn("trial-final", dispatch.STEWARD_FIXABLE)
        d = dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard([]), log=lambda *_: None)
        self.assertIn("variant better", d.steward_reason(0))
        self.assertEqual(self.sweep(BODY.replace('measure', 'ended = "%s"\nmeasure' % (date.today() + timedelta(days=1)))), [])

    def test_no_trials_declared_changes_no_finding(self):
        self.assertEqual(self.sweep(""), [])


class SweepUnreadNotes(unittest.TestCase):
    """commonroom#588: the sweep raises a project's old unread inbox notes; a read or fresh one is not raised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "home"
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(home)})
        env.start()
        self.addCleanup(env.stop)
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        (work / "room.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
        registry.save(registry.Registry({"alpha": registry.Project("alpha", str(work), 40)}))
        self.ledger = Ledger(home / "room.sqlite3")
        self.addCleanup(self.ledger.close)
        self.where = handshake.folder_of(registry.load().projects["alpha"])
        self.now = time.time()

    def sweep(self):
        with mock.patch.object(dispatch, "TRIALS_FILE", declared(self.tmp.name, "")):
            dispatch.Dispatcher(cfg_with(self.tmp.name), self.ledger, board=FakeBoard([]), log=lambda *_: None).sweep([], [])
        return [f for f in self.ledger.findings("t") if f["invariant"] == "unread-note"]

    def test_an_old_unread_note_is_raised_and_a_fresh_one_is_not(self):
        handshake.write_note(self.where, "old", "someone", now=self.now - 30 * 3600)
        handshake.write_note(self.where, "new", "someone", now=self.now - 3600)
        (found,) = self.sweep()
        self.assertIn("30 hours", found["detail"])

    def test_a_note_the_project_has_read_is_not_raised(self):
        handshake.write_note(self.where, "old", "someone", now=self.now - 30 * 3600)
        self.ledger.mark("alpha", handshake.READ + "alpha", at=self.now - 3600)
        self.assertEqual(self.sweep(), [])

    def test_the_page_shows_the_count_and_the_oldest(self):
        handshake.write_note(self.where, "old", "someone", now=self.now - 30 * 3600)
        handshake.write_note(self.where, "newer", "someone", now=self.now - 3600)
        got = handshake.unread_summary(registry.load(), registry.load().projects["alpha"], self.ledger, self.now)
        self.assertEqual(got["count"], 2)
        self.assertAlmostEqual(got["oldest_at"], self.now - 30 * 3600, delta=2)
