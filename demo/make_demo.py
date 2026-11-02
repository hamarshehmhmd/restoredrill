"""Build the demo sandbox: a real Postgres 17 'shop' database, dumped the way a nightly cron job would.

Usage: RESTOREDRILL_PG_BIN=/path/to/pg17/bin python demo/make_demo.py /home/dev/acme
"""
import gzip
import os
import shutil
import subprocess
import sys

from restoredrill import engine

root = sys.argv[1]
shutil.rmtree(root, ignore_errors=True)
os.makedirs(os.path.join(root, "backups"))
os.makedirs(os.path.join(root, "backups", "nov-01-failed-job"))
hit = engine.find_local(17)
if not hit:
    sys.exit("need Postgres 17 binaries (RESTOREDRILL_PG_BIN)")
bindir, version = hit
SQL = """
create extension if not exists pgcrypto;
set role app;
create table customers(id serial primary key, email text not null, created_at timestamptz not null);
insert into customers(email, created_at)
  select 'customer' || g || '@example.com', now() - (g || ' minutes')::interval from generate_series(1, 4000) g;
create table orders(id serial primary key, customer_id int not null references customers,
  total_cents int not null, created_at timestamptz not null);
insert into orders(customer_id, total_cents, created_at)
  select (g % 4000) + 1, 500 + (g * 37) % 20000, now() - interval '10 hours' - (g || ' seconds')::interval
  from generate_series(1, 60000) g;
create index on orders(created_at);
create table api_keys(id uuid primary key default gen_random_uuid(), customer_id int references customers, label text);
insert into api_keys(customer_id, label) select g, 'default' from generate_series(1, 800) g;
"""
with engine.LocalServer(bindir, version).start() as srv:
    srv.query("postgres", "create role app")
    srv.query("postgres", "create database shop owner app")
    p = srv.psql("shop", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", SQL])
    assert p.returncode == 0, p.stderr
    dump = os.path.join(bindir, "pg_dump")
    b = os.path.join(root, "backups")
    for day in ("2026-10-31", "2026-11-01", "2026-11-02"):
        subprocess.run([dump, "-Fc", "-d", "shop", "-f", f"{b}/shop-{day}.dump"], env=srv.env, check=True)
    sqlpath = f"{b}/nov-01-failed-job/shop-2026-11-01.sql"
    subprocess.run([dump, "-d", "shop", "-f", sqlpath], env=srv.env, check=True)
# The Nov 1 plain-SQL job ran out of disk halfway through: keep only the first 45% of the gzip stream.
raw = open(sqlpath, "rb").read()
gz = gzip.compress(raw)
with open(sqlpath + ".gz", "wb") as fh:
    fh.write(gz[: int(len(gz) * 0.45)])
os.remove(sqlpath)
with open(os.path.join(root, "drill.toml"), "w") as fh:
    fh.write('''backup = "backups/shop-*.dump"
max_age_hours = 26
min_size = "500KB"
roles = ["app"]

expect_tables = { "public.customers" = 1000, "public.orders" = 1000 }

[[check]]
name = "newest order is < 26 h old"
sql = "select extract(epoch from now() - max(created_at)) / 3600 from orders"
max = 26

[[check]]
name = "pgcrypto works"
sql = "select length(gen_random_uuid()::text)"
equals = "36"
''')
print("demo sandbox ready:", root)
