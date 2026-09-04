"""Verdict logging — every pipeline run becomes a gradable record.
Distances (stop_dist, t1_dist) are basis-invariant point gaps,
so grading can anchor on the wall in index terms."""
from .. import store


def log_verdict(res, gex, asset, wall=None, context="dashboard"):
    judge = res.get("judge", {}) or {}
    ticket = res.get("execution", {}) or {}
    flow = res.get("flow", {}) or {}
    mom = res.get("live_momentum", {}) or {}

    decision = judge.get("decision", "WAIT")
    actionable = decision in ("FADE", "BREAK") and ticket.get("direction") != "FLAT"

    stop_dist = abs(ticket.get("entry", 0) - ticket.get("stop_loss", 0)) if actionable else 0.0
    t1_dist = abs(ticket.get("target_1", 0) - ticket.get("entry", 0)) if actionable else 0.0

    rec = {
        "asset": asset["options_root"],
        "context": context,
        "wall": wall or ticket.get("wall"),
        "decision": decision,
        "confidence": judge.get("confidence", 0),
        "regime": gex.get("regime"),
        "spot": gex.get("spot"),
        "put_wall": gex.get("put_wall"),
        "call_wall": gex.get("call_wall"),
        "flip": gex.get("flip_point"),
        "net_gex": gex.get("net_gex"),
        "slope": mom.get("slope_pct"),
        "vol_ratio": mom.get("vol_ratio"),
        "flow_conviction": flow.get("conviction"),
        "flow_sweep": 1 if flow.get("sweep") else 0,
        "direction": ticket.get("direction", "FLAT"),
        "entry": ticket.get("entry", 0),
        "stop": ticket.get("stop_loss", 0),
        "target_1": ticket.get("target_1", 0),
        "target_2": ticket.get("target_2", 0),
        "stop_dist": stop_dist,
        "t1_dist": t1_dist,
        "rationale": str(judge.get("rationale", ""))[:600],
    }
    return store.save_verdict(rec)