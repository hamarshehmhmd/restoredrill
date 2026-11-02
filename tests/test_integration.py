"""Real restores against real Postgres binaries. Skipped when none are installed.

Point RESTOREDRILL_PG_BIN at one or more bin dirs (':'-separated) that contain initdb, pg_ctl,
postgres, psql, pg_dump and pg_restore.
"""
import gzip
import json
import os
import shutil
import subprocess
import time

import pytest

from restoredrill import engine
from restoredrill.cli import main
from restoredrill.drill import drill

HIT = engine.find_local(None)
pytestmark = pytest.mark.skipif(
    HIT is None or not os.path.isfile(os.path.join(HIT[0], "pg_dump")),
    reason="no local Postgres binaries (set RESTOREDRILL_PG_BIN)")


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A real source database 'shop' with an app role, an extension and data; dumped 3 ways."""
    bindir, version = HIT
    out = tmp_path_factory.mktemp("dumps")
    with engine.LocalServer(bindir, version).start() as srv:
        assert srv.query("postgres", "create role app")[0]
        assert srv.query("postgres", "create database shop owner app")[0]
        sql = ("create extension if not exists pgcrypto; set role app; "
               "create table users(id serial primary key, email text, created_at timestamptz default now()); "
               "insert into users(email) select 'u' || g || '@x.io' from generate_series(1, 300) g; "
               "create table orders(id serial primary key, user_id int references users, total numeric, "
               "created_at timestamptz default now() - interval '2 hours'); "
               "insert into orders(user_id, total) select (g % 300) + 1, g from generate_series(1, 1200) g;")
        p = srv.psql("shop", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql])
        assert p.returncode == 0, p.stderr
        dump = os.path.join(bindir, "pg_dump")
        paths = {}
        for fmt, name in (("c", "shop.dump"), ("p", "shop.sql"), ("d", "shop.dir"), ("t", "shop.tar")):
            dest = str(out / name)
            r = subprocess.run([dump, "-F", fmt, "-d", "shop", "-f", dest], env=srv.env, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            paths[fmt] = dest
        r = subprocess.run([os.path.join(bindir, "pg_dumpall"), "-f", str(out / "all.sql")], env=srv.env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        paths["all"] = str(out / "all.sql")
        gz = str(out / "shop.sql.gz")
        with open(paths["p"], "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        paths["gz"] = gz
    paths["version"] = version
    return paths


def _cfg(**kw):
    c = {"engine": "local", "roles": ["app"],
         "expect_tables": [{"table": "public.users", "min_rows": 300}, {"table": "public.orders", "min_rows": 1}]}
    c.update(kw)
    return c


@pytest.mark.parametrize("fmt", ["c", "p", "d", "t", "gz"])
def test_every_format_restores_and_passes(source, fmt):
    rep = drill(_cfg(jobs=2), source[fmt])
    assert rep.status == "pass", [r.to_dict() for r in rep.results if not r.ok] + rep.restore_errors
    assert rep.tables["public.users"] == 300 and rep.tables["public.orders"] == 1200
    assert rep.target.split(".")[0] == source["version"].split(".")[0]
    names = [r.name for r in rep.results]
    assert "all 2 tables in the dump were restored" in names and "extensions restored" in names


def test_missing_role_fails_with_hint(source):
    rep = drill(_cfg(roles=[]), source["c"])
    assert rep.status == "fail"
    assert any('role "app" does not exist' in e["error"] and "roles" in e["hint"] for e in rep.restore_errors)


def test_no_owner_passes_without_roles(source):
    assert drill(_cfg(roles=[], no_owner=True), source["c"]).status == "pass"


def test_allow_errors(source):
    assert drill(_cfg(roles=[], allow_errors=5), source["p"]).status == "pass"


def test_custom_checks(source):
    cfg = _cfg(check=[
        {"name": "orders are fresh", "sql": "select extract(epoch from now() - max(created_at)) / 3600 from orders",
         "max": 26},
        {"name": "revenue", "sql": "select sum(total) from orders", "min": 700000},
        {"name": "pgcrypto works", "sql": "select length(gen_random_uuid()::text)", "equals": "36"},
        {"name": "impossible", "sql": "select count(*) from users where email is null", "min": 1},
    ])
    rep = drill(cfg, source["c"])
    by = {r.name: r for r in rep.results}
    assert by["orders are fresh"].ok and by["revenue"].ok and by["pgcrypto works"].ok
    assert not by["impossible"].ok and rep.status == "fail"


def test_truncated_custom_dump_fails(source, tmp_path):
    raw = open(source["c"], "rb").read()
    p = tmp_path / "cut.dump"
    p.write_bytes(raw[: len(raw) // 2])
    rep = drill(_cfg(), str(p))
    assert rep.status == "fail"
    assert rep.restore_errors


def test_truncated_plain_dump_fails(source, tmp_path):
    raw = open(source["p"], "rb").read()
    p = tmp_path / "cut.sql"
    p.write_bytes(raw[: int(len(raw) * 0.6)])
    rep = drill(_cfg(), str(p))
    assert rep.status == "fail"
    assert any(r.name == "backup is complete" and not r.ok for r in rep.results)


def test_stale_backup_fails(source, tmp_path):
    p = tmp_path / "old.dump"
    shutil.copy(source["c"], p)
    old = time.time() - 4 * 86400
    os.utime(p, (old, old))
    rep = drill(_cfg(max_age_hours=26), str(p))
    assert rep.status == "fail"
    assert [r for r in rep.results if not r.ok][0].name == "backup is newer than 26 h"


def test_history_detects_shrink(source, tmp_path):
    h = str(tmp_path / "h.jsonl")
    rc = main(["run", source["c"], "--role", "app", "--history", h, "--format", "json"])
    assert rc == 0
    with open(h) as fh:
        prev = json.loads(fh.readline())
    assert prev["rows_total"] == 1500
    # pretend the last good backup was 10x bigger
    with open(h, "a") as fh:
        fh.write(json.dumps(dict(prev, size=prev["size"] * 10, rows_total=15000)) + "\n")
    rep = drill(_cfg(history=h, max_shrink_pct=50), source["c"])
    bad = {r.name for r in rep.results if not r.ok}
    assert "size didn't drop more than 50% since last pass" in bad
    assert "row count didn't drop more than 50% since last pass" in bad


def test_cli_end_to_end_reports(source, tmp_path, capsys):
    cfgp = tmp_path / "restoredrill.toml"
    cfgp.write_text(f'backup = "{os.path.dirname(source["c"])}/shop.dump"\nroles = ["app"]\nengine = "local"\n'
                    'expect_tables = {{ "public.users" = 300 }}\n'.replace("{{", "{").replace("}}", "}"))
    summary, junit = tmp_path / "s.md", tmp_path / "j.xml"
    rc = main(["run", "-c", str(cfgp), "--summary", str(summary), "--junit", str(junit), "--color", "never"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "PASS" in out and "public.users has >= 300 rows" in out
    assert "✅ Restore drill: PASS" in summary.read_text()
    assert 'failures="0"' in junit.read_text()


def test_older_target_if_available(source):
    majors = engine.available_majors()
    src = int(source["version"].split(".")[0])
    older = [m for m in majors if m < src]
    if not older:
        pytest.skip("only one Postgres major installed")
    rep = drill(_cfg(target_version=str(older[0])), source["c"])
    assert rep.status == "fail"
    assert any(r.kind == "compat" and not r.ok for r in rep.results)


def test_pg_dumpall_restores_with_database_setting(source):
    rep = drill(_cfg(roles=[], database="shop"), source["all"])
    assert rep.status == "pass", [r.to_dict() for r in rep.results if not r.ok] + rep.restore_errors
    assert rep.tables["public.orders"] == 1200
