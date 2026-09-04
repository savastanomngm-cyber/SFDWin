"""
SFD Wall Interpreter v1 — automatic AI commentary on structural GEX changes.
Deterministic math classifies the change; the LLM only narrates (v4 principle:
judgment proposes, math disposes). Multi-asset, thread-safe, quota-aware.

Change taxonomy:
  REAL_MIGRATION  raw OI wall moved >=25pts and persisted PERSIST_POLLS polls -> ping + AI
  DATA_ARTIFACT   announced migration reverted inside ARTIFACT_WINDOW -> auto-retract
  BASIS_DRIFT     raw stable, translated (raw+basis) moved -> silent MATH line
  REGIME_FLIP     net GEX sign changed -> ping + AI
  FLIP_CROSS      spot crossed zero-gamma flip -> ping + AI
"""
import time, json, threading
from collections import deque
from ..llm import llm, LAST_CALL_META

PERSIST_POLLS   = 2       # migration must survive 2 consecutive polls
ARTIFACT_WINDOW = 240     # secs; revert inside this => artifact
MIN_AI_GAP      = 120     # secs between AI narrations per asset (quota guard)
WALL_MOVE_MIN   = 25.0    # pts; smaller raw moves ignored
BASIS_DRIFT_MIN = 15.0    # pts; basis move worth reporting
PING_TYPES      = {"REAL_MIGRATION", "REGIME_FLIP", "FLIP_CROSS"}

INTERPRETER_SYS = """You are the SFD Wall Interpreter for an intraday NQ/NDX gamma desk.
You receive a STRUCTURED EVENT + current GEX state. Write a 2-4 sentence operator update.
Rules:
1. State the change class (real OI migration / basis translation / data artifact).
2. State the ACTIONABLE levels NOW: during RTH use translated (raw+basis) futures levels; during NQ-only use raw levels. Cite the numbers given.
3. State ONE invalidation condition consistent with the regime (POSITIVE gamma = fade walls; NEGATIVE gamma = walls break, favor momentum).
4. Never invent numbers. No filler."""


