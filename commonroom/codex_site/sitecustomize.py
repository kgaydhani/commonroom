"""Loaded by every Python a Codex worker starts: `worker_env` puts this folder first on PYTHONPATH (commonroom#182).

Since Python 3.13, `os.mkdir(path, 0o700)` on Windows gives the folder a protected ACL naming only the user. The Codex
sandbox runs commands with a restricted token, which fails that check, so a file written into a `tempfile.mkdtemp()`
folder raises PermissionError. Here `tempfile` makes its folders with the default mode instead, so they inherit the
worktree's ACL. Nothing else changes, and on other platforms this does nothing.

Python imports only the first `sitecustomize` it finds, so this one then runs the next one on the path, if any."""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile


class _TempfileOs:
    """`os` as `tempfile` sees it, except that `mkdir` drops the 0o700 mode."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def mkdir(path, mode=0o777, *args, **kwargs):
        return os.mkdir(path, 0o777 if mode == 0o700 else mode, *args, **kwargs)


def install(platform: str = sys.platform):
    if platform == "win32" and not isinstance(tempfile._os, _TempfileOs):
        tempfile._os = _TempfileOs()


def chain():
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = module
    spec.loader.exec_module(module)


install()
chain()
