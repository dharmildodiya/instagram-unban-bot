"""
checker.py — Async Instagram account status checker.

Uses aiohttp for async HTTP requests.
Status codes:
  200  → active
  404  → banned / not found
  429  → rate limited (unknown)
  other → unknown
"""

import asyncio
import logging
import random
import re
import time
from typing import Optional

import aiohttp

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

# Backoff settings
MAX_RETRIES   = 3
BASE_DELAY    = 2.0   # seconds
MAX_DELAY     = 30.0  # seconds


def _headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


async def check_account(username: str, timeout: int = 15) -> str:
    """
    Check account status with retry + exponential backoff.
    Returns STATUS_ACTIVE, STATUS_BANNED, or STATUS_UNKNOWN.
    """
    url = f"https://www.instagram.com/{username}/"

    for attempt in range(1, MAX_RETRIES + 1):
        proxy_dict = proxy_manager.get()
        proxy_url  = proxy_dict.get("https") if proxy_dict else None

        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                connector=connector,
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                async with session.get(
                    url,
                    proxy=proxy_url,
                    allow_redirects=True,
                ) as resp:
                    status_code = resp.status
                    final_url   = str(resp.url)

                    logger.info(
                        "Check @%s → HTTP %s (proxy: %s, attempt %d/%d)",
                        username, status_code,
                        (proxy_url[:30] + "...") if proxy_url else "none",
                        attempt, MAX_RETRIES
                    )

                    if status_code == 200:
                        proxy_manager.report_success(proxy_dict)
                        # Redirect to login = blocked/challenge, not a real 200
                        if "accounts/login" in final_url or "challenge" in final_url:
                            return STATUS_UNKNOWN
                        return STATUS_ACTIVE

                    if status_code == 404:
                        proxy_manager.report_success(proxy_dict)
                        return STATUS_BANNED

                    if status_code == 429:
                        logger.warning("Rate limited on @%s (attempt %d)", username, attempt)
                        proxy_manager.report_failure(proxy_dict)
                        await _backoff(attempt)
                        continue

                    # Other unexpected codes
                    logger.warning("Unexpected HTTP %s for @%s", status_code, username)
                    return STATUS_UNKNOWN

        except asyncio.TimeoutError:
            logger.warning("Timeout on @%s (attempt %d/%d)", username, attempt, MAX_RETRIES)
            proxy_manager.report_failure(proxy_dict)
            await _backoff(attempt)

        except aiohttp.ClientProxyConnectionError as e:
            logger.warning("Proxy error on @%s: %s (attempt %d)", username, e, attempt)
            proxy_manager.report_failure(proxy_dict)
            await _backoff(attempt)

        except aiohttp.ClientError as e:
            logger.warning("Client error on @%s: %s (attempt %d)", username, e, attempt)
            proxy_manager.report_failure(proxy_dict)
            await _backoff(attempt)

        except Exception as e:
            logger.error("Unexpected error checking @%s: %s", username, e)
            return STATUS_UNKNOWN

    logger.warning("All retries exhausted for @%s — returning unknown", username)
    return STATUS_UNKNOWN


async def get_profile_stats(username: str, timeout: int = 15) -> Optional[dict]:
    """
    Scrape followers/following from profile page.
    Returns {"followers": int, "following": int} or None.
    """
    url = f"https://www.instagram.com/{username}/"
    proxy_dict = proxy_manager.get()
    proxy_url  = proxy_dict.get("https") if proxy_dict else None

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            connector=connector,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(url, proxy=proxy_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

        # Strategy 1: meta description
        meta = re.search(
            r'([\d,\.]+[KMB]?)\s+Followers?,\s*([\d,\.]+[KMB]?)\s+Following',
            html, re.IGNORECASE
        )
        if meta:
            return {
                "followers": _parse_count(meta.group(1)),
                "following": _parse_count(meta.group(2)),
            }

        # Strategy 2: JSON blob
        f1 = re.search(r'"edge_followed_by"\s*:\s*\{"count"\s*:\s*(\d+)\}', html)
        f2 = re.search(r'"edge_follow"\s*:\s*\{"count"\s*:\s*(\d+)\}', html)
        if f1 and f2:
            return {"followers": int(f1.group(1)), "following": int(f2.group(1))}

        return None

    except Exception as e:
        logger.error("Stats error @%s: %s", username, e)
        return None


async def check_accounts_batch(usernames: list[str], delay: float = 1.0) -> dict[str, str]:
    """
    Check multiple accounts concurrently with a semaphore to limit parallelism.
    Returns {username: status}.
    """
    semaphore = asyncio.Semaphore(5)  # max 5 concurrent checks

    async def _check_one(u: str) -> tuple[str, str]:
        async with semaphore:
            status = await check_account(u)
            await asyncio.sleep(delay)
            return u, status

    tasks = [asyncio.create_task(_check_one(u)) for u in usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = {}
    for r in results:
        if isinstance(r, Exception):
            logger.error("Batch check error: %s", r)
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


async def _backoff(attempt: int):
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    jitter = random.uniform(0, delay * 0.2)
    await asyncio.sleep(delay + jitter)
