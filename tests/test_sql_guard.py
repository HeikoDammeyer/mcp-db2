import pytest

from mcp_db2.sql_guard import SqlNotAllowed, ensure_read_only, strip_comments

ALLOWED = [
    "SELECT * FROM EMPLOYEE",
    "select firstnme from employee where workdept = ?",
    "WITH d AS (SELECT deptno FROM department) SELECT * FROM d",
    "VALUES CURRENT SCHEMA",
    "SELECT * FROM EMPLOYEE FETCH FIRST 10 ROWS ONLY WITH UR",
    "SELECT 'insert into t values(1)' AS literal FROM SYSIBM.SYSDUMMY1",
    'SELECT "DROP" FROM MY.TABLE1',
    "SELECT * FROM T -- drop table x\n WHERE ID = 1",
    "SELECT a, b /* update t set a=1 */ FROM T",
    "SELECT 'it''s fine; really' FROM SYSIBM.SYSDUMMY1",
]

REJECTED = [
    ("", "Empty"),
    ("   ", "Empty"),
    ("-- nothing but a comment", "comments"),
    ("SELECT 1 FROM SYSIBM.SYSDUMMY1; DROP TABLE T", "one statement"),
    ("DROP TABLE EMPLOYEE", "starts with DROP"),
    ("UPDATE EMPLOYEE SET SALARY = 1", "starts with UPDATE"),
    ("CALL SYSPROC.ADMIN_CMD('runstats on table x')", "starts with CALL"),
    ("SELECT * FROM FINAL TABLE (INSERT INTO T VALUES (1))", "INSERT"),
    ("SELECT * FROM OLD TABLE (DELETE FROM T)", "DELETE"),
    ("WITH x AS (SELECT * FROM NEW TABLE (UPDATE T SET A=1)) SELECT * FROM x", "UPDATE"),
    ("SELECT CURRENT SCHEMA, SET CURRENT PATH FROM T", "SET CURRENT"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert ensure_read_only(sql)


@pytest.mark.parametrize("sql,fragment", REJECTED)
def test_rejected(sql, fragment):
    with pytest.raises(SqlNotAllowed) as excinfo:
        ensure_read_only(sql)
    assert fragment.lower() in str(excinfo.value).lower()


def test_trailing_semicolon_is_fine():
    assert ensure_read_only("SELECT 1 FROM SYSIBM.SYSDUMMY1;") == "SELECT 1 FROM SYSIBM.SYSDUMMY1"


def test_comments_are_stripped_from_the_result():
    assert "secret" not in ensure_read_only("SELECT 1 /* secret */ FROM SYSIBM.SYSDUMMY1")


def test_semicolon_inside_a_literal_is_not_a_separator():
    assert ensure_read_only("SELECT ';' FROM SYSIBM.SYSDUMMY1")


def test_data_change_table_reference_is_caught_on_its_own():
    # No INSERT/UPDATE/DELETE keyword here — only the `FINAL TABLE` reference gives it away.
    with pytest.raises(SqlNotAllowed, match="data-change"):
        ensure_read_only("SELECT * FROM FINAL TABLE (MY_WRITING_FUNCTION())")


def test_unterminated_block_comment_swallows_the_rest():
    assert ensure_read_only("SELECT 1 FROM T /* drop table x") == "SELECT 1 FROM T"


def test_strip_comments_keeps_literals_intact():
    assert strip_comments("SELECT '-- not a comment' FROM T") == "SELECT '-- not a comment' FROM T"
