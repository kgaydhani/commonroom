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
    (dispatch, "HEARTBEAT_FAILURES"): 3,
    (workers, "RUN_TIMEOUT"): 3600,
}


# Values added to the table since the move, with the value each was added at.
NEW = {"prompt_max_tokens": 700, "templates_max_tokens": 3300,   # commonroom#231; 3300: #624 (trimmed to 3200); 3332: #571; 3308: #515; 3290: #455; 3200: #417 (trimmed to 3100); 3342: #410; 3300: #399; 3282: #367; 3222: #368; 3209: #366; 3180: #363; 3122: #335; 2530: #262; 2914: #223; 3024: #316; 3080: #208
       "pie_idle_minutes": 60,                                    # commonroom#240: today's [share] idle_minutes
       "pie_notice_margin": 1, "pie_own_active_minutes": 20,      # commonroom#243
       "cold_return_minutes": 60, "cold_return_context": 150000,   # commonroom#367
       "stamp_cooldown_seconds": 900,   # commonroom#434
       "handoff_offer_hours": 72,       # commonroom#384
       "unread_note_hours": 24,         # commonroom#588
       "looking_max_percent": 35,       # commonroom#440
       "person_max_lines": 60, "person_max_tokens": 800,   # commonroom#573
       "person_stale_days": 180,        # commonroom#574
       "places_listed": 5, "places_days": 14, "session_start_max_tokens": 300, "fresh_start_days": 3, "fresh_start_places": 3,   # commonroom#578
       "question_hours": 24,            # commonroom#577
       "owner_floor_weekly": 5}                                   # commonroom#241: the director's decision


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
                 "rework_resume_seconds": ("REWORK_RESUME", 0),   # 3300 until #609
                 "progress_min_work_seconds": ("PROGRESS_MIN_WORK", 6 * 3600), "notify_max": ("NOTIFY_MAX", 5), "heartbeat_failures": ("HEARTBEAT_FAILURES", 3),
                 "run_timeout_seconds": ("RUN_TIMEOUT", 3600)}
        with open(REPO / "commonroom" / "ways" / "policy.toml", "rb") as f:
            raw = tomllib.load(f)
        raw = {k: v["value"] if isinstance(v, dict) else v for k, v in raw.items()}   # bounded: { value = .., min = .. }
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

    def test_a_bound_entry_is_checked_and_the_rooms_value_sits_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            for entry, error in (("{ value = 0, min = 1 }", "notify_max must be a whole number of at least 1, not 0"),
                                 ("{ value = 9, min = 1, max = 8 }", "notify_max must be a whole number between 1 and 8"),
                                 ("{ value = 5, min = 6, max = 5 }", "notify_max max must be a whole number of at least 6"),
                                 ("{ value = 5, mni = 1 }", "notify_max takes value, min and max, not mni"),
                                 ("{ min = 1 }", "notify_max takes value, min and max")):
                with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, error):
                    policy.load_policy(self.write(tmp, self.full(notify_max=entry)))
            values, bounds = policy.read_table(self.write(tmp, self.full(notify_max="{ value = 5, min = 1, max = 9 }")))
        self.assertEqual((values, bounds["notify_max"], bounds["ci_reruns"]), (policy.DEFAULTS, (1, 9), (0, None)))
        # The shipped table can't contradict itself: each room value sits inside its own floor and ceiling.
        for name, value in policy.DEFAULTS.items():
            low, high = policy.BOUNDS[name]
            self.assertTrue(low <= value and (high is None or value <= high), name)

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
        with self.assertRaisesRegex(ValueError, "progress_min_work_seconds is room-wide only"):
            self.dispatcher("a", {"progress_min_work_seconds": 1})
        for name in ("prompt_max_tokens", "templates_max_tokens"):
            with self.assertRaisesRegex(ValueError, f"{name} is room-wide only because it limits the room's own text"):
                self.dispatcher("a", {name: 5000})
        with self.assertRaisesRegex(ValueError, "pie_idle_minutes is room-wide only because every project's dispatcher"):
            self.dispatcher("a", {"pie_idle_minutes": 30})
        with self.assertRaisesRegex(ValueError, "owner_floor_weekly is room-wide only because it keeps the owner's"):
            self.dispatcher("a", {"owner_floor_weekly": 1})
        for name in ("cold_return_minutes", "cold_return_context", "handoff_offer_hours"):
            self.assertIn(name, policy.GLOBAL_ONLY)
            with self.assertRaisesRegex(ValueError, f"{name} is room-wide only because it shapes the owner's own"):
                self.dispatcher("a", {name: policy.DEFAULTS[name]})
        with self.assertRaisesRegex(ValueError, "review_rounds must be a whole number"):
            self.dispatcher("a", {"review_rounds": True})

    def test_an_override_outside_its_floor_or_ceiling_fails_at_start_naming_it(self):
        for name, value, span in (("review_rounds", 0, "of at least 1"), ("incomplete_rounds", 0, "of at least 1"),
                                  ("ci_bounces", 0, "of at least 1"), ("room_error_escalate", 0, "of at least 1"),
                                  ("room_error_backoff_seconds", 59, "of at least 60"),
                                  ("run_timeout_seconds", 299, "between 300 and 14400"),
                                  ("run_timeout_seconds", 14401, "between 300 and 14400"),
                                  ("run_timeout_seconds", 0, "between 300 and 14400")):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, f"room.toml \\[policy\\]: {name} must be a whole number "
                                                        f"{span}, not {value}"):
                    self.dispatcher("a", {name: value})

    def test_every_in_range_value_still_works(self):
        for name in dispatch.POLICY_CONSTANTS:
            if name in policy.GLOBAL_ONLY:
                continue
            low, high = policy.BOUNDS[name]
            for value in (low, policy.DEFAULTS[name], high if high is not None else 10 ** 6):
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.dispatcher("a", {name: value}).policy(name), value)

    def test_the_progress_breakers_timing_is_room_wide_only(self):
        for value in (0, 21600, 86400):
            with self.assertRaisesRegex(ValueError, "progress_min_work_seconds is room-wide only because it is part "
                                                    "of the progress breaker's timing"):
                self.dispatcher("a", {"progress_min_work_seconds": value})
        # The room's value still reaches check_progress, with or without other overrides.
        for d in (self.dispatcher("a"), self.dispatcher("b", {"review_rounds": 4})):
            self.assertEqual(d.policy("progress_min_work_seconds"), dispatch.PROGRESS_MIN_WORK)
        self.assertEqual(dispatch.PROGRESS_MIN_WORK, 6 * 3600)

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
