import json

import pytest

from restoredrill import config


@pytest.mark.parametrize("v,n", [("1MB", 1024 ** 2), ("500k", 500 * 1024), ("1.5GB", int(1.5 * 1024 ** 3)),
                                 (10, 10), ("20 MiB", 20 * 1024 ** 2), ("7", 7), (None, 0)])
def test_parse_size(v, n):
    assert config.parse_size(v) == n


def test_parse_size_bad():
    with pytest.raises(config.ConfigError):
        config.parse_size("lots")


def test_sample_parses():
    cfg = config.validate(config._toml.loads(config.SAMPLE))
    assert cfg["max_age_hours"] == 26 and cfg["min_size"] == 1024 ** 2


def test_load_toml(tmp_path):
    p = tmp_path / "restoredrill.toml"
    p.write_text('backup = "b/*.dump"\nroles = "app"\nexpect_tables = { "public.users" = 5 }\n'
                 '[[check]]\nname = "x"\nsql = "select 1"\nmin = 1\n')
    cfg = config.load(str(p))
    assert cfg["roles"] == ["app"]
    assert cfg["expect_tables"] == [{"table": "public.users", "min_rows": 5}]
    assert cfg["check"][0]["min"] == 1
    assert config.find_config(str(tmp_path)) == str(p)


def test_load_json(tmp_path):
    p = tmp_path / "restoredrill.json"
    p.write_text(json.dumps({"backup": "x.sql", "expect_tables": ["public.t"]}))
    assert config.load(str(p))["expect_tables"] == [{"table": "public.t", "min_rows": 0}]


@pytest.mark.parametrize("body,msg", [
    ('bakup = "x"', "unknown key"),
    ('[[check]]\nname = "no sql"', "needs an sql"),
    ('[[check]]\nsql = "select 1"\nmin = "one"', "must be a number"),
    ('[[check]]\nsql = "select 1"\nmaximum = 3', "unknown key"),
    ('backup = ', "config|Invalid|Expected|value"),
])
def test_bad_config(tmp_path, body, msg):
    p = tmp_path / "c.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError, match=msg):
        config.load(str(p))


def test_missing_config():
    with pytest.raises(config.ConfigError, match="not found"):
        config.load("/nonexistent/restoredrill.toml")
