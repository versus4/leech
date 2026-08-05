"""
The leech worker. run_prompt(model, prompt) -> str is the only thing the
backend calls. Three paths, fastest first:

  1. DIRECT  -> claim a banked token, replay use.ai's API over HTTP (no browser)
  2. WARM    -> claim a banked session, load it into a browser (skip signup)
  3. COLD    -> full inline signup -> prompt -> scrape (original fallback)

Every browser context auto-rotates through the proxy pool (if configured), and
a banked account reuses the SAME proxy it signed up on for IP consistency.
"""
import json
import logging

from . import config
from .email_gen import gen_email, gen_password

log = logging.getLogger("leech")

try:
    from cloakbrowser import async_playwright
    USING_CLOAK = True
except ImportError:
    from playwright.async_api import async_playwright
    USING_CLOAK = False
    log.warning("cloakbrowser not installed -> falling back to stock playwright (NO stealth)")


def _ok(sel_key: str) -> bool:
    """True if a selector has actually been filled in."""
    return config.SELECTORS.get(sel_key, "REPLACE_ME") != "REPLACE_ME"


def _launch_kwargs() -> dict:
    kw = {"headless": config.HEADLESS}
    if USING_CLOAK:
        kw["humanize"] = config.HUMANIZE
    return kw


async def _new_context(p, storage_state=None, proxy="auto"):
    """proxy: a playwright proxy dict, None (force direct IP), or
    'auto' (pull the next one from the rotating pool)."""
    from . import proxies
    if proxy == "auto":
        proxy = proxies.next_proxy()
    browser = await p.chromium.launch(**_launch_kwargs())
    ctx = await browser.new_context(storage_state=storage_state, proxy=proxy)
    ctx.set_default_timeout(config.ACTION_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
    return browser, ctx


async def _switch_model(page, model_key: str):
    if not _ok("model_dropdown"):
        return
    label = config.resolve_model(model_key)
    sel = config.SELECTORS
    try:
        await page.click(sel["model_dropdown"])
        opt = sel["model_option"]
        if "%s" in opt:
            await page.click(opt % label)
        else:
            await page.get_by_text(label, exact=True).click()
    except Exception as e:
        log.warning("model switch failed (%s): %s -- continuing", model_key, e)


async def _signup(page) -> str:
    sel = config.SELECTORS
    await page.click(sel["signup_button"])
    if _ok("email_reveal"):
        try:
            await page.click(sel["email_reveal"])
        except Exception as e:
            log.warning("email reveal step failed: %s -- continuing", e)
    for attempt in range(1, config.SIGNUP_MAX_RETRIES + 1):
        email, pw = gen_email(), gen_password()
        await page.fill(sel["email_input"], email)
        if _ok("password_input"):
            await page.fill(sel["password_input"], pw)
        await page.click(sel["signup_submit"])
        if _ok("email_taken_error"):
            try:
                await page.wait_for_selector(sel["email_taken_error"], timeout=2500)
                log.info("email taken, rerolling (%d/%d)", attempt, config.SIGNUP_MAX_RETRIES)
                continue
            except Exception:
                pass
        log.info("registered as %s", email)
        return email
    raise RuntimeError("signup failed: ran out of email reroll attempts")


async def _ask(page, prompt: str) -> str:
    sel = config.SELECTORS
    await page.fill(sel["prompt_input"], prompt)
    await page.click(sel["prompt_submit"])
    await page.wait_for_selector(sel["response_block"], timeout=config.RESPONSE_TIMEOUT_MS)
    if _ok("response_done"):
        try:
            await page.wait_for_selector(sel["response_done"], timeout=config.RESPONSE_TIMEOUT_MS)
        except Exception:
            pass
    else:
        try:
            await page.wait_for_load_state("networkidle", timeout=config.RESPONSE_TIMEOUT_MS)
        except Exception:
            pass
    blocks = await page.query_selector_all(sel["response_block"])
    return (await blocks[-1].inner_text()).strip() if blocks else ""


async def _prompt_with_state(state_path: str, model: str, prompt: str, proxy=None) -> str:
    """WARM path: reuse a banked session, skip signup entirely."""
    if not state_path:
        raise RuntimeError("no storage state for warm path")
    async with async_playwright() as p:
        browser, ctx = await _new_context(p, storage_state=state_path, proxy=proxy)
        try:
            page = await ctx.new_page()
            await page.goto(config.TARGET_URL, wait_until="domcontentloaded")
            await _switch_model(page, model)
            return await _ask(page, prompt)
        finally:
            await ctx.close()
            await browser.close()


async def _cold_run(model: str, prompt: str) -> str:
    """COLD path: a fresh browser/IP spends ONE message, then is discarded.

    GUEST_MODE (default): use.ai gives anonymous guests one free prompt with no
    signup, so we skip straight to the prompt. The fresh proxy IP per context is
    what grants a new free prompt (the cap is fingerprint/IP-keyed).
    GUEST_MODE off: attempt the inline email signup first (OTP-gated -- generally
    will not complete headless against use.ai).
    """
    async with async_playwright() as p:
        browser, ctx = await _new_context(p)
        try:
            page = await ctx.new_page()
            await page.goto(config.TARGET_URL, wait_until="domcontentloaded")
            await _switch_model(page, model)
            if not getattr(config, "GUEST_MODE", False):
                await _signup(page)
            return await _ask(page, prompt)
        finally:
            await ctx.close()
            await browser.close()


async def run_prompt(model: str, prompt: str) -> str:
    """Send exactly ONE message via a fresh throwaway account.

    PRIMARY path is headless: sign up over HTTP, stream the reply over the
    budget-agent WebSocket -- no browser at all. Each account is worth exactly
    one free message; the direct path signs up a fresh one per call (and rerolls
    internally on cap). The browser COLD path stays as a fallback.
    """
    from . import direct, health

    if direct.enabled():
        from . import account_pool
        try:
            acct = await account_pool.POOL.acquire()
            reply = await direct.complete(model, prompt=prompt, acct=acct)
            health.H.send(True, "direct")
            return reply
        except Exception as e:
            health.H.send(False, "direct", e)
            if not getattr(config, "BROWSER_FALLBACK_ENABLED", False):
                log.warning("direct WS path failed: %r", e)
                raise RuntimeError(f"model runner unavailable: direct WS failed ({e!r})") from e
            log.warning("direct WS path failed (%r) -> falling back to browser", e)

    try:
        reply = await _cold_run(model, prompt)
        health.H.send(True, "cold")
        return reply
    except Exception as e:
        health.H.send(False, "cold", e)
        raise


def _flatten_messages(messages: list) -> str:
    """Collapse a role-tagged history into one prompt (browser fallback only)."""
    msgs = [m for m in messages if (m.get("content") or "").strip()]
    if len(msgs) <= 1:
        return msgs[0]["content"] if msgs else ""
    lines = ["[Previous conversation]"]
    for m in msgs[:-1]:
        who = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {m['content']}")
    lines.append("\n[Now respond only to this latest message]")
    lines.append(f"User: {msgs[-1]['content']}")
    return "\n".join(lines)


async def run_messages(model: str, messages: list, acct: dict | None = None) -> str:
    """Native multi-turn: pass the role-tagged history straight to the WS frame
    (the budget-agent honors prior turns). Browser fallback flattens to text."""
    from . import direct, health

    if direct.enabled():
        from . import account_pool
        try:
            if acct is None:
                acct = await account_pool.POOL.acquire()
            reply = await direct.complete(model, messages=messages, acct=acct)
            health.H.send(True, "direct")
            return reply
        except Exception as e:
            health.H.send(False, "direct", e)
            if not getattr(config, "BROWSER_FALLBACK_ENABLED", False):
                log.warning("direct WS path failed: %r", e)
                raise RuntimeError(f"model runner unavailable: direct WS failed ({e!r})") from e
            log.warning("direct WS path failed (%r) -> falling back to browser", e)

    try:
        reply = await _cold_run(model, _flatten_messages(messages))
        health.H.send(True, "cold")
        return reply
    except Exception as e:
        health.H.send(False, "cold", e)
        raise


async def stream_messages(model: str, messages: list, acct: dict | None = None):
    """Streaming multi-turn: yield text deltas as the WS produces them, so the
    client sees a long reply build up live instead of waiting for the whole thing
    (and no total-time cap kills big code generations)."""
    from . import direct, health

    if direct.enabled():
        from . import account_pool
        produced = False
        try:
            if acct is None:
                acct = await account_pool.POOL.acquire()
            async for delta in direct.stream(model, messages=messages, acct=acct):
                produced = True
                yield delta
            health.H.send(True, "direct")
            return
        except Exception as e:
            health.H.send(False, "direct", e)
            if produced:
                return
            if not getattr(config, "BROWSER_FALLBACK_ENABLED", False):
                log.warning("direct WS stream failed: %r", e)
                raise RuntimeError(f"model runner unavailable: direct WS failed ({e!r})") from e
            log.warning("direct WS stream failed (%r) -> browser fallback", e)

    reply = await _cold_run(model, _flatten_messages(messages))
    health.H.send(True, "cold")
    yield reply
