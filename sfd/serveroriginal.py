"""
SFD Chart Terminal — FastAPI server (FULLY PATCHED v3).
Mode-aware spot + NaN-safe chain fetching + Graceful Degradation + Redis Bus.
"""
import time
import math
import asyncio
import threading
import traceback
import functools
import json
from pathlib import Path

import numpy as np
import pandas as pd
import uvicorn
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

# Graceful bus import — if sfd/bus.py is missing, server still runs perfectly
try:
    from . import bus
except ImportError:
    bus = None

WEB = Path(__file__).resolve().parent / "web"
RTH_PHASES = ("OPEN_DRIVE", "MIDDAY", "THETA_BURN", "POWER_HOUR")

app = FastAPI(title="SFD Chart Terminal")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_cache = {}

# ── thread-safe chain cache ──────────────────────────────────
_chain_cache = {}
_chain_fetch_lock = threading.Lock()

def _fetch_chain_once(symbol, ttl=60):
    now = time.time()
    hit = _chain_cache.get(symbol)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _chain_fetch_lock:
        hit = _chain_cache.get(symbol)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        chain = opra.get_chain(symbol)
        _chain_cache[symbol] = (time.time(), chain)
        return chain

def _get_chain_cached_sync(symbol, ttl=60):
    return _fetch_chain_once(symbol, ttl)

async def _get_chain_cached(symbol, ttl=60):
    return await asyncio.to_thread(_fetch_chain_once, symbol, ttl)

def _cached(key, fn, ttl):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val

def _resolve(symbol):
    if symbol:
        try: return get_asset(symbol.upper())
        except Exception: pass
    return get_asset(active_asset_key())

def _session_mode():
    return "RTH" if session_phase().get("phase", "") in RTH_PHASES else "NQ_ONLY"

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

# ── live candle tail ─────────────────────────────────────────
_live_lock = threading.Lock()
_live_samples = {}
_live_keys = {}
_LIVE_BUCKETS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

def _sample_live(asset, source):
    from .data.spot import get_spot
    kind = "futures" if source == "futures" else "index"
    try:
        r = get_spot(asset, kind)
        if r and r.get("price"):
            key = (asset["options_root"], source)
            with _live_lock:
                buf = _live_samples.setdefault(key, [])
                buf.append((time.time(), float(r["price"])))
                cutoff = time.time() - 7200
                while buf and buf[0][0] < cutoff: buf.pop(0)
    except Exception: pass

def _register_live(asset, source):
    with _live_lock: _live_keys[(asset["options_root"], source)] = time.time()
    _sample_live(asset, source)

def _live_sampler_loop():
    while True:
        time.sleep(10)
        with _live_lock:
            active = [k for k, last in _live_keys.items() if time.time() - last < 600]
        for root, source in active:
            try: _sample_live(get_asset(root), source)
            except Exception: pass

threading.Thread(target=_live_sampler_loop, daemon=True).start()

# ── history + scorecard background sampler (every 5 min) ─────
def _run_history_cycle():
    try:
        from .quant import history, scorecard
        for a in list_assets():
            try:
                root = a.get("options_root") or a.get("key")
                chain = _get_chain_cached_sync(root, ttl=240)
                g = GEXEngine(multiplier=a.get("multiplier", 100)).calculate(chain)
                basis = None
                try:
                    from .data.spot import get_spot
                    fr = get_spot(a, "futures")
                    ir = get_spot(a, "index")
                    if fr.get("price") and ir.get("price"): basis = fr["price"] - ir["price"]
                except Exception: basis = None
                history.snapshot_and_diff(root, g, basis)
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
    level = ("LOW" if intensity < 0.15 else "MODERATE" if intensity < 0.30 else "HIGH" if intensity < 0.5 else "EXTREME")
    return {"level": level, "intensity": round(intensity, 3), "valid": tot > 0, "call_volume": zc, "put_volume": zp, "note": "0DTE share of total volume"}

