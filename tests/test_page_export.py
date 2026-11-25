"""commonroom#647: `room page export` writes the page as static, read-only, noindex files, on a temporary room home."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commonroom import dashboard, page, pageexport
from tests.test_dispatch import cfg_with
from tests.test_standings import Room


class Export(Room):
    def cfg(self):
        cfg = cfg_with(self.root)
        cfg["project"]["name"] = "alpha-site"
        cfg["_root"] = str(self.root / "work" / "alpha")
        return cfg

    def state(self, queue=None):
        return page.page_state(self.ledger, self.cfg(), self.now, queue=queue)

    def setUp(self):
        super().setUp()
        self.week()
        self.register()
        # beta runs on a local board: no GitHub repo behind it
        (self.root / "work" / "beta" / "room.toml").write_text('[project]\nname = "beta-site"\nboard = "local"\n', encoding="utf-8")
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        self.out = Path(out.name) / "site"

    def export(self):
        data = self.state(queue=lambda repo: [])
        with mock.patch.object(dashboard, "page_data", return_value=data):
            return pageexport.export(self.cfg(), self.out, now=self.now)

    def html(self):
        return (self.out / "index.html").read_text(encoding="utf-8")

    def test_writes_the_page_and_robots_with_both_projects(self):
        paths = self.export()
        self.assertEqual(sorted(p.name for p in paths), ["index.html", "robots.txt"])
        self.assertEqual((self.out / "robots.txt").read_text(), "User-agent: *\nDisallow: /\n")
        html = self.html()
        self.assertIn('"name": "alpha"', html)
        self.assertIn('"name": "beta"', html)

    def test_no_local_server_no_api_and_no_write_controls(self):
        self.export()
        html = self.html()
        for bad in ("127.0.0.1", "localhost", "/api/", "fetch(", "X-Room", "textarea", "<select", "range", "POST", "setInterval",
                    "http://", "https://fonts"):
            self.assertNotIn(bad, html)
        self.assertNotRegex(html, r"\b(Apply|Note to this project)\b")

    def test_build_time_noindex_and_viewport(self):
        self.export()
        html = self.html()
        self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
        self.assertIn('<meta name="viewport" content="width=device-width,initial-scale=1">', html)
        self.assertIn(f"const BUILT = {self.now!r}", html)
        self.assertIn("Built ", html)
        self.assertEqual(len(re.findall(r"<script>", html)), 1)

    def test_data_cannot_close_the_script(self):
        html = pageexport.build({"x": "</script><b>"}, 1.0)
        self.assertNotIn("</script><b>", html)

    def test_dashboard_still_serves_the_live_page(self):
        for needed in ("/api/page", "/api/note", "/api/project", "setInterval", "<write>", "textarea"):
            self.assertIn(needed, dashboard.PAGE)
