import json

from helpers import PLAIN, custom_header, write
from restoredrill import __version__
from restoredrill.cli import main


def test_version(capsys):
    try:
        main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "restore" in capsys.readouterr().out


def test_init(capsys):
    assert main(["init"]) == 0
    assert "max_age_hours" in capsys.readouterr().out


def test_inspect_text(tmp_path, capsys):
    p = write(tmp_path / "a.sql", PLAIN)
    assert main(["inspect", p, "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert "Postgres 17.6" in out and "pgcrypto" in out and "app" in out and "\\restrict" in out


def test_inspect_incompatible_target_exit_1(tmp_path, capsys):
    p = write(tmp_path / "a.sql", PLAIN)
    assert main(["inspect", p, "--target-version", "15.8", "--format", "json"]) == 1
    j = json.loads(capsys.readouterr().out)
    assert any("transaction_timeout" in h["message"] for h in j["hazards"])


def test_inspect_custom(tmp_path, capsys):
    p = write(tmp_path / "a.dump", custom_header())
    main(["inspect", p, "--format", "json", "--pg-bin", str(tmp_path)])
    j = json.loads(capsys.readouterr().out)
    assert j["info"]["dbname"] == "shop" and j["info"]["format"] == "custom"


def test_inspect_bad_path(tmp_path, capsys):
    assert main(["inspect", str(tmp_path)]) == 2
    assert "toc.dat" in capsys.readouterr().err


def test_run_needs_backup(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["run"]) == 2
    assert "restoredrill init" in capsys.readouterr().err


def test_run_bad_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "restoredrill.toml", "nope = 1\n")
    assert main(["run", "x.sql"]) == 2
    assert "unknown key" in capsys.readouterr().err


def test_run_missing_backup_reports_fail(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = tmp_path / "summary.md"
    junit = tmp_path / "j.xml"
    rc = main(["run", str(tmp_path / "*.dump"), "--summary", str(summary), "--junit", str(junit)])
    assert rc == 1
    assert "nothing matches" in capsys.readouterr().out
    assert "FAIL" in summary.read_text() and "failures=\"1\"" in junit.read_text()


def test_run_engine_unavailable_is_exit_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("RESTOREDRILL_PG_BIN", raising=False)
    import restoredrill.engine as eng
    monkeypatch.setattr(eng, "candidate_bin_dirs", lambda extra=None: [])
    p = write(tmp_path / "a.sql", PLAIN)
    rc = main(["run", p, "--engine", "local", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["status"] == "error" and "no local Postgres 17 binaries" in out["error"]


def test_pick_newest_of_shell_expanded_glob(tmp_path):
    import os
    from restoredrill.cli import _pick
    a, b = tmp_path / "a.dump", tmp_path / "b.dump"
    a.write_text("x"); b.write_text("y")
    os.utime(a, (1000, 1000)); os.utime(b, (2000, 2000))
    assert _pick([str(a), str(b)]) == str(b)
    assert _pick([]) is None
    assert _pick([str(a)]) == str(a)
