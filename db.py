import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "driving_scheduler.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

DEFAULT_WORK_HOURS = {
    "0": {"enabled": True, "start": "08:00", "end": "19:00"},   # Sunday
    "1": {"enabled": True, "start": "08:00", "end": "19:00"},   # Monday
    "2": {"enabled": True, "start": "08:00", "end": "19:00"},   # Tuesday
    "3": {"enabled": True, "start": "08:00", "end": "19:00"},   # Wednesday
    "4": {"enabled": True, "start": "08:00", "end": "19:00"},   # Thursday
    "5": {"enabled": True, "start": "08:00", "end": "13:00"},   # Friday
}

DEFAULT_AREAS = ["רמלה", "נס ציונה", "באר יעקב"]

_TABLES_WITH_ID = ("students", "instructor_blocks", "scheduled_lessons")


class _PgCursorWrapper:
    def __init__(self, cur: Any, lastrowid: int | None = None) -> None:
        self._cur = cur
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()


class _PgConnWrapper:
    """Thin adapter so existing SQLite-style queries work seamlessly on PostgreSQL."""

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn

    def __enter__(self) -> "_PgConnWrapper":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> _PgCursorWrapper:
        q = sql.strip()
        is_ignore = False
        if re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO\s+", q, re.IGNORECASE):
            q = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", q, flags=re.IGNORECASE)
            is_ignore = True

        q = q.replace("?", "%s")
        if is_ignore and "ON CONFLICT" not in q.upper():
            q = q.rstrip(";") + " ON CONFLICT DO NOTHING"

        need_id = False
        if not is_ignore and "ON CONFLICT" not in q.upper() and "RETURNING" not in q.upper():
            m = re.match(r"^INSERT\s+INTO\s+(\w+)", q, re.IGNORECASE)
            if m and m.group(1) in _TABLES_WITH_ID:
                q = q.rstrip(";") + " RETURNING id"
                need_id = True

        cur = self._conn.cursor()
        cur.execute(q, params)
        last_id = None
        if need_id:
            row = cur.fetchone()
            if row:
                last_id = row["id"]
        return _PgCursorWrapper(cur, lastrowid=last_id)


