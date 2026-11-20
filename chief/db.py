import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .migrations import MIGRATIONS


def connect(path: Path | None = None) -> sqlite3.Connection:
    # Autocommit mode; multi-statement writes use explicit BEGIN/COMMIT via `transaction`.
    conn = sqlite3.connect(path or paths.db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version() -> int:
    return MIGRATIONS[-1].version


def backup(conn: sqlite3.Connection, label: str) -> Path:
    paths.backups_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = paths.backups_dir() / f"cos-{label}-{stamp}.db"
    with sqlite3.connect(dest) as out:
        conn.backup(out)
    return dest


def migrate(conn: sqlite3.Connection) -> tuple[list[int], Path | None]:
    current = schema_version(conn)
    if current > latest_version():
        raise RuntimeError(f"database is at v{current}, newer than this code (v{latest_version()})")
    pending = [m for m in MIGRATIONS if m.version > current]
    if not pending:
        return [], None
    backup_path = backup(conn, f"v{current}") if current > 0 else None
    applied = []
    for m in pending:
        # PRAGMA foreign_keys is a no-op inside a transaction, so it is switched before BEGIN.
        if m.foreign_keys_off:
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # user_version is transactional in SQLite, so a failed migration leaves the version untouched.
            with transaction(conn):
                for stmt in m.statements:
                    conn.execute(stmt)
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(f"migration v{m.version} would break {len(broken)} foreign keys: {broken[:3]}")
                conn.execute(f"PRAGMA user_version = {m.version}")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        applied.append(m.version)
    return applied, backup_path


def open_db() -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = connect()
    if schema_version(conn) != latest_version():
        raise SystemExit(f"schema is v{schema_version(conn)}, expected v{latest_version()} — run `cos.py migrate`")
    return conn
