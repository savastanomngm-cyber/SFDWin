"""
SFD real-time SPOT ladder — kills the 15-min yfinance lag on triggers.
Provider order (first success wins):
  1. TradingView scanner (no key, near-real-time CME/index)
  2. Polygon (hook for a future paid plan)
  3. yfinance fast_info (15-min delayed, always-available floor)
Every result carries provenance: {"price","source","ts","live"}
Cached 5s per (root,kind) so we never hammer providers.
"""
import os
import json
import time
import threading
import urllib.request

_CACHE = {}
_LOCK = threading.Lock()
TTL = 5.0

# TradingView symbols: (options_root, kind) -> TV ticker
# FIXED: CME futures require the "CME_MINI:" prefix [[10]], [[7]]
TV_MAP = {
    ("NDX", "index"):   "NASDAQ:NDX",
    ("NDX", "futures"): "CME_MINI:NQ1!",
    ("SPX", "index"):   "SP:SPX",
    ("SPX", "futures"): "CME_MINI:ES1!",
}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _tv_quote(tv_symbol):
    """Unofficial TradingView scanner quote (no auth)."""
    payload = {"symbols": {"tickers": [tv_symbol]}, "columns": ["close"]}
    # Added cme/scan endpoint for CME futures contracts
    for host in ("https://scanner.tradingview.com/cme/scan",
                 "https://scanner.tradingview.com/america/scan",
                 "https://scanner.tradingview.com/scan",
                 "https://scanner.tradingview.com/global/scan"):
        try:
            req = urllib.request.Request(
                host, data=json.dumps(payload).encode(),
                headers={"User-Agent": _UA,
                         "Content-Type": "text/plain;charset=UTF-8",
                         "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode())
            rows = data.get("data") or []
            if not rows:
                continue
            d = rows[0].get("d") or []
            px = d[0] if d else None
            if px:
                return float(px)
        except Exception:
            continue
    return None


def _polygon_quote(_sym):
    """Hook for a future real-time Polygon/IBKR/ThetaData plan."""
    if not os.getenv("POLYGON_API_KEY", ""):
        return None
    return None


def _yf_quote(yf_sym):
    import yfinance as yf
    try:
        px = yf.Ticker(yf_sym).fast_info.get("last_price")
        if px:
            return float(px)
    except Exception:
        pass
    try:
        h = yf.Ticker(yf_sym).history(period="1d", interval="1m")
        if h is not None and not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def get_spot(asset, kind="index"):
    """Ladder: TV -> Polygon -> yfinance. Returns {price, source, ts, live}."""
    from ..assets import index_yf_symbol
    key = (asset.get("options_root", "?"), kind)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]

    price, source, live = None, None, False

    tv = TV_MAP.get(key)
    if tv:
        try:
            price = _tv_quote(tv)
            if price:
                source, live = "tradingview", True
        except Exception:
            price = None

    if not price:
        price = _polygon_quote(key)
        if price:
            source, live = "polygon", True

    if not price:
        yf_sym = asset["futures"] + "=F" if kind == "futures" else index_yf_symbol(asset)
        price = _yf_quote(yf_sym)
        if price:
            source, live = "yfinance", False

    out = {"price": price, "source": source or "none",
           "ts": time.time(), "live": bool(live)}
    with _LOCK:
        _CACHE[key] = (time.time(), out)
    return out


def get_index_spot(asset):
    r = get_spot(asset, "index")
    return r["price"]


def get_futures_spot(asset):
    r = get_spot(asset, "futures")
    return r["price"]