"""SFD Intraday Pipeline. 4-stage firm simulation for options flow.

Router: Fast chain for analysts, Deep chain for debate + judge.
Stage 4 is DETERMINISTIC (Principle 2): math builds the ticket, not the LLM.
Environment-aware (calendar blackout, no-fade, abstain), vol-surface-aware,
and confirmation-aware (tape state-machine context injected for the Judge).

v4 PATCHES:
- 6-field structured verdict grid
- Calibration anchor (cross-references empirical scorecard)
- Model provenance tracking (fixes import scoping for LAST_CALL_META)
"""
import json
from concurrent.futures import ThreadPoolExecutor

# 🟢 FIX: Import module instead of symbol to access live LAST_CALL_META
from ..llm import llm, extract_json
from .. import llm as llm_module

from ..quant.momentum import get_tape_speed
from ..quant.flow import compute_flow
from ..quant.vol_surface import analyze_surface, _surface_brief
from ..quant.mkt_calendar import opex_status
from ..quant.session_clock import session_phase
from ..exec.ticket import build_ticket
from ..assets import get_asset, active_asset_key
from .. import config
from .prompts import (GEX_SYS, FLOW_SYS, MOMENTUM_SYS, VOL_SURFACE_SYS,
                      FADE_SYS, BREAK_SYS, JUDGE_SYS)

_EMPTY_SURFACE = {"spikes": [], "pockets": [], "top_convexity_levels": [],
                  "skew": {"valid": False}, "term_structure": {"valid": False}}