class WallInterpreter:
    def __init__(self):
        self._lock = threading.Lock()
        self._st = {}                       # root -> per-asset tracker

    def _get(self, root):
        if root not in self._st:
            self._st[root] = {"last": None, "pending": {}, "announced": {},
                              "feed": deque(maxlen=200), "last_ai": 0.0}
        return self._st[root]

    # ── public API ─────────────────────────────────────────────
    def on_state(self, root, g, basis=None, ctx=None):
        """Feed one GEX snapshot. Returns list of new events (may be empty)."""
        s = {"ts": time.time(), "spot": g.get("spot"),
             "put_raw": g.get("put_wall"), "call_raw": g.get("call_wall"),
             "flip": g.get("flip_point"), "regime": g.get("regime"),
             "gamma_zone": g.get("gamma_zone"), "basis": basis}
        if basis is not None and s["put_raw"] is not None:
            s["put_fut"] = round(s["put_raw"] + basis, 2)
            s["call_fut"] = round(s["call_raw"] + basis, 2) if s["call_raw"] else None
        if ctx:
            s.update(ctx)

        st = self._get(root)
        with self._lock:
            events = self._diff(st, st["last"], s) if st["last"] else []
            st["last"] = s

        out = []
        for ev in events:
            handled = self._handle(st, ev, s, root)
            if handled:
                out.append(handled)
        return out

    def history(self, root, limit=50):
        st = self._st.get(root)
        return list(st["feed"])[-limit:] if st else []

    def since(self, root, ts):
        st = self._st.get(root)
        return [e for e in (st["feed"] if st else []) if e.get("ts", 0) > ts]

    # ── deterministic classifier (the math) ────────────────────
    def _diff(self, st, o, n):
        ev = []
        if not o:
            return ev
        for side in ("put_raw", "call_raw"):
            ov, nv = o.get(side), n.get(side)
            if ov is None or nv is None:
                continue
            d = nv - ov
            if abs(d) >= WALL_MOVE_MIN:
                e = self._wall_move(st, side, ov, nv, n["ts"])
                if e:
                    ev.append(e)
        # basis drift: raw stable but translated moved
        ob, nb = o.get("basis"), n.get("basis")
        if ob is not None and nb is not None and abs(nb - ob) >= BASIS_DRIFT_MIN:
            raw_moved = any(abs((n.get(s) or 0) - (o.get(s) or 0)) >= WALL_MOVE_MIN
                            for s in ("put_raw", "call_raw"))
            if not raw_moved:
                ev.append({"type": "BASIS_DRIFT", "basis_old": round(ob, 1),
                           "basis_new": round(nb, 1),
                           "put_fut_old": o.get("put_fut"), "put_fut_new": n.get("put_fut"),
                           "call_fut_old": o.get("call_fut"), "call_fut_new": n.get("call_fut")})
        if o.get("regime") and n.get("regime") and o["regime"] != n["regime"]:
            ev.append({"type": "REGIME_FLIP", "from": o["regime"], "to": n["regime"]})
        if o.get("flip") and n.get("flip") and o.get("spot") and n.get("spot"):
            if (o["spot"] - o["flip"]) * (n["spot"] - n["flip"]) < 0:
                ev.append({"type": "FLIP_CROSS", "spot": n["spot"], "flip": n["flip"],
                           "dir": "above" if n["spot"] > n["flip"] else "below"})
        return ev

    def _wall_move(self, st, side, ov, nv, now):
        # revert of a recently announced migration => artifact, retract it
        ann = st["announced"].get(side)
        if ann and abs(nv - ann["from"]) < WALL_MOVE_MIN and (now - ann["ts"]) < ARTIFACT_WINDOW:
            st["announced"].pop(side, None)
            return {"type": "DATA_ARTIFACT", "side": side,
                    "fake_level": ann["to"], "true_level": nv}
        p = st["pending"].get(side)
        if not p or p["to"] != nv:
            st["pending"][side] = {"ts": now, "from": ov, "to": nv, "polls": 1}
            return None                       # candidate — stay silent
        p["polls"] += 1
        if p["polls"] >= PERSIST_POLLS:
            st["pending"].pop(side, None)
            st["announced"][side] = {"ts": now, "from": p["from"], "to": nv}
            return {"type": "REAL_MIGRATION", "side": side,
                    "from": p["from"], "to": nv, "delta": round(nv - p["from"], 1)}
        return None

    # ── output: log + narrate with provenance ──────────────────
    def _handle(self, st, ev, s, root):
        ev = {**ev, "ts": s["ts"], "root": root, "state": s}
        if ev["type"] == "DATA_ARTIFACT":
            ev["narrative"] = (f"⚠️ RETRACT: {ev['side']} wall {ev['fake_level']:,.0f} was a data "
                               f"artifact (reverted <{ARTIFACT_WINDOW // 60}min). True level "
                               f"{ev['true_level']:,.0f}. No action, thesis unchanged.")
            ev["provenance"] = "MATH"
        elif ev["type"] == "BASIS_DRIFT":
            ev["narrative"] = (f"Basis {ev['basis_old']:+.0f}→{ev['basis_new']:+.0f}: translated walls "
                               f"floated to {ev.get('put_fut_new') or 0:,.0f}/{ev.get('call_fut_new') or 0:,.0f}. "
                               f"RAW OI unchanged — repriced levels, thesis intact.")
            ev["provenance"] = "MATH"
        elif ev["type"] in PING_TYPES:
            now = time.time()
            if now - st["last_ai"] < MIN_AI_GAP:
                ev["narrative"] = self._template(ev, s)
                ev["provenance"] = "MATH"
            else:
                st["last_ai"] = now
                try:
                    txt = llm(INTERPRETER_SYS,
                              f"EVENT: {json.dumps({k: v for k, v in ev.items() if k != 'state'}, default=str)}\n"
                              f"STATE: {json.dumps(s, default=str)}",
                              role="fast", temperature=0.2)
                    ev["narrative"] = txt or self._template(ev, s)
                    ev["provenance"] = f"AI · {LAST_CALL_META.get('model', '?')}@{LAST_CALL_META.get('provider', '?')}"
                except Exception:
                    ev["narrative"] = self._template(ev, s)
                    ev["provenance"] = "MATH"
        else:
            return None
        st["feed"].append(ev)
        return ev

    @staticmethod
    def _template(ev, s):
        t = ev["type"]
        pf, cf = s.get("put_fut") or s.get("put_raw"), s.get("call_fut") or s.get("call_raw")
        lvl = f"Actionable: PUT {pf:,.0f} / CALL {cf:,.0f}." if pf and cf else ""
        if t == "REAL_MIGRATION":
            return (f"{ev['side'].upper()} wall migrated {ev['from']:,.0f}→{ev['to']:,.0f} (persisted). "
                    f"{lvl} Invalidation: spot closing beyond the moved wall.")
        if t == "REGIME_FLIP":
            rule = "fade walls" if ev["to"] == "POSITIVE" else "walls break — favor momentum, no fades"
            return f"Regime flipped {ev['from']}→{ev['to']}: {rule}. {lvl}"
        if t == "FLIP_CROSS":
            return (f"Spot crossed flip to the {ev['dir']} side at {ev['spot']:,.0f} "
                    f"(flip {ev['flip']:,.0f}). Zone changed — re-check fade vs break bias. {lvl}")
        return json.dumps({k: v for k, v in ev.items() if k not in ("state", "ts")}, default=str)


INTERPRETER = WallInterpreter()   # server-wide singleton