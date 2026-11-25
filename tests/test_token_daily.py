"""commonroom#643: housekeeping runs `room tokens collect` once per local day for the machine. Fake clock, fake collector,
a temporary room home: nothing reads the live ledger or transcripts."""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from unittest import mock

from commonroom import dispatch
from test_self_serve import Base, MergeBoard, project_cfg


def at(day, hour=12):
    return datetime(2026, 11, day, hour).timestamp()   # local time


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name)                # the ledger lives here, so this is the room home
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home)})
        env.start(); self.addCleanup(env.stop)
        self.logs = []

    def dispatcher(self, cfg, board=None):
        return dispatch.Dispatcher(cfg, self.ledger, board=board or MergeBoard([], {}), log=self.logs.append)

    def cfg(self, name="commonroom"):
        return project_cfg(self.home, name=name)


class DailyTokens(Setup):
    def setUp(self):
        super().setUp()
        meters = mock.patch.object(dispatch.weights_mod, "METERS", ())     # these tests are about collect, not the fit
        meters.start(); self.addCleanup(meters.stop)

    def test_two_passes_a_day_collect_once_and_the_next_day_again(self):
        d = self.dispatcher(self.cfg())
        with mock.patch.object(dispatch.tokens_mod, "collect", return_value={"added": 3}) as collect:
            d.collect_tokens(at(25, 8)); d.collect_tokens(at(25, 23))
            self.assertEqual(collect.call_count, 1)
            d.collect_tokens(at(26, 1))
            self.assertEqual(collect.call_count, 2)
        self.assertEqual((self.home / "token-collect-day").read_text(), "2026-11-26\n")

    def test_several_projects_give_one_run_a_day(self):
        ds = [self.dispatcher(self.cfg(n)) for n in ("alpha", "beta", "gamma")]
        with mock.patch.object(dispatch.tokens_mod, "collect", return_value={"added": 0}) as collect:
            for d in ds:
                d.collect_tokens(at(25))
            self.assertEqual(collect.call_count, 1)

    def test_concurrent_dispatchers_collect_once(self):
        stamp = self.home / "token-collect-day"
        exists, read_text = Path.exists, Path.read_text
        for previous in (None, "2026-11-24\n"):
            with self.subTest(previous=previous):
                stamp.unlink(missing_ok=True)
                (self.home / "token-collect-2026-11-25").unlink(missing_ok=True)
                if previous is not None:
                    stamp.write_text(previous, encoding="utf-8", newline="\n")
                ready = Barrier(2, timeout=10)

                def check(path):
                    found = exists(path)
                    if path == stamp and previous is None:
                        ready.wait()  # both dispatchers have seen the missing marker
                    return found

                def read(path, *args, **kwargs):
                    text = read_text(path, *args, **kwargs)
                    if path == stamp:
                        ready.wait()  # both dispatchers have read yesterday before either can write
                    return text

                ds = [self.dispatcher(self.cfg(n)) for n in ("alpha", "beta")]
                with mock.patch.object(Path, "exists", check), mock.patch.object(Path, "read_text", read), \
                        mock.patch.object(dispatch.tokens_mod, "collect", return_value={"added": 3}) as collect:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        list(pool.map(lambda d: d.collect_tokens(at(25)), ds))
                    collect.assert_called_once_with()
                self.assertEqual(stamp.read_text(), "2026-11-25\n")
                self.assertFalse(any("failed" in line for line in self.logs), self.logs)

    def test_an_existing_date_marker_prevents_another_collect_on_upgrade(self):
        (self.home / "token-collect-day").write_text("2026-11-25\n", encoding="utf-8", newline="\n")
        with mock.patch.object(dispatch.tokens_mod, "collect") as collect:
            self.dispatcher(self.cfg("alpha")).collect_tokens(at(25))
            collect.assert_not_called()

    def test_a_failed_attempt_keeps_the_day_claimed_across_projects(self):
        ds = [self.dispatcher(self.cfg(n)) for n in ("alpha", "beta")]
        with mock.patch.object(dispatch.tokens_mod, "collect", side_effect=RuntimeError("boom")) as collect:
            ds[0].collect_tokens(at(25))
            # The atomic claim still prevents duplicates if the last-run date marker is lost.
            (self.home / "token-collect-day").unlink()
            ds[1].collect_tokens(at(25))
            self.assertEqual(collect.call_count, 1)
            ds[1].collect_tokens(at(26))
            self.assertEqual(collect.call_count, 2)
        self.assertEqual(self.logs, ["   tokens collect failed: RuntimeError: boom"] * 2)

    def test_a_failing_collector_is_logged_and_the_pass_goes_on(self):
        d = self.dispatcher(self.cfg())
        with mock.patch.object(dispatch.tokens_mod, "collect", side_effect=RuntimeError("boom")) as collect, \
                mock.patch.object(dispatch.Dispatcher, "take_measure") as after:
            d.housekeeping()
            self.assertEqual(collect.call_count, 1)
        self.assertEqual([l for l in self.logs if "tokens collect failed" in l], ["   tokens collect failed: RuntimeError: boom"])
        d2 = self.dispatcher(self.cfg())
        with mock.patch.object(dispatch.tokens_mod, "collect", side_effect=RuntimeError("boom")), \
                mock.patch.object(dispatch.Dispatcher, "clear_stray_ready", return_value=[]) as stray:
            d2.collect_tokens(at(26)); d2.housekeeping()
            stray.assert_called()                      # the work after the collector still ran

    def test_a_ledger_outside_the_room_home_collects_nothing(self):
        d = self.dispatcher(self.cfg())
        with mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.home / "elsewhere")}), \
                mock.patch.object(dispatch.tokens_mod, "collect") as collect:
            d.collect_tokens(at(25))
            collect.assert_not_called()
        self.assertFalse((self.home / "token-collect-day").exists())

    def test_a_dry_run_collects_nothing(self):
        d = self.dispatcher(self.cfg()); d.dry_run = True
        with mock.patch.object(dispatch.tokens_mod, "collect") as collect:
            d.collect_tokens(at(25))
            collect.assert_not_called()


