"""
SFD Volatility Surface layer (PATCHED for weekend robustness).

Provides:
  analyze_surface(chain, spot)  -> spikes / pockets / two-lines / skew / term structure
  route_assessment(...)         -> does the wall->runner route cross an IV mountain (block)
                                   or a valley (accelerate)?
  complexity_level(...)         -> CX-1..CX-4 tape-complexity grade derived from volatility.

All outputs are pure math on the fetched chain — no LLM opinion.
"""
from collections import defaultdict

# ── helpers ──────────────────────────────────────────────────
def _strike_iv(chain, max_dte=45):
    """strike -> mean IV across front-month call+put contracts."""
    iv = defaultdict(list)
    for c in chain.contracts:
        if c.iv and c.dte is not None and 0 <= c.dte <= max_dte:
            iv[c.strike].append(c.iv)
    return {k: sum(v) / len(v) for k, v in iv.items() if v}

def _convexity(chain, max_dte=45):
    """strike -> signed gamma*OI (dealer convexity), front month."""
    g = defaultdict(float)
    for c in chain.contracts:
        if c.dte is None or c.dte > max_dte or not c.open_interest:
            continue
        w = (c.gamma or 0.0) * c.open_interest
        g[c.strike] += w if c.contract_type == "call" else -w
    return dict(g)

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0

