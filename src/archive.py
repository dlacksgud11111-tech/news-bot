"""텔레그램 DM 으로 들어온 것 → 옵시디언 볼트에 아카이브 카드로 저장.

DM 에 넣는 세 가지 모양:
  1) 링크 (+ #태그, 메모)        → 기사 요약 카드를 만들고, 같은 내용을 볼트에도 저장
  2) 다른 채널 글을 '전달'        → 전달된 본문으로 요약 카드를 만들고 볼트에 저장
  3) 링크 없는 메모 (#여행 등)    → AI 없이 해당 폴더 _inbox 에 한 줄로 저장

카드는 곧바로 20_Work/<섹터>/아카이브/YYYY-MM/ 에 들어가 옵시디언에서 바로 보이고,
주제 페이지·기업 페이지와의 연결은 밤에 Claude 가 00_Inbox/_대기열.md 를 보고 한다.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import vault

TAG_RE = re.compile(r"(?<![\w/])#([0-9A-Za-z가-힣_]+)")
URL_RE = re.compile(r"https?://\S+")


def parse(text: str) -> tuple[list[str], str]:
    """'#원전 #웨스팅하우스 밸류 근거 https://…' → (['원전','웨스팅하우스'], '밸류 근거')."""
    tags = TAG_RE.findall(text or "")
    memo = URL_RE.sub(" ", text or "")
    memo = TAG_RE.sub(" ", memo)
    memo = re.sub(r"\s+", " ", memo).strip()
    return tags, memo


def route(tags: list[str], model_sector: str = "") -> tuple[str, str]:
    """('work', '원전') 또는 ('personal', '여행'). 내 태그가 모델 판단보다 우선."""
    for t in tags:
        if t in vault.PERSONAL:
            return "personal", vault.PERSONAL[t]
    for t in tags:
        s = vault.norm_sector(t)
        if s:
            return "work", s
    return "work", vault.norm_sector(model_sector) or "기타"


def forward_info(msg: dict) -> tuple[str, str]:
    """전달된 메시지의 (출처 이름, 원문 링크). 전달이 아니면 ('', '')."""
    o = msg.get("forward_origin") or {}
    if not o and not msg.get("forward_from_chat") and not msg.get("forward_sender_name"):
        return "", ""
    chat = o.get("chat") or msg.get("forward_from_chat") or {}
    name = (chat.get("title") or o.get("sender_user_name") or msg.get("forward_sender_name")
            or ((o.get("sender_user") or {}).get("first_name")) or "텔레그램")
    link = ""
    mid = o.get("message_id") or msg.get("forward_from_message_id")
    if chat.get("username") and mid:
        link = f"https://t.me/{chat['username']}/{mid}"
    return name, link


def _yaml_list(xs: list[str]) -> str:
    return "[" + ", ".join(x.replace(",", " ") for x in xs if x) + "]"


def save_card(card: dict, url: str, sector: str, tags: list[str], memo: str,
              origin: str = "", src_type: str = "") -> str | None:
    """요약 카드를 볼트에 저장하고 경로를 돌려준다. 볼트 미설정·실패면 None."""
    if not vault.enabled():
        return None
    t = vault.now()
    title = card.get("title") or "제목 없음"
    src = src_type or vault.source_type(url)
    path = (f"20_Work/{sector}/아카이브/{t:%Y-%m}/"
            f"{t:%Y-%m-%d}_{vault.slug(origin or src, 15)}_{vault.slug(title)}.md")
    all_tags = list(dict.fromkeys([*(card.get("tags") or []), *[x for x in tags if not vault.norm_sector(x)]]))

    lines = [
        "---",
        f"섹터: {sector}",
        f"태그: {_yaml_list(all_tags)}",
        f"출처유형: {src}",
        f"채널/작성자: {origin}",
        f"원문: {url}",
        f"수집일: {t:%Y-%m-%d}",
        f"관련기업: {_yaml_list(card.get('companies') or [])}",
        "입력: 텔레그램 봇",
        "---",
        f"# {card.get('emoji', '')} {title}".strip(),
        "",
        "## 한 줄 요약",
        card.get("lead") or "",
        "",
        "## 핵심 내용",
        *[f"- {d}" for d in card.get("details") or []],
        "",
        "## 인용할 만한 문장",
    ]
    quotes = [q for q in card.get("quotes") or [] if q.strip()]
    # 인용 출처: 채널명이 있으면 그것, 없으면 매체 도메인 + 수집일 (예: yna.co.kr, 2026-10-12)
    host = urlparse(url).netloc.removeprefix("www.").removeprefix("m.") if url else ""
    who = f"{origin or host or src}, {t:%Y-%m-%d}"
    lines += [f'> "{q.strip().strip(chr(34) + "“”")}" — {who}' for q in quotes] or ["(원문에서 그대로 옮길 만한 문장 없음)"]
    lines += [
        "",
        "## 왜 저장했나",
        memo or "(메모 없음)",
        "",
        "## 관련 페이지",
        "(밤 정리 때 Claude 가 연결)",
        "",
    ]
    if not card.get("_body_ok", True):
        lines.insert(lines.index("## 한 줄 요약"), "> ⚠️ 본문 추출이 불완전했던 카드 — 원문 확인 필요\n")

    if vault.put(path, "\n".join(lines), f"archive: {sector} · {title[:40]}"):
        vault.queue_line(f"카드·{sector}", path, memo[:60])
        return path
    return None


def save_personal(folder: str, text: str, url: str, memo: str) -> str | None:
    """개인 태그 메모는 요약 없이 해당 폴더 _inbox.md 에 한 줄로 쌓는다."""
    if not vault.enabled():
        return None
    t = vault.now()
    path = f"30_Personal/{folder}/_inbox.md"
    line = f"- [ ] {t:%Y-%m-%d %H:%M} · {memo or '(메모 없음)'}" + (f" · {url}" if url else "")
    if vault.put(path, line, f"personal: {folder}", append=True):
        vault.queue_line(f"개인·{folder}", path, (memo or url)[:60])
        return path
    return None


def save_memo(text: str, tags: list[str]) -> str | None:
    """링크도 개인 태그도 없는 그냥 메모. 받아만 두고 밤에 Claude 가 자리를 찾는다."""
    if not vault.enabled():
        return None
    t = vault.now()
    path = "00_Inbox/메모.md"
    tag_s = " ".join("#" + x for x in tags)
    line = f"- [ ] {t:%Y-%m-%d %H:%M} {tag_s} {text}".replace("  ", " ")
    if vault.put(path, line, "memo", append=True):
        return path
    return None
