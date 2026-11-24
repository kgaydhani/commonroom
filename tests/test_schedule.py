import contextlib
import io
import subprocess
import unittest
from unittest import mock

from commonroom import schedule


class ScheduleTest(unittest.TestCase):
    def test_windows_names_beat_vbs_and_interval(self):
        cmd, check = schedule.build("win32", r"C:\rooms\cr", 10)
        self.assertTrue(cmd.startswith("schtasks /create"))
        self.assertIn("/mo 10", cmd)
        self.assertIn(r"wscript.exe C:\rooms\cr\beat.vbs", cmd)
        self.assertTrue(check.startswith("schtasks /query /tn"))

    def test_linux_and_macos_give_cron_line(self):
        for plat in ("linux", "darwin"):
            cmd, check = schedule.build(plat, "/home/x/cr", 15)
            self.assertIn("*/15 * * * *", cmd)
            self.assertIn("/home/x/cr/room.py beat", cmd)
            self.assertEqual(check, "crontab -l")

    def test_minutes_out_of_range_refused(self):
        for bad in (0, -5, 1441):
            with self.assertRaises(ValueError):
                schedule.build("linux", "/x", bad)
        schedule.build("win32", r"C:\x", 1)
        schedule.build("win32", r"C:\x", 1440)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = schedule.cmd_schedule(mock.Mock(minutes=0))
        self.assertEqual(code, 2)
        self.assertIn("refused", out.getvalue())

    def test_starts_no_process(self):
        boom = mock.Mock(side_effect=AssertionError("started a process"))
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(subprocess, "call", boom), mock.patch.object(subprocess, "check_output", boom), \
                mock.patch("os.system", boom), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(schedule.cmd_schedule(mock.Mock(minutes=15)), 0)
        self.assertIn("Check it is registered", out.getvalue())
        boom.assert_not_called()
