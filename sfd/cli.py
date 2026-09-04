"""
SFD CLI — The Intraday Terminal.
Modes:
    python -m sfd.cli                 # dashboard on active asset
    python -m sfd.cli NDX             # dashboard on NDX
    python -m sfd.cli NDX --sentinel  # SKIP dashboard/AI deep-run, go straight to watch
    python -m sfd.cli NDX --tv        # print TradingView level string only
    python -m sfd.cli --scorecard     # grade past verdicts
"""
import sys
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from .data import opra
from .quant.gex import GEXEngine
from .quant.momentum import get_tape_speed
from .quant.mkt_calendar import opex_status
from .quant.session_clock import session_phase
from .agents.pipeline import run_intraday_pipeline
from .assets import get_asset, active_asset_key
from . import store, config
from .llm import AI_ENABLED, providers_available

console = Console()


def fmt_level(v):
    """Format an optional price level; '—' when the engine couldn't compute it."""
    if v is None:
        return "[dim]—[/dim]"
    return f"${v:,.2f}"


def render_calendar(cal, clock):
    level = cal["risk_level"]
    color = {"BLACKOUT": "red", "NO_FADE": "magenta",
             "ELEVATED": "yellow", "NORMAL": "green"}.get(level, "white")
    console.print(Panel(
        f"[bold]Session:[/bold] {clock['phase']} @ {clock['local_time']}  |  "
        f"[bold]Calendar:[/bold] [{color}]{level}[/{color}]\n"
        f"[dim]{clock['note']}[/dim]\n"
        f"[dim]{cal['guidance']}[/dim]\n"
        f"[dim]days to OPEX: {cal['days_to_opex']}  ·  days to VIX-exp: {cal['days_to_vix']}[/dim]",
        title="🗓️ Market Environment", border_style=color, box=box.ROUNDED))


def render_zero_dte(res):
    zd = res.get("flow", {}).get("zero_dte", {})
    if not zd or not zd.get("valid"):
        return
    intensity = zd.get("intensity", 0)
    if intensity > 0.50:
        level, color = "EXTREME", "bold red"
    elif intensity > 0.30:
        level, color = "HIGH", "red"
    elif intensity > 0.15:
        level, color = "MODERATE", "yellow"
    else:
        level, color = "LOW", "green"
    sweep = (f"  [bold red]⚡ SWEEP: {zd.get('sweep_side', '?')}[/bold red]"
             if zd.get("sweep_count", 0) > 0 else "")
    pcr = zd.get("pcr", 0)
    console.print(Panel(
        f"[bold]Intensity:[/bold] [{color}]{level}[/{color}] "
        f"({intensity*100:.0f}% of total volume){sweep}\n"
        f"[dim]0DTE: {zd.get('call_volume',0):,} calls / {zd.get('put_volume',0):,} puts "
        f"| PCR {pcr:.2f} | near-money strikes: {zd.get('near_money_strikes',0)}[/dim]\n"
        f"[dim]{zd.get('interpretation', '')}[/dim]",
        title="🏎️ 0DTE Engine (F1 Car)",
        border_style="red" if intensity > 0.3 else "yellow", box=box.ROUNDED))


def render_surface(res):
    surf = res.get("surface", {})
    if not surf or not surf.get("spikes"):
        return
    skew = surf.get("skew", {})
    term = surf.get("term_structure", {})
    top2 = surf.get("top_convexity_levels", [])
    lines = []
    if top2:
        lines.append("[bold]Two Lines:[/bold] " + "  ·  ".join(f"${x:,.0f}" for x in top2))
    spikes = "  ".join(f"${s['strike']:,.0f}" for s in surf["spikes"][:3])
    pockets = "  ".join(f"${p['strike']:,.0f}" for p in surf["pockets"][:3])
    if spikes:
        lines.append(f"[red]▲ Spikes (reject):[/red] {spikes}")
    if pockets:
        lines.append(f"[green]▼ Pockets (accel):[/green] {pockets}")
    if skew.get("valid"):
        lines.append(f"[bold]Skew:[/bold] {skew['skew']:+.4f} — {skew['interpretation']}")
    if term.get("valid"):
        t_color = "red" if term["inverted"] else "green"
        lines.append(f"[bold]Term:[/bold] [{t_color}]{term['note']}[/{t_color}]")
    if lines:
        console.print(Panel("\n".join(lines) + "\n[dim]provenance: CBOE-DELAYED[/dim]",
                            title="🌋 Volatility Surface", border_style="magenta", box=box.ROUNDED))


def render_flow_panel(res):
    flow = res.get("flow", {})
    if not flow:
        return
    conv = flow.get("conviction", 0.0)
    if conv > 0.3:
        label, color = f"+{conv:.2f} CALL FLOW", "green"
    elif conv < -0.3:
        label, color = f"{conv:.2f} PUT FLOW", "red"
    else:
        label, color = f"{conv:+.2f} NEUTRAL", "yellow"
    sweep = " [bold red]⚡ SWEEP DETECTED[/bold red]" if flow.get("sweep") else ""
    prior = "yes (delta-based)" if flow.get("has_prior_snapshot") else "no (seeded)"
    console.print(Panel(
        f"[bold]Conviction:[/bold] [{color}]{label}[/{color}]{sweep}  |  "
        f"PCR: {flow.get('pcr', 'n/a')}  |  prior snapshot: {prior}\n"
        f"[dim]{flow.get('summary', '')}[/dim]\n"
        f"[dim]provenance: CBOE-DELAYED[/dim]",
        title="📊 Live Options Flow (dual-channel)", border_style=color, box=box.ROUNDED))


def render_ticket(res):
    t = res.get("execution", {})
    if not t:
        return
    direction = t.get("direction", "FLAT")
    decision = t.get("decision", "WAIT")
    color = {"LONG": "green", "SHORT": "red", "FLAT": "yellow"}.get(direction, "white")
    if direction == "FLAT":
        console.print(Panel(
            f"[bold]Decision:[/bold] {decision}\n"
            f"[dim]{t.get('note', 'No position.')}[/dim]\n"
            f"[dim]provenance: MATH[/dim]",
            title="Execution Ticket (deterministic)", border_style=color, box=box.ROUNDED))
        return
    act = ("[green]AT WALL — actionable[/green]" if t.get("actionable")
           else f"[yellow]{t.get('distance_to_wall_pct', 0):.2f}% from wall — not yet actionable[/yellow]")
    tbl = Table(title=f"Execution Ticket — {t.get('instrument', '')} "
                      f"[{t.get('mode', 'futures')}]  (provenance: MATH)",
                box=box.SIMPLE_HEAVY)
    tbl.add_column("Parameter", style="bold")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Decision", f"{decision} the {t.get('wall', '')} wall")
    tbl.add_row("Direction", f"[bold {color}]{direction}[/bold {color}]")
    tbl.add_row("Entry", fmt_level(t.get("entry")))
    tbl.add_row("Stop Loss", f"{fmt_level(t.get('stop_loss'))}  ({t.get('stop_ticks', 0)} ticks)")
    tbl.add_row("Target 1 (scale-out)", f"{fmt_level(t.get('target_1'))}  ({t.get('rr_target_1', 0)}R)")
    tbl.add_row("Target 2 (runner)", f"{fmt_level(t.get('target_2'))}  "
                                      f"({t.get('rr_target_2', 0)}R, {t.get('t2_source', '?')})")
    tbl.add_row("Risk / contract", f"${t.get('risk_usd', 0):,.2f}")
    tbl.add_row("Index→Fut basis", f"{t.get('basis', 0):+.2f}")
    console.print(tbl)
    console.print(f"  {act}")


def run_scorecard():
    from .exec.grading import grade_all, scorecard
    console.print(Panel.fit(
        "[bold cyan]📊 SFD SIGNAL SCORECARD[/bold cyan]\n"
        "[dim]The system grades itself — improvements.txt Part 8[/dim]",
        box=box.DOUBLE, border_style="cyan"))
    with console.status("[cyan]Grading verdicts against realized 5m tape...[/cyan]"):
        stats = grade_all()
    console.print(f"[dim]grading pass: {stats['graded']} graded "
                  f"({stats['win']}W/{stats['loss']}L/{stats['expired']}EXP/"
                  f"{stats['no_trade']}NO-TRADE) · {stats['waiting']} still waiting on tape[/dim]")
    sc = scorecard()
    if not sc["groups"]:
        console.print("[yellow]No graded actionable verdicts yet.[/yellow]")
    else:
        tbl = Table(title="Signal Scorecard — hit rate by setup", box=box.HEAVY_HEAD)
        for col in ("Decision", "Wall", "Regime", "N", "W", "L", "Exp", "Hit%", "Total R", "Avg R"):
            tbl.add_column(col, justify="right" if col not in ("Decision", "Wall", "Regime") else "left")
        for g in sc["groups"]:
            hr = f"{g['hit_rate']*100:.0f}%" if g["hit_rate"] is not None else "—"
            r_color = "green" if g["total_r"] > 0 else "red" if g["total_r"] < 0 else "white"
            tbl.add_row(g["decision"], g["wall"], g["regime"], str(g["n"]),
                        str(g["wins"]), str(g["losses"]), str(g["expired"]),
                        hr, f"[{r_color}]{g['total_r']:+.2f}[/{r_color}]", f"{g['avg_r']:+.2f}")
        console.print(tbl)
    chain = "[green]intact[/green]" if sc["chain_ok"] else "[red]BROKEN[/red]"
    console.print(Panel(
        f"Actionable verdicts: {sc['total_actionable']}  |  "
        f"NO-TRADE (discipline): {sc['no_trade_count']}  |  "
        f"Cumulative R: {sc['total_r']:+.2f}\n"
        f"Audit chain: {chain}",
        title="Honesty Anchor", box=box.ROUNDED,
        border_style="green" if sc["total_r"] >= 0 else "red"))


def launch_sentinel(asset_key, s, cal):
    """Shared launcher: calendar-gated MultiSentinel with config knobs."""
    if not cal["trade_allowed"]:
        console.print(f"[bold red]🚫 SENTINEL BLOCKED — calendar {cal['risk_level']}:[/bold red]")
        console.print(f"[red]{cal['guidance']}[/red]")
        return
    from .exec.watcher import MultiSentinel, sentinel_cfg
    cfg = sentinel_cfg()
    watchlist = s.get("watchlist") or [asset_key]
    if asset_key not in watchlist:
        watchlist = [asset_key] + watchlist
    console.print(
        f"[dim]knobs: proximity ±{cfg['proximity_pct']*100:.1f}% · "
        f"confirm {cfg['confirm_window_secs']}s · poll {cfg['poll_secs']}s · "
        f"wall-refresh {cfg['wall_refresh_secs']}s · cooldown {cfg['post_signal_cooldown_secs']}s[/dim]")
    MultiSentinel(watchlist).run()


