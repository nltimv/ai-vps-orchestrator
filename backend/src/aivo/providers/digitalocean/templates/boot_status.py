#!/usr/bin/env python3
"""aivo start-up status server. Runs on a new machine until the worker agent takes over.

Standard library only (the machine has nothing else yet). It answers the backend over HTTPS
with the machine's own certificate, and checks the same bearer token as the agent:

  GET /v1/health                  -> 503 {"bootstrap": {stage, detail, progress_bytes, failed, error, ...}}
  GET /v1/logs?since=&wait=&tail= -> lines of the start-up log (same numbering the agent continues)

Subcommands (used by bootstrap.sh):
  serve                 run the server (writes a pid file)
  stop                  stop a running server and wait until the port is free
  stage <name> [detail] record the current start-up step
  fail <message>        record that start-up failed (the server keeps serving the reason)

Never serves or logs bootstrap.json (it holds the machine's private key).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BOOT = os.environ.get("AIVO_BOOTSTRAP", "/etc/aivo/bootstrap.json")
STATUS = os.environ.get("AIVO_BOOT_STATUS", "/run/aivo/boot-status.json")
LOG = os.environ.get("AIVO_SETUP_LOG", "/var/log/aivo-bootstrap.log")
PIDFILE = os.environ.get("AIVO_BOOT_STATUS_PID", "/run/aivo/boot-status.pid")
LINE_CAP = 2000  # same cap as the agent, so line numbering continues seamlessly
_SKIP_NICS = ("lo", "docker", "veth", "br-", "virbr")


# ---------------------------------------------------------------- status file


def read_status(path: str = STATUS) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {"stage": "starting"}


def write_status(path: str = STATUS, **fields: object) -> dict:
    data = read_status(path)
    data.update(fields)
    data["updated_at"] = time.time()
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".boot-status.")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return data


def rx_bytes() -> int:
    """Bytes received on the machine's real network interfaces (shows download progress)."""
    total = 0
    with contextlib.suppress(OSError):
        for nic in os.listdir("/sys/class/net"):
            if nic.startswith(_SKIP_NICS):
                continue
            with contextlib.suppress(OSError, ValueError), open(f"/sys/class/net/{nic}/statistics/rx_bytes") as fh:
                total += int(fh.read().strip())
    return total


# ---------------------------------------------------------------- log tail


def _level(text: str) -> str | None:
    low = text.lower()
    if "failed" in low or "error" in low:
        return "error"
    if "warning" in low or "retrying" in low:
        return "warning"
    return None


class LogTail:
    """Follows the start-up log; each non-blank line gets the next number (as in the agent's buffer)."""

    def __init__(self, path: str):
        self.path = path
        self.lines: list[dict] = []
        self._pos = 0
        self._partial = ""
        self._cond = threading.Condition()
        self._poll_lock = threading.Lock()

    def poll(self) -> None:
        with self._poll_lock:
            self._poll()

    def _poll(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        text = self._partial + chunk.decode("utf-8", errors="replace")
        parts = text.split("\n")
        self._partial = parts.pop()
        now = time.time()
        new = []
        for raw in parts:
            for line in raw.splitlines():
                line = line.rstrip()
                if not line.strip():
                    continue
                if len(line) > LINE_CAP:
                    line = line[:LINE_CAP] + " …"
                new.append(line)
        if new:
            with self._cond:
                for line in new:
                    seq = len(self.lines) + 1
                    self.lines.append({"seq": seq, "ts": now, "source": "setup", "level": _level(line), "text": line})
                self._cond.notify_all()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.poll()
            stop.wait(0.5)

    def read(self, since: int, limit: int, tail: int | None, wait: float) -> dict:
        with self._cond:
            last = len(self.lines)
            if tail is not None:
                sel = self.lines[-tail:] if tail > 0 else []
                return {"lines": sel, "next_seq": last, "dropped": 0, "reset": False}
            reset = since > last
            if reset:
                since = 0
            if wait > 0 and not reset and since >= last:
                self._cond.wait(wait)
                last = len(self.lines)
            sel = self.lines[since : since + limit]
        return {"lines": sel, "next_seq": sel[-1]["seq"] if sel else since, "dropped": 0, "reset": reset}


# ---------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server: BootStatusServer
    protocol_version = "HTTP/1.1"
    server_version = "aivo-boot"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # quiet
        pass

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        scheme, _, presented = self.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return False
        digest = hashlib.sha256(presented.encode()).hexdigest()
        return hmac.compare_digest(digest, self.server.token_sha256)

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(401, {"detail": "bad token"})
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)

        def num(name: str, default: float | None, lo: float, hi: float) -> float | None:
            try:
                v = float(q[name][0]) if name in q else default
            except ValueError:
                v = default
            return None if v is None else max(lo, min(hi, v))

        if url.path == "/v1/health":
            st = self.server.status()
            self._json(503, {"bootstrap": st})
        elif url.path == "/v1/logs":
            since = int(num("since", 0, 0, 1e12) or 0)
            limit = int(num("limit", 1000, 1, 5000) or 1000)
            tail = num("tail", None, 0, 5000)
            wait = num("wait", 0, 0, 30) or 0
            self.server.tail.poll()
            out = self.server.tail.read(since, limit, None if tail is None else int(tail), wait)
            self._json(200, out)
        else:
            self._json(404, {"detail": "not found"})


class BootStatusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        addr: tuple[str, int],
        *,
        cert_pem: str,
        key_pem: str,
        token_sha256: str,
        status_path: str = STATUS,
        log_path: str = LOG,
    ):
        super().__init__(addr, Handler)
        self.token_sha256 = token_sha256
        self.status_path = status_path
        self.tail = LogTail(log_path)
        self.rx_start = rx_bytes()
        self.ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # load_cert_chain needs files: write them to a private temp dir and remove them right away
        d = tempfile.mkdtemp(prefix="aivo-boot-tls-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        try:
            cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
            for path, pem in ((cert, cert_pem), (key, key_pem)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(pem)
            self.ctx.load_cert_chain(cert, key)
        finally:
            for name in os.listdir(d):
                os.unlink(os.path.join(d, name))
            os.rmdir(d)
        self._stop = threading.Event()
        self._tail_thread = threading.Thread(target=self.tail.run, args=(self._stop,), daemon=True)
        self._tail_thread.start()

    def status(self) -> dict:
        st = read_status(self.status_path)
        st.setdefault("stage", "starting")
        st["progress_bytes"] = max(0, rx_bytes() - self.rx_start)
        st["failed"] = bool(st.get("failed"))
        return st

    def finish_request(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        # TLS handshake in the per-connection thread, so a slow client can't block others
        request.settimeout(45)
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            with contextlib.suppress(OSError):
                tls.close()

    def shutdown_request(self, request) -> None:  # type: ignore[no-untyped-def]
        with contextlib.suppress(OSError):
            request.close()

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        pass  # never print tracebacks into the start-up log

    def server_close(self) -> None:
        self._stop.set()
        super().server_close()


def load_boot(path: str = BOOT) -> dict:
    with open(path) as fh:
        return json.load(fh)


def make_server(boot: dict, *, host: str | None = None, port: int | None = None, **paths: str) -> BootStatusServer:
    addr = (
        host if host is not None else boot.get("listen_host") or "0.0.0.0",
        port if port is not None else int(boot.get("listen_port") or 443),
    )
    return BootStatusServer(
        addr,
        cert_pem=boot["tls_cert_pem"],
        key_pem=boot["tls_key_pem"],
        token_sha256=boot["token_sha256"],
        **paths,
    )


# ---------------------------------------------------------------- commands


def _serve() -> int:
    boot = load_boot()
    srv = None
    for attempt in range(30):  # the port may still be held briefly by a previous owner
        try:
            srv = make_server(boot)
            break
        except OSError as e:
            if attempt == 29:
                print(f"[aivo] status server could not start: {e}", file=sys.stderr)
                return 1
            time.sleep(1)
    assert srv is not None
    os.makedirs(os.path.dirname(PIDFILE) or ".", exist_ok=True)
    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    srv.serve_forever(poll_interval=0.5)
    return 0


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _stop() -> int:
    try:
        with open(PIDFILE) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return 0
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.unlink(PIDFILE)
    with contextlib.suppress(OSError, ValueError, KeyError):
        port = int(load_boot().get("listen_port") or 443)
        for _ in range(50):
            if _port_free(port):
                break
            time.sleep(0.2)
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "serve":
        return _serve()
    if cmd == "stop":
        return _stop()
    if cmd == "stage" and len(argv) >= 3:
        write_status(stage=argv[2], detail=(argv[3] if len(argv) > 3 and argv[3] else None))
        return 0
    if cmd == "fail" and len(argv) >= 3:
        write_status(failed=True, error=argv[2][:1000])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
