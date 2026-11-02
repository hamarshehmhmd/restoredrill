"""Throwaway Postgres servers to restore into: local binaries (initdb + pg_ctl) or Docker."""
from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Optional


class EngineError(RuntimeError):
    pass


def _bin_version(path: str) -> Optional[str]:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+(?:\.\d+)*)", out)
    return m.group(1) if m else None


def candidate_bin_dirs(extra: Optional[str] = None) -> list:
    dirs = []
    for src in (extra, os.environ.get("RESTOREDRILL_PG_BIN")):
        if src:
            dirs += [d for d in src.split(os.pathsep) if d]
    pats = ["/usr/lib/postgresql/*/bin", "/usr/pgsql-*/bin", "/opt/homebrew/opt/postgresql@*/bin",
            "/usr/local/opt/postgresql@*/bin", "/Applications/Postgres.app/Contents/Versions/*/bin",
            "/opt/homebrew/bin", "/usr/local/pgsql/bin"]
    for p in pats:
        dirs += sorted(glob.glob(p), reverse=True)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            dirs.append(d)
    seen, out = set(), []
    for d in dirs:
        d = os.path.abspath(d)
        if d not in seen and os.path.isfile(os.path.join(d, "pg_ctl")):
            seen.add(d)
            out.append(d)
    return out


def find_local(major: Optional[int], extra: Optional[str] = None, allow_newer: bool = False) -> Optional[tuple]:
    """Return (bindir, version) of a full local Postgres install for `major` (or the best available)."""
    found = []
    for d in candidate_bin_dirs(extra):
        need = ("initdb", "pg_ctl", "postgres", "psql", "pg_restore")
        if not all(os.path.isfile(os.path.join(d, n)) for n in need):
            continue
        v = _bin_version(os.path.join(d, "postgres"))
        if v:
            found.append((d, v))
    if not found:
        return None
    if major is None:
        return max(found, key=lambda x: [int(p) for p in x[1].split(".")])
    exact = [f for f in found if int(f[1].split(".")[0]) == major]
    if exact:
        return exact[0]
    if allow_newer:
        newer = sorted((f for f in found if int(f[1].split(".")[0]) > major), key=lambda f: int(f[1].split(".")[0]))
        if newer:
            return newer[0]
    return None


def available_majors(extra: Optional[str] = None) -> list:
    out = set()
    for d in candidate_bin_dirs(extra):
        v = _bin_version(os.path.join(d, "postgres")) if os.path.isfile(os.path.join(d, "postgres")) else None
        if v:
            out.add(int(v.split(".")[0]))
    return sorted(out)


def docker_available() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pipe_gzip(cmd, path, env, timeout):
    """Run cmd with the decompressed contents of a .gz file on stdin (streamed, constant memory)."""
    import gzip
    import threading
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out = {}

    def drain(name, stream):
        out[name] = stream.read()
    threads = [threading.Thread(target=drain, args=(n, s)) for n, s in (("o", proc.stdout), ("e", proc.stderr))]
    for t in threads:
        t.start()
    err_extra = b""
    try:
        with gzip.open(path, "rb") as g:
            shutil.copyfileobj(g, proc.stdin, 1024 * 1024)
    except (OSError, EOFError) as e:  # truncated / corrupt gzip, or psql went away
        err_extra = f"\nrestoredrill: error: could not decompress backup: {e}\n".encode()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    for t in threads:
        t.join()
    return subprocess.CompletedProcess(cmd, proc.returncode, out.get("o", b""), out.get("e", b"") + err_extra)


class Server:
    """Common interface: run psql / pg_restore against the scratch server."""

    kind = "?"
    version: str = ""

    def psql(self, db: str, args: list, stdin=None, timeout=None) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def pg_restore(self, db: str, path: str, args: list, timeout=None) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def tool_version(self) -> str:
        return self.version

    def query(self, db: str, sql: str, timeout: int = 300) -> tuple:
        """Return (ok, first_value_or_error)."""
        p = self.psql(db, ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql], timeout=timeout)
        if p.returncode != 0:
            err = (p.stderr or "").strip().splitlines()
            return False, (err[0] if err else f"psql exited {p.returncode}")
        lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip() != ""]
        return True, (lines[0].split("|")[0] if lines else "")

    def stop(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


class LocalServer(Server):
    kind = "local"

    def __init__(self, bindir: str, version: str, keep: bool = False):
        self.bindir, self.version, self.keep = bindir, version, keep
        self.tmp = tempfile.mkdtemp(prefix="restoredrill-")
        self.data = os.path.join(self.tmp, "data")
        self.sock = self.tmp
        self.port = _free_port()
        self.env = dict(os.environ, PGHOST=self.sock, PGPORT=str(self.port), PGUSER="postgres")
        for k in ("PGPASSWORD", "PGDATABASE", "PGSERVICE", "PGSSLMODE", "PGOPTIONS"):
            self.env.pop(k, None)
        self._started = False

    def _b(self, name):
        return os.path.join(self.bindir, name)

    def start(self):
        p = subprocess.run([self._b("initdb"), "-D", self.data, "-U", "postgres", "--auth=trust", "-E", "UTF8",
                            "--locale=C", "--no-sync"], capture_output=True, text=True, env=self.env)
        if p.returncode:
            raise EngineError("initdb failed: " + p.stderr.strip()[-500:])
        opts = f"-p {self.port} -k {self.sock} -c listen_addresses='' -c fsync=off -c full_page_writes=off"
        p = subprocess.run([self._b("pg_ctl"), "-D", self.data, "-o", opts, "-w", "-t", "60",
                            "-l", os.path.join(self.tmp, "server.log"), "start"],
                           capture_output=True, text=True, env=self.env)
        if p.returncode:
            log = ""
            try:
                log = open(os.path.join(self.tmp, "server.log")).read()[-800:]
            except OSError:
                pass
            raise EngineError("could not start scratch Postgres: " + (p.stderr.strip() or log))
        self._started = True
        return self

    def psql(self, db, args, stdin=None, timeout=None):
        return subprocess.run([self._b("psql"), "-d", db] + args, input=stdin, capture_output=True, text=True,
                              env=self.env, timeout=timeout)

    def psql_file(self, db, path, compressed, timeout=None):
        cmd = [self._b("psql"), "-X", "-q", "-d", db, "-v", "ON_ERROR_STOP=0"]
        if compressed:
            return _pipe_gzip(cmd, path, self.env, timeout)
        return subprocess.run(cmd + ["-f", path], capture_output=True, env=self.env, timeout=timeout)

    def pg_restore(self, db, path, args, timeout=None):
        return subprocess.run([self._b("pg_restore"), "-d", db] + args + [path], capture_output=True, text=True,
                              env=self.env, timeout=timeout)

    def tool_version(self):
        return _bin_version(self._b("psql")) or self.version

    def list_archive(self, path):
        return subprocess.run([self._b("pg_restore"), "-l", path], capture_output=True, text=True, env=self.env)

    def stop(self):
        if self._started:
            subprocess.run([self._b("pg_ctl"), "-D", self.data, "-m", "immediate", "-w", "stop"],
                           capture_output=True, env=self.env)
            self._started = False
        if not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)


