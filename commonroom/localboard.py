"""A board for projects that are not code (commonroom#454): the issue methods of `GitHubBoard`, kept in files.

Layout under the folder it is given:
    board/items/<n>/item.json                         written once when the item is created
    board/items/<n>/events/<ns>-<pid>-<random>.json   one file per comment or change

Nothing is rewritten or deleted. A number is claimed with `os.mkdir`, which fails when the folder exists, so two boards on
one folder never share a number. An event is written as `*.tmp` and renamed to a name nobody has, so a reader sees
all of it or none. State is read by replaying the events in name order. Standard library only."""
import json
import os
import secrets
import time
from pathlib import Path

MARK = "[room]"      # the same prefix GitHubBoard puts on a room comment (dispatch.MARK; not imported, dispatch will import this)
AUTHOR = "room"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _write_new(path: Path, data: dict):
    """Write `data` to `path`, which must not exist yet: through a `.tmp` file, then a rename that fails on an existing name."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    try:
        os.rename(tmp, path)
    except FileExistsError:
        tmp.unlink()
        raise


class LocalBoard:
    """Board operations on files. Swappable for GitHubBoard: same call shapes, same dicts."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.items = self.folder / "board" / "items"
        self.items.mkdir(parents=True, exist_ok=True)

    # -- reading
    def _numbers(self) -> list[int]:
        return sorted(int(p.name) for p in self.items.iterdir() if p.name.isdigit() and p.is_dir())

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _item(self, number: int) -> dict | None:
        """The item as `board.issues()` shapes it, or None when it doesn't exist (or its create is still being written)."""
        base = self._read(self.items / str(number) / "item.json")
        if base is None:
            return None
        labels, state, milestone, comments = list(base.get("labels") or []), "OPEN", base.get("milestone"), []
        title, body, updated = base["title"], base.get("body") or "", base["ts"]
        events = self.items / str(number) / "events"
        for path in sorted(events.glob("*.json")) if events.is_dir() else []:
            ev = self._read(path)
            if ev is None or "kind" not in ev:
                continue
            updated = max(updated, ev.get("ts", 0))
            kind = ev["kind"]
            if kind == "comment":
                comments.append({"createdAt": _iso(ev["ts"]), "body": ev.get("body", ""), "author": {"login": ev.get("author", AUTHOR)},
                                 "ts": ev["ts"]})
            elif kind == "labels":
                labels = [n for n in labels if n not in ev.get("remove", [])] + [n for n in ev.get("add", []) if n not in labels]
            elif kind == "state":
                state = ev["state"]
            elif kind == "edit":
                title, body = ev["title"], ev["body"]
            elif kind == "milestone":
                milestone = ev["title"]
        return {"number": number, "title": title, "body": body, "state": state,
                "labels": [{"name": n} for n in labels], "assignees": [{"login": n} for n in base.get("assignees") or []],
                "milestone": {"title": milestone} if milestone else None,
                "author": {"login": base.get("author", AUTHOR)}, "createdAt": _iso(base["ts"]), "updatedAt": _iso(updated),
                "closedAt": None, "comments": [{k: v for k, v in c.items() if k != "ts"} for c in comments]}

    def all_issues(self) -> list[dict]:
        return [i for i in (self._item(n) for n in self._numbers()) if i]

    def open_issues(self, milestone: str | None) -> list[dict]:
        return [i for i in self.all_issues() if i["state"] == "OPEN"
                and (not milestone or (i["milestone"] or {}).get("title") == milestone)]

    def issue_state(self, number: int) -> str | None:
        item = self._item(number)
        return item["state"] if item else None

    def issue_comments(self, number: int, since: float) -> list[dict]:
        events = self.items / str(number) / "events"
        out = []
        for path in sorted(events.glob("*.json")) if events.is_dir() else []:
            ev = self._read(path)
            if ev and ev.get("kind") == "comment" and ev["ts"] >= since:
                out.append({"createdAt": _iso(ev["ts"]), "body": ev.get("body", ""), "author": {"login": ev.get("author", AUTHOR)}})
        return out

    # -- the PR side: a local board has no pull requests, so housekeeping finds nothing to merge (commonroom#455)
    def open_prs(self) -> list[dict]:
        return []

    def open_prs_by(self, author: str | None) -> list[dict]:
        return []

    def finished_prs(self) -> list[dict]:
        return []

    def queued_runs(self) -> list[dict]:
        return []

    def pr_for_branch(self, branch: str) -> dict | None:
        return None

    def pr_detail(self, number: int) -> dict:
        return {}

    def main_checks(self) -> list[dict]:
        return []

    def merge(self, number: int, *args, **kwargs):
        """No pull requests, nothing to merge."""

    def behind_main(self, detail: dict) -> int:
        return 0

    def update_branch(self, number: int):
        """Nothing to update."""

    def close_pr(self, number: int, *args, **kwargs):
        """Nothing to close."""

    def label_pr(self, number: int, *args, **kwargs):
        """Nothing to label."""

    def compare_since(self, *args, **kwargs) -> dict:
        return {}

    def own_patches(self, *args, **kwargs) -> dict:
        return {}

    # -- writing
    def _event(self, number: int, kind: str, **fields):
        if not (self.items / str(number) / "item.json").is_file():
            raise RuntimeError(f"no item #{number} on this board")
        events = self.items / str(number) / "events"
        events.mkdir(exist_ok=True)
        while True:
            ts = time.time()
            name = f"{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(4)}.json"
            try:
                _write_new(events / name, {"kind": kind, "ts": ts, **fields})
                return
            except FileExistsError:
                continue

    def create_issue(self, title: str, body: str, labels=(), milestone: str | None = None, assignees=()) -> int:
        number = (self._numbers() or [0])[-1] + 1
        while True:
            try:
                (self.items / str(number)).mkdir()
                break
            except FileExistsError:
                number += 1
        _write_new(self.items / str(number) / "item.json",
                   {"title": title, "body": body, "labels": list(labels), "milestone": milestone,
                    "assignees": list(assignees), "author": AUTHOR, "ts": time.time()})
        return number

    def comment(self, number: int, body: str):
        self._event(number, "comment", body=f"{MARK} {body}", author=AUTHOR)

    def owner_comment(self, number: int, text: str):
        """A comment from the owner: no `[room]` mark, author `owner` (commonroom#477)."""
        self._event(number, "comment", body=text, author="owner")

    def owner_close(self, number: int, text: str = ""):
        if text:
            self.owner_comment(number, text)
        self._event(number, "state", state="CLOSED")

    def set_labels(self, number: int, add=(), remove=()):
        if add or remove:
            self._event(number, "labels", add=list(add), remove=list(remove))

    def set_milestone(self, number: int, title: str):
        self._event(number, "milestone", title=title)

    def ensure_label(self, name: str):
        """Labels are free text here: nothing to create."""

    def close_issue(self, number: int, body: str):
        self.comment(number, body)
        self._event(number, "state", state="CLOSED")

    def edit_issue(self, number: int, title: str, body: str):
        self._event(number, "edit", title=title, body=body)
