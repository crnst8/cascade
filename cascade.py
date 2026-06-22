#!/usr/bin/env python3
"""
cascade.py

Reads .env for provider keys and auto-enables any of ~18 OpenAI-compatible
free providers (Groq, Cerebras, Google Gemini, Mistral, SambaNova, Nvidia NIM,
Cloudflare, OpenRouter, OVHcloud keyless, and more — sourced from the
free-ai-tools catalog). Discovers their live models, ranks them best -> worst,
and routes your chat to the best available one. When a model runs out of usage
(rate limit / quota) it automatically fails over to the next best model.
Dependency-free (Python 3 stdlib only).

Usage:
    python3 cascade.py            # interactive chat with auto-routing
    python3 cascade.py --list     # print the ranked leaderboard and exit
    python3 cascade.py --providers# show the provider catalog (connected + addable)
    python3 cascade.py --bench    # race top models for latency + tokens/sec
    python3 cascade.py -q "..."   # one-shot prompt, then exit

Daily usage counts and long cooldowns persist to ~/.cascade_state.json.
"""

import os
import re
import sys
import json
import time
import uuid
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ----------------------------------------------------------------------------
# Colours
# ----------------------------------------------------------------------------
_NO_COLOR = (not sys.stdout.isatty()) or os.environ.get("NO_COLOR")


def c(code):
    return "" if _NO_COLOR else code


RESET = c("\033[0m")
BOLD = c("\033[1m")
DIM = c("\033[2m")
ITALIC = c("\033[3m")


def fg(n):
    return c(f"\033[38;5;{n}m")


GREEN = fg(78)
RED = fg(203)
YELLOW = fg(221)
BLUE = fg(75)
GREY = fg(244)
WHITE = fg(255)
CYAN = fg(80)
PURPLE = fg(141)

# ----------------------------------------------------------------------------
# Provider catalog — every entry is an OpenAI-compatible endpoint. A provider
# auto-enables when its env key(s) are present in .env. Sourced from the
# free-ai-tools catalog (github.com/ShaikhWarsi/free-ai-tools).
#   pref      : provider speed/quality tiebreak added to a model's score
#   env       : required env var(s); the first is the bearer token unless 'token'
#   public    : model-list endpoint needs no auth
#   keyless   : chat works with no key at all (shared anonymous quota)
#   free_only : keep only ':free' model ids
#   rpd       : documented requests/day (for the daily budget bar; None if n/a)
# ----------------------------------------------------------------------------
CATALOG = [
    {"key": "cerebras", "label": "Cerebras", "color": 213, "pref": 7,
     "base": "https://api.cerebras.ai/v1", "env": ["CEREBRAS_KEY", "CEREBRAS_API_KEY"],
     "signup": "https://cloud.cerebras.ai/", "limits": "30 RPM · 1M tokens/day", "rpd": None},
    {"key": "groq", "label": "Groq", "color": 208, "pref": 6,
     "base": "https://api.groq.com/openai/v1", "env": ["GROQ_KEY", "GROQ_API_KEY"],
     "signup": "https://console.groq.com", "limits": "~1,000 req/day per model", "rpd": 1000},
    {"key": "sambanova", "label": "SambaNova", "color": 105, "pref": 5,
     "base": "https://api.sambanova.ai/v1", "env": ["SAMBANOVA_KEY", "SAMBANOVA_API_KEY"],
     "public": True, "signup": "https://cloud.sambanova.ai/", "limits": "$5 trial / 3 mo", "rpd": None},
    {"key": "gemini", "label": "Google Gemini", "color": 75, "pref": 4,
     "base": "https://generativelanguage.googleapis.com/v1beta/openai",
     "env": ["GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"],
     "signup": "https://aistudio.google.com", "limits": "250–1,500 req/day", "rpd": 1500},
    {"key": "cloudflare", "label": "Cloudflare", "color": 214, "pref": 4,
     "base": "https://api.cloudflare.com/client/v4/accounts/{CF_ACC_ID}/ai/v1",
     "models": "https://api.cloudflare.com/client/v4/accounts/{CF_ACC_ID}/ai/models/search?task=Text+Generation&per_page=100",
     "list_style": "cloudflare", "env": ["CF_ACC_ID", "CF_API_TOKEN"], "token": "CF_API_TOKEN",
     "signup": "https://developers.cloudflare.com/workers-ai", "limits": "10,000 neurons/day", "rpd": None},
    {"key": "nim", "label": "Nvidia NIM", "color": 118, "pref": 3,
     "base": "https://integrate.api.nvidia.com/v1", "env": ["NIM_KEY", "NVIDIA_API_KEY"],
     "signup": "https://build.nvidia.com", "limits": "40 RPM · 1K–5K credits", "rpd": None},
    {"key": "mistral", "label": "Mistral", "color": 209, "pref": 3,
     "base": "https://api.mistral.ai/v1", "env": ["MISTRAL_KEY", "MISTRAL_API_KEY"],
     "signup": "https://console.mistral.ai/", "limits": "1 req/s · 1B tok/month", "rpd": None},
    {"key": "scaleway", "label": "Scaleway", "color": 134, "pref": 2,
     "base": "https://api.scaleway.ai/v1", "env": ["SCALEWAY_KEY", "SCALEWAY_API_KEY"],
     "signup": "https://console.scaleway.com/generative-api/models", "limits": "1M tokens", "rpd": None},
    {"key": "nebius", "label": "Nebius", "color": 39, "pref": 2,
     "base": "https://api.studio.nebius.com/v1", "env": ["NEBIUS_KEY", "NEBIUS_API_KEY"],
     "signup": "https://tokenfactory.nebius.com/", "limits": "$1 trial (permanent)", "rpd": None},
    {"key": "hyperbolic", "label": "Hyperbolic", "color": 81, "pref": 2,
     "base": "https://api.hyperbolic.xyz/v1", "env": ["HYPERBOLIC_KEY", "HYPERBOLIC_API_KEY"],
     "signup": "https://app.hyperbolic.ai/", "limits": "$1 trial", "rpd": None},
    {"key": "deepinfra", "label": "DeepInfra", "color": 156, "pref": 2,
     "base": "https://api.deepinfra.com/v1/openai", "env": ["DEEPINFRA_KEY", "DEEPINFRA_API_KEY"],
     "signup": "https://deepinfra.com/login", "limits": "200 concurrent", "rpd": None},
    {"key": "fireworks", "label": "Fireworks", "color": 203, "pref": 2,
     "base": "https://api.fireworks.ai/inference/v1", "env": ["FIREWORKS_KEY", "FIREWORKS_API_KEY"],
     "signup": "https://fireworks.ai/", "limits": "$1 trial (permanent)", "rpd": None},
    {"key": "novita", "label": "Novita", "color": 170, "pref": 2,
     "base": "https://api.novita.ai/v3/openai", "env": ["NOVITA_KEY", "NOVITA_API_KEY"],
     "public": True, "signup": "https://novita.ai/", "limits": "$0.50 trial / 1 yr", "rpd": None},
    {"key": "siliconflow", "label": "SiliconFlow", "color": 45, "pref": 2,
     "base": "https://api.siliconflow.com/v1", "env": ["SILICONFLOW_KEY", "SILICONFLOW_API_KEY"],
     "signup": "https://cloud.siliconflow.cn/account/ak", "limits": "1K RPM · 50K TPM", "rpd": None},
    {"key": "zai", "label": "Z.AI (GLM)", "color": 117, "pref": 2,
     "base": "https://api.z.ai/api/paas/v4", "env": ["ZAI_KEY", "ZAI_API_KEY"],
     "signup": "https://z.ai", "limits": "free tier (generous)", "rpd": None},
    {"key": "openrouter", "label": "OpenRouter", "color": 111, "pref": 2,
     "base": "https://openrouter.ai/api/v1", "env": ["OR_KEY", "OPENROUTER_API_KEY"],
     "public": True, "free_only": True, "signup": "https://openrouter.ai",
     "limits": "20 RPM · 50–1,000 req/day", "rpd": 1000},
    {"key": "chutes", "label": "Chutes AI", "color": 220, "pref": 1,
     "base": "https://llm.chutes.ai/v1", "env": ["CHUTES_KEY", "CHUTES_API_KEY"],
     "signup": "https://chutes.ai", "limits": "community GPU", "rpd": None},
    {"key": "ovh", "label": "OVHcloud", "color": 27, "pref": -8,
     "base": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
     "env": ["OVH_AI_ENDPOINTS_ACCESS_TOKEN"], "public": True, "keyless": True,
     "signup": "https://endpoints.ai.cloud.ovh.net", "limits": "2 RPM keyless · 400 RPM w/ key", "rpd": None},
]
CAT_BY_KEY = {e["key"]: e for e in CATALOG}
PROVIDER_LABEL = {e["key"]: e["label"] for e in CATALOG}
PROVIDER_PREF = {e["key"]: e["pref"] for e in CATALOG}


