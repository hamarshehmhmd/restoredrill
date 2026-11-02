import os
import struct


def pg_int(v, size=4):
    return bytes([1 if v < 0 else 0]) + abs(v).to_bytes(size, "little")


def pg_str(s):
    if s is None:
        return pg_int(-1)
    b = s.encode()
    return pg_int(len(b)) + b


def custom_header(ver=(1, 16, 0), dbname="shop", server="17.6", dump="17.6", fmt=1, when=(5, 4, 3, 2, 10, 126)):
    """Synthesize the start of a pg_dump -Fc archive."""
    out = b"PGDMP" + bytes(ver) + bytes([4, 8, fmt])
    if ver >= (1, 15, 0):
        out += bytes([1])  # gzip
    else:
        out += pg_int(-1)
    sec, mn, hr, mday, mon, year = when
    for v in (sec, mn, hr, mday, mon, year, 0):
        out += pg_int(v)
    out += pg_str(dbname)
    if ver >= (1, 10, 0):
        out += pg_str(server) + pg_str(dump)
    return out + b"\x00" * 64


PLAIN = """--
-- PostgreSQL database dump
--

\\restrict abc123

-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.6

SET statement_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';

CREATE SCHEMA billing;

ALTER SCHEMA billing OWNER TO app;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE TABLE public.users (
    id integer NOT NULL,
    email text
);

ALTER TABLE public.users OWNER TO app;

CREATE TABLE billing.invoices (
    id integer NOT NULL
);

ALTER TABLE billing.invoices OWNER TO "billing_admin";

COPY public.users (id, email) FROM stdin;
1\tCREATE TABLE evil (x int);
2\tb@x.io
\\.

GRANT SELECT ON TABLE public.users TO readonly, analyst;

\\unrestrict abc123

--
-- PostgreSQL database dump complete
--
"""


def write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return str(path)
