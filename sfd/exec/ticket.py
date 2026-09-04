"""
SFD Deterministic Ticket Builder.
Principle 2 — Judgment proposes, math disposes.

Basis guard (NDX→NQ / SPX→ES translation):
  1. manual override (flowdesk.yaml basis_guard.overrides) wins
  2. live basis trusted only inside a cost-of-carry sanity band
  3. otherwise fair-value estimate, badged EST (never silent)
"""
from datetime import date
from ..assets import tick_econ
from ..quant.gex import next_major_level
from ..quant.vol_surface import route_assessment

INDEX_FUTURES = {"ES", "NQ", "RTY", "VX"}

DIRECTION_MAP = {
    ("FADE", "PUT"):   "LONG",
    ("FADE", "CALL"):  "SHORT",
    ("BREAK", "PUT"):  "SHORT",
    ("BREAK", "CALL"): "LONG",
}

DEFAULT_RUNNER_R = 5.0
DEFAULT_STOCK_STOP_PCT = 0.005

# cost-of-carry inputs for fair-value basis
RISK_FREE = 0.043
DIV_YIELD = {"NDX": 0.007, "SPX": 0.013, "RUT": 0.012, "VIX": 0.0}


def _flat_ticket(decision, wall, note):
    return {
        "direction": "FLAT", "decision": decision, "wall": wall,
        "entry": 0.0, "stop_loss": 0.0, "target_1": 0.0, "target_2": 0.0,
        "t2_source": "none",
        "stop_ticks": 0, "risk_usd": 0.0,
        "rr_target_1": 0.0, "rr_target_2": 0.0,
        "basis": 0.0, "basis_provenance": "NONE", "basis_note": note,
        "provenance": "MATH", "note": note,
    }


def get_futures_price(futures_symbol):
    import yfinance as yf
    yf_sym = futures_symbol + "=F"
    try:
        px = yf.Ticker(yf_sym).fast_info["last_price"]
        if px:
            return float(px)
    except Exception:
        pass
    try:
        h = yf.Ticker(yf_sym).history(period="1d", interval="1m")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _days_to_quad_expiry(now=None):
    """Days to the 3rd-Friday quarterly futures expiry (Mar/Jun/Sep/Dec)."""
    now = now or date.today()
    def third_friday(y, m):
        first = date(y, m, 1)
        offset = (4 - first.weekday()) % 7
        return date(y, m, 1 + offset + 14)
    y, m = now.year, now.month
    for _ in range(8):
        if m in (3, 6, 9, 12):
            exp = third_friday(y, m)
            if exp >= now:
                return (exp - now).days
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return 30


def _fair_basis(spot, asset_key):
    q = DIV_YIELD.get(asset_key, 0.01)
    T = _days_to_quad_expiry()
    return spot * (RISK_FREE - q) * T / 365.0


def compute_basis(asset, spot, settings):
    """Returns (basis, provenance, note). Guarded index→futures translation."""
    guard = settings.get("basis_guard") or {}
    key = asset["options_root"]

    # 1. manual override
    overrides = guard.get("overrides") or {}
    if key in overrides:
        b = float(overrides[key])
        return b, "OVERRIDE", f"manual override {b:+.2f}"

    fut_px = get_futures_price(asset["futures"])
    if fut_px is None:
        fb = _fair_basis(spot, key)
        return fb, "EST", "no futures quote -> fair-value estimate"

    raw = fut_px - spot
    if not guard.get("enabled", True):
        return raw, "MATH", "live basis (guard disabled)"

    # 2. sanity band
    lo = spot * guard.get("min_frac", -0.0015)
    hi = spot * guard.get("max_frac", 0.012)
    if lo <= raw <= hi:
        return raw, "MATH", "live basis within sanity band"

    # 3. fair-value fallback
    fb = _fair_basis(spot, key)
    return (fb, "EST",
            f"live basis {raw:+.1f} outside band [{lo:+.1f},{hi:+.1f}] "
            f"-> fair-value {fb:+.1f} (likely stale premarket quote)")


def round_to_tick(price, tick_size):
    if not tick_size:
        return round(price, 2)
    return round(round(price / tick_size) * tick_size, 8)


def infer_wall(gex_results):
    spot = gex_results["spot"]
    return ("PUT" if abs(spot - gex_results["put_wall"])
                   < abs(spot - gex_results["call_wall"]) else "CALL")


