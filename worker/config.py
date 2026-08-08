"""
Central config for the leech worker.

EVERYTHING site-specific lives here. When use.ai changes its UI, you fix it
in ONE place. The two things you MUST verify against the live site:
  1. The SELECTORS dict  -> open the site, inspect, paste real CSS selectors.
  2. The cloakbrowser launch call in leech.py (_new_context).
"""

import logging

_log = logging.getLogger("config")

TARGET_URL = "https://use.ai"

HEADLESS = True
HUMANIZE = True
GUEST_MODE = False
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
RESPONSE_TIMEOUT_MS = 90_000

MAX_CONCURRENT_BROWSERS = 4

EMAIL_LOCAL_MIN = 8
EMAIL_LOCAL_MAX = 14
EMAIL_DOMAIN_MIN = 5
EMAIL_DOMAIN_MAX = 9
EMAIL_TLDS = ["com", "net", "org", "io", "co", "xyz"]
PASSWORD_LENGTH = 16
SIGNUP_MAX_RETRIES = 5

DEFAULT_MODEL = "gpt-5-6-sol"

MODELS = [
    {"slug": "claude-fable-5",           "label": "Claude Fable 5"},
    {"slug": "claude-opus-5",            "label": "Claude Opus 5"},
    {"slug": "claude-opus-4-8",          "label": "Claude Opus 4.8"},
    {"slug": "claude-sonnet-5",          "label": "Claude Sonnet 5"},
    {"slug": "gpt-5-6-sol",              "label": "OpenAI GPT-5.6 Sol"},
    {"slug": "gpt-5-5",                  "label": "OpenAI GPT-5.5"},
    {"slug": "gpt-5-4",                  "label": "OpenAI GPT-5.4"},
    {"slug": "gemini-3-6-flash",         "label": "Gemini 3.6 Flash"},
    {"slug": "deepseek-v4-pro",          "label": "DeepSeek V4 Pro"},
    {"slug": "kimi-k2-6",                "label": "Kimi K2.6"},
    {"slug": "grok-4-5",                 "label": "Grok 4.5"},
    {"slug": "glm-5-2",                  "label": "GLM 5.2"},
]

PREFERRED_DEFAULTS = ["gpt-5-6-sol", "claude-sonnet-5", "gpt-5-5"]
ALIAS_PREFERENCES = {
    "default": PREFERRED_DEFAULTS,
    "fast":    ["gemini-3-6-flash", "deepseek-v4-flash", "gpt-5-4"],
    "smart":   ["claude-fable-5", "claude-opus-5", "claude-opus-4-8", "gpt-5-6-sol"],
}

MODEL_ALIASES = {
    "default": "gpt-5-6-sol",
    "fast":    "gemini-3-6-flash",
    "smart":   "claude-fable-5",
}

_MODEL_SLUGS = {m["slug"] for m in MODELS}


def resolve_model(name: str) -> str:
    """Map a UI/API model name (slug OR alias) to a real use.ai slug."""
    if not name:
        return DEFAULT_MODEL
    if name in _MODEL_SLUGS:
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    _log.warning("unknown model %r -> falling back to %s", name, DEFAULT_MODEL)
    return DEFAULT_MODEL


MODEL_MAP = {**MODEL_ALIASES, **{m["slug"]: m["slug"] for m in MODELS}}

SELECTORS = {
    "model_dropdown":    '[data-testid="model-selector"]',
    "model_option":      '[data-testid="model-option-gateway-%s"]',
    "signup_button":     '[data-testid="header-sign-in-button"]',
    "email_reveal":      '[data-testid="signin-with-email-button"]',
    "email_input":       '[data-testid="email-input"]',
    "password_input":    "REPLACE_ME",
    "signup_submit":     '[data-testid="signin-with-email-button"]',
    "email_taken_error": "REPLACE_ME",
    "prompt_input":      '[data-testid="chat-input-textarea"]',
    "prompt_submit":     '[data-testid="send-button"]',
    "response_block":    '[data-testid="message-assistant"]',
    "response_done":     '[data-testid="message-upvote"]',
}

AUTH_TOKEN_STORAGE = "cookie"
AUTH_TOKEN_KEY = "__Secure-better-auth.session_token"

DIRECT_WS_ENABLED = True
AUTH_BASE     = "https://api.use.ai/v1/auth"
WS_AGENT_BASE = "wss://agents.use.ai/agents/budget-agent"
MODEL_PREFIX  = "gateway-"
WS_OPEN_TIMEOUT = 30
WS_REPLY_TIMEOUT = 90
WS_IDLE_TIMEOUT = 150
WS_IDLE_TIMEOUT_REASONING = 300
REASONING_MODEL_MARKERS = ("-r1", "reasoning", "-think")
DIRECT_WS_RETRIES = 2
EMPTY_REPLY_RETRIES = 2
DIRECT_WS_BACKOFF = 0.75
DIRECT_WS_BACKOFF_CAP = 8.0
USEAI_RESUME_RETRIES = 2
BROWSER_FALLBACK_ENABLED = False
ACCOUNT_POOL_SIZE = 8
ACCOUNT_POOL_REFILL_SEC = 3
ACCOUNT_TTL_SEC = 600
DIRECT_MAX_CONCURRENCY = 24

DIRECT_API_URL = ""
DIRECT_API_METHOD = "POST"
DIRECT_API_BODY = '{"model": "{model}", "messages": [{"role": "user", "content": "{prompt}"}]}'
DIRECT_API_AUTH_HEADER = "Authorization"
DIRECT_API_AUTH_FORMAT = "Bearer {token}"
DIRECT_API_RESPONSE_PATH = "choices.0.message.content"

BANK_PATH = "bank/accounts.db"
STORAGE_STATE_DIR = "bank/states"
BANK_MIN_FRESH = 10
BANK_PREWARM_BATCH = 5
PREWARM_INTERVAL_SEC = 30
MAX_BANKED_ATTEMPTS = 2

PROXIES = []
PROXY_FILE = ""
PROXY_ROTATION = "round_robin"
PROXY_DEFAULT_SCHEME = "http"

PROXY_TOR = True
TOR_BROWSER_DIR = r"C:\Users\Emir\Desktop\Tor Browser"
TOR_SOCKS = "socks5://127.0.0.1:9050"
TOR_CONTROL_PORT = 9051
TOR_CONTROL_PASSWORD = ""
TOR_DATA_DIR = "tor_data"
TOR_COOKIE_PATH = ""
TOR_NEWNYM_DELAY = 10
