"""In-memory ring buffer of machine log lines, served at GET /v1/logs (long-poll).

Sources: ``setup`` (machine start-up script), ``agent`` (this process), ``engine`` (ComfyUI).
Thread-safe: lines may be added from logging handlers in other threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from aivo_protocol.worker_api import LogLine, LogsOut

MAX_LINES = 5000
LINE_CAP = 2000


class LogBuffer:
    def __init__(
        self,
        max_lines: int = MAX_LINES,
        line_cap: int = LINE_CAP,
        echo: Callable[[LogLine], None] | None = None,
    ):
        self.echo = echo  # e.g. print to stdout so `docker logs` shows the same lines
        self._lines: deque[LogLine] = deque(maxlen=max_lines)
        self._line_cap = line_cap
        self._seq = 0
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, source: str, text: str, level: str | None = None, *, echo: bool = True) -> None:
        parts = str(text).replace("\r\n", "\n").split("\n")
        added: list[LogLine] = []
        with self._lock:
            for part in parts:
                part = part.rstrip()
                if not part.strip():
                    continue
                if len(part) > self._line_cap:
                    part = part[: self._line_cap] + " …"
                self._seq += 1
                line = LogLine(seq=self._seq, ts=time.time(), source=source, level=level, text=part)
                self._lines.append(line)
                added.append(line)
            waiters = list(self._waiters)
        if echo and self.echo is not None:
            for line in added:
                with contextlib.suppress(Exception):
                    self.echo(line)
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # loop closed
                pass

    def read(self, since: int = 0, limit: int = 1000, tail: int | None = None) -> LogsOut:
        with self._lock:
            lines = list(self._lines)
            last = self._seq
        if tail is not None:
            sel = lines[-tail:] if tail > 0 else []
            return LogsOut(lines=sel, next_seq=last, dropped=0)
        if since > last:  # cursor from an earlier run of this process: start over
            out = self.read(0, limit)
            out.reset = True
            return out
        first = lines[0].seq if lines else last + 1
        dropped = max(0, first - since - 1) if since < first else 0
        sel = [ln for ln in lines if ln.seq > since][:limit]
        return LogsOut(lines=sel, next_seq=sel[-1].seq if sel else max(since, last), dropped=dropped)

    async def wait_read(self, since: int, timeout: float, limit: int = 1000) -> LogsOut:
        out = self.read(since, limit)
        if out.lines or out.reset or timeout <= 0:
            return out
        ev = asyncio.Event()
        key = (asyncio.get_running_loop(), ev)
        with self._lock:
            self._waiters.add(key)
        try:
            if self._seq > since:  # raced with an emit
                return self.read(since, limit)
            try:
                await asyncio.wait_for(ev.wait(), timeout)
            except TimeoutError:
                pass
            return self.read(since, limit)
        finally:
            with self._lock:
                self._waiters.discard(key)

    def load_file(self, path: Path, source: str = "setup") -> int:
        """Import an existing log file (e.g. the start-up log mounted into the container)."""
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return 0
        n = 0
        for line in text.splitlines():
            if line.strip():
                self.emit(source, line, echo=False)
                n += 1
        return n


class BufferHandler(logging.Handler):
    """Copies log records into a LogBuffer (installed only in the real worker process)."""

    def __init__(self, buf: LogBuffer, source: str = "agent"):
        super().__init__()
        self.buf = buf
        self.source = source
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:  # already printed by the regular logging handler, so no echo
            self.buf.emit(self.source, self.format(record), record.levelname.lower(), echo=False)
        except Exception:  # never let logging break the agent
            self.handleError(record)
