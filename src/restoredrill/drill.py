"""Run one restore drill: pick the backup, restore it into a throwaway server, check it, report."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from . import dumpinfo
from .engine import EngineError, make_server

PASS, FAIL, ERROR = "pass", "fail", "error"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    kind: str = "check"  # check | backup | restore | compat

    def to_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "kind": self.kind}


@dataclass
class Report:
    backup: str = ""
    status: str = ERROR
    started: str = ""
    info: Optional[dict] = None
    target: str = ""
    engine: str = ""
    restore_seconds: Optional[float] = None
    total_seconds: Optional[float] = None
    restore_errors: list = field(default_factory=list)
    results: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)
    error: str = ""

    @property
    def rows_total(self) -> int:
        return int(sum(self.tables.values()))

    def add(self, name, ok, detail="", kind="check"):
        self.results.append(Result(name, bool(ok), detail, kind))

    def finish(self):
        if self.error:
            self.status = ERROR
        else:
            self.status = PASS if all(r.ok for r in self.results) else FAIL
        return self

    def to_dict(self):
        return {
            "tool": "restoredrill", "status": self.status, "backup": self.backup, "started": self.started,
            "target": self.target, "engine": self.engine, "restore_seconds": self.restore_seconds,
            "total_seconds": self.total_seconds, "restore_errors": self.restore_errors,
            "results": [r.to_dict() for r in self.results], "warnings": self.warnings,
            "tables": self.tables, "rows_total": self.rows_total, "info": self.info, "error": self.error,
        }


def newest(pattern) -> Optional[str]:
    pattern = str(pattern)
    if os.path.exists(pattern) and not any(c in pattern for c in "*?["):
        return pattern
    hits = [p for p in glob.glob(os.path.expanduser(pattern))]
    if not hits:
        return None
    return max(hits, key=lambda p: os.stat(p).st_mtime)


def fetch(cmd: str, workdir: str) -> str:
    out = os.path.join(workdir, "backup")
    full = cmd.replace("{out}", _shq(out))
    p = subprocess.run(full, shell=True, capture_output=True, text=True)
    if p.returncode:
        raise EngineError(f"fetch command failed ({p.returncode}): {(p.stderr or p.stdout).strip()[-400:]}")
    if not os.path.exists(out):
        raise EngineError("fetch command succeeded but did not write {out}")
    return out


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def human_age(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} days"


_ERR = re.compile(r"(?:^|\n)(?:pg_restore: error: |psql:[^\n]*?: )?(ERROR|FATAL|error):\s+(.*)")


def restore_errors(stderr: str) -> list:
    """Pull human-meaningful error lines out of pg_restore/psql stderr."""
    out = []
    lines = (stderr or "").splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if "ERROR:" in s:
            msg = s.split("ERROR:", 1)[1].strip()
        elif s.startswith("pg_restore: error:") and "could not execute query" not in s:
            msg = s.split("error:", 1)[1].strip()
        elif s.startswith("psql:") and ("error" in s.lower() or "invalid command" in s):
            msg = s.split(": ", 2)[-1].strip()
        elif s.startswith("restoredrill: error:"):
            msg = s.split("error:", 1)[1].strip()
        elif s.startswith("FATAL:"):
            msg = s[6:].strip()
        else:
            continue
        if msg and msg not in out:
            out.append(msg)
    return out


def hint_for(msg: str) -> str:
    m = re.search(r'role "([^"]+)" does not exist', msg)
    if m:
        return f'add roles = ["{m.group(1)}"] to restoredrill.toml (your real restore target needs it too) or set no_owner = true'
    m = re.search(r'extension "([^"]+)" is not available|could not open extension control file .*/([\w-]+)\.control', msg)
    if m:
        return "the restore target lacks this extension; install it there (or use a Docker image that has it)"
    if "unrecognized configuration parameter" in msg:
        return "the dump was made by a newer pg_dump than the target server understands"
    if "invalid command \\restrict" in msg or "invalid command \\unrestrict" in msg:
        return "restore with psql 17.6/16.10/15.14 or newer"
    if "unsupported version" in msg and "file header" in msg:
        return "pg_restore is older than the pg_dump that wrote this archive"
    if "end of file" in msg or "unexpected end" in msg:
        return "the backup file is truncated"
    return ""


def load_history(path: Optional[str]) -> list:
    if not path or not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def append_history(path: Optional[str], rep: Report):
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    entry = {"started": rep.started, "status": rep.status, "backup": rep.backup,
             "size": (rep.info or {}).get("size"), "tables": len(rep.tables), "rows_total": rep.rows_total,
             "restore_seconds": rep.restore_seconds, "target": rep.target}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _num(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_checks(server, db: str, cfg: dict, rep: Report):
    for t in cfg.get("expect_tables", []):
        name = t["table"]
        q = f"select count(*) from {name}"
        ok, val = server.query(db, q)
        need = t.get("min_rows", 0) or 0
        if not ok:
            rep.add(f"table {name} exists", False, val)
        else:
            n = _num(val) or 0
            rep.add(f"table {name} has >= {need} rows", n >= need, f"{int(n)} rows")
    for i, c in enumerate(cfg.get("check", []), 1):
        name = c.get("name") or c["sql"][:60]
        ok, val = server.query(c.get("database", db), c["sql"])
        if not ok:
            rep.add(name, False, val)
            continue
        problems, n = [], _num(val)
        if "equals" in c and str(val) != str(c["equals"]):
            problems.append(f"expected {c['equals']!r}")
        for k, cmp, word in (("min", lambda a, b: a >= b, ">="), ("max", lambda a, b: a <= b, "<=")):
            if k in c:
                if n is None:
                    problems.append(f"not a number (wanted {word} {c[k]})")
                elif not cmp(n, c[k]):
                    problems.append(f"wanted {word} {c[k]}")
        shown = "NULL" if val == "" else (f"{n:.6g}" if n is not None else val)
        rep.add(name, not problems, f"got {shown}" + (": " + ", ".join(problems) if problems else ""))


def table_counts(server, db: str) -> dict:
    server.query(db, "analyze", timeout=3600)
    p = server.psql(db, ["-X", "-q", "-A", "-t", "-F", "\t", "-c",
                         "select schemaname || '.' || relname, n_live_tup from pg_stat_user_tables order by 1"])
    out = {}
    for line in (p.stdout or "").splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            n = _num(v)
            if n is not None:
                out[k] = int(n)
    return out


def drill(cfg: dict, backup: Optional[str] = None, now: Optional[float] = None) -> Report:
    t0 = time.time()
    now = now or t0
    rep = Report(started=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)))
    work = tempfile.mkdtemp(prefix="restoredrill-fetch-")
    try:
        return _drill(cfg, backup, now, rep, work, t0)
    except EngineError as e:
        rep.error = str(e)
        return rep.finish()
    finally:
        shutil.rmtree(work, ignore_errors=True)
        rep.total_seconds = round(time.time() - t0, 2)


def _drill(cfg, backup, now, rep, work, t0):
    pattern = backup or cfg.get("backup")
    if cfg.get("fetch") and not backup:
        path = fetch(cfg["fetch"], work)
    elif pattern:
        path = newest(pattern)
        if not path:
            rep.backup = pattern
            rep.add("backup exists", False, f"nothing matches {pattern}", "backup")
            return rep.finish()
    else:
        raise EngineError("no backup given: pass a path/glob or set backup = \"...\" in restoredrill.toml")
    rep.backup = path

    # --- 1. static checks on the file
    try:
        info = dumpinfo.inspect(path)
    except (ValueError, OSError) as e:
        rep.add("backup is readable", False, str(e), "backup")
        return rep.finish()
    rep.info = info.to_dict()
    age_h = (now - info.mtime) / 3600
    if cfg.get("max_age_hours") is not None:
        rep.add(f"backup is newer than {cfg['max_age_hours']} h", age_h <= cfg["max_age_hours"],
                f"newest backup is {human_age(age_h)} old", "backup")
    min_size = cfg.get("min_size") or 0
    if min_size:
        rep.add(f"backup is at least {human_size(min_size)}", info.size >= min_size, human_size(info.size), "backup")
    else:
        rep.add("backup is not empty", info.size > 0, human_size(info.size), "backup")
    for p in info.problems:
        rep.warnings.append(p)

    hist = [h for h in load_history(cfg.get("history")) if h.get("status") == PASS]
    shrink = cfg.get("max_shrink_pct")
    last = hist[-1] if hist else None
    if shrink is not None and last and last.get("size"):
        drop = 100.0 * (1 - info.size / last["size"])
        rep.add(f"size didn't drop more than {shrink}% since last pass", drop <= shrink,
                f"{human_size(last['size'])} -> {human_size(info.size)} ({-drop:+.0f}%)", "backup")

    # --- 2. pick a target
    src_major = info.server_major
    tv = cfg.get("target_version")
    tmajor = dumpinfo.major_of(str(tv)) if tv else src_major
    server = make_server(cfg.get("engine", "auto"), tmajor, cfg.get("pg_bin"), cfg.get("image"),
                         allow_newer=not tv)
    with server:
        rep.engine = server.kind
        rep.target = server.version
        if info.format != dumpinfo.PLAIN and not info.tables:
            lp = server.list_archive(path)
            if lp.returncode == 0:
                dumpinfo.parse_toc_listing(lp.stdout, info)
                rep.info = info.to_dict()
        for level, msg in dumpinfo.hazards(info, server.version, server.tool_version()):
            if msg == "dump looks truncated":
                rep.add("backup is complete", False, "no 'dump complete' footer: the file is cut off", "backup")
            elif level == "error":
                rep.add("compatible with target", False, msg, "compat")
            else:
                rep.warnings.append(msg)
        if tmajor and dumpinfo.major_of(server.version) != tmajor:
            rep.warnings.append(f"no Postgres {tmajor} available; restored into {server.version} instead")

        # --- 3. restore
        db = cfg.get("database") or info.dbname or "restoredrill"
        if db == "postgres":
            db = "restoredrill"
        for role in cfg.get("roles", []):
            server.query("postgres", f'create role "{role}"')
        if not info.is_dumpall:
            ok, err = server.query("postgres", f'create database "{db}"')
            if not ok:
                raise EngineError(f"could not create scratch database: {err}")
        timeout = int(cfg.get("timeout_minutes", 240)) * 60
        r0 = time.time()
        try:
            if info.format == dumpinfo.PLAIN:
                p = server.psql_file("postgres" if info.is_dumpall else db, path, info.compressed, timeout=timeout)
                stderr = p.stderr.decode("utf-8", "replace") if isinstance(p.stderr, bytes) else p.stderr
            else:
                args = ["--no-password"]
                if cfg.get("no_owner"):
                    args += ["--no-owner", "--no-acl"]
                if cfg.get("jobs") and info.format in (dumpinfo.CUSTOM, dumpinfo.DIRECTORY):
                    args += ["-j", str(cfg["jobs"])]
                p = server.pg_restore(db, path, args, timeout=timeout)
                stderr = p.stderr
        except subprocess.TimeoutExpired:
            rep.add("restore finished", False, f"timed out after {timeout // 60} min", "restore")
            return rep.finish()
        rep.restore_seconds = round(time.time() - r0, 2)
        errs = restore_errors(stderr)
        if info.is_dumpall:
            # pg_dumpall always re-creates the bootstrap superuser (and any role we pre-created);
            # Postgres documents that "already exists" error as harmless.
            mine = {"postgres"} | set(cfg.get("roles") or [])
            errs = [e for e in errs if not any(e == f'role "{r}" already exists' for r in mine)]
        if info.format != dumpinfo.PLAIN and p.returncode and not errs:
            errs = [(stderr or "").strip().splitlines()[-1] if (stderr or "").strip() else f"pg_restore exited {p.returncode}"]
        rep.restore_errors = [{"error": e, "hint": hint_for(e)} for e in errs]
        allowed = int(cfg.get("allow_errors", 0))
        rep.add("restore ran without errors" if not allowed else f"restore had <= {allowed} errors",
                len(errs) <= allowed, f"{len(errs)} error(s), {rep.restore_seconds:.2f} s", "restore")

        # --- 4. what came back
        if not info.is_dumpall or cfg.get("database"):  # pg_dumpall: count the database you named
            rep.tables = table_counts(server, db)
            if info.tables and not info.is_dumpall:
                missing = [t for t in info.tables if t not in rep.tables]
                rep.add(f"all {len(info.tables)} tables in the dump were restored", not missing,
                        ("missing: " + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")) if missing
                        else f"{len(rep.tables)} tables, {rep.rows_total:,} rows", "restore")
            if info.extensions and not info.is_dumpall:
                ok, val = server.query(db, "select string_agg(extname, ',') from pg_extension")
                have = set((val or "").split(",")) if ok else set()
                miss = [e for e in info.extensions if e not in have]
                rep.add("extensions restored", not miss, "missing: " + ", ".join(miss) if miss
                        else ", ".join(info.extensions), "restore")
            if shrink is not None and last and last.get("rows_total"):
                drop = 100.0 * (1 - rep.rows_total / last["rows_total"])
                rep.add(f"row count didn't drop more than {shrink}% since last pass", drop <= shrink,
                        f"{last['rows_total']:,} -> {rep.rows_total:,} rows ({-drop:+.0f}%)", "restore")
            run_checks(server, db, cfg, rep)
    return rep.finish()
