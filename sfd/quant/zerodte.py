"""
SFD 0DTE layer: intraday magnets + instrument recommendation.
"""
from collections import defaultdict


def zero_dte_walls(chain, multiplier=100):
    """Same-day gamma walls; falls back to OI then volume if gamma missing."""
    g = defaultdict(float)
    spot = chain.spot
    for c in chain.contracts:
        if c.dte is None or c.dte != 0:
            continue
        oi = c.open_interest or 0
        vol = c.volume or 0
        if c.gamma is not None and oi:
            w = c.gamma * oi * multiplier * spot * 0.01
        elif oi:
            w = float(oi)
        elif vol:
            w = float(vol)
        else:
            continue
        g[c.strike] += w if c.contract_type == "call" else -w
    pw = cw = None
    mg = MG = 0.0
    for s, v in g.items():
        if v < mg:
            mg, pw = v, s
        if v > MG:
            MG, cw = v, s
    return {
        "put_wall_0dte": pw if (pw is not None and mg < 0) else None,
        "call_wall_0dte": cw if (cw is not None and MG > 0) else None,
    }


def recommend_instrument(phase, zd):
    level = (zd or {}).get("level", "LOW")
    hot = level in ("HIGH", "EXTREME")
    if phase in ("PREMARKET", "CLOSED"):
        return {"instrument": "FUTURES", "note": "Prep only - no live 0DTE edge outside RTH."}
    if phase == "OPEN_DRIVE":
        if hot:
            return {"instrument": "0DTE", "note": "Gamma ignition - small size, hard clock exit by ~11:30."}
        return {"instrument": "FUTURES", "note": "Calm tape - futures cleanest for the fade."}
    if phase == "MIDDAY":
        return {"instrument": "FUTURES", "note": "Chop - half size, no 0DTE (theta noise)."}
    if phase == "THETA_BURN":
        return {"instrument": "1DTE", "note": "Fade needs hours - 1DTE gives theta runway, hold to close."}
    if phase == "POWER_HOUR":
        if level == "EXTREME":
            return {"instrument": "0DTE", "note": "Expiry violence - 0DTE only for experts, else flat."}
        return {"instrument": "FUTURES", "note": "Late hedging - futures or stand aside."}
    return {"instrument": "FUTURES", "note": "Standard playbook."}
