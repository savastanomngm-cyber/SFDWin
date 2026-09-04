"""
SFD Flow Engine v2 — dual-channel (multi-DTE + 0DTE isolation)
+ aggressor-classified order flow.

volatilityfullscript.txt (Freddy Sento):
  "zero DTE options now represent 60 to 70% of all S&P daily options volume."
  "these guys are driving a Formula One car... the amount of money they need
   to hedge in the market is huge."
  Delta profile: 5 hours to expiry = soft curve; 1 hour = exponential.
  Theta decay is terminal at 4pm. 0DTE is the intraday violence engine.

Multi-DTE channel (0-14 DTE): positional flow, sweeps, PCR — weight 0.5
0DTE channel (dte == 0):      the F1 car, isolated, own sweep detection — weight 0.3
Wall concentration:           volume loading at the walls — weight 0.2

v2 adds (no weight changes — new fields are surfaced, not silently blended):
  - Aggressor side estimation (Lee-Ready quote rule: last vs bid/ask).
    Provenance: EST — a quote-rule estimate on delayed feeds, stated openly.
  - Wall aggression imbalance: net BUYING at a wall = break fuel
    (call wall -> BREAKOUT, put wall -> BREAKDOWN); net SELLING = hold fuel.
  - Vol/OI churn at the walls (volume exceeding open interest = fresh positions).
  - Large-volume block proxy (daily volume >= LARGE_VOLUME_MIN).
"""
import time
import numpy as np
from .. import store

MAX_DTE = 14
SWEEP_THRESHOLD = 5.0
SWEEP_MIN_CONTRACTS = 500
STALE_PRIOR_SECS = 2 * 3600
# 0DTE sweep: more sensitive — a single-strike volume >3x the 0DTE average
# AND >1000 contracts flags aggressive expiring-flow conviction.
ZDTE_SWEEP_MIN = 1000
ZDTE_SWEEP_MULT = 3.0
# v2: order-flow additions
LARGE_VOLUME_MIN = 5000     # daily-volume proxy for block activity (delayed data)
AGGR_MIN_IMBALANCE = 0.20   # report wall aggression only above this |imbalance|


def _contract_key(c):
    return f"{c.expiry}|{c.strike}|{c.contract_type}"


def capture_volume_snapshot(chain, max_dte=MAX_DTE):
    snap = {}
    for c in chain.contracts:
        if c.dte is None or c.dte < 0 or c.dte > max_dte:
            continue
        snap[_contract_key(c)] = {"volume": int(c.volume or 0),
                                  "open_interest": int(c.open_interest or 0)}
    return {"ts": time.time(), "spot": chain.spot, "contracts": snap}


def _pcr_signal(chain, max_dte=MAX_DTE):
    call_vol = put_vol = 0
    for c in chain.contracts:
        if c.dte is None or c.dte < 0 or c.dte > max_dte:
            continue
        if c.contract_type == "call":
            call_vol += c.volume or 0
        else:
            put_vol += c.volume or 0
    if call_vol + put_vol == 0:
        return 0.0, {}
    pcr = put_vol / max(call_vol, 1)
    return max(-1.0, min(1.0, 1.0 - pcr)), \
           {"pcr": round(pcr, 3), "call_vol": call_vol, "put_vol": put_vol}


# ── v2: aggressor classification (EST quality on delayed feeds) ──────
def _aggressor_side(c):
    """Classify a contract's flow as buyer- or seller-initiated using the
    quote rule: last >= ask -> buy; last <= bid -> sell; else unknown.
    Returns 'buy' | 'sell' | None."""
    last, bid, ask = c.last, c.bid, c.ask
    if last is None or bid is None or ask is None:
        return None
    if last <= 0 or ask <= bid:
        return None
    if last >= ask:
        return "buy"
    if last <= bid:
        return "sell"
    return None


def _imbalance(buy, sell):
    tot = buy + sell
    return (buy - sell) / tot if tot else 0.0


