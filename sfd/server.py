"""
SFD Chart Terminal — FastAPI server
(v4 + WALL INTERPRETER v1 + OI ANCHOR v2 + FLIP SANITY + REAL-TIME SENTINEL v4).
🟢 INTERPRETER:  /api/interpreter + /api/interpreter_stream + /api/interpreter_test
🟢 OI ANCHOR v2: EOD-OI anchored walls + geometry guards + /api/oi_anchor
🟢 FLIP SANITY:  _sanitize_walls() — nearest-to-spot zero-crossing + zone re-derive
🟢 RT SENTINEL:  _fresh_spot() v4 — LADDER-FIRST (same feed as chart) → Tradovate →
                 TV scanner (corrected payload, NASDAQ:NDX) → stale kill switch
🟢 SYNC FIX:     sentinel always tracks 24h futures ladder; distances vs translated walls
"""
from dotenv import load_dotenv
load_dotenv()   # reads .env from the folder you run the command in
import time, math, asyncio, threading, traceback, functools, json
import urllib.request
from pathlib import Path
import numpy as np, pandas as pd, uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from .data import opra
from .quant.gex import GEXEngine
from .quant.vol_surface import analyze_surface
from .quant.mkt_calendar import opex_status
from .quant.session_clock import session_phase
from .quant.momentum import get_tape_speed
from .quant.tvlevels import build_tv_string, fetch_open_atr
from .agents.pipeline import run_intraday_pipeline
from .exec.logger import log_verdict
from .exec.confirmation import ConfirmationMonitor
from .exec.notify import alert, telegram_enabled
from .assets import get_asset, active_asset_key, index_yf_symbol, list_assets
from . import store, config
try: from . import bus
except ImportError: bus = None
try: from .agents.wall_interpreter import INTERPRETER
except ImportError: INTERPRETER = None
try: from .data import oi_anchor
except ImportError: oi_anchor = None

WEB = Path(__file__).resolve().parent / "web"
RTH_PHASES = ("OPEN_DRIVE", "MIDDAY", "THETA_BURN", "POWER_HOUR")

app = FastAPI(title="SFD Chart Terminal")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache = {}
_chain_cache = {}
_chain_fetch_lock = threading.Lock()

def _fetch_chain_once(symbol, ttl=60):
    now = time.time()
    hit = _chain_cache.get(symbol)
    if hit and now - hit[0] < ttl: return hit[1]
    with _chain_fetch_lock:
        hit = _chain_cache.get(symbol)
        if hit and time.time() - hit[0] < ttl: return hit[1]
        chain = opra.get_chain(symbol)
        _chain_cache[symbol] = (time.time(), chain)
        return chain

def _get_chain_cached_sync(symbol, ttl=60): return _fetch_chain_once(symbol, ttl)
async def _get_chain_cached(symbol, ttl=60): return await asyncio.to_thread(_fetch_chain_once, symbol, ttl)

def _cached(key, fn, ttl):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl: return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val

def _resolve(symbol):
    if symbol:
        try: return get_asset(symbol.upper())
        except Exception: pass
    return get_asset(active_asset_key())

def _session_mode(): return "RTH" if session_phase().get("phase", "") in RTH_PHASES else "NQ_ONLY"

async def _mode_spot(asset):
    mode = _session_mode()
    if mode == "RTH":
        px = await asyncio.to_thread(_fetch_index_spot, index_yf_symbol(asset), asset)
        return px, "index (live)"
    px = await asyncio.to_thread(_fetch_futures_spot, asset)
    return px, "futures (live)"

def _fetch_index_spot(index_yf, asset=None):
    if asset is not None:
        try:
            from .data.spot import get_spot
            r = get_spot(asset, "index")
            if r and r.get("price"): return r["price"]
        except Exception: pass
    import yfinance as yf
    try:
        px = yf.Ticker(index_yf).fast_info["last_price"]
        if px: return float(px)
    except Exception: pass
    try:
        h = yf.Ticker(index_yf).history(period="1d", interval="1m")
        if not h.empty: return float(h["Close"].iloc[-1])
    except Exception: pass
    return None

def _fetch_futures_spot(asset):
    try:
        from .data.spot import get_spot
        r = get_spot(asset, "futures")
        if r and r.get("price"): return r["price"]
    except Exception: pass
    import yfinance as yf
    try:
        px = yf.Ticker(asset["futures"] + "=F").fast_info["last_price"]
        if px: return float(px)
    except Exception: pass
    return None

# ──  FRESH-SPOT LAYER v4: ladder-first, corrected scanner, stale-safe ──
SPOT_META = {"age": None, "src": None, "stale_warned": False}

# Real-time proxies. NASDAQ:NDX confirmed working on /america/ scanner.
_RT_PROXY = {
    "NQ": ["NASDAQ:NDX", "TVC:NDX", "INDEX:NDX"],
    "ES": ["NASDAQ:SPX", "TVC:INX", "INDEX:SPX", "OANDA:SPX500"],
}

def _tv_scan_quote(symbols):
    """TradingView scanner. Corrected payload (nested tickers + columns)."""
    if isinstance(symbols, str): symbols = [symbols]
    for sym in symbols:
        prefix = sym.split(":")[0].upper() if ":" in sym else ""
        if prefix in ("OANDA", "CAPITALCOM", "FOREXCOM", "PEPPERSTONE", "FXCM"):
            scanners = ["cfd", "forex", "america"]
        elif prefix in ("TVC", "INDEX", "SP", "DJI", "NASDAQ"):
            scanners = ["america", "index", "cfd"]
        elif prefix in ("CME", "NYMEX", "CBOT", "COMEX"):
            scanners = ["america", "cfd"]
        else:
            scanners = ["america", "cfd", "forex"]
        for scanner in scanners:
            try:
                body = json.dumps({
                    "symbols": {"tickers": [sym]},
                    "columns": ["close", "timestamp"]
                }).encode()
                req = urllib.request.Request(
                    f"https://scanner.tradingview.com/{scanner}/scan",
                    data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=4) as r:
                    d = json.loads(r.read())
                data = d.get("data") or []
                if data and data[0].get("d"):
                    row = data[0]["d"]
                    px = float(row[0])
                    ts = float(row[1]) if len(row) > 1 and row[1] and float(row[1]) > 1e9 else time.time()
                    return px, ts, sym
            except Exception:
                continue
    return None, None, None

