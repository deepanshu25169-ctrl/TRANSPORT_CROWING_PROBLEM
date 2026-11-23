"""SQLite schema and shared database helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stops (
  idx INTEGER PRIMARY KEY, stop_id TEXT NOT NULL, stop_name TEXT NOT NULL,
  lat REAL, lon REAL
);
CREATE TABLE IF NOT EXISTS routes (
  route_id TEXT PRIMARY KEY, name TEXT NOT NULL, agency TEXT,
  description TEXT, direction TEXT
);
CREATE TABLE IF NOT EXISTS patterns (
  pattern_id TEXT PRIMARY KEY, route_name TEXT, direction TEXT,
  stop_indices TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
  trip_id TEXT PRIMARY KEY, route_name TEXT NOT NULL, pattern_id TEXT NOT NULL,
  dep_secs TEXT NOT NULL, arr_secs TEXT NOT NULL, first_dep INTEGER,
  last_arr INTEGER, headsign TEXT, service_id TEXT, direction TEXT
);
CREATE TABLE IF NOT EXISTS service_calendar (
  service_id TEXT PRIMARY KEY, flags TEXT, start_date TEXT, end_date TEXT
);
CREATE TABLE IF NOT EXISTS ticket_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL, pax INTEGER NOT NULL, vehicle TEXT,
  route_name TEXT, pattern_id TEXT, trip_id TEXT, run_key TEXT,
  board_idx INTEGER, alight_idx INTEGER, board_pos INTEGER, alight_pos INTEGER,
  ticket_sec INTEGER, match_start TEXT, match_end TEXT, match_route TEXT,
  is_ac INTEGER, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ticket_date ON ticket_events(date);
CREATE INDEX IF NOT EXISTS ix_ticket_run ON ticket_events(run_key);
CREATE INDEX IF NOT EXISTS ix_ticket_route ON ticket_events(route_name);
CREATE TABLE IF NOT EXISTS run_info (
  run_key TEXT PRIMARY KEY, date TEXT NOT NULL, vehicle TEXT, route_name TEXT,
  pattern_id TEXT, trip_id TEXT, n_tickets INTEGER, total_pax INTEGER,
  first_ticket_sec INTEGER, split_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_date ON run_info(date);
CREATE INDEX IF NOT EXISTS ix_run_pattern ON run_info(pattern_id);
CREATE TABLE IF NOT EXISTS run_loads (
  run_key TEXT PRIMARY KEY, loads TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_params (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_profiles (
  profile_key TEXT PRIMARY KEY, route_name TEXT, pattern_id TEXT,
  direction TEXT, day_type TEXT, time_bin INTEGER, n_runs INTEGER,
  n_days INTEGER, median_load REAL, p75_load REAL, p90_load REAL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def put_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
    conn.commit()


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_audit(conn, counts: dict) -> None:
    conn.executemany("INSERT OR REPLACE INTO audit VALUES (?,?)",
                     [(str(k), str(v)) for k, v in counts.items()])
    conn.commit()
