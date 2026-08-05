"""
Proxy rotation. cloakbrowser hides the BROWSER; this hides the NETWORK so a
flood of signups doesn't all come from one IP.

Three modes, all optional:
  - Tor          (PROXY_TOR=True): free rotation via the Tor network
  - proxy pool   (PROXIES / PROXY_FILE): a list you bring (free or paid)
  - nothing      (defaults): runs on your direct IP, exactly as before

Line formats (scheme optional, defaults to PROXY_DEFAULT_SCHEME):
    1.2.3.4:8000
    http://1.2.3.4:8000
    socks5://user:pass@1.2.3.4:1080
    user:pass@1.2.3.4:8000
"""
import itertools
import logging
import os
import random
import socket
import threading

from . import config

log = logging.getLogger("proxies")

_lock = threading.Lock()
_pool: list = []
_rr = None
_loaded = False


def _parse(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        scheme, rest = line.split("://", 1)
    else:
        scheme, rest = config.PROXY_DEFAULT_SCHEME, line

    username = password = None
    if "@" in rest:
        creds, host = rest.rsplit("@", 1)
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
    else:
        host = rest

    proxy = {"server": f"{scheme}://{host}"}
    if username is not None:
        proxy["username"] = username
    if password is not None:
        proxy["password"] = password
    return proxy


def _load():
    global _pool, _rr, _loaded
    pool = [p for p in (_parse(l) for l in config.PROXIES) if p]
    if config.PROXY_FILE and os.path.exists(config.PROXY_FILE):
        with open(config.PROXY_FILE, encoding="utf-8") as f:
            pool += [p for p in (_parse(l) for l in f) if p]
    _pool = pool
    _rr = itertools.cycle(pool) if pool else None
    _loaded = True


def has_proxies() -> bool:
    if config.PROXY_TOR:
        return True
    if not _loaded:
        _load()
    return bool(_pool)


def next_proxy():
    """Return the next proxy dict (playwright format) or None if none configured."""
    if config.PROXY_TOR:
        return {"server": config.TOR_SOCKS}
    if not _loaded:
        _load()
    if not _pool:
        return None
    with _lock:
        if config.PROXY_ROTATION == "random":
            return random.choice(_pool)
        return next(_rr)


def _tor_auth_line() -> str:
    """Build the control-port AUTHENTICATE line: password if set, else cookie."""
    pw = config.TOR_CONTROL_PASSWORD
    if pw:
        return f'AUTHENTICATE "{pw}"'
    cookie_path = config.TOR_COOKIE_PATH or os.path.join(config.TOR_DATA_DIR, "control_auth_cookie")
    with open(cookie_path, "rb") as f:
        return "AUTHENTICATE " + f.read().hex()


def renew_tor_circuit() -> bool:
    """Ask Tor for a fresh exit IP (NEWNYM). Tor rate-limits this to ~10s."""
    if not config.PROXY_TOR:
        return False
    try:
        s = socket.create_connection(("127.0.0.1", config.TOR_CONTROL_PORT), timeout=5)
        s.send((_tor_auth_line() + "\r\n").encode())
        if not s.recv(256).startswith(b"250"):
            log.warning("tor control auth failed (cookie/password mismatch)")
            s.close()
            return False
        s.send(b"SIGNAL NEWNYM\r\n")
        ok = s.recv(256).startswith(b"250")
        s.close()
        return ok
    except FileNotFoundError:
        log.warning("tor auth cookie not found -- did you run start_tor.bat from leech/?")
        return False
    except Exception as e:
        log.warning("tor circuit renew failed: %s (is the tor daemon running?)", e)
        return False


def to_url(proxy):
    """playwright proxy dict -> a proxy URL string (for httpx). None stays None."""
    if not proxy:
        return None
    server = proxy["server"]
    user, pw = proxy.get("username"), proxy.get("password")
    if user:
        scheme, host = server.split("://", 1)
        auth = user + (f":{pw}" if pw else "")
        return f"{scheme}://{auth}@{host}"
    return server
