"""restoredrill command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__, config, dumpinfo, engine, report
from .drill import append_history, drill

EPILOG = """examples:
  restoredrill inspect backups/shop.dump
  restoredrill run backups/*.dump --role app --max-age 26
  restoredrill run                       # uses ./restoredrill.toml
  restoredrill run --format json > drill.json
  restoredrill init > restoredrill.toml
exit codes: 0 pass, 1 drill failed, 2 could not run"""


def _parser():
    ap = argparse.ArgumentParser(prog="restoredrill", description="Prove your Postgres backups actually restore.",
                                 epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"restoredrill {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="restore a backup into a throwaway server and check it")
    r.add_argument("backup", nargs="*", help="backup file(s), directory or glob (newest match wins)")
    r.add_argument("-c", "--config", help="config file (default: ./restoredrill.toml if present)")
    r.add_argument("--engine", choices=["auto", "local", "docker"])
    r.add_argument("--target-version", help="restore into this major version (default: same as the dump)")
    r.add_argument("--pg-bin", help="Postgres bin dir(s) to use, separated by ':'")
    r.add_argument("--image", help="Docker image for --engine docker (default postgres:<major>)")
    r.add_argument("--role", action="append", default=None, help="create this role before restoring (repeatable)")
    r.add_argument("--no-owner", action="store_true", default=None, help="restore with --no-owner --no-acl")
    r.add_argument("--max-age", type=float, help="fail if the backup is older than this many hours")
    r.add_argument("--min-size", help="fail if the backup is smaller (e.g. 10MB)")
    r.add_argument("--expect-table", action="append", default=None, metavar="TABLE[:MIN_ROWS]")
    r.add_argument("--history", help="JSONL file to compare against and append to")
    r.add_argument("--format", choices=["text", "json", "markdown", "junit"], default="text")
    r.add_argument("--summary", help="also write a Markdown report here (default: $GITHUB_STEP_SUMMARY if set)")
    r.add_argument("--junit", help="also write a JUnit XML report here")
    r.add_argument("--color", choices=["auto", "always", "never"], default="auto")

    i = sub.add_parser("inspect", help="read a backup without restoring it")
    i.add_argument("backup")
    i.add_argument("--target-version", help="also check compatibility with this Postgres version")
    i.add_argument("--pg-bin")
    i.add_argument("--format", choices=["text", "json"], default="text")
    i.add_argument("--color", choices=["auto", "always", "never"], default="auto")

    sub.add_parser("init", help="print a starter restoredrill.toml")
    d = sub.add_parser("doctor", help="show which Postgres versions and engines are available")
    d.add_argument("--pg-bin")
    return ap


def _merge(cfg: dict, a) -> dict:
    cfg = dict(cfg)
    for key, val in (("engine", a.engine), ("target_version", a.target_version), ("pg_bin", a.pg_bin),
                     ("image", a.image), ("max_age_hours", a.max_age), ("history", a.history)):
        if val is not None:
            cfg[key] = val
    if a.role:
        cfg["roles"] = list(cfg.get("roles", [])) + a.role
    if a.no_owner:
        cfg["no_owner"] = True
    if a.min_size:
        cfg["min_size"] = config.parse_size(a.min_size)
    if a.expect_table:
        et = list(cfg.get("expect_tables", []))
        for t in a.expect_table:
            name, _, n = t.partition(":")
            et.append({"table": name, "min_rows": int(n or 0)})
        cfg["expect_tables"] = et
    return cfg


def cmd_run(a) -> int:
    path = a.config or config.find_config()
    try:
        cfg = _merge(config.load(path), a)
    except (config.ConfigError, ValueError) as e:
        print(f"restoredrill: {e}", file=sys.stderr)
        return 2
    a.backup = _pick(a.backup)
    if not a.backup and not cfg.get("backup") and not cfg.get("fetch"):
        print("restoredrill: give a backup path/glob, or create restoredrill.toml (restoredrill init)", file=sys.stderr)
        return 2
    rep = drill(cfg, a.backup)
    if not rep.error:
        append_history(cfg.get("history"), rep)
    color = report.use_color(a.color)
    out = {"text": lambda: report.text(rep, color), "json": lambda: report.as_json(rep),
           "markdown": lambda: report.markdown(rep), "junit": lambda: report.junit(rep)}[a.format]()
    print(out)
    summary = a.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report.markdown(rep))
    if a.junit:
        with open(a.junit, "w", encoding="utf-8") as fh:
            fh.write(report.junit(rep))
    return {"pass": 0, "fail": 1}.get(rep.status, 2)


def cmd_inspect(a) -> int:
    pg_restore = None
    hit = engine.find_local(None, a.pg_bin)
    if hit:
        pg_restore = os.path.join(hit[0], "pg_restore")
    else:
        pg_restore = shutil.which("pg_restore")
    try:
        info = dumpinfo.inspect(a.backup, pg_restore)
    except (ValueError, OSError) as e:
        print(f"restoredrill: {e}", file=sys.stderr)
        return 2
    hz = dumpinfo.hazards(info, a.target_version) if a.target_version else \
        [h for h in dumpinfo.hazards(info, None) if h]
    if a.format == "json":
        print(json.dumps({"info": info.to_dict(), "hazards": [{"level": l, "message": m} for l, m in hz]}, indent=2))
    else:
        print(report.inspect_text(info, hz, report.use_color(a.color)))
    return 1 if any(l == "error" for l, _ in hz) else 0


def cmd_doctor(a) -> int:
    dirs = engine.candidate_bin_dirs(a.pg_bin)
    found = False
    for d in dirs:
        v = engine._bin_version(os.path.join(d, "postgres")) if os.path.isfile(os.path.join(d, "postgres")) else None
        if v:
            found = True
            print(f"local  Postgres {v:<8} {d}")
    print(f"docker {'available' if engine.docker_available() else 'not available'}")
    if not found:
        print("no local Postgres server binaries found; restoredrill will use Docker if available")
    return 0


def _pick(paths):
    """A shell-expanded glob arrives as many paths: the newest existing one wins."""
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    real = [p for p in paths if os.path.exists(p)]
    return max(real, key=lambda p: os.stat(p).st_mtime) if real else paths[0]


def main(argv=None) -> int:
    ap = _parser()
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "inspect":
        return cmd_inspect(a)
    if a.cmd == "init":
        sys.stdout.write(config.SAMPLE)
        return 0
    if a.cmd == "doctor":
        return cmd_doctor(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
