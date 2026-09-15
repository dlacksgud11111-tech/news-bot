"""config.yaml 의 모든 피드가 실제로 살아있는지 점검.

    python scripts/check_feeds.py

죽은 피드는 config.yaml 에서 지우거나 다른 주소로 바꾸세요.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from sources import HEADERS, google_news_url  # noqa: E402


def check(url: str) -> tuple[str, str, int]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return url, f"HTTP {r.status_code}", 0
        n = len(feedparser.parse(r.content).entries)
        return url, ("OK" if n else "빈 피드"), n
    except requests.RequestException as e:
        return url, type(e).__name__, 0


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    urls = []
    for group in ("global", "korea"):
        urls += (cfg.get("feeds") or {}).get(group) or []
    for g in cfg.get("google_news") or []:
        urls.append(google_news_url(g["q"], g.get("hl", "ko"), g.get("gl", "KR"),
                                    g.get("ceid", "KR:ko")))

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(check, urls))

    bad = 0
    for url, status, n in results:
        mark = "✅" if status == "OK" else "❌"
        if status != "OK":
            bad += 1
        print(f"{mark} {n:>4}건  {status:<20} {url[:90]}")

    print(f"\n총 {len(results)}개 중 {len(results) - bad}개 정상, {bad}개 문제")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