def _wall_mode():
    phase = session_phase().get("phase", "")
    rth = phase in RTH_PHASES
    return {"phase": phase, "mode": "RTH" if rth else "NQ_ONLY", "active": "translated" if rth else "raw",
            "note": ("RTH: NDX & NQ both live — dealers hedge at raw+basis. Use TRANSLATED levels on NQ." if rth else
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

# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────
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

@app.get("/api/walls")
@endpoint_wrapper
def walls(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        # ── GRACEFUL DEGRADATION: Catch chain fetch failures ──
        try:
            chain = _get_chain_cached_sync(asset["options_root"])
        except Exception as e:
            from .data.spot import get_spot
            mode = _session_mode()
            spot_r = get_spot(asset, "index" if mode == "RTH" else "futures")
            return {
                "error": "chain_unavailable", "reason": str(e)[:120],
                "spot": spot_r.get("price") if spot_r else None,
                "put_wall": None, "call_wall": None, "flip_point": None,
                "regime": "UNKNOWN", "gamma_zone": "UNKNOWN", "profile": {}, "net_gex": 0,
                "wall_mode": _wall_mode(), "basis": None
            }

        g = GEXEngine(multiplier=asset["multiplier"]).calculate(chain)
        try:
            from .quant.zerodte import zero_dte_walls, recommend_instrument
            g.update(zero_dte_walls(chain, asset["multiplier"]))
            g["instrument"] = recommend_instrument(session_phase().get("phase"), _zero_dte(chain))
        except Exception: pass
        
        mode = _wall_mode()
        basis = None
        try:
            from .data.spot import get_spot
            fr = get_spot(asset, "futures")
            ir = get_spot(asset, "index")
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
        
        # ── SFD bus: fan walls out to CLI / agents / subscribers ──
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
        except Exception:
            return {"error": "chain_unavailable"}
    return _cached(f"surface:{asset['options_root']}", compute, ttl=60)

@app.get("/api/chart")
@endpoint_wrapper
def chart(symbol: str = None, interval: str = "5m", period: str = "5d", source: str = "index"):
    asset = _resolve(symbol)
    fut = asset["futures"]
    if source == "futures" and fut in {"ES", "NQ", "RTY", "VX"}: sym = fut + "=F"
    else: sym = index_yf_symbol(asset)
    def compute():
        import yfinance as yf
        df = yf.download(sym, interval=interval, period=period, progress=False, threads=False)
        out = []
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            for ts, row in df.iterrows():
                out.append({"time": int(ts.timestamp()), "open": float(row["Open"]), "high": float(row["High"]), "low": float(row["Low"]), "close": float(row["Close"])})
        out = _merge_live_tail(asset, source, interval, out)
        return out
    return _cached(f"chart:{sym}:{interval}:{period}", compute, ttl=10)

@app.get("/api/playbook")
@endpoint_wrapper
def playbook(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            chain = _get_chain_cached_sync(asset["options_root"])
        except Exception:
            return {"error": "chain_unavailable"}
        gex = GEXEngine(multiplier=asset["multiplier"]).calculate(chain)
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
        except Exception:
            return {"error": "chain_unavailable"}
    return _cached(f"flow:{asset['options_root']}", compute, ttl=60)

@app.get("/api/verdicts")
@endpoint_wrapper
def verdicts(symbol: str = None):
    asset = _resolve(symbol)
    rows = store.con().execute("SELECT * FROM verdicts WHERE asset=? ORDER BY ts DESC LIMIT 50", (asset["options_root"],)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/ticket")
@endpoint_wrapper
def ticket(symbol: str = None):
    asset = _resolve(symbol)
    row = store.con().execute("SELECT * FROM verdicts WHERE asset=? AND entry IS NOT NULL ORDER BY ts DESC LIMIT 1", (asset["options_root"],)).fetchone()
    if not row: return {}
    return {"entry": row["entry"], "stop": row["stop"], "target_1": row["target_1"], "target_2": row["target_2"], "direction": row["direction"], "decision": row["decision"], "wall": row["wall"]}

@app.get("/api/vol_surface")
@endpoint_wrapper
def vol_surface(symbol: str = None):
    asset = _resolve(symbol)
    def compute():
        try:
            chain = _get_chain_cached_sync(asset["options_root"])
            return _iv_grid(chain)
        except Exception:
            return {"error": "chain_unavailable"}
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
            return {"tv_string": build_tv_string(chain, gex, surf, asset["multiplier"], o, a), "spot": chain.spot, "provenance": "CBOE-DELAYED"}
        except Exception:
            return {"error": "chain_unavailable"}
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
        except Exception:
            return {}
    return _cached(f"gexsig:{asset['options_root']}", compute, ttl=60)

# ── Dashboard one-shot (SSE) ───────────────
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
            yield sse("environment", {"session": safe_json(sess), "calendar": safe_json(cal), "asset_key": asset["options_root"], "futures": asset["futures"]})
            yield sse("status", {"message": "Fetching chain & computing walls..."})
            chain = await _get_chain_cached(asset["options_root"])
            gex = GEXEngine(multiplier=asset["multiplier"]).calculate(chain)
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
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

# ── Sentinel watch (SSE) ───
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
            yield sse("started", {"asset_key": root, "futures": asset["futures"], "index_yf": index_yf, "proximity_pct": proximity_pct, "telegram": telegram_enabled(), "calendar": safe_json(cal)})
            chain = await asyncio.to_thread(opra.get_chain, root)
            walls = GEXEngine(multiplier=multiplier).calculate(chain)
            stored_chain = chain
            yield sse("walls", safe_json(walls))
            monitor = None
            cooldown_until = 0.0
            last_wall_sync = time.time()
            while True:
                mode = _session_mode()
                if mode == "RTH": spot = await asyncio.to_thread(_fetch_index_spot, index_yf, asset)
                else: spot = await asyncio.to_thread(_fetch_futures_spot, asset)
                basis = None
                try:
                    from .data.spot import get_spot
                    fr = get_spot(asset, "futures")
                    ir = get_spot(asset, "index")
                    if fr.get("price") and ir.get("price"): basis = fr["price"] - ir["price"]
                except Exception: basis = None
                if mode == "RTH" and basis is not None:
                    put_exec = walls["put_wall"] + basis
                    call_exec = walls["call_wall"] + basis
                else:
                    put_exec = walls["put_wall"]
                    call_exec = walls["call_wall"]
                walls_ok = (walls is not None and walls.get("put_wall") is not None and walls.get("call_wall") is not None)
                if not spot or not walls_ok:
                    yield sse("status", {"state": "WAITING", "message": "waiting for spot/walls", "armed": False, "mode": mode})
                    if not walls_ok:
                        try:
                            chain = await asyncio.to_thread(opra.get_chain, root, True)
                            cand = GEXEngine(multiplier=multiplier).calculate(chain)
                            if (cand.get("put_wall") is not None and cand.get("call_wall") is not None):
                                walls, stored_chain = cand, chain
                                last_wall_sync = time.time()
                                yield sse("walls", safe_json(walls))
                        except Exception: pass
                    await asyncio.sleep(poll_secs)
                    continue
                if time.time() - last_wall_sync > wall_refresh_secs:
                    try:
                        new_chain = await asyncio.to_thread(opra.get_chain, root, True)
                        new_walls = GEXEngine(multiplier=multiplier).calculate(new_chain)
                        if (new_walls.get("put_wall") is not None and new_walls.get("call_wall") is not None):
                            migrations = []
                            for wt in ("put_wall", "call_wall"):
                                ov, nv = walls.get(wt), new_walls.get(wt)
                                if ov is not None and abs(nv - ov) >= 0.01:
                                    label = "PUT" if wt == "put_wall" else "CALL"
                                    migrations.append({"wall": label, "old": ov, "new": nv})
                                    if monitor and getattr(monitor, "wall_type", None) == label: monitor = None
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
                        last_wall_sync = time.time()
                    except Exception: pass
                put_dist = (spot - walls["put_wall"]) / spot
                call_dist = (walls["call_wall"] - spot) / spot
                if monitor:
                    res = await asyncio.to_thread(monitor.update, spot)
                    sig, state = res.get("signal"), res.get("state")
                    wl = monitor.wall_type
                    ex = put_exec if wl == "PUT" else call_exec
                    if sig == "precursor":
                        _notify_bg(f"SFD {root} ⚠️", f"PRECURSOR: approaching {wl} wall @ {monitor.wall_level:,.0f}")
                        yield sse("precursor", {"wall": wl, "wall_level": monitor.wall_level, "exec": ex, "mode": mode, "basis": basis, "spot": spot, "state": state})
                    elif sig == "touch":
                        _notify_bg(f"SFD {root} •", f"TOUCH: at {wl} wall (approach {res.get('approach')})")
                        yield sse("touch", {"wall": wl, "approach": res.get("approach"), "exec": ex, "mode": mode, "basis": basis, "spot": spot, "state": state})
                    elif sig == "expired":
                        yield sse("expired", {"wall": wl, "spot": spot})
                        monitor = None
                    elif sig == "trade":
                        _notify_bg(f"SFD {root} ✅", f"CONFIRMED: {res.get('confirmation')} at {wl} wall — running AI...")
                        yield sse("confirmed", {"wall": wl, "confirmation": res.get("confirmation"), "approach": res.get("approach"), "exec": ex, "mode": mode, "basis": basis, "spot": spot})
                        yield sse("pipeline_start", {"message": "Running AI pipeline (30-90s)..."})
                        pipe = await asyncio.to_thread(lambda: run_intraday_pipeline(walls, chain=stored_chain, asset=asset, wall=wl))
                        vid = await asyncio.to_thread(lambda: log_verdict(pipe, walls, asset, wl, "browser_sentinel"))
                        tk = pipe.get("execution", {}) or {}
                        jd = pipe.get("judge", {}) or {}
                        _notify_bg(f"SFD {root} 🎫", f"{wl} wall | {jd.get('decision')} {tk.get('direction')}\nEntry {tk.get('entry',0):,.2f} / Stop {tk.get('stop_loss',0):,.2f}\nT1 {tk.get('target_1',0):,.2f} / T2 {tk.get('target_2',0):,.2f}")
                        yield sse("pipeline_done", {"verdict_id": vid, "pipeline": safe_json(pipe)})
                        monitor = None
                        cooldown_until = time.time() + cooldown_secs
                    elif state == "WATCHING": monitor = None
                    yield sse("status", {"state": state or ("WATCHING" if not monitor else monitor.state), "spot": spot, "put_dist": put_dist * 100, "call_dist": call_dist * 100, "armed": monitor is not None, "mode": mode})
                else:
                    if time.time() > cooldown_until:
                        new_monitor = None
                        if abs(put_dist) <= proximity_pct:
                            new_monitor = ConfirmationMonitor("PUT", walls["put_wall"], index_yf, proximity_pct, confirm_window_secs)
                        elif abs(call_dist) <= proximity_pct:
                            new_monitor = ConfirmationMonitor("CALL", walls["call_wall"], index_yf, proximity_pct, confirm_window_secs)
                        if new_monitor:
                            monitor = new_monitor
                            first = await asyncio.to_thread(monitor.update, spot)
                            wl = monitor.wall_type
                            ex = put_exec if wl == "PUT" else call_exec
                            if first.get("signal") == "precursor":
                                _notify_bg(f"SFD {root} ⚠️", f"PRECURSOR: approaching {wl} wall @ {monitor.wall_level:,.0f}")
                                yield sse("precursor", {"wall": wl, "wall_level": monitor.wall_level, "exec": ex, "mode": mode, "basis": basis, "spot": spot, "state": first.get("state")})
                    nearer = "PUT" if abs(put_dist) < abs(call_dist) else "CALL"
                    yield sse("status", {"state": monitor.state if monitor else "WATCHING", "spot": spot, "put_dist": put_dist * 100, "call_dist": call_dist * 100, "armed": monitor is not None, "nearest": nearer, "mode": mode})
                await asyncio.sleep(poll_secs)
        except asyncio.CancelledError: return
        except Exception as e:
            traceback.print_exc()
            yield sse("error", {"message": str(e)})
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

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
        m += 1
        if m > 12: m, y = 1, y + 1
    return 30

@app.get("/api/basis")
@endpoint_wrapper
def basis():
    def compute():
        import yfinance as yf
        R = 0.043
        pairs = [("NDX", "^NDX", "NQ=F", 0.007), ("SPX", "^GSPC", "ES=F", 0.013)]
        out = {}
        T = _quad_days()
        for key, idx_sym, fut_sym, q in pairs:
            try:
                fut_px = idx_px = None
                try:
                    from .data.spot import get_spot
                    asset = get_asset(key)
                    fr = get_spot(asset, "futures")
                    ir = get_spot(asset, "index")
                    if fr.get("price") and ir.get("price"):
                        fut_px = float(fr["price"])
                        idx_px = float(ir["price"])
                except Exception: pass
                if fut_px is None:
                    fut_px = float(yf.Ticker(fut_sym).fast_info["last_price"])
                    idx_px = float(yf.Ticker(idx_sym).fast_info["last_price"])
                fh = yf.Ticker(fut_sym).history(period="5d")["Close"]
                ih = yf.Ticker(idx_sym).history(period="5d")["Close"]
            except Exception: continue
            basis = fut_px - idx_px
            prev_basis = None
            if len(fh) >= 2 and len(ih) >= 2: prev_basis = float(fh.iloc[-2]) - float(ih.iloc[-2])
            fair = idx_px * (R - q) * T / 365.0
            out[key] = {"fut": round(fut_px, 2), "index": round(idx_px, 2), "basis": round(basis, 2), "basis_pct": round(basis / idx_px * 100, 3),
                        "prev_basis": round(prev_basis, 2) if prev_basis is not None else None, "day_change": round(basis - prev_basis, 2) if prev_basis is not None else None,
                        "fair": round(fair, 2), "vs_fair": round(basis - fair, 2), "days_to_exp": T}
        if bus:
            try: bus.publish("basis", {"pairs": out, "ts": time.time()}, last_ttl=120)
            except Exception: pass
        return {"pairs": out, "ts": time.time(), "provenance": "SPOT-LADDER + YFINANCE history"}
    return _cached("basis", compute, ttl=120)

if __name__ == "__main__":
    print("\n  SFD Chart Terminal -> http://127.0.0.1:8321\n")
    store.init()
    uvicorn.run(app, host="127.0.0.1", port=8321, log_level="info")