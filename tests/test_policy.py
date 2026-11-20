"""The dispatcher's ways-of-working numbers (commonroom#217): their values must not change when they move."""
import unittest

from commonroom import dispatch, workers

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
    (workers, "RUN_TIMEOUT"): 3600,
}


class PinnedValues(unittest.TestCase):
    def test_every_constant_keeps_its_value(self):
        for (module, name), value in PINNED.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(module, name), value)
                self.assertIs(type(getattr(module, name)), int)


if __name__ == "__main__":
    unittest.main()
