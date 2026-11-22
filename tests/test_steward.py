import calendar
import time
import unittest
from unittest import mock

from commonroom import steward

NOW = calendar.timegm((2026, 11, 21, 12, 0, 0))


def issue(n, labels=(), body="", state="OPEN", updated=None, milestone="M"):
    d = {"milestone": {"title": milestone} if milestone else None, "number": n, "state": state, "body": body, "labels": [{"name": l} for l in labels]}
    if updated:
        d["updatedAt"] = updated
    return d


def slugs(found):
    return [(f["invariant"], f["number"]) for f in found]


class Invariants(unittest.TestCase):
    def test_unplaced_fires(self):
        found = steward.check([issue(1, ["bug"])], [], [], NOW, "M")
        self.assertEqual(slugs(found), [("unplaced", 1)])
        self.assertTrue(found[0]["detail"])

    def test_only_the_goal_milestone_counts_as_unplaced(self):
        board = [issue(1, ["bug"]), issue(2, ["bug"], milestone="Other"), issue(3, ["bug"], milestone=None)]
        self.assertEqual(slugs(steward.check(board, [], [], NOW, "M")), [("unplaced", 1)])
        self.assertEqual(steward.check(board, [], [], NOW), [])   # no milestone configured: nothing

    def test_parked_and_closed_are_not_unplaced(self):
        board = [issue(1, ["parked"]), issue(2, [], state="CLOSED"), issue(3, ["claim:external"])]
        self.assertEqual(steward.check(board, [], [], NOW, "M"), [])

    def test_blocked_on_nothing_fires(self):
        board = [issue(1, ["status:blocked"], "Depends on #2"), issue(2, [], state="CLOSED"),
                 issue(3, ["status:blocked"], "no deps")]
        self.assertEqual(slugs(steward.check(board, [], [], NOW)), [("blocked-on-nothing", 1), ("blocked-on-nothing", 3)])

    def test_blocked_near_misses(self):
        board = [issue(1, ["status:blocked"], "Depends on #2"), issue(2, ["status:ready"]),
                 issue(3, ["status:blocked", "needs:director"]), issue(4, ["status:blocked", "owner:human"])]
        self.assertEqual(steward.check(board, [], [], NOW), [])

    def test_orphan_pr_fires(self):
        prs = [{"number": 50, "headRefName": "i7-thing"}]
        self.assertEqual(slugs(steward.check([issue(7, [], state="CLOSED")], prs, [], NOW)), [("orphan-pr", 50)])

    def test_pr_of_open_issue_or_other_branch_is_not_orphan(self):
        prs = [{"number": 50, "headRefName": "i7-thing"}, {"number": 51, "headRefName": "feature"}]
        self.assertEqual(steward.check([issue(7, ["status:review"])], prs, [], NOW), [])

    def test_stale_claim_fires_and_matches_dispatch(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3 * 3600))
        board = [issue(9, ["status:in-progress"], updated=old)]
        self.assertEqual(slugs(steward.check(board, [], [], NOW)), [("stale-claim", 9)])

    def test_held_claim_is_not_stale(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3 * 3600))
        board = [issue(9, ["status:in-progress"], updated=old)]
        self.assertEqual(steward.check(board, [], [{"role": "builder", "task": "#9"}], NOW), [])

    def test_owner_task_overdue_fires(self):
        board = [issue(4, ["owner:human"], "Do it.\nDue: 2026-11-20\n")]
        self.assertEqual(slugs(steward.check(board, [], [], NOW)), [("owner-overdue", 4)])

    def test_due_today_future_or_other_issue_is_not_overdue(self):
        board = [issue(4, ["owner:human"], "Due: 2026-11-21"), issue(5, ["owner:human"], "Due: 2026-12-01"),
                 issue(6, ["owner:human"], "Due: 2026-11-01", state="CLOSED"), issue(7, ["parked"], "Due: 2026-01-01")]
        self.assertEqual(steward.check(board, [], [], NOW), [])

    def test_clean_board_is_empty(self):
        board = [issue(1, ["status:ready"]), issue(2, ["status:review"]), issue(3, ["needs:director"])]
        self.assertEqual(steward.check(board, [{"number": 60, "headRefName": "i2-x"}], [], NOW), [])

    def test_no_subprocess(self):
        boom = mock.Mock(side_effect=AssertionError("steward must not shell out"))
        with mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom), \
                mock.patch("commonroom.board.gh", boom):
            steward.check([issue(1, ["bug"]), issue(2, ["owner:human"], "Due: 2020-01-01")], [], [], NOW)
        boom.assert_not_called()



DIR = calendar.timegm((2026, 11, 10, 0, 0, 0))


def made(n, created, **kw):
    d = issue(n, ["status:ready"], **kw)
    d["createdAt"] = created
    return d


class Unreconciled(unittest.TestCase):
    OLD, NEW = "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z"

    def run_it(self, issues, reconciled=None):
        return slugs(steward.unreconciled(issues, DIR, reconciled or {}, "M"))

    def test_an_old_issue_with_no_mark_is_a_finding(self):
        self.assertEqual(self.run_it([made(1, self.OLD)]), [("unreconciled", 1)])

    def test_a_mark_after_the_direction_clears_it_and_one_before_does_not(self):
        self.assertEqual(self.run_it([made(1, self.OLD)], {1: DIR + 5}), [])
        self.assertEqual(self.run_it([made(1, self.OLD)], {1: DIR - 5}), [("unreconciled", 1)])

    def test_new_closed_and_outside_milestone_issues_are_not_findings(self):
        board = [made(1, self.NEW), made(2, self.OLD, state="CLOSED"), made(3, self.OLD, milestone="Other")]
        self.assertEqual(self.run_it(board), [])

    def test_check_is_unchanged_without_a_direction_mark(self):
        board = [made(1, self.OLD), issue(2, ["bug"])]
        self.assertEqual(slugs(steward.check(board, [], [], NOW, "M")), [("unplaced", 2)])
        self.assertEqual(slugs(steward.check(board, [], [], NOW, "M", None, {})), [("unplaced", 2)])
        self.assertEqual(slugs(steward.check(board, [], [], NOW, "M", DIR, {})), [("unplaced", 2), ("unreconciled", 1)])


if __name__ == "__main__":
    unittest.main()
