"""Load restoredrill.toml (or .json)."""
from __future__ import annotations

import json
import os
import re

try:
    import tomllib as _toml  # Python 3.11+
except ImportError:  # pragma: no cover - exercised on 3.9/3.10
    try:
        import tomli as _toml
    except ImportError:
        _toml = None

DEFAULT_NAMES = ("restoredrill.toml", ".restoredrill.toml", "restoredrill.json")

KNOWN = {"backup", "fetch", "max_age_hours", "min_size", "target_version", "roles", "no_owner", "allow_errors",
         "jobs", "database", "check", "expect_tables", "max_shrink_pct", "history", "engine", "image",
         "pg_bin", "timeout_minutes", "create_extensions_schema"}


class ConfigError(ValueError):
    pass


def parse_size(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?)?\s*", str(v), re.I)
    if not m:
        raise ConfigError(f"can't parse size {v!r} (use e.g. 500KB, 20MB, 1.5GB)")
    n = float(m.group(1))
    unit = (m.group(2) or "").lower().rstrip("b").rstrip("i")
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[unit]
    return int(n * mult)


def find_config(start: str = ".") -> str | None:
    for n in DEFAULT_NAMES:
        p = os.path.join(start, n)
        if os.path.isfile(p):
            return p
    return None


def load(path: str | None) -> dict:
    if not path:
        return {}
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.endswith(".json"):
        data = json.loads(raw.decode("utf-8"))
    else:
        if _toml is None:
            raise ConfigError("TOML config needs Python 3.11+ or `pip install tomli` (or use restoredrill.json)")
        try:
            data = _toml.loads(raw.decode("utf-8"))
        except Exception as e:  # tomllib.TOMLDecodeError
            raise ConfigError(f"{path}: {e}") from None
    return validate(data, path)


def validate(data: dict, where: str = "config") -> dict:
    unknown = set(data) - KNOWN
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    checks = data.get("check", [])
    if not isinstance(checks, list):
        raise ConfigError(f"{where}: use [[check]] tables for checks")
    for i, c in enumerate(checks, 1):
        if not isinstance(c, dict) or not c.get("sql"):
            raise ConfigError(f"{where}: check #{i} needs an sql = \"...\" query")
        bad = set(c) - {"name", "sql", "min", "max", "equals", "database"}
        if bad:
            raise ConfigError(f"{where}: check #{i} has unknown key(s): {', '.join(sorted(bad))}")
        for k in ("min", "max"):
            if k in c and not isinstance(c[k], (int, float)):
                raise ConfigError(f"{where}: check #{i} {k} must be a number")
    et = data.get("expect_tables", [])
    if isinstance(et, dict):
        data["expect_tables"] = [{"table": k, "min_rows": v} for k, v in et.items()]
    elif isinstance(et, list):
        data["expect_tables"] = [{"table": t, "min_rows": 0} if isinstance(t, str) else t for t in et]
    roles = data.get("roles", [])
    if isinstance(roles, str):
        data["roles"] = [roles]
    if "min_size" in data:
        data["min_size"] = parse_size(data["min_size"])
    return data


SAMPLE = '''# restoredrill.toml: what "this backup is good" means for you.
# Run: restoredrill run            (reads this file)

# Path or glob; the newest match is drilled.
backup = "backups/*.dump"
# Or fetch it first: {out} is replaced with a temp file path.
# fetch = "aws s3 cp s3://my-bucket/db/latest.dump {out}"

max_age_hours = 26          # fail if the newest backup is older (the cron job silently stopped)
min_size = "1MB"            # fail if it's suspiciously small
max_shrink_pct = 50         # fail if size or row count dropped this much since the last passing drill
history = ".restoredrill-history.jsonl"

# Restore target. Default: the same major version the dump came from.
# target_version = 17
# engine = "auto"           # auto | local | docker
roles = []                  # roles your dump references (OWNER TO / GRANT); created before restore
no_owner = false            # true = pg_restore --no-owner --no-acl
allow_errors = 0            # restore errors you accept

# Tables that must exist after restore, with a minimum row count.
# expect_tables = { "public.users" = 1, "public.orders" = 1 }

# Your own smoke queries. First column of the first row is compared.
# [[check]]
# name = "newest order is recent"
# sql = "select extract(epoch from now() - max(created_at)) / 3600 from orders"
# max = 26
'''
