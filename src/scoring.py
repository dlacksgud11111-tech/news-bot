"""키워드 기반 1차 선별.

모델 호출은 돈이 드니까, 그 전에 싸고 빠른 규칙으로 후보를 확 줄인다.
제목에 걸린 키워드는 본문에 걸린 것보다 2배로 친다.
"""

from __future__ import annotations

import re

WEIGHTS = {"strong": 3, "medium": 2, "weak": 1}
WATCHLIST_BONUS = 4


def _contains(haystack: str, needle: str) -> bool:
    """영문은 단어 경계로, 한글은 부분 문자열로 매칭."""
    n = needle.lower()
    if re.fullmatch(r"[a-z0-9 .\-]+", n):
        return re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", haystack) is not None
    return n in haystack


def _alias_map(cfg: dict) -> dict[str, str]:
    """표기 변형 -> 대표 회사명. '효성重' 과 '효성중공업' 을 한 회사로 묶는다."""
    watchlist = cfg.get("watchlist") or {}
    out: dict[str, str] = {}
    for group in ("domestic", "overseas"):
        for name in watchlist.get(group) or []:
            out[name] = name
    for canon, variants in (watchlist.get("aliases") or {}).items():
        out[canon] = canon
        for v in variants or []:
            out[v] = canon
    return out


def companies_in(text: str, cfg: dict) -> list[str]:
    """본문/제목에 등장하는 회사들의 대표명 (중복 없이)."""
    t = text.lower()
    found: list[str] = []
    for variant, canon in _alias_map(cfg).items():
        if canon not in found and _contains(t, variant):
            found.append(canon)
    return found


def score(item: dict, cfg: dict) -> tuple[int, list[str]]:
    """(점수, 걸린 키워드들). 블록리스트에 걸리면 (-1, [사유])."""
    title = item["title"].lower()
    body = (item.get("summary") or "").lower()

    for bad in cfg.get("blocklist") or []:
        if _contains(title, bad):
            return -1, [f"blocklist:{bad}"]

    total = 0
    hits: list[str] = []

    for variant, canon in _alias_map(cfg).items():
        if _contains(title, variant):
            total += WATCHLIST_BONUS
            hits.append(f"★{canon}")
        elif _contains(body, variant):
            total += WATCHLIST_BONUS // 2
            hits.append(canon)

    keywords = cfg.get("keywords") or {}
    for tier, weight in WEIGHTS.items():
        for kw in keywords.get(tier) or []:
            if _contains(title, kw):
                total += weight * 2
                hits.append(kw)
            elif _contains(body, kw):
                total += weight
                hits.append(kw)

    return total, hits


def shortlist(items: list[dict], cfg: dict) -> list[dict]:
    """점수 순으로 정렬해 상위 후보만 남긴다."""
    limits = cfg.get("limits") or {}
    min_score = limits.get("min_score", 5)
    cap = limits.get("max_candidates_per_run", 20)

    scored = []
    for it in items:
        s, hits = score(it, cfg)
        if s < min_score:
            continue
        it = dict(it, _score=s, _hits=hits[:8])
        scored.append(it)

    scored.sort(key=lambda x: (x["_score"], x["published"] or 0), reverse=True)
    return scored[:cap]
