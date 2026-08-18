"""SQLite persistence for nearby strokes."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from .source import Stroke

SCHEMA = """
CREATE TABLE IF NOT EXISTS strokes (
    src          INTEGER NOT NULL,
    stroke_id    INTEGER NOT NULL,
    time_ms      INTEGER NOT NULL,   -- whole ms since epoch, UTC
    lat          REAL    NOT NULL,
    lon          REAL    NOT NULL,
    dev_m        REAL,
    delay_ms     INTEGER,
    alt_m        REAL,
    server       INTEGER,
    distance_km  REAL    NOT NULL,
    bearing_deg  REAL    NOT NULL,
    received_at  REAL    NOT NULL,
    PRIMARY KEY (src, stroke_id, time_ms)
);

CREATE INDEX IF NOT EXISTS idx_strokes_time ON strokes (time_ms);
CREATE INDEX IF NOT EXISTS idx_strokes_distance ON strokes (distance_km);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Columns of the ``strokes`` table, in the order rows are returned.
COLUMNS = (
    "src", "stroke_id", "time_ms", "lat", "lon", "dev_m", "delay_ms",
    "alt_m", "server", "distance_km", "bearing_deg", "received_at",
)


class Store:
    """Thin SQLite wrapper. Safe to use as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def add_strokes(self, rows: Iterable[tuple]) -> int:
        """Insert rows, ignoring ones already stored.

        Returns the number of genuinely new rows, which is what the caller
        should count -- the feed re-sends strokes across cursor boundaries.
        """
        rows = list(rows)
        if not rows:
            return 0
        placeholders = ", ".join("?" * len(COLUMNS))
        before = self.conn.total_changes
        self.conn.executemany(
            f"INSERT OR IGNORE INTO strokes ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    @staticmethod
    def row_for(stroke: Stroke, distance_km: float, bearing_deg: float) -> tuple:
        return (
            stroke.src,
            stroke.stroke_id,
            int(round(stroke.time_utc * 1000)),
            stroke.lat,
            stroke.lon,
            stroke.dev_m,
            stroke.delay_ms,
            stroke.alt_m,
            stroke.server,
            distance_km,
            bearing_deg,
            time.time(),
        )

    def strokes_since(self, since_utc: float, limit: int | None = None) -> list[sqlite3.Row]:
        sql = f"SELECT {', '.join(COLUMNS)} FROM strokes WHERE time_ms >= ? ORDER BY time_ms"
        params: list = [int(since_utc * 1000)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.conn.execute(sql, params))

    def count_since(self, since_utc: float) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM strokes WHERE time_ms >= ?", (int(since_utc * 1000),)
        )
        return int(cur.fetchone()[0])

    def nearest_since(self, since_utc: float) -> sqlite3.Row | None:
        cur = self.conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM strokes WHERE time_ms >= ? "
            "ORDER BY distance_km LIMIT 1",
            (int(since_utc * 1000),),
        )
        return cur.fetchone()

    def total(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM strokes").fetchone()[0])

    def time_range(self) -> tuple[float | None, float | None]:
        row = self.conn.execute("SELECT MIN(time_ms), MAX(time_ms) FROM strokes").fetchone()
        lo, hi = row[0], row[1]
        return (lo / 1000.0 if lo else None, hi / 1000.0 if hi else None)

    def prune(self, older_than_utc: float) -> int:
        before = self.conn.total_changes
        self.conn.execute("DELETE FROM strokes WHERE time_ms < ?", (int(older_than_utc * 1000),))
        self.conn.commit()
        return self.conn.total_changes - before

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
