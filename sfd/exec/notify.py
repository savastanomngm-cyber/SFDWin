"""
SFD alert dispatch — Telegram + macOS local notification.
Secrets via .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Falls back to a macOS notification if Telegram isn't configured.
"""
import os, json, subprocess
import urllib.request
from pathlib import Path

def _load_dotenv():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

def _tg():
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")

def telegram_enabled():
    t, c = _tg()
    return bool(t and c)

def send_telegram(text):
    token, chat = _tg()
    if not (token and chat):
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False

def local_notify(title, text):
    try:
        script = f'display notification "{text[:200].replace(chr(34), chr(39))}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        return True
    except Exception:
        return False

def alert(title, text):
    """Push to every enabled external channel. Returns channels hit."""
    sent = []
    if telegram_enabled() and send_telegram(f"<b>{title}</b>\n{text}"):
        sent.append("telegram")
    if local_notify(title, text):
        sent.append("macos")
    return sent