"""Read a Postgres backup without restoring it: format, versions, objects, compatibility hazards."""
from __future__ import annotations

import gzip
import io
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

CUSTOM, DIRECTORY, TAR, PLAIN = "custom", "directory", "tar", "plain"

# psql learned the \restrict meta-command in the Aug 2025 minor releases (CVE-2025-8714).
# pg_dump 17.6/16.10/15.14/14.19/13.22 and newer write it into plain dumps; older psql chokes on it.
RESTRICT_MIN = {17: (17, 6), 16: (16, 10), 15: (15, 14), 14: (14, 19), 13: (13, 22)}

# Settings that pg_dump emits but older servers don't know.
SETTING_MIN_MAJOR = {"transaction_timeout": 17}


@dataclass
class DumpInfo:
    path: str
    format: str
    size: int
    mtime: float
    compressed: bool = False
    archive_version: Optional[str] = None
    dbname: Optional[str] = None
    server_version: Optional[str] = None
    pg_dump_version: Optional[str] = None
    created: Optional[str] = None
    extensions: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    schemas: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    uses_restrict: bool = False
    settings: list = field(default_factory=list)
    is_dumpall: bool = False
    complete: Optional[bool] = None  # plain dumps end with a footer; None = can't tell
    problems: list = field(default_factory=list)  # reading problems (truncated, unreadable)

    @property
    def server_major(self) -> Optional[int]:
        return major_of(self.server_version)

    @property
    def dump_major(self) -> Optional[int]:
        return major_of(self.pg_dump_version)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["server_major"] = self.server_major
        return d


