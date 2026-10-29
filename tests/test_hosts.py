"""commonroom#151: the room runs any project as its host. The heartbeat names the host, per-project heartbeat
commands, scheduled role runs, owner notifications to a phone, and no project hard-coded in room code."""
import ast
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from commonroom import config, dispatch, notify, services
from commonroom.ledger import Ledger

from test_dispatch import FakeBoard, cfg_with, issue

REPO = Path(__file__).resolve().parent.parent
MAGAZINE = Path("D:/Dev/magazine/room.toml")


class Notes:
    """A notifier that records what it was asked to send, or fails when told to."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, title, message, url=None, priority=None):
        if self.fail:
            raise OSError("ntfy unreachable")
        self.sent.append({"title": title, "message": message, "url": url, "priority": priority})


def at_local(hour, minute, days_ago=0):
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return datetime.datetime.combine(day, datetime.time(hour, minute)).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "room.sqlite3", synchronous="OFF")
        self.cfg = cfg_with(self.root)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def dispatcher(self, issues=(), notifier=None, **kw):
        self.board = FakeBoard(list(issues))
        return dispatch.Dispatcher(self.cfg, self.ledger, board=self.board, log=lambda *_: None, notifier=notifier, **kw)


class HostFromHeartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.magazine, self.game, self.room = base / "magazine", base / "game", base / "room"
        for folder, name, reserve in ((self.magazine, "magazine", 20), (self.game, "game", 10)):
            folder.mkdir()
            (folder / "room.toml").write_text(f'[project]\nname = "{name}"\n[budget.claude]\nreserve_weekly = {reserve}\n')
        self.room.mkdir()
        (self.room / "room.toml").write_text(f'[project]\nname = "room"\n[share]\nhost = "{self.game.as_posix()}"\nmax = 0.15\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_heartbeats_project_is_the_guests_host(self):
        with mock.patch.dict(os.environ, {config.HOST_ENV: str(self.magazine)}):
            cfg = config.load(self.room / "room.toml")
        self.assertEqual(cfg["_host"], "magazine")
        self.assertEqual(cfg["budget"]["claude"]["reserve_weekly"], 20)
        self.assertEqual(cfg["share"]["max"], 0.15)                      # only the host moves

    def test_without_the_heartbeat_the_configured_host_stands(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(config.HOST_ENV, None)
            cfg = config.load(self.room / "room.toml")
        self.assertEqual(cfg["_host"], "game")

    def test_a_project_without_share_is_never_made_a_guest(self):
        with mock.patch.dict(os.environ, {config.HOST_ENV: str(self.game)}):
            cfg = config.load(self.magazine / "room.toml")
        self.assertNotIn("_host", cfg)
        self.assertEqual(cfg["share"], {})
        self.assertEqual(cfg["budget"]["claude"]["reserve_weekly"], 20)

    def test_a_heartbeat_for_the_guests_own_folder_gives_it_no_host(self):
        with mock.patch.dict(os.environ, {config.HOST_ENV: str(self.room)}):
            cfg = config.load(self.room / "room.toml")
        self.assertNotIn("_host", cfg)

    def test_owner_defaults_to_the_repo_owner(self):
        self.assertEqual(config.owner({"project": {"repo": "someone/thing"}}), "someone")
        self.assertEqual(config.owner({"project": {"repo": "someone/thing", "owner": "boss"}}), "boss")
        self.assertEqual(config.owner({"project": {}}), "")


class ThisRepo(unittest.TestCase):
    def test_room_code_names_no_project(self):
        """String values in room code (docstrings aside, which cite history) name no project, path or login."""
        for path in (REPO / "commonroom").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                    text = node.value.replace("kgaydhani/commonroom", "")
                    for word in ("gamemirage", "moonshots", "D:/Dev", "D:\\Dev", "kgaydhani"):
                        self.assertNotIn(word, text, f"{path.name}:{node.lineno}")

    def test_heartbeat_runs_project_commands_not_a_fixed_script(self):
        text = (REPO / "heartbeat.cmd").read_text(encoding="utf-8")
        self.assertNotIn("moonshots", text)
        self.assertNotIn("D:\\Dev", text)
        self.assertIn('"%PY%" "%~dp0room.py" tick >> "%LOGS%\\heartbeat-tick.log" 2>&1', text)
        lines = text.splitlines()
        cd = next(i for i, l in enumerate(lines) if l.startswith("cd /d"))
        host = next(i for i, l in enumerate(lines) if l == f'set "{config.HOST_ENV}=%CD%"')
        self.assertLess(cd, host)                                          # set after the cd into the project
        self.assertLess(host, lines.index("("))

    def test_agent_prompts_and_rules_are_project_neutral(self):
        for path in [REPO / "AGENTS.md", REPO / "README.md", *(REPO / ".claude" / "agents").glob("*.md")]:
            text = path.read_text(encoding="utf-8").lower()
            for phrase in ("the game", "game's", "gamemirage studio", "builds gamemirage", "gamemirage#376"):
                self.assertNotIn(phrase, text, path.name)

    @unittest.skipUnless(MAGAZINE.is_file(), "the magazine isn't on this machine")
    def test_the_magazine_loads_as_a_host_with_a_daily_curator(self):
        cfg = config.load(MAGAZINE)
        self.assertEqual(cfg["project"]["repo"], "kgaydhani/magazine")
        self.assertEqual(cfg["share"], {})
        self.assertNotIn("_host", cfg)
        self.assertIn("curator", dispatch.runnable_roles(cfg))
        if "curator" in cfg["schedule"]:
            self.assertTrue(cfg["schedule"]["curator"].get("at") or cfg["schedule"]["curator"].get("every_hours"))


class SteerReason(unittest.TestCase):
    def test_names_the_log_issue_only_when_set(self):
        self.assertEqual(dispatch.steer_reason({"director": {}}), dispatch.STEER_REASON)
        self.assertNotIn("gamemirage", dispatch.STEER_REASON)
        self.assertEqual(dispatch.steer_reason({"director": {"log_issue": 376}}), dispatch.STEER_REASON + " (#376)")
        self.assertEqual(dispatch.steer_reason({"director": {"log_issue": "o/game#376"}}), dispatch.STEER_REASON + " (o/game#376)")


class Schedule(Base):
    def setUp(self):
        super().setUp()
        agents = self.root / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "curator.md").write_text("curator")
        self.cfg["roles"]["curator"] = ["claude:opus"]
        self.cfg["schedule"] = {"curator": {"at": "07:00", "title": "Shortlist {date}", "labels": ["prio:high"]}}

    def test_due_at_the_latest_passed_time_once(self):
        now = at_local(9, 0)
        spec = {"at": "07:00"}
        self.assertTrue(dispatch.schedule_due(spec, None, now))
        self.assertTrue(dispatch.schedule_due(spec, at_local(7, 0, days_ago=1), now))    # yesterday's: today's is due
        self.assertFalse(dispatch.schedule_due(spec, at_local(7, 30), now))
        self.assertFalse(dispatch.schedule_due(spec, at_local(10, 0, days_ago=1), at_local(6, 0)))   # not 07:00 yet
        self.assertTrue(dispatch.schedule_due({"at": ["07:00", "19:00"]}, at_local(7, 30), at_local(19, 5)))
        self.assertTrue(dispatch.schedule_due({"every_hours": 6}, now - 6 * 3600, now))
        self.assertFalse(dispatch.schedule_due({"every_hours": 6}, now - 5 * 3600, now))
        self.assertFalse(dispatch.schedule_due({}, None, now))

    def test_fill(self):
        now = at_local(9, 0)
        self.assertEqual(dispatch.fill("Shortlist {date} ({weekday}) {other}", now),
                         time.strftime("Shortlist %Y-%m-%d (%A) {other}", time.localtime(now)))

    def test_opens_a_ready_role_issue_in_the_goal_milestone(self):
        d = self.dispatcher()
        now = at_local(9, 0)
        actions = d.open_scheduled(self.board.issues, now=now)
        title = time.strftime("Shortlist %Y-%m-%d", time.localtime(now))
        self.assertEqual(self.board.created, [(title, ("role:curator", "status:ready", "prio:high"), "M0")])
        self.assertEqual(self.board.ensured, ["role:curator", "status:ready", "prio:high"])
        self.assertIn("<!-- room-schedule:curator -->", self.board.created_bodies[0])
        self.assertIn("opened #900", actions[0])
        # The issue just opened is waiting: nothing more opens, now or at the next slot while it is still open.
        self.assertEqual(d.open_scheduled(self.board.issues, now=now + 60), [])
        self.assertEqual(d.open_scheduled(self.board.issues, now=now + 86400), [])
        self.assertEqual(len(self.board.created), 1)
        # Once the curator closed it, the same slot doesn't open another; the next day's does.
        self.board.issues[-1]["state"] = "CLOSED"
        self.assertEqual(d.open_scheduled(self.board.issues, now=now + 3600), [])
        d.open_scheduled(self.board.issues, now=now + 86400)
        self.assertEqual(len(self.board.created), 2)

    def test_housekeeping_opens_it_and_the_dispatcher_runs_the_role(self):
        d = self.dispatcher()
        d.housekeeping()
        self.assertEqual(len(self.board.created), 1)
        (title, names, _), = self.board.created                       # the next read lists it from GitHub
        self.board.issues.append(issue(901, names, body=self.board.created_bodies[0], milestone="M0") | {"title": title})
        self.assertEqual([(i["number"], role) for i, role in d.ready_on_demand(self.board.issues)], [(901, "curator")])

    def test_a_role_that_cannot_run_is_reported_and_nothing_opens(self):
        self.cfg["schedule"] = {"stylist": {"every_hours": 24}}
        d = self.dispatcher()
        actions = d.open_scheduled(self.board.issues)
        self.assertIn("stylist has no [roles] entry or agent file", actions[0])
        self.assertFalse(getattr(self.board, "created", []))

    def test_no_schedule_and_dry_run_open_nothing(self):
        self.cfg["schedule"] = {}
        self.assertEqual(self.dispatcher().open_scheduled([]), [])
        self.cfg["schedule"] = {"curator": {"every_hours": 24}}
        d = self.dispatcher(dry_run=True)
        self.assertIn("would open", d.open_scheduled([])[0])
        self.assertFalse(getattr(self.board, "created", []))
        self.assertIsNone(self.ledger.marked("t", "schedule:curator"))


class OwnerNotifications(Base):
    def task(self, n, title="Pick the picks"):
        found = issue(n, ["owner:human"])
        found["title"] = title
        return found

    def comment(self, body, minutes_ago):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
        return {"body": body, "createdAt": stamp, "url": f"https://example/{minutes_ago}"}

    def test_without_a_topic_there_is_no_notifier(self):
        self.assertIsNone(notify.from_config(self.cfg))
        self.assertIsNone(notify.from_config({"notify": {"ntfy_topic": "  "}}))
        self.assertIsNone(self.dispatcher().notifier)
        self.assertEqual(self.dispatcher([self.task(5)]).notify_owner(self.board.issues), [])
        found = notify.from_config({"notify": {"ntfy_topic": "secret-topic"}})
        self.assertEqual((found.topic, found.server), ("secret-topic", "https://ntfy.sh"))

    def test_ntfy_posts_json_with_utf8_title(self):
        sent = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def urlopen(request, timeout):
            sent.append((request.full_url, request.get_method(), json.loads(request.data.decode("utf-8")), timeout))
            return Response()

        with mock.patch.object(notify.urllib.request, "urlopen", urlopen):
            notify.Ntfy("topic-x", "https://push.example/").send("Picks £90", "body", url="https://gh/1", priority=4)
        url, method, payload, _ = sent[0]
        self.assertEqual((url, method), ("https://push.example", "POST"))
        self.assertEqual(payload, {"topic": "topic-x", "title": "Picks £90", "message": "body", "click": "https://gh/1",
                                   "priority": 4})

    def test_new_owner_tasks_are_pushed_once(self):
        notes = Notes()
        d = self.dispatcher([self.task(5), issue(6, ["role:builder"])], notifier=notes)
        self.assertEqual(d.notify_owner(self.board.issues), ["told the owner about #5"])
        self.assertEqual(notes.sent[0]["title"], "t: Pick the picks")
        self.assertEqual(notes.sent[0]["url"], "https://github.com/o/t/issues/5")
        self.assertEqual(d.notify_owner(self.board.issues), [])            # already told
        self.board.issues += [self.task(7, "A"), self.task(8, "B")]
        d.notify_owner(self.board.issues)
        self.assertEqual(notes.sent[1]["title"], "t: 2 new tasks for you")
        self.assertIn("#7 A\n#8 B", notes.sent[1]["message"])
        self.assertEqual(len(notes.sent), 2)

    def test_a_failed_push_is_tried_again(self):
        notes = Notes(fail=True)
        d = self.dispatcher([self.task(5)], notifier=notes)
        self.assertEqual(d.notify_owner(self.board.issues), [])
        notes.fail = False
        self.assertEqual(d.notify_owner(self.board.issues), ["told the owner about #5"])

    def test_mentions_after_the_first_pass_are_pushed(self):
        notes = Notes()
        old = issue(9, [], comments=[self.comment("@o bulletin from last week", 600)])
        d = self.dispatcher([old], notifier=notes)
        self.assertEqual(d.notify_owner(self.board.issues), [])            # the first pass replays nothing
        self.assertEqual(notes.sent, [])
        old["comments"] += [self.comment("[agent:director] nothing for you", 1),
                            self.comment("@o digest: 3 merges, 1 problem", 1),
                            self.comment("mail @other or user@o.com", 1)]
        self.assertEqual(d.notify_owner(self.board.issues), ["told the owner about a mention on #9"])
        self.assertEqual(notes.sent[0]["message"], "@o digest: 3 merges, 1 problem")
        self.assertEqual(notes.sent[0]["url"], "https://example/1")
        self.assertEqual(d.notify_owner(self.board.issues), [])            # not twice

    def test_breaker_stop_assigns_the_owner_and_pushes_once(self):
        notes = Notes()
        d = self.dispatcher(notifier=notes)
        d.room_root = Path("X:/room")
        d.escalate_breaker({"kind": "no_progress", "detail": "spent 12% with nothing merged"})
        title, names, _ = self.board.created[0]
        self.assertEqual((title, names), ("[You] Room stopped: no progress", ("owner:human",)))
        self.assertEqual(self.board.created_assignees[0], ("o",))
        self.assertIn("python X:/room/room.py breaker reset", self.board.created_bodies[0])
        self.assertEqual((notes.sent[0]["priority"], notes.sent[0]["url"]), (4, "https://github.com/o/t/issues/900"))
        self.board.issues.append(self.task(900))
        self.assertEqual(d.notify_owner(self.board.issues), [])            # already pushed

    def test_breaker_stop_without_a_notifier_still_files_the_issue(self):
        d = self.dispatcher()
        d.escalate_breaker({"kind": "no_progress", "detail": "x"})
        self.assertEqual(len(self.board.created), 1)


class SweepBody(Base):
    def test_names_the_mission_and_the_configured_machine_notes(self):
        calls = []
        self.cfg["scout"] = {"environment": "docs/machine.md"}
        d = self.dispatcher()
        with mock.patch.object(dispatch.board_mod, "gh", lambda args, *a, **k: calls.append(args) or "https://x/issues/4"):
            d.create_sweep_issue("tools")
            self.cfg["scout"] = {}
            d.create_sweep_issue("tools")
        bodies = [args[args.index("--body") + 1] for args in calls]
        self.assertIn("this machine (docs/machine.md), and would serve the project's mission", bodies[0])
        self.assertIn("this machine, and would serve", bodies[1])
        self.assertNotIn("game", bodies[0] + bodies[1])


class Tick(unittest.TestCase):
    def test_runs_the_projects_commands_in_its_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_root": tmp, "heartbeat": {"commands": [
                ["{python}", "-c", "open('ran.txt', 'w').write('yes')"],
                ["{python}", "-c", "raise SystemExit(3)"],
                ["no-such-program-anywhere"],
            ]}}
            lines = services.tick(cfg)
            self.assertEqual((Path(tmp) / "ran.txt").read_text(), "yes")
            self.assertTrue(lines[0].startswith(sys.executable) and lines[0].endswith("exit 0"), lines)
            self.assertTrue(lines[1].endswith("exit 3"))
            self.assertIn("no-such-program-anywhere: FileNotFoundError", lines[2])
        self.assertEqual(services.tick({"_root": ".", "heartbeat": {}}), [])


if __name__ == "__main__":
    unittest.main()
