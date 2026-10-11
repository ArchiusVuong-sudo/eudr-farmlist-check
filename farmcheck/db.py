"""PostgreSQL 18 + PostGIS on PGlite, run as a Node child process, with one shared psycopg connection.

pglite-server multiplexes a single Postgres session, so the app keeps exactly one connection,
disables psycopg's auto-prepare (prepared statement names collide across reconnects), and
serialises every transaction behind a lock. Slow work (model calls) must run outside the lock.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import ROOT

log = logging.getLogger(__name__)

SERVER_BIN = ROOT / "node_modules" / ".bin" / "pglite-server"


class DatabaseError(RuntimeError):
    """The embedded database could not be started or reached."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _process_command(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return out.stdout.strip()


class Database:
    """Owns the pglite-server child and the single connection to it.

    data_dir=None runs an in-memory database (tests); otherwise the directory is file-backed.
    """

    def __init__(self, data_dir: Path | None) -> None:
        self.data_dir = data_dir.resolve() if data_dir else None
        self.port: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._conn: psycopg.Connection | None = None
        self._lock = threading.RLock()
        self._log_file: Path | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def pidfile(self) -> Path | None:
        return self.data_dir.parent / "pglite.pid" if self.data_dir else None

    def _clear_stale_server(self) -> None:
        """Stop a server left by an earlier run of this app on the same data dir, and nothing else."""
        pidfile = self.pidfile
        if not pidfile or not pidfile.exists():
            return
        try:
            pid = int(pidfile.read_text().strip())
        except ValueError:
            pidfile.unlink(missing_ok=True)
            return
        cmd = _process_command(pid)
        if "pglite-server" in cmd and self.data_dir and str(self.data_dir) in cmd:
            log.warning("stopping stale database server pid %s", pid)
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _process_command(pid):
                    break
                time.sleep(0.1)
        pidfile.unlink(missing_ok=True)

    def start(self, timeout_s: float = 60.0) -> None:
        if self._proc is not None:
            return
        node = shutil.which("node")
        if not node or not SERVER_BIN.exists():
            raise DatabaseError("Node 20+ and `npm ci` are needed: node_modules/.bin/pglite-server not found")
        if self.data_dir:
            self.data_dir.parent.mkdir(parents=True, exist_ok=True)
            self._clear_stale_server()
            db_arg = str(self.data_dir)
            self._log_file = self.data_dir.parent / "pglite.log"
        else:
            db_arg = "memory://"
            self._log_file = None
        self.port = _free_port()
        cmd = [
            node,
            str(SERVER_BIN),
            f"--db={db_arg}",
            f"--port={self.port}",
            "--host=127.0.0.1",
            "--extensions=postgis",
            "--max-connections=1",
        ]
        out = open(self._log_file, "ab") if self._log_file else subprocess.DEVNULL  # noqa: SIM115
        self._proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        if self.pidfile:
            self.pidfile.write_text(str(self._proc.pid))
        atexit.register(self.stop)
        deadline = time.monotonic() + timeout_s
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise DatabaseError(f"pglite-server exited with code {self._proc.returncode}")
            try:
                self._connect()
                return
            except psycopg.OperationalError as exc:
                last_err = exc
                time.sleep(0.25)
        self.stop()
        raise DatabaseError(f"database did not accept connections in {timeout_s:.0f} s: {last_err}")

    def _connect(self) -> None:
        conn = psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            user="postgres",
            password="postgres",
            dbname="postgres",
            sslmode="disable",
            autocommit=True,
            prepare_threshold=None,
            row_factory=dict_row,
            connect_timeout=5,
        )
        # The server keeps one session for every client: never rely on state left in it.
        conn.execute("ROLLBACK")
        conn.execute("DISCARD ALL")
        self._conn = conn

    def stop(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001 - closing a dead connection
                    pass
                self._conn = None
            proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if self.pidfile:
                self.pidfile.unlink(missing_ok=True)

    # ------------------------------------------------------------------ access
    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Yield the shared connection inside one transaction, holding the lock."""
        with self._lock:
            conn = self._ensure_conn()
            with conn.transaction():
                yield conn

    @contextmanager
    def session(self) -> Iterator[psycopg.Connection]:
        """Yield the shared connection in autocommit mode, holding the lock (for reads and DDL)."""
        with self._lock:
            yield self._ensure_conn()

    def _ensure_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            if self._proc is None:
                raise DatabaseError("database is not started")
            self._connect()
        assert self._conn is not None
        return self._conn
