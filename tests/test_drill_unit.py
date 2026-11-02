import json
import os
import time

from helpers import PLAIN, write
from restoredrill import drill as dr
from restoredrill import report
from restoredrill.drill import Report


def test_restore_errors_pg_restore():
    err = ('pg_restore: error: could not execute query: ERROR:  role "app" does not exist\n'
           'Command was: ALTER TABLE public.users OWNER TO app;\n'
           'pg_restore: error: could not execute query: ERROR:  role "app" does not exist\n'
           'pg_restore: warning: errors ignored on restore: 2\n')
    assert dr.restore_errors(err) == ['role "app" does not exist']


def test_restore_errors_psql_and_eof():
    err = ('psql:/tmp/x.sql:5: error: invalid command \\restrict\n'
           'psql:/tmp/x.sql:9: ERROR:  extension "postgis" is not available\n'
           'pg_restore: error: could not read from input file: end of file\n')
    errs = dr.restore_errors(err)
    assert "invalid command \\restrict" in errs[0]
    assert 'extension "postgis" is not available' in errs
    assert "could not read from input file: end of file" in errs


def test_hints():
    assert 'roles = ["app"]' in dr.hint_for('role "app" does not exist')
    assert "extension" in dr.hint_for('extension "postgis" is not available')
    assert "newer pg_dump" in dr.hint_for('unrecognized configuration parameter "transaction_timeout"')
    assert "17.6" in dr.hint_for("invalid command \\restrict")
    assert "truncated" in dr.hint_for("could not read from input file: end of file")
    assert dr.hint_for("something else") == ""


def test_newest_picks_latest(tmp_path):
    a = write(tmp_path / "db-1.dump", "a")
    b = write(tmp_path / "db-2.dump", "b")
    os.utime(a, (time.time() - 100, time.time() - 100))
    assert dr.newest(str(tmp_path / "db-*.dump")) == str(b)
    assert dr.newest(str(tmp_path / "nope-*")) is None
    assert dr.newest(str(a)) == str(a)


def test_missing_backup_fails_without_server(tmp_path):
    rep = dr.drill({}, str(tmp_path / "*.dump"))
    assert rep.status == "fail"
    assert rep.results[0].name == "backup exists"


def test_stale_and_small_backup_fail_before_restore(tmp_path, monkeypatch):
    p = write(tmp_path / "db.sql", PLAIN)
    old = time.time() - 9 * 86400
    os.utime(p, (old, old))
    calls = []

    def fake(*a, **k):
        calls.append(a)
        raise dr.EngineError("no server in unit tests")
    monkeypatch.setattr(dr, "make_server", fake)
    rep = dr.drill({"max_age_hours": 26, "min_size": 10 ** 6}, p)
    by = {r.name: r for r in rep.results}
    assert not by["backup is newer than 26 h"].ok
    assert "9.0 days" in by["backup is newer than 26 h"].detail
    assert not by["backup is at least 976.6 KB"].ok
    assert rep.status == "error" and calls and calls[0][1] == 17  # same major as the dump


def test_unreadable_backup(tmp_path):
    d = tmp_path / "notadump"
    d.mkdir()
    rep = dr.drill({}, str(d))
    assert rep.status == "fail" and "toc.dat" in rep.results[0].detail


def test_fetch_command(tmp_path):
    src = write(tmp_path / "src.sql", PLAIN)
    out = dr.fetch(f"cp {src} {{out}}", str(tmp_path))
    assert open(out).read() == PLAIN


def test_fetch_failure(tmp_path):
    import pytest
    with pytest.raises(dr.EngineError, match="fetch command failed"):
        dr.fetch("exit 3", str(tmp_path))


def test_history_roundtrip(tmp_path):
    h = str(tmp_path / "sub" / "h.jsonl")
    rep = Report(backup="b", status="pass", info={"size": 100}, tables={"public.a": 3, "public.b": 4})
    dr.append_history(h, rep)
    dr.append_history(h, rep)
    with open(h, "a") as fh:
        fh.write("not json\n")
    hist = dr.load_history(h)
    assert len(hist) == 2 and hist[0]["rows_total"] == 7 and hist[0]["size"] == 100


