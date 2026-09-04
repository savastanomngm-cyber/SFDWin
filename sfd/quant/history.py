"""
SFD GEX history layer (gammagrid-style).
- gex_snapshots: periodic captures of spot/walls/flip/netGEX/basis
- wall_migrations: immutable log of wall moves (≥1.0 pt)
Own SQLite file so the main store schema stays untouched.
"""
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "sfd_history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gex_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, asset TEXT NOT NULL,
  spot REAL, put_wall REAL, call_wall REAL, flip REAL,
  net_gex REAL, regime TEXT, basis REAL, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_asset_ts ON gex_snapshots(asset, ts);
CREATE TABLE IF NOT EXISTS wall_migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, asset TEXT NOT NULL, wall TEXT NOT NULL,
  old REAL, new REAL, delta REAL
);
"""


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with con() as c:
        c.executescript(SCHEMA)


def snapshot_and_diff(asset, gex, basis=None, source="chain"):
    """Record a snapshot; log migrations vs the previous snapshot."""
    init()
    ts = time.time()
    pw, cw = gex.get("put_wall"), gex.get("call_wall")
    with con() as c:
        last = c.execute(
            "SELECT put_wall, call_wall FROM gex_snapshots "
            "WHERE asset=? ORDER BY ts DESC LIMIT 1", (asset,)).fetchone()
        c.execute("""INSERT INTO gex_snapshots
            (ts, asset, spot, put_wall, call_wall, flip, net_gex, regime, basis, source)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (ts, asset, gex.get("spot"), pw, cw, gex.get("flip_point"),
                   gex.get("net_gex"), gex.get("regime"), basis, source))
    migs = []
    if last:
        for wall, old, new in (("PUT", last["put_wall"], pw),
                               ("CALL", last["call_wall"], cw)):
            if old is not None and new is not None and abs(new - old) >= 1.0:
                with con() as c:
                    c.execute("""INSERT INTO wall_migrations
                        (ts, asset, wall, old, new, delta) VALUES (?,?,?,?,?,?)""",
                              (ts, asset, wall, old, new, new - old))
                migs.append({"wall": wall, "old": old, "new": new})
    return migs


def snapshots(asset, limit=400):
    init()
    rows = con().execute(
        "SELECT * FROM gex_snapshots WHERE asset=? ORDER BY ts DESC LIMIT ?",
        (asset, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def migrations(asset, limit=50):
    init()
    rows = con().execute(
        "SELECT * FROM wall_migrations WHERE asset=? ORDER BY ts DESC LIMIT ?",
        (asset, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]