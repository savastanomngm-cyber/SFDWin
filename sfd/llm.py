"""
SFD Multi-Provider LLM Router (HARDENED + PROVENANCE).
Chains:
  DEEP: Nous (direct) -> Ox Alpha (Groq) -> Gemini -> Groq (Llama) -> AIHubMix -> Ollama
  FAST: Gemini -> Ox Alpha (Groq) -> Groq (Llama) -> AIHubMix -> Ollama

Hardening:
  • Quota errors (AIHubMix "recharged/10 times") skip to next provider
  • Connection errors (Ollama not running) skip to next provider
  • Rate limits retry same model, then skip
  • JSON mode unsupported retries without JSON mode
  • Returns safe fallback JSON if entire chain exhausted
  • Tracks which model/provider answered via LAST_CALL_META
"""
import os, time, json, re
from pathlib import Path

# ── load .env from project root (no external deps) ───────────
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

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ── Provenance tracker ───────────────────────────────────────
LAST_CALL_META = {"model": "unknown", "provider": "unknown", "elapsed": 0.0, "fallback": False}

# ── Provider Registry ─────────────────────────────────────────
PROVIDERS = {
    "nous":     {"base": "https://inference-api.nousresearch.com/v1", "key": "NOUS_API_KEY"},
    "groq":     {"base": "https://api.groq.com/openai/v1",           "key": "GROQ_API_KEY"},
    "gemini":   {"base": "https://generativelanguage.googleapis.com/v1beta/openai/", "key": "GEMINI_API_KEY"},
    "aihubmix": {"base": os.getenv("AIHUBMIX_BASE_URL", "https://aihubmix.com/v1"), "key": "AIHUBMIX_API_KEY"},
    "ollama":   {"base": "http://localhost:11434/v1",                 "key": "OLLAMA_API_KEY"},
}

# ── Model Chains (ordered by preference, matching your working setup) ──
DEEP_CHAIN = [
    ("nous",     "stealth/ox-alpha"),         # Nous direct (original working)
    ("groq",     "openai/gpt-oss-120b"),      # Ox Alpha via Groq
    ("gemini",   "gemini-2.5-flash"),
    ("groq",     "llama-3.3-70b-versatile"),
    ("aihubmix", "ox-alpha"),
    ("aihubmix", "hy3-free"),
    ("ollama",   "llama3.1:8b"),              # Local fallback
    ("ollama",   "qwen2.5:14b"),              # Local fallback
]

FAST_CHAIN = [
    ("gemini",   "gemini-2.5-flash-lite"),
    ("groq",     "openai/gpt-oss-20b"),       # Ox Alpha via Groq
    ("groq",     "llama-3.1-8b-instant"),
    ("aihubmix", "hy3-free"),
    ("aihubmix", "ox-alpha"),
    ("ollama",   "llama3.1:8b"),
]

MAX_TOKENS = {"deep": 4096, "fast": 1024}

_clients = {}
def get_client(provider):
    if provider not in PROVIDERS or not OpenAI:
        return None
    key = os.getenv(PROVIDERS[provider]["key"], "")
    # Ollama doesn't need a real key, but the OpenAI client requires a non-empty string
    if provider == "ollama" and not key:
        key = "ollama"
    if not key:
        return None
    if provider not in _clients:
        _clients[provider] = OpenAI(api_key=key, base_url=PROVIDERS[provider]["base"])
    return _clients[provider]

def providers_available():
    return [p for p in PROVIDERS if get_client(p)]

AI_ENABLED = bool(providers_available())

MAX_RETRIES, RETRY_DELAY = 2, 3

def llm(system, user, role="fast", temperature=0.5, force_json=False):
    global LAST_CALL_META
    
    if not AI_ENABLED:
        LAST_CALL_META = {"model": "none", "provider": "none", "elapsed": 0.0, "fallback": True}
        return _fallback_json(force_json)
    
    chain = DEEP_CHAIN if role == "deep" else FAST_CHAIN

    for provider, model in chain:
        client = get_client(provider)
        if not client:
            continue

        use_json = force_json
        for attempt in range(1, MAX_RETRIES + 1):
            t0 = time.time()
            try:
                kw = dict(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    temperature=temperature,
                    max_tokens=MAX_TOKENS.get(role, 1024),
                )
                if use_json:
                    kw["response_format"] = {"type": "json_object"}

                r = client.chat.completions.create(**kw)
                content = r.choices[0].message.content.strip()
                
                # 🟢 TRACK PROVENANCE ON SUCCESS
                LAST_CALL_META = {
                    "model": model, 
                    "provider": provider, 
                    "elapsed": round(time.time() - t0, 2), 
                    "fallback": False
                }
                return content

            except Exception as e:
                err = str(e)
                
                # AIHubMix free quota exhausted -> skip to next provider (don't break chain)
                if "recharged" in err or "10 times" in err or "abuse of free resources" in err:
                    break
                
                # Ollama not running -> skip to next provider
                if provider == "ollama" and ("Connection error" in err or "localhost:11434" in err):
                    break
                
                # JSON mode unsupported -> retry without it on same model
                if "response_format" in err or ("json" in err.lower() and "400" in err):
                    use_json = False
                    continue
                
                # Rate limit -> wait and retry same model
                if "429" in err or "rate" in err.lower():
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                
                # Model not found / bad model -> skip to next provider
                if "404" in err or "not_found" in err or "does not exist" in err:
                    break
                
                # Other errors -> skip to next provider
                break

    # 🔴 TRACK PROVENANCE ON TOTAL FAILURE
    LAST_CALL_META = {"model": "fallback_json", "provider": "none", "elapsed": 0.0, "fallback": True}
    return _fallback_json(force_json)

def _fallback_json(force_json):
    """If all providers fail, return a safe JSON so the dashboard doesn't crash."""
    if force_json:
        return json.dumps({
            "decision": "WAIT",
            "confidence": 0,
            "rationale": "All AI provider quotas exhausted or offline. Standing down safely."
        })
    return ""

def extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None