def major_of(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    m = re.match(r"\s*(\d+)(?:\.(\d+))?", v)
    if not m:
        return None
    a = int(m.group(1))
    # 9.6 style: major is "9.6" -> encode as 9
    return a


def version_tuple(v: Optional[str]) -> tuple:
    if not v:
        return ()
    m = re.match(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", v)
    if not m:
        return ()
    return tuple(int(x) for x in m.groups() if x is not None)


def detect_format(path: str) -> tuple:
    """Return (format, compressed)."""
    if os.path.isdir(path):
        if os.path.exists(os.path.join(path, "toc.dat")):
            return DIRECTORY, False
        raise ValueError(f"{path} is a directory but has no toc.dat (not a pg_dump -Fd backup)")
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head.startswith(b"PGDMP"):
        return CUSTOM, False
    if head[:2] == b"\x1f\x8b":
        with gzip.open(path, "rb") as g:
            try:
                inner = g.read(512)
            except (OSError, EOFError):
                inner = b""
        if inner.startswith(b"PGDMP"):
            raise ValueError(f"{path} is a gzipped custom-format dump; gunzip it first (pg_restore can't read it)")
        return PLAIN, True
    if len(head) >= 262 and head[257:262] == b"ustar":
        return TAR, False
    return PLAIN, False


class _Reader:
    def __init__(self, data: bytes):
        self.b = io.BytesIO(data)
        self.int_size = 4

    def byte(self) -> int:
        c = self.b.read(1)
        if not c:
            raise EOFError("unexpected end of header")
        return c[0]

    def int(self) -> int:
        sign = self.byte()
        raw = self.b.read(self.int_size)
        if len(raw) != self.int_size:
            raise EOFError("unexpected end of header")
        v = int.from_bytes(raw, "little")
        return -v if sign else v

    def str(self) -> Optional[str]:
        n = self.int()
        if n < 0:
            return None
        raw = self.b.read(n)
        if len(raw) != n:
            raise EOFError("unexpected end of header")
        return raw.decode("utf-8", "replace")


def read_custom_header(data: bytes) -> dict:
    """Parse the header of a pg_dump custom/directory archive (pg_backup_archiver.c ReadHead)."""
    if not data.startswith(b"PGDMP"):
        raise ValueError("not a pg_dump archive (missing PGDMP magic)")
    r = _Reader(data[5:])
    vmaj, vmin, vrev = r.byte(), r.byte(), r.byte()
    ver = (vmaj, vmin, vrev)
    r.int_size = r.byte()
    _off = r.byte()
    fmt = r.byte()
    out = {"archive_version": f"{vmaj}.{vmin}-{vrev}", "format_code": fmt}
    if ver >= (1, 15, 0):
        algo = r.byte()
        out["compression"] = {0: "none", 1: "gzip", 2: "lz4", 3: "zstd"}.get(algo, str(algo))
    elif ver >= (1, 2, 0):
        lvl = r.int()
        out["compression"] = "none" if lvl == 0 else f"gzip({lvl})"
    if ver >= (1, 4, 0):
        sec, mn, hr, mday, mon, year, _dst = (r.int() for _ in range(7))
        out["created"] = f"{year + 1900:04d}-{mon + 1:02d}-{mday:02d} {hr:02d}:{mn:02d}:{sec:02d}"
        out["dbname"] = r.str()
    if ver >= (1, 10, 0):
        out["server_version"] = r.str()
        out["pg_dump_version"] = r.str()
    return out


_TOC_LINE = re.compile(r"^\d+;\s+\d+\s+\d+\s+(?P<rest>.*)$")
_TOC_KINDS = ["TABLE DATA", "SEQUENCE SET", "SEQUENCE OWNED BY", "MATERIALIZED VIEW", "FK CONSTRAINT",
              "DEFAULT ACL", "EXTENSION", "SCHEMA", "TABLE", "SEQUENCE", "VIEW", "FUNCTION", "INDEX",
              "CONSTRAINT", "DEFAULT", "COMMENT", "ACL", "TYPE", "TRIGGER", "DATABASE", "ENCODING"]


def parse_toc_listing(text: str, info: DumpInfo) -> None:
    """Fill objects from `pg_restore -l` output."""
    roles, ext, schemas, tables = set(), [], set(), []
    for line in text.splitlines():
        m = _TOC_LINE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        kind = next((k for k in _TOC_KINDS if rest.startswith(k + " ")), None)
        if not kind:
            continue
        parts = rest[len(kind):].split()
        if kind == "EXTENSION" and len(parts) >= 2:
            ext.append(parts[1])
        elif kind == "SCHEMA" and len(parts) >= 2:
            schemas.add(parts[1])
            if len(parts) >= 3:
                roles.add(parts[2])
        elif kind == "TABLE" and len(parts) >= 2:
            tables.append(f"{parts[0]}.{parts[1]}")
            if len(parts) >= 3:
                roles.add(parts[2])
        elif kind in ("SEQUENCE", "VIEW", "MATERIALIZED VIEW", "FUNCTION", "TYPE") and len(parts) >= 3:
            roles.add(parts[-1])
            if parts[0] != "-":
                schemas.add(parts[0])
    roles.discard("-")
    roles.discard("pg_database_owner")
    info.extensions = sorted(set(ext))
    info.roles = sorted(r for r in roles if not r.startswith("pg_"))
    info.schemas = sorted(schemas)
    info.tables = sorted(set(tables))


_RE_EXT = re.compile(r"^CREATE EXTENSION (?:IF NOT EXISTS )?\"?([\w-]+)\"?", re.M)
_RE_OWNER = re.compile(r"\bOWNER TO \"?([\w$-]+)\"?;", re.M)
_RE_GRANT = re.compile(r"^(?:GRANT|REVOKE) .*? (?:TO|FROM) ([\w$\", -]+);", re.M)
_RE_TABLE = re.compile(r"^CREATE (?:UNLOGGED )?TABLE (?:IF NOT EXISTS )?([\w\".]+) \(", re.M)
_RE_SCHEMA = re.compile(r"^CREATE SCHEMA (?:IF NOT EXISTS )?\"?([\w-]+)\"?", re.M)
_RE_SET = re.compile(r"^SET (\w+) = ", re.M)
_RE_ROLE = re.compile(r"^CREATE ROLE \"?([\w$-]+)\"?", re.M)


def _open_text(path: str, compressed: bool):
    if compressed:
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def parse_plain(path: str, compressed: bool, info: DumpInfo, limit: int = 512 * 1024 * 1024) -> None:
    """Scan a plain SQL dump (streamed; COPY data blocks are skipped)."""
    ext, roles, tables, schemas, settings, created_roles = [], set(), [], set(), set(), set()
    in_copy = False
    tail = ""
    read = 0
    try:
        with _open_text(path, compressed) as fh:
            for line in fh:
                read += len(line)
                tail = (tail + line)[-400:]
                if in_copy:
                    if line.startswith("\\."):
                        in_copy = False
                    continue
                if line.startswith("COPY ") and line.rstrip().endswith("FROM stdin;"):
                    in_copy = True
                    continue
                if line.startswith("-- Dumped from database version"):
                    info.server_version = line.split("version", 1)[1].strip()
                elif line.startswith("-- Dumped by pg_dump version") or line.startswith("-- Dumped by pg_dumpall version"):
                    info.pg_dump_version = line.split("version", 1)[1].strip()
                elif line.startswith("-- PostgreSQL database cluster dump"):
                    info.is_dumpall = True
                elif line.startswith("\\restrict "):
                    info.uses_restrict = True
                elif line.startswith("\\connect ") and info.dbname is None and not info.is_dumpall:
                    info.dbname = line.split()[1].strip('"')
                for rx, bucket in ((_RE_EXT, ext), (_RE_TABLE, tables)):
                    m = rx.match(line)
                    if m:
                        bucket.append(m.group(1).replace('"', ""))
                m = _RE_SCHEMA.match(line)
                if m:
                    schemas.add(m.group(1))
                m = _RE_SET.match(line)
                if m:
                    settings.add(m.group(1))
                m = _RE_ROLE.match(line)
                if m:
                    created_roles.add(m.group(1))
                for m in _RE_OWNER.finditer(line):
                    roles.add(m.group(1))
                m = _RE_GRANT.match(line)
                if m:
                    for r in m.group(1).split(","):
                        r = r.strip().strip('"')
                        if r and r.upper() != "PUBLIC":
                            roles.add(r)
                if read > limit:
                    info.problems.append(f"stopped scanning after {limit // (1024 * 1024)} MB")
                    break
    except (OSError, EOFError) as e:
        info.problems.append(f"could not read the whole file: {e}")
        info.complete = False
    if info.complete is None and read <= limit:
        info.complete = "-- PostgreSQL database dump complete" in tail or "-- PostgreSQL database cluster dump complete" in tail
        if not info.complete:
            info.problems.append("did pg_dump die or run out of disk?")
    if in_copy and info.complete is False:
        info.problems.append("file ends in the middle of table data")
    info.extensions = sorted(set(ext))
    info.tables = sorted(t if "." in t else f"public.{t}" for t in set(tables))
    info.schemas = sorted(schemas)
    info.settings = sorted(settings)
    info.roles = sorted(r for r in roles - created_roles if not r.startswith("pg_") and r != "postgres")


def inspect(path: str, pg_restore: Optional[str] = None) -> DumpInfo:
    fmt, compressed = detect_format(path)
    st = os.stat(path)
    size = st.st_size
    if fmt == DIRECTORY:
        size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
    info = DumpInfo(path=path, format=fmt, size=size, mtime=st.st_mtime, compressed=compressed)
    if fmt in (CUSTOM, DIRECTORY):
        hp = os.path.join(path, "toc.dat") if fmt == DIRECTORY else path
        with open(hp, "rb") as fh:
            head = fh.read(4096)
        try:
            h = read_custom_header(head)
            info.archive_version = h.get("archive_version")
            info.dbname = h.get("dbname")
            info.server_version = h.get("server_version")
            info.pg_dump_version = h.get("pg_dump_version")
            info.created = h.get("created")
        except (ValueError, EOFError) as e:
            info.problems.append(f"unreadable archive header: {e}")
    if fmt in (CUSTOM, DIRECTORY, TAR) and pg_restore:
        p = subprocess.run([pg_restore, "-l", path], capture_output=True, text=True)
        if p.returncode == 0:
            parse_toc_listing(p.stdout, info)
            if fmt == TAR:
                for line in p.stdout.splitlines():
                    if "Dumped from database version:" in line:
                        info.server_version = line.split(":", 1)[1].strip()
                    elif "Dumped by pg_dump version:" in line:
                        info.pg_dump_version = line.split(":", 1)[1].strip()
                    elif line.startswith(";     dbname:"):
                        info.dbname = line.split(":", 1)[1].strip()
        else:
            info.problems.append("pg_restore -l failed: " + (p.stderr.strip().splitlines() or ["?"])[-1])
    elif fmt == PLAIN:
        parse_plain(path, compressed, info)
    return info


def hazards(info: DumpInfo, target_version: Optional[str], tool_version: Optional[str] = None) -> list:
    """Things that will (or are likely to) break restoring this dump into `target_version`.

    Returns a list of (level, message) where level is "error" or "warn".
    """
    out = []
    tmaj = major_of(target_version)
    smaj = info.server_major
    dmaj = info.dump_major
    if smaj and tmaj and tmaj < smaj:
        out.append(("error", f"dump comes from Postgres {smaj} but the restore target is {tmaj}: "
                             "restoring into an older major version is not supported and usually fails"))
    if dmaj and tmaj and tmaj < dmaj and info.format != PLAIN:
        out.append(("error", f"archive written by pg_dump {dmaj}; pg_restore {tmaj} can't read newer archive formats"))
    for s in info.settings:
        need = SETTING_MIN_MAJOR.get(s)
        if need and tmaj and tmaj < need:
            out.append(("error", f"dump sets '{s}', which only exists in Postgres {need}+ (target is {tmaj})"))
    if info.uses_restrict:
        tv = version_tuple(tool_version or target_version)
        if tv:
            need = RESTRICT_MIN.get(tv[0])
            if tv[0] < 13 or (need and tv[:2] < need):
                nice = ".".join(map(str, need)) if need else "a 2025+ release"
                out.append(("error", f"dump uses psql's \\restrict command; psql {'.'.join(map(str, tv))} is too old "
                                     f"(needs {nice} or newer)"))
    if info.complete is False:
        out.append(("error", "dump looks truncated"))
    if info.format == PLAIN and info.is_dumpall:
        out.append(("warn", "pg_dumpall output: it will create roles and databases on the scratch server"))
    return out
