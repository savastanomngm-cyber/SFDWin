"""Market calendar guard — OPEX / Triple Witching / VIX expiry.

volatilitysurface.txt is explicit:
  - Triple Witching: 'He doesn't even turn his screens on.'
  - VIX expiry: 'someone cut the brake lines on the F1 car' — relentless
    one-directional trend days; mean-reversion fades get crushed.
  - 'Recognizing the environment is just as important as recognizing the setup.'

Risk tiers:
  BLACKOUT -> trade_allowed=False  (triple witching, monthly OPEX w/ skip_opex)
  NO_FADE  -> breaks only           (VIX expiry: fades get bulldozed)
  ELEVATED -> caution               (day before OPEX / VIX)
  NORMAL   -> full playbook
"""
from datetime import date, timedelta

TRIPLE_WITCH_MONTHS = {3, 6, 9, 12}


def third_friday(year, month):
    """3rd Friday = monthly OPEX."""
    first = date(year, month, 1)
    days_until_fri = (4 - first.weekday()) % 7
    return first + timedelta(days=days_until_fri + 14)


def vix_expiry(year, month):
    """Wednesday exactly 30 days before the month's 3rd Friday."""
    return third_friday(year, month) - timedelta(days=30)


def _month_shift(year, month, delta):
    m, y = month + delta, year
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return y, m


def opex_status(d=None, skip_opex=True):
    d = d or date.today()
    y, m = d.year, d.month

    # Scan a 5-month window so next_opex / next_vix ALWAYS resolve.
    # (A -1..+1 window can leave all VIX expiries in the past late in a month,
    #  because VIX-exp for month M falls ~2 months earlier.)
    triples, opexes, vixes = set(), set(), set()
    for dm in range(-1, 4):
        yy, mm = _month_shift(y, m, dm)
        tf = third_friday(yy, mm)
        opexes.add(tf)
        if mm in TRIPLE_WITCH_MONTHS:
            triples.add(tf)
        vixes.add(vix_expiry(yy, mm))

    is_triple = d in triples
    is_opex = d in opexes
    is_vix = d in vixes
    is_pre_opex = (d + timedelta(days=1)) in opexes
    is_pre_vix = (d + timedelta(days=1)) in vixes

    # Defensive: never crash on an empty sequence
    future_opex = [x for x in opexes if x >= d]
    future_vix = [x for x in vixes if x >= d]
    next_opex = min(future_opex) if future_opex else d + timedelta(days=30)
    next_vix = min(future_vix) if future_vix else d + timedelta(days=30)

    if is_triple:
        level, allowed, no_fade = "BLACKOUT", False, True
        guidance = ("TRIPLE WITCHING — index, equity and futures options all expire. "
                    "The plumbing is being ripped out. Per Freddy: don't turn the screens on.")
    elif is_opex and skip_opex:
        level, allowed, no_fade = "BLACKOUT", False, True
        guidance = "Monthly OPEX — dealer rolls make gamma structure chaotic. skip_opex=true."
    elif is_vix:
        level, allowed, no_fade = "NO_FADE", True, True
        guidance = ("VIX EXPIRY — brake lines cut. Expect a relentless one-directional trend. "
                    "Fades get bulldozed; only BREAK-with-trend is permissible.")
    elif is_pre_opex or is_pre_vix:
        level, allowed, no_fade = "ELEVATED", True, False
        guidance = "Day before OPEX/VIX-exp — positioning churn; reduce size and conviction."
    else:
        level, allowed, no_fade = "NORMAL", True, False
        guidance = "Normal session. Full playbook active."

    return {
        "date": d.isoformat(),
        "risk_level": level,
        "trade_allowed": allowed,
        "no_fade": no_fade,
        "is_triple_witching": is_triple,
        "is_monthly_opex": is_opex,
        "is_vix_expiry": is_vix,
        "days_to_opex": (next_opex - d).days,
        "days_to_vix": (next_vix - d).days,
        "guidance": guidance,
    }