def _wall_concentration(chain, gex_results, band_pct=0.0025, max_dte=MAX_DTE):
    """Volume, OI, vol/OI and aggressor-classified flow in a ±band around
    each wall."""
    spot = gex_results.get("spot") or 0.0
    pw, cw = gex_results.get("put_wall"), gex_results.get("call_wall")
    out = {"call_wall_vol": 0, "put_wall_vol": 0,
           "call_wall_oi": 0, "put_wall_oi": 0,
           "call_wall_buy": 0, "call_wall_sell": 0,
           "put_wall_buy": 0, "put_wall_sell": 0,
           "call_wall_vol_oi": None, "put_wall_vol_oi": None}
    if not spot:
        return out
    for c in chain.contracts:
        if c.dte is None or c.dte < 0 or c.dte > max_dte:
            continue
        near_call = (cw is not None and c.contract_type == "call"
                     and abs(c.strike - cw) / spot <= band_pct)
        near_put = (pw is not None and c.contract_type == "put"
                    and abs(c.strike - pw) / spot <= band_pct)
        if not (near_call or near_put):
            continue
        vol = int(c.volume or 0)
        oi = int(c.open_interest or 0)
        side = _aggressor_side(c)
        if near_call:
            out["call_wall_vol"] += vol
            out["call_wall_oi"] += oi
            if side == "buy":
                out["call_wall_buy"] += vol
            elif side == "sell":
                out["call_wall_sell"] += vol
        if near_put:
            out["put_wall_vol"] += vol
            out["put_wall_oi"] += oi
            if side == "buy":
                out["put_wall_buy"] += vol
            elif side == "sell":
                out["put_wall_sell"] += vol
    if out["call_wall_oi"]:
        out["call_wall_vol_oi"] = round(out["call_wall_vol"] / out["call_wall_oi"], 3)
    if out["put_wall_oi"]:
        out["put_wall_vol_oi"] = round(out["put_wall_vol"] / out["put_wall_oi"], 3)
    return out


def _aggression_text(call_vol, call_aggr, put_vol, put_aggr):
    """Human-readable wall-aggression line. Convention:
    net BUYING at a wall = break fuel (call->BREAKOUT, put->BREAKDOWN);
    net SELLING at a wall = hold fuel (call->FADE, put->BID)."""
    parts = []
    if call_vol and abs(call_aggr) >= AGGR_MIN_IMBALANCE:
        fuel = "BREAKOUT fuel" if call_aggr > 0 else "FADE fuel"
        parts.append(f"call-wall aggression {call_aggr:+.2f} → {fuel}")
    if put_vol and abs(put_aggr) >= AGGR_MIN_IMBALANCE:
        fuel = "BREAKDOWN fuel" if put_aggr > 0 else "BID fuel"
        parts.append(f"put-wall aggression {put_aggr:+.2f} → {fuel}")
    return " ".join(parts)


def _churn_text(wc):
    parts = []
    if wc["call_wall_vol_oi"] and wc["call_wall_vol_oi"] > 1.0:
        parts.append(f"vol>OI churn at call wall ({wc['call_wall_vol_oi']:.2f}x)")
    if wc["put_wall_vol_oi"] and wc["put_wall_vol_oi"] > 1.0:
        parts.append(f"vol>OI churn at put wall ({wc['put_wall_vol_oi']:.2f}x)")
    return " ".join(parts)


