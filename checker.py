"""
checker.py — Instagram account status checker.
Uses requests (proven to work with proxies) wrapped in asyncio executor.
"""

import asyncio
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from requests.exceptions import ProxyError, Timeout, ConnectionError as ReqConnError

from proxy_manager import proxy_manager

logger = logging.getLogger(__name__)

STATUS_ACTIVE  = "active"
STATUS_BANNED  = "banned"
STATUS_UNKNOWN = "unknown"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

MAX_RETRIES = 3
BASE_DELAY  = 2.0
MAX_DELAY   = 30.0

_executor = ThreadPoolExecutor(max_workers=10)


def _headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def _check_sync(username: str, timeout: int = 15) -> str:
    """Synchronous check — runs in thread pool."""
    url = f"https://www.instagram.com/{username}/"

    for attempt in range(1, MAX_RETRIES + 1):
        proxy_dict = proxy_manager.get()

        try:
            resp = requests.get(
                url,
                headers=_headers(),
                proxies=proxy_dict,
                timeout=timeout,
                allow_redirects=True,
                verify=False,          # skip SSL verify for proxy compat
            )
            code = resp.status_code
            logger.info("Check @%s → HTTP %s (attempt %d/%d)", username, code, attempt, MAX_RETRIES)

            if code == 200:
                proxy_manager.report_success(proxy_dict)
                if "accounts/login" in resp.url or "challenge" in resp.url:
                    return STATUS_UNKNOWN
                return STATUS_ACTIVE

            if code == 404:
                proxy_manager.report_success(proxy_dict)
                return STATUS_BANNED

            if code == 429:
                logger.warning("Rate limited on @%s (attempt %d)", username, attempt)
                proxy_manager.report_failure(proxy_dict)
                time.sleep(min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY))
                continue

            logger.warning("Unexpected HTTP %s for @%s", code, username)
            return STATUS_UNKNOWN

        except (ProxyError, ReqConnError) as e:
            logger.warning("Proxy/connection error @%s: %s (attempt %d)", username, e, attempt)
            proxy_manager.report_failure(proxy_dict)
            time.sleep(min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY))

        except Timeout:
            logger.warning("Timeout @%s (attempt %d)", username, attempt)
            proxy_manager.report_failure(proxy_dict)
            time.sleep(min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY))

        except Exception as e:
            logger.error("Unexpected error @%s: %s", username, e)
            return STATUS_UNKNOWN

    logger.warning("All retries exhausted for @%s", username)
    return STATUS_UNKNOWN


def _stats_sync(username: str, timeout: int = 15) -> Optional[dict]:
    url = f"https://www.instagram.com/{username}/"
    proxy_dict = proxy_manager.get()
    try:
        resp = requests.get(
            url, headers=_headers(), proxies=proxy_dict,
            timeout=timeout, allow_redirects=True, verify=False,
        )
        if resp.status_code != 200:
            return None
        html = resp.text

        meta = re.search(
            r'([\d,\.]+[KMB]?)\s+Followers?,\s*([\d,\.]+[KMB]?)\s+Following',
            html, re.IGNORECASE
        )
        if meta:
            return {"followers": _parse_count(meta.group(1)),
                    "following": _parse_count(meta.group(2))}

        f1 = re.search(r'"edge_followed_by"\s*:\s*\{"count"\s*:\s*(\d+)\}', html)
        f2 = re.search(r'"edge_follow"\s*:\s*\{"count"\s*:\s*(\d+)\}', html)
        if f1 and f2:
            return {"followers": int(f1.group(1)), "following": int(f2.group(1))}
        return None
    except Exception as e:
        logger.error("Stats error @%s: %s", username, e)
        return None


# ── Async wrappers ────────────────────────────────────────────────────────────

async def check_account(username: str, timeout: int = 15) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _check_sync, username, timeout)


async def get_profile_stats(username: str, timeout: int = 15) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _stats_sync, username, timeout)


async def check_accounts_batch(usernames: list[str], delay: float = 1.0) -> dict[str, str]:
    """Check up to 5 accounts in parallel."""
    semaphore = asyncio.Semaphore(5)

    async def _one(u: str) -> tuple[str, str]:
        async with semaphore:
            s = await check_account(u)
            await asyncio.sleep(delay)
            return u, s

    tasks = [asyncio.create_task(_one(u)) for u in usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = {}
    for r in results:
        if isinstance(r, Exception):
            logger.error("Batch error: %s", r)
        else:
            u, s = r
            out[u] = s
    return out


def _parse_count(raw: str) -> int:
    raw = raw.strip().replace(",", "").upper()
    try:
        if raw.endswith("M"): return int(float(raw[:-1]) * 1_000_000)
        if raw.endswith("K"): return int(float(raw[:-1]) * 1_000)
        return int(float(raw))
    except ValueError:
        return 0
