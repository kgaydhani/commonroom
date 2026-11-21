"""One-week raises use a temporary room home and ledger, never the live room (#242)."""
import contextlib
import io
import json
import os
from unittest import mock

from commonroom import cli, dispatch, owner_reserve, pie, raises, registry, standings
from tests.test_dispatch import cfg_with
from tests.test_standings import H, Room


class Raises(Room):
    def setUp(self):
        super().setUp()
        self.reg = self.register(30, 55)
        self.add_run("alpha-site", self.now - 2 * H, self.now - H, cost=28)
        self.add_run("beta-site", self.now - 2 * H, self.now - H, cost=20)
        self.reading(self.now - 2 * H, 0)
        self.reading(self.now - H, 48)

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(cli.time, "time", return_value=self.now):
            code = cli.main(["pie", *args])
        return code, out.getvalue(), err.getvalue()

    def report(self, provider="claude", now=None):
        return standings.standings(self.ledger, self.reg, provider, now or self.now)

    def shares(self, report=None):
        return {s.name: s.share for s in (report or self.report()).rows}

    def test_ten_points_moves_shares_and_lines_without_changing_the_registry(self):
        before = registry.default_path().read_bytes()
        self.assertEqual(self.command("raise", "alpha", "10")[0], 0)
        report = self.report()
        self.assertEqual(self.shares(report), {"alpha": 40, "beta": 45})
        self.assertEqual([s.spent for s in report.rows], [28, 20])
        self.assertAlmostEqual(report.rows[0].allowed, pie.line(40, .5, 8))
        self.assertAlmostEqual(report.rows[1].allowed, pie.line(45, .5, 8))
        self.assertEqual(report.owner_share, 15)
        self.assertEqual(registry.default_path().read_bytes(), before)
        code, out, _ = self.command()
        self.assertEqual(code, 0)
        self.assertIn("raise alpha +10 from beta 10; ends", out)
        self.assertIn("early reset", out)
        data = json.loads(self.command("--json")[1])["providers"]["claude"]
        self.assertEqual(data["raises"][0]["from"], {"beta": 10})
        self.assertEqual(data["raises"][0]["resets_at"], self.resets)
        self.assertNotIn(b"\r", raises.path().read_bytes())

    def test_forty_is_refused_naming_thirty_five_and_writes_nothing(self):
        code, _, err = self.command("raise", "alpha", "40")
        self.assertEqual(code, 1)
        self.assertIn("35 points available", err)
        self.assertFalse(raises.path().exists())

    def third(self, share=15):
        self.reg.projects["gamma"] = registry.Project("gamma", self.project_dir("gamma", "gamma-site"), share)
        registry.save(self.reg)

    def test_default_donors_are_proportional_and_never_include_the_owner(self):
        self.third(10)
        self.assertEqual(self.command("raise", "alpha", "9")[0], 0)
        self.assertEqual(raises.load()[0]["from"], {"beta": 7, "gamma": 2})
        self.assertEqual(self.report().owner_share, 5)

    def test_from_takes_only_named_projects_and_accepts_repeated_options(self):
        self.third()
        self.assertEqual(self.command("raise", "alpha", "10", "--from", "gamma")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 40, "beta": 55, "gamma": 5})
        self.assertEqual(self.command("raise", "alpha", "5", "--from", "gamma", "--from", "beta")[0], 0)
        self.assertAlmostEqual(sum(self.shares().values()), 100)

    def test_owner_slice_is_used_only_when_named_and_only_its_unspent_part(self):
        self.reading(self.now, 53)  # 5 own points, so only 10 of the 15-point slice remains
        self.assertEqual(self.command("raise", "alpha", "11", "--from", "@owner")[0], 1)
        self.assertEqual(self.command("raise", "alpha", "10", "--from", "owner")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 40, "beta": 55})
        self.assertEqual(self.report().owner_share, 5)

    def test_all_available_can_be_transferred_and_more_cannot(self):
        self.assertEqual(self.command("raise", "alpha", "35")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 65, "beta": 20})
        saved = raises.path().read_bytes()
        self.assertEqual(self.command("raise", "alpha", "1")[0], 1)
        self.assertEqual(raises.path().read_bytes(), saved)

    def test_fractional_transfers_keep_effective_shares_in_bounds(self):
        self.third()
        report = self.report()
        entries = []
        for _ in range(30):
            entries.append(raises.transfer(self.reg, report, entries, "alpha", .1, ["beta", "gamma"]))
            shares, owner = raises.effective(self.reg, entries)
            self.assertTrue(all(v >= 0 for v in shares.values()))
            self.assertLessEqual(sum(shares.values()), 100)
            self.assertEqual(owner, 0)

    def test_tiny_positive_points_never_write_zero_donations(self):
        self.third()
        self.assertEqual(self.command("raise", "alpha", "5e-324")[0], 0)
        self.assertTrue(all(v > 0 for v in raises.load()[0]["from"].values()))

    def test_normal_reset_and_early_reset_ignore_and_prune_raises(self):
        self.command("raise", "alpha", "10")
        self.assertIsNone(self.report(now=self.resets + 1))
        self.reading(self.now, 0, resets=self.resets + 86400)
        self.assertEqual(self.shares(), {"alpha": 30, "beta": 55})
        self.assertEqual(self.report().raises, [])
        self.assertEqual(len(raises.load()), 1)  # a read never writes
        self.assertEqual(self.command("raise", "alpha", "1")[0], 0)
        self.assertEqual(len(raises.load()), 1)
        self.assertEqual(raises.load()[0]["resets_at"], self.resets + 86400)

    def test_drop_without_a_changed_reset_time_also_expires_the_raise(self):
        self.command("raise", "alpha", "10")
        self.reading(self.now, 0)
        self.assertEqual(self.shares(), {"alpha": 30, "beta": 55})
        self.assertEqual(self.command("raise", "alpha", "1")[0], 0)
        self.assertEqual(len(raises.load()), 1)
        self.assertEqual(raises.load()[0]["reset_at"], self.now)

    def test_meter_flapping_and_reset_time_jitter_do_not_expire_a_raise(self):
        self.command("raise", "alpha", "10")
        self.reading(self.now, 47, resets=self.resets + 10)
        self.assertEqual(self.shares(), {"alpha": 40, "beta": 45})

    def test_clear_one_recipient_preserves_the_others_and_clear_all_restores_baseline(self):
        self.third()
        self.command("raise", "alpha", "10", "--from", "beta")
        self.command("raise", "gamma", "5", "--from", "beta")
        self.assertEqual(self.command("raise", "--clear", "alpha")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 30, "beta": 50, "gamma": 20})
        self.assertEqual(self.command("raise", "--clear")[0], 0)
        self.assertEqual(raises.load(), [])
        self.assertEqual(self.shares(), {"alpha": 30, "beta": 55, "gamma": 15})

    def test_raised_share_can_fund_a_later_raise_and_clear_protects_dependent_transfers(self):
        self.third()
        self.command("raise", "gamma", "20", "--from", "beta")
        self.assertEqual(self.command("raise", "alpha", "35", "--from", "gamma")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 65, "beta": 35, "gamma": 0})
        saved = raises.path().read_bytes()
        self.assertEqual(self.command("raise", "--clear", "gamma")[0], 1)
        self.assertEqual(raises.path().read_bytes(), saved)
        self.assertEqual(self.command("raise", "--clear", "alpha")[0], 0)
        self.assertEqual(self.command("raise", "--clear", "gamma")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 30, "beta": 55, "gamma": 15})

    def test_providers_have_independent_windows_and_optional_selection(self):
        self.reading(self.now, 0, resets=self.resets + 86400, provider="codex")
        self.assertEqual(self.command("raise", "alpha", "10")[0], 0)
        self.assertEqual(self.shares(self.report("codex")), {"alpha": 40, "beta": 45})
        self.assertEqual(len(raises.load()), 2)
        self.command("raise", "--clear", "--provider", "codex")
        self.assertEqual(self.shares(self.report("codex")), {"alpha": 30, "beta": 55})
        self.assertEqual(self.shares(), {"alpha": 40, "beta": 45})

    def test_multi_provider_refusal_is_atomic(self):
        self.reading(self.now, 0, provider="codex")
        # beta has 55 on codex but only 35 unspent on claude; neither provider gets a partial write.
        self.assertEqual(self.command("raise", "alpha", "40")[0], 1)
        self.assertFalse(raises.path().exists())

    def test_a_busy_writer_is_refused_without_a_lost_update(self):
        lock = raises.path().with_suffix(".lock")
        lock.write_text(str(os.getpid()), encoding="utf-8")
        code, _, err = self.command("raise", "alpha", "1")
        self.assertEqual(code, 1)
        self.assertIn("write is in progress", err)
        self.assertFalse(raises.path().exists())

    def test_failed_replace_preserves_the_old_file_and_removes_the_temporary_file(self):
        self.command("raise", "alpha", "1")
        saved = raises.path().read_bytes()
        with mock.patch.object(raises.os, "replace", side_effect=OSError("disk error")):
            self.assertEqual(self.command("raise", "alpha", "1")[0], 1)
        self.assertEqual(raises.path().read_bytes(), saved)
        self.assertEqual(list(self.home.glob("pie-raises.json.*.tmp")), [])

    def test_malformed_state_is_reported_without_replacing_it(self):
        for raw in ('{}', '[{"project": "alpha"}]', 'not json'):
            with self.subTest(raw=raw):
                raises.path().write_text(raw, encoding="utf-8")
                code, _, err = self.command("raise", "alpha", "1")
                self.assertEqual(code, 1)
                self.assertIn("pie-raises.json", err)
                self.assertEqual(raises.path().read_text(encoding="utf-8"), raw)

    def test_no_raise_is_identical_to_explicitly_empty_transfers(self):
        before = self.report()
        raises.save([])
        self.assertEqual(self.report(), before)
        self.assertNotIn("raises", json.loads(self.command("--json")[1])["providers"]["claude"])

    def test_owner_reserve_cache_observes_raise_clear_and_expiry_without_a_meter_change(self):
        def owner(now=None):
            found = owner_reserve._owner(self.ledger, self.reg, "claude", now or self.now, self.transcripts)
            return found and found[:2]
        self.assertEqual(owner(), (15, 0))
        self.command("raise", "alpha", "10", "--from", "@owner")
        self.assertEqual(owner(), (5, 0))
        self.command("raise", "--clear")
        self.assertEqual(owner(), (15, 0))
        self.assertIsNone(owner(self.resets + 1))

    def test_standings_read_transfers_beside_the_supplied_ledger(self):
        self.command("raise", "alpha", "10")
        with mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.root / "another-home")}):
            self.assertEqual(self.shares(), {"alpha": 40, "beta": 45})

    def test_invalid_arguments_and_missing_reading_do_not_write(self):
        for args in (("alpha", "nan"), ("alpha", "inf"), ("alpha", "-1"), ("alpha", "0"),
                     ("unknown", "1"), ("alpha", "1", "--from", "alpha"),
                     ("alpha", "1", "--from", "missing"), ("alpha",), ("--clear", "alpha", "1"),
                     ("alpha", "1", "--provider", "codex")):
            with self.subTest(args=args):
                self.assertEqual(self.command("raise", *args)[0], 1)
                self.assertFalse(raises.path().exists())

    def test_registry_changes_cannot_make_effective_shares_invalid_and_clear_repairs(self):
        self.command("raise", "alpha", "10")
        self.reg.projects["beta"].share = 5
        registry.save(self.reg)
        with self.assertRaisesRegex(registry.RegistryError, "no longer fit"):
            self.report()
        self.assertEqual(self.command("raise", "--clear")[0], 0)
        self.assertEqual(self.shares(), {"alpha": 30, "beta": 5})

    def test_clear_refuses_to_leave_invalid_transfers_after_a_registry_edit(self):
        self.third()
        self.command("raise", "gamma", "20", "--from", "beta")
        self.command("raise", "alpha", "15", "--from", "gamma")
        self.reg.projects["gamma"].share = 0
        self.reg.projects["beta"].share = 70
        registry.save(self.reg)
        saved = raises.path().read_bytes()
        self.assertEqual(self.command("raise", "--clear", "gamma")[0], 1)
        self.assertEqual(raises.path().read_bytes(), saved)
        self.assertEqual(self.command("raise", "--clear")[0], 0)

    def test_dispatcher_uses_the_effective_line_and_clear_restores_its_hold(self):
        self.reading(self.now, 48)
        cfg = cfg_with(self.reg.projects["alpha"].path)
        cfg["project"]["name"] = "alpha-site"
        verdict = {"claude": mock.Mock(allowed=True)}
        self.add_run("beta-site", self.now - 10)  # active beta prevents idle lending
        def held():
            return dispatch.pie_holds(self.ledger, cfg, verdict, self.now, self.transcripts)
        self.assertIn("claude", held())
        self.command("raise", "alpha", "20")  # line 29 > spend 28
        self.assertEqual(held(), {})
        self.command("raise", "--clear")
        self.assertIn("claude", held())