class DailyWeights(Setup):
    def seed(self, days):
        import json
        import test_weights as tw
        rows, readings = tw.synthetic(days=days)
        (self.home / "token-rows.jsonl").write_text("".join(json.dumps(r) + chr(10) for r in rows), encoding="utf-8")
        for meter in ("seven_day", "five_hour"):
            for r in readings:
                self.ledger.db.execute(
                    "INSERT INTO usage (observed_at, first_seen, provider, window, used_pct, resets_at, source) "
                    "VALUES (?,?,?,?,?,?,?)", (r["observed_at"], r["first_seen"], "claude", meter, r["used_pct"], r["resets_at"], "test"))
        self.ledger.db.commit()

    def lines(self):
        from commonroom import weights
        return weights.read_history(self.home / weights.FILENAME)

    def test_a_new_day_records_one_line_per_meter_and_the_same_day_adds_nothing(self):
        self.seed(10)
        d = self.dispatcher(self.cfg())
        with mock.patch.object(dispatch.tokens_mod, "collect", return_value={"added": 0}):
            d.collect_tokens(at(25, 8)); d.collect_tokens(at(25, 23))
        got = self.lines()
        self.assertEqual(sorted((r["date"], r["meter"]) for r in got), [("2026-11-25", "five_hour"), ("2026-11-25", "seven_day")])
        with mock.patch.object(dispatch.tokens_mod, "collect", return_value={"added": 0}):
            d.collect_tokens(at(26, 8))
        self.assertEqual(len(self.lines()), 4)

    def test_too_little_data_records_nothing_and_housekeeping_goes_on(self):
        self.seed(2)
        d = self.dispatcher(self.cfg())
        with mock.patch.object(dispatch.tokens_mod, "collect", return_value={"added": 0}),                 mock.patch.object(dispatch.Dispatcher, "clear_stray_ready", return_value=[]) as stray:
            d.collect_tokens(at(25)); d.housekeeping()
            stray.assert_called()
        self.assertEqual(self.lines(), [])
        self.assertFalse(any("failed" in l for l in self.logs), self.logs)


if __name__ == "__main__":
    unittest.main()
