"""RSS 수집. 여러 피드를 동시에 긁어 표준 형태의 기사 목록으로 돌려준다."""

from __future__ import annotations

import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}

# 이보다 오래된 기사는 무시 (봇을 처음 켰을 때 과거 기사가 쏟아지는 것 방지)
MAX_AGE_HOURS = 30


def _fetch_feed(url: str, timeout: int = 20) -> list[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("피드 실패 %s: %s", url, e)
        return []

    parsed = feedparser.parse(r.content)
    out = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue

        published = None
        for field in ("published_parsed", "updated_parsed"):
            if e.get(field):
                published = time.mktime(e[field])
                break

        source = ""
        if e.get("source") and isinstance(e["source"], dict):
            source = e["source"].get("title", "")
        if not source:
            source = parsed.feed.get("title", "") or urllib.parse.urlparse(link).netloc

        out.append(
            {
                "title": title,
                "link": link,
                "source": source,
                "published": published,
                "summary": (e.get("summary") or "")[:2000],
                "feed": url,
            }
        )
    return out


def google_news_url(q: str, hl: str, gl: str, ceid: str) -> str:
    params = urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid})
    return f"https://news.google.com/rss/search?{params}"


def collect(cfg: dict, max_workers: int = 12) -> list[dict]:
    """설정의 모든 소스를 병렬로 수집 → 최신순 정렬된 기사 리스트."""
    urls: list[str] = []
    feeds = cfg.get("feeds") or {}
    for group in ("global", "korea"):
        urls.extend(feeds.get(group) or [])
    for g in cfg.get("google_news") or []:
        urls.append(google_news_url(g["q"], g.get("hl", "ko"), g.get("gl", "KR"),
                                    g.get("ceid", "KR:ko")))

    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_feed, u): u for u in urls}
        for fut in as_completed(futures):
            try:
                items.extend(fut.result())
            except Exception as e:  # 한 피드가 죽어도 전체는 계속
                log.warning("피드 예외 %s: %s", futures[fut], e)

    cutoff = time.time() - MAX_AGE_HOURS * 3600
    fresh = [i for i in items if i["published"] is None or i["published"] >= cutoff]

    # 같은 URL 이 여러 피드에 걸린 경우 하나만
    seen, uniq = set(), []
    for i in sorted(fresh, key=lambda x: x["published"] or 0, reverse=True):
        if i["link"] in seen:
            continue
        seen.add(i["link"])
        uniq.append(i)

    log.info("피드 %d개에서 기사 %d건 수집 (신선 %d건)", len(urls), len(items), len(uniq))
    return uniq
