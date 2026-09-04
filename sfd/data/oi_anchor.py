"""
OI ANCHOR — stable intraday GEX walls (v4 data-integrity layer).
OPRA has NO intraday open interest: every vendor shows you EOD OI.
This module anchors OI once per day (CBOE full chain) into SQLite,
then walls are recomputed intraday from anchor-OI + LIVE spot only.
Result: raw walls move when the MATH says so (strike dominance flip
or daily OI refresh) — never when a feed hiccups.
PATCHED v2:
• Spot sanity: refuse to anchor on prints deviating >3% from last anchor
• Strike window: ±12% of spot, real IV (>0.5%), 0-45 DTE (kills LEAPS tail)
• Load-time re-filter: protects against already-poisoned rows
"""
import time
from datetime import datetime, date
from .. import store

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS options_chain (
  root TEXT NOT NULL,
  anchor_date TEXT NOT NULL,
  expiry TEXT NOT NULL,
  strike REAL NOT NULL,
  ctype TEXT NOT NULL,
  oi REAL, volume REAL, iv REAL,
  delta REAL, gamma REAL, theta REAL, vega REAL,
  PRIMARY KEY (root, anchor_date, expiry, strike, ctype)
);
CREATE TABLE IF NOT EXISTS anchor_meta (
  root TEXT PRIMARY KEY,
  anchor_date TEXT, fetched_at REAL, source TEXT,
  n_rows INTEGER, spot_at_fetch REAL
);
"""

def _ensure():
    with store.con() as c:
        c.executescript(TABLE_SQL)

def _parse_expiry_dt(exp):
    try:
        f = float(exp)
        if f > 1e12: f /= 1000.0
        if f > 1e9:
            return datetime.fromtimestamp(f).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try: return datetime.strptime(str(exp)[:10], fmt).date()
        except Exception: continue
    return None

def refresh_anchor(root, force=False, source="cboe"):
    """Fetch full CBOE chain, store OI snapshot. Runs once per day."""
    _ensure()
    today = date.today().isoformat()
    con = store.con()
    row = con.execute("SELECT anchor_date FROM anchor_meta WHERE root=?",
                      (root,)).fetchone()
    if row and row["anchor_date"] == today and not force:
        return True
    from . import opra
    chain = opra.get_chain(root)
    if chain is None or not getattr(chain, "contracts", None):
        return False
    # 🟢 SPOT SANITY: refuse to anchor on a bad overnight print
    spot0 = float(getattr(chain, "spot", 0) or 0)
    prev = con.execute("SELECT spot_at_fetch FROM anchor_meta WHERE root=?",
                       (root,)).fetchone()
    if spot0 <= 0:
        return False
    if prev and prev["spot_at_fetch"]:
        if abs(spot0 - prev["spot_at_fetch"]) / prev["spot_at_fetch"] > 0.03:
            print(f"[oi_anchor] {root}: spot {spot0} deviates >3% from last anchor "
                  f"{prev['spot_at_fetch']} — keeping previous anchor")
            return False
    # 🟢 STRIKE WINDOW: only ±12% of spot, real IV, 0-45 DTE (kills LEAPS tail)
    lo, hi = spot0 * 0.88, spot0 * 1.12
    rows = []
    for c in chain.contracts:
        oi = getattr(c, "open_interest", 0) or 0
        iv = getattr(c, "iv", 0) or 0
        dte = getattr(c, "dte", None)
        if oi <= 0 or iv <= 0.005:
            continue
        if dte is None or dte < 0 or dte > 45:
            continue
        if not (lo <= float(c.strike) <= hi):
            continue
        rows.append((root, today, str(c.expiry), float(c.strike),
                     c.contract_type, float(oi),
                     float(getattr(c, "volume", 0) or 0),
                     float(iv),
                     float(getattr(c, "delta", 0) or 0),
                     float(getattr(c, "gamma", 0) or 0),
                     float(getattr(c, "theta", 0) or 0),
                     float(getattr(c, "vega", 0) or 0)))
    if not rows:
        return False
    with store.con() as c:
        c.execute("DELETE FROM options_chain WHERE root=? AND anchor_date=?",
                  (root, today))
        c.executemany("""INSERT OR REPLACE INTO options_chain
            (root,anchor_date,expiry,strike,ctype,oi,volume,iv,
             delta,gamma,theta,vega) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        c.execute("""INSERT OR REPLACE INTO anchor_meta
            (root,anchor_date,fetched_at,source,n_rows,spot_at_fetch)
            VALUES (?,?,?,?,?,?)""",
                  (root, today, time.time(), source, len(rows), spot0))
    return True

def anchor_snapshot(root, live_spot=None):
    """Rebuild a ChainSnapshot from anchored OI + live spot (static IV)."""
    _ensure()
    con = store.con()
    meta = con.execute("SELECT * FROM anchor_meta WHERE root=?",
                       (root,)).fetchone()
    if not meta:
        return None
    rows = con.execute("SELECT * FROM options_chain WHERE root=? AND anchor_date=?",
                       (root, meta["anchor_date"])).fetchall()
    if not rows:
        return None
    from .schema import ContractQuote, ChainSnapshot
    today = date.today()
    # 🟢 LOAD-TIME RE-FILTER: protects against already-poisoned rows
    spot_ref = float(live_spot or meta["spot_at_fetch"] or 0)
    lo, hi = (spot_ref * 0.88, spot_ref * 1.12) if spot_ref > 0 else (0.0, 1e18)
    contracts = []
    for r in rows:
        ed = _parse_expiry_dt(r["expiry"])
        dte = (ed - today).days if ed else None
        if dte is not None and (dte < 0 or dte > 45):
            continue
        if (r["iv"] or 0) <= 0.005:
            continue
        if not (lo <= float(r["strike"]) <= hi):
            continue
        kw = dict(symbol=root, expiry=r["expiry"], strike=r["strike"],
                  contract_type=r["ctype"], iv=r["iv"], delta=r["delta"],
                  gamma=r["gamma"], theta=r["theta"], vega=r["vega"],
                  open_interest=r["oi"], volume=r["volume"],
                  source="anchor", provenance="EOD-OI ANCHOR")
        try:
            q = ContractQuote(**kw, dte=dte)
        except TypeError:
            q = ContractQuote(**kw)
            try: q.dte = dte
            except Exception: pass
        contracts.append(q)
    if not contracts:
        return None
    spot = live_spot or meta["spot_at_fetch"] or 0
    return ChainSnapshot(symbol=root, spot=spot,
                         as_of=datetime.now().isoformat(),
                         source="anchor", provenance="EOD-OI ANCHOR + LIVE SPOT",
                         contracts=contracts)

def anchor_date(root):
    _ensure()
    r = store.con().execute("SELECT anchor_date FROM anchor_meta WHERE root=?",
                            (root,)).fetchone()
    return r["anchor_date"] if r else None

def status(root):
    _ensure()
    con = store.con()
    m = con.execute("SELECT * FROM anchor_meta WHERE root=?", (root,)).fetchone()
    ad = m["anchor_date"] if m else None
    n = con.execute("SELECT COUNT(*) AS n FROM options_chain WHERE root=? AND anchor_date=?",
                    (root, ad or "")).fetchone()
    return {"root": root, "anchor_date": ad,
            "fetched_at": m["fetched_at"] if m else None,
            "source": m["source"] if m else None,
            "n_rows": n["n"] if n else 0,
            "today": date.today().isoformat(),
            "stale": bool(ad and ad != date.today().isoformat())}