def pcolor(p):
    e = CAT_BY_KEY.get(p)
    return fg(e["color"]) if e else WHITE


# ----------------------------------------------------------------------------
# .env loader
# ----------------------------------------------------------------------------
def load_env():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("CASCADE_ENV"),
        os.path.join(here, ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    env = {}
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
            return env, path
    return env, None


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
# Some providers (Groq, Cloudflare) sit behind bot protection that rejects the
# default "Python-urllib" User-Agent with a 403. Always present a normal UA.
USER_AGENT = "cascade/1.0 (+https://localhost)"


def _with_ua(headers):
    h = dict(headers or {})
    h.setdefault("User-Agent", USER_AGENT)
    return h


def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=_with_ua(headers))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), dict(r.headers)


def parse_duration(s):
    """Parse '1m26.4s', '205ms', '13.5s', or a plain number of seconds."""
    if s is None:
        return None
    s = str(s).strip()
    try:
        return float(s)  # plain seconds
    except ValueError:
        pass
    total = 0.0
    matched = False
    for val, unit in re.findall(r"([\d.]+)\s*(ms|s|m|h)", s):
        matched = True
        v = float(val)
        total += {"ms": v / 1000, "s": v, "m": v * 60, "h": v * 3600}[unit]
    return total if matched else None


# ----------------------------------------------------------------------------
# Persistent state: daily request counts + cooldowns survive restarts.
# ----------------------------------------------------------------------------
STATE_PATH = os.path.join(os.path.expanduser("~"), ".cascade_state.json")
STATE = {"date": "", "counts": {}, "cooldowns": {}}
# Serialises state-file writes + cooldown mutations across server worker threads.
STATE_LOCK = threading.RLock()


def _today():
    return time.strftime("%Y-%m-%d")


def load_state():
    global STATE
    try:
        with open(STATE_PATH) as f:
            STATE = json.load(f)
    except Exception:
        STATE = {"date": _today(), "counts": {}, "cooldowns": {}}
    if STATE.get("date") != _today():          # new day → reset daily counters
        STATE = {"date": _today(), "counts": {}, "cooldowns": STATE.get("cooldowns", {})}
    STATE.setdefault("counts", {})
    STATE.setdefault("cooldowns", {})
    # drop expired persisted cooldowns
    now = time.time()
    STATE["cooldowns"] = {k: v for k, v in STATE["cooldowns"].items() if v.get("until", 0) > now}
    save_state()


def save_state():
    with STATE_LOCK:
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(STATE, f)
        except Exception:
            pass


def state_bump(prov_key):
    with STATE_LOCK:
        STATE["counts"][prov_key] = STATE["counts"].get(prov_key, 0) + 1
    save_state()


def state_used(prov_key):
    return STATE["counts"].get(prov_key, 0)


# ----------------------------------------------------------------------------
# Provider configuration
# ----------------------------------------------------------------------------
def _resolve_key(entry, env):
    """Return the first present value among an entry's env vars, else None."""
    for var in entry["env"]:
        if env.get(var):
            return env[var]
    return None


def _fmt_url(template, env):
    """Fill {CF_ACC_ID}-style placeholders from env."""
    out = template
    for m in re.findall(r"\{(\w+)\}", template):
        out = out.replace("{" + m + "}", env.get(m, ""))
    return out


class Provider:
    def __init__(self, entry, env):
        self.key = entry["key"]
        self.entry = entry
        self.base = _fmt_url(entry["base"], env)
        models = entry.get("models") or (entry["base"] + "/models")
        self.models_url = _fmt_url(models, env)
        # token: explicit 'token' var, else first env var, else keyless
        token_var = entry.get("token", entry["env"][0])
        self.api_key = env.get(token_var) or _resolve_key(entry, env) or ""
        self.list_style = entry.get("list_style", "openai")
        self.public_list = entry.get("public", False)
        self.keyless = entry.get("keyless", False)
        self.free_only = entry.get("free_only", False)
        self.auth = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            self.auth["Authorization"] = f"Bearer {self.api_key}"
        self.models_headers = {} if self.public_list else dict(self.auth)
        self.disabled = False
        self.note = ""

    @property
    def connected(self):
        return bool(self.api_key) or self.keyless


def build_providers(env):
    """Enable every catalog provider whose key(s) are present (or that is keyless)."""
    provs = {}
    for entry in CATALOG:
        key = _resolve_key(entry, env)
        # Cloudflare needs all its env vars; others need the token (or keyless).
        have_all = all(env.get(v) for v in entry["env"])
        if entry.get("keyless") or key or (entry.get("token") and have_all):
            provs[entry["key"]] = Provider(entry, env)
    return provs


