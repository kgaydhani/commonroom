"""The dispatcher's ways-of-working numbers (commonroom#217): their values must not change when they move."""
import calendar
import fnmatch
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from commonroom import config, dispatch, policy, usage, workers
from commonroom.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent

# Every constant the issue names, with the value it had in code before it moved to commonroom/ways/policy.toml.
PINNED = {
    (dispatch, "REVIEW_ROUNDS"): 2,
    (dispatch, "INCOMPLETE_ROUNDS"): 2,
    (dispatch, "CI_BOUNCES"): 3,
    (dispatch, "CI_RERUNS"): 6,
    (dispatch, "CI_BACKOFF_MINUTES"): 20,
    (dispatch, "SCOUT_INTERLEAVE"): 3 * 3600,
    (dispatch, "DIRECTOR_COOLDOWN"): 3600,
    (dispatch, "OWNER_COOLDOWN"): 300,
    (dispatch, "STALE_CLAIM_MINUTES"): 30,
    (dispatch, "ROOM_ERROR_BACKOFF"): 600,
    (dispatch, "ROOM_ERROR_ESCALATE"): 3,
    (dispatch, "RESUME_WINDOW"): 12 * 3600,
    (dispatch, "PROGRESS_MIN_WORK"): 6 * 3600,
    (dispatch, "NOTIFY_MAX"): 5,
    (workers, "RUN_TIMEOUT"): 3600,
}


# Values added to the table since the move, with the value each was added at.
NEW = {"prompt_max_tokens": 700, "templates_max_tokens": 2500}   # commonroom#231


class PinnedValues(unittest.TestCase):
    def test_every_constant_keeps_its_value(self):
        for (module, name), value in PINNED.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(module, name), value)
                self.assertIs(type(getattr(module, name)), int)

    def test_the_table_holds_each_value_the_code_had(self):
        # Name in commonroom/ways/policy.toml -> the constant it replaced, and that constant's value before the move.
        table = {"review_rounds": ("REVIEW_ROUNDS", 2), "incomplete_rounds": ("INCOMPLETE_ROUNDS", 2),
                 "ci_bounces": ("CI_BOUNCES", 3), "ci_reruns": ("CI_RERUNS", 6),
                 "ci_backoff_minutes": ("CI_BACKOFF_MINUTES", 20),
                 "scout_interleave_seconds": ("SCOUT_INTERLEAVE", 3 * 3600),
                 "director_cooldown_seconds": ("DIRECTOR_COOLDOWN", 3600),
                 "owner_cooldown_seconds": ("OWNER_COOLDOWN", 300), "stale_claim_minutes": ("STALE_CLAIM_MINUTES", 30),
                 "room_error_backoff_seconds": ("ROOM_ERROR_BACKOFF", 600),
                 "room_error_escalate": ("ROOM_ERROR_ESCALATE", 3),
                 "resume_window_seconds": ("RESUME_WINDOW", 12 * 3600),
                 "progress_min_work_seconds": ("PROGRESS_MIN_WORK", 6 * 3600), "notify_max": ("NOTIFY_MAX", 5),
                 "run_timeout_seconds": ("RUN_TIMEOUT", 3600)}
        with open(REPO / "commonroom" / "ways" / "policy.toml", "rb") as f:
            raw = tomllib.load(f)
        self.assertEqual(set(raw), set(table) | set(NEW))
        self.assertEqual(set(policy.NAMES), set(table) | set(NEW))
        self.assertEqual(dispatch.POLICY_CONSTANTS, {k: c for k, (c, _) in table.items()})
        for name, value in NEW.items():   # no code constant: tests/test_prompt_budget.py reads them
            with self.subTest(name=name):
                self.assertEqual((raw[name], policy.DEFAULTS[name]), (value, value))
        for name, (constant, value) in table.items():
            with self.subTest(name=name):
                self.assertEqual(raw[name], value)
                self.assertEqual(policy.DEFAULTS[name], value)
                module = workers if constant == "RUN_TIMEOUT" else dispatch
                self.assertEqual(getattr(module, constant), value)