def _fresh_spot(asset, kind, max_age=90):
    """(price, age_s, source).
    🟢 v4: LADDER FIRST — the same accurate 24h feed your Futures chart uses.
    Scanner cash-index proxies are RTH-only redundancy; yfinance = stale."""
    now = time.time()
    fut = asset.get("futures", "")
    # 0) Tradovate — exact real-time CME print (if configured & fresh)
    if fut in ("NQ", "ES"):
        try:
            from .data import tradovate_feed as tvf
            q = tvf.last_quote(fut)
            if q and (time.time() - q["ts"]) < 10:
                age = time.time() - q["ts"]
                SPOT_META.update(age=age, src="TRADOVATE:" + q["sym"])
                return q["price"], age, "TRADOVATE:" + q["sym"]
        except Exception:
            pass
    # 1) CHART-TERMINAL LADDER — same source the Futures chart tail merges.
    try:
        from .data.spot import get_spot
        r = get_spot(asset, kind)
        if r and r.get("price"):
            src = str(r.get("source", "ladder"))
            if "yfinance" not in src.lower():
                SPOT_META.update(age=0.0, src="LADDER:" + src)
                return float(r["price"]), 0.0, "LADDER:" + src
    except Exception:
        pass
    # 2) TV scanner cash-index — RTH redundancy ONLY (frozen outside RTH)
    if kind == "index":
        px, ts, sym = _tv_scan_quote(_RT_PROXY.get(fut, ["NASDAQ:NDX", "TVC:NDX"]))
        if px:
            age = max(0.0, now - ts) if ts else 0.0
            if age < 30:
                SPOT_META.update(age=age, src="TV-RT:" + sym)
                return px, age, "TV-RT:" + sym
    # 3) yfinance — stale by definition (kill switch pauses the sentinel)
    px = (_fetch_index_spot(index_yf_symbol(asset), asset) if kind == "index"
          else _fetch_futures_spot(asset))
    if px:
        SPOT_META.update(age=900.0, src="YF-DELAYED")
        return float(px), 900.0, "YF-DELAYED"
    return None, None, None

# ── live spot sampler ─────────────────────────────────────────
_live_lock = threading.Lock()
_live_samples = {}
_live_keys = {}
_LIVE_BUCKETS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

def _sample_live(asset, source):
    kind = "futures" if source == "futures" else "index"
    try:
        px, age, src = _fresh_spot(asset, kind)
        if px and (age is None or age < 60):
            key = (asset["options_root"], source)
            with _live_lock:
                buf = _live_samples.setdefault(key, [])
                buf.append((time.time(), float(px)))
                cutoff = time.time() - 7200
                while buf and buf[0][0] < cutoff: buf.pop(0)
    except Exception: pass

def _register_live(asset, source):
    with _live_lock: _live_keys[(asset["options_root"], source)] = time.time()
    _sample_live(asset, source)

def _live_sampler_loop():
    while True:
        time.sleep(10)
        with _live_lock: active = [k for k, last in _live_keys.items() if time.time() - last < 600]
        for root, source in active:
            try: _sample_live(get_asset(root), source)
            except Exception: pass

threading.Thread(target=_live_sampler_loop, daemon=True).start()

# ──  FLIP/ZONE SANITY LAYER ──────────────────────────────────
def _nearest_crossing_flip(profile, sp):
    try:
        pts = sorted((float(k), v) for k, v in (profile or {}).items() if v)
    except Exception:
        return None
    best = None
    for (k1, v1), (k2, v2) in zip(pts, pts[1:]):
        if (v1 < 0 < v2) or (v2 < 0 < v1):
            denom = abs(v1) + abs(v2)
            t = (abs(v1) / denom) if denom else 0.5
            x = k1 + t * (k2 - k1)
            if best is None or abs(x - sp) < abs(best - sp):
                best = x
    return best

def _sanitize_walls(g):
    try:
        sp = float(g.get("spot") or 0)
        pw, cw = g.get("put_wall"), g.get("call_wall")
        fp = g.get("flip_point")
        if not (sp and pw and cw):
            return g
        ok = fp and (pw <= fp <= cw) and abs(fp - sp) / sp <= 0.06
        if not ok:
            fix = _nearest_crossing_flip(g.get("profile"), sp)
            fp = round(fix, 2) if (fix is not None and pw <= fix <= cw) \
                 else round(pw + (cw - pw) * 0.5, 2)
            g["flip_point"] = fp
            g["zero_gamma_level"] = fp
            g["flip_sanitized"] = True
        g["flip_distance_pct"] = round((fp - sp) / sp * 100, 3)
        reg = g.get("regime")
        g["gamma_zone"] = ("PIN" if sp >= fp else "MIXED") if reg == "POSITIVE" \
            else ("TRANSITION" if sp >= fp else "VOLATILE")
    except Exception:
        pass
    return g

# ── 🟢 history cycle (OI ANCHOR first + sanitized + interpreter feed) ──
def _run_history_cycle():
    try:
        from .quant import history, scorecard
        for a in list_assets():
            try:
                root = a.get("options_root") or a.get("key")
                basis = None; spot_live = None
                try:
                    from .data.spot import get_spot
                    fr = get_spot(a, "futures"); ir = get_spot(a, "index")
                    if fr.get("price") and ir.get("price"):
                        basis = fr["price"] - ir["price"]; spot_live = ir["price"]
                except Exception: basis = None
                chain = None
                if oi_anchor:
                    try:
                        chain = oi_anchor.anchor_snapshot(root, live_spot=spot_live)
                        if chain is None and oi_anchor.refresh_anchor(root):
                            chain = oi_anchor.anchor_snapshot(root, live_spot=spot_live)
                    except Exception: chain = None
                if chain is None:
                    chain = _get_chain_cached_sync(root, ttl=240)
                g = _sanitize_walls(GEXEngine(multiplier=a.get("multiplier", 100)).calculate(chain))
                history.snapshot_and_diff(root, g, basis)
                if INTERPRETER:
                    try:
                        INTERPRETER.on_state(root, g, basis=basis,
                                             ctx={"session_mode": _session_mode()})
                    except Exception: pass
            except Exception: pass
        try: scorecard.grade_pending()
        except Exception: pass
    except Exception: pass

def _history_sampler_loop():
    _run_history_cycle()
    while True:
        time.sleep(300)
        _run_history_cycle()

threading.Thread(target=_history_sampler_loop, daemon=True).start()