# ----------------------------------------------------------------------------
# Model discovery + filtering
# ----------------------------------------------------------------------------
# substrings that mean "not a general chat model"
_EXCLUDE = re.compile(
    r"whisper|tts|orpheus|prompt-guard|llama-guard|guard-|safeguard|content-safety|"
    r"\bembed|bge-|nv-embed|reranker|rerank|retriev|nemoretriever|reward|"
    r"deplot|fuyu|ocr|paddle|florence|clip|vila|diffusion|sdxl|stable-diffusion|"
    r"parakeet|riva|canary|-lora|codellama|starcoder|granite-.*code",
    re.IGNORECASE,
)


def discover_models(prov):
    try:
        data, _ = http_json(prov.models_url, prov.models_headers, timeout=18)
    except Exception as e:
        prov.note = f"list failed: {type(e).__name__}"
        return []
    ids = []
    if prov.list_style == "cloudflare":
        for m in data.get("result", []):
            ids.append(m.get("name"))
    else:
        for m in data.get("data", []):
            ids.append(m.get("id"))
    out = []
    for mid in ids:
        if not mid or _EXCLUDE.search(mid):
            continue
        # Some gateways (OpenRouter) mix paid + free; keep only ':free'.
        if prov.free_only and not mid.endswith(":free"):
            continue
        out.append(mid)
    return out


# ----------------------------------------------------------------------------
# Ranking heuristic: best -> worst
# ----------------------------------------------------------------------------
_FAMILY = [
    ("deepseek-v4", 96), ("deepseek-v3", 90), ("deepseek-r1", 88), ("deepseek", 70),
    ("nemotron-3-ultra", 93), ("nemotron-3-super", 86), ("nemotron", 72),
    ("llama-4", 82), ("llama-3.3", 78), ("llama3.3", 78), ("llama-3.1", 66), ("llama-3.2", 56), ("llama", 58),
    ("glm-5", 88), ("glm-4", 78), ("glm", 70),
    ("kimi", 84),
    ("gpt-oss-120b", 86), ("gpt-oss", 74),
    ("qwen3", 78), ("qwq", 76), ("qwen2.5", 70), ("qwen", 62),
    ("gemma-4", 70), ("gemma-3", 60), ("gemma-2", 50), ("gemma", 46),
    ("mistral-small", 62), ("mixtral", 58), ("mistral", 54),
    ("command", 64), ("cohere", 60), ("north", 56),
    ("hermes", 60), ("dolphin", 50), ("nex-", 62), ("laguna", 58), ("poolside", 58),
    ("seed-oss", 60), ("dbrx", 58), ("jamba", 58), ("yi-large", 60),
    ("granite", 46), ("phi", 46), ("sea-lion", 38), ("allam", 30),
    ("liquid", 40), ("lfm", 40), ("compound", 66),
]
def _size_b(mid):
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)b\b", mid.lower())]
    return max(nums) if nums else 0.0


def _size_weight(b):
    # Non-monotonic on purpose: 70–200B is the free-tier sweet spot. Models
    # >=300B are penalised — on free tiers they are the slowest / most
    # rate-limited, so they should not auto-rank #1 (use /bench + /fastest).
    table = [(300, 24), (130, 36), (90, 34), (60, 30), (40, 24),
             (28, 19), (20, 15), (12, 11), (9, 8), (7, 6), (3, 3)]
    for thresh, w in table:
        if b >= thresh:
            return w
    return 1


def _family_weight(mid):
    low = mid.lower()
    best = 0
    for sub, w in _FAMILY:
        if sub in low and w > best:
            best = w
    return best


def score_model(prov_key, mid):
    return _family_weight(mid) + _size_weight(_size_b(mid)) + PROVIDER_PREF.get(prov_key, 0)


class Candidate:
    def __init__(self, prov, mid):
        self.prov = prov
        self.mid = mid
        self.score = score_model(prov.key, mid)
        self.size = _size_b(mid)
        self.status = "ok"            # ok | cooldown | down
        self.cooldown_until = 0.0
        self.reason = ""
        self.calls = 0
        self.rl = {}                  # last seen rate-limit snapshot
        self.tps = None               # measured tokens/sec (from /bench)
        self.ttft = None              # measured first-token latency (s)
        self.last_usage = None        # provider-reported token usage (non-stream)

    @property
    def cid(self):
        return f"{self.prov.key}|{self.mid}"

    @property
    def available(self):
        if self.status == "down":
            return False
        if self.status == "cooldown" and time.time() < self.cooldown_until:
            return False
        return True

    def short(self):
        return f"{PROVIDER_LABEL[self.prov.key]}:{self.mid.split('/')[-1]}"


def build_leaderboard(providers):
    cands = []
    with ThreadPoolExecutor(max_workers=len(providers) or 1) as ex:
        futs = {ex.submit(discover_models, p): p for p in providers.values()}
        for fut in futs:
            p = futs[fut]
            for mid in fut.result():
                cands.append(Candidate(p, mid))
    cands.sort(key=lambda x: (-x.score, -x.size, x.mid))
    apply_persisted_cooldowns(cands)
    return cands


def apply_persisted_cooldowns(cands):
    """Re-apply long cooldowns (e.g. daily quota) saved from a previous run."""
    now = time.time()
    for cand in cands:
        cd = STATE["cooldowns"].get(cand.cid)
        if cd and cd.get("until", 0) > now:
            cand.status = "cooldown"
            cand.cooldown_until = cd["until"]
            cand.reason = cd.get("reason", "cooldown") + " (persisted)"


# ----------------------------------------------------------------------------
# OpenRouter usage probe
# ----------------------------------------------------------------------------
def openrouter_usage(prov):
    try:
        data, _ = http_json(
            "https://openrouter.ai/api/v1/auth/key",
            {"Authorization": f"Bearer {prov.api_key}"}, timeout=12)
        return data.get("data", {})
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Chat call with streaming + failover classification
# ----------------------------------------------------------------------------
def _record_rate_headers(cand, headers):
    h = {k.lower(): v for k, v in headers.items()}
    snap = {}
    if "x-ratelimit-remaining-requests" in h:
        snap["req"] = h.get("x-ratelimit-remaining-requests")
        snap["req_limit"] = h.get("x-ratelimit-limit-requests")
    if "x-ratelimit-remaining-tokens" in h:
        snap["tok"] = h.get("x-ratelimit-remaining-tokens")
        snap["tok_limit"] = h.get("x-ratelimit-limit-tokens")
    if "x-ratelimit-reset-requests" in h:
        snap["reset"] = h.get("x-ratelimit-reset-requests")
    if snap:
        snap["at"] = time.time()
        cand.rl = snap


