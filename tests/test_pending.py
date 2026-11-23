"""Scheduled registry changes (#488): temporary homes and made-up projects only."""
import contextlib
import io
import os
from datetime import datetime, timezone
from unittest import mock

from commonroom import beat, cli, pending, registry
from commonroom.lock import claim_lock, release_lock
from tests.test_registry import Home


class Pending(Home):
    AT = "2026-11-26T01:00Z"

    def setUp(self):
        super().setUp()
        registry.save(registry.Registry({
            name: registry.Project(name, self.project_dir(name), share)
            for name, share in (("alpha", 55), ("beta", 20))
        }))
        self.at = pending.parse_time(self.AT)
        self.lines, self.calls = [], []

    def command(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["projects", *args])
        return code, out.getvalue()

    def run_beat(self, now):
        return beat.beat(self.home, now=now, starter=lambda *a: self.calls.append(a), log=self.lines.append)

    def schedule(self, name="alpha", share="5", at=None, *extra):
        code, out = self.command("set", name, "--share", share, "--at", at or self.AT, *extra)
        self.assertEqual(code, 0, out)

    def test_future_change_applies_once_before_planning_and_logs_old_and_new(self):
        before = self.file.read_bytes()
        beta = registry.load().projects["beta"]
        self.schedule()
        self.assertEqual(self.file.read_bytes(), before)
        saved = pending.path().read_bytes()
        self.assertNotIn(b"\r", saved)
        self.assertEqual(self.run_beat(self.at - 1), 0)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(pending.path().read_bytes(), saved)
        with mock.patch.object(registry, "set_project", wraps=registry.set_project) as setter, \
                mock.patch.object(beat, "plan", wraps=beat.plan) as planner:
            self.assertEqual(self.run_beat(self.at + 1), 0)
        setter.assert_called_once_with("alpha", share=5.0, path=self.file)
        self.assertEqual(planner.call_args_list[0].args[1], 5)
        self.assertEqual(registry.load().projects["alpha"].share, 5)
        self.assertEqual(registry.load().projects["beta"], beta)
        self.assertEqual(pending.load(), [])
        self.assertEqual(self.lines, ["beat: pending alpha: share 55 -> 5.0"])
        self.assertEqual(self.run_beat(self.at + 2), 0)
        self.assertEqual(len(self.lines), 1)

    def test_overallocated_share_is_refused_without_registry_write_and_beat_runs(self):
        self.schedule(share="90")
        before = self.file.read_bytes()
        self.assertEqual(self.run_beat(self.at), 0)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(pending.load(), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("pending alpha", self.lines[0])
        self.assertIn("shares sum to 110, more than 100", self.lines[0])
        self.assertTrue(any(call[0][2] == "run" for call in self.calls))

    def test_unreadable_time_writes_nothing_even_without_a_room_home(self):
        for text in ("tomorrow", "2026-13-26T01:00Z", "2026-11-26", "2026-11-26T01:00"):
            with self.subTest(text=text):
                empty = self.root / "empty-home"
                with mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(empty)}):
                    code, out = self.command("set", "alpha", "--share", "5", "--at", text)
                self.assertEqual(code, 1)
                self.assertIn("can't read", out)
                self.assertFalse(empty.exists())

    def test_timezones_and_local_minutes(self):
        self.assertEqual(pending.parse_time("2026-11-26T03:00:00+02:00"), self.at)
        self.assertEqual(self.at, datetime(2026, 11, 26, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(pending.parse_time("2026-11-26 03:14"), datetime(2026, 11, 26, 3, 14).timestamp())

    def test_list_shows_pending_times_and_fields_without_consuming_them(self):
        self.schedule()
        saved = pending.path().read_bytes()
        code, out = self.command()
        self.assertEqual(code, 0)
        self.assertIn("pending alpha at 2026-11-26T01:00:00Z: share=5.0", out)
        self.assertEqual(pending.path().read_bytes(), saved)

    def test_cancel_drops_all_for_only_the_named_project_even_after_removal(self):
        self.schedule()
        self.schedule(at="2026-11-27T01:00Z")
        self.schedule(name="beta")
        self.assertEqual(self.command("remove", "alpha")[0], 0)
        before = self.file.read_bytes()
        code, out = self.command("set", "alpha", "--cancel-pending")
        self.assertEqual(code, 0)
        self.assertIn("cancelled 2", out)
        self.assertEqual([e["project"] for e in pending.load()], ["beta"])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.command("set", "alpha", "--cancel-pending")[0], 0)

    def test_modes_and_zero_share_apply_before_dispatchers_are_planned(self):
        self.schedule("alpha", "0", self.AT, "--work", "fixed", "--when", "spare", "--strict")
        self.assertEqual(self.run_beat(self.at), 0)
        project = registry.load().projects["alpha"]
        self.assertEqual((project.share, project.work, project.when, project.strict), (0, "fixed", "spare", True))
        self.assertFalse(any(call[0][2] == "run" and call[1] == project.path for call in self.calls))
        self.assertEqual(len(self.lines), 1)
        for change in ("share 55 -> 0.0", "work 'open' -> 'fixed'", "when 'paced' -> 'spare'", "strict False -> True"):
            self.assertIn(change, self.lines[0])

    def test_multiple_due_changes_run_in_time_order_and_refusal_does_not_stop_the_rest(self):
        self.schedule(share="0", at="2026-11-27T01:00Z")
        self.schedule(share="90")  # refused
        self.schedule(share="10")  # same time, recorded second
        self.schedule(name="beta", at="2026-11-28T01:00Z")
        self.assertEqual(self.run_beat(pending.parse_time("2026-11-27T01:00Z")), 0)
        self.assertEqual(registry.load().projects["alpha"].share, 0)
        self.assertEqual(len(pending.load()), 1)
        self.assertEqual(pending.load()[0]["project"], "beta")
        self.assertEqual(len(self.lines), 3)
        self.assertIn("refused", self.lines[0])
        self.assertIn("55 -> 10.0", self.lines[1])
        self.assertIn("10 -> 0.0", self.lines[2])

    def test_missing_project_is_consumed_and_logged(self):
        self.schedule()
        self.command("remove", "alpha")
        self.assertEqual(self.run_beat(self.at), 0)
        self.assertEqual(pending.load(), [])
        self.assertIn("no project 'alpha'", self.lines[0])

    def test_no_pending_file_changes_neither_registry_nor_beat_plan(self):
        before = self.file.read_bytes()
        self.assertEqual(self.run_beat(self.at), 0)
        calls = self.calls[:]
        self.calls.clear()
        pending.path().write_text("[]\n", encoding="utf-8")
        self.assertEqual(self.run_beat(self.at), 0)
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.lines, [])

    def test_empty_registry_without_pending_keeps_the_existing_read_only_noop(self):
        home = self.root / "absent-home"
        self.assertEqual(beat.beat(home, starter=mock.Mock(side_effect=AssertionError("started a process"))), 0)
        self.assertFalse(home.exists())

    def test_busy_writer_preserves_pending_changes_and_beat_continues(self):
        self.schedule()
        saved = pending.path().read_bytes()
        lock = pending.path().with_suffix(".lock")
        self.assertIsNone(claim_lock(lock))
        self.addCleanup(release_lock, lock)
        self.assertEqual(self.command("set", "alpha", "--cancel-pending")[0], 1)
        self.assertEqual(self.run_beat(self.at), 0)
        self.assertIn("write is in progress", self.lines[0])
        self.assertEqual(pending.path().read_bytes(), saved)
        self.assertEqual(registry.load().projects["alpha"].share, 55)
        self.assertTrue(self.calls)

    def test_held_beat_lock_does_not_apply_changes(self):
        self.schedule()
        lock = self.home / "locks" / "beat.lock"
        self.assertIsNone(claim_lock(lock))
        self.addCleanup(release_lock, lock)
        self.assertEqual(self.run_beat(self.at), 0)
        self.assertEqual(registry.load().projects["alpha"].share, 55)
        self.assertEqual(len(pending.load()), 1)
        self.assertEqual(self.calls, [])

    def test_bad_options_do_not_write_and_immediate_set_still_works(self):
        before = self.file.read_bytes()
        for args in (("--at", self.AT), ("--share", "nan", "--at", self.AT),
                     ("--share", "10", "--cancel-pending")):
            self.assertEqual(self.command("set", "alpha", *args)[0], 1)
            self.assertFalse(pending.path().exists())
            self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.command("set", "alpha", "--share", "5")[0], 0)
        self.assertEqual(registry.load().projects["alpha"].share, 5)
        self.assertFalse(pending.path().exists())

    def test_malformed_file_is_preserved_and_beat_continues(self):
        pending.path().write_text("not json", encoding="utf-8")
        self.assertEqual(self.run_beat(self.at), 0)
        self.assertIn(pending.FILENAME, self.lines[0])
        self.assertEqual(pending.path().read_text(encoding="utf-8"), "not json")
        self.assertEqual(self.command()[0], 1)

    def test_failed_replace_preserves_queue_and_cleans_temporary_file(self):
        self.schedule()
        saved = pending.path().read_bytes()
        with mock.patch.object(pending.os, "replace", side_effect=OSError("disk error")):
            self.assertEqual(self.command("set", "alpha", "--cancel-pending")[0], 1)
        self.assertEqual(pending.path().read_bytes(), saved)
        self.assertEqual(list(self.home.glob(pending.FILENAME + ".*.tmp")), [])
