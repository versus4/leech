"""
Watchdog. Records harvest / claim / send outcomes so SILENT failures surface
loudly instead of just slowing everything down. Read it via /health and /bank.

It distinguishes the failure modes that actually happen:
  - Tor blocked / proxies dead  -> harvest success rate craters
  - selectors drifted           -> harvest or message success craters
  - tokens expiring             -> message (send) success drops
  - bank draining faster than refill / startup  -> fresh == 0
  - Tor daemon not running       -> control renew always fails
"""
import threading
import time
from collections import defaultdict, deque

from . import config


class Health:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = defaultdict(int)
        self.harvest_window = deque(maxlen=50)
        self.send_window = deque(maxlen=50)
        self.errors = deque(maxlen=20)
        self.last_harvest_ok = None
        self.last_send_ok = None

    def harvest(self, ok: bool, err=None):
        with self._lock:
            self.counters["harvest_ok" if ok else "harvest_fail"] += 1
            self.harvest_window.append(bool(ok))
            if ok:
                self.last_harvest_ok = time.time()
            elif err is not None:
                self.errors.appendleft({"where": "harvest", "error": str(err)[:200]})

    def send(self, ok: bool, path: str, err=None):
        with self._lock:
            self.counters[f"{path}_ok" if ok else f"{path}_fail"] += 1
            self.counters["send_ok" if ok else "send_fail"] += 1
            self.send_window.append(bool(ok))
            if ok:
                self.last_send_ok = time.time()
            elif err is not None:
                self.errors.appendleft({"where": path, "error": str(err)[:200]})

    def claim(self, hit: bool):
        with self._lock:
            self.counters["claim_hit" if hit else "claim_empty"] += 1

    def tor(self, ok: bool):
        with self._lock:
            self.counters["tor_renew_ok" if ok else "tor_renew_fail"] += 1

    @staticmethod
    def _rate(window):
        return sum(window) / len(window) if window else None

    def snapshot(self, fresh: int) -> dict:
        with self._lock:
            hr = self._rate(self.harvest_window)
            sr = self._rate(self.send_window)
            status, reasons = self._diagnose(fresh, hr, sr)
            now = time.time()
            return {
                "status": status,
                "reasons": reasons,
                "fresh_accounts": fresh,
                "harvest_success_rate": round(hr, 2) if hr is not None else None,
                "send_success_rate": round(sr, 2) if sr is not None else None,
                "seconds_since_harvest":
                    round(now - self.last_harvest_ok, 1) if self.last_harvest_ok else None,
                "seconds_since_send":
                    round(now - self.last_send_ok, 1) if self.last_send_ok else None,
                "counters": dict(self.counters),
                "recent_errors": list(self.errors)[:5],
            }

    def _diagnose(self, fresh, hr, sr):
        if getattr(config, "DIRECT_WS_ENABLED", False):
            status, reasons = "ok", []
            n = len(self.send_window)
            if sr is not None and n >= 5 and sr < 0.5:
                status = "critical"
                reasons.append(f"message success low ({sr:.0%}) -> model runner "
                               "protocol may have changed")
            elif sr is not None and n >= 5 and sr < 0.85:
                status = "warning"
                reasons.append(f"message success degraded ({sr:.0%}) -> intermittent failures")
            if not reasons:
                reasons.append("headless WS path nominal")
            return status, reasons

        status, reasons = "ok", []

        if hr is not None and len(self.harvest_window) >= 5 and hr < 0.3:
            status = "warning"
            reasons.append(f"harvest success low ({hr:.0%}) -> Tor blocked, proxies dead, "
                           "or signup selectors drifted")

        if sr is not None and len(self.send_window) >= 5 and sr < 0.5:
            status = "warning"
            reasons.append(f"message success low ({sr:.0%}) -> chat selectors drifted "
                           "or tokens expiring")

        if config.PROXY_TOR and self.counters["tor_renew_fail"] and not self.counters["tor_renew_ok"]:
            status = "critical"
            reasons.append("Tor control unreachable -> is start_tor.bat running in leech/ ?")

        if fresh == 0:
            if self.counters["harvest_fail"] > self.counters["harvest_ok"]:
                status = "critical"
                reasons.append("bank EMPTY and harvesting is failing -> the farm is down")
            else:
                status = "critical" if status == "critical" else "warning"
                reasons.append("bank empty -> warming up, or draining faster than refill "
                               "(raise BANK_PREWARM_BATCH / BANK_MIN_FRESH)")

        if not reasons:
            reasons.append("all systems nominal")
        return status, reasons


H = Health()