# A "sink" receives the model's tokens as they arrive. call_model is agnostic to
# where they go: the REPL streams them to the terminal (StdoutSink), the REST API
# forwards them as Server-Sent Events (SSESink), and non-streaming callers use the
# silent base Sink and read the returned text. This keeps one failover/parse path.
class Sink:
    sent = False           # set True once any content token has been emitted
    def reasoning(self, t): pass
    def content(self, t): pass
    def close(self): pass


class StdoutSink(Sink):
    """Terminal sink: content printed plainly, reasoning dimmed + italic."""
    def __init__(self):
        self._r_open = False
    def reasoning(self, t):
        if not self._r_open:
            sys.stdout.write(DIM + ITALIC)
            self._r_open = True
        sys.stdout.write(t)
        sys.stdout.flush()
    def content(self, t):
        if self._r_open:
            sys.stdout.write(RESET)
            self._r_open = False
        self.sent = True
        sys.stdout.write(t)
        sys.stdout.flush()
    def close(self):
        if self._r_open:
            sys.stdout.write(RESET)
            self._r_open = False


def call_model(cand, messages, stream=True, max_tokens=2048, temperature=0.7, sink=None):
    """Run one chat completion against `cand`, emitting tokens to `sink`.

    Returns (ok, text, err) where err is None or {'type','msg','retry'}. `sink`
    defaults to the terminal; pass a custom Sink to capture or forward tokens.
    """
    if sink is None:
        sink = StdoutSink()
    prov = cand.prov
    url = prov.base + "/chat/completions"
    payload = {
        "model": cand.mid,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=prov.auth, method="POST")
    cand.calls += 1
    try:
        resp = urllib.request.urlopen(req, timeout=75)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return _classify_http_error(cand, e.code, dict(e.headers), body)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return False, "", {"type": "network", "msg": f"{type(e).__name__}", "retry": 15}
    except Exception as e:
        return False, "", {"type": "network", "msg": f"{type(e).__name__}", "retry": 15}

    _record_rate_headers(cand, dict(resp.headers))
    ctype = resp.headers.get("Content-Type", "")

    # Non-streaming JSON response
    if "text/event-stream" not in ctype:
        try:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            return False, "", {"type": "badmodel", "msg": f"parse: {e}", "retry": 0}
        if obj.get("error"):
            return False, "", {"type": "badmodel", "msg": str(obj["error"])[:160], "retry": 0}
        try:
            txt = obj["choices"][0]["message"]["content"] or ""
        except Exception:
            return False, "", {"type": "badmodel", "msg": "no content", "retry": 0}
        cand.last_usage = obj.get("usage")
        sink.content(txt)
        sink.close()
        state_bump(prov.key)
        return True, txt, None

    # Streaming SSE
    full = []
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if obj.get("usage"):
                cand.last_usage = obj["usage"]
            delta = (obj.get("choices") or [{}])[0].get("delta", {}) or {}
            rcon = delta.get("reasoning_content") or delta.get("reasoning")
            if rcon:
                sink.reasoning(rcon)
            con = delta.get("content")
            if con:
                sink.content(con)
                full.append(con)
    except Exception as e:
        sink.close()
        return False, "".join(full), {"type": "network", "msg": f"stream: {e}", "retry": 10}
    sink.close()
    state_bump(prov.key)
    return True, "".join(full), None


def _classify_http_error(cand, code, headers, body):
    msg = body[:200].replace("\n", " ") if body else f"HTTP {code}"
    retry = parse_duration(headers.get("Retry-After")) or \
        parse_duration(headers.get("retry-after"))
    rl_reset = parse_duration(headers.get("x-ratelimit-reset-requests") or
                              headers.get("X-RateLimit-Reset-Requests"))
    low = body.lower()
    if code == 429 or "rate limit" in low or "rate_limit" in low:
        return False, "", {"type": "ratelimit", "msg": msg,
                           "retry": retry or rl_reset or 60}
    if code in (402,) or "quota" in low or "credit" in low or "insufficient" in low or \
            "exceeded" in low or "out of" in low:
        return False, "", {"type": "quota", "msg": msg, "retry": retry or 3600}
    if code in (401, 403):
        return False, "", {"type": "auth", "msg": msg, "retry": 0}
    if code in (400, 404, 422):
        return False, "", {"type": "badmodel", "msg": msg, "retry": 0}
    if 500 <= code < 600:
        return False, "", {"type": "server", "msg": msg, "retry": retry or 20}
    return False, "", {"type": "server", "msg": msg, "retry": retry or 30}


def apply_failure(cand, err):
    t = err["type"]
    if t == "auth":
        cand.prov.disabled = True
        cand.status = "down"
        cand.reason = "auth failed (provider disabled)"
    elif t == "badmodel":
        cand.status = "down"
        cand.reason = "unsupported / rejected"
    elif t in ("ratelimit", "quota", "server", "network"):
        cand.status = "cooldown"
        cand.cooldown_until = time.time() + float(err.get("retry") or 30)
        cand.reason = {
            "ratelimit": "rate limited",
            "quota": "quota exhausted",
            "server": "server error",
            "network": "network error",
        }[t]
        # Persist long cooldowns (quota/rate-limit) so they survive a restart.
        if t in ("ratelimit", "quota") and cand.cooldown_until - time.time() > 120:
            with STATE_LOCK:
                STATE["cooldowns"][cand.cid] = {"until": cand.cooldown_until, "reason": cand.reason}
            save_state()


# ----------------------------------------------------------------------------
# UI rendering
# ----------------------------------------------------------------------------
def banner(env_path, providers, n_models):
    line = "═" * 64
    print(f"{PURPLE}{line}{RESET}")
    print(f"{BOLD}{WHITE}  ⚡ CASCADE {RESET}{GREY}— free-tier auto-failover router{RESET}")
    print(f"{PURPLE}{line}{RESET}")
    src = env_path or "(no .env found)"
    print(f"  {GREY}env:{RESET} {src}")
    parts = []
    for p in providers.values():
        col = pcolor(p.key)
        if p.disabled:
            dot = f"{RED}✗{RESET}"
        elif p.keyless and not p.api_key:
            dot = f"{CYAN}◌{RESET}"
        else:
            dot = f"{GREEN}●{RESET}"
        parts.append(f"{col}{PROVIDER_LABEL[p.key]}{RESET}{dot}")
    print(f"  {GREY}connected ({len(providers)}/{len(CATALOG)}):{RESET} " + "  ".join(parts))
    print(f"  {GREY}models discovered:{RESET} {BOLD}{n_models}{RESET}   "
          f"{GREY}/providers to add more · /bench to race speed{RESET}")
    print(f"{PURPLE}{line}{RESET}")


