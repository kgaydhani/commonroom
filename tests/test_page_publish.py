"""commonroom#649: the beat exports the page and publishes it with the room home's configured command."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from commonroom import beat, pagepublish, registry


class Publish(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.proj = self.tmp / "alpha"
        self.proj.mkdir()
        (self.proj / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
        self.reg = registry.Registry(projects={"alpha": registry.Project(name="alpha", path=str(self.proj), share=0)}) \
            if hasattr(registry, "Project") else None
        self.lines, self.exports, self.published, self.built, self.exit = [], [], [], 1, 0

    def conf(self, publish="tool {folder}", extra=""):
        (self.home / "page.toml").write_text(f'[page]\npublish = "{publish}"\n{extra}', encoding="utf-8")

    def starter(self, argv, cwd, log, wait):
        if argv[2:4] == ["page", "export"]:
            self.exports.append((argv[4], cwd))
            folder = Path(argv[4])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.html").write_text(f"<p>same</p>\nconst BUILT = {self.built};\n", encoding="utf-8")
        return SimpleNamespace(returncode=self.exit, wait=lambda: None)

    def runner(self, argv, **kw):
        self.published.append((argv, kw))
        return SimpleNamespace(returncode=self.code, stdout="", stderr="boom\nlast line")
    code = 0

    def beat(self, runner=None):
        pagepublish.publish(self.home, str(self.proj), self.starter, self.lines.append, python="py",
                            runner=runner or self.runner)

    def test_exports_and_publishes_once_with_the_folder(self):
        self.conf()
        self.beat()
        folder = str(self.home / "page" / "public")
        self.assertEqual(self.exports, [(folder, str(self.proj))])
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0][0], ["tool", folder])
        self.assertEqual(self.lines, ["beat: page published"])

    def test_nothing_configured_runs_nothing_and_logs_nothing(self):
        self.beat()
        self.conf(publish="")
        self.beat()
        self.assertEqual((self.exports, self.published, self.lines), ([], [], []))

    def test_failing_publish_is_one_line_and_is_tried_again(self):
        self.conf()
        self.code = 2
        self.beat()
        self.assertEqual(self.lines, ["beat: page publish failed (exit 2): last line"])
        self.code = 0
        self.beat()
        self.assertEqual(len(self.published), 2)

    def test_timeout_and_crash_are_logged_not_raised(self):
        self.conf(extra="timeout = 7\n")
        def slow(argv, **kw):
            self.assertEqual(kw["timeout"], 7)
            raise subprocess.TimeoutExpired(argv, 7)
        self.beat(slow)
        def missing(argv, **kw):
            raise FileNotFoundError("tool")
        self.beat(missing)
        self.assertEqual(self.lines[0], "beat: page publish timed out after 7s")
        self.assertTrue(self.lines[1].startswith("beat: page publish error: FileNotFoundError"))

    def test_unchanged_page_is_not_published_again_despite_a_new_build_time(self):
        self.conf()
        self.beat()
        self.built = 2
        self.beat()
        self.assertEqual(len(self.published), 1)
        self.assertEqual(len(self.exports), 2)

    def test_changed_page_is_published_again(self):
        self.conf()
        self.beat()
        orig = self.starter
        def changed(argv, cwd, log, wait):
            proc = orig(argv, cwd, log, wait)
            (Path(argv[4]) / "index.html").write_text("<p>new</p>\n", encoding="utf-8")
            return proc
        self.starter = changed
        self.beat()
        self.assertEqual(len(self.published), 2)

    def test_failed_export_is_not_published(self):
        self.conf()
        self.exit = 1
        self.beat()
        self.assertEqual((self.published, self.lines), ([], ["beat: page export failed (exit 1); not published"]))

    def test_a_real_command_runs_with_the_folder(self):
        out = self.tmp / "ran.txt"
        code = f"import sys,pathlib; pathlib.Path(sys.argv[2]).write_text(sys.argv[1])"
        py = Path(sys.executable).as_posix()
        self.conf(publish=f"{py} -c \\\"{code}\\\" {{folder}} {out.as_posix()}")
        pagepublish.publish(self.home, str(self.proj), self.starter, self.lines.append, python="py")
        self.assertEqual(self.lines, ["beat: page published"])
        self.assertEqual(out.read_text(), str(self.home / "page" / "public"))

    def test_a_bare_program_name_is_found_on_path_like_npx_cmd(self):
        out = self.tmp / "ran.txt"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        if sys.platform == "win32":
            (bin_dir / "shimtool.cmd").write_text(f'@echo off\r\necho %1>"{out}"\r\n', encoding="utf-8", newline="")
        else:
            shim = bin_dir / "shimtool"
            shim.write_text(f'#!/bin/sh\necho "$1" > "{out}"\n', encoding="utf-8")
            shim.chmod(0o755)
        self.conf(publish="shimtool {folder}")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            pagepublish.publish(self.home, str(self.proj), self.starter, self.lines.append, python="py")
        self.assertEqual(self.lines, ["beat: page published"])
        self.assertEqual(out.read_text().strip(), str(self.home / "page" / "public"))


class InBeat(unittest.TestCase):
    def test_beat_without_page_toml_starts_only_what_it_did_before(self):
        with tempfile.TemporaryDirectory() as t:
            home, proj = Path(t) / "home", Path(t) / "alpha"
            home.mkdir(); proj.mkdir()
            (proj / "room.toml").write_text('[project]\nname = "alpha"\n', encoding="utf-8")
            reg = registry.Registry(projects={"alpha": registry.Project(name="alpha", path=str(proj), share=0)})
            calls = []
            def starter(argv, cwd, log, wait):
                calls.append(argv[2:])
                return SimpleNamespace(wait=lambda: None)
            beat.beat(home, reg, starter=starter, log=lambda s: None, python="py")
            self.assertEqual(calls, [["probe"], ["services"], ["tick"], ["housekeep"]])
            (home / "page.toml").write_text('[page]\npublish = "x"\n', encoding="utf-8")
            calls.clear()
            beat.beat(home, reg, starter=starter, log=lambda s: None, python="py")
            self.assertIn(["page", "export", str(home / "page" / "public")], calls)


if __name__ == "__main__":
    unittest.main()