def build_ticket(gex_results, decision, wall, asset, settings, surface=None):
    decision = (decision or "WAIT").upper()
    wall = (wall or infer_wall(gex_results)).upper()

    if decision not in ("FADE", "BREAK"):
        return _flat_ticket(decision, wall, "No actionable edge — standing down.")

    direction = DIRECTION_MAP.get((decision, wall))
    if not direction:
        return _flat_ticket(decision, wall,
                            f"Unmapped (decision,wall)=({decision},{wall}).")

    fut = asset["futures"]
    is_stock = fut not in INDEX_FUTURES and fut == asset["options_root"]
    sign = 1 if direction == "LONG" else -1
    spot = gex_results["spot"]
    wall_index = gex_results["put_wall"] if wall == "PUT" else gex_results["call_wall"]
    profile = gex_results.get("profile", {}) or {}

    stop_ticks = int(settings.get("stop_ticks", 40))
    scale_out_ticks = int(settings.get("scale_out_ticks", 60))
    runner_r = float(settings.get("runner_r", DEFAULT_RUNNER_R))
    max_runner_r = float(settings.get("max_runner_r", 20.0))

    if is_stock:
        tick_size = 0.01
        basis, basis_prov, basis_note = 0.0, "NONE", "single-stock (no basis)"
        entry = round(wall_index, 2)
        stop_pct = float(settings.get("stock_stop_pct", DEFAULT_STOCK_STOP_PCT))
        risk_points = entry * stop_pct
        stop = round(entry - sign * risk_points, 2)
        target_1 = round(entry + sign * risk_points * (scale_out_ticks / stop_ticks), 2)
        target_2 = round(entry + sign * risk_points * runner_r, 2)
        t2_source = "fixed"
        risk_usd = round(entry * stop_pct * 100, 2)
    else:
        ticks = tick_econ(fut)
        tick_size = ticks["tick_size"]
        tick_value = ticks["tick_value"]

        basis, basis_prov, basis_note = compute_basis(asset, spot, settings)
        entry = round_to_tick(wall_index + basis, tick_size)
        risk_points = stop_ticks * tick_size
        stop = round_to_tick(entry - sign * risk_points, tick_size)
        target_1 = round_to_tick(entry + sign * (scale_out_ticks * tick_size), tick_size)

        # Wall-distance runner targeting
        dir_sign = 1 if direction == "LONG" else -1
        t2_index = next_major_level(profile, wall_index, dir_sign,
                                    min_distance=risk_points, max_range_pct=0.03)
        if t2_index is not None:
            target_2 = round_to_tick(t2_index + basis, tick_size)
            t2_source = "wall"
        else:
            target_2 = round_to_tick(entry + sign * (runner_r * stop_ticks) * tick_size, tick_size)
            t2_source = "fixed"

        # Vol-surface route refinement
        if surface and t2_index is not None:
            route = route_assessment(surface, wall_index, t2_index, dir_sign)
            if route["path"] == "SPIKE_BLOCKED":
                blocked_at = route["effective_target"]
                if abs(blocked_at - wall_index) > risk_points:
                    target_2 = round_to_tick(blocked_at + basis, tick_size)
                    t2_source = "spike-capped"
            elif route["path"] == "POCKET_ACCELERATED":
                t2_source = "wall+pocket"

        # cap the runner
        max_t2_dist = max_runner_r * risk_points
        if abs(target_2 - entry) > max_t2_dist:
            target_2 = round_to_tick(entry + sign * max_t2_dist, tick_size)
            t2_source = "capped"
        risk_usd = round(stop_ticks * tick_value, 2)

    dist_pct = abs(spot - wall_index) / spot * 100
    rr1 = abs(target_1 - entry) / risk_points if risk_points else 0
    rr2 = abs(target_2 - entry) / risk_points if risk_points else 0

    return {
        "direction": direction, "decision": decision, "wall": wall,
        "instrument": fut, "mode": "stock" if is_stock else "futures",
        "entry": entry, "stop_loss": stop,
        "target_1": target_1, "target_2": target_2,
        "t2_source": t2_source,
        "stop_ticks": stop_ticks,
        "scale_out_ticks": scale_out_ticks,
        "risk_usd": risk_usd,
        "rr_target_1": round(rr1, 2), "rr_target_2": round(rr2, 2),
        "basis": round(basis, 2),
        "basis_provenance": basis_prov,
        "basis_note": basis_note,
        "distance_to_wall_pct": round(dist_pct, 3),
        "actionable": dist_pct <= 0.5,
        "provenance": "MATH",
        "note": "",
    }