def status_tag(cand):
    if cand.status == "down":
        return f"{RED}✗ down{RESET}"
    if cand.status == "cooldown" and time.time() < cand.cooldown_until:
        rem = int(cand.cooldown_until - time.time())
        return f"{YELLOW}⏳ {rem}s{RESET}"
    return f"{GREEN}✓ ready{RESET}"


def print_leaderboard(cands, limit=30, highlight=None):
    print(f"\n{BOLD}{WHITE}  RANK  PROVIDER     SCORE  STATUS     MODEL{RESET}")
    print(f"  {GREY}{'─'*70}{RESET}")
    shown = 0
    for i, cand in enumerate(cands):
        if shown >= limit:
            break
        shown += 1
        col = pcolor(cand.prov.key)
        mark = f"{BOLD}{CYAN}▶{RESET}" if cand is highlight else " "
        prov = f"{col}{PROVIDER_LABEL[cand.prov.key]:<13}{RESET}"
        extra = ""
        if cand.tps:
            extra = f"   {CYAN}⚡{cand.tps:.0f} tok/s{RESET}"
        elif cand.rl.get("req"):
            extra = f"   {GREY}reqs left:{cand.rl['req']}{RESET}"
        print(f"  {mark}{i:>3}  {prov}  {cand.score:>4}  {status_tag(cand):<18}  "
              f"{WHITE}{cand.mid}{RESET}{extra}")
    if len(cands) > limit:
        print(f"  {GREY}… +{len(cands)-limit} more (failover chain includes all){RESET}")
    print()


def _bar(used, total, width=18):
    if not total:
        return f"{GREY}{'┄'*width}{RESET}"
    frac = min(1.0, used / total)
    fill = int(round(frac * width))
    col = GREEN if frac < 0.6 else (YELLOW if frac < 0.9 else RED)
    return f"{col}{'█'*fill}{GREY}{'░'*(width-fill)}{RESET}"


def print_usage(providers, cands):
    print(f"\n{BOLD}{WHITE}  USAGE / LIMITS  {RESET}{GREY}(session date {STATE.get('date')}){RESET}")
    print(f"  {GREY}{'─'*72}{RESET}")
    for key, p in providers.items():
        entry = CAT_BY_KEY[key]
        col = pcolor(key)
        used = state_used(key)
        rpd = entry.get("rpd")
        bar = _bar(used, rpd) if rpd else f"{GREY}{'┄'*18}{RESET}"
        cap = f"{used}/{rpd} req" if rpd else f"{used} req today"
        print(f"  {col}{PROVIDER_LABEL[key]:<13}{RESET} {bar} {cap:<16}"
              f"{GREY}{entry.get('limits','')}{RESET}")
    # Live signals where available
    if "openrouter" in providers:
        u = openrouter_usage(providers["openrouter"])
        if u is not None:
            print(f"  {pcolor('openrouter')}↳ OpenRouter live:{RESET} "
                  f"tier={'free' if u.get('is_free_tier') else 'paid'}  "
                  f"spend today=${u.get('usage_daily',0)}")
    snaps = [x for x in cands if x.prov.key == "groq" and x.rl]
    if snaps:
        x = snaps[0]
        print(f"  {pcolor('groq')}↳ Groq live:{RESET} {x.mid} "
              f"reqs {x.rl.get('req','?')}/{x.rl.get('req_limit','?')}  "
              f"tokens {x.rl.get('tok','?')}/{x.rl.get('tok_limit','?')}")
    cools = [x for x in cands if x.status == "cooldown" and time.time() < x.cooldown_until]
    if cools:
        print(f"  {YELLOW}on cooldown:{RESET} " +
              ", ".join(f"{x.short()}({int(x.cooldown_until-time.time())}s)" for x in cools[:6]))
    print()


def print_providers(env, providers, cands):
    """Catalog view: connected providers + the ones you can still unlock."""
    print(f"\n{BOLD}{WHITE}  PROVIDER CATALOG  {RESET}{GREY}(from free-ai-tools){RESET}")
    print(f"  {GREY}{'─'*74}{RESET}")
    n_on = sum(1 for p in providers.values() if p.connected)
    print(f"  {GREEN}● connected: {n_on}{RESET}   {GREY}○ available to add: "
          f"{len(CATALOG)-len(providers)}{RESET}\n")
    for entry in CATALOG:
        key = entry["key"]
        col = pcolor(key)
        if key in providers:
            p = providers[key]
            tag = f"{GREEN}● keyless{RESET}" if (p.keyless and not p.api_key) else f"{GREEN}● connected{RESET}"
            n = sum(1 for x in cands if x.prov.key == key)
            print(f"  {col}{entry['label']:<14}{RESET} {tag:<22} {GREY}{n} models · "
                  f"{entry.get('limits','')}{RESET}")
        else:
            envvar = entry["env"][0]
            print(f"  {col}{entry['label']:<14}{RESET} {GREY}○ add {RESET}{YELLOW}{envvar}{RESET}"
                  f"{GREY} → {entry['signup']}{RESET}")
    print(f"\n  {GREY}Add a key to .env and run {CYAN}/refresh{GREY} to light it up.{RESET}\n")


# ----------------------------------------------------------------------------
# /bench — empirical speed race: measure first-token latency + tokens/sec
# ----------------------------------------------------------------------------
def bench_one(cand, prompt="Reply with a single short sentence about the sea."):
    prov = cand.prov
    url = prov.base + "/chat/completions"
    payload = {"model": cand.mid, "stream": True, "max_tokens": 64,
               "messages": [{"role": "user", "content": prompt}]}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=prov.auth, method="POST")
    t0 = time.time()
    ttft = None
    n_tok = 0
    try:
        resp = urllib.request.urlopen(req, timeout=40)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                delta = (json.loads(chunk).get("choices") or [{}])[0].get("delta", {})
            except Exception:
                continue
            if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                if ttft is None:
                    ttft = time.time() - t0
                n_tok += 1
    except Exception as e:
        return cand, None, None, type(e).__name__
    dur = time.time() - t0
    tps = (n_tok / dur) if dur > 0 and n_tok else 0
    cand.tps, cand.ttft = tps, ttft
    return cand, ttft, tps, None


