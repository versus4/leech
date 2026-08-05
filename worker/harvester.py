"""
Harvester: signs up throwaway accounts in the background and banks each one's
auth token + saved browser session + the proxy it was born on. Run by the
backend prewarmer so the bank always has warm accounts ready.
"""
import asyncio
import json
import logging
import os
import uuid

from . import bank, config, health, proxies
from .leech import (
    USING_CLOAK,
    _new_context,
    _ok,
    _signup,
    _switch_model,
    async_playwright,
)

log = logging.getLogger("harvester")


async def _extract_token(page, ctx) -> str:
    key = config.AUTH_TOKEN_KEY
    if key == "REPLACE_ME" or config.AUTH_TOKEN_STORAGE == "none":
        return ""
    try:
        if config.AUTH_TOKEN_STORAGE == "local":
            return await page.evaluate("(k) => localStorage.getItem(k)", key) or ""
        if config.AUTH_TOKEN_STORAGE == "cookie":
            for ck in await ctx.cookies():
                if ck.get("name") == key:
                    return ck.get("value", "")
    except Exception as e:
        log.warning("token extract failed: %s", e)
    return ""


async def harvest_one() -> bool:
    """Sign up one account, bank its token + session + proxy. Returns success."""
    proxy = proxies.next_proxy()
    async with async_playwright() as p:
        browser, ctx = await _new_context(p, proxy=proxy)
        try:
            page = await ctx.new_page()
            await page.goto(config.TARGET_URL, wait_until="domcontentloaded")
            await _switch_model(page, "default")
            email = await _signup(page)
            token = await _extract_token(page, ctx)

            os.makedirs(config.STORAGE_STATE_DIR, exist_ok=True)
            state_path = os.path.join(config.STORAGE_STATE_DIR, f"{uuid.uuid4().hex}.json")
            await ctx.storage_state(path=state_path)

            bank.add(email, token, state_path, proxy=json.dumps(proxy) if proxy else None)
            log.info("harvested %s (token=%s, proxy=%s)",
                     email, "yes" if token else "no",
                     proxy["server"] if proxy else "direct")
            health.H.harvest(True)
            return True
        except Exception as e:
            log.warning("harvest failed: %s", e)
            health.H.harvest(False, e)
            return False
        finally:
            await ctx.close()
            await browser.close()


async def top_up() -> int:
    """Bring the bank back up to BANK_MIN_FRESH, one batch at a time."""
    need = config.BANK_MIN_FRESH - bank.count_fresh()
    batch = min(max(need, 0), config.BANK_PREWARM_BATCH)
    if batch <= 0:
        return 0

    if config.PROXY_TOR:
        ok = 0
        for i in range(batch):
            if i > 0:
                await asyncio.sleep(config.TOR_NEWNYM_DELAY)
            health.H.tor(proxies.renew_tor_circuit())
            if await harvest_one():
                ok += 1
        return ok

    results = await asyncio.gather(*[harvest_one() for _ in range(batch)])
    return sum(1 for r in results if r)
