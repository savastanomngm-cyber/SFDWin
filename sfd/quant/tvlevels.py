"""
SFD TradingView levels layer.
build_tv_string(chain, gex, surface, multiplier, open_price, atr)
  -> chart-ready levels dict (walls, flip, 0DTE, two-lines, spikes/pockets,
     session open ± ATR bands) + compact legacy pipe-string.
fetch_open_atr(tape_proxy) -> (open_price, atr14) from 5m bars.
"""
import pandas as pd
import yfinance as yf


def fetch_open_atr(tape_proxy, interval="5m", period="5d"):
    """Session open + 14-period ATR on 5m bars for the tape proxy (e.g. 'NQ')."""
    sym = tape_proxy if "=" in tape_proxy else tape_proxy + "=F"
    try:
        df = yf.download(sym, interval=interval, period=period,
                         progress=False, threads=False)
        if df is None or df.empty:
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        prev_close = df["Close"].shift(1)
        tr = pd.concat([df["High"] - df["Low"],
                        (df["High"] - prev_close).abs(),
                        (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        last_day = df.index[-1].date()
        today = df[df.index.date == last_day]
        open_price = (float(today["Open"].iloc[0]) if not today.empty
                      else float(df["Open"].iloc[-1]))
        return open_price, atr
    except Exception:
        return None, None


def build_tv_string(chain, gex, surface, multiplier=100,
                    open_price=None, atr=None):
    """Assemble every tradeable level into one payload for chart overlays."""
    spot = chain.spot
    levels = []

    def add(price, kind, strength=1.0):
        if price is None:
            return
        levels.append({"price": round(float(price), 2),
                       "kind": kind, "strength": round(float(strength), 2)})

    # structural walls + regime flip
    add(gex.get("put_wall"), "PUT_WALL", 1.0)
    add(gex.get("call_wall"), "CALL_WALL", 1.0)
    add(gex.get("flip_point"), "FLIP", 0.8)

    # same-day gamma magnets (present when server enriches gex)
    add(gex.get("put_wall_0dte"), "PUT_0DTE", 0.6)
    add(gex.get("call_wall_0dte"), "CALL_0DTE", 0.6)

    # vol-surface structure: two lines, reject mountains, accel valleys
    surf = surface or {}
    for k in (surf.get("top_convexity_levels") or []):
        add(k, "TWO_LINE", 0.9)
    for s in (surf.get("spikes") or [])[:3]:
        add(s.get("strike"), "SPIKE", 0.5)
    for p in (surf.get("pockets") or [])[:3]:
        add(p.get("strike"), "POCKET", 0.5)

    # session open + ATR bands (tape reference)
    if open_price:
        add(open_price, "OPEN", 0.7)
        if atr:
            add(open_price + atr, "OPEN+1ATR", 0.4)
            add(open_price - atr, "OPEN-1ATR", 0.4)

    tv = "|".join(f"{l['kind']}:{l['price']}" for l in levels)
    return {"levels": levels, "tv_string": tv, "spot": spot,
            "open": open_price, "atr": atr}