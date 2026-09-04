"""
SFD persistence layer — sfd.db
Single source of truth for all SFD state.
PATCHED: WAL mode + busy timeout + synchronous=NORMAL
  to eliminate 'database is locked' errors from concurrent
  FastAPI endpoint threads all writing simultaneously.
INTEGRATED: OI Anchor tables (options_chain + anchor_meta)
"""
import sqlite3
import json
import hashlib
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "sfd.db"

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

CREATE TABLE IF NOT EXISTS flow_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    spot REAL,
    contracts_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flow_asset_ts ON flow_snapshots(asset, ts);

CREATE TABLE IF NOT EXISTS wall_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    wall_type TEXT NOT NULL,
    old_level REAL,
    new_level REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wm_asset ON wall_migrations(asset, ts);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    context TEXT NOT NULL,
    wall TEXT,
    decision TEXT,
    confidence REAL,
    regime TEXT,
    spot REAL, put_wall REAL, call_wall REAL,
    flip REAL, net_gex REAL,
    slope REAL, vol_ratio REAL,
    flow_conviction REAL, flow_sweep INTEGER,
    direction TEXT,
    entry REAL, stop REAL, target_1 REAL, target_2 REAL,
    stop_dist REAL, t1_dist REAL,
    rationale TEXT,
    outcome TEXT,
    realized_r REAL,
    graded_ts REAL,
    prev_hash TEXT,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_asset_ts ON verdicts(asset, ts);

CREATE TABLE IF NOT EXISTS options_chain (
    root TEXT NOT NULL,
    anchor_date TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    ctype TEXT NOT NULL,
    oi REAL,
    volume REAL,
    iv REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    PRIMARY KEY (root, anchor_date, expiry, strike, ctype)
);

CREATE TABLE IF NOT EXISTS anchor_meta (
    root TEXT PRIMARY KEY,
    anchor_date TEXT,
    fetched_at REAL,
    source TEXT,
    n_rows INTEGER,
    spot_at_fetch REAL
);
"""


def con() -> sqlite3.Connection:
    """Connection factory with WAL mode + busy timeout.

    WAL (Write-Ahead Logging) allows concurrent readers + 1 writer
    without blocking. busy_timeout makes writers wait up to 30s
    for the lock instead of failing instantly. This eliminates the
    'database is locked' errors caused by FastAPI running each sync
    endpoint in its own thread and all of them calling opra.get_chain()
    → store.save_chain() simultaneously on a cold cache.
    """
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init():
    """Create all tables if they don't exist. Idempotent."""
    with con() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")


# ── options chains ────────────────────────────────────────────────────────────