class DockerServer(Server):
    kind = "docker"

    def __init__(self, image: str):
        self.image = image
        self.cid = None
        self.version = ""

    def _docker(self, args, **kw):
        return subprocess.run(["docker"] + args, capture_output=True, **kw)

    def start(self):
        p = self._docker(["run", "-d", "--rm", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", self.image], text=True)
        if p.returncode:
            raise EngineError(f"docker run {self.image} failed: {p.stderr.strip()[-500:]}")
        self.cid = p.stdout.strip()
        deadline = time.time() + 120
        while time.time() < deadline:
            # the entrypoint restarts the server once after init; wait for the final one
            r = self._docker(["exec", self.cid, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"], text=True)
            if r.returncode == 0:
                logs = self._docker(["logs", self.cid], text=True)
                if "PostgreSQL init process complete" in (logs.stdout + logs.stderr) or \
                        "Skipping initialization" in (logs.stdout + logs.stderr):
                    break
            time.sleep(1)
        else:
            raise EngineError("scratch container did not become ready in 120 s")
        ok, v = self.query("postgres", "show server_version")
        self.version = v.split()[0] if ok else ""
        return self

    def psql(self, db, args, stdin=None, timeout=None):
        return self._docker(["exec", "-i", self.cid, "psql", "-U", "postgres", "-h", "127.0.0.1", "-d", db] + args,
                            input=stdin, text=True, timeout=timeout)

    def _copy_in(self, path):
        dest = "/tmp/restoredrill-backup"
        p = self._docker(["cp", path, f"{self.cid}:{dest}"], text=True)
        if p.returncode:
            raise EngineError("docker cp failed: " + p.stderr.strip())
        return dest

    def psql_file(self, db, path, compressed, timeout=None):
        dest = self._copy_in(path)
        cat = f"gzip -dc {dest}" if compressed else f"cat {dest}"
        return self._docker(["exec", self.cid, "sh", "-c",
                             f"{cat} | psql -X -q -U postgres -h 127.0.0.1 -d '{db}' -v ON_ERROR_STOP=0"],
                            timeout=timeout)

    def pg_restore(self, db, path, args, timeout=None):
        dest = self._copy_in(path)
        return self._docker(["exec", self.cid, "pg_restore", "-U", "postgres", "-h", "127.0.0.1", "-d", db]
                            + args + [dest], text=True, timeout=timeout)

    def list_archive(self, path):
        dest = self._copy_in(path)
        return self._docker(["exec", self.cid, "pg_restore", "-l", dest], text=True)

    def tool_version(self):
        p = self._docker(["exec", self.cid, "psql", "--version"], text=True)
        m = re.search(r"(\d+(?:\.\d+)*)", p.stdout or "")
        return m.group(1) if m else self.version

    def stop(self):
        if self.cid:
            self._docker(["rm", "-f", self.cid])
            self.cid = None


def make_server(engine: str, major: Optional[int], pg_bin: Optional[str] = None, image: Optional[str] = None,
                allow_newer: bool = False, keep: bool = False) -> Server:
    """Pick and start a scratch server. engine: auto | local | docker.

    auto prefers: local binaries of the exact major > Docker postgres:<major> > newer local binaries.
    """
    if engine in ("auto", "local"):
        hit = find_local(major, pg_bin)
        if hit:
            return LocalServer(hit[0], hit[1], keep=keep).start()
    if engine in ("auto", "docker") and docker_available():
        return DockerServer(image or f"postgres:{major or 'latest'}").start()
    if engine == "docker":
        raise EngineError("docker is not available (is the daemon running?)")
    if engine in ("auto", "local") and allow_newer:
        hit = find_local(major, pg_bin, allow_newer=True)
        if hit:
            return LocalServer(hit[0], hit[1], keep=keep).start()
    have = available_majors(pg_bin)
    raise EngineError(
        f"nothing to restore into: no local Postgres {major or ''} binaries (have: "
        f"{', '.join(map(str, have)) or 'none'}){'' if engine == 'local' else ' and no Docker'}. "
        "Point --pg-bin at a bin dir with initdb, pg_ctl, postgres, psql and pg_restore, or use Docker.")
