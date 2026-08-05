"""
Fetch + validate FREE public proxies (no paid account needed).

Free proxies are mostly dead, slow, or honeypots -- so we pull thousands from
public lists, TEST every one against a live endpoint, and keep only the working
ones. The survivors are written to PROXY_FILE for the worker to rotate through.

Run it (from the leech/ root):
    python -m worker.proxy_sources              # refresh the live list
    python -m worker.proxy_sources 500          # only test the first 500 candidates

Then set  PROXY_FILE = "proxies.txt"  in config.py and you're rotating for free.
Re-run it periodically -- free proxies die fast.
"""
import asyncio
import logging
import sys

from . import config

log = logging.getLogger("proxy_sources")

try:
    import httpx
except ImportError:
    httpx = None

SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=ipport&format=text",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]

TEST_URL = "http://httpbin.org/ip"


async def _fetch_list(client, url):
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        return [l.strip() for l in r.text.splitlines() if l.strip() and ":" in l]
    except Exception as e:
        log.warning("source failed %s: %s", url, e)
        return []


async def _check(sem, line):
    url = line if "://" in line else "http://" + line
    async with sem:
        try:
            async with httpx.AsyncClient(proxy=url, timeout=8) as c:
                r = await c.get(TEST_URL)
                if r.status_code == 200:
                    return line
        except Exception:
            return None
    return None


async def refresh(limit: int = 2000, concurrency: int = 200):
    if httpx is None:
        raise RuntimeError("httpx not installed (pip install httpx)")

    async with httpx.AsyncClient() as c:
        lists = await asyncio.gather(*[_fetch_list(c, s) for s in SOURCES])
    candidates = list(dict.fromkeys(p for lst in lists for p in lst))
    if limit:
        candidates = candidates[:limit]
    log.info("fetched %d unique candidates, testing %d...", len(candidates), len(candidates))

    sem = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(*[_check(sem, p) for p in candidates])
    live = [r for r in results if r]

    out = config.PROXY_FILE or "proxies.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(live) + ("\n" if live else ""))
    log.info("kept %d/%d live proxies -> %s", len(live), len(candidates), out)
    if not live:
        log.warning("no live proxies this run -- free lists are flaky, try again")
    return live


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    asyncio.run(refresh(limit=limit))


if __name__ == "__main__":
    main()