def test_shrink_vs_history(tmp_path, monkeypatch):
    h = tmp_path / "h.jsonl"
    h.write_text(json.dumps({"status": "pass", "size": 10 ** 6, "rows_total": 1000}) + "\n")
    p = write(tmp_path / "db.sql", PLAIN)
    monkeypatch.setattr(dr, "make_server", lambda *a, **k: (_ for _ in ()).throw(dr.EngineError("x")))
    rep = dr.drill({"history": str(h), "max_shrink_pct": 50}, p)
    r = [r for r in rep.results if r.name.startswith("size didn't drop")][0]
    assert not r.ok and "-100%" in r.detail or "-99%" in r.detail


class FakeServer:
    def __init__(self, answers):
        self.answers = answers

    def query(self, db, sql, timeout=300):
        return self.answers[sql]


def test_run_checks():
    srv = FakeServer({"select count(*) from public.users": (True, "42"),
                      "select count(*) from public.gone": (False, 'relation "public.gone" does not exist'),
                      "q1": (True, "3.5"), "q2": (True, "abc"), "q3": (True, ""), "q4": (False, "syntax error")})
    cfg = {"expect_tables": [{"table": "public.users", "min_rows": 10}, {"table": "public.gone", "min_rows": 0}],
           "check": [{"name": "fresh", "sql": "q1", "max": 26}, {"name": "eq", "sql": "q2", "equals": "abc"},
                     {"name": "null", "sql": "q3", "min": 1}, {"name": "bad", "sql": "q4"},
                     {"name": "too low", "sql": "q1", "min": 4}]}
    rep = Report()
    dr.run_checks(srv, "db", cfg, rep)
    got = {r.name: r for r in rep.results}
    assert got["table public.users has >= 10 rows"].ok
    assert not got["table public.gone exists"].ok
    assert got["fresh"].ok and got["fresh"].detail == "got 3.5"
    assert got["eq"].ok
    assert not got["null"].ok and "NULL" in got["null"].detail
    assert not got["bad"].ok and "syntax" in got["bad"].detail
    assert not got["too low"].ok and "wanted >= 4" in got["too low"].detail


def _rep():
    r = Report(backup="b.dump", info={"format": "custom", "size": 2048, "server_version": "17.6"},
               target="17.6", engine="local", restore_seconds=1.25, total_seconds=2, tables={"public.u": 5})
    r.add("restore ran without errors", False, "1 error(s)", "restore")
    r.add("table x <ok> & fine", True, "5 rows")
    r.restore_errors = [{"error": 'role "app" does not exist', "hint": "add roles"}]
    r.warnings = ["careful"]
    return r.finish()


def test_report_status_and_renderers():
    r = _rep()
    assert r.status == "fail"
    t = report.text(r)
    assert "FAIL  1/2 checks passed" in t and 'role "app"' in t and "→ add roles" in t
    assert "\x1b[" not in t and "\x1b[" in report.text(r, color=True)
    md = report.markdown(r)
    assert "❌ Restore drill: FAIL" in md and "| ✅ |" in md and "Restore time:** 1.25 s" in md
    j = json.loads(report.as_json(r))
    assert j["status"] == "fail" and j["rows_total"] == 5
    x = report.junit(r)
    import xml.dom.minidom
    dom = xml.dom.minidom.parseString(x)
    suite = dom.getElementsByTagName("testsuite")[0]
    assert suite.getAttribute("failures") == "1" and suite.getAttribute("tests") == "2"


def test_report_error_state():
    r = Report(error="docker is not available").finish()
    assert r.status == "error"
    assert "docker is not available" in report.text(r)
    assert "<error" in report.junit(r)


def test_human():
    assert dr.human_size(512) == "512 B" and dr.human_size(1536) == "1.5 KB"
    assert dr.human_age(0.5) == "30 min" and dr.human_age(5) == "5.0 h" and dr.human_age(72) == "3.0 days"