# ── 0DTE CHANNEL (the F1 car) ────────────────────────────────
def compute_zero_dte_channel(chain):
    """
    Isolate dte == 0 contracts. Separate conviction, separate sweep detection,
    and an intensity gauge (0DTE volume / total volume).
    """
    zero = [c for c in chain.contracts if c.dte is not None and c.dte == 0]
    all_vol = sum((c.volume or 0) for c in chain.contracts)

    if not zero:
        return {"valid": False, "intensity": 0.0,
                "note": "no 0DTE contracts in chain (market closed or data lag)"}

    call_vol = sum((c.volume or 0) for c in zero if c.contract_type == "call")
    put_vol = sum((c.volume or 0) for c in zero if c.contract_type == "put")
    total = call_vol + put_vol
    intensity = total / max(all_vol, 1)
    pcr = put_vol / max(call_vol, 1)

    # Near-money 0DTE strikes (within 2% of spot)
    spot = chain.spot or 1.0
    near_money = [c for c in zero
                  if 0.98 <= (c.strike / spot) <= 1.02 and (c.volume or 0) > 0]
    vols = [c.volume or 0 for c in near_money]
    avg_vol = float(np.mean(vols)) if vols else 0

    # 0DTE sweep: single strike volume > 3x average AND > 1000 contracts
    sweeps = [c for c in near_money
              if avg_vol > 0
              and (c.volume or 0) >= ZDTE_SWEEP_MIN
              and (c.volume or 0) >= ZDTE_SWEEP_MULT * avg_vol]
    sweep_side = None
    if sweeps:
        best = max(sweeps, key=lambda c: c.volume or 0)
        sweep_side = "CALL" if best.contract_type == "call" else "PUT"

    # Conviction: call-heavy 0DTE = bullish fuel; put-heavy = bearish fuel
    conv = max(-1.0, min(1.0, (call_vol - put_vol) / max(total, 1)))

    return {
        "valid": True,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "total_volume": total,
        "pcr": round(pcr, 3),
        "intensity": round(intensity, 4),
        "conviction": round(conv, 3),
        "near_money_strikes": len(near_money),
        "sweep_count": len(sweeps),
        "sweep_side": sweep_side,
        "interpretation": _0dte_interp(intensity, pcr, len(sweeps), sweep_side),
    }


def _0dte_interp(intensity, pcr, sweep_count, sweep_side):
    if sweep_count > 0:
        return (f"⚡ 0DTE SWEEP ({sweep_side}) — aggressive expiring-flow conviction. "
                f"F1 car is running hot.")
    if intensity > 0.50:
        hot = "EXTREME"
    elif intensity > 0.30:
        hot = "HIGH"
    elif intensity > 0.15:
        hot = "MODERATE"
    else:
        hot = "LOW"
    skew = "call-heavy" if pcr < 0.85 else "put-heavy" if pcr > 1.15 else "balanced"
    return (f"0DTE intensity {hot} ({intensity*100:.0f}% of total volume). "
            f"PCR {pcr:.2f} ({skew}). Theta terminal at 16:00 ET.")