def _merge_live_tail(asset, source, interval, out):
    key = (asset["options_root"], source)
    _register_live(asset, source)
    b = _LIVE_BUCKETS.get(interval, 300)
    with _live_lock: raw = list(_live_samples.get(key, []))
    if not raw: return out
    nowb = (int(time.time()) // b) * b
    cur = [p for t, p in raw if (int(t) // b) * b == nowb]
    if not cur: cur = [raw[-1][1]]
    live_c = {"time": nowb, "open": cur[0], "high": max(cur), "low": min(cur), "close": cur[-1]}
    if out and out[-1]["time"] == nowb:
        prev = out[-1]
        live_c["open"] = prev["open"]
        live_c["high"] = max(prev["high"], live_c["high"])
        live_c["low"] = min(prev["low"], live_c["low"])
        out[-1] = live_c
    elif not out or nowb > out[-1]["time"]: out.append(live_c)
    return out

def safe_json(obj):
    if isinstance(obj, dict): return {k: safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list): return [safe_json(v) for v in obj]
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)): return None
    return obj

def endpoint_wrapper(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try: return JSONResponse(content=safe_json(fn(*args, **kwargs)))
        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n[SERVER ERROR in {fn.__name__}]\n{tb}")
            return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {str(e)}"})
    return wrapper

def _notify_bg(title, text):
    try: asyncio.create_task(asyncio.to_thread(alert, title, text))
    except Exception: pass

# ── 🟢 server-side TV-levels mirror + Telegram wrapper w/ freshness ──
def _tv_levels_string(asset):
    def build():
        root = asset["options_root"]
        g, chain = None, None
        if oi_anchor:
            spot_live = None
            try:
                from .data.spot import get_spot
                r0 = get_spot(asset, "index" if _session_mode() == "RTH" else "futures")
                spot_live = r0.get("price")
            except Exception: pass
            chain = oi_anchor.anchor_snapshot(root, live_spot=spot_live)
            if chain is not None:
                cand = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(chain))
                sp = float(spot_live or getattr(chain, "spot", 0) or 0)
                pw0, cw0 = cand.get("put_wall"), cand.get("call_wall")
                if sp and pw0 and cw0 and pw0 < sp < cw:
                    g = cand
        if g is None:
            chain = _get_chain_cached_sync(root, ttl=240)
            g = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(chain))
        mode = _wall_mode(); basis = None
        try:
            from .data.spot import get_spot
            fr = get_spot(asset, "futures"); ir = get_spot(asset, "index")
            if fr.get("price") and ir.get("price"): basis = fr["price"] - ir["price"]
        except Exception: pass
        if mode["mode"] == "RTH" and basis is not None and g.get("put_wall") and g.get("call_wall"):
            g["put_wall_fut"] = round(g["put_wall"] + basis, 2)
            g["call_wall_fut"] = round(g["call_wall"] + basis, 2)
        try: s = analyze_surface(chain, g.get("spot"))
        except Exception: s = {}
        rows, used = [], []
        def add(v, label, kind):
            if v is None: return
            try: v = round(float(v), 2)
            except Exception: return
            if any(abs(u - v) <= 5 for u in used): return
            used.append(v); rows.append((v, label, kind))
        pw, cw = g.get("put_wall"), g.get("call_wall")
        rth = mode.get("active") == "translated"
        add(g.get("call_wall_0dte"), "0DTE Call", "res0")
        if rth:
            add(g.get("put_wall_fut"), "Put Wall (fut)", "res")
            add(g.get("call_wall_fut"), "Call Wall (fut)", "sup")
            add(pw, "Put raw", "emb"); add(cw, "Call raw", "emb")
        else:
            add(pw, "Put Wall", "res"); add(cw, "Call Wall", "sup")
            add(g.get("put_wall_fut"), "Put (fut)", "emb")
            add(g.get("call_wall_fut"), "Call (fut)", "emb")
        add(g.get("flip_point"), "Gamma Flip", "flip")
        add(g.get("put_wall_0dte"), "0DTE Put", "sup0")
        tcl = (s or {}).get("top_convexity_levels") or []
        if len(tcl) > 0: add(tcl[0], "Range Line High", "ivh")
        if len(tcl) > 1: add(tcl[1], "Range Line Low", "ivl")
        for p in ((s or {}).get("spikes") or [])[:3]:
            if p.get("strike") is not None: add(p["strike"], "Reject " + str(round(p["strike"])), "res")
        for p in ((s or {}).get("pockets") or [])[:3]:
            if p.get("strike") is not None: add(p["strike"], "Accel " + str(round(p["strike"])), "sup")
        prof = g.get("profile") or {}
        ent = sorted(((float(k), v) for k, v in prof.items() if v), key=lambda x: x[0])
        if ent:
            maxAbs = max(abs(v) for _, v in ent)
            sec = []
            for i, (k, v) in enumerate(ent):
                a = abs(v)
                pv = abs(ent[i-1][1]) if i > 0 else 0
                nv = abs(ent[i+1][1]) if i < len(ent)-1 else 0
                if a >= 0.15*maxAbs and a >= pv and a >= nv: sec.append((k, v, a))
            sec.sort(key=lambda x: -x[2])
            for k, v, a in sec[:10]:
                add(k, "GEX " + str(round(k)), "gpos" if v >= 0 else "gneg")
        rows.sort(key=lambda r: -r[0])
        return ";".join(f"{v},{lab},{kind}" for v, lab, kind in rows)
    try:
        return _cached(f"tvstr:{asset['options_root']}", build, ttl=60)
    except Exception:
        return ""

def _notify_levels(title, text, asset):
    a, s = SPOT_META.get("age"), SPOT_META.get("src")
    if a is not None:
        text = f"{text}\n⏱ SPOT {a:.0f}s old ({s})"
    lv = _tv_levels_string(asset)
    if lv:
        text = f"{text}\n\n📋 TV LEVELS ({asset['options_root']}):\n{lv}"
    _notify_bg(title, text)

def _zero_dte(chain):
    tot_c = tot_p = zc = zp = 0
    for c in chain.contracts:
        v = c.volume or 0
        if c.contract_type == "call": tot_c += v
        else: tot_p += v
        if c.dte == 0:
            if c.contract_type == "call": zc += v
            else: zp += v
    tot = tot_c + tot_p
    intensity = (zc + zp) / tot if tot else 0
    level = ("LOW" if intensity < 0.15 else "MODERATE" if intensity < 0.30
             else "HIGH" if intensity < 0.5 else "EXTREME")
    return {"level": level, "intensity": round(intensity, 3), "valid": tot > 0,
            "call_volume": zc, "put_volume": zp, "note": "0DTE share of total volume"}

def _wall_mode():
    phase = session_phase().get("phase", "")
    rth = phase in RTH_PHASES
    return {"phase": phase, "mode": "RTH" if rth else "NQ_ONLY",
            "active": "translated" if rth else "raw",
            "note": ("RTH: NDX & NQ both live — dealers hedge at raw+basis. Use TRANSLATED levels on NQ."
                     if rth else
                     "NQ-only: index frozen — futures flow trades the visible strike. Use RAW levels on NQ.")}

