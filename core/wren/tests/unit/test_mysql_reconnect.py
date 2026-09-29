"""Unit tests for MySQL connector liveness handling.

A connector outlives a single request -- the MCP server holds one for the
lifetime of the process -- so it must survive MySQL closing an idle connection
after ``wait_timeout`` (28800 s by default). These tests drive
``_ensure_connection`` with a fake driver so they need no live server and run
in the default unit job.
"""

from __future__ import annotations

import MySQLdb
import pytest

from wren.connector.mysql import DorisConnector, MySqlConnector
from wren.model.error import ErrorCode, WrenError

pytestmark = pytest.mark.unit


class _FakeCursor:
    # ``_build_mysql_arrow_table`` reads ``description`` to build the schema;
    # one nullable column is enough for these tests.
    description = (("x", None, None, None, None, None, None),)

    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn

    def execute(self, sql: str) -> None:
        self._conn.executed.append(sql)

    def fetchall(self):
        return ()

    def close(self) -> None:
        pass


class _FakeConnection:
    """Minimal stand-in for a MySQLdb connection.

    ``alive=False`` reproduces a connection the server has dropped: ``ping()``
    raises, exactly as mysqlclient does with error 2006.
    """

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.executed: list[str] = []
        self.closed = False
        self.pings = 0

    def ping(self) -> None:
        self.pings += 1
        if not self.alive:
            raise Exception("(2006, 'MySQL server has gone away')")

    def cursor(self):
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def _make_connector(monkeypatch, connections: list[_FakeConnection]) -> MySqlConnector:
    """Build a connector whose ``_open`` hands out ``connections`` in order."""
    handed_out = iter(connections)
    monkeypatch.setattr(
        MySqlConnector, "_open", lambda self: next(handed_out), raising=True
    )
    connector = MySqlConnector.__new__(MySqlConnector)
    connector._closed = False
    connector._connect_kwargs = {"host": "127.0.0.1"}
    connector.connection = connector._open()
    return connector


def test_healthy_connection_is_reused(monkeypatch) -> None:
    live = _FakeConnection(alive=True)
    connector = _make_connector(monkeypatch, [live])

    connector._ensure_connection()

    assert connector.connection is live
    assert live.pings == 1
    assert live.closed is False


def test_stale_connection_is_replaced(monkeypatch) -> None:
    stale = _FakeConnection(alive=False)
    fresh = _FakeConnection(alive=True)
    connector = _make_connector(monkeypatch, [stale, fresh])

    connector._ensure_connection()

    assert connector.connection is fresh, "a dead connection must be replaced"
    assert stale.closed is True, "the dead handle must be released"


def test_query_survives_a_dropped_connection(monkeypatch) -> None:
    """The regression this fixes: every query after ``wait_timeout`` failed."""
    stale = _FakeConnection(alive=False)
    fresh = _FakeConnection(alive=True)
    connector = _make_connector(monkeypatch, [stale, fresh])

    connector.query("SELECT 1")

    assert fresh.executed == ["SELECT 1"], "query must run on the new connection"
    assert stale.executed == [], "nothing may be sent to the dead connection"


def test_dry_run_survives_a_dropped_connection(monkeypatch) -> None:
    stale = _FakeConnection(alive=False)
    fresh = _FakeConnection(alive=True)
    connector = _make_connector(monkeypatch, [stale, fresh])

    connector.dry_run("SELECT 1")

    assert fresh.executed == ["EXPLAIN SELECT 1"]


def test_statement_is_never_sent_twice(monkeypatch) -> None:
    """Liveness is checked BEFORE executing, so no statement can be re-sent.

    Retrying after a failure would risk applying a write twice; pinging first
    means a statement reaches the server at most once.
    """
    live = _FakeConnection(alive=True)
    connector = _make_connector(monkeypatch, [live])

    connector.query("INSERT INTO t VALUES (1)")

    assert live.executed == ["INSERT INTO t VALUES (1)"]


def test_closed_connector_refuses_to_reopen(monkeypatch) -> None:
    """``close()`` is explicit intent -- it must not be undone by a ping."""
    live = _FakeConnection(alive=True)
    connector = _make_connector(monkeypatch, [live])
    connector.close()

    with pytest.raises(WrenError) as excinfo:
        connector._ensure_connection()

    assert excinfo.value.error_code == ErrorCode.GET_CONNECTION_ERROR


def test_reconnect_reapplies_session_init(monkeypatch) -> None:
    """A new connection must get ANSI_QUOTES again, or quoted identifiers break.

    This is why the driver's own ``ping(reconnect=True)`` is not used: it would
    hand back a session without the init statement.
    """
    opened: list[_FakeConnection] = []

    def fake_connect(**_kwargs):
        conn = _FakeConnection(alive=True)
        opened.append(conn)
        return conn

    monkeypatch.setattr(MySQLdb, "connect", fake_connect, raising=True)

    connector = MySqlConnector.__new__(MySqlConnector)
    connector._closed = False
    connector._connect_kwargs = {"host": "127.0.0.1"}
    connector.connection = connector._open()

    assert opened[0].executed == ["SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')"]

    connector.connection.alive = False
    connector._ensure_connection()

    assert len(opened) == 2, "a second connection must have been opened"
    assert opened[1].executed == ["SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')"]


def test_doris_reconnect_skips_ansi_quotes(monkeypatch) -> None:
    """Doris rejects the ANSI_QUOTES statement, so its init must stay empty."""
    opened: list[_FakeConnection] = []

    def fake_connect(**_kwargs):
        conn = _FakeConnection(alive=True)
        opened.append(conn)
        return conn

    monkeypatch.setattr(MySQLdb, "connect", fake_connect, raising=True)

    connector = DorisConnector.__new__(DorisConnector)
    connector._closed = False
    connector._connect_kwargs = {"host": "127.0.0.1"}
    connector.connection = connector._open()

    connector.connection.alive = False
    connector._ensure_connection()

    assert len(opened) == 2
    assert all(conn.executed == [] for conn in opened)
