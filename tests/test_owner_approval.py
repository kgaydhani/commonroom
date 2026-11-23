"""commonroom#413: `[goal] approve = "item"` holds ready builder and scout work until it is labelled owner:approved."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import config, dispatch, gate, handshake, registry
from commonroom.ledger import Ledger
from tests.test_dispatch import FakeBoard, cfg_with, issue
from tests.test_handshake import Room

ALLOWED = {"claude": gate.Verdict(True, "ok"), "codex": gate.Verdict(False, "dry")}


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, issues, approve=None, **kw):
        cfg = cfg_with(self.tmp.name)
        if approve:
            cfg["goal"]["approve"] = approve
        board = FakeBoard(issues)
        return dispatch.Dispatcher(cfg, self.ledger, board=board, log=lambda *_: None, **kw), board

    def started(self, d):
        picked = d.pick(ALLOWED)
        return picked and picked[0].issue and (picked[0].role, picked[0].issue["number"])

    def test_without_the_key_a_ready_build_is_picked_as_today(self):
        # test_dispatch's test_scout_interleaves_with_builds board and result, with no `approve` key and with "never".
        for approve in (None, "never"):
            issues = [issue(60, ["status:ready", "role:builder"]), issue(61, ["status:ready", "role:scout"], milestone="M1")]
            d, board = self.dispatcher(issues, approve)
            self.assertEqual(self.started(d), ("scout", 61))
            run = self.ledger.start_run("t", "#61", "scout", "claude"); self.ledger.finish_run(run, "done", outputs=[{"kind": "pr", "ref": "PR #70"}])
            self.assertEqual(self.started(d), ("builder", 60))
            self.assertEqual(board.label_calls, [])
            self.ledger.db.execute("DELETE FROM runs"); self.ledger.db.commit()

    def test_item_holds_a_build_until_it_is_approved(self):
        issues = [issue(60, ["status:ready", "role:builder"])]
        d, board = self.dispatcher(issues, "item")
        self.assertFalse(self.started(d))
        self.assertEqual(d.ready_builds(board.open_issues(None)), [])
        issues[0]["labels"].append({"name": "owner:approved"})
        self.assertEqual(self.started(d), ("builder", 60))
        self.assertEqual(board.label_calls, [])   # the room never adds or removes labels for this

    def test_item_holds_a_scout_until_it_is_approved(self):
        issues = [issue(61, ["status:ready", "role:scout"])]
        d, board = self.dispatcher(issues, "item")
        self.assertFalse(self.started(d))
        issues[0]["labels"].append({"name": "owner:approved"})
        self.assertEqual(self.started(d), ("scout", 61))
        self.assertEqual(board.label_calls, [])

    def test_item_holds_the_extra_slot_too(self):
        issues = [issue(n, ["status:ready", "role:builder"]) for n in (60, 61, 62)] + [issue(63, ["status:ready", "role:scout"])]
        d, board = self.dispatcher(issues, "item", slot=2)
        self.ledger.start_run("t", "#99", "builder", "claude")   # slot 1 is busy
        with mock.patch.object(d, "pick_review", return_value=None), mock.patch.object(d, "pace_headroom_ok", return_value=True):
            self.assertIsNone(d.pick_extra(ALLOWED))
            for i in issues:
                i["labels"].append({"name": "owner:approved"})
            job, _, _ = d.pick_extra(ALLOWED)
        self.assertEqual((job.role, job.issue["number"]), ("builder", 60))

    def test_item_files_one_capability_sweep_and_waits_for_approval(self):
        d, board = self.dispatcher([], "item", dry_run=True)
        d.cfg["scout"] = {"focus": ["tools"], "every_hours": 24}
        with mock.patch.object(d, "create_sweep_issue", wraps=d.create_sweep_issue) as create:
            self.assertFalse(self.started(d))
            self.assertEqual(create.call_count, 1)
            board.issues.append(issue(80, ["status:ready", "role:scout", "kind:research"]) | {"title": "Capability sweep: tools"})
            self.assertFalse(self.started(d))
            self.assertEqual(create.call_count, 1)   # one is waiting: no second
        board.issues[0]["labels"].append({"name": "owner:approved"})
        self.assertEqual(self.started(d), ("scout", 80))

    def test_never_runs_the_sweep_at_once_as_today(self):
        d, _ = self.dispatcher([], dry_run=True)
        d.cfg["scout"] = {"focus": ["tools"], "every_hours": 24}
        job, _, _ = d.pick(ALLOWED)
        self.assertEqual((job.role, job.reason), ("scout", "scheduled capability sweep: tools"))


class Config(unittest.TestCase):
    def load(self, goal):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "room.toml"
            path.write_text(f'[project]\nname = "x"\n[goal]\nmilestone = "M"\n{goal}\n', encoding="utf-8")
            return config.load(path)

    def test_default_is_never_and_item_is_accepted(self):
        self.assertEqual(self.load("")["goal"]["approve"], "never")
        self.assertEqual(self.load('approve = "item"')["goal"]["approve"], "item")

    def test_an_unknown_value_is_a_config_error_naming_the_key(self):
        for value in ("plan", "sometimes"):
            with self.assertRaises(config.ConfigError) as caught:
                self.load(f'approve = "{value}"')
            self.assertIn("approve", str(caught.exception))
            self.assertIn(value, str(caught.exception))


class Digest(Room):
    def setUp(self):
        super().setUp()
        self.register()
        self.found, self.calls = [], []

    def toml(self, approve=None):
        line = f'approve = "{approve}"\n' if approve else ""
        (self.alpha / "room.toml").write_text(f'[project]\nname = "alpha"\nrepo = "me/alpha"\n[goal]\nmilestone = "M"\n{line}',
                                              encoding="utf-8")

    def ready(self, n, *extra, milestone="M"):
        self.found.append({"number": n, "labels": [{"name": l} for l in ("status:ready", *extra)], "milestone": {"title": milestone}})

    def line(self):
        def gh(args, timeout=30):
            self.calls.append(args)
            return self.found if "status:ready" in args else []
        reg = registry.load()
        ledger = Ledger()
        try:
            with mock.patch.object(handshake.board, "gh", gh):
                text = handshake.build_digest(reg, reg.projects["alpha"], ledger)
        finally:
            ledger.close()
        return [l for l in text.split("\n") if l.startswith("Ready items waiting")]

    def test_item_with_unapproved_ready_work_gets_one_line(self):
        self.toml("item")
        self.ready(4, "role:builder")
        self.ready(5, "role:scout")
        self.ready(6, "role:builder", "owner:approved")
        self.ready(7, "role:builder", milestone="other")
        self.ready(8, "role:playtester")
        self.assertEqual(self.line(), ["Ready items waiting for your approval: 2. To approve one, add the label `owner:approved`"
                                       " (`gh issue edit 4 --repo me/alpha --add-label owner:approved`)."])

    def test_item_with_everything_approved_has_no_line(self):
        self.toml("item")
        self.ready(6, "role:builder", "owner:approved")
        self.assertEqual(self.line(), [])

    def test_never_or_no_key_has_no_line_and_no_board_call(self):
        self.ready(4, "role:builder")
        for approve in (None, "never"):
            self.toml(approve)
            self.assertEqual(self.line(), [])
        self.assertFalse([a for a in self.calls if "status:ready" in a])


if __name__ == "__main__":
    unittest.main()
