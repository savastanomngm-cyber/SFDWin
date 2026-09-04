"""
SFD Sentinel — single & multi-asset watch mode + alert dispatch (FULLY PATCHED).

Hardening layers:
  • Config knobs from flowdesk.yaml (settings.sentinel) with code defaults
  • Timeout-bounded fetches on daemon threads (a stalled feed can never hang)
  • Background chain retries (broken asset can't stall the shared loop)
  • NO_WALLS degraded status instead of silent blanks
  • Session-mode awareness:
      RTH     -> watch INDEX spot vs raw walls; alerts carry TRANSLATED NQ levels
      NQ_ONLY -> watch NQ FUTURES spot vs RAW walls; alerts carry RAW levels
  • Real-time spot ladder (sfd/data/spot.py: TradingView -> Polygon -> yfinance)
"""
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf
from rich.console import Console
from rich.panel import Panel
from rich import box

from ..data import opra
from ..quant.gex import GEXEngine
from ..quant.mkt_calendar import opex_status
from ..quant.session_clock import session_phase
from ..agents.pipeline import run_intraday_pipeline
from ..assets import get_asset, index_yf_symbol
from .. import config, store
from .logger import log_verdict
from .confirmation import ConfirmationMonitor
from .notify import alert

console = Console()

# Fallback defaults (used when flowdesk.yaml omits a knob)
DEFAULTS = {
    "proximity_pct": 0.003,
    "confirm_window_secs": 480,
    "poll_secs": 15,
    "wall_refresh_secs": 300,
    "post_signal_cooldown_secs": 600,
}
RTH_PHASES = ("OPEN_DRIVE", "MIDDAY", "THETA_BURN", "POWER_HOUR")


def sentinel_cfg():
    """Read settings.sentinel from config, merged over defaults."""
    try:
        s = config.load().get("settings", {}) or {}
    except Exception:
        s = {}
    g = s.get("sentinel") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in g:
            try:
                out[k] = type(DEFAULTS[k])(g[k])
            except Exception:
                pass
    return out


def _with_timeout(fn, timeout_secs, *args):
    """Run fn(*args) on a daemon thread; raise TimeoutError if it stalls."""
    result = {}
    def target():
        try:
            result["v"] = fn(*args)
        except Exception as e:
            result["e"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_secs)
    if t.is_alive():
        raise TimeoutError(f"fetch timed out after {timeout_secs}s")
    if "e" in result:
        raise result["e"]
    return result.get("v")


