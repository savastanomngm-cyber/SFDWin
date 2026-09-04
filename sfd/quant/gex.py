"""
SFD GEX Engine v2.3 — vectorized Black-Scholes core + multi-expiry ladder.
Architecture ported from 0DTE-dealer-gamma: raw_gex = OI × Γ × multiplier × S² × 0.01,
signed by type; all Greeks computed vectorized (NumPy, no loops, no scipy).

v2.3: Wall detection capped to near-term expiries (max_dte). Far-dated OI
      (quarterlies/LEAPS) concentrates at round strikes and drags walls away
      from spot — it's structural positioning, not intraday hedging flow.
v2.2: Multi-expiry ladder, gamma zones, wall strength.
v2.1: Robust zero-gamma (flip) level.

Public interface:
    GEXEngine(multiplier=...).calculate(chain, max_dte=30) -> dict
    next_major_level(profile, wall, dir_sign) -> next significant GEX level
"""
import numpy as np

R_DEF, Q_DEF = 0.043, 0.015   # matches SFD fair-basis / dividend assumptions

LADDER_HORIZONS = (0, 1, 7, 14, 30)   # cumulative DTE horizons


def _pdf(x):
    return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)


def _cdf(x):
    ax = np.abs(x)
    t = 1.0 / (1.0 + 0.2316419 * ax)
    p = (_pdf(ax) * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
         + t * (-1.821255978 + t * 1.330274429)))))
    return np.where(x >= 0, 1.0 - p, p)


def _bs_gamma(S, K, T, iv, r=R_DEF, q=Q_DEF):
    """Vectorized Black-Scholes gamma (dividend-yield aware)."""
    T = np.maximum(T, 1e-8)
    iv = np.maximum(iv, 1e-8)
    sq = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * iv * iv) * T) / sq
    return np.exp(-q * T) * _pdf(d1) / (S * sq)


def _zero_gamma_level(ks, net, cum, spot, put_wall, call_wall):
    """Robust zero-gamma (flip) level from cumulative signed GEX."""
    candidates = []
    sgn = np.sign(cum)
    cross_idx = np.where(np.diff(sgn) != 0)[0]
    for j in cross_idx:
        y0, y1 = cum[j], cum[j + 1]
        if y1 != y0:
            x = ks[j] + (0.0 - y0) * (ks[j + 1] - ks[j]) / (y1 - y0)
            candidates.append(float(x))
    for j in np.where(cum == 0)[0]:
        candidates.append(float(ks[j]))
    flip = None
    if candidates:
        if put_wall is not None and call_wall is not None:
            lo, hi = min(put_wall, call_wall), max(put_wall, call_wall)
            inside = [x for x in candidates if lo <= x <= hi]
            if inside:
                candidates = inside
        flip = min(candidates, key=lambda x: abs(x - spot))
    elif len(ks):
        flip = float(ks[int(np.argmin(np.abs(cum)))])
    if flip is not None and not (0.75 * spot <= flip <= 1.25 * spot):
        flip = None
    return flip


