"""
SFD Confirmation State Machine.

volatilityfullscript.txt (Freddy Siento) — the entry rule:
  "You do not try to front-run the wall. You wait for the collision... wait for
   that first red candle, that initial rejection confirming the institution is
   taking profits and the market maker is beginning to unwind."

  Approach slope decides the confirmation type:
    - GENTLE approach  -> wall expected to HOLD  -> confirm on REJECTION flinch
    - VIOLENT approach -> wall expected to BREAK -> confirm on RECLAIM / BREAK-HOLD

States:
  WATCHING  -> price far from wall, idle
  ARMED     -> price within proximity (precursor warning), monitor active
  TOUCHED   -> price reached/crossed wall, awaiting confirmation
  CONFIRMED -> rejection/reclaim detected -> MAIN TRIGGER (wakes AI pipeline)
  EXPIRED   -> no confirmation within window -> stand down

Proximity is the PRECURSOR (fair warning). Confirmation is the TRIGGER.
"""
import time
import numpy as np
import pandas as pd
import yfinance as yf

# Module-level bars cache (avoid hammering yfinance)
_BARS_CACHE = {}
_BARS_CACHE_SECS = 45

WATCHING = "WATCHING"
ARMED = "ARMED"
TOUCHED = "TOUCHED"
CONFIRMED = "CONFIRMED"
EXPIRED = "EXPIRED"


def fetch_bars(symbol, interval="1m", period="1d"):
    """Fetch OHLC bars for the index (same coordinate system as the walls)."""
    now = time.time()
    key = (symbol, interval)
    cached = _BARS_CACHE.get(key)
    if cached and now - cached[0] < _BARS_CACHE_SECS:
        return cached[1]
    try:
        df = yf.download(symbol, interval=interval, period=period,
                         progress=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is not None and not df.empty:
            _BARS_CACHE[key] = (now, df)
            return df
    except Exception:
        pass
    return cached[1] if cached else None


def classify_approach(bars, lookback=5, violent_pct_per_bar=0.12):
    """Classify the approach slope as GENTLE or VIOLENT."""
    if bars is None or len(bars) < lookback + 1:
        return "GENTLE"
    closes = bars["Close"].tail(lookback + 1).values.astype(float)
    x = np.arange(len(closes))
    try:
        slope = np.polyfit(x, closes, 1)[0]
    except Exception:
        return "GENTLE"
    avg = float(np.mean(closes)) or 1.0
    slope_pct = abs(slope / avg) * 100
    return "VIOLENT" if slope_pct >= violent_pct_per_bar else "GENTLE"


def detect_rejection(bars, wall, side, lookback=3):
    """Rejection flinch: wick pierces the wall but close returns to the safe side."""
    if bars is None or bars.empty:
        return False
    for _, bar in bars.tail(lookback).iterrows():
        h, l, c = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        if side == "CALL" and h >= wall and c < wall:
            return True
        if side == "PUT" and l <= wall and c > wall:
            return True
    return False


def detect_reclaim(bars, wall, side, lookback=5):
    """Break-then-reclaim: price crossed the wall then closed back on the
    original side (a failed break -> favors a fade)."""
    if bars is None or len(bars) < 2:
        return False
    crossed = False
    for _, bar in bars.tail(lookback).iterrows():
        c = float(bar["Close"])
        if side == "CALL":
            if c > wall:
                crossed = True
            elif crossed and c < wall:
                return True
        else:
            if c < wall:
                crossed = True
            elif crossed and c > wall:
                return True
    return False


def detect_break_hold(bars, wall, side, hold_bars=2):
    """Clean break: the last N bars all closed beyond the wall (favors break)."""
    if bars is None or len(bars) < hold_bars:
        return False
    recent = bars.tail(hold_bars)
    if side == "CALL":
        return all(float(b["Close"]) > wall for _, b in recent.iterrows())
    return all(float(b["Close"]) < wall for _, b in recent.iterrows())


class ConfirmationMonitor:
    """Tracks ONE wall through the confirmation state machine."""

    def __init__(self, wall_type, wall_level, symbol,
                 proximity_pct=0.003, confirm_window_secs=480, interval="1m"):
        self.wall_type = wall_type              # 'CALL' or 'PUT'
        self.wall_level = float(wall_level)
        self.symbol = symbol                    # index symbol for bars (e.g. '^NDX')
        self.proximity_pct = proximity_pct
        self.confirm_window_secs = confirm_window_secs
        self.interval = interval
        self.state = WATCHING
        self.approach = None
        self.confirmation = None
        self.touch_time = None
        self._disarm_dist = proximity_pct * 2.5

    def _distance(self, spot):
        return (spot - self.wall_level) / spot

    def _touched(self, bars):
        """Did price reach/cross the wall in recent bars?"""
        if bars is None or bars.empty:
            return False
        recent = bars.tail(3)
        if self.wall_type == "CALL":
            return any(float(b["High"]) >= self.wall_level for _, b in recent.iterrows())
        return any(float(b["Low"]) <= self.wall_level for _, b in recent.iterrows())

    def update(self, spot):
        """Advance the state machine. Returns dict with state + signal."""
        bars = fetch_bars(self.symbol, self.interval)

        if self.state == WATCHING:
            if abs(self._distance(spot)) <= self.proximity_pct:
                self.state = ARMED
                return {"state": ARMED, "signal": "precursor",
                        "wall": self.wall_type, "wall_level": self.wall_level}
            return {"state": WATCHING, "signal": None}

        if self.state == ARMED:
            if bars is not None and self._touched(bars):
                self.state = TOUCHED
                self.approach = classify_approach(bars)
                self.touch_time = time.time()
                return {"state": TOUCHED, "signal": "touch",
                        "approach": self.approach, "wall": self.wall_type}
            if abs(self._distance(spot)) > self._disarm_dist:
                self.state = WATCHING
                return {"state": WATCHING, "signal": "disarmed"}
            return {"state": ARMED, "signal": None}

        if self.state == TOUCHED:
            if time.time() - self.touch_time > self.confirm_window_secs:
                self.state = EXPIRED
                return {"state": EXPIRED, "signal": "expired", "wall": self.wall_type}
            if bars is None:
                return {"state": TOUCHED, "signal": None}

            if self.approach == "GENTLE":
                if detect_rejection(bars, self.wall_level, self.wall_type):
                    self.state = CONFIRMED
                    self.confirmation = "REJECTION"
                    return {"state": CONFIRMED, "signal": "trade",
                            "confirmation": "REJECTION", "approach": "GENTLE",
                            "wall": self.wall_type}
            else:  # VIOLENT
                if detect_reclaim(bars, self.wall_level, self.wall_type):
                    self.state = CONFIRMED
                    self.confirmation = "RECLAIM"
                    return {"state": CONFIRMED, "signal": "trade",
                            "confirmation": "RECLAIM", "approach": "VIOLENT",
                            "wall": self.wall_type}
                if detect_break_hold(bars, self.wall_level, self.wall_type):
                    self.state = CONFIRMED
                    self.confirmation = "BREAK_HOLD"
                    return {"state": CONFIRMED, "signal": "trade",
                            "confirmation": "BREAK_HOLD", "approach": "VIOLENT",
                            "wall": self.wall_type}
            return {"state": TOUCHED, "signal": None}

        # Terminal states (CONFIRMED / EXPIRED); caller resets the monitor
        return {"state": self.state, "signal": None}