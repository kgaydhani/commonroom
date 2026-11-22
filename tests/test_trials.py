import tempfile
import unittest
from datetime import date
from pathlib import Path

from commonroom import trials

REAL = Path(trials.__file__).resolve().parent / "ways" / "trials.toml"
BOUNDS = {"review_rounds": (1, None), "run_timeout_seconds": (300, 14400)}
NAMES = ("build", "review")

HEAD = 'name = "t1"\narea = "dispatch"\nstarted = "2026-12-01"\nshare = 0.3\nmeasure = "minutes_per_landing"\n' \
       'min_landings = 5\nmax_days = 14\n'
VARIANT = '[trial.variant]\nprompts = { build = "trials/b.md" }\npolicy = { review_rounds = 3 }\n'


class Trials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ways = Path(self.tmp.name)
        (self.ways / "prompts").mkdir()
        (self.ways / "trials").mkdir()
        (self.ways / "prompts" / "build.md").write_text("Do {{issue}}\n", encoding="utf-8")
        (self.ways / "trials" / "b.md").write_text("Better {{issue}}\n", encoding="utf-8")

    def load(self, *blocks):
        p = self.ways / "trials.toml"
        p.write_text("\n".join("[[trial]]\n" + b for b in blocks), encoding="utf-8", newline="\n")
        return trials.load(p, NAMES, BOUNDS)

    def bad(self, text, *needles):
        with self.assertRaises(ValueError) as cm:
            self.load(text)
        for n in needles:
            self.assertIn(n, str(cm.exception))

    def test_shipped_file_declares_nothing(self):
        self.assertEqual(trials.load(REAL, NAMES, BOUNDS), [])

    def test_valid_trial(self):
        (t,) = self.load(HEAD + VARIANT)
        self.assertEqual((t.name, t.share, t.prompts, t.policy), ("t1", 0.3, {"build": "trials/b.md"}, {"review_rounds": 3}))

    def test_errors(self):
        self.bad(HEAD.replace("minutes_per_landing", "nope") + VARIANT, "t1", "measure")
        self.bad(HEAD + '[trial.variant]\nprompts = { zzz = "trials/b.md" }\n', "t1", "variant.prompts", "zzz")
        self.bad(HEAD + '[trial.variant]\nprompts = { build = "trials/missing.md" }\n', "t1", "does not exist")
        (self.ways / "trials" / "c.md").write_text("{{other}}", encoding="utf-8")
        self.bad(HEAD + '[trial.variant]\nprompts = { build = "trials/c.md" }\n', "t1", "other")
        self.bad(HEAD + '[trial.variant]\npolicy = { nope = 1 }\n', "t1", "variant.policy", "nope")
        self.bad(HEAD + '[trial.variant]\npolicy = { review_rounds = 0 }\n', "t1", "variant.policy", "bounds")
        self.bad(HEAD.replace("0.3", "1") + VARIANT, "t1", "share")
        self.bad(HEAD.replace("0.3", "0") + VARIANT, "t1", "share")
        self.bad(HEAD + "[trial.variant]\n", "t1", "variant", "nothing")

    def test_duplicate_and_area_overlap(self):
        with self.assertRaises(ValueError) as cm:
            self.load(HEAD + VARIANT, HEAD + VARIANT)
        self.assertIn("duplicate", str(cm.exception))
        other = HEAD.replace('"t1"', '"t2"')
        with self.assertRaises(ValueError) as cm:
            self.load(HEAD + VARIANT, other + VARIANT)
        self.assertIn("area", str(cm.exception))
        ended = HEAD.replace('area = "dispatch"', 'area = "dispatch"\nended = "2026-12-10"')
        later = other.replace("2026-12-01", "2026-12-11")
        self.assertEqual(len(self.load(ended + VARIANT, later + VARIANT)), 2)

    def test_active(self):
        ended = HEAD.replace('area = "dispatch"', 'area = "dispatch"\nended = "2026-12-10"')
        ts = self.load(ended + VARIANT)
        self.assertEqual(trials.active(ts, date(2026, 11, 30)), [])
        self.assertEqual(len(trials.active(ts, date(2026, 12, 1))), 1)
        self.assertEqual(len(trials.active(ts, date(2026, 12, 10))), 1)
        self.assertEqual(trials.active(ts, date(2026, 12, 11)), [])

    def test_arm(self):
        (t,) = self.load(HEAD + VARIANT)
        self.assertEqual(trials.arm(t, "proj#1"), trials.arm(t, "proj#1"))
        self.assertEqual([trials.arm(t, f"proj#{i}") for i in range(1, 7)], EXPECTED)
        n = sum(trials.arm(t, f"p#{i}") == "variant" for i in range(1000))
        self.assertTrue(250 <= n <= 350, n)


EXPECTED = ["control", "control", "variant", "control", "control", "control"]

if __name__ == "__main__":
    unittest.main()