def run_bench(cands, top=8):
    pool = [x for x in cands if x.available and not x.prov.disabled][:top]
    if not pool:
        print(f"{RED}No available models to benchmark.{RESET}")
        return
    print(f"\n{BOLD}{WHITE}  BENCHMARK{RESET} {GREY}racing top {len(pool)} available models…{RESET}")
    results = []
    with ThreadPoolExecutor(max_workers=len(pool)) as ex:
        for cand, ttft, tps, err in ex.map(bench_one, pool):
            results.append((cand, ttft, tps, err))
    results.sort(key=lambda r: (r[3] is not None, -(r[2] or 0)))  # ok first, fastest tok/s
    print(f"  {GREY}{'─'*70}{RESET}")
    print(f"  {BOLD}{'PROVIDER':<14}{'TTFT':>8}{'TOK/S':>9}   MODEL{RESET}")
    for cand, ttft, tps, err in results:
        col = pcolor(cand.prov.key)
        if err:
            print(f"  {col}{PROVIDER_LABEL[cand.prov.key]:<14}{RESET}{RED}{'  —':>8}{'  fail':>9}{RESET}"
                  f"   {GREY}{cand.mid} ({err}){RESET}")
        else:
            ttft_s = f"{ttft:.2f}s" if ttft else "—"
            print(f"  {col}{PROVIDER_LABEL[cand.prov.key]:<14}{RESET}{ttft_s:>8}"
                  f"{CYAN}{tps:>8.0f}{RESET}   {WHITE}{cand.mid}{RESET}")
    ok = [r for r in results if r[3] is None and r[2]]
    if ok:
        fastest = ok[0][0]
        print(f"\n  {GREEN}fastest:{RESET} {pcolor(fastest.prov.key)}{fastest.short()}{RESET} "
              f"{CYAN}{ok[0][2]:.0f} tok/s{RESET}  "
              f"{GREY}— use {CYAN}/fastest{GREY} to prioritise measured speed{RESET}")
    print()
    return results


HELP = f"""
{BOLD}Commands{RESET}
  {CYAN}/models{RESET}        ranked leaderboard (best → worst) with live status
  {CYAN}/providers{RESET}     catalog: connected providers + ones you can unlock
  {CYAN}/usage{RESET}         daily budget bars + live rate-limit snapshots
  {CYAN}/bench{RESET}         race top models, measure real latency + tokens/sec
  {CYAN}/fastest{RESET}       re-rank by measured speed (run /bench first)
  {CYAN}/quality{RESET}       restore the quality (best → worst) ranking
  {CYAN}/use <n>{RESET}       pin to leaderboard index n (disable auto-routing)
  {CYAN}/auto{RESET}          resume automatic best-available routing
  {CYAN}/system <txt>{RESET}  set a system prompt
  {CYAN}/clear{RESET}         clear conversation history
  {CYAN}/refresh{RESET}       re-discover models & reset cooldowns
  {CYAN}/help{RESET}          this help
  {CYAN}/quit{RESET}          exit
Anything else is sent to the best available model. On rate-limit/quota it
auto-fails over to the next model and retries your message.
"""


# ----------------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------------
def next_available(cands, pinned):
    if pinned is not None:
        return [pinned] if pinned.available else []
    return [x for x in cands if x.available and not x.prov.disabled]


def send(cands, pinned, messages):
    chain = next_available(cands, pinned)
    if not chain:
        soon = sorted((x for x in cands if x.status == "cooldown"),
                      key=lambda x: x.cooldown_until)
        if soon:
            wait = int(soon[0].cooldown_until - time.time())
            print(f"{YELLOW}All models on cooldown. Next ready in ~{max(wait,0)}s "
                  f"({soon[0].short()}).{RESET}")
        else:
            print(f"{RED}No available models.{RESET}")
        return None
    for cand in chain:
        col = pcolor(cand.prov.key)
        print(f"{GREY}┌─ routing →{RESET} {col}{BOLD}{cand.short()}{RESET} "
              f"{GREY}(score {cand.score}){RESET}")
        print(f"{col}│{RESET} ", end="")
        ok, text, err = call_model(cand, messages)
        print()
        if ok:
            print(f"{GREY}└─ {GREEN}✓ delivered by {cand.short()}{RESET}\n")
            return text
        apply_failure(cand, err)
        tag = err["type"]
        print(f"{GREY}└─ {YELLOW}⚠ {cand.short()} failed: {tag} — {err['msg'][:80]}{RESET}")
        print(f"{GREY}   failing over…{RESET}\n")
    print(f"{RED}Exhausted all candidates for this turn.{RESET}")
    return None


# ----------------------------------------------------------------------------
# Server mode — OpenAI-compatible REST API over the same routing + failover.
#
# Point any OpenAI client at http://host:port/v1 and it just works:
#   • POST /v1/chat/completions  — auto-routed chat (stream or not), with failover
#   • GET  /v1/models            — discovered models (+ the 'auto' meta-model)
#   • GET  /v1/providers         — provider status / limits / signup links
#   • POST /v1/refresh           — re-discover models, clear transient cooldowns
#   • GET  /health               — liveness + summary
# Set CASCADE_API_KEY to require a bearer token; otherwise any key is accepted.
# ----------------------------------------------------------------------------
def api_id(cand):
    """Canonical, unique model id exposed over the API: '<provider>/<model>'."""
    return f"{cand.prov.key}/{cand.mid}"


_AUTO_ALIASES = {"", "auto", "cascade", "best", "default", "router"}


def _model_matches(cand, m):
    short = cand.mid.split("/")[-1]
    return m in (api_id(cand), f"{cand.prov.key}:{cand.mid}", cand.mid,
                 short, f"{cand.prov.key}/{short}")


def select_chain(cands, model):
    """Ordered failover chain for a requested model id ('' / 'auto' => all)."""
    avail = [c for c in cands if c.available and not c.prov.disabled]
    if (model or "").strip().lower() in _AUTO_ALIASES:
        return avail
    m = model.strip()
    return [c for c in avail if _model_matches(c, m)]


def model_known(cands, model):
    m = (model or "").strip()
    return any(_model_matches(c, m) for c in cands)


def status_word(c):
    if c.status == "down":
        return "down"
    if c.status == "cooldown" and time.time() < c.cooldown_until:
        return "cooldown"
    return "ready"


def route_api(chain, messages, sink, stream, max_tokens, temperature):
    """Non-printing sibling of send(): try each candidate, fail over on errors.

    Returns (cand, text, attempts, err). The big failover triggers (429, quota,
    auth, bad-model, 5xx) all surface before any token is streamed, so streaming
    failover is clean; only a rare mid-stream drop (sink.sent already True) ends
    the response on the current model instead of switching.
    """
    attempts = []
    for cand in chain:
        if stream:
            sink.model = api_id(cand)
        ok, text, err = call_model(cand, messages, stream=stream,
                                   max_tokens=max_tokens, temperature=temperature,
                                   sink=sink)
        if ok:
            return cand, text, attempts, None
        attempts.append({"model": api_id(cand), "error": err["type"],
                         "detail": err["msg"][:160]})
        if stream and sink.sent:          # bytes already on the wire — can't switch
            return cand, text, attempts, err
        apply_failure(cand, err)
    return None, "", attempts, {"type": "exhausted",
                                "msg": "all candidate models failed"}


