"""commonroom#654: the ready cap is 25, and the director's prompt says the same number."""
import unittest
from pathlib import Path

from commonroom import config

ROOT = Path(__file__).resolve().parent.parent


class ReadyCap(unittest.TestCase):
    def test_default_ready_cap_is_25(self):
        self.assertEqual(config.DEFAULTS["board"]["ready_cap"], 25)

    def test_director_prompt_names_the_same_cap_and_parks_over_cap_work(self):
        text = (ROOT / ".claude" / "agents" / "director.md").read_text(encoding="utf-8")
        self.assertIn(f"at most {config.DEFAULTS['board']['ready_cap']} issues in `status:ready`", text)
        self.assertIn("labelled `parked`", text)


if __name__ == "__main__":
    unittest.main()
