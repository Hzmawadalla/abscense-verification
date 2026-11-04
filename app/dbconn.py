"""Opening the app's Postgres connection with bounded waits.

Without `connect_timeout`, libpq waits indefinitely when the database (e.g. a paused or still
resuming Supabase project) accepts the TCP connection but never answers. Every new visitor's first
script run reads the HRBP login list, so one stalled connect turned into an endless spinner for
everyone. TCP keepalives let a half-open socket (Supabase drops idle connections) fail fast too,
so the `select 1` revive in streamlit_app.conn() errors instead of hanging."""

CONNECT_TIMEOUT_S = 10

CONNECT_KWARGS = {
    "autocommit": True,          # never leave an idle-in-transaction session (see get_conn)
    "prepare_threshold": None,   # the Supabase pooler does not support prepared statements
    "connect_timeout": CONNECT_TIMEOUT_S,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

UNAVAILABLE_MESSAGE = ("The database is not responding right now. Please wait a minute and "
                       "refresh the page.")


def open_connection(dsn, connect=None):
    """Return a new connection; raises psycopg.OperationalError if the database does not answer
    within CONNECT_TIMEOUT_S. `connect` is injectable for tests."""
    if connect is None:
        import psycopg
        connect = psycopg.connect
    return connect(dsn, **CONNECT_KWARGS)