def _iv_grid(chain):
    pts = [(c.strike, c.dte, c.iv) for c in chain.contracts if c.iv and c.dte is not None and c.dte >= 0]
    if not pts: return {"strikes": [], "dtes": [], "iv_grid": [], "spot": chain.spot}
    strikes = sorted({p[0] for p in pts})[:60]
    dtes = sorted({p[1] for p in pts})[:12]
    lookup = {(p[0], p[1]): p[2] for p in pts}
    grid = []
    for d in dtes:
        row = []
        for k in strikes:
            v = lookup.get((k, d))
            if v is None:
                near = [lookup.get((k, dd)) for dd in dtes if lookup.get((k, dd)) is not None]
                v = sum(near) / len(near) if near else 0
            row.append(round(v, 4))
        grid.append(row)
    return {"strikes": strikes, "dtes": dtes, "iv_grid": grid, "spot": chain.spot}

# ═════════════════════ ROUTES ═════════════════════
@app.get("/")
async def index(): return FileResponse(WEB / "index.html")

@app.get("/api/assets")
@endpoint_wrapper
def assets(): return list_assets()

@app.get("/api/health")
@endpoint_wrapper
def health(): return {"status": "ok", "time": time.time()}

@app.get("/api/spot_health")
@endpoint_wrapper
def spot_health(symbol: str = None):
    from .data import spot as spotmod
    asset = _resolve(symbol)
    out = {}
    for kind in ("index", "futures"):
        tv = spotmod.TV_MAP.get((asset["options_root"], kind))
        tv_px = None
        if tv:
            try: tv_px = spotmod._tv_quote(tv)
            except Exception: tv_px = None
        out[kind] = {"tv_symbol": tv, "tv_price": tv_px, "ladder": spotmod.get_spot(asset, kind)}
    return out

@app.get("/api/gex_history")
@endpoint_wrapper
def gex_history(symbol: str = None, limit: int = 400):
    from .quant import history
    return history.snapshots(_resolve(symbol)["options_root"], limit)

@app.get("/api/wall_migrations")
@endpoint_wrapper
def wall_migrations(symbol: str = None, limit: int = 50):
    from .quant import history
    return history.migrations(_resolve(symbol)["options_root"], limit)

@app.get("/api/scorecard")
@endpoint_wrapper
def scorecard():
    def compute():
        from .quant import scorecard as sc
        return sc.summary()
    return _cached("scorecard", compute, ttl=300)

@app.get("/api/test_alert")
async def test_alert():
    sent = await asyncio.to_thread(alert, "SFD Test 📨", "Telegram alerts are working!")
    return {"sent": sent, "telegram_enabled": telegram_enabled()}

@app.get("/api/environment")
@endpoint_wrapper
def environment():
    def compute():
        s = config.load().get("settings", {})
        return {"session": session_phase(), "calendar": opex_status(skip_opex=s.get("skip_opex", True))}
    return _cached("environment", compute, ttl=30)

@app.get("/api/interpreter")
@endpoint_wrapper
def interpreter(symbol: str = None, limit: int = 50):
    if not INTERPRETER:
        return {"enabled": False, "feed": []}
    asset = _resolve(symbol)
    return {"enabled": True, "root": asset["options_root"],
            "feed": INTERPRETER.history(asset["options_root"], limit)}

@app.get("/api/interpreter_stream")
async def interpreter_stream(symbol: str = None):
    asset = _resolve(symbol)
    root = asset["options_root"]

    async def generate():
        def sse(data): return f"data: {json.dumps(data, default=str)}\n\n"
        last_ts = 0.0
        last_ping = time.time()
        try:
            if INTERPRETER:
                for ev in INTERPRETER.history(root):
                    last_ts = max(last_ts, ev.get("ts", 0))
                    yield sse({"event": "history", "data": safe_json(ev)})
            yield sse({"event": "started", "data": {"root": root, "enabled": bool(INTERPRETER)}})
            while True:
                await asyncio.sleep(2)
                if INTERPRETER:
                    for ev in INTERPRETER.since(root, last_ts):
                        last_ts = max(last_ts, ev.get("ts", 0))
                        yield sse({"event": "interpreter_update", "data": safe_json(ev)})
                if time.time() - last_ping > 30:
                    last_ping = time.time()
                    yield sse({"event": "ping", "data": {}})
        except asyncio.CancelledError:
            return

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.get("/api/interpreter_test")
@endpoint_wrapper
def interpreter_test(symbol: str = None):
    if not INTERPRETER:
        return {"enabled": False, "error": "wall_interpreter not loaded"}
    asset = _resolve(symbol)
    root = asset["options_root"]
    st = INTERPRETER._get(root)
    snap = {k: st[k] for k in list(st.keys())}
    ctx = {"session_mode": _session_mode(), "synthetic": True}
    base = {"spot": 29400.0, "put_wall": 29600.0, "call_wall": 29275.0,
            "flip_point": 29500.0, "regime": "POSITIVE", "gamma_zone": "MIXED"}
    fired = []
    try:
        st["last"], st["pending"], st["announced"] = None, {}, {}
        INTERPRETER.on_state(root, dict(base), basis=50.0, ctx=ctx)
        fired += INTERPRETER.on_state(root, dict(base), basis=84.0, ctx=ctx)
        INTERPRETER.on_state(root, {**base, "put_wall": 29320.0}, basis=84.0, ctx=ctx)
        fired += INTERPRETER.on_state(root, {**base, "put_wall": 29320.0}, basis=84.0, ctx=ctx)
        fired += INTERPRETER.on_state(root, dict(base), basis=84.0, ctx=ctx)
    finally:
        st.clear(); st.update(snap)
    return {"enabled": True, "root": root, "events": fired,
            "note": "Events also pushed to the 🧠 panel via SSE."}

@app.get("/api/oi_anchor")
@endpoint_wrapper
def oi_anchor_api(symbol: str = None, refresh: int = 0):
    if not oi_anchor:
        return {"enabled": False, "error": "oi_anchor module not loaded"}
    root = _resolve(symbol)["options_root"]
    if refresh:
        oi_anchor.refresh_anchor(root, force=True)
    return {"enabled": True, **oi_anchor.status(root)}

@app.get("/api/tv_string")
@endpoint_wrapper
def tv_string_api(symbol: str = None):
    return {"tv_string": _tv_levels_string(_resolve(symbol))}

@app.get("/api/live_spot")
@endpoint_wrapper
def live_spot_api(symbol: str = None, kind: str = "futures"):
    asset = _resolve(symbol)
    px, age, src = _fresh_spot(asset, kind)
    return {"price": px, "age": round(age, 1) if age is not None else None,
            "src": src, "ts": time.time(), "kind": kind}

