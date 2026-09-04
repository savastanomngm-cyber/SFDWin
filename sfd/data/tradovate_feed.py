"""Tradovate real-time NQ/ES feed — CORRECTED for working endpoint."""
import os, json, time, threading, urllib.request, urllib.parse

DEMO = os.getenv("TRADOVATE_DEMO", "") not in ("", "0", "false")
BASE = "https://demo-api.tradovate.com" if DEMO else "https://api.tradovate.com"

_token, _token_ts = None, 0.0
_lock = threading.Lock()
_last = {}

def available():
    return bool(os.getenv("TRADOVATE_TOKEN") or
                (os.getenv("TRADOVATE_USER") and os.getenv("TRADOVATE_PASS")))

def get_token():
    global _token, _token_ts
    with _lock:
        if _token and time.time() - _token_ts < 3600:
            return _token
        # Pre-generated token takes precedence
        pre = os.getenv("TRADOVATE_TOKEN", "")
        if pre:
            _token, _token_ts = pre, time.time()
            return _token
        # 🟢 CORRECTED: form-encoded POST to /auth/accessTokenRequest
        body = urllib.parse.urlencode({
            "name": os.getenv("TRADOVATE_USER", ""),
            "password": os.getenv("TRADOVATE_PASS", ""),
            "appId": "sfd-terminal",
            "appVersion": "1.0",
            "cid": "sfd",
            "sec": "sfd",
        }).encode()
        req = urllib.request.Request(
            BASE + "/auth/accessTokenRequest",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
            tok = d.get("accessToken") or d.get("token") or ""
            if tok:
                _token, _token_ts = tok, time.time()
                print(f"[tradovate] ✅ authenticated as {os.getenv('TRADOVATE_USER')}")
            elif d.get("errorText"):
                print(f"[tradovate] ❌ auth rejected: {d.get('errorText')}")
            return tok
        except Exception as e:
            print(f"[tradovate] ❌ auth call failed: {e}")
            return None

def _front_symbol(root):
    codes = {3: "H", 6: "M", 9: "U", 12: "Z"}
    now = time.localtime()
    y, m = now.tm_year, now.tm_mon
    for yy in (y, y + 1):
        for mm, cc in codes.items():
            if (yy, mm) >= (y, m):
                return f"{root}{cc}{yy % 10}"
    return f"{root}Z{y % 10}"

def _quote(root):
    tok = get_token()
    if not tok: return None
    h = {"Authorization": f"Bearer {tok}"}
    sym = _front_symbol(root)
    try:
        req = urllib.request.Request(
            BASE + f"/md/getQuote?symbol={sym}",
            headers={"Content-Type": "application/json", **h})
        with urllib.request.urlopen(req, timeout=6) as r:
            q = json.loads(r.read())
        px = q.get("lastPrice") or q.get("consPrice") or q.get("bidPrice")
        if px: return float(px), sym
    except Exception: pass
    try:
        req = urllib.request.Request(
            BASE + f"/md/getChart?symbol={sym}&interval=1&intervalUnit=m&barCount=2",
            headers={"Content-Type": "application/json", **h})
        with urllib.request.urlopen(req, timeout=6) as r:
            c = json.loads(r.read())
        bars = c if isinstance(c, list) else c.get("bars", [])
        if bars: return float(bars[-1].get("c")), sym
    except Exception: pass
    return None

def _loop():
    while True:
        for root in ("NQ", "ES"):
            try:
                r = _quote(root)
                if r: _last[root] = {"price": r[0], "ts": time.time(), "sym": r[1]}
            except Exception: pass
        time.sleep(2.0)

_thread = None
def start():
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()

def last_quote(root="NQ"):
    return _last.get(root)

if available():
    start()