"""Outcome grading + signal scorecard (Priority 5).
improvements.txt Part 8: 'The signal scorecard is the honesty anchor.'

Grading semantics (conservative by design):
- Path check on realized 5m index bars, same calendar day as the verdict
  (intraday desk — overnight gaps don't count for or against).
- If stop and target fall in the SAME bar, grade LOSS (unknown path order).
- WIN = Target 1 touched first (+rr1 R). LOSS = stop touched first (−1 R).
- EXPIRED = neither within the horizon; terminal P&L in R.
- WAIT verdicts grade instantly as NO_TRADE (discipline metric).
"""
import time
import pandas as pd
import yfinance as yf
from .. import store
from ..assets import get_asset, index_yf_symbol

GRADE_MIN_BARS = 6        # need ≥30 min of tape before grading
HORIZON_BARS = 24         # cap: 24 x 5m = 2 hours
STALE_SECS = 24 * 3600    # no same-day tape after 24h -> close as EXPIRED/0


def _bars_after(asset_key, ts):
    """Realized 5m bars for the verdict's calendar day, after ts."""
    asset = get_asset(asset_key)
    sym = index_yf_symbol(asset)
    try:
        df = yf.download(sym, period="5d", interval="5m", progress=False)
    except Exception:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        return df
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx
    verdict_day = pd.Timestamp(ts, unit="s").date()
    df = df[[d == verdict_day for d in df.index.date]]
    df = df[df.index > pd.Timestamp(ts, unit="s")]
    return df.head(HORIZON_BARS)


def grade_verdict(v, bars):
    """Returns (outcome, realized_r) or None if not gradable yet."""
    if v["decision"] not in ("FADE", "BREAK") or v["direction"] == "FLAT":
        return ("NO_TRADE", 0.0)

    wall = v["call_wall"] if v["wall"] == "CALL" else v["put_wall"]
    stop_d, t1_d = v["stop_dist"], v["t1_dist"]
    if not wall or not stop_d or not t1_d:
        return None
    if len(bars) < GRADE_MIN_BARS:
        return None

    rr1 = t1_d / stop_d
    long_side = v["direction"] == "LONG"

    for _, bar in bars.iterrows():
        h, l = float(bar["High"]), float(bar["Low"])
        if long_side:
            if l <= wall - stop_d:              # stop first (conservative)
                return ("LOSS", -1.0)
            if h >= wall + t1_d:
                return ("WIN", round(rr1, 2))
        else:
            if h >= wall + stop_d:
                return ("LOSS", -1.0)
            if l <= wall - t1_d:
                return ("WIN", round(rr1, 2))

    final = float(bars["Close"].iloc[-1])
    r = ((final - wall) / stop_d) if long_side else ((wall - final) / stop_d)
    return ("EXPIRED", round(max(-1.0, min(rr1, r)), 2))


def grade_all():
    """Grade every ungraded verdict. Returns a summary dict."""
    stats = {"graded": 0, "win": 0, "loss": 0, "expired": 0,
             "no_trade": 0, "waiting": 0}
    for v in store.load_ungraded_verdicts():
        if v["decision"] not in ("FADE", "BREAK"):
            store.mark_graded(v["id"], "NO_TRADE", 0.0)
            stats["no_trade"] += 1
            stats["graded"] += 1
            continue

        bars = _bars_after(v["asset"], v["ts"])
        result = grade_verdict(v, bars)

        if result is None:
            if time.time() - v["ts"] > STALE_SECS:
                store.mark_graded(v["id"], "EXPIRED", 0.0)
                stats["expired"] += 1
                stats["graded"] += 1
            else:
                stats["waiting"] += 1
            continue

        outcome, r = result
        store.mark_graded(v["id"], outcome, r)
        stats["graded"] += 1
        stats[outcome.lower()] += 1
    return stats


def scorecard():
    """Aggregate graded verdicts into the honesty anchor."""
    rows = store.load_graded_verdicts()
    actionable = [r for r in rows if r["decision"] in ("FADE", "BREAK")]
    no_trade = [r for r in rows if r["outcome"] == "NO_TRADE"]

    groups = {}
    for r in actionable:
        key = (r["decision"], r["wall"] or "?", r["regime"] or "?")
        groups.setdefault(key, []).append(r)

    lines = []
    for (dec, wall, regime), rs in sorted(groups.items()):
        wins = [r for r in rs if r["outcome"] == "WIN"]
        losses = [r for r in rs if r["outcome"] == "LOSS"]
        expired = [r for r in rs if r["outcome"] == "EXPIRED"]
        decided = len(wins) + len(losses)
        total_r = sum(r["realized_r"] or 0 for r in rs)
        lines.append({
            "decision": dec, "wall": wall, "regime": regime,
            "n": len(rs), "wins": len(wins), "losses": len(losses),
            "expired": len(expired),
            "hit_rate": round(len(wins) / decided, 2) if decided else None,
            "total_r": round(total_r, 2),
            "avg_r": round(total_r / len(rs), 2) if rs else 0.0,
        })

    return {
        "groups": lines,
        "no_trade_count": len(no_trade),
        "total_actionable": len(actionable),
        "total_r": round(sum(r["realized_r"] or 0 for r in actionable), 2),
        "chain_ok": store.verify_verdict_chain(),
    }