def get_conn() -> Any:
    if IS_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return _PgConnWrapper(raw)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    if IS_POSTGRES:
        ddl_statements = [
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                pin TEXT NOT NULL,
                area TEXT NOT NULL DEFAULT 'רמלה',
                address TEXT NOT NULL DEFAULT '',
                lat DOUBLE PRECISION DEFAULT NULL,
                lng DOUBLE PRECISION DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS weeks (
                week_start TEXT PRIMARY KEY,
                lock_override INTEGER DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'collecting' CHECK (status IN ('collecting', 'draft', 'approved'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS student_constraints (
                week_start TEXT NOT NULL,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                requested_lessons INTEGER NOT NULL DEFAULT 1,
                prefer_double INTEGER NOT NULL DEFAULT 0,
                blocked_slots TEXT NOT NULL DEFAULT '[]',
                submitted_at TEXT NOT NULL,
                PRIMARY KEY (week_start, student_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS instructor_blocks (
                id SERIAL PRIMARY KEY,
                week_start TEXT NOT NULL,
                day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 5),
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                block_type TEXT NOT NULL DEFAULT 'personal' CHECK (block_type IN ('test', 'personal', 'lunch')),
                title TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scheduled_lessons (
                id SERIAL PRIMARY KEY,
                week_start TEXT NOT NULL,
                day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 5),
                start_time TEXT NOT NULL,
                duration_min INTEGER NOT NULL DEFAULT 40,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                is_locked INTEGER NOT NULL DEFAULT 0,
                is_double INTEGER NOT NULL DEFAULT 0
            )
            """,
        ]
        with get_conn() as conn:
            for stmt in ddl_statements:
                conn.execute(stmt)
    else:
        with get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL UNIQUE,
                    pin TEXT NOT NULL,
                    area TEXT NOT NULL DEFAULT 'רמלה',
                    address TEXT NOT NULL DEFAULT '',
                    lat REAL DEFAULT NULL,
                    lng REAL DEFAULT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS weeks (
                    week_start TEXT PRIMARY KEY,
                    lock_override INTEGER DEFAULT NULL,
                    status TEXT NOT NULL DEFAULT 'collecting' CHECK (status IN ('collecting', 'draft', 'approved'))
                );

                CREATE TABLE IF NOT EXISTS student_constraints (
                    week_start TEXT NOT NULL,
                    student_id INTEGER NOT NULL,
                    requested_lessons INTEGER NOT NULL DEFAULT 1,
                    prefer_double INTEGER NOT NULL DEFAULT 0,
                    blocked_slots TEXT NOT NULL DEFAULT '[]',
                    submitted_at TEXT NOT NULL,
                    PRIMARY KEY (week_start, student_id),
                    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS instructor_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 5),
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    block_type TEXT NOT NULL DEFAULT 'personal' CHECK (block_type IN ('test', 'personal', 'lunch')),
                    title TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS scheduled_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 5),
                    start_time TEXT NOT NULL,
                    duration_min INTEGER NOT NULL DEFAULT 40,
                    student_id INTEGER NOT NULL,
                    is_locked INTEGER NOT NULL DEFAULT 0,
                    is_double INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
                );
                """
            )

            cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()}
            if "address" not in cols:
                conn.execute("ALTER TABLE students ADD COLUMN address TEXT NOT NULL DEFAULT ''")
            if "lat" not in cols:
                conn.execute("ALTER TABLE students ADD COLUMN lat REAL DEFAULT NULL")
            if "lng" not in cols:
                conn.execute("ALTER TABLE students ADD COLUMN lng REAL DEFAULT NULL")

    default_pin = os.environ.get("INSTRUCTOR_PIN", "1234").strip() or "1234"
    defaults = {
        "lesson_duration_min": "40",
        "lock_day": "4",
        "lock_time": "18:00",
        "lunch_mode": "flexible",
        "lunch_window_start": "12:30",
        "lunch_window_end": "15:00",
        "lunch_fixed_start": "13:00",
        "work_hours": json.dumps(DEFAULT_WORK_HOURS),
        "areas": json.dumps(DEFAULT_AREAS, ensure_ascii=False),
        "instructor_pin": default_pin,
    }
    with get_conn() as conn:
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )


def get_settings() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    raw = {r["key"]: r["value"] for r in rows}
    env_pin = os.environ.get("INSTRUCTOR_PIN", "").strip()
    return {
        "lesson_duration_min": int(raw.get("lesson_duration_min", 40)),
        "lock_day": int(raw.get("lock_day", 4)),
        "lock_time": raw.get("lock_time", "18:00"),
        "lunch_mode": raw.get("lunch_mode", "flexible"),
        "lunch_window_start": raw.get("lunch_window_start", "12:30"),
        "lunch_window_end": raw.get("lunch_window_end", "15:00"),
        "lunch_fixed_start": raw.get("lunch_fixed_start", "13:00"),
        "work_hours": json.loads(raw.get("work_hours", json.dumps(DEFAULT_WORK_HOURS))),
        "areas": json.loads(raw.get("areas", json.dumps(DEFAULT_AREAS, ensure_ascii=False))),
        "instructor_pin": env_pin or raw.get("instructor_pin", "1234"),
    }


def update_settings(updates: dict[str, Any]) -> dict[str, Any]:
    with get_conn() as conn:
        for k, v in updates.items():
            val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, val),
            )
    return get_settings()


def get_next_sunday(ref: date | None = None) -> str:
    """Return YYYY-MM-DD of the upcoming Sunday (the target week for scheduling)."""
    today = ref or date.today()
    days_ahead = (6 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).isoformat()


def ensure_week(week_start: str) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO weeks (week_start) VALUES (?)",
            (week_start,),
        )
        row = conn.execute("SELECT * FROM weeks WHERE week_start = ?", (week_start,)).fetchone()
    return dict(row)


def is_week_locked(week_start: str, now: datetime | None = None) -> bool:
    """Determine if student submissions are locked for the given target week."""
    week = ensure_week(week_start)
    if week["lock_override"] is not None:
        return bool(week["lock_override"])

    settings = get_settings()
    lock_day = settings["lock_day"]
    lock_hh, lock_mm = map(int, settings["lock_time"].split(":"))

    sunday = date.fromisoformat(week_start)
    prev_sunday = sunday - timedelta(days=7)
    lock_date = prev_sunday + timedelta(days=lock_day)
    lock_dt = datetime(lock_date.year, lock_date.month, lock_date.day, lock_hh, lock_mm)
    current = now or datetime.now()
    return current >= lock_dt
