"""commonroom#452: `POST /api/project` changes a share or mode through the registry, as `room projects set` does."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dashboard, registry

PAGE = (Path(dashboard.__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")


class Poster(dashboard.Handler):
    def __init__(self, path, body, headers):
        self.path, self.headers = path, headers
        self.rfile, self.wfile = io.BytesIO(json.dumps(body).encode()), io.BytesIO()
        self.server = type("S", (), {"server_port": 8770})()
        self.sent = None

    def send(self, code, body, kind):
        self.sent = (code, body)


class ProjectEndpoint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"COMMONROOM_HOME": str(self.tmp / "home")})
        env.start()
        self.addCleanup(env.stop)
        for n in "ab":
            (self.tmp / n).mkdir()
            (self.tmp / n / "room.toml").write_text(f'[project]\nname = "{n}"\n', encoding="utf-8")
        reg = registry.Registry()
        reg.projects["alpha"] = registry.Project("alpha", str(self.tmp / "a"), 30)
        reg.projects["beta"] = registry.Project("beta", str(self.tmp / "b"), 50)
        registry.save(reg)
        self.file = registry.default_path()

    def post(self, body, origin=None):
        h = {"Content-Length": "9999", "X-Room": "1"}
        if origin:
            h["Origin"] = origin
        r = Poster("/api/project", body, h)
        r.headers = type("H", (), {"get": lambda s, k, d=None: h.get(k, d)})()
        r.cfg = {"project": {"name": "alpha"}}
        with mock.patch.object(dashboard, "Ledger"):
            r.do_POST()
        code, out = r.sent
        return code, json.loads(out) if code == 200 else None

    def test_valid_post_matches_room_projects_set(self):
        before = self.file.read_text(encoding="utf-8")
        code, out = self.post({"project": "alpha", "share": 40, "work": "fixed", "when": "spare"}, "http://127.0.0.1:8770")
        self.assertEqual((code, out["ok"]), (200, True))
        via_page = self.file.read_text(encoding="utf-8")
        self.file.write_text(before, encoding="utf-8")
        registry.set_project("alpha", 40, "fixed", "spare")
        self.assertEqual(via_page, self.file.read_text(encoding="utf-8"))
        self.assertNotEqual(via_page, before)

    def test_refused_share_returns_error_and_leaves_file(self):
        before = self.file.read_text(encoding="utf-8")
        for body in ({"project": "alpha", "share": 90}, {"project": "alpha", "share": "x"}, {"project": "nope", "share": 1},
                     {"project": "alpha", "work": "wild"}):
            _, out = self.post(body)
            self.assertTrue(out["error"], body)
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)

    def test_foreign_origin_refused(self):
        before = self.file.read_text(encoding="utf-8")
        code, _ = self.post({"project": "alpha", "share": 40}, "https://evil.example")
        self.assertEqual(code, 403)
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)

    def test_page_wires_slider_preview_and_apply(self):
        for part in ('type = "range"', "After Apply", '"Apply"', "/api/project"):
            self.assertIn(part, PAGE)


if __name__ == "__main__":
    unittest.main()