class Sentinel:
    """Per-asset state machine. step() returns events; no printing here."""
    def __init__(self, asset_key, proximity_pct=None, confirm_window_secs=None):
        self.cfg = sentinel_cfg()
        self.asset = get_asset(asset_key)
        self.symbol = self.asset["options_root"]
        self.proxy = self.asset["tape_proxy"]
        self.index_yf = index_yf_symbol(self.asset)
        self.fut_yf = self.asset["futures"] + "=F"
        self.futures = self.asset["futures"]
        self.multiplier = self.asset["multiplier"]
        self.proximity_pct = proximity_pct or self.cfg["proximity_pct"]
        self.confirm_window_secs = confirm_window_secs or self.cfg["confirm_window_secs"]
        self.walls = None
        self.chain = None
        self.last_sync = 0
        self.last_attempt = 0
        self.cooldown = 0
        self.monitor = None
        self._fetch_thread = None
        self._pending = None

    # ── session mode ──────────────────────────────────────────
    def _mode(self):
        return "RTH" if session_phase().get("phase", "") in RTH_PHASES else "NQ_ONLY"

    # ── spots (ladder first, yfinance fallback) ───────────────
    def _index_sync(self):
        try:
            px = yf.Ticker(self.index_yf).fast_info["last_price"]
            if px:
                return float(px)
        except Exception:
            pass
        try:
            h = yf.Ticker(self.index_yf).history(period="1d", interval="1m")
            if not h.empty:
                return float(h["Close"].iloc[-1])
        except Exception:
            pass
        return None

    def _fut_sync(self):
        try:
            px = yf.Ticker(self.fut_yf).fast_info["last_price"]
            if px:
                return float(px)
        except Exception:
            pass
        try:
            h = yf.Ticker(self.fut_yf).history(period="1d", interval="1m")
            if not h.empty:
                return float(h["Close"].iloc[-1])
        except Exception:
            pass
        return None

    def get_index_spot(self):
        try:
            from ..data.spot import get_spot
            r = get_spot(self.asset, "index")
            if r and r.get("price"):
                return r["price"]
        except Exception:
            pass
        try:
            return _with_timeout(self._index_sync, 10)
        except Exception:
            return None

    def get_futures_spot(self):
        try:
            from ..data.spot import get_spot
            r = get_spot(self.asset, "futures")
            if r and r.get("price"):
                return r["price"]
        except Exception:
            pass
        try:
            return _with_timeout(self._fut_sync, 10)
        except Exception:
            return None

    def _basis(self):
        i, f = self.get_index_spot(), self.get_futures_spot()
        return (f - i) if (i and f) else None

    # ── walls ────────────────────────────────────────────────
    def _walls_ok(self):
        return (self.walls is not None
                and self.walls.get("put_wall") is not None
                and self.walls.get("call_wall") is not None)

    def _apply(self, chain, walls):
        self.chain, self.walls = chain, walls
        self.last_sync = time.time()

    def sync_walls_blocking(self, timeout_secs=45):
        try:
            chain = _with_timeout(opra.get_chain, timeout_secs, self.symbol, False)
            if chain is None:
                return False
            walls = GEXEngine(multiplier=self.multiplier).calculate(chain)
            if walls.get("put_wall") is not None and walls.get("call_wall") is not None:
                self._apply(chain, walls)
                return True
        except Exception:
            pass
        return False

    def _kick(self):
        """Non-blocking background chain fetch; result lands in _pending."""
        if self._fetch_thread and self._fetch_thread.is_alive():
            return
        def work():
            try:
                chain = opra.get_chain(self.symbol, True)
                walls = GEXEngine(multiplier=self.multiplier).calculate(chain)
                self._pending = (chain, walls)
            except Exception:
                pass
        self._fetch_thread = threading.Thread(target=work, daemon=True)
        self._fetch_thread.start()

    def _adopt(self):
        p = self._pending
        if p:
            self._pending = None
            chain, walls = p
            if (walls is not None and walls.get("put_wall") is not None
                    and walls.get("call_wall") is not None):
                self._apply(chain, walls)
                return True
        return False

    def setup(self):
        store.init()
        s = config.load().get("settings", {})
        cal = opex_status(skip_opex=s.get("skip_opex", True))
        if not cal["trade_allowed"]:
            return ("blackout", cal)
        console.print(f"[dim]⏳ {self.symbol}: fetching options chain...[/dim]")
        if not self.sync_walls_blocking():
            console.print(f"[yellow]⚠️ {self.symbol}: walls incomplete — "
                          f"will keep retrying in the background[/yellow]")
        mode = self._mode()
        console.print(f"[dim]   mode: {mode} "
                      f"({'index spot vs raw walls; alerts=translated' if mode == 'RTH' else 'NQ spot vs RAW walls; alerts=raw'})[/dim]")
        return ("ok", cal)

    def step(self):
        """One monitoring iteration. Returns a list of event dicts."""
        events = []
        mode = self._mode()
        spot = self.get_index_spot() if mode == "RTH" else self.get_futures_spot()
        if not spot:
            return events

        self._adopt()
        walls_ok = self._walls_ok()
        if not walls_ok and time.time() - self.last_attempt > 60:
            self.last_attempt = time.time()
            self._kick()

        basis = self._basis()
        if walls_ok:
            if mode == "RTH" and basis is not None:
                put_exec = self.walls["put_wall"] + basis
                call_exec = self.walls["call_wall"] + basis
            else:
                put_exec = self.walls["put_wall"]
                call_exec = self.walls["call_wall"]
        else:
            put_exec = call_exec = None

        if not walls_ok:
            events.append(dict(type="status", asset=self.symbol, state="NO_WALLS",
                               spot=spot, put_dist=None, call_dist=None,
                               nearest=None, armed=False, mode=mode))
            return events

        if time.time() - self.last_sync > self.cfg["wall_refresh_secs"]:
            self._kick()

        put_dist = (spot - self.walls["put_wall"]) / spot
        call_dist = (self.walls["call_wall"] - spot) / spot

        if self.monitor:
            res = self.monitor.update(spot)
            sig, state = res.get("signal"), res.get("state")
            wl = self.monitor.wall_type
            ex = put_exec if wl == "PUT" else call_exec
            if sig == "precursor":
                events.append(dict(type="precursor", asset=self.symbol, wall=wl,
                                   level=self.monitor.wall_level, exec=ex,
                                   mode=mode, basis=basis, spot=spot))
            elif sig == "touch":
                events.append(dict(type="touch", asset=self.symbol, wall=wl,
                                   approach=res.get("approach"), exec=ex,
                                   mode=mode, basis=basis, spot=spot))
            elif sig == "expired":
                events.append(dict(type="expired", asset=self.symbol,
                                   wall=wl, spot=spot))
                self.monitor = None
            elif sig == "trade":
                events.append(dict(type="confirmed", asset=self.symbol, wall=wl,
                                   confirmation=res.get("confirmation"),
                                   approach=res.get("approach"), exec=ex,
                                   mode=mode, basis=basis, spot=spot))
                self.monitor = None
                self.cooldown = time.time() + self.cfg["post_signal_cooldown_secs"]
            elif state == "WATCHING":
                self.monitor = None
            st = state
        else:
            if time.time() > self.cooldown:
                mon = None
                if abs(put_dist) <= self.proximity_pct:
                    mon = ConfirmationMonitor("PUT", self.walls["put_wall"], self.index_yf,
                                              self.proximity_pct, self.confirm_window_secs)
                elif abs(call_dist) <= self.proximity_pct:
                    mon = ConfirmationMonitor("CALL", self.walls["call_wall"], self.index_yf,
                                              self.proximity_pct, self.confirm_window_secs)
                if mon:
                    self.monitor = mon
                    first = self.monitor.update(spot)
                    if first.get("signal") == "precursor":
                        events.append(dict(type="precursor", asset=self.symbol,
                                           wall=mon.wall_type, level=mon.wall_level,
                                           exec=put_exec if mon.wall_type == "PUT" else call_exec,
                                           mode=mode, basis=basis, spot=spot))
            st = self.monitor.state if self.monitor else "WATCHING"

        nearer = "PUT" if abs(put_dist) < abs(call_dist) else "CALL"
        events.append(dict(type="status", asset=self.symbol, state=st, spot=spot,
                           put_dist=put_dist * 100, call_dist=call_dist * 100,
                           nearest=nearer, armed=self.monitor is not None,
                           mode=mode, put_exec=put_exec, call_exec=call_exec))
        return events


