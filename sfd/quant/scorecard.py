"""
SFD signal scorecard — the honesty anchor.
Grades every logged verdict (from the main store) with realized forward
returns on the futures proxy at 1h / 4h / 1d, then aggregates hit rates.
FIX: Uses store.con() so the grades table lives in the SAME database as verdicts.
"""
import time
import numpy as np
import pandas as pd

from .. import store
from ..assets import get_asset

SCHEMA = """
CREATE TABLE IF NOT EXISTS grades (
  verdict_id INTEGER PRIMARY KEY,
  h1_ret REAL, h4_ret REAL, h1d_ret REAL,
  grade_h1 TEXT, grade_h4 TEXT, grade_h1d TEXT,
  graded_at REAL
);
"""
WIN, LOSS = 0.15, -0.15

def init():
    with store.con() as c:
        c.executescript(SCHEMA)

def _grade(r):
    if r is None:
        return None
    return "WIN" if r > WIN else "LOSS" if r < LOSS else "FLAT"

def grade_pending(max_age_days=14):
    """Grade ungraded verdicts older than 1h. Returns count graded."""
    init()
    rows = store.con().execute(
        "SELECT id, ts, asset, direction, entry FROM verdicts "
        "WHERE entry IS NOT NULL AND direction IS NOT NULL "
        "AND direction != 'FLAT' AND ts > ? ORDER BY ts DESC",
        (time.time() - max_age_days * 86400,)).fetchall()
    if not rows:
        return 0
    done = {r["verdict_id"] for r in
            store.con().execute("SELECT verdict_id FROM grades").fetchall()}
    pending = [r for r in rows
               if r["id"] not in done and (time.time() - r["ts"]) > 3600]
    if not pending:
        return 0

    by_asset = {}
    for r in pending:
        by_asset.setdefault(r["asset"], []).append(r)

    n = 0
    for asset, rlist in by_asset.items():
        try:
            fut = get_asset(asset)["futures"] + "=F"
        except Exception:
            continue
        try:
            import yfinance as yf
            df = yf.download(fut, interval="1h", period="30d",
                             progress=False, threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            ts_arr = np.array([int(t.timestamp()) for t in df.index])
            px_arr = df["Close"].to_numpy(float)
        except Exception:
            continue

        for r in rlist:
            entry = float(r["entry"])
            sign = 1 if r["direction"] == "LONG" else -1

            def fwd(hours):
                i = np.searchsorted(ts_arr, r["ts"] + hours * 3600)
                if i >= len(ts_arr):
                    return None
                return (px_arr[i] / entry - 1) * 100 * sign

            h1, h4, h1d = fwd(1), fwd(4), fwd(24)
            if h1 is None and h4 is None and h1d is None:
                continue
            with store.con() as c:
                c.execute("""INSERT OR REPLACE INTO grades
                    (verdict_id,h1_ret,h4_ret,h1d_ret,
                     grade_h1,grade_h4,grade_h1d,graded_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                          (r["id"], h1, h4, h1d,
                           _grade(h1), _grade(h4), _grade(h1d), time.time()))
            n += 1
    return n

def summary():
    init()
    rows = store.con().execute(
        "SELECT v.id, v.context, v.wall, v.decision, "
        "g.grade_h4, g.h4_ret FROM verdicts v "
        "JOIN grades g ON g.verdict_id = v.id").fetchall()
    groups = {}
    for r in rows:
        key = f"{r['context']}|{r['wall']}|{r['decision']}"
        g = groups.setdefault(key, {"n": 0, "wins": 0, "losses": 0,
                                    "flats": 0, "ret_sum": 0.0, "ret_n": 0})
        g["n"] += 1
        gr = r["grade_h4"] or "FLAT"
        if gr == "WIN":
            g["wins"] += 1
        elif gr == "LOSS":
            g["losses"] += 1
        else:
            g["flats"] += 1
        if r["h4_ret"] is not None:
            g["ret_sum"] += r["h4_ret"]
            g["ret_n"] += 1
    out = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
        out.append({"group": key, "n": g["n"], "wins": g["wins"],
                    "losses": g["losses"], "flats": g["flats"],
                    "hit_rate": round(g["wins"] / g["n"], 3) if g["n"] else 0,
                    "avg_h4_ret": round(g["ret_sum"] / g["ret_n"], 3)
                    if g["ret_n"] else None})
    totals = {"n": len(rows),
              "wins": sum(g["wins"] for g in groups.values()),
              "losses": sum(g["losses"] for g in groups.values()),
              "flats": sum(g["flats"] for g in groups.values())}
    totals["hit_rate"] = round(totals["wins"] / totals["n"], 3) if totals["n"] else 0
    return {"totals": totals, "groups": out, "ts": time.time()}