@app.get("/api/walls")
@endpoint_wrapper
def walls(symbol: str = None):
    asset = _resolve(symbol)

    def compute():
        from .data.schema import ContractQuote, ChainSnapshot
        from datetime import datetime
        g, chain, source_tag, prov_tag = None, None, "unknown", "UNKNOWN"
        if oi_anchor:
            try:
                live0 = None
                try:
                    from .data.spot import get_spot
                    r0 = get_spot(asset, "index" if _session_mode() == "RTH" else "futures")
                    live0 = r0.get("price")
                except Exception:
                    live0 = None
                a_chain = oi_anchor.anchor_snapshot(asset["options_root"], live_spot=live0)
                if a_chain is None:
                    oi_anchor.refresh_anchor(asset["options_root"])
                    a_chain = oi_anchor.anchor_snapshot(asset["options_root"], live_spot=live0)
                if a_chain is not None:
                    cand = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(a_chain))
                    sp = float(live0 or getattr(a_chain, "spot", 0) or 0)
                    pw, cw = cand.get("put_wall"), cand.get("call_wall")
                    sane = (sp > 0 and pw and cw and pw < sp < cw
                            and (sp - pw) / sp < 0.08 and (cw - sp) / sp < 0.08)
                    if not sane:
                        print(f"[oi_anchor] {asset['options_root']}: degenerate walls "
                              f"(put {pw} / call {cw} vs spot {sp}) — falling back to live chain")
                    elif pw is not None and cw is not None:
                        g, chain = cand, a_chain
                        source_tag, prov_tag = "anchor", "EOD-OI ANCHOR + LIVE SPOT"
            except Exception as e:
                print(f"OI anchor unavailable ({e}) — falling back to live chain")
        if g is None:
            try:
                from .data.alpaca_feed import get_realtime_option_chain
                alpaca_raw = get_realtime_option_chain(asset["options_root"])
                if alpaca_raw and len(alpaca_raw) > 100:
                    contracts = [ContractQuote(
                        symbol=asset["options_root"], expiry=c["expiry"], strike=c["strike"],
                        contract_type=c["type"], bid=c.get("bid"), ask=c.get("ask"),
                        iv=c.get("iv"), delta=c.get("delta"), gamma=c.get("gamma"),
                        theta=c.get("theta"), vega=c.get("vega"),
                        open_interest=c.get("open_interest", 0), volume=c.get("volume", 0),
                        source="alpaca", provenance="REAL-TIME OPRA") for c in alpaca_raw]
                    spot = None
                    try:
                        from .data.spot import get_spot
                        mode = _session_mode()
                        spot_r = get_spot(asset, "index" if mode == "RTH" else "futures")
                        spot = spot_r.get("price")
                    except Exception: pass
                    if spot:
                        alpaca_chain = ChainSnapshot(
                            symbol=asset["options_root"], spot=spot,
                            as_of=datetime.now().isoformat(), source="alpaca",
                            provenance="REAL-TIME OPRA", contracts=contracts)
                        candidate = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(alpaca_chain))
                        if candidate.get("put_wall") is not None and candidate.get("call_wall") is not None:
                            g, chain = candidate, alpaca_chain
                            source_tag, prov_tag = "alpaca", "REAL-TIME OPRA"
                        else:
                            print("Alpaca chain yielded no walls (weekend/data gap) → falling back to CBOE")
            except Exception as e:
                print(f"Alpaca options fetch failed: {e}")
        if g is None:
            try:
                chain = _get_chain_cached_sync(asset["options_root"])
                g = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(chain))
                source_tag, prov_tag = "cboe", "DELAYED 15-MIN"
            except Exception as e:
                from .data.spot import get_spot
                mode = _session_mode()
                spot_r = get_spot(asset, "index" if mode == "RTH" else "futures")
                return {"error": "chain_unavailable", "reason": str(e)[:120],
                        "spot": spot_r.get("price") if spot_r else None,
                        "put_wall": None, "call_wall": None, "flip_point": None,
                        "regime": "UNKNOWN", "gamma_zone": "UNKNOWN", "profile": {},
                        "net_gex": 0, "wall_mode": _wall_mode(), "basis": None,
                        "source": "none", "provenance": "UNAVAILABLE"}
        g["source"], g["provenance"] = source_tag, prov_tag
        try:
            from .quant.zerodte import zero_dte_walls, recommend_instrument
            g.update(zero_dte_walls(chain, asset["multiplier"]))
            g["instrument"] = recommend_instrument(session_phase().get("phase"), _zero_dte(chain))
        except Exception: pass
        mode = _wall_mode()
        basis = None
        try:
            from .data.spot import get_spot
            fr = get_spot(asset, "futures"); ir = get_spot(asset, "index")
            if fr.get("price") and ir.get("price"): basis = fr["price"] - ir["price"]
        except Exception: basis = None
        if basis is None:
            try:
                import yfinance as yf
                fp = float(yf.Ticker(asset["futures"] + "=F").fast_info["last_price"])
                ip = float(yf.Ticker(index_yf_symbol(asset)).fast_info["last_price"])
                if fp and ip: basis = fp - ip
            except Exception: basis = None
        pw, cw = g.get("put_wall"), g.get("call_wall")
        g["basis"] = round(basis, 2) if basis is not None else None
        if mode["mode"] == "RTH" and basis is not None:
            g["put_wall_fut"] = round(pw + basis, 2) if pw else None
            g["call_wall_fut"] = round(cw + basis, 2) if cw else None
        else:
            g["put_wall_fut"] = None
            g["call_wall_fut"] = None
        g["wall_mode"] = mode
        if oi_anchor:
            try:
                g["oi_anchor_date"] = oi_anchor.anchor_date(asset["options_root"])
                st = oi_anchor.status(asset["options_root"])
                g["oi_anchor_rows"] = st.get("n_rows", 0)
                g["oi_anchor_stale"] = st.get("stale", False)
            except Exception: pass
        if bus:
            try:
                bus.publish(bus.channel(bus.CH_WALLS, asset["options_root"]), g, last_ttl=120)
                bus.cache_set(f"walls:{asset['options_root']}", g, ttl=60)
            except Exception: pass
        return g

    return _cached(f"walls:{asset['options_root']}", compute, ttl=60)

