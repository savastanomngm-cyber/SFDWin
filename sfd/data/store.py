"""SFD options persistence — sfd.db, never touches saf.db."""
import sqlite3, json
from pathlib import Path

DB = Path(__file__).resolve().parent.parent.parent / "sfd.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_quotes (
    ts TEXT NOT NULL, symbol TEXT NOT NULL,
    expiry TEXT NOT NULL, strike REAL NOT NULL, type TEXT NOT NULL,
    bid REAL, ask REAL, last REAL, volume INTEGER, open_interest INTEGER,
    iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
    source TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('LIVE','DELAYED','CACHED','EST')),
    PRIMARY KEY (symbol, expiry, strike, type, ts)
);
CREATE INDEX IF NOT EXISTS idx_oq_lookup ON options_quotes(symbol, ts);

CREATE TABLE IF NOT EXISTS chain_meta (
    ts TEXT NOT NULL, symbol TEXT NOT NULL, spot REAL,
    source TEXT, provenance TEXT, n_contracts INTEGER, quality_json TEXT,
    PRIMARY KEY (symbol, ts)
);
"""

def con():
    """WAL mode + busy timeout to prevent 'database is locked' errors
    when server threads and CLI hit the cache simultaneously."""
    c = sqlite3.connect(DB, timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA busy_timeout=30000;")
    c.execute("PRAGMA synchronous=NORMAL;")
    return c

def init():
    with con() as c:
        c.executescript(SCHEMA)

def save_chain(snap):
    with con() as c:
        c.executemany(
            """INSERT OR REPLACE INTO options_quotes
               (ts,symbol,expiry,strike,type,bid,ask,last,volume,open_interest,
                iv,delta,gamma,theta,vega,source,provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [q.to_row(snap.as_of) for q in snap.contracts])
        c.execute(
            """INSERT OR REPLACE INTO chain_meta
               (ts,symbol,spot,source,provenance,n_contracts,quality_json)
               VALUES (?,?,?,?,?,?,?)""",
            (snap.as_of, snap.symbol, snap.spot, snap.source, snap.provenance,
             len(snap.contracts), json.dumps(snap.quality, default=str)))

def latest_chain_ts(symbol):
    row = con().execute(
        "SELECT MAX(ts) AS t FROM options_quotes WHERE symbol=?", (symbol,)).fetchone()
    # Safely handle empty DB to prevent NoneType crashes in opra.py
    return row["t"] if row and row["t"] else None

def load_chain_asof(symbol, ts):
    """Point-in-time slice for backtesting the walls strategy (no lookahead)."""
    rows = con().execute(
        """SELECT * FROM options_quotes WHERE symbol=? AND ts<=? ORDER BY ts DESC""",
        (symbol, ts)).fetchall()
    # keep only the newest ts per contract
    latest_ts = rows[0]["ts"] if rows else None
    return [r for r in rows if r["ts"] == latest_ts]