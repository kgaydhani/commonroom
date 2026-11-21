"""Tell the owner when his own session spends a per-model weekly window that runs out before its reset (commonroom#256,
part 4). Every ledger, registry and transcripts folder is temporary (tests.test_standings.Room), never ~/.commonroom or
~/.claude/projects."""
import json
from datetime import datetime, timezone
from unittest import mock

from commonroom import dispatch, model_notice, registry, sessions, usage, weekly
from commonroom.registry import Project, Registry
from tests.test_dispatch import FakeBoard, cfg_with
from tests.test_pie_notice import Phone
from tests.test_standings import H, Room

DAY = 86400
TOP = "seven_day_fable"
TOP_MODEL, CHEAPER = "claude-fable-5-2", "claude-sonnet-5-5"


class OwnSessionOnTheTopModel(Room):
    def setUp(self):
        super().setUp()
        self.dirs = {key: self.project_dir(key, f"{key}-site") for key in ("alpha", "beta")}
        registry.save(Registry({"alpha": Project("alpha", self.dirs["alpha"], 40),
                                "beta": Project("beta", self.dirs["beta"], 20)}))
        cfg = cfg_with(self.dirs["alpha"])
        cfg["project"]["name"] = "alpha-site"
        self.phone = Phone()
        self.d = dispatch.Dispatcher(cfg, self.ledger, board=FakeBoard([]), log=lambda *_: None, notifier=self.phone)
        self.add_run("alpha-site", self.now - 5 * H, self.now - 4 * H, cost=1, model=CHEAPER)

    def readings(self, top=(60, 72), windows=("seven_day", TOP)):
        # All models 40 -> 45 over the day lasts the week; the top model 60 -> 72 runs out in 2.3 days, before the reset.
        for w, (first, last) in (("seven_day", (40, 45)), (TOP, top)):
            if w in windows:
                for at, pct in ((self.now - DAY, first), (self.now - H, last)):
                    self.ledger.record_usage([usage.observation("claude", w, pct, self.resets, "test")], now=at)

    def own_session(self, model=TOP_MODEL, ago=60):
        at = datetime.fromtimestamp(self.now - ago, timezone.utc).isoformat()
        line = {"type": "assistant", "timestamp": at, "sessionId": "s1", "cwd": self.dirs["alpha"],
                "message": {"id": "m1", "model": model, "content": [{"type": "text", "text": "private words"}],
                            "usage": {"input_tokens": 5, "output_tokens": 50, "cache_read_input_tokens": 400_000}}}
        path = self.transcripts / usage.session_folder(self.dirs["alpha"]) / "s1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        return path

    def run_pass(self):
        with mock.patch("commonroom.dispatch.time.time", return_value=self.now):
            return self.d.notify_model_window(self.transcripts)

    def test_a_session_on_a_window_running_out_gets_one_notice_and_is_not_changed(self):
        self.readings()
        path = self.own_session()
        before = path.read_bytes()
        self.assertEqual(len(self.run_pass()), 1)
        (title, message), = self.phone.sent
        self.assertEqual(title, f"{TOP_MODEL} is using up {TOP}")
        self.assertIn(f"Your session in {self.dirs['alpha']} is on {TOP_MODEL}", message)
        self.assertIn(f"{TOP}: 72% used", message)
        self.assertIn("up to 400,005 tokens", message)
        self.assertIn(f"The same work on {CHEAPER} would spend the all-models week only (45% used now)", message)
        self.assertNotIn("private", message)
        self.assertEqual(self.run_pass(), [])                       # once per session per window period
        self.assertEqual(len(self.phone.sent), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_a_window_that_lasts_the_week_sends_nothing(self):
        self.readings(top=(60, 62))
        self.own_session()
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.phone.sent, [])

    def test_a_session_on_another_model_or_quiet_for_a_while_sends_nothing(self):
        self.readings()
        self.own_session(model=CHEAPER)
        self.assertEqual(self.run_pass(), [])
        self.own_session(ago=3600)
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.phone.sent, [])

    def test_with_one_weekly_window_nothing_is_read_or_sent(self):
        self.readings(windows=("seven_day",))
        self.own_session()
        with mock.patch.object(sessions, "spans", side_effect=AssertionError("read the transcripts")):
            self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.phone.sent, [])

    def test_with_no_room_run_off_the_window_it_names_no_model(self):
        p = weekly.Projection(TOP, 72, self.resets, self.now, 12, self.now + DAY)
        span = {"folder": "here", "context": 10}
        self.assertIsNone(model_notice.next_model([{"model": TOP_MODEL}], ["seven_day", TOP]))
        text = model_notice.text(span, TOP_MODEL, p, [p], None)
        self.assertIn("The same work on a model without its own weekly window would spend the all-models week only (?% ",
                      text)
