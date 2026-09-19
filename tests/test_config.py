import pytest

from mcp_db2.config import Settings

BASE = {
    "hostname": "h",
    "database": "d",
    "uid": "u",
    "pwd": "p",
}


def settings(**overrides) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


def test_comma_separated_lists_are_split_and_uppercased():
    s = settings(schema_denylist="sys*, nullid ,sqlj")
    assert s.schema_denylist == ["SYS*", "NULLID", "SQLJ"]


@pytest.mark.parametrize("name", ["NULLID", "NULLID  ", " nullid ", "SYSCAT", "SYSIBM  "])
def test_padded_catalog_names_are_still_matched(name):
    # SYSCAT.SCHEMATA values come back space-padded; the denylist must not be fooled.
    assert not settings().schema_visible(name)


def test_allowlist_wins_over_everything_else():
    s = settings(schema_allowlist="APP*")
    assert s.schema_visible("APPDATA ")
    assert not s.schema_visible("OTHER")


def test_row_cap_is_clamped_to_the_hard_ceiling():
    s = settings(max_rows=10, max_rows_limit=50)
    assert s.clamp_rows(None) == 10
    assert s.clamp_rows(9999) == 50


def test_the_password_stays_out_of_the_description():
    s = settings(pwd="hunter2")
    assert "hunter2" not in s.describe_target()
    assert "PWD=hunter2" in s.connection_string()


def test_schema_sql_filter_excludes_the_denylist():
    predicate, params = settings().schema_sql_filter("TABSCHEMA")
    assert predicate.count("NOT LIKE") == 3
    assert params == ["SYS%", "NULLID", "SQLJ"]


def test_schema_sql_filter_requires_the_allowlist():
    s = settings(schema_allowlist="app*,ch_*", schema_denylist="")
    predicate, params = s.schema_sql_filter("S")
    assert predicate == "(RTRIM(S) LIKE ? OR RTRIM(S) LIKE ?)"
    assert params == ["APP%", "CH_%"]