def _fmt_status(s):
    p = f"P{s['put_dist']:+.1f}%" if s.get("put_dist") is not None else "P—"
    c = f"C{s['call_dist']:+.1f}%" if s.get("call_dist") is not None else "C—"
    return f"{s['asset']}[{s.get('mode', '?')}] ${s['spot']:,.0f} {p} {c} [{s['state']}]"


def _run_confirmed(sent, ev):
    wall = ev["wall"]
    try:
        pipe = run_intraday_pipeline(sent.walls, chain=sent.chain, asset=sent.asset, wall=wall)
        judge = pipe.get("judge", {}) or {}
        ticket = pipe.get("execution", {}) or {}
        log_verdict(pipe, sent.walls, sent.asset, wall, "sentinel_confirmed")
        decision = judge.get("decision", "WAIT")
        direction = ticket.get("direction", "FLAT")
        color = {"FADE": "green", "BREAK": "red", "WAIT": "yellow"}.get(decision, "white")
        console.print(Panel(
            f"[bold]VERDICT:[/bold] [{color}]{decision}[/{color}] · {direction}\n"
            f"[bold]ENTRY:[/bold] {ticket.get('entry',0):,.2f}  [bold]STOP:[/bold] {ticket.get('stop_loss',0):,.2f}\n"
            f"[bold]T1:[/bold] {ticket.get('target_1',0):,.2f}  [bold]T2:[/bold] {ticket.get('target_2',0):,.2f}\n\n"
            f"[dim]{str(judge.get('rationale',''))[:400]}[/dim]",
            title=f"✅ {sent.symbol} {wall} WALL — {ev.get('confirmation','')}",
            border_style=color, box=box.HEAVY))
        alert(f"SFD {sent.symbol} 🎫",
              f"{wall} wall | {decision} {direction} | {ev.get('mode','?')}\n"
              f"NQ level {ev.get('exec',0):,.2f}\n"
              f"Entry {ticket.get('entry',0):,.2f} / Stop {ticket.get('stop_loss',0):,.2f}\n"
              f"T1 {ticket.get('target_1',0):,.2f} / T2 {ticket.get('target_2',0):,.2f}")
    except Exception as e:
        console.print(f"[red]pipeline error ({sent.symbol}): {e}[/red]")