@app.get("/api/surface")
@endpoint_wrapper
def surface(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            chain = _get_chain_cached_sync(asset["options_root"])
            return analyze_surface(chain, chain.spot)
        except Exception: return {"error": "chain_unavailable"}
    return _cached(f"surface:{asset['options_root']}", compute, ttl=60)

@app.get("/api/chart")
@endpoint_wrapper
def chart(symbol: str = None, interval: str = "5m", period: str = "5d", source: str = "index"):
    asset = _resolve(symbol)
    fut = asset["futures"]
    sym = fut + "=F" if (source == "futures" and fut in {"ES", "NQ", "RTY", "VX"}) else index_yf_symbol(asset)

    def compute():
        if source == "futures" and asset.get("futures") == "NQ" and interval != "1d":
            try:
                from .data.alpaca_feed import get_realtime_equity_bars
                alpaca_bars = get_realtime_equity_bars("QQQ", interval)
                if alpaca_bars and len(alpaca_bars) > 10:
                    return _merge_live_tail(asset, source, interval, alpaca_bars)
            except Exception: pass
        import yfinance as yf
        df = yf.download(sym, interval=interval, period=period, progress=False, threads=False)
        out = []
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            for ts, row in df.iterrows():
                out.append({"time": int(ts.timestamp()), "open": float(row["Open"]),
                            "high": float(row["High"]), "low": float(row["Low"]),
                            "close": float(row["Close"])})
        return _merge_live_tail(asset, source, interval, out)

    return _cached(f"chart:{sym}:{interval}:{period}", compute, ttl=10)

@app.get("/api/playbook")
@endpoint_wrapper
def playbook(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try: chain = _get_chain_cached_sync(asset["options_root"])
        except Exception: return {"error": "chain_unavailable"}
        gex = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(chain))
        s = config.load().get("settings", {})
        sess = session_phase()
        cal = opex_status(skip_opex=s.get("skip_opex", True))
        surf = analyze_surface(chain, gex["spot"])
        from .quant.flow import compute_zero_dte_channel
        zdte = compute_zero_dte_channel(chain)
        from .quant.playbook import build_playbook
        pb = build_playbook(gex["spot"], gex, surf, zdte, sess, cal)
        try:
            from .quant.vol_surface import complexity_level
            pb["complexity"] = complexity_level(gex.get("regime"), surf, zdte.get("level", "LOW"))
        except Exception: pass
        return pb
    return _cached(f"playbook:{asset['options_root']}", compute, ttl=30)

@app.get("/api/flow")
@endpoint_wrapper
def flow(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            from .quant.flow import compute_flow
            chain = _get_chain_cached_sync(asset["options_root"])
            gex = GEXEngine(multiplier=asset["multiplier"]).calculate(chain)
            result, _ = compute_flow(chain, gex, asset["options_root"])
            return result
        except Exception: return {"error": "chain_unavailable"}
    return _cached(f"flow:{asset['options_root']}", compute, ttl=60)

@app.get("/api/verdicts")
@endpoint_wrapper
def verdicts(symbol: str = None):
    asset = _resolve(symbol)
    rows = store.con().execute(
        "SELECT * FROM verdicts WHERE asset=? ORDER BY ts DESC LIMIT 50",
        (asset["options_root"],)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/ticket")
@endpoint_wrapper
def ticket(symbol: str = None):
    asset = _resolve(symbol)
    row = store.con().execute(
        "SELECT * FROM verdicts WHERE asset=? AND entry IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (asset["options_root"],)).fetchone()
    if not row: return {}
    return {"entry": row["entry"], "stop": row["stop"], "target_1": row["target_1"],
            "target_2": row["target_2"], "direction": row["direction"],
            "decision": row["decision"], "wall": row["wall"]}

@app.get("/api/vol_surface")
@endpoint_wrapper
def vol_surface(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            chain = _get_chain_cached_sync(asset["options_root"])
            return _iv_grid(chain)
        except Exception: return {"error": "chain_unavailable"}
    return _cached(f"volsurf:{asset['options_root']}", compute, ttl=60)

@app.get("/api/tv_levels")
@endpoint_wrapper
def tv_levels(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            chain = _get_chain_cached_sync(asset["options_root"])
            gex = GEXEngine(multiplier=asset["multiplier"]).calculate(chain)
            surf = analyze_surface(chain, chain.spot)
            o, a = fetch_open_atr(asset["tape_proxy"])
            return {"tv_string": build_tv_string(chain, gex, surf, asset["multiplier"], o, a),
                    "spot": chain.spot, "provenance": "CBOE-DELAYED"}
        except Exception: return {"error": "chain_unavailable"}
    return _cached(f"tv:{asset['options_root']}", compute, ttl=60)

@app.get("/api/gex_signals")
@endpoint_wrapper
def gex_signals(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            from .quant.gex_signals import gex_profile
            chain = _get_chain_cached_sync(asset["options_root"])
            return gex_profile(chain, chain.spot) or {}
        except Exception: return {}
    return _cached(f"gexsig:{asset['options_root']}", compute, ttl=60)

@app.get("/api/run_dashboard_stream")
async def run_dashboard_stream(symbol: str = None):
    asset = _resolve(symbol)

    async def generate():
        def sse(stage, data): return f"data: {json.dumps({'stage': stage, 'data': data}, default=str)}\n\n"
        try:
            yield sse("status", {"message": "Fetching environment..."})
            s = config.load().get("settings", {})
            cal = opex_status(skip_opex=s.get("skip_opex", True))
            sess = session_phase()
            yield sse("environment", {"session": safe_json(sess), "calendar": safe_json(cal),
                                      "asset_key": asset["options_root"], "futures": asset["futures"]})
            yield sse("status", {"message": "Fetching chain & computing walls..."})
            chain = await _get_chain_cached(asset["options_root"])
            gex = _sanitize_walls(GEXEngine(multiplier=asset["multiplier"]).calculate(chain))
            if gex.get("put_wall") is None or gex.get("call_wall") is None:
                yield sse("error", {"message": "Incomplete walls (chain in flux) — retry in a minute."})
                return
            mode = _session_mode()
            live_spot, spot_label = await _mode_spot(asset)
            if live_spot: gex["spot"] = live_spot
            gex["spot_source"] = spot_label
            gex["session_mode"] = mode
            yield sse("walls", safe_json(gex))
            yield sse("status", {"message": "Reading tape speed..."})
            tape = get_tape_speed(asset["tape_proxy"], "5m")
            yield sse("tape", safe_json(tape))
            yield sse("status", {"message": "Running AI pipeline (30-90s)..."})
            pipe = await asyncio.to_thread(lambda: run_intraday_pipeline(gex, chain=chain, asset=asset))
            yield sse("pipeline", safe_json(pipe))
            vid = await asyncio.to_thread(lambda: log_verdict(pipe, gex, asset, wall=None, context="web_dashboard"))
            yield sse("logged", {"verdict_id": vid})
            yield sse("done", {})
        except asyncio.CancelledError: return
        except Exception as e:
            traceback.print_exc()
            yield sse("error", {"message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.get("/api/sentinel_stream")
async def sentinel_stream(symbol: str = None):
    asset = _resolve(symbol)

    async def generate():
        def sse(event, data): return f"data: {json.dumps({'event': event, 'data': data}, default=str)}\n\n"
        try:
            from .exec.watcher import sentinel_cfg
            _cfg = sentinel_cfg()
            root = asset["options_root"]
            index_yf = index_yf_symbol(asset)
            multiplier = asset["multiplier"]
            proximity_pct = _cfg["proximity_pct"]
            confirm_window_secs = _cfg["confirm_window_secs"]
            wall_refresh_secs = _cfg["wall_refresh_secs"]
            cooldown_secs = _cfg["post_signal_cooldown_secs"]
            poll_secs = _cfg["poll_secs"]
            s = config.load().get("settings", {})
            cal = opex_status(skip_opex=s.get("skip_opex", True))
            if not cal["trade_allowed"]:
                yield sse("blackout", {"calendar": safe_json(cal)})
                yield sse("done", {})
                return
            yield sse("started", {"asset_key": root, "futures": asset["futures"],
                                  "index_yf": index_yf, "proximity_pct": proximity_pct,
                                  "telegram": telegram_enabled(), "calendar": safe_json(cal)})
            chain = await asyncio.to_thread(opra.get_chain, root)
            walls = _sanitize_walls(GEXEngine(multiplier=multiplier).calculate(chain))
            stored_chain = chain
            yield sse("walls", safe_json(walls))
            monitor = None
            cooldown_until = 0.0
            last_wall_sync = time.time()
            while True:
                mode = _session_mode()
                # 🟢 SYNC FIX: always track the traded 24h futures contract —
                # the cash index freezes at 16:00 ET, futures never do.
                kind = "futures"
                spot, spot_age, spot_src = await asyncio.to_thread(_fresh_spot, asset, kind)
                if spot is not None and spot_age is not None and spot_age > 90:
                    if not SPOT_META.get("stale_warned"):
                        _notify_levels(f"SFD {root} ⏱",
                                       f"STALE SPOT {spot_age:.0f}s ({spot_src}) — sentinel signals paused until fresh data.",
                                       asset)
                        SPOT_META["stale_warned"] = True
                    yield sse("status", {"state": "STALE", "spot": spot,
                                         "spot_age": spot_age, "spot_source": spot_src,
                                         "armed": False, "mode": mode})
                    await asyncio.sleep(poll_secs); continue
                SPOT_META["stale_warned"] = False
                basis = None
                try:
                    from .data.spot import get_spot
                    fr = get_spot(asset, "futures"); ir = get_spot(asset, "index")
                    if fr.get("price") and ir.get("price"): basis = fr["price"] - ir["price"]
                except Exception: basis = None
                if mode == "RTH" and basis is not None and walls.get("put_wall") and walls.get("call_wall"):
                    put_exec = walls["put_wall"] + basis
                    call_exec = walls["call_wall"] + basis
                else:
                    put_exec = walls.get("put_wall")
                    call_exec = walls.get("call_wall")
                walls_ok = (walls is not None and walls.get("put_wall") is not None
                            and walls.get("call_wall") is not None)
                if not spot or not walls_ok:
                    yield sse("status", {"state": "WAITING", "message": "waiting for spot/walls",
                                         "armed": False, "mode": mode})
                    if not walls_ok:
                        try:
                            chain = await asyncio.to_thread(opra.get_chain, root, True)
                            cand = _sanitize_walls(GEXEngine(multiplier=multiplier).calculate(chain))
                            if cand.get("put_wall") is not None and cand.get("call_wall") is not None:
                                walls, stored_chain = cand, chain
                                last_wall_sync = time.time()
                                yield sse("walls", safe_json(walls))
                        except Exception: pass
                    await asyncio.sleep(poll_secs)
                    continue
                if time.time() - last_wall_sync > wall_refresh_secs:
                    try:
                        new_chain = await asyncio.to_thread(opra.get_chain, root, True)
                        new_walls = _sanitize_walls(GEXEngine(multiplier=multiplier).calculate(new_chain))
                        if new_walls.get("put_wall") is not None and new_walls.get("call_wall") is not None:
                            migrations = []
                            for wt in ("put_wall", "call_wall"):
                                ov, nv = walls.get(wt), new_walls.get(wt)
                                if ov is not None and nv is not None and abs(nv - ov) >= 0.01:
                                    label = "PUT" if wt == "put_wall" else "CALL"
                                    migrations.append({"wall": label, "old": ov, "new": nv})
                                    if monitor and getattr(monitor, "wall_type", None) == label:
                                        monitor = None
                            walls = new_walls
                            stored_chain = new_chain
                            yield sse("walls", safe_json(walls))
                            if bus:
                                try: bus.publish(bus.channel(bus.CH_WALLS, root), walls, last_ttl=120)
                                except Exception: pass
                            if migrations:
                                yield sse("wall_migration", {"migrations": migrations})
                            try:
                                from .quant import history
                                history.snapshot_and_diff(root, new_walls, basis)
                            except Exception: pass
                            if INTERPRETER:
                                try:
                                    for ie in INTERPRETER.on_state(
                                            root, new_walls, basis=basis,
                                            ctx={"session_mode": mode}):
                                        yield sse("interpreter_update", safe_json(ie))
                                except Exception: pass
                            last_wall_sync = time.time()
                    except Exception: pass
                # 🟢 SYNC FIX: measure proximity vs the translated (exec) walls
                put_dist = (spot - put_exec) / spot
                call_dist = (call_exec - spot) / spot
                if monitor:
                    res = await asyncio.to_thread(monitor.update, spot)
                    sig, state = res.get("signal"), res.get("state")
                    wl = monitor.wall_type
                    ex = put_exec if wl == "PUT" else call_exec
                    if sig == "precursor":
                        _notify_levels(f"SFD {root} ⚠️",
                                       f"PRECURSOR: approaching {wl} wall @ {monitor.wall_level:,.0f}", asset)
                        yield sse("precursor", {"wall": wl, "wall_level": monitor.wall_level,
                                                "exec": ex, "mode": mode, "basis": basis,
                                                "spot": spot, "state": state})
                    elif sig == "touch":
                        _notify_levels(f"SFD {root} •",
                                       f"TOUCH: at {wl} wall (approach {res.get('approach')})", asset)
                        yield sse("touch", {"wall": wl, "approach": res.get("approach"),
                                            "exec": ex, "mode": mode, "basis": basis,
                                            "spot": spot, "state": state})
                    elif sig == "expired":
                        yield sse("expired", {"wall": wl, "spot": spot})
                        monitor = None
                    elif sig == "trade":
                        _notify_levels(f"SFD {root} ✅",
                                       f"CONFIRMED: {res.get('confirmation')} at {wl} wall — running AI...", asset)
                        yield sse("confirmed", {"wall": wl, "confirmation": res.get("confirmation"),
                                                "approach": res.get("approach"), "exec": ex,
                                                "mode": mode, "basis": basis, "spot": spot})
                        yield sse("pipeline_start", {"message": "Running AI pipeline (30-90s)..."})
                        pipe = await asyncio.to_thread(lambda: run_intraday_pipeline(walls, chain=stored_chain, asset=asset, wall=wl))
                        vid = await asyncio.to_thread(lambda: log_verdict(pipe, walls, asset, wl, "browser_sentinel"))
                        tk = pipe.get("execution", {}) or {}
                        jd = pipe.get("judge", {}) or {}
                        _notify_levels(f"SFD {root} 🎫",
                                       f"{wl} wall | {jd.get('decision')} {tk.get('direction')}\n"
                                       f"Entry {tk.get('entry', 0):,.2f} / Stop {tk.get('stop_loss', 0):,.2f}\n"
                                       f"T1 {tk.get('target_1', 0):,.2f} / T2 {tk.get('target_2', 0):,.2f}", asset)
                        yield sse("pipeline_done", {"verdict_id": vid, "pipeline": safe_json(pipe)})
                        monitor = None
                        cooldown_until = time.time() + cooldown_secs
                    elif state == "WATCHING":
                        monitor = None
                    yield sse("status", {"state": state or ("WATCHING" if not monitor else monitor.state),
                                         "spot": spot, "spot_age": SPOT_META.get("age"),
                                         "spot_source": SPOT_META.get("src"),
                                         "put_dist": put_dist * 100,
                                         "call_dist": call_dist * 100,
                                         "armed": monitor is not None, "mode": mode})
                else:
                    if time.time() > cooldown_until:
                        new_monitor = None
                        # 🟢 SYNC FIX: arm on the translated (exec) walls
                        if abs(put_dist) <= proximity_pct:
                            new_monitor = ConfirmationMonitor("PUT", put_exec, index_yf,
                                                              proximity_pct, confirm_window_secs)
                        elif abs(call_dist) <= proximity_pct:
                            new_monitor = ConfirmationMonitor("CALL", call_exec, index_yf,
                                                              proximity_pct, confirm_window_secs)
                        if new_monitor:
                            monitor = new_monitor
                            first = await asyncio.to_thread(monitor.update, spot)
                            wl = monitor.wall_type
                            ex = put_exec if wl == "PUT" else call_exec
                            if first.get("signal") == "precursor":
                                _notify_levels(f"SFD {root} ⚠️",
                                               f"PRECURSOR: approaching {wl} wall @ {monitor.wall_level:,.0f}", asset)
                                yield sse("precursor", {"wall": wl, "wall_level": monitor.wall_level,
                                                        "exec": ex, "mode": mode, "basis": basis,
                                                        "spot": spot, "state": first.get("state")})
                    nearer = "PUT" if abs(put_dist) < abs(call_dist) else "CALL"
                    yield sse("status", {"state": monitor.state if monitor else "WATCHING",
                                         "spot": spot, "spot_age": SPOT_META.get("age"),
                                         "spot_source": SPOT_META.get("src"),
                                         "put_dist": put_dist * 100,
                                         "call_dist": call_dist * 100,
                                         "armed": monitor is not None, "nearest": nearer, "mode": mode})
                await asyncio.sleep(poll_secs)
        except asyncio.CancelledError: return
        except Exception as e:
            traceback.print_exc()
            yield sse("error", {"message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no", "Connection": "keep-alive"})

def _quad_days():
    from datetime import date
    now = date.today()
    def third_friday(y, m):
        first = date(y, m, 1)
        offset = (4 - first.weekday()) % 7
        return date(y, m, 1 + offset + 14)
    y, m = now.year, now.month
    for _ in range(8):
        if m in (3, 6, 9, 12):
            exp = third_friday(y, m)
            if exp >= now: return (exp - now).days
            return (exp - now).days
        m += 1
        if m > 12: m, y = 1, y + 1
    return 30

@app.get("/api/basis")
@endpoint_wrapper
def basis():
    def compute():
        import yfinance as yf
        R, qdef = 0.043, 0.015
        pairs = [("NDX", "^NDX", "NQ=F", 0.007), ("SPX", "^GSPC", "ES=F", 0.013)]
        out = {}
        T = _quad_days()
        for key, idx_sym, fut_sym, q in pairs:
            try:
                fut_px = idx_px = None
                try:
                    from .data.spot import get_spot
                    asset = get_asset(key)
                    fr = get_spot(asset, "futures"); ir = get_spot(asset, "index")
                    if fr.get("price") and ir.get("price"):
                        fut_px = float(fr["price"]); idx_px = float(ir["price"])
                except Exception: pass
                if fut_px is None:
                    fut_px = float(yf.Ticker(fut_sym).fast_info["last_price"])
                    idx_px = float(yf.Ticker(idx_sym).fast_info["last_price"])
                fh = yf.Ticker(fut_sym).history(period="5d")["Close"]
                ih = yf.Ticker(idx_sym).history(period="5d")["Close"]
            except Exception: continue
            basis = fut_px - idx_px
            prev_basis = None
            if len(fh) >= 2 and len(ih) >= 2:
                prev_basis = float(fh.iloc[-2]) - float(ih.iloc[-2])
            fair = idx_px * (R - q) * T / 365.0
            out[key] = {"fut": round(fut_px, 2), "index": round(idx_px, 2),
                        "basis": round(basis, 2), "basis_pct": round(basis / idx_px * 100, 3),
                        "prev_basis": round(prev_basis, 2) if prev_basis is not None else None,
                        "day_change": round(basis - prev_basis, 2) if prev_basis is not None else None,
                        "fair": round(fair, 2), "vs_fair": round(basis - fair, 2),
                        "days_to_exp": T}
        if bus:
            try: bus.publish("basis", {"pairs": out, "ts": time.time()}, last_ttl=120)
            except Exception: pass
        return {"pairs": out, "ts": time.time(), "provenance": "SPOT-LADDER + YFINANCE history"}
    return _cached("basis", compute, ttl=120)

@app.post("/api/execute")
@endpoint_wrapper
def execute(payload: dict):
    from .exec.alpaca_client import execute_ticket
    ticket = payload.get("ticket")
    symbol = payload.get("symbol", "QQQ")
    if not ticket: return {"status": "error", "reason": "No ticket provided"}
    return execute_ticket(ticket, symbol)

import os 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8321))
    print(f"\n SFD Chart Terminal -> http://0.0.0.0:{port}\n")
    store.init()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
