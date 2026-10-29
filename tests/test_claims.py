"""A run's ACTIONS claims checked against GitHub (commonroom#68). Fake fetches only: no network."""
import calendar
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from commonroom import cli, dispatch, workers
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with

START = calendar.timegm((2026, 10, 10, 1, 0, 0))
BEFORE, AFTER = "2026-10-10T00:30:00Z", "2026-10-10T01:05:00Z"


def fake(timelines=None, created=None):
    """A fetch over fixed data: `timelines` maps number -> [(event, at)], `created` maps number -> created_at."""
    calls = []

    def fetch(n, kind):
        calls.append((n, kind))
        if kind == "issue":
            return {"number": n, "created_at": (created or {}).get(n, BEFORE)}
        return [{"event": e, "created_at": at} for e, at in (timelines or {}).get(n, [])]
    fetch.calls = calls
    return fetch


class ClaimChecks(unittest.TestCase):
    def test_the_10_october_case(self):
        claims = ["#346 ready→blocked on #343", "#383 ready→blocked on #293", "#347 ready→blocked on #293"]
        fetch = fake({346: [("labeled", AFTER), ("unlabeled", AFTER)], 343: [("labeled", AFTER)],
                      293: [("labeled", AFTER)], 383: [("commented", AFTER)]})
        checked, unverified = dispatch.check_claims(claims, START, fetch)
        self.assertEqual(checked, claims)
        self.assertEqual(unverified, claims[1:])
        self.assertNotIn(293, {n for n, _ in fetch.calls})              # the "on #N" is not a subject

    def test_subjects(self):
        self.assertEqual(dispatch.claim_subjects("made #49 #45 #10 ready"), [49, 45, 10])
        self.assertEqual(dispatch.claim_subjects("closed #3, #4 and #5/#6 as stale"), [3, 4, 5, 6])
        self.assertEqual(dispatch.claim_subjects("#383 ready->blocked on #293"), [383])
        self.assertIsNone(dispatch.claim_subjects("wrote the Director log"))
        self.assertIsNone(dispatch.claim_subjects("closed gamemirage#12"))
        self.assertIsNone(dispatch.claim_subjects("closed kgaydhani/gamemirage#12"))
        self.assertIsNone(dispatch.claim_subjects("opened gamemirage PR #400", ["gamemirage"]))
        self.assertIsNone(dispatch.claim_subjects("filed Gamemirage issue #7", ["gamemirage"]))
        self.assertEqual(dispatch.claim_subjects("opened PR #400", ["gamemirage"]), [400])

    def test_a_created_claim_is_judged_by_its_subject_only(self):
        checked, unverified = dispatch.check_claims(["created #50 (split from #18)"], START, fake(created={50: AFTER}))
        self.assertEqual((checked, unverified), (["created #50 (split from #18)"], []))
        checked, unverified = dispatch.check_claims(["created #50 (split from #18)"], START, fake())
        self.assertEqual(unverified, ["created #50 (split from #18)"])

    def test_close_merge_and_comment_claims(self):
        claims = ["closed #13 as duplicate", "merged PR #403", "commented PR #54"]
        late = fake({13: [("closed", AFTER)], 403: [("merged", AFTER)], 54: [("commented", AFTER)]})
        self.assertEqual(dispatch.check_claims(claims, START, late), (claims, []))
        early = fake({13: [("closed", BEFORE)], 403: [("merged", BEFORE)], 54: [("commented", BEFORE)]})
        self.assertEqual(dispatch.check_claims(claims, START, early), (claims, claims))
        wrong = fake({13: [("labeled", AFTER)], 403: [("closed", AFTER)]})      # an event, but not the claimed one
        self.assertEqual(dispatch.check_claims(claims[:2], START, wrong)[1], claims[:2])

    # The 11 October false flags (commonroom#99): the verb that governs the subjects picks the evidence.
    ELEVENTH = {
        "closed suggestion #492 as accepted -> opened #494 (split)": {492: "closed"},
        "commented #418 and #419: block is a studio auth bug, re-ready after commonroom#89":
            {418: "commented", 419: "commented"},
        "commented PR #498 asking the director to merge": {498: "commented"},
        "commented #460 with 08:45 lab batch": {460: "commented"},
        "commented PR #476 needs director merge": {476: "commented"},
        "created label provider:claude:sonnet; pinned #494, #474, #516 for Sonnet builder A/B":
            {494: "labeled", 474: "labeled", 516: "labeled"},
    }
    EDIT = "#467 acceptance criteria amended: adds a playtest"

    def test_the_11_october_claims_verify_by_their_governing_verb(self):
        claims = list(self.ELEVENTH) + [self.EDIT]
        timelines = {n: [(event, AFTER)] for subjects in self.ELEVENTH.values() for n, event in subjects.items()}
        fetch = fake(timelines, created={492: BEFORE, 494: BEFORE})
        checked, unverified = dispatch.check_claims(claims, START, fetch)
        self.assertEqual((checked, unverified), (list(self.ELEVENTH), []))
        self.assertNotIn(467, {n for n, _ in fetch.calls})                     # an edit leaves no timeline event

    def test_the_11_october_claims_are_unverified_when_nothing_happened(self):
        claims = list(self.ELEVENTH) + [self.EDIT]
        self.assertEqual(dispatch.check_claims(claims, START, fake()), (list(self.ELEVENTH), list(self.ELEVENTH)))
        wrong = fake({492: [("labeled", AFTER)], 498: [("merged", AFTER)], 476: [("merged", AFTER)]})
        some = [c for c in self.ELEVENTH if set(self.ELEVENTH[c]) & {492, 498, 476}]
        self.assertEqual(dispatch.check_claims(some, START, wrong)[1], some)    # an event, but not the governing one

    def test_the_rule_comes_from_the_verb_nearest_the_subjects(self):
        rule = lambda claim: dispatch.claim_rule(claim, *dispatch.claim_span(claim)[1:])
        self.assertEqual(rule("closed #3, #4 and #5/#6 as stale"), {"closed"})
        self.assertEqual(rule("made #49 #45 #10 ready"), {"labeled", "unlabeled"})
        self.assertEqual(rule("#427 #428 -> prio:high"), {"labeled", "unlabeled"})
        self.assertEqual(rule("labelled #5 prio:high"), {"labeled", "unlabeled"})
        self.assertEqual(rule("reworded #5 then closed it"), "skip")
        self.assertIsNone(rule("approved PR #54"))
        stale = "closed #3, #4 and #5/#6 as stale"
        self.assertEqual(dispatch.check_claims([stale], START, fake()), ([stale], [stale]))
        ready = "made #49 #45 #10 ready"
        fetch = fake({n: [("labeled", AFTER)] for n in (49, 45, 10)})
        self.assertEqual(dispatch.check_claims([ready], START, fetch), ([ready], []))

    def test_a_claim_with_no_recognised_verb_is_skipped(self):
        fetch = fake({54: [("commented", AFTER)]})
        self.assertEqual(dispatch.check_claims(["approved PR #54", "rewrote #54 body"], START, fetch), ([], []))
        self.assertEqual(fetch.calls, [])

    def test_another_projects_claim_is_skipped(self):
        fetch = fake()
        self.assertEqual(dispatch.check_claims(["opened gamemirage PR #400"], START, fetch, {"gamemirage"}), ([], []))
        self.assertEqual(fetch.calls, [])

    def test_a_failed_read_leaves_the_claim_unchecked(self):
        def broken(n, kind):
            raise RuntimeError("gh: HTTP 502")
        self.assertEqual(dispatch.check_claims(["closed #13", "created #50"], START, broken), ([], []))

    def test_reads_are_cached_and_capped(self):
        fetch = fake({5: [("labeled", AFTER)]})
        dispatch.check_claims(["#5 ready", "made #5 blocked", "#5 parked"], START, fetch)
        self.assertEqual(fetch.calls, [(5, "timeline")])
        fetch = fake()
        claims = [f"closed #{n}" for n in range(1, 31)]
        checked, _ = dispatch.check_claims(claims, START, fetch)
        self.assertEqual(len(checked), dispatch.CLAIM_SUBJECTS_MAX)
        self.assertEqual(len({n for n, _ in fetch.calls}), dispatch.CLAIM_SUBJECTS_MAX)


class RecordedOnTheRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "room.sqlite3", synchronous="OFF")

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def direct(self, board, text):
        root = Path(self.tmp.name)
        (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "agents" / "director.md").write_text("---\nmodel: opus\n---\nDirect.\n")
        d = dispatch.Dispatcher(cfg_with(root), self.ledger, board=board, log=lambda *_: None)
        d.prepare_worktree = lambda job, provider=None: (root / "wt", None)
        d.remote_head = lambda branch: None
        d.prompt = lambda job, branch: "direct"
        d.preserve_work = lambda job, worktree, branch: None
        d.cleanup_worktree = lambda path, run_id=None: None
        orig_git, orig_run = dispatch.git, dispatch.run_worker
        dispatch.git = lambda args, cwd: ""
        dispatch.run_worker = lambda *a, **k: workers.WorkerResult(ok=True, final_text=text)
        try:
            return d.run_job(dispatch.Job("direct", "director", reason="steer"), "claude", "opus")
        finally:
            dispatch.git, dispatch.run_worker = orig_git, orig_run

    def test_a_director_run_keeps_its_result_and_records_what_did_not_apply(self):
        text = 'Done.\nACTIONS: ["#346 ready\\u2192blocked on #343", "#383 ready\\u2192blocked on #293", ' \
               '"opened gamemirage PR #400", "wrote the Director log"]'
        self.ledger.finish_run(self.ledger.start_run("gamemirage", "#1", "builder", "claude"), "done")
        plain = self.direct(FakeBoard([]), text)                               # a board that can't check: as before
        board = FakeBoard([])
        board.claim_record = fake({346: [("labeled", "2099-01-01T00:00:00Z")], 383: [("labeled", "2000-01-01T00:00:00Z")]})
        checked = self.direct(board, text)
        self.assertEqual((checked["status"], checked["outputs"]), (plain["status"], plain["outputs"]))
        self.assertEqual(checked["status"], "done"); self.assertEqual(len(checked["outputs"]), 4)
        runs = self.ledger.recent_runs("t")
        self.assertEqual(runs[0]["unverified"], ["#383 ready→blocked on #293"])
        self.assertEqual(runs[1]["unverified"], [])
        self.assertNotIn(400, {n for n, _ in board.claim_record.calls})
        cfg = cfg_with(self.tmp.name); cfg["project"]["repo"] = None
        orig = cli.Ledger
        cli.Ledger = lambda: Ledger(self.ledger.path, synchronous="OFF")
        try:
            result = cli.digest(cfg, 1)
        finally:
            cli.Ledger = orig
        self.assertEqual((result["summary"]["actions_reported"], result["summary"]["actions_unverified"]), (2, 1))
        self.assertEqual(result["summary"]["no_output"], 0)
        self.assertIn("#383 ready→blocked on #293", result["runs"][0]["unverified"])

    def test_a_failing_check_never_fails_the_run(self):
        board = FakeBoard([])
        board.claim_record = lambda n, kind: 1 / 0
        result = self.direct(board, 'ACTIONS: ["closed #13"]')
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.ledger.recent_runs("t")[0]["unverified"], [])

    def test_an_older_ledger_gains_the_column(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, project TEXT, task TEXT, role TEXT, provider TEXT, model TEXT, "
                   "effort TEXT, autonomous INTEGER NOT NULL DEFAULT 1, started_at REAL NOT NULL, ended_at REAL, "
                   "status TEXT NOT NULL, session_id TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, "
                   "outputs TEXT NOT NULL DEFAULT '[]', note TEXT)")
        db.execute("INSERT INTO runs (id, project, role, provider, started_at, status) VALUES ('a', 't', 'director', "
                   "'claude', ?, 'done')", (time.time(),))
        db.commit(); db.close()
        old = Ledger(path, synchronous="OFF")
        try:
            self.assertIn("unverified", {r[1] for r in old.db.execute("PRAGMA table_info(runs)")})
            self.assertEqual(old.recent_runs("t")[0]["unverified"], [])
        finally:
            old.close()


if __name__ == "__main__":
    unittest.main()