def save_chain(snap):
    """Persist a full options chain snapshot + metadata."""
    rows = [q.to_row(snap.as_of) for q in snap.contracts]
    with con() as c:
        c.executemany(
            """INSERT OR REPLACE INTO options_quotes
               (ts, symbol, expiry, strike, type, bid, ask, last,
                volume, open_interest, iv, delta, gamma, theta, vega,
                source, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows
        )
        c.execute(
            """INSERT OR REPLACE INTO chain_meta
               (ts, symbol, spot, source, provenance, n_contracts, quality_json)
               VALUES (?,?,?,?,?,?,?)""",
            (snap.as_of, snap.symbol, snap.spot, snap.source,
             snap.provenance, len(snap.contracts),
             json.dumps(snap.quality, default=str))
        )


def latest_chain_ts(symbol: str):
    """Return the most recent timestamp we have for a symbol."""
    row = con().execute(
        "SELECT MAX(ts) AS d FROM options_quotes WHERE symbol=?",
        (symbol,)
    ).fetchone()
    return row["d"] if row else None


def load_chain_asof(symbol: str, upto: str):
    """Point-in-time slice — critical for backtests (no lookahead)."""
    c = con()
    rows = c.execute(
        """SELECT * FROM options_quotes
           WHERE symbol=? AND ts <= ? ORDER BY ts""",
        (symbol, upto)
    ).fetchall()
    return [dict(r) for r in rows]


# ── flow snapshots ────────────────────────────────────────────────────────────

def save_flow_snapshot(asset: str, snap: dict):
    """Persist a volume snapshot for delta computation.

    snap = {"ts": float, "spot": float, "contracts": {key: {...}}}
    """
    with con() as c:
        c.execute(
            """INSERT INTO flow_snapshots (ts, asset, spot, contracts_json)
               VALUES (?,?,?,?)""",
            (snap["ts"], asset, snap.get("spot"),
             json.dumps(snap["contracts"], default=str))
        )


def latest_flow_snapshot(asset: str, before_ts: float):
    """Get the most recent snapshot strictly before before_ts."""
    row = con().execute(
        """SELECT ts, spot, contracts_json FROM flow_snapshots
           WHERE asset=? AND ts < ? ORDER BY ts DESC LIMIT 1""",
        (asset, before_ts)
    ).fetchone()
    if not row:
        return None
    return {
        "ts": row["ts"],
        "spot": row["spot"],
        "contracts": json.loads(row["contracts_json"])
    }


# ── wall migrations ──────────────────────────────────────────────────────────

def log_wall_migration(asset: str, wall_type: str,
                       old_level: float, new_level: float):
    """Record a wall level change for audit / chart overlay."""
    with con() as c:
        c.execute(
            """INSERT INTO wall_migrations (ts, asset, wall_type,
                                            old_level, new_level)
               VALUES (?,?,?,?,?)""",
            (time.time(), asset, wall_type, old_level, new_level)
        )


def last_wall_migration(asset: str, wall_type: str):
    """Return the most recent migration for a wall, or None."""
    row = con().execute(
        """SELECT * FROM wall_migrations
           WHERE asset=? AND wall_type=?
           ORDER BY ts DESC LIMIT 1""",
        (asset, wall_type)
    ).fetchone()
    return dict(row) if row else None


# ── verdicts (hash-chained, append-only) ─────────────────────────────────────

def save_verdict(rec: dict):
    """Append a verdict with hash-chain integrity.

    Hash-chaining means you can later prove no verdict was
    retroactively edited — cheap integrity for a system whose
    whole credibility depends on 'did it really say that?'
    """
    prev_row = con().execute(
        "SELECT hash FROM verdicts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = prev_row["hash"] if prev_row else "GENESIS"

    entry = {
        "ts": time.time(),
        "asset": rec.get("asset"),
        "context": rec.get("context"),
        "wall": rec.get("wall"),
        "decision": rec.get("decision"),
        "confidence": rec.get("confidence"),
        "regime": rec.get("regime"),
        "spot": rec.get("spot"),
        "put_wall": rec.get("put_wall"),
        "call_wall": rec.get("call_wall"),
        "flip": rec.get("flip"),
        "net_gex": rec.get("net_gex"),
        "slope": rec.get("slope"),
        "vol_ratio": rec.get("vol_ratio"),
        "flow_conviction": rec.get("flow_conviction"),
        "flow_sweep": rec.get("flow_sweep"),
        "direction": rec.get("direction"),
        "entry": rec.get("entry"),
        "stop": rec.get("stop"),
        "target_1": rec.get("target_1"),
        "target_2": rec.get("target_2"),
        "stop_dist": rec.get("stop_dist"),
        "t1_dist": rec.get("t1_dist"),
        "rationale": rec.get("rationale"),
        "prev": prev_hash
    }

    h = hashlib.sha256(
        json.dumps(entry, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    with con() as c:
        c.execute(
            """INSERT INTO verdicts
               (ts, asset, context, wall, decision, confidence,
                regime, spot, put_wall, call_wall, flip, net_gex,
                slope, vol_ratio, flow_conviction, flow_sweep,
                direction, entry, stop, target_1, target_2,
                stop_dist, t1_dist, rationale, prev_hash, hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry["ts"], entry["asset"], entry["context"],
             entry["wall"], entry["decision"], entry["confidence"],
             entry["regime"], entry["spot"], entry["put_wall"],
             entry["call_wall"], entry["flip"], entry["net_gex"],
             entry["slope"], entry["vol_ratio"],
             entry["flow_conviction"], entry["flow_sweep"],
             entry["direction"], entry["entry"], entry["stop"],
             entry["target_1"], entry["target_2"],
             entry["stop_dist"], entry["t1_dist"],
             entry["rationale"], prev_hash, h)
        )
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def load_ungraded_verdicts():
    """All verdicts that haven't been graded yet."""
    rows = con().execute(
        "SELECT * FROM verdicts WHERE outcome IS NULL ORDER BY ts"
    ).fetchall()
    return [dict(r) for r in rows]


def load_graded_verdicts():
    """All verdicts that have been graded."""
    rows = con().execute(
        "SELECT * FROM verdicts WHERE outcome IS NOT NULL ORDER BY ts"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_graded(vid: int, outcome: str, realized_r: float):
    """Fill in the outcome for a graded verdict."""
    with con() as c:
        c.execute(
            """UPDATE verdicts
               SET outcome=?, realized_r=?, graded_ts=?
               WHERE id=?""",
            (outcome, realized_r, time.time(), vid)
        )


def verify_verdict_chain() -> bool:
    """Walk the hash chain and confirm no verdict was tampered with."""
    rows = con().execute(
        "SELECT * FROM verdicts ORDER BY id"
    ).fetchall()
    prev = "GENESIS"
    for r in rows:
        entry = {
            "ts": r["ts"],
            "asset": r["asset"],
            "context": r["context"],
            "wall": r["wall"],
            "decision": r["decision"],
            "confidence": r["confidence"],
            "regime": r["regime"],
            "spot": r["spot"],
            "put_wall": r["put_wall"],
            "call_wall": r["call_wall"],
            "flip": r["flip"],
            "net_gex": r["net_gex"],
            "slope": r["slope"],
            "vol_ratio": r["vol_ratio"],
            "flow_conviction": r["flow_conviction"],
            "flow_sweep": r["flow_sweep"],
            "direction": r["direction"],
            "entry": r["entry"],
            "stop": r["stop"],
            "target_1": r["target_1"],
            "target_2": r["target_2"],
            "stop_dist": r["stop_dist"],
            "t1_dist": r["t1_dist"],
            "rationale": r["rationale"],
            "prev": prev
        }
        expect = hashlib.sha256(
            json.dumps(entry, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        if expect != r["hash"]:
            return False
        prev = r["hash"]
    return True