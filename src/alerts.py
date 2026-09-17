"""키워드 알람 — AI를 쓰지 않는 즉시 경보.

규칙은 단순하다. 제목에 (관심 회사)와 (사건을 뜻하는 단어)가 함께 있으면 울린다.
'수주' 하나만으로 울리면 하루 수십 건이 오고, 회사명만으로 울리면 증권사 리포트까지
다 온다. 둘을 곱해야 "무슨 일이 실제로 벌어졌다"에 가까워진다.

정책·통상처럼 회사명이 안 붙는 사건은 require_company: false 로 따로 뺀다.
"""

from __future__ import annotations

import re

import clipping
from scoring import _contains, companies_in


def _hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if _contains(text, w)]


def match(item: dict, cfg: dict) -> dict | None:
    """알람 대상이면 {rule, company, words} 를, 아니면 None.

    제목만 본다. 본문까지 보면 스쳐 지나가듯 언급된 단어에도 울린다.
    """
    title = item["title"].lower()

    for bad in cfg.get("blocklist") or []:
        if _contains(title, bad):
            return None

    # 업종 관문. '착수', '준공' 같은 단어는 어느 분야에나 쓰여서
    # 이것 없이는 우분 고체연료화나 도시재생 준공까지 알람이 울린다.
    if not clipping.sector_of(item, cfg):
        return None

    companies = companies_in(item["title"], cfg)

    for rule in (cfg.get("alerts") or {}).get("rules") or []:
        words = _hits(title, rule.get("words") or [])
        if not words:
            continue
        if rule.get("require_company", True):
            if not companies:
                continue
            return {"rule": rule["name"], "company": companies[0],
                    "companies": companies, "words": words}
        return {"rule": rule["name"], "company": companies[0] if companies else "",
                "companies": companies, "words": words}
    return None


def pick(items: list[dict], cfg: dict, store, limit: int) -> list[tuple[dict, dict]]:
    """알람 후보를 고른다. 최신 것부터, 이미 울린 건과 쿨다운 중인 건 제외."""
    cooldown = (cfg.get("alerts") or {}).get("cooldown_hours", 4)
    out: list[tuple[dict, dict]] = []
    fired: set[str] = set()  # 이번 실행 안에서도 같은 (회사, 규칙)은 한 번만

    for item in items:
        if len(out) >= limit:
            break
        if store.already_alerted(item["link"], item["title"]):
            continue
        m = match(item, cfg)
        if not m:
            continue

        key = (m["company"] or "") + "|" + m["rule"]
        if m["company"] and (key in fired or store.in_cooldown(m["company"], m["rule"], cooldown)):
            continue

        fired.add(key)
        out.append((item, m))
    return out
