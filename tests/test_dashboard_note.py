"""commonroom#412: `POST /api/note` writes the owner's note to a registered project's inbox."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dashboard, handshake, registry


class Poster(dashboard.Handler):
    def __init__(self, path, body, headers):
        self.path, self.headers = path, headers
        self.rfile, self.wfile = io.BytesIO(json.dumps(body).encode()), io.BytesIO()
        self.sent = None

    def send(self, code, body, kind):
        self.sent = (code, body)


class NoteEndpoint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.tmp / "home")})
        env.start()
        self.addCleanup(env.stop)
        work = self.tmp / "work" / "alpha"
        work.mkdir(parents=True)
        (work / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        reg = registry.Registry()
        reg.projects["alpha"] = registry.Project("alpha", str(work), 30)
        registry.save(reg)
        self.reg, self.p = reg, reg.projects["alpha"]
        self.inbox = handshake.folder_of(self.p) / "inbox"

    def post(self, body, header="1"):
        h = {"Content-Length": "9999"}
        if header:
            h["X-Room"] = header
        r = Poster("/api/note", body, h)
        r.headers = type("H", (), {"get": lambda s, k, d=None: h.get(k, d)})()
        r.cfg = {"project": {"name": "alpha"}}
        with mock.patch.object(dashboard, "Ledger"):
            r.do_POST()
        code, out = r.sent
        return code, json.loads(out) if code == 200 else None

    def files(self):
        return sorted(self.inbox.glob("*.md")) if self.inbox.exists() else []

    def test_valid_note_is_written_from_owner(self):
        code, out = self.post({"project": "alpha", "text": "Keep the gate steady"})
        self.assertEqual((code, len(self.files())), (200, 1))
        self.assertEqual(out["name"], self.files()[0].name)
        text = self.files()[0].read_text(encoding="utf-8")
        self.assertIn("from: owner", text)
        self.assertIn("Keep the gate steady", text)
        self.assertNotIn("kind:", text)

    def test_kind_correction_is_written(self):
        self.post({"project": "alpha", "text": "x is wrong", "kind": "correction"})
        self.assertIn("kind: correction", self.files()[0].read_text(encoding="utf-8"))

    def test_unknown_kind_writes_nothing(self):
        _, out = self.post({"project": "alpha", "text": "hi", "kind": "rant"})
        self.assertIn("error", out)
        self.assertEqual(self.files(), [])

    def test_refusals_write_nothing(self):
        for body in ({"project": "nope", "text": "hi"}, {"project": "alpha", "text": "  "},
                     {"project": "alpha", "text": "a" * 8001}):
            _, out = self.post(body)
            self.assertIn("error", out)
        code, _ = self.post({"project": "alpha", "text": "hi"}, header=None)
        self.assertEqual(code, 403)
        self.assertEqual(self.files(), [])

    def test_owner_notes_lists_it(self):
        self.post({"project": "alpha", "text": "Listed"})
        notes = handshake.owner_notes(self.reg, self.p, 0, 9e12)
        self.assertEqual([n["text"].strip() for n in notes], ["Listed"])


if __name__ == "__main__":
    unittest.main()
