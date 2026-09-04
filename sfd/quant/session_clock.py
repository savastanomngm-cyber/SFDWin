"""Session phase / theta-burn clock.

volatilitysurface.txt reversal mechanic: institutions take profit when
convexity flattens + theta decay accelerates (0DTE dies at 4pm). That
profit-taking forces market makers to dump hedges -> the wall bounce.
So TIME-OF-DAY changes whether a wall touch is fade-able:
  OPEN_DRIVE (09:30-10:30) fuel loading   -> breaks favored, fading risky
  MIDDAY     (10:30-13:00) chop           -> low conviction
  THETA_BURN (13:00-15:30) profit-taking  -> FADES highest probability
  POWER_HOUR (15:30-16:00) expiry violence-> pin/whip, extreme caution
"""
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PHASES = {
    "PREMARKET":  {"fade_bias": 0.0,  "break_bias": 0.0,
                   "note": "No regular session yet."},
    "OPEN_DRIVE": {"fade_bias": -0.3, "break_bias": 0.3,
                   "note": "Fuel loading. Momentum ignitions; fading a wall here means "
                           "stepping in front of a hedging tsunami."},
    "MIDDAY":     {"fade_bias": 0.0,  "break_bias": 0.0,
                   "note": "Chop zone; low conviction both ways."},
    "THETA_BURN": {"fade_bias": 0.4,  "break_bias": -0.2,
                   "note": "Institutions lock profits as theta accelerates; forced MM "
                           "hedge-unwind makes wall fades highest probability."},
    "POWER_HOUR": {"fade_bias": 0.0,  "break_bias": 0.0,
                   "note": "0DTE expiry violence; gamma pin or whip. Extreme caution."},
    "CLOSED":     {"fade_bias": 0.0,  "break_bias": 0.0,
                   "note": "Market closed — data is frozen/final."},
}


def session_phase(now=None):
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    hm, wd = now.hour * 100 + now.minute, now.weekday()

    if wd >= 5:
        phase = "CLOSED"
    elif hm < 930:
        phase = "PREMARKET"
    elif hm < 1030:
        phase = "OPEN_DRIVE"
    elif hm < 1300:
        phase = "MIDDAY"
    elif hm < 1530:
        phase = "THETA_BURN"
    elif hm < 1600:
        phase = "POWER_HOUR"
    else:
        phase = "CLOSED"

    meta = PHASES[phase]
    return {
        "phase": phase,
        "local_time": now.strftime("%H:%M ET"),
        "fade_bias": meta["fade_bias"],
        "break_bias": meta["break_bias"],
        "note": meta["note"],
    }