# ── MAIN compute_flow (dual-channel + order flow) ─────────────
def compute_flow(chain, gex_results, asset_key):
    current = capture_volume_snapshot(chain)
    prior = store.latest_flow_snapshot(asset_key, current["ts"])
    stale = prior and (current["ts"] - prior["ts"] > STALE_PRIOR_SECS)

    pcr_score, pcr_detail = _pcr_signal(chain)
    wc = _wall_concentration(chain, gex_results)
    call_wall_vol, put_wall_vol = wc["call_wall_vol"], wc["put_wall_vol"]
    wall_total = call_wall_vol + put_wall_vol
    wall_score = ((call_wall_vol - put_wall_vol) / wall_total) if wall_total else 0.0

    # v2: aggressor imbalance at the walls (EST — delayed-quote rule)
    call_aggr = _imbalance(wc["call_wall_buy"], wc["call_wall_sell"])
    put_aggr = _imbalance(wc["put_wall_buy"], wc["put_wall_sell"])

    # v2: large-volume prints (block proxy on delayed data)
    large = sorted((int(c.volume or 0) for c in chain.contracts
                    if c.dte is not None and 0 <= c.dte <= MAX_DTE
                    and (c.volume or 0) >= LARGE_VOLUME_MIN),
                   reverse=True)

    # 0DTE channel (always computed from current chain — no prior needed)
    zero_dte = compute_zero_dte_channel(chain)

    result = {
        "conviction": 0.0,
        "sweep": False,
        "pcr": pcr_detail.get("pcr"),
        "call_wall_volume": call_wall_vol,
        "put_wall_volume": put_wall_vol,
        "wall_aggression": {"call": round(call_aggr, 3),
                            "put": round(put_aggr, 3)},
        "wall_vol_oi": {"call": wc["call_wall_vol_oi"],
                        "put": wc["put_wall_vol_oi"]},
        "large_volume": {"count": len(large),
                         "max": large[0] if large else 0},
        "aggressor_quality": "EST — last-vs-quote rule on delayed feed",
        "has_prior_snapshot": bool(prior and not stale),
        "zero_dte": zero_dte,
        "summary": "",
    }

    extra = " ".join(x for x in (
        _aggression_text(call_wall_vol, call_aggr, put_wall_vol, put_aggr),
        _churn_text(wc)) if x)

    # ── Seed path ─────────────────────────────────────────────
    if not prior or stale:
        result["conviction"] = round(pcr_score * 0.6, 2)
        result["summary"] = (
            f"No usable prior snapshot (seeded). PCR-based read only: "
            f"PCR {pcr_detail.get('pcr', 'n/a')} "
            f"({pcr_detail.get('call_vol', 0):,} call vol / "
            f"{pcr_detail.get('put_vol', 0):,} put vol, score {pcr_score:+.2f}). "
            f"Wall loading: {call_wall_vol:,} at call wall vs {put_wall_vol:,} at put wall "
            f"(score {wall_score:+.2f}). {extra} "
            f"Snapshot saved — next run computes real deltas.")
        store.save_flow_snapshot(asset_key, current)
        return result, current

    # ── Delta path ────────────────────────────────────────────
    call_delta = put_delta = 0
    deltas = []
    for key, cur in current["contracts"].items():
        prev = prior["contracts"].get(key)
        if not prev:
            continue
        d = cur["volume"] - prev["volume"]
        if d <= 0:
            continue
        deltas.append(d)
        if key.split("|")[2] == "call":
            call_delta += d
        else:
            put_delta += d

    total_delta = call_delta + put_delta
    delta_score = ((call_delta - put_delta) / total_delta) if total_delta else 0.0

    sweep = False
    if deltas:
        avg_d = sum(deltas) / len(deltas)
        max_d = max(deltas)
        if avg_d > 0 and max_d / avg_d >= SWEEP_THRESHOLD and max_d >= SWEEP_MIN_CONTRACTS:
            sweep = True

    # ── Composite: multi-DTE deltas + 0DTE conviction + wall ─
    # (weights unchanged from v1 — new fields surfaced, not blended)
    zdte_conv = zero_dte.get("conviction", 0.0) if zero_dte.get("valid") else 0.0
    conviction = (0.5 * delta_score
                  + 0.3 * zdte_conv          # 0DTE replaces raw PCR weight
                  + 0.2 * wall_score)

    elapsed_min = (current["ts"] - prior["ts"]) / 60
    result.update({
        "conviction": round(max(-1.0, min(1.0, conviction)), 2),
        "sweep": sweep or zero_dte.get("sweep_count", 0) > 0,
        "call_delta": call_delta,
        "put_delta": put_delta,
        "elapsed_min": round(elapsed_min, 1),
    })
    result["summary"] = (
        f"{'⚡ SWEEP DETECTED. ' if result['sweep'] else ''}"
        f"Δvolume over {elapsed_min:.1f}min: +{call_delta:,} calls vs +{put_delta:,} puts "
        f"(delta score {delta_score:+.2f}). "
        f"Wall loading: {call_wall_vol:,} call / {put_wall_vol:,} put (score {wall_score:+.2f}). "
        f"{extra} "
        f"{zero_dte.get('interpretation', '')} "
        f"Composite conviction: {result['conviction']:+.2f}.")

    store.save_flow_snapshot(asset_key, current)
    return result, current