def main():
    argv = sys.argv[1:]
    scorecard_mode = "--scorecard" in argv
    sentinel_only = "--sentinel" in argv or "--watch" in argv
    tv_mode = "--tv" in argv
    positional = [a for a in argv if not a.startswith("--")]

    store.init()

    if scorecard_mode:
        run_scorecard()
        return

    asset_key = positional[0].upper() if positional else active_asset_key()
    asset = get_asset(asset_key)
    s = config.settings()

    avail = ", ".join(providers_available()) or "none"
    ai = f"[green]{avail}[/green]" if avail else "[red]none[/red]"

    console.print(Panel.fit(
        f"[bold magenta]SKIA FLOW DESK — INTRADAY TERMINAL[/bold magenta]\n"
        f"[dim]asset {asset_key} -> trade {asset['futures']} · stop {s['stop_ticks']}t · AI: {ai}[/dim]",
        box=box.DOUBLE, border_style="magenta"))

    cal = opex_status(skip_opex=s.get("skip_opex", True))
    clock = session_phase()
    render_calendar(cal, clock)

    # ── MODE: sentinel-only (skip dashboard + AI deep run) ─────
    if sentinel_only:
        console.print("[cyan]👁️ Sentinel-only mode — skipping dashboard & AI deep run.[/cyan]")
        launch_sentinel(asset_key, s, cal)
        return

    # ── MODE: TV level string ──────────────────────────────────
    if tv_mode:
        from .quant.tvlevels import build_tv_string, fetch_open_atr
        from .quant.vol_surface import analyze_surface
        snap = opra.get_chain(asset["options_root"])
        gex = GEXEngine(multiplier=asset["multiplier"]).calculate(snap)
        surf = analyze_surface(snap, snap.spot)
        o, a = fetch_open_atr(asset["tape_proxy"])
        tv = build_tv_string(snap, gex, surf, asset["multiplier"], o, a)
        console.print(Panel(
            f"[white]{tv}[/white]\n\n"
            f"[dim]provenance: CBOE-DELAYED · paste into the matching src input "
            f"of the GEX Daily Levels indicator[/dim]",
            title=f"📺 TV LEVEL STRING — {asset_key}", border_style="cyan", box=box.ROUNDED))
        return

    # ── MODE: full dashboard ───────────────────────────────────
    with console.status(f"[cyan]Fetching {asset['options_root']} Options Chain & Calculating GEX...[/cyan]"):
        snap = opra.get_chain(asset["options_root"])
        gex = GEXEngine(multiplier=asset["multiplier"]).calculate(snap)

    if gex.get("put_wall") is None or gex.get("call_wall") is None:
        console.print("[yellow]⚠️ Incomplete wall data — options chain in flux. "
                      "Levels shown as '—'. Re-run in a minute.[/yellow]")

    gex_tbl = Table(title=f"Structural Walls ({asset['options_root']} · {gex.get('wall_dte', 90)}d OI)",
                    box=box.HEAVY_HEAD)
    gex_tbl.add_column("Metric", style="bold cyan")
    gex_tbl.add_column("Value", justify="right")
    gex_tbl.add_row("Spot", fmt_level(gex.get("spot")))
    gex_tbl.add_row("Put Wall (Support)", fmt_level(gex.get("put_wall")))
    gex_tbl.add_row("Call Wall (Resist)", fmt_level(gex.get("call_wall")))
    gex_tbl.add_row("Flip Point", fmt_level(gex.get("flip_point")))
    gex_tbl.add_row("Regime (0-14d)", f"{gex.get('regime', '?')} ({gex.get('net_gex', 0):.2e})")
    console.print(gex_tbl)

    tape = get_tape_speed(asset["tape_proxy"], "5m")
    speed_color = {"VIOLENT": "red", "GENTLE": "green", "MODERATE": "yellow"}.get(tape.get("speed"), "white")
    console.print(Panel(
        f"[bold]Tape Speed:[/bold] [{speed_color}]{tape.get('speed', 'UNKNOWN')}[/{speed_color}]  |  "
        f"Slope: {tape.get('slope_pct', 0):+.3f}%/bar  |  Vol Ratio: {tape.get('vol_ratio', 0):.2f}x\n"
        f"[dim]{tape.get('summary', '')}[/dim]",
        title=f"Live 5m Tape ({asset['tape_proxy']} Proxy)", border_style=speed_color, box=box.ROUNDED))

    if not AI_ENABLED:
        console.print("[yellow]AI disabled. Set provider keys in .env to run the pipeline.[/yellow]")
        return

    with console.status("[magenta]Running Intraday AI Pipeline (Fade vs Break)...[/magenta]"):
        res = run_intraday_pipeline(gex, chain=snap, asset=asset)

    render_zero_dte(res)
    render_surface(res)
    render_flow_panel(res)

    judge = res.get("judge", {})
    decision = judge.get("decision", "WAIT")
    color = {"FADE": "green", "BREAK": "red", "WAIT": "yellow"}.get(decision, "white")
    console.print(Panel(
        f"[bold]Decision:[/bold] [{color}]{decision}[/{color}] (Confidence: {judge.get('confidence', 0):.1f})\n"
        f"[italic]{judge.get('rationale', '')}[/italic]",
        title="Risk Manager Verdict", border_style=color, box=box.ROUNDED))

    render_ticket(res)

    from .exec.logger import log_verdict
    vid = log_verdict(res, gex, asset, wall=None, context="dashboard")
    console.print(f"[dim]verdict #{vid} logged to scorecard (hash-chained)[/dim]")

    console.print("\n[cyan]Dashboard run complete.[/cyan]")
    launch = input("Launch Sentinel background watcher? (y/n): ").strip().lower()
    if launch == 'y':
        launch_sentinel(asset_key, s, cal)


if __name__ == "__main__":
    main()