class Loading(unittest.TestCase):
    def write(self, folder, text):
        path = Path(folder) / "policy.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def full(self, **change):
        values = dict(policy.DEFAULTS) | change
        return "".join(f"{k} = {v}\n" for k, v in values.items() if v is not None)

    def test_a_missing_file_fails_naming_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as caught:
                policy.load_policy(Path(tmp) / "policy.toml")
        self.assertIn("policy.toml", str(caught.exception))

    def test_a_missing_unknown_or_bad_name_fails_naming_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "missing notify_max"):
                policy.load_policy(self.write(tmp, self.full(notify_max=None)))
            with self.assertRaisesRegex(ValueError, "unknown notify_maxx"):
                policy.load_policy(self.write(tmp, self.full() + "notify_maxx = 1\n"))
            with self.assertRaisesRegex(ValueError, "notify_max must be a whole number"):
                policy.load_policy(self.write(tmp, self.full(notify_max='"5"')))
            self.assertEqual(policy.load_policy(self.write(tmp, self.full())), policy.DEFAULTS)

    def test_the_table_ships_with_the_package_and_checks_out_lf(self):
        with open(REPO / "pyproject.toml", "rb") as f:
            globs = tomllib.load(f)["tool"]["setuptools"]["package-data"]["commonroom"]
        self.assertTrue(any(fnmatch.fnmatch("ways/policy.toml", g) for g in globs))
        attributes = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("commonroom/ways/policy.toml text eol=lf", attributes)


class ProjectOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, name, overrides=None):
        cfg = config.merge(config.DEFAULTS, {"project": {"name": name, "repo": f"o/{name}"}, "goal": {"milestone": "M"},
                                             **({"policy": overrides} if overrides is not None else {})})
        cfg["_root"] = self.tmp.name
        return dispatch.Dispatcher(cfg, self.ledger, board=object(), log=lambda *_: None)

    def test_unset_means_the_rooms_values(self):
        for d in (self.dispatcher("a"), self.dispatcher("b", {})):
            self.assertEqual(d.policy_overrides, {})
            for name in dispatch.POLICY_CONSTANTS:   # the dispatcher's numbers; the rest are room-wide only
                self.assertEqual(d.policy(name), policy.DEFAULTS[name], name)

    def test_an_unknown_room_wide_or_bad_name_fails_naming_it(self):
        with self.assertRaisesRegex(ValueError, "review_round'"):
            self.dispatcher("a", {"review_round": 3})
        with self.assertRaisesRegex(ValueError, "ci_reruns is room-wide only"):
            self.dispatcher("a", {"ci_reruns": 1})
        for name in NEW:
            with self.assertRaisesRegex(ValueError, f"{name} is room-wide only because it limits the room's own text"):
                self.dispatcher("a", {name: 5000})
        with self.assertRaisesRegex(ValueError, "review_rounds must be a whole number"):
            self.dispatcher("a", {"review_rounds": True})

    def test_an_override_is_that_projects_only(self):
        a = self.dispatcher("a", {"review_rounds": 4, "run_timeout_seconds": 1800, "room_error_backoff_seconds": 3600})
        b = self.dispatcher("b")
        self.assertEqual((a.policy("review_rounds"), b.policy("review_rounds")), (4, 2))
        self.assertEqual((a.policy("notify_max"), b.policy("notify_max")), (5, 5))
        self.assertEqual((dispatch.REVIEW_ROUNDS, workers.RUN_TIMEOUT), (2, 3600))   # the room's values are untouched
        # The run time limit the worker is told.
        self.assertIn("ends this run after 30 minutes.", a.headless_note())
        self.assertIn("ends this run after 60 minutes.", b.headless_note())
        # A room-error rests its task: 20 minutes on it is still inside a's hour, but past b's ten minutes.
        for d in (a, b):
            self.ledger.finish_run(self.ledger.start_run(d.project, "#5", "builder", "claude"), "room-error", [], note="x")
        self.ledger.db.execute("UPDATE runs SET started_at=started_at-1200, ended_at=ended_at-1200")
        self.ledger.db.commit()
        self.assertIn("#5", a.busy_tasks())
        self.assertNotIn("#5", b.busy_tasks())

    def test_the_module_functions_take_the_projects_value(self):
        now = calendar.timegm((2026, 1, 1, 0, 45, 0))
        claimed = [{"number": 3, "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                    "labels": [{"name": "status:in-progress"}]}]
        self.assertEqual([c["number"] for c in dispatch.stale_claims(claimed, [], [], now)], [3])   # 45 >= 30
        self.assertEqual(dispatch.stale_claims(claimed, [], [], now, stale_minutes=60), [])
        # Work began an hour ago with nothing merged: too early under the room's six hours, not under half an hour.
        now = time.time()
        self.ledger.finish_run(self.ledger.start_run("t", "#1", "builder", "claude", now=now - 3600), "failed", now=now - 3000)
        for t, pct in ((now - 3600, 10), (now - 3000, 22)):
            self.ledger.record_usage([usage.observation("claude", "seven_day", pct, now + 86400, "test")], now=t)
        self.assertIsNone(dispatch.check_progress(self.ledger, "t", now))
        self.assertEqual(dispatch.check_progress(self.ledger, "t", now, min_work=1800)["kind"], "no_progress")


if __name__ == "__main__":
    unittest.main()