class SSESink(Sink):
    """Forwards content tokens to the client as OpenAI chat.completion.chunk SSE."""
    def __init__(self, wfile, cid, created, model="cascade"):
        self.wfile = wfile
        self.cid = cid
        self.created = created
        self.model = model
        self.sent = False
        self._role_sent = False
        self.broken = False

    def _emit(self, payload):
        if self.broken:
            return
        try:
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            self.broken = True

    def _chunk(self, delta, finish=None):
        return {"id": self.cid, "object": "chat.completion.chunk",
                "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def _role(self):
        if not self._role_sent:
            self._emit(self._chunk({"role": "assistant"}))
            self._role_sent = True

    def content(self, t):
        self._role()
        self.sent = True
        self._emit(self._chunk({"content": t}))

    def finish(self):
        self._role()
        self._emit(self._chunk({}, finish="stop"))
        if not self.broken:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                self.broken = True

    def send_error_event(self, err):
        self._emit({"error": {"message": (err or {}).get("msg", "all models failed"),
                              "type": "upstream_error",
                              "code": (err or {}).get("type", "exhausted")}})


def approx_usage(messages, text):
    """Rough token estimate (~4 chars/token) when the provider reports none."""
    pt = sum(len(str(m.get("content", ""))) for m in messages) // 4
    ct = len(text) // 4
    return {"prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": pt + ct, "estimated": True}


def _err_obj(message, etype="invalid_request_error", code=None, **extra):
    e = {"message": message, "type": etype}
    if code is not None:
        e["code"] = code
    e.update(extra)
    return {"error": e}


def _models_payload(cands):
    now = int(time.time())
    data = [{"id": "auto", "object": "model", "created": now, "owned_by": "cascade",
             "description": "auto-routed best available model with failover"}]
    seen = set()
    for c in cands:
        i = api_id(c)
        if i in seen:
            continue
        seen.add(i)
        data.append({"id": i, "object": "model", "created": now,
                     "owned_by": c.prov.key,
                     "cascade": {"provider": PROVIDER_LABEL[c.prov.key],
                                 "model": c.mid, "score": c.score,
                                 "status": status_word(c)}})
    return {"object": "list", "data": data}


def _providers_payload(providers, cands):
    counts = {}
    for c in cands:
        counts[c.prov.key] = counts.get(c.prov.key, 0) + 1
    out = []
    for entry in CATALOG:
        k = entry["key"]
        p = providers.get(k)
        out.append({"key": k, "name": entry["label"], "enabled": bool(p),
                    "disabled": bool(p and p.disabled),
                    "keyless": entry.get("keyless", False),
                    "models": counts.get(k, 0), "env": entry["env"],
                    "limit": entry.get("limits"), "signup": entry.get("signup"),
                    "requests_today": state_used(k)})
    return {"object": "list", "data": out}


def _root_payload(env_path, providers, cands):
    return {"name": "cascade", "version": "1.0",
            "description": "unified OpenAI-compatible API over free-tier providers "
                           "with automatic failover",
            "env": env_path or None,
            "providers_enabled": len(providers), "providers_total": len(CATALOG),
            "models": len(cands),
            "endpoints": {"chat": "POST /v1/chat/completions",
                          "models": "GET /v1/models",
                          "providers": "GET /v1/providers",
                          "refresh": "POST /v1/refresh",
                          "health": "GET /health"}}


class CascadeHandler(BaseHTTPRequestHandler):
    server_version = "cascade/1.0"
    protocol_version = "HTTP/1.1"

    # --- plumbing -----------------------------------------------------------
    def _st(self):
        return self.server.cascade_state

    def log_message(self, fmt, *args):
        if os.environ.get("CASCADE_QUIET"):
            return
        sys.stderr.write(f"{GREY}  {self.address_string()} {fmt % args}{RESET}\n")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _authorized(self):
        required = os.environ.get("CASCADE_API_KEY")
        if not required:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token == required:
            return True
        self._send_json(401, _err_obj("invalid or missing API key",
                                      "authentication_error", code=401))
        return False

    # --- routes -------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        st = self._st()
        if path in ("/", "/health", "/v1"):
            return self._send_json(200, _root_payload(st["env_path"], st["providers"],
                                                      st["cands"]))
        if not self._authorized():
            return
        if path in ("/v1/models", "/models"):
            self._send_json(200, _models_payload(st["cands"]))
        elif path in ("/v1/providers", "/providers"):
            self._send_json(200, _providers_payload(st["providers"], st["cands"]))
        else:
            self._send_json(404, _err_obj(f"unknown route {path}", "not_found", code=404))

    def do_POST(self):
        if not self._authorized():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/v1/chat/completions", "/chat/completions"):
            self._chat()
        elif path in ("/v1/refresh", "/refresh"):
            self._refresh()
        else:
            self._send_json(404, _err_obj(f"unknown route {path}", "not_found", code=404))

    def _refresh(self):
        st = self._st()
        with st["lock"]:
            for p in st["providers"].values():
                p.disabled = False
            st["cands"] = build_leaderboard(st["providers"])
        self._send_json(200, {"object": "refresh", "models": len(st["cands"])})

    def _chat(self):
        st = self._st()
        try:
            body = self._read_json()
        except Exception:
            return self._send_json(400, _err_obj("request body is not valid JSON"))
        if not isinstance(body, dict):
            return self._send_json(400, _err_obj("request body must be a JSON object"))
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._send_json(400, _err_obj("'messages' must be a non-empty array"))
        model = str(body.get("model") or "auto")
        stream = bool(body.get("stream", False))
        try:
            max_tokens = int(body.get("max_tokens") or 2048)
        except (TypeError, ValueError):
            max_tokens = 2048
        temperature = body.get("temperature", 0.7)

        cands = st["cands"]
        chain = select_chain(cands, model)
        if not chain:
            if model_known(cands, model):
                return self._send_json(503, _err_obj(
                    f"model '{model}' is known but currently on cooldown / unavailable",
                    "model_unavailable", code=503))
            return self._send_json(404, _err_obj(
                f"model '{model}' not found; GET /v1/models for ids (or use 'auto')",
                "model_not_found", code=404))

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if stream:
            self.close_connection = True       # end the SSE stream by closing
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            sink = SSESink(self.wfile, cid, created)
            cand, text, attempts, err = route_api(chain, messages, sink, True,
                                                  max_tokens, temperature)
            if cand is None:
                sink.send_error_event(err)
            sink.finish()
            return

        cand, text, attempts, err = route_api(chain, messages, Sink(), False,
                                              max_tokens, temperature)
        if cand is None:
            return self._send_json(502, _err_obj("all candidate models failed",
                                                 "upstream_error", code=502,
                                                 attempts=attempts))
        usage = cand.last_usage or approx_usage(messages, text)
        self._send_json(200, {
            "id": cid, "object": "chat.completion", "created": created,
            "model": api_id(cand),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage,
            "cascade": {"provider": cand.prov.key, "model": cand.mid,
                        "attempts": attempts}})


def serve(env, env_path, providers, cands, host, port):
    httpd = ThreadingHTTPServer((host, port), CascadeHandler)
    httpd.daemon_threads = True
    httpd.cascade_state = {"env": env, "env_path": env_path, "providers": providers,
                           "cands": cands, "lock": threading.Lock()}
    line = "═" * 64
    base = f"http://{host}:{port}"
    print(f"{PURPLE}{line}{RESET}")
    print(f"{BOLD}{WHITE}  ⚡ CASCADE SERVER {RESET}{GREY}— OpenAI-compatible unified API{RESET}")
    print(f"{PURPLE}{line}{RESET}")
    print(f"  {GREY}listening:{RESET} {BOLD}{base}{RESET}   "
          f"{GREY}({len(providers)}/{len(CATALOG)} providers · {len(cands)} models){RESET}")
    print(f"  {GREY}chat:{RESET}      POST {base}/v1/chat/completions")
    print(f"  {GREY}models:{RESET}    GET  {base}/v1/models")
    print(f"  {GREY}providers:{RESET} GET  {base}/v1/providers")
    print(f"  {GREY}refresh:{RESET}   POST {base}/v1/refresh")
    auth = "set (bearer required)" if os.environ.get("CASCADE_API_KEY") else "open (any key)"
    print(f"  {GREY}auth:{RESET}      {auth}   "
          f"{GREY}point any OpenAI client at {base}/v1{RESET}")
    print(f"{PURPLE}{line}{RESET}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{GREY}server stopped.{RESET}")
    finally:
        httpd.server_close()


def _arg_value(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    load_state()
    env, env_path = load_env()
    providers = build_providers(env)
    if not providers:
        print(f"{RED}No providers available. Add a key to .env "
              f"(e.g. GROQ_KEY, CEREBRAS_KEY, OR_KEY, NIM_KEY, CF_*).{RESET}")
        sys.exit(1)

    print(f"{GREY}Discovering models across {len(providers)} providers…{RESET}")
    cands = build_leaderboard(providers)
    if not cands:
        print(f"{RED}No usable models discovered.{RESET}")
        sys.exit(1)

    args = sys.argv[1:]
    if "--serve" in args or "--server" in args:
        host = _arg_value(args, "--host") or os.environ.get("CASCADE_HOST", "127.0.0.1")
        try:
            port = int(_arg_value(args, "--port") or os.environ.get("CASCADE_PORT", "8000"))
        except ValueError:
            print(f"{RED}--port must be an integer.{RESET}")
            sys.exit(1)
        serve(env, env_path, providers, cands, host, port)
        return

    if "--list" in args or "-l" in args:
        banner(env_path, providers, len(cands))
        print_leaderboard(cands, limit=60)
        return

    if "--providers" in args:
        banner(env_path, providers, len(cands))
        print_providers(env, providers, cands)
        return

    if "--bench" in args:
        banner(env_path, providers, len(cands))
        run_bench(cands)
        return

    if "-q" in args or "--query" in args:
        idx = args.index("-q") if "-q" in args else args.index("--query")
        prompt = " ".join(args[idx + 1:])
        send(cands, None, [{"role": "user", "content": prompt}])
        return

    banner(env_path, providers, len(cands))
    top = cands[0]
    print(f"  {GREY}top pick:{RESET} {pcolor(top.prov.key)}{BOLD}{top.short()}{RESET} "
          f"{GREY}— type /help for commands{RESET}\n")

    messages = []
    system_prompt = None
    pinned = None

    while True:
        try:
            mode = "auto" if pinned is None else f"pinned:{pinned.short()}"
            user = input(f"{BOLD}{CYAN}you{RESET} {GREY}[{mode}]{RESET} › ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{GREY}bye.{RESET}")
            break
        if not user:
            continue

        if user.startswith("/"):
            parts = user.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            if cmd in ("/quit", "/exit", "/q"):
                print(f"{GREY}bye.{RESET}")
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd in ("/models", "/leaderboard", "/m"):
                print_leaderboard(cands, highlight=pinned)
            elif cmd in ("/providers", "/p"):
                print_providers(env, providers, cands)
            elif cmd in ("/usage", "/u"):
                print_usage(providers, cands)
            elif cmd in ("/bench", "/b"):
                run_bench(cands)
            elif cmd == "/fastest":
                if any(x.tps for x in cands):
                    cands.sort(key=lambda x: (x.tps is None, -(x.tps or 0)))
                    print(f"{GREEN}Re-ranked by measured speed (fastest first).{RESET}\n")
                else:
                    print(f"{YELLOW}Run /bench first to measure speeds.{RESET}\n")
            elif cmd == "/quality":
                cands.sort(key=lambda x: (-x.score, -x.size, x.mid))
                print(f"{GREEN}Re-ranked by quality (best → worst).{RESET}\n")
            elif cmd == "/use":
                try:
                    n = int(arg)
                    pinned = cands[n]
                    print(f"{GREEN}Pinned to {pinned.short()}.{RESET}\n")
                except (ValueError, IndexError):
                    print(f"{RED}Usage: /use <index from /models>{RESET}")
            elif cmd == "/auto":
                pinned = None
                print(f"{GREEN}Auto-routing re-enabled.{RESET}\n")
            elif cmd == "/system":
                system_prompt = arg or None
                print(f"{GREEN}System prompt {'set' if arg else 'cleared'}.{RESET}\n")
            elif cmd == "/clear":
                messages = []
                print(f"{GREEN}Conversation cleared.{RESET}\n")
            elif cmd == "/refresh":
                print(f"{GREY}Re-discovering…{RESET}")
                for p in providers.values():
                    p.disabled = False
                cands = build_leaderboard(providers)
                print(f"{GREEN}{len(cands)} models.{RESET}\n")
            else:
                print(f"{RED}Unknown command. /help for list.{RESET}")
            continue

        messages.append({"role": "user", "content": user})
        outbound = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages
        reply = send(cands, pinned, outbound)
        if reply is not None:
            messages.append({"role": "assistant", "content": reply})
        else:
            messages.pop()  # drop unanswered turn


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{GREY}interrupted.{RESET}")
