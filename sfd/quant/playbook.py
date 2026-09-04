"""
SFD Playbook Engine — the operator's manual as computed state.
Encodes the volatilityfullscript.txt rules. Pure computation, no side effects.
Single source of truth: consumed by the web terminal (and available to CLI/Sentinel).

v2.1: consumes gex v2.2 gamma_zone + flip_distance_pct (additive).
v2:   directional wall mapping — Call Wall = SELL zone, Put Wall = BUY zone,
      gated by gamma regime (fade in POSITIVE, no-fade in NEGATIVE).
"""

PROXIMITY = 0.003    # ARMED band (±0.3% of wall)
TOUCH_BAND = 0.001   # TOUCHED band (±0.1% of wall)

SESSION_BIAS = {
    "OPEN_DRIVE": "Fuel loading — breaks favored, small size, no early fades",
    "MIDDAY":     "Chop zone — default is NO trade; halve size if forced",
    "THETA_BURN": "PRIME WINDOW — fades highest probability on wall reactions",
    "POWER_HOUR": "0DTE expiry violence — only overwhelming setups",
    "PREMARKET":  "No regular session — prep & mark levels only",
    "CLOSED":     "Market closed — no action",
}

CALENDAR_RULE = {
    "BLACKOUT": "DO NOT TRADE — plumbing rebuilt (OPEX / triple witching)",
    "NO_FADE":  "VIX expiry — relentless trend day; NO fades, trend-with only",
    "ELEVATED": "Day before OPEX/VIX-exp — reduce size & conviction",
    "NORMAL":   "Full playbook active",
}


def _loop_state(spot, walls, proximity=PROXIMITY, touch_band=TOUCH_BAND):
    pw, cw = walls.get("put_wall"), walls.get("call_wall")
    put_d = abs(spot - pw) / spot if pw else 9.0
    call_d = abs(spot - cw) / spot if cw else 9.0
    nearest = "PUT" if put_d < call_d else "CALL"
    dist = min(put_d, call_d)
    if dist <= touch_band:
        state = "TOUCHED"
    elif dist <= proximity:
        state = "ARMED"
    else:
        state = "WATCHING"
    return state, nearest, dist


def _zdte_level(intensity):
    if intensity > 0.50:
        return "EXTREME", "Violent reactions — halve size, tighten stops"
    if intensity > 0.30:
        return "HIGH", "F1 car hot — fast reactions"
    if intensity > 0.15:
        return "MODERATE", "Normal reactions"
    return "LOW", "Calmer reactions"


def build_playbook(spot, walls, surface, zero_dte, session, calendar):
    state, nearest, dist = _loop_state(spot, walls)

    regime = walls.get("regime", "?")
    gamma_zone = walls.get("gamma_zone")
    flip_dist = walls.get("flip_distance_pct")
    phase = session.get("phase", "CLOSED")
    cal_level = calendar.get("risk_level", "NORMAL")
    tradeable = calendar.get("trade_allowed", True) and phase not in ("CLOSED", "PREMARKET")

    zd_intensity = zero_dte.get("intensity", 0.0) if zero_dte.get("valid") else 0.0
    zd_level, zd_note = _zdte_level(zd_intensity)

    skew = surface.get("skew", {})
    term = surface.get("term_structure", {})

    # ── Regime bias (unchanged from v1) ───────────────────────
    if regime == "POSITIVE":
        regime_bias = "Walls hold — bias FADE on reactions"
    elif regime == "NEGATIVE":
        regime_bias = "Walls break — bias BREAK / trend-with"
    else:
        regime_bias = "—"

    # ── Directional mapping: Call Wall = SELL zone, Put Wall = BUY zone ──
    if nearest == "CALL":
        direction = "SELL"
        if regime == "POSITIVE":
            action_bias = "SHORT the Call Wall (resistance) — fade the reaction"
        elif regime == "NEGATIVE":
            action_bias = "NO FADE — negative gamma: if Call Wall breaks, momentum continues UP"
        else:
            action_bias = "Call Wall in play — await regime read"
    else:
        direction = "BUY"
        if regime == "POSITIVE":
            action_bias = "LONG the Put Wall (support) — bid the reaction"
        elif regime == "NEGATIVE":
            action_bias = "NO FADE — negative gamma: if Put Wall breaks, momentum continues DOWN"
        else:
            action_bias = "Put Wall in play — await regime read"

    # ── DO / DON'T rules ──────────────────────────────────────
    do, dont = [], []
    do.append("Wait for collision + flinch before entry")
    if state in ("ARMED", "TOUCHED"):
        do.append(action_bias)
        do.append("Tie-breaker: basis compressing at the wall = fade valid; basis widening = break risk")
    sb = SESSION_BIAS.get(phase)
    if sb:
        do.append(sb)
    if regime == "POSITIVE":
        do.append("Fade wall reactions (positive-gamma pin)")

    # gamma zone guidance (gex v2.2)
    if gamma_zone == "PIN":
        fd = f" (flip {flip_dist:+.2f}% away)" if flip_dist is not None else ""
        do.append(f"Gamma zone PIN — spot above flip{fd}; walls hold, fade reactions")
    elif gamma_zone == "VOLATILE":
        dont.append("Gamma zone VOLATILE — spot below flip; walls break, do NOT fade")
        do.append("Trade with momentum; expect wall breaks")
    elif gamma_zone == "MIXED":
        dont.append("Gamma zone MIXED — positive book but spot below flip; halve conviction")
    elif gamma_zone == "TRANSITION":
        dont.append("Gamma zone TRANSITION — negative book, spot above flip; unstable, wait")

    if not tradeable:
        dont.append("Do NOT trade — calendar/session blocks it")
    else:
        dont.append("Don't front-run the wall")
        dont.append("Don't enter without a stop")
    if regime == "NEGATIVE":
        dont.append("Don't fade without strong confirmation")
    if cal_level == "NO_FADE":
        dont.append("No fades — VIX expiry trend day")
    if zd_level == "EXTREME":
        dont.append("0DTE EXTREME — avoid oversized positions")
    if term.get("valid") and term.get("inverted"):
        dont.append("Term structure inverted — stress day, raise conviction bar")

    headline = (f"{phase} · {regime} γ · {gamma_zone or '—'} · "
                f"0DTE {zd_level} → {CALENDAR_RULE.get(cal_level, '')}")

    return {
        "loop_state": state,
        "nearest_wall": nearest,
        "distance_pct": round(dist * 100, 3),
        "direction": direction,
        "action_bias": action_bias,
        "session_phase": phase,
        "session_bias": SESSION_BIAS.get(phase, ""),
        "calendar_level": cal_level,
        "calendar_rule": CALENDAR_RULE.get(cal_level, ""),
        "tradeable": tradeable,
        "regime": regime,
        "regime_bias": regime_bias,
        "gamma_zone": gamma_zone,
        "flip_point": walls.get("flip_point"),
        "flip_distance_pct": flip_dist,
        "zero_dte": {"intensity": round(zd_intensity, 4), "level": zd_level,
                     "note": zd_note, "valid": zero_dte.get("valid", False)},
        "skew": {"valid": skew.get("valid", False), "skew": skew.get("skew"),
                 "interpretation": skew.get("interpretation", "")},
        "term": {"valid": term.get("valid", False), "inverted": term.get("inverted"),
                 "note": term.get("note", "")},
        "do": [d for d in do if d],
        "dont": [d for d in dont if d],
        "headline": headline,
    }