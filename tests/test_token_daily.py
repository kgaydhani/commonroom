"""commonroom#643: housekeeping runs `room tokens collect` once per local day for the machine. Fake clock, fake collector,
a temporary room home: nothing reads the live ledger or transcripts."""
import os
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from commonroom import dispatch
from test_self_serve import Base, MergeBoard, project_cfg


def at(day, hour=12):
    return datetime(2026, 11, day, hour).timestamp()   # local time


class DailyTokens(Base):
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


if __name__ == "__main__":
    unittest.main()