def _dispatch(sent, ev, executor):
    t, a = ev["type"], ev["asset"]
    ex = ev.get("exec")
    ex_txt = f" | NQ level {ex:,.0f} ({ev.get('mode')})" if ex else ""
    if t == "precursor":
        msg = f"⚠️ PRECURSOR {a}: approaching {ev['wall']} wall @ {ev['level']:,.0f}{ex_txt}"
        console.print(f"\n[yellow]{msg}[/yellow]")
        alert(f"SFD {a}", msg)
    elif t == "touch":
        msg = f"• TOUCH {a}: at {ev['wall']} wall (approach {ev.get('approach')}){ex_txt}"
        console.print(f"\n[cyan]{msg}[/cyan]")
        alert(f"SFD {a}", msg)
    elif t == "expired":
        console.print(f"\n[dim]○ {a}: no confirmation at {ev['wall']} — stand down[/dim]")
    elif t == "confirmed":
        msg = f"✅ CONFIRMED {a}: {ev.get('confirmation')} at {ev['wall']} wall{ex_txt} — running AI..."
        console.print(f"\n[bold green]{msg}[/bold green]")
        alert(f"SFD {a} ✅", msg)
        executor.submit(_run_confirmed, sent, ev)


class MultiSentinel:
    """Watch N assets in a single poll loop."""
    def __init__(self, asset_keys, proximity_pct=0.003, confirm_window_secs=480):
        self.cfg = sentinel_cfg()
        self.sents = []
        for k in asset_keys:
            s = Sentinel(k, proximity_pct, confirm_window_secs)
            status, cal = s.setup()
            if status == "blackout":
                console.print(f"[red]🚫 {k}: calendar {cal['risk_level']} — skipped.[/red]")
            else:
                console.print(f"[green]👁️ watching {k} ({s.symbol} → {s.futures})[/green]")
                self.sents.append(s)
        self.executor = ThreadPoolExecutor(max_workers=2)

    def run(self):
        if not self.sents:
            console.print("[red]No assets to watch.[/red]")
            return
        console.print(Panel.fit(
            f"[bold magenta]SFD MULTI-SENTINEL ACTIVE[/bold magenta]\n"
            f"Watching: {', '.join(s.symbol for s in self.sents)} | poll {self.cfg['poll_secs']}s\n"
            f"[dim]RTH = index vs raw walls (alerts translated) · NQ_ONLY = NQ vs RAW walls[/dim]",
            box=box.DOUBLE, border_style="magenta"))
        try:
            while True:
                statuses = []
                for sent in self.sents:
                    for ev in sent.step():
                        if ev["type"] == "status":
                            statuses.append(ev)
                        else:
                            _dispatch(sent, ev, self.executor)
                ts = time.strftime("%H:%M:%S")
                line = " | ".join(_fmt_status(s) for s in statuses)
                print(f"\r[{ts}] {line}   ", end="", flush=True)
                time.sleep(self.cfg["poll_secs"])
        except KeyboardInterrupt:
            console.print("\n[red]Sentinel terminated.[/red]")