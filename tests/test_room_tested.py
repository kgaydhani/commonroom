"""commonroom#335: with `[checks] test_before_review`, an approval counts with the room's passing run on the same head,
and the reviewer's prompt says so. The board is a fake; the carry test uses a real git fixture like test_carry."""
import unittest
from pathlib import Path

from commonroom import dispatch
import test_carry
from test_carry import PASS
from test_dispatch import issue
from test_self_serve import Base, MergeBoard, project_cfg

HEAD = "a" * 40
OLD = "b" * 40
CMD = "python -m unittest discover -s tests"
COMMIT_AT, ROOM_AT, APPROVE_AT = "2026-11-20T12:00:00Z", "2026-11-20T12:30:00Z", "2026-11-20T13:00:00Z"


def room(head=HEAD, command=CMD, at=ROOM_AT, result="PASS"):
    return {"createdAt": at, "body": f"[room] TESTS: {result} {head} {command}: Ran 9 tests in 1s OK"}


def review(body="REVIEW: APPROVE\n\nLooks right.", at=APPROVE_AT):
    return {"createdAt": at, "body": f"[agent:reviewer] {body}"}


def detail(comments, head=HEAD, committed=COMMIT_AT):
    return {"number": 50, "headRefName": "i7-thing", "headRefOid": head, "statusCheckRollup": [],
            "commits": [{"oid": head, "committedDate": committed}], "comments": list(comments)}


class Tested(Base):
    def make(self, on=True):
        cfg = project_cfg(Path(self.tmp.name), ci=False)
        cfg["checks"]["test"] = CMD
        if on:
            cfg["checks"]["test_before_review"] = True
        return self.dispatcher(cfg, MergeBoard([], {}))

    def test_a_bare_approve_with_the_rooms_pass_on_the_head_is_tested(self):
        self.assertTrue(self.make().tested(detail([room(), review()])))

    def test_flag_off_the_same_comments_are_not_tested(self):
        self.assertFalse(self.make(on=False).tested(detail([room(), review()])))

    def test_a_pass_for_an_older_head_does_not_count(self):
        self.assertFalse(self.make().tested(detail([room(head=OLD), review()])))

    def test_an_approval_with_its_own_fail_or_incomplete_line_blocks(self):
        for line in ("TESTS: FAIL aaaaaaa " + CMD + ": 1 failed", "TESTS: INCOMPLETE aaaaaaa " + CMD + ": timeout"):
            with self.subTest(line):
                self.assertFalse(self.make().tested(detail([room(), review(f"REVIEW: APPROVE\n\n{line}")])))

    def test_an_approve_before_the_last_commit_does_not_count(self):
        self.assertFalse(self.make().tested(detail([room(at="2026-11-20T10:30:00Z"), review(at="2026-11-20T11:00:00Z")])))

    def test_a_verdict_that_is_not_an_approve_or_a_pass_of_another_command_does_not_count(self):
        self.assertFalse(self.make().tested(detail([room(), review("REVIEW: CHANGES\n\nNo.")])))
        self.assertFalse(self.make().tested(detail([room(command="true"), review()])))
        self.assertFalse(self.make().tested(detail([room(result="FAIL"), review()])))

    def test_the_approvals_own_passing_line_still_works_with_the_flag_on_or_off(self):
        own = review(f"REVIEW: APPROVE\n\nTESTS: PASS {HEAD[:7]} {CMD}: Ran 9 tests OK")
        for on in (True, False):
            self.assertTrue(self.make(on).tested(detail([own])))


class Prompt(Base):
    def render(self, on, comments):
        cfg = project_cfg(Path(self.tmp.name), ci=False)
        cfg["checks"]["test"] = CMD
        if on:
            cfg["checks"]["test_before_review"] = True
        d = self.dispatcher(cfg, MergeBoard([], {}))
        return d.prompt(dispatch.Job("review", "reviewer", pr=detail(comments)), "i7-thing")

    def test_with_the_rooms_pass_the_prompt_gives_its_sha_and_line_and_drops_the_no_ci_text(self):
        text = self.render(True, [room()])
        self.assertIn(f"TESTS: PASS {HEAD} {CMD}: Ran 9 tests in 1s OK", text)
        self.assertIn("needs no `TESTS:` line", text)
        self.assertNotIn("your approval merges this PR", text)
        self.assertIn("REVIEW: INCOMPLETE", text)

    def test_without_a_pass_on_the_current_head_or_with_the_flag_off_it_is_the_no_ci_text(self):
        plain = self.render(False, [])
        self.assertIn("your approval merges this PR", plain)
        for text in (self.render(True, []), self.render(True, [room(head=OLD)]), self.render(False, [room()])):
            self.assertEqual(text, plain)


class CarriedRoomPass(test_carry.Carry):
    def test_an_approval_resting_on_the_rooms_pass_carries_across_a_clean_merge_of_main(self):
        head = self.merge_main()
        board = self.board(head)
        d0 = board.details[222]
        d0["comments"] = [{"createdAt": "2026-11-20T12:30:00Z", "body": f"[room] TESTS: PASS {self.tested} {PASS}: Ran 3 tests OK"},
                          {"createdAt": "2026-11-20T13:00:00Z", "body": "[agent:reviewer] REVIEW: APPROVE\n\nLooks right."}]
        d = self.dispatcher(self.cfg(test_before_review=True), board)
        self.assertEqual(d.carry_reason(board.pr_detail(222)), "")
        d.housekeeping()
        self.assertEqual(board.merged, [222])
        self.assertIn(f"CARRIES: APPROVE 2026-11-20T13:00:00Z TESTED {self.tested}", board.posted[-1][1])

    def test_with_the_flag_off_a_bare_approval_carries_nothing(self):
        head = self.merge_main()
        board = self.board(head)
        board.details[222]["comments"] = [
            {"createdAt": "2026-11-20T12:30:00Z", "body": f"[room] TESTS: PASS {self.tested} {PASS}: Ran 3 tests OK"},
            {"createdAt": "2026-11-20T13:00:00Z", "body": "[agent:reviewer] REVIEW: APPROVE\n\nLooks right."}]
        d = self.dispatcher(self.cfg(), board)
        self.assertNotEqual(d.carry_reason(board.pr_detail(222)), "")


for _name in dir(test_carry.Carry):   # the fixture's own tests run in test_carry, not again here
    if _name.startswith("test_") and _name not in vars(CarriedRoomPass):
        setattr(CarriedRoomPass, _name, None)


class Rounds(unittest.TestCase):
    def test_two_room_fails_at_different_heads_count_two_rounds_with_no_merge_only_check(self):
        fails = [{"createdAt": f"2026-11-20T1{i}:00:00Z",
                  "body": f"[room] TESTS: FAIL {str(i) * 40} {CMD}: exit 1\n\n{dispatch.carry_mod.PRE_REVIEW} failed"}
                 for i in (1, 2)]
        self.assertEqual(dispatch.changes_rounds({"comments": fails}), 2)


if __name__ == "__main__":
    unittest.main()
