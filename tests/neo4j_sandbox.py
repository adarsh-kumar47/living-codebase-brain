"""A throwaway Neo4j for tests: its own data directory and ports, so a test run can
--wipe freely without touching the real code graph on 7687.

Uses whatever Neo4j is installed (NEO4J_BIN, else `neo4j` on PATH, else Homebrew's),
pointed at a copy of its config with the data/log/run directories and ports
overridden, and auth disabled.
"""

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable

CANDIDATES = ("/opt/homebrew/opt/neo4j/bin/neo4j", "/usr/local/opt/neo4j/bin/neo4j")


def find_neo4j():
    for path in (os.environ.get("NEO4J_BIN"), shutil.which("neo4j"), *CANDIDATES):
        if path and os.path.exists(path):
            return path
    return None


def neo4j_home(neo4j_bin):
    """NEO4J_HOME the launcher would use -- Homebrew's wrapper sets it inline."""
    with open(neo4j_bin, encoding="utf-8", errors="ignore") as fh:
        for token in fh.read().split():
            if token.startswith("NEO4J_HOME="):
                return token.split("=", 1)[1].strip('"')
    return os.path.dirname(os.path.dirname(os.path.realpath(neo4j_bin)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Neo4jSandbox:
    def __init__(self, startup_timeout=120):
        self.bin = find_neo4j()
        if not self.bin:
            raise RuntimeError("no Neo4j install found; set NEO4J_BIN")
        self.timeout = startup_timeout
        self.dir = tempfile.mkdtemp(prefix="lcb-neo4j-")
        self.bolt_port, self.http_port = free_port(), free_port()
        self.uri = f"bolt://127.0.0.1:{self.bolt_port}"
        self.proc = None

    def _write_conf(self):
        conf_dir = os.path.join(self.dir, "conf")
        os.makedirs(conf_dir)
        for d in ("data", "logs", "run"):
            os.makedirs(os.path.join(self.dir, d))
        overrides = {
            "server.directories.data": f"{self.dir}/data",
            "server.directories.logs": f"{self.dir}/logs",
            "server.directories.run": f"{self.dir}/run",
            "server.bolt.listen_address": f"127.0.0.1:{self.bolt_port}",
            "server.http.listen_address": f"127.0.0.1:{self.http_port}",
            "server.https.enabled": "false",
            "dbms.security.auth_enabled": "false",
            "server.memory.heap.initial_size": "256m",
            "server.memory.heap.max_size": "512m",
            "server.memory.pagecache.size": "64m",
        }
        base = os.path.join(neo4j_home(self.bin), "conf", "neo4j.conf")
        with open(base, encoding="utf-8") as fh:
            # Neo4j rejects a setting declared twice, so drop the base's copy of each override.
            lines = [ln for ln in fh.read().splitlines()
                     if ln.split("=", 1)[0].strip() not in overrides]
        lines += [f"{k}={v}" for k, v in overrides.items()]
        with open(os.path.join(conf_dir, "neo4j.conf"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return conf_dir

    def start(self):
        env = dict(os.environ, NEO4J_CONF=self._write_conf())
        self.log = open(os.path.join(self.dir, "console.log"), "w")
        # Its own process group: the launcher script starts the server as a child
        # JVM, and terminating only the launcher orphaned that JVM, still running.
        self.proc = subprocess.Popen([self.bin, "console"], env=env, start_new_session=True,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                with open(os.path.join(self.dir, "console.log"), encoding="utf-8") as fh:
                    tail = fh.read()[-2000:]
                self.stop()
                raise RuntimeError(f"Neo4j exited early:\n{tail}")
            try:
                with GraphDatabase.driver(self.uri, auth=("neo4j", "unused")) as d:
                    d.verify_connectivity()
                return self
            except (ServiceUnavailable, OSError):
                time.sleep(1)
        self.stop()
        raise RuntimeError(f"Neo4j did not come up in {self.timeout}s")

    def _signal_group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _bolt_open(self):
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", self.bolt_port)) == 0

    def stop(self):
        """Stops the launcher and every JVM it started, and waits until the port is
        closed -- so nothing is left running, or writing into the folder removed below."""
        if self.proc:
            self._signal_group(signal.SIGTERM)
            deadline = time.time() + 30
            while time.time() < deadline and (self.proc.poll() is None or self._bolt_open()):
                time.sleep(0.5)
            if self.proc.poll() is None or self._bolt_open():
                self._signal_group(signal.SIGKILL)
                self.proc.wait(timeout=10)
        if getattr(self, "log", None):
            self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