class GEXEngine:
    def __init__(self, multiplier=100, r=R_DEF, q=Q_DEF):
        self.multiplier = multiplier
        self.r = r
        self.q = q

    @staticmethod
    def _stats_for_mask(Ka, signed, mask, spot):
        """Reduce a contract-mask to wall/flip/net stats (vectorized)."""
        if not mask.any():
            return {"net_gex": 0.0, "put_wall": None, "call_wall": None,
                    "flip_point": None}
        ks_h, inv_h = np.unique(Ka[mask], return_inverse=True)
        net_h = np.zeros(len(ks_h))
        np.add.at(net_h, inv_h, signed[mask])
        i_p, i_c = int(np.argmin(net_h)), int(np.argmax(net_h))
        pw = float(ks_h[i_p]) if net_h[i_p] < 0 else None
        cw = float(ks_h[i_c]) if net_h[i_c] > 0 else None
        flip = _zero_gamma_level(ks_h, net_h, np.cumsum(net_h), spot, pw, cw)
        return {"net_gex": float(net_h.sum()), "put_wall": pw,
                "call_wall": cw, "flip_point": flip}

    def calculate(self, chain, max_dte=30):                    # ← CHANGED: added max_dte=30
        S = float(chain.spot)
        K, T, DTE, IV, OI, CALL = [], [], [], [], [], []
        for c in chain.contracts:
            if c.dte is None or c.dte < 0:
                continue
            if c.dte > max_dte:                                 # ← CHANGED: cap to near-term
                continue
            oi = c.open_interest or 0
            if not oi:
                continue
            iv = c.iv / 100.0 if c.iv and c.iv > 1 else (c.iv or 0)
            if not iv:
                continue
            K.append(float(c.strike)); T.append(c.dte / 365.0)
            DTE.append(float(c.dte))
            IV.append(float(iv)); OI.append(float(oi))
            CALL.append(c.contract_type == "call")

        empty = {"spot": S, "put_wall": None, "call_wall": None,
                 "flip_point": None, "zero_gamma_level": None,
                 "flip_distance_pct": None, "gamma_zone": "UNKNOWN",
                 "put_wall_strength": None, "call_wall_strength": None,
                 "regime": "NEGATIVE", "net_gex": 0.0, "profile": {},
                 "wall_dte": None, "ladder": {}}
        if not K:
            return empty

        Ka = np.array(K); Ta = np.array(T); IVa = np.array(IV)
        DTEa = np.array(DTE)
        OIa = np.array(OI); Ca = np.array(CALL, bool)

        # vectorized greeks — the <1ms core
        gamma = _bs_gamma(S, Ka, Ta, IVa, self.r, self.q)
        raw = OIa * gamma * self.multiplier * S * S * 0.01   # $ gamma / 1% move
        signed = np.where(Ca, raw, -raw)                     # calls +, puts −

        ks, inv = np.unique(Ka, return_inverse=True)
        net = np.zeros(len(ks))
        np.add.at(net, inv, signed)
        profile = {float(k): float(v) for k, v in zip(ks, net)}

        net_gex = float(net.sum())
        i_p, i_c = int(np.argmin(net)), int(np.argmax(net))
        put_wall = float(ks[i_p]) if net[i_p] < 0 else None
        call_wall = float(ks[i_c]) if net[i_c] > 0 else None

        # wall strength: wall |GEX| relative to the strongest strike
        max_abs = float(np.abs(net).max()) if len(net) else 0.0
        put_wall_strength = (round(abs(float(net[i_p])) / max_abs, 3)
                             if (max_abs > 0 and net[i_p] < 0) else None)
        call_wall_strength = (round(abs(float(net[i_c])) / max_abs, 3)
                              if (max_abs > 0 and net[i_c] > 0) else None)

        # zero-gamma level — robust multi-crossing handling (v2.1)
        cum = np.cumsum(net)
        flip = _zero_gamma_level(ks, net, cum, S, put_wall, call_wall)

        flip_distance_pct = (round((flip - S) / S * 100.0, 3)
                             if flip is not None else None)

        # gamma zone: net regime × spot-vs-flip position
        if flip is None:
            gamma_zone = "UNKNOWN"
        elif net_gex > 0 and S >= flip:
            gamma_zone = "PIN"           # walls hold — fade reactions
        elif net_gex > 0 and S < flip:
            gamma_zone = "MIXED"         # positive book, spot under flip — caution
        elif net_gex < 0 and S < flip:
            gamma_zone = "VOLATILE"      # walls break — momentum favored
        else:
            gamma_zone = "TRANSITION"    # negative book, spot above flip — unstable

        # ── multi-expiry ladder (cumulative horizons + band GEX) ───
        ladder = {}
        prev_h = -1.0
        for h in LADDER_HORIZONS:
            cum_mask = DTEa <= h
            band_mask = (DTEa > prev_h) & (DTEa <= h)
            stats = self._stats_for_mask(Ka, signed, cum_mask, S)
            stats["band_gex"] = (float(signed[band_mask].sum())
                                 if band_mask.any() else 0.0)
            ladder[str(h)] = stats
            prev_h = h
        ladder["all"] = {"net_gex": net_gex, "band_gex": net_gex,
                         "put_wall": put_wall, "call_wall": call_wall,
                         "flip_point": flip}

        return {"spot": S, "put_wall": put_wall, "call_wall": call_wall,
                "flip_point": flip, "zero_gamma_level": flip,
                "flip_distance_pct": flip_distance_pct,
                "gamma_zone": gamma_zone,
                "put_wall_strength": put_wall_strength,
                "call_wall_strength": call_wall_strength,
                "regime": "POSITIVE" if net_gex > 0 else "NEGATIVE",
                "net_gex": net_gex, "profile": profile, "wall_dte": None,
                "ladder": ladder}


# ── backward-compat helper used by exec/ticket.py (T2 runner target) ──
def next_major_level(profile, wall=0.0, dir_sign=1, **_):
    """Next significant GEX level beyond `wall` in the trade direction."""
    if isinstance(profile, dict) and "profile" in profile and isinstance(profile["profile"], dict):
        profile = profile["profile"]
    if not profile:
        return None
    if isinstance(dir_sign, str):
        s = 1 if dir_sign.upper() in ("LONG", "UP", "BUY", "1", "+1") else -1
    else:
        try:
            s = 1 if (dir_sign is None or float(dir_sign) >= 0) else -1
        except Exception:
            s = 1
    base = float(wall or 0.0)
    absd = {float(k): abs(float(v)) for k, v in profile.items()}
    maxabs = max(absd.values(), default=0.0) or 1.0
    major_thr = 0.25 * maxabs
    ahead = sorted((k for k in absd if (k > base if s >= 0 else k < base)),
                   key=lambda k: (k - base) * s)
    if not ahead:
        return None
    for k in ahead:
        if absd[k] >= major_thr:
            return float(k)
    return float(ahead[0])