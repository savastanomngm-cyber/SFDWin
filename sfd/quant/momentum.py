"""
SFD Tape Speed & Momentum Engine.
Calculates real-time velocity and volume profile using 5m intraday bars.
Uses SPY as the real-time proxy for SPX walls.
"""
import yfinance as yf
import numpy as np
import pandas as pd

def get_tape_speed(proxy="SPY", interval="5m", lookback_bars=20):
    """
    Calculates the real-time velocity and volume profile of the underlying.
    """
    try:
        # Fetch intraday data
        df = yf.download(proxy, period="5d", interval=interval, progress=False)
        if df.empty or len(df) < lookback_bars:
            return {"speed": "UNKNOWN", "summary": "Insufficient intraday data."}

        # Handle MultiIndex columns if yfinance returns them
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        closes = df['Close'].dropna().values
        volumes = df['Volume'].dropna().values

        # 1. Calculate Slope (Velocity) using linear regression on last N bars
        recent_closes = closes[-lookback_bars:]
        x = np.arange(len(recent_closes))
        slope, _ = np.polyfit(x, recent_closes, 1)

        # Normalize slope to percentage move per bar
        avg_price = np.mean(recent_closes)
        slope_pct = (slope / avg_price) * 100

        # 2. Calculate Volume Profile
        recent_vols = volumes[-lookback_bars:]
        avg_vol = np.mean(volumes[:-1]) if len(volumes) > 1 else 1
        current_vol = volumes[-1]
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        # 3. Classify the Approach (The "Slope" Logic)
        # Thresholds tuned for 5m SPY bars
        if abs(slope_pct) > 0.15 and vol_ratio > 1.5:
            speed = "VIOLENT"
            summary = (f"High velocity ({slope_pct:+.3f}%/bar) with expanding volume "
                       f"({vol_ratio:.1f}x avg). Institutional sweep or liquidation cascade. "
                       f"Wall is likely to BREAK.")
        elif abs(slope_pct) < 0.05 and vol_ratio < 0.8:
            speed = "GENTLE"
            summary = (f"Low velocity ({slope_pct:+.3f}%/bar) with declining volume "
                       f"({vol_ratio:.1f}x avg). Exhaustion likely. Wall should HOLD (Fade).")
        else:
            speed = "MODERATE"
            summary = (f"Moderate velocity ({slope_pct:+.3f}%/bar) and volume "
                       f"({vol_ratio:.1f}x avg). No clear structural imbalance yet.")

        return {
            "speed": speed,
            "slope_pct": round(slope_pct, 4),
            "vol_ratio": round(vol_ratio, 2),
            "summary": summary,
            "last_price": round(closes[-1], 2)
        }
    except Exception as e:
        return {"speed": "ERROR", "summary": f"Data fetch failed: {str(e)[:60]}"}