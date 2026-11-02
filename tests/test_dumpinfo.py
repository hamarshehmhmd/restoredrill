import gzip
import os

import pytest

from helpers import PLAIN, custom_header, write
from restoredrill import dumpinfo as d


def test_custom_header_roundtrip():
    h = d.read_custom_header(custom_header())
    assert h["archive_version"] == "1.16-0"
    assert h["dbname"] == "shop"
    assert h["server_version"] == "17.6"
    assert h["pg_dump_version"] == "17.6"
    assert h["compression"] == "gzip"
    assert h["created"] == "2026-11-02 03:04:05"


def test_custom_header_old_archive_version():
    h = d.read_custom_header(custom_header(ver=(1, 14, 0), server="14.9", dump="14.9"))
    assert h["archive_version"] == "1.14-0"
    assert h["server_version"] == "14.9"
    assert h["compression"] == "gzip(1)" or h["compression"].startswith("gzip") or h["compression"] == "none"


def test_custom_header_rejects_non_archive():
    with pytest.raises(ValueError):
        d.read_custom_header(b"hello")


def test_custom_header_truncated():
    with pytest.raises(EOFError):
        d.read_custom_header(custom_header()[:20])


def test_detect_formats(tmp_path):
    assert d.detect_format(write(tmp_path / "a.dump", custom_header())) == (d.CUSTOM, False)
    assert d.detect_format(write(tmp_path / "a.sql", PLAIN)) == (d.PLAIN, False)
    gz = tmp_path / "a.sql.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write(PLAIN)
    assert d.detect_format(str(gz)) == (d.PLAIN, True)
    dd = tmp_path / "dir"
    dd.mkdir()
    write(dd / "toc.dat", custom_header(fmt=5))
    assert d.detect_format(str(dd)) == (d.DIRECTORY, False)
    tar = bytearray(1024)
    tar[257:262] = b"ustar"
    assert d.detect_format(write(tmp_path / "a.tar", bytes(tar))) == (d.TAR, False)


def test_detect_rejects_dir_without_toc(tmp_path):
    with pytest.raises(ValueError, match="toc.dat"):
        d.detect_format(str(tmp_path))


def test_detect_rejects_gzipped_custom(tmp_path):
    p = tmp_path / "x.dump.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(custom_header())
    with pytest.raises(ValueError, match="gunzip"):
        d.detect_format(str(p))


def test_inspect_plain(tmp_path):
    i = d.inspect(write(tmp_path / "a.sql", PLAIN))
    assert i.format == d.PLAIN
    assert i.server_version == "17.6" and i.server_major == 17
    assert i.extensions == ["pgcrypto"]
    assert i.tables == ["billing.invoices", "public.users"]
    assert i.schemas == ["billing"]
    assert set(i.roles) == {"app", "billing_admin", "readonly", "analyst"}
    assert i.uses_restrict
    assert "transaction_timeout" in i.settings
    assert i.complete is True
    assert i.problems == []


def test_inspect_plain_skips_copy_data(tmp_path):
    i = d.inspect(write(tmp_path / "a.sql", PLAIN))
    assert "evil" not in " ".join(i.tables)


def test_inspect_plain_gzip(tmp_path):
    p = tmp_path / "a.sql.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(PLAIN)
    i = d.inspect(str(p))
    assert i.compressed and i.complete and i.tables == ["billing.invoices", "public.users"]


def test_truncated_plain(tmp_path):
    cut = PLAIN[: PLAIN.index("2\tb@x.io")]
    i = d.inspect(write(tmp_path / "a.sql", cut))
    assert i.complete is False
    assert any("middle of table data" in p for p in i.problems)
    assert ("error", "dump looks truncated") in d.hazards(i, "17.6")


def test_truncated_gzip(tmp_path):
    p = tmp_path / "a.sql.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(PLAIN * 50)
    raw = open(p, "rb").read()
    write(p, raw[: len(raw) // 2])
    i = d.inspect(str(p))
    assert i.complete is False


def test_pg_dumpall_detected(tmp_path):
    txt = "--\n-- PostgreSQL database cluster dump\n--\nCREATE ROLE app;\nALTER ROLE app WITH LOGIN;\n" \
          "-- Dumped by pg_dumpall version 16.4\nCREATE TABLE public.t (id int);\nALTER TABLE public.t OWNER TO app;\n" \
          "--\n-- PostgreSQL database cluster dump complete\n--\n"
    i = d.inspect(write(tmp_path / "all.sql", txt))
    assert i.is_dumpall and i.complete
    assert i.roles == []  # created by the dump itself
    assert any(l == "warn" for l, _ in d.hazards(i, "16"))


def test_inspect_custom_header_only(tmp_path):
    i = d.inspect(write(tmp_path / "a.dump", custom_header(server="16.3", dump="17.2")))
    assert i.format == d.CUSTOM and i.dbname == "shop"
    assert i.server_major == 16 and i.dump_major == 17


TOC = """;
; Archive created at 2026-11-02 13:19:00 EET
;     dbname: shop
;     Dumped from database version: 17.11
;     Dumped by pg_dump version: 17.11
;
; Selected TOC Entries:
;
6; 2615 16390 SCHEMA - billing app
2; 3079 16387 EXTENSION - pgcrypto 
3503; 0 0 COMMENT - EXTENSION pgcrypto 
219; 1259 16425 TABLE public users app
220; 1259 16430 TABLE billing invoices billing_admin
218; 1259 16424 SEQUENCE public users_id_seq app
3496; 0 16425 TABLE DATA public users app
230; 1255 16500 FUNCTION public touch() pg_database_owner
3349; 2606 16433 CONSTRAINT public users users_pkey app
"""


def test_parse_toc_listing():
    i = d.DumpInfo(path="x", format=d.CUSTOM, size=1, mtime=0)
    d.parse_toc_listing(TOC, i)
    assert i.extensions == ["pgcrypto"]
    assert i.tables == ["billing.invoices", "public.users"]
    assert i.roles == ["app", "billing_admin"]
    assert "billing" in i.schemas


@pytest.mark.parametrize("v,maj", [("17.6", 17), ("16.10 (Debian 16.10-1)", 16), ("9.6.24", 9), (None, None), ("x", None)])
def test_major_of(v, maj):
    assert d.major_of(v) == maj


def _info(**kw):
    base = dict(path="x", format=d.PLAIN, size=1, mtime=0, server_version="17.6", pg_dump_version="17.6")
    base.update(kw)
    return d.DumpInfo(**base)


def test_hazard_older_target():
    hz = d.hazards(_info(), "15.8")
    assert any("older major" in m for _, m in hz)


def test_hazard_same_version_clean():
    assert d.hazards(_info(), "17.6") == []


def test_hazard_archive_needs_newer_pg_restore():
    hz = d.hazards(_info(format=d.CUSTOM, server_version="15.8", pg_dump_version="17.6"), "15.8")
    assert any("pg_restore 15" in m for _, m in hz)
    assert not any("older major" in m for _, m in hz)


def test_hazard_transaction_timeout():
    hz = d.hazards(_info(settings=["transaction_timeout"], server_version="15.8", pg_dump_version="17.6"), "15.8")
    assert any("transaction_timeout" in m for _, m in hz)


@pytest.mark.parametrize("psql,bad", [("17.5", True), ("17.6", False), ("16.9", True), ("16.10", False),
                                      ("15.13", True), ("15.14", False), ("12.20", True), ("18.0", False)])
def test_hazard_restrict(psql, bad):
    hz = d.hazards(_info(uses_restrict=True, server_version=psql), psql, psql)
    assert any("\\restrict" in m for _, m in hz) == bad
