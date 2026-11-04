"""The app's DB connection must have a bounded connect wait, or a slow database leaves every new
visitor on an endless spinner (2026-11-04 incident)."""
import pytest

from app.dbconn import CONNECT_KWARGS, CONNECT_TIMEOUT_S, open_connection


def test_connect_is_called_with_a_timeout_and_keepalives():
    calls = []

    def fake_connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return "conn"

    assert open_connection("postgresql://example", connect=fake_connect) == "conn"
    dsn, kwargs = calls[0]
    assert dsn == "postgresql://example"
    assert kwargs["connect_timeout"] == CONNECT_TIMEOUT_S
    assert 0 < CONNECT_TIMEOUT_S <= 15
    assert kwargs["keepalives"] == 1


def test_existing_connection_semantics_are_kept():
    # autocommit and no prepared statements were already required by the pooler setup.
    assert CONNECT_KWARGS["autocommit"] is True
    assert CONNECT_KWARGS["prepare_threshold"] is None


def test_connect_errors_propagate():
    class Boom(Exception):
        pass

    def failing(dsn, **kwargs):
        raise Boom("timeout expired")

    with pytest.raises(Boom):
        open_connection("postgresql://example", connect=failing)
