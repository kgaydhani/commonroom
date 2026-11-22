"""Test helper: make a temporary folder count as outside any Git repository (commonroom#371).

A worker keeps TEMP inside its worktree, so a bare temporary folder discovers the worktree's repository.
Git stops looking upward at the entries of GIT_CEILING_DIRECTORIES, so naming the folder's parent
keeps the folder itself outside any repository, even when its parent is inside one.
"""
import contextlib
import os
from pathlib import Path
from unittest import mock


@contextlib.contextmanager
def outside_any_repository(folder):
    parent = str(Path(folder).resolve().parent)
    with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": parent}):
        yield