def run_intraday_pipeline(gex_results, chain=None, asset=None, wall=None,
                          confirmation=None):
    if asset is None:
        asset = get_asset(active_asset_key())
    settings = config.load().get("settings", {})
    asset_key = asset["options_root"]

    # ── ENVIRONMENT: calendar + session (the master filter) ──
    cal = opex_status(skip_opex=settings.get("skip_opex", True))
    clock = session_phase()

    # ── LIVE MOMENTUM + FLOW + VOL SURFACE ───────────────────
    live_momentum = get_tape_speed(proxy=asset.get("tape_proxy", "SPY"), interval="5m")
    if chain is not None:
        flow_data, _ = compute_flow(chain, gex_results, asset_key)
        surface = analyze_surface(chain, gex_results["spot"])
    else:
        flow_data = {"conviction": 0.0, "sweep": False, "has_prior_snapshot": False,
                     "summary": "No chain available for flow computation."}
        surface = _EMPTY_SURFACE

    base_result = {"live_momentum": live_momentum, "flow": flow_data,
                   "calendar": cal, "session": clock, "surface": surface}

    # ── CALENDAR BLACKOUT: hard stop, no AI spend ────────────
    if not cal["trade_allowed"]:
        ticket = build_ticket(gex_results, "WAIT", wall, asset, settings, surface=surface)
        return {**base_result, "analysts": {}, "debate": {},
                "judge": {"decision": "WAIT", "confidence": 0.0,
                          "rationale": f"[CALENDAR BLACKOUT] {cal['guidance']}",
                          "calibration": None, "model_meta": {}},
                "execution": ticket}

    env_block = (f"MARKET ENVIRONMENT:\nSession: {clock['phase']} — {clock['note']}\n"
                 f"Calendar: {cal['risk_level']} — {cal['guidance']}")

    if confirmation:
        confirm_block = (
            "TAPE CONFIRMATION (from the confirmation state machine):\n"
            f"- Wall: {confirmation.get('wall')}\n"
            f"- Approach slope: {confirmation.get('approach')}\n"
            f"- Confirmation type: {confirmation.get('confirmation')}\n"
            "This is a TAPE-CONFIRMED setup (collision + flinch), not a speculative "
            "front-run. Weigh the confirmation type: REJECTION after a GENTLE approach "
            "favors a FADE; RECLAIM/BREAK_HOLD after a VIOLENT approach favors a BREAK "
            "or a failed-break fade.")
    else:
        confirm_block = ("TAPE CONFIRMATION: none (dashboard scan — price has not "
                         "confirmed a wall reaction). Treat any signal as lower conviction.")

    surf_brief = _surface_brief(surface)

    # ── STAGE 1: ANALYSTS (parallel, FAST chain) — 4 agents ──
    def _gex():
        return llm(GEX_SYS, f"GEX DATA:\n{json.dumps(gex_results, indent=2, default=str)}\n{env_block}", role="fast")
    def _flow():
        return llm(FLOW_SYS, f"LIVE OPTIONS FLOW DATA:\n{json.dumps(flow_data, indent=2, default=str)}\n{env_block}", role="fast")
    def _mom():
        return llm(MOMENTUM_SYS, f"LIVE MOMENTUM DATA:\n{json.dumps(live_momentum)}\n{env_block}", role="fast")
    def _volsurf():
        return llm(VOL_SURFACE_SYS, f"VOL SURFACE DATA:\n{json.dumps(surf_brief, indent=1, default=str)}\n{env_block}", role="fast")

    analysts = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        analysts["gex"] = ex.submit(_gex).result()
        analysts["flow"] = ex.submit(_flow).result()
        analysts["momentum"] = ex.submit(_mom).result()
        analysts["vol_surface"] = ex.submit(_volsurf).result()

    # ── STAGE 2: FADE vs BREAK DEBATE (DEEP chain) ────────────
    context = (f"{env_block}\n\n{confirm_block}\n\nGEX:\n{analysts['gex']}\n"
               f"FLOW:\n{analysts['flow']}\n"
               f"LIVE MOMENTUM:\n{analysts['momentum']}\n"
               f"VOL SURFACE:\n{analysts['vol_surface']}")
    fade_arg = llm(FADE_SYS, f"CONTEXT:\n{context}\nArgue to FADE the wall.", role="deep")
    break_arg = llm(BREAK_SYS, f"CONTEXT:\n{context}\nFADE says:\n{fade_arg}\nArgue to BREAK the wall.", role="deep")

    # ── STAGE 3: JUDGE (DEEP chain) ──────────────────────────
    debate = f"[FADE]\n{fade_arg}\n[BREAK]\n{break_arg}"
    
    # Build evidence pack for judge (Hard Math)
    evidence_pack = {
        "spot": gex_results.get("spot"),
        "put_wall": gex_results.get("put_wall"),
        "call_wall": gex_results.get("call_wall"),
        "flip": gex_results.get("flip_point"),
        "gamma_zone": gex_results.get("gamma_zone"),
        "basis": gex_results.get("basis"),
        "flow_conviction": flow_data.get("conviction"),
        "zero_dte": flow_data.get("zero_dte", {}).get("level"),
        "tape_speed": live_momentum.get("speed")
    }
    
    judge_prompt = (f"{env_block}\n\n{confirm_block}\n\n"
                    f"EVIDENCE PACK (HARD MATH):\n{json.dumps(evidence_pack, indent=2)}\n\n"
                    f"DEBATE:\n[FADE]\n{fade_arg}\n[BREAK]\n{break_arg}")
    
    judge_raw = llm(JUDGE_SYS, judge_prompt, role="deep", force_json=True)
    
    # 🟢 FIX: Capture which model answered via the module reference
    judge_meta = llm_module.LAST_CALL_META.copy()
    
    judge_out = extract_json(judge_raw)
    if not judge_out:
        judge_raw = llm(JUDGE_SYS, f"{env_block}\n\n{confirm_block}\n\nDEBATE:\n{debate}\nReturn ONLY the JSON object. No prose.", role="deep")
        judge_out = extract_json(judge_raw)
    if not judge_out:
        judge_out = {"decision": "WAIT", "confidence": 0.0,
                     "rationale": f"Judge parse failed. Raw output: {judge_raw[:300]}"}

    # ── STAGE 3.5a: VIX-EXPIRY NO-FADE gate ──────────────────
    if cal["no_fade"] and judge_out.get("decision") == "FADE":
        judge_out = {**judge_out, "decision": "WAIT",
                     "rationale": f"[NO-FADE DAY] {cal['guidance']} " + judge_out.get("rationale", "")}

    # ── STAGE 3.5b: ABSTAIN RULE (improvements.txt Part 6) ───
    min_conf = float(settings.get("min_confidence", 0.5))
    if judge_out.get("decision") != "WAIT" and judge_out.get("confidence", 0) < min_conf:
        judge_out = {**judge_out, "decision": "WAIT",
                     "rationale": (f"[ABSTAIN] Confidence {judge_out.get('confidence', 0):.2f} "
                                   f"below {min_conf} threshold. " + judge_out.get("rationale", ""))}

    # ── CALIBRATION: cross-reference scorecard for empirical hit rate ──
    calibration = None
    try:
        from ..quant import scorecard as sc
        summary = sc.summary()
        # Scorecard groups are keyed: "context|wall|decision"
        target_decision = judge_out.get("decision", "WAIT")
        for g in summary.get("groups", []):
            grp = g.get("group", "")
            parts = grp.split("|")
            if len(parts) >= 3 and parts[2] == target_decision and g.get("n", 0) >= 5:
                calibration = {
                    "hit_rate": round(g["hit_rate"], 2),
                    "n": g["n"],
                    "avg_ret": round(g.get("avg_h4_ret", 0), 2),
                    "group": grp,
                }
                break
    except Exception:
        pass

    # Inject metadata and calibration into judge output
    judge_out["calibration"] = calibration
    judge_out["model_meta"] = judge_meta

    # ── STAGE 4: DETERMINISTIC TICKET (Principle 2) ──────────
    ticket = build_ticket(gex_results, judge_out.get("decision", "WAIT"),
                          wall, asset, settings, surface=surface)

    return {**base_result,
            "analysts": analysts,
            "debate": {"fade": fade_arg, "break": break_arg},
            "judge": judge_out,
            "execution": ticket}