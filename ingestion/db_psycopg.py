"""Production DB adapter: wraps a psycopg connection to satisfy loader.DB.

Kept separate so the loader orchestration stays import-free of psycopg (and unit-testable with a
fake). Install psycopg to use this: `pip install "psycopg[binary]"`."""


class PsycopgDB:
    def __init__(self, conn):
        self.conn = conn

    def one(self, sql, params):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def many(self, sql, rows):
        rows = list(rows)
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)


def connect(dsn):
    import psycopg
    return psycopg.connect(dsn)
