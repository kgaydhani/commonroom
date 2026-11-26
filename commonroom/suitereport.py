"""Keep a test command's output while it passes through, and say where a suite stopped at its time limit (#669)."""
from __future__ import annotations

import codecs
import re
import sys
import threading
import time

# unittest -v: `test_x (pkg.module.Class.test_x) ... ok`. The docstring form puts the `...` on a second line.
VERBOSE = re.compile(r"^\w+ \(([\w.]+)\.\w+\.\w+\)")
CRLF, LF = chr(13) + chr(10), chr(10)
TOP = 10
TAIL = 20


class Capture:
    """Reads a process's stdout and stderr, echoes each to our own, and keeps the lines with when each began."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries: list[list] = []   # [started, text]; the last may be a line still being written
        self._open = False
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._streams: list = []

    def feed(self, text: str, now: float) -> None:
        with self._lock:
            for piece in text.splitlines(keepends=True):
                if self._open:
                    self.entries[-1][1] += piece
                else:
                    self.entries.append([now, piece])
                self._open = not piece.endswith(("\n", "\r"))

    def attach(self, process) -> None:
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            if stream is not None:
                self._streams.append(stream)
                thread = threading.Thread(target=self._pump, args=(stream, name), daemon=True)
                thread.start()
                self._threads.append(thread)

    def _pump(self, stream, name: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        read = getattr(stream, "read1", stream.read)
        while True:
            try:
                data = read(65536)
            except (OSError, ValueError):
                return
            if not data:
                return
            text = decoder.decode(data)
            if not text:
                continue
            self.feed(text, self.clock())
            text = text.replace(CRLF, LF)   # the console's own text mode adds the CR back
            out = getattr(sys, name)
            try:
                out.write(text)
            except UnicodeEncodeError:
                out.write(text.encode("ascii", "replace").decode("ascii"))
            out.flush()

    def finish(self, wait: float = 2.0) -> None:
        """Let the readers drain what is already written. A stray child that still holds a pipe keeps its reader
        blocked: wait a short, shared time, and never close the streams (close() would block on that reader)."""
        end = time.monotonic() + wait
        for thread in self._threads:
            thread.join(max(0.0, end - time.monotonic()))

    def report(self, now: float) -> str:
        with self._lock:
            entries = [(t, text) for t, text in self.entries]
        runs: list[list] = []   # consecutive lines of one module: [module, started]
        for started, text in entries:
            found = VERBOSE.match(text)
            if found and (not runs or runs[-1][0] != found.group(1)):
                runs.append([found.group(1), started])
        if not runs:
            tail = [line for _, text in entries for line in text.splitlines()][-TAIL:]
            return "last output:\n" + "\n".join(tail) if tail else "no output before the limit"
        spent: dict[str, float] = {}
        for (module, began), (_, ended) in zip(runs, runs[1:]):
            spent[module] = spent.get(module, 0.0) + (ended - began)
        reached, began = runs[-1]
        lines = [f"reached: {reached} (running {now - began:.1f}s)"]
        if spent:
            lines.append("finished modules, slowest first:")
            for module, seconds in sorted(spent.items(), key=lambda kv: -kv[1])[:TOP]:
                lines.append(f"  {seconds:7.1f}s  {module}")
        return "\n".join(lines)
