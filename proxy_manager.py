"""
proxy_manager.py — Rotating proxy pool with failure tracking.

Reads proxies from proxies.txt (one per line):
  http://user:pass@ip:port
  socks5://user:pass@ip:port
  http://ip:port
"""

import logging
import threading
import time
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

PROXY_FILE = "proxies.txt"
COOLDOWN_SECONDS = 60       # ban a proxy for this long after failure
MAX_FAILURES = 3            # failures before temporary ban


class ProxyManager:
    def __init__(self, proxy_file: str = PROXY_FILE):
        self._proxies: list[str] = []
        self._index: int = 0
        self._lock = threading.Lock()
        self._failures: dict[str, int] = defaultdict(int)
        self._banned_until: dict[str, float] = {}
        self._load(proxy_file)

    def _load(self, path: str):
        p = Path(path)
        if not p.exists():
            logger.warning("No proxy file found at %s — running without proxies", path)
            return
        lines = [l.strip() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]
        self._proxies = lines
        logger.info("Loaded %d proxies from %s", len(self._proxies), path)

    def reload(self):
        """Hot-reload proxy list."""
        with self._lock:
            self._load(PROXY_FILE)

    def get(self) -> dict | None:
        """Get next available proxy dict or None if no proxies loaded."""
        if not self._proxies:
            return None

        with self._lock:
            now = time.time()
            tried = 0
            total = len(self._proxies)

            while tried < total:
                url = self._proxies[self._index % total]
                self._index = (self._index + 1) % total
                tried += 1

                # Skip if temporarily banned
                if self._banned_until.get(url, 0) > now:
                    continue

                return {"http": url, "https": url}

            # All proxies banned — clear bans and return first
            logger.warning("All proxies temporarily banned, clearing bans")
            self._banned_until.clear()
            self._failures.clear()
            url = self._proxies[0]
            return {"http": url, "https": url}

    def report_failure(self, proxy_dict: dict | None):
        """Call when a request fails due to proxy issue."""
        if not proxy_dict:
            return
        url = proxy_dict.get("https") or proxy_dict.get("http")
        if not url:
            return
        with self._lock:
            self._failures[url] += 1
            if self._failures[url] >= MAX_FAILURES:
                self._banned_until[url] = time.time() + COOLDOWN_SECONDS
                logger.warning("Proxy banned for %ds: %s", COOLDOWN_SECONDS, url[:40])

    def report_success(self, proxy_dict: dict | None):
        """Call after a successful request to reset failure count."""
        if not proxy_dict:
            return
        url = proxy_dict.get("https") or proxy_dict.get("http")
        if url:
            with self._lock:
                self._failures[url] = 0

    @property
    def count(self) -> int:
        return len(self._proxies)


# Singleton
proxy_manager = ProxyManager()