# ── surface analysis ─────────────────────────────────────────
def analyze_surface(chain, spot):
    iv = _strike_iv(chain)
    strikes = sorted(iv)
    spikes, pockets = [], []
    
    # 🟢 PATCHED: Lowered threshold from 7 to 5 for weekend/sparse data robustness
    if len(strikes) >= 5:
        for i, k in enumerate(strikes):
            # Use a smaller window for sparse data
            window = min(3, max(1, len(strikes) // 4))
            lo, hi = max(0, i - window), min(len(strikes), i + window + 1)
            
            neighbors = [iv[s] for j, s in enumerate(strikes)
                        if lo <= j < hi and s != k]
            if not neighbors:
                continue
                
            base = _median(neighbors)
            dev = iv[k] - base
            
            # 🟢 PATCHED: More lenient threshold for sparse data
            # Use 5% deviation instead of 6%, with a lower floor
            thr = max(0.5, base * 0.05)
            
            if dev >= thr:                      # vol bought -> mountain -> reject
                spikes.append({"strike": k, "iv": round(iv[k], 2), "dev": round(dev, 2)})
            elif dev <= -thr:                   # vol sold -> valley -> accelerate
                pockets.append({"strike": k, "iv": round(iv[k], 2), "dev": round(dev, 2)})
    
    spikes.sort(key=lambda x: -x["dev"])
    pockets.sort(key=lambda x: x["dev"])

    conv = _convexity(chain)
    top2 = sorted(conv, key=lambda k: -abs(conv[k]))[:2]

    # skew: OTM put IV vs OTM call IV (front month), in vol points
    puts = [iv[k] for k in strikes if spot and 0.95 <= k / spot < 0.995]
    calls = [iv[k] for k in strikes if spot and 1.005 < k / spot <= 1.05]
    skew = {"valid": False, "skew": None, "interpretation": "—"}
    if puts and calls:
        s = _median(puts) - _median(calls)
        interp = ("puts expensive — downside hedging/fear" if s > 1.0
                  else "calls expensive — upside chase" if s < -1.0
                  else "balanced wings")
        skew = {"valid": True, "skew": round(s, 2), "interpretation": interp}

    # term structure: near ATM IV vs deferred ATM IV
    term = {"valid": False, "inverted": False, "note": "—"}
    near = [c.iv for c in chain.contracts if c.iv and c.dte is not None
            and 0 <= c.dte <= 7 and spot and abs(c.strike - spot) / spot <= 0.01]
    far = [c.iv for c in chain.contracts if c.iv and c.dte is not None
           and 25 <= c.dte <= 70 and spot and abs(c.strike - spot) / spot <= 0.01]
    if near and far:
        inv = _median(near) > _median(far)
        term = {"valid": True, "inverted": inv,
                "note": ("INVERTED — near vol rich (event stress)" if inv
                         else "CONTANGO — normal upward slope")}

    return {
        "spot": spot,
        "spikes": spikes[:6],
        "pockets": pockets[:6],
        "top_convexity_levels": sorted(top2),
        "skew": skew,
        "term_structure": term,
    }

# ── route assessment (ticket runner targeting) ───────────────
def route_assessment(surface, wall_index, t2_index, dir_sign):
    lo, hi = sorted((wall_index, t2_index))
    spikes = [s["strike"] for s in surface.get("spikes", []) if lo < s["strike"] < hi]
    pockets = [p["strike"] for p in surface.get("pockets", []) if lo < p["strike"] < hi]
    if spikes:
        block = min(spikes) if dir_sign > 0 else max(spikes)
        return {"path": "SPIKE_BLOCKED", "effective_target": block,
                "reason": f"IV mountain at {block:.0f} caps the runner"}
    if pockets:
        return {"path": "POCKET_ACCELERATED", "effective_target": t2_index,
                "reason": f"vol valley at {pockets[0]:.0f} — little resistance to {t2_index:.0f}"}
    return {"path": "CLEAR", "effective_target": t2_index,
            "reason": "no vol obstacles on route"}

# ── complexity levels (derived from volatility structure) ────
def complexity_level(regime, surface, zd_level):
    """CX-1 (simple) .. CX-4 (chaotic): counts conflicting hedging forces."""
    flags, score = [], 0
    if (regime or "").upper().startswith("NEG"):
        score += 1; flags.append("negative gamma")
    tm = (surface or {}).get("term_structure") or {}
    if tm.get("inverted"):
        score += 1; flags.append("inverted term structure")
    sk = (surface or {}).get("skew") or {}
    if sk.get("valid") and abs(sk.get("skew") or 0) > 2.0:
        score += 1; flags.append("steep skew")
    if (zd_level or "LOW") in ("HIGH", "EXTREME"):
        score += 1; flags.append("0DTE " + str(zd_level).lower())
    twists = len((surface or {}).get("spikes", [])) + len((surface or {}).get("pockets", []))
    if twists >= 4:
        score += 1; flags.append("twisted surface")
    level = 1 if score <= 1 else 2 if score == 2 else 3 if score == 3 else 4
    label, playbook = {
        1: ("SIMPLE", "Calm tape — fades at walls high probability, full size."),
        2: ("NORMAL", "Standard playbook — confirmation required, standard size."),
        3: ("COMPLEX", "Conflicting forces — prefer breaks/trend, half size, skip midday."),
        4: ("CHAOTIC", "Whipsaw risk — stand aside or quarter size, A+ setups only."),
    }[level]
    return {"level": level, "label": label, "score": score,
            "flags": flags, "playbook": playbook}

# ── text brief for the AI pipeline ───────────────────────────
def _surface_brief(surface, spot=None):
    """One-paragraph vol-surface brief for LLM prompts.
    Accepts either an analyze_surface() dict or a raw chain."""
    if surface is None:
        return "Vol surface: no data."
    if hasattr(surface, "contracts"):          # a chain was passed -> analyze first
        surface = analyze_surface(surface, spot or getattr(surface, "spot", None))
    sk = surface.get("skew") or {}
    tm = surface.get("term_structure") or {}
    lines = []
    two = surface.get("top_convexity_levels") or []
    if two:
        lines.append("two lines at " + " and ".join(f"{x:,.0f}" for x in two))
    sp = surface.get("spikes") or []
    if sp:
        lines.append("IV spikes (reject zones) at "
                     + ", ".join(f"{s['strike']:,.0f}" for s in sp[:3]))
    pk = surface.get("pockets") or []
    if pk:
        lines.append("IV pockets (accel zones) at "
                     + ", ".join(f"{p['strike']:,.0f}" for p in pk[:3]))
    if sk.get("valid"):
        lines.append(f"skew {sk.get('skew'):+.2f} — {sk.get('interpretation')}")
    if tm.get("valid"):
        lines.append(f"term structure: {tm.get('note')}")
    if not lines:
        return "Vol surface: flat, no notable features."
    return "Vol surface: " + "; ".join(lines) + "."