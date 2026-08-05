"""
Headless account factory. use.ai signup is two unauthenticated POSTs and needs
NO password, NO email verification (fake emails are accepted; emailVerified stays
null). One free message per account, unlimited accounts per IP -> no proxies.

create_account() -> {email, user_id, cookie_header, token}
"""
import asyncio
import time
import uuid
import logging

import httpx

from . import config
from .email_gen import gen_email

log = logging.getLogger("session_http")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36")
_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://use.ai",
    "Referer": "https://use.ai/",
    "User-Agent": _UA,
}


async def create_account(proxy: str | None = None) -> dict:
    """Sign up a throwaway account. Returns email, user id, cookie header, token."""
    email = gen_email()
    async with httpx.AsyncClient(timeout=30, headers=_HEADERS, proxy=proxy) as c:
        r1 = await c.post(f"{config.AUTH_BASE}/email-login", json={"email": email})
        r1.raise_for_status()
        r2 = await c.post(f"{config.AUTH_BASE}/sign-in/credentials", json={
            "email": email,
            "mixpanelUserId": str(uuid.uuid4()),
            "guestId": str(uuid.uuid4()),
            "mid": str(uuid.uuid4()),
        })
        r2.raise_for_status()
        token = r2.headers.get("set-auth-token", "")

        s = await c.get(f"{config.AUTH_BASE}/get-session")
        if s.status_code != 200 or s.text in ("", "null"):
            raise RuntimeError(f"get-session empty after signup ({s.status_code})")
        j = s.json()
        user_id = j["user"]["id"]
        cookie_header = "; ".join(f"{k}={v}" for k, v in c.cookies.items())

    log.info("created account %s (userId=%s)", email, user_id[:8])
    return {"email": email, "user_id": user_id,
            "cookie_header": cookie_header, "token": token}


async def get_ws_tokens(acct: dict, proxy: str | None = None) -> tuple[str, str]:
    lock = acct.get("_ws_lock")
    if lock is None:
        lock = acct["_ws_lock"] = asyncio.Lock()
    async with lock:
        cached = acct.get("_ws_tokens")
        if cached and time.time() < cached.get("expires_at", 0) - 30:
            return cached["token"], cached["app_token"]
        hdrs = {"Cookie": acct["cookie_header"], "Origin": "https://use.ai",
                "Referer": "https://use.ai/", "User-Agent": _UA}
        async with httpx.AsyncClient(timeout=30, headers=hdrs, proxy=proxy) as c:
            r, r2 = await asyncio.gather(
                c.get(f"{config.AUTH_BASE}/token"),
                c.post(f"{config.AUTH_BASE}/app-attestation",
                       json={"priorToken": (cached or {}).get("app_token", "")}),
            )
            r.raise_for_status()
            r2.raise_for_status()
            token = r.json().get("token", "")
            attestation = r2.json()
            app_token = attestation.get("token", "")
            expires_in = float(attestation.get("expiresIn") or 300)
        if not token or not app_token:
            raise RuntimeError("token/app-attestation mint returned empty")
        now = time.time()
        acct["_ws_tokens"] = {"token": token, "app_token": app_token,
                              "at": now, "expires_at": now + max(30, expires_in)}
        return token, app_token
