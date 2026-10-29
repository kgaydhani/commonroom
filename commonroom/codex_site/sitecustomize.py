"""Loaded by every Python a Codex worker starts: `worker_env` puts this folder first on PYTHONPATH (commonroom#182).

Since Python 3.13, `os.mkdir(path, 0o700)` on Windows gives the folder a protected ACL naming only the user. The Codex
sandbox runs commands with a restricted token, which fails that check, so a file written into a `tempfile.mkdtemp()`
folder raises PermissionError. Here `tempfile` makes its folders with the default mode instead, so they inherit the
worktree's ACL. Nothing else changes, and on other platforms this does nothing.

The sandbox also can't open the named pipe behind a `multiprocessing` queue (WinError 5), and every `Pool.__init__` makes
one for its change notifier. A `ThreadPool` lives in one process and only `put`s to that notifier, so here it gets an
in-process queue instead, which `Pool` waits on without the OS (commonroom#192). Process pools are left alone.

Python imports only the first `sitecustomize` it finds, so this one then runs the next one on the path, if any."""
import importlib.machinery
import importlib.util
import multiprocessing
import multiprocessing.pool
import os
import queue
import sys
import tempfile
import threading


class _TempfileOs:
    """`os` as `tempfile` sees it, except that `mkdir` drops the 0o700 mode."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def mkdir(path, mode=0o777, *args, **kwargs):
        return os.mkdir(path, 0o777 if mode == 0o700 else mode, *args, **kwargs)


class _NotifierReader:
    """Stands in for the pipe end `Pool` waits on: `wait` on it returns once something has been put."""

    def __init__(self):
        self.ready = threading.Event()


class _NotifierQueue(queue.SimpleQueue):
    """A `ThreadPool`'s change notifier without an OS pipe."""

    def __init__(self):
        super().__init__()
        self._reader = _NotifierReader()

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self._reader.ready.set()


_real_wait = multiprocessing.pool.wait


def _wait(object_list, timeout=None):
    """`multiprocessing.pool.wait`, except that a list of only notifier readers waits in-process."""
    if not object_list or not all(isinstance(o, _NotifierReader) for o in object_list):
        return _real_wait(object_list, timeout)
    ready = []
    for o in object_list:
        if o.ready.wait(timeout):
            o.ready.clear()
            ready.append(o)
            timeout = 0
    return ready


class _ThreadPoolContext:
    """The multiprocessing context a `ThreadPool` is built with: `SimpleQueue()` is in-process, the rest is unchanged."""

    def __init__(self, ctx):
        self._ctx = ctx

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    @staticmethod
    def SimpleQueue():
        return _NotifierQueue()


def _thread_pool_init(self, processes=None, initializer=None, initargs=(), maxtasksperchild=None, context=None):
    multiprocessing.pool.Pool.__init__(self, processes, initializer, initargs, maxtasksperchild,
                                       _ThreadPoolContext(context or multiprocessing.get_context()))


def install(platform: str = sys.platform):
    if platform != "win32":
        return
    if not isinstance(tempfile._os, _TempfileOs):
        tempfile._os = _TempfileOs()
    if multiprocessing.pool.ThreadPool.__init__ is not _thread_pool_init:
        multiprocessing.pool.ThreadPool.__init__ = _thread_pool_init
        multiprocessing.pool.